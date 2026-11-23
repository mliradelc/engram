# SPEC-046 — Movie path must honour `always_review`

## Context

Engram's `always_review` config (currently `1` in `app_config`) gates every TV disc in
`REVIEW_NEEDED` after rip+match so Max confirms the episode assignments before anything
moves. The movie path bypasses this gate entirely: `_finalize_ripped_movie` (job_manager.py
line 4336) resolves the feature, tags extras, then goes straight to `ORGANIZING` →
organize → `COMPLETED` with no `always_review` check.

The consequence (observed on job 13, GBR DISC2): a TV disc misidentified as `content_type=movie`
auto-completed, `cleanup_service.on_job_terminal` deleted the staging directory, and every
corrective endpoint (`/review`, `/review/batch`, `/re-identify`) refused the job with
`400 "Job is not awaiting review"`. Max saw "cannot save or process" — the selection was
stuck and the disc stranded.

**The fix:** insert the `always_review` gate at the top of `_finalize_ripped_movie`, after
feature/extras resolution but before the auto-organize. When `always_review` is on, the job
parks in `REVIEW_NEEDED` with all titles pre-filled (machine's best guess). Max confirms in
the review UI → `apply_review` → movie workflow → organize → `COMPLETED`. The files are still
in staging when the confirmation runs (no terminal cleanup has fired), so the save works.

This mirrors the existing TV gate in `finalize_after_matching` (finalization_coordinator.py
line 1240–1251) exactly.

## Files allowed to change

| File | What |
|------|------|
| `backend/app/services/job_manager.py` | The gate itself (one insertion in `_finalize_ripped_movie`) |
| `backend/tests/unit/test_finalize_ripped_movie.py` (or the existing test file for `_finalize_ripped_movie` — locate it with `grep -rln "_finalize_ripped_movie" backend/tests/`) | New tests for the gate |

Do **not** modify: `finalization_coordinator.py`, `organizer.py`, `cleanup_service.py`,
`analyst.py`, `routes.py`, any frontend file, any config model.

## The change — `job_manager.py`, `_finalize_ripped_movie`

**Insertion point:** between the end of the multi-title resolution block and the
`job.state = JobState.ORGANIZING` line. In the current tree (HEAD `48aea12`), that is
between lines 4376 and 4381:

```
4375:                if sent_to_review:
4376:                    return
4377:
4378:            # Single title flow (Standard Movie). Commit ORGANIZING and
4379:            # release the session before the blocking organize so no DB
4380:            # connection is held across it (mirrors the rip itself).
4381:            job.state = JobState.ORGANIZING
```

**Insert after line 4376** (the `return` for the multi-title review case) and **before
line 4378** (the "Single title flow" comment):

```python
            # always_review gate for movies: mirror the TV gate in
            # finalize_after_matching (finalization_coordinator.py ~line 1240).
            # Hold every disc for confirmation before anything moves. The
            # resolution above has already pre-filled is_selected / is_extra
            # on every title, so the review UI opens with the machine's best
            # guess and the user confirms or corrects.
            from app.services.config_service import get_config
            if (await get_config()).always_review:
                logger.info(
                    f"Job {safe_job}: always_review enabled — holding movie for confirmation"
                )
                job.state = JobState.REVIEW_NEEDED
                job.review_reason = (
                    "Manual review is on for every disc — confirm the assignments "
                    "below, then organize."
                )
                job.updated_at = datetime.now(UTC)
                await session.commit()
                await ws_manager.broadcast_job_update(
                    job_id, JobState.REVIEW_NEEDED.value
                )
                return
```

**Why here and not elsewhere:**
- Both the single-title path and the multi-title fall-through (when
  `_resolve_multi_title_movie` returns `False`) converge at this point before the
  auto-organize. One gate covers both.
- `_resolve_multi_title_movie` returning `True` (already parked in review for a version
  pick or an "ask" extras policy) hits the `return` at line 4376 before reaching the gate
  — no double-park.
- The `session` is still open, `job` is loaded, `safe_job` is in scope. All the symbols
  the gate needs (`datetime`, `UTC`, `ws_manager`, `JobState`, `logger`) are already
  imported at module level in `job_manager.py`.
- The three callers of `_finalize_ripped_movie` (rip-end, walk-away B5,
  `identify_from_staging`) all benefit automatically.

**What the gate does NOT do:**
- Does not change `is_selected` / `is_extra` — the resolution already set them.
- Does not organize any files — that happens when Max confirms via `apply_review`.
- Does not affect `always_review=False` (the default) — the existing auto-organize path
  is untouched.

## Tests required

Locate the existing test file for `_finalize_ripped_movie`:
```
grep -rln "_finalize_ripped_movie" backend/tests/
```

Add (or extend) the following tests in that file:

### Test 1: `always_review=True` parks a single-title movie in REVIEW_NEEDED
- Mock `get_config()` to return an object with `always_review=True`.
- Call `_finalize_ripped_movie` with a single selected title that has a valid
  `output_filename`.
- Assert: job.state == `JobState.REVIEW_NEEDED`
- Assert: job.review_reason contains "Manual review"
- Assert: the organizer (`movie_organizer.organize` or `organize_movie`) was **not**
  called.
- Assert: `ws_manager.broadcast_job_update` was called with `REVIEW_NEEDED`.

### Test 2: `always_review=False` (default) auto-completes as before
- Mock `get_config()` to return `always_review=False`.
- Same setup.
- Assert: the organizer **was** called.
- Assert: job.state ends up `COMPLETED` (or `ORGANIZING` if the test stops before the
  post-organize session).

### Test 3: `always_review=True` with multi-title where resolution returns False
- Two titles, `_resolve_multi_title_movie` mocked to return `False` (fell through).
- `always_review=True`.
- Assert: job.state == `REVIEW_NEEDED` (the gate fires after the fall-through).

### Test 4: `always_review=True` with multi-title where resolution returns True
- `_resolve_multi_title_movie` mocked to return `True` (already parked).
- `always_review=True`.
- Assert: the function returns after `_resolve_multi_title_movie` — the gate is not
  reached (no double-park). Verify by asserting the organizer was not called AND the
  review_reason is the one set by `_resolve_multi_title_movie`, not the gate's.

### Test 5: Confirming a parked movie organizes and completes
- Set up the same scenario as Test 1 (job parked in REVIEW_NEEDED).
- Call `apply_review(job_id, title_id, episode_code="S01E01")` (or the movie equivalent
  — check how the movie review flow passes the episode code; for a movie the feature
  title's `output_filename` is the key input, not an episode code).
- Assert: the organizer was called.
- Assert: job.state == `COMPLETED`.
- Assert: the title's `organized_to` is set.

If Test 5 is too coupled to the full `apply_review` machinery (it spans two files), a
lighter assertion is acceptable: assert that after parking, the title's `output_filename`
still points at an existing file (i.e. no cleanup has run), which is the precondition that
makes the confirmation's organize succeed.

## Constraints

- **Do not change the TV path.** The TV gate at `finalize_after_matching` line 1240 already
  works. Leave it alone.
- **Do not change `cleanup_service.py`.** The staging cleanup on terminal states is correct
  behaviour; the bug is that the movie path reaches a terminal state too early.
- **Do not change `organizer.py` or `analyst.py`.** The feature selection logic is correct;
  it just needs a gate after it.
- **Do not change `routes.py`.** The `/review` and `/review/batch` endpoints already work
  correctly for a `REVIEW_NEEDED` movie job.
- **Do not change the default of `always_review`.** It stays `False` in the model. The
  gate only activates when the config is `True`.
- **Preserve the existing auto-complete path for `always_review=False`.** This is the
  upstream Engram behaviour and must not regress.
- **The `from app.services.config_service import get_config` import** should be inside the
  `if` block (lazy import), matching the pattern at line 4642 in
  `_resolve_multi_title_movie`. Do not add it to the module-level imports.

## Verification (the agent runs these after the change)

```bash
cd backend && uv sync --frozen
uv run pytest tests/unit/ -q -x --timeout=120
# Quote the summary line: "N passed, M warnings in Ss"
```

Then run the new tests specifically:
```bash
uv run pytest <test_file>::<new_test_class> -v --timeout=60
```

## Report anything in this spec you found to be wrong about the code

Before implementing, read the actual code at the insertion point and verify:
1. The line numbers are correct (the tree may have shifted slightly).
2. `datetime` and `UTC` are imported at module level in `job_manager.py`.
3. `ws_manager` is a module-level import (not a method argument).
4. `safe_job` is in scope at the insertion point (it is set at line 4361).
5. `get_config` is the correct function name (not `get_db_config` or similar).
6. The `JobState.REVIEW_NEEDED` enum value exists (check `app/models/disc_job.py`).
7. The test file location is correct.

If any of these are wrong, correct the spec in your implementation and note the
discrepancy in your final report.
