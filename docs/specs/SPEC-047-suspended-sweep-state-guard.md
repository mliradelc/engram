# SPEC-047 — A suspended organize sweep must not clobber a job another path completed

## The incident (job 13, 2026-11-24, measured from live logs + DB)

A TV job's organize sweep (`_finalize_tv_if_resolved`, finalization_coordinator.py
~line 2253) was suspended for **9.5 hours** inside its inline encode await:

```
2026-11-23 16:50:40  organize: S01E03 moved            (sweep started, HTTP request still open)
2026-11-23 16:50:50  encode started (AV1, 16.1 GB)     -> line 2433: await asyncio.to_thread(encode_hook.after_organize, ...)
2026-11-23 23:17-23:50  a SEPARATE request (review/batch) organized the 3 extras,
                        set them COMPLETED + organized_to, and transitioned the
                        job COMPLETED at 23:50:39
2026-11-24 02:26:18  encode finished; the 9.5 h-old suspended sweep RESUMED
                     -> its in-memory title objects were the 16:50 snapshot (extras
                        still state=REVIEW, output_filename = old staging paths)
                     -> the line 2329 skip-check (COMPLETED + organized_to) saw STALE
                        state, so all 3 extras were re-processed
                     -> sources were already moved -> 3x "Source file not found"
                     -> terminal decision (line 2459+) transitioned the job
                        ORGANIZING -> REVIEW_NEEDED with "3 title(s) failed to organize"
                     -> a completed job was demoted into a broken review state
```

Verified facts: no POST/PUT in the access log at 02:26 (the sweep was a continuation
of the 16:50 request, not a new one); the encode hook itself never touches job state
(0 references to finalize/transition in encode_hook.py); the data was never lost
(all four files verified at their destinations).

## Two defects, both in `_finalize_tv_if_resolved`

**D1 — stale ORM objects across a multi-hour await.** The sweep loads titles once
(line ~2307) and iterates; between iterations it may await an encode for hours
(line ~2433). The per-title skip-check (line ~2329) then reads 16:50 state for
titles that a concurrent request completed at 23:17.

**D2 — terminal decision without re-reading the job.** The end-of-sweep state
decision (line ~2459-2487) transitions based on the sweep's own counters, without
re-reading the job's current state. If another path completed the job during the
await, the sweep demotes it (ORGANIZING -> REVIEW_NEEDED) or re-completes it.

Additionally (cosmetic, same symptom family): completing a job via the sweep leaves
stale `review_reason` text on the COMPLETED row, so a healthy completed job still
displays "N title(s) failed to organize" in the API/UI.

## The fix — `backend/app/services/finalization_coordinator.py` only

**Audit-verified facts (checked at the lines, 2026-11-24):**

- **THERE ARE TWO SWEEPS, and BOTH await encodes mid-loop.**
  - Sweep A: `_finalize_tv_if_resolved` (def ~line 2253); its encode await is at
    line ~2437 (`encode_hook.after_organize`). This is the one that flapped job 13.
  - Sweep B: `process_matched_titles` (def line 2561); it has its OWN encode await
    at line ~2701. Its terminal decision is line ~2735
    (`if not unresolved and conflict_count == 0 and failure_count == 0: await
    self._complete_tv_job(...)`). It does NOT demote a completed job (no
    transition_to_review in that block), but it CAN re-organize stale titles and
    re-run encodes after a concurrent save. The same D1 refresh belongs there.
  - Do not confuse the two: both iterate titles and both commit per title.
- `_complete_tv_job` (line ~2250 area, verified) sets `progress_percent`,
  `error_message = None`, `final_path`, then transitions COMPLETED. It does NOT
  clear `review_reason` — that is the cosmetic defect to fix (see below).

### Fix D1: refresh each title before the skip-check

In the sweep loop (line ~2326, `for disc_title in resolved_titles:`), at the TOP of
the loop body — before the `output_filename` check and the skip-check — re-read the
title from the DB:

```python
        for disc_title in resolved_titles:
            # The encode await below can suspend this sweep for hours; a
            # concurrent save may have completed this title since the list was
            # loaded. Re-read it so the skip-check sees current state, not the
            # pre-encode snapshot.
            await session.refresh(disc_title)
            if disc_title.output_filename and disc_title.matched_episode != "skip":
                ...
```

`session.refresh` issues one SELECT per title (4-6 titles per disc) — negligible
next to a CIFS move. Place it so it runs on EVERY iteration (i.e. before the
`if disc_title.output_filename` line), because the await happens mid-iteration
and the NEXT iteration's object is the one that can be stale.

### Fix D2: re-read the job before the terminal decision

Immediately before the `failure_total = len(...)` decision block (line ~2459),
re-read the job and bail if it is no longer ORGANIZING:

```python
        # The encode await above can suspend this sweep for hours. If another
        # path (a concurrent review save) already moved the job to a terminal
        # state in that window, this sweep is stale: its counters describe a
        # world that no longer exists. Do not demote or re-complete the job;
        # the other path owns its state now.
        await session.refresh(job)
        if job.state != JobState.ORGANIZING:
            logger.info(
                f"Job {job_id}: sweep finished after job reached "
                f"{job.state.value} (concurrent save during encode await) — "
                f"skipping terminal decision"
            )
            return
```

This guard makes the zombie-request scenario a harmless no-op: the suspended
sweep finishes its (now correct, refreshed) loop, then refuses to touch a job
that is already COMPLETED/FAILED.

### Cosmetic: clear stale review_reason when a job completes

Verified: `_complete_tv_job` sets `progress_percent`, `error_message = None`,
`final_path` and transitions COMPLETED — but NOT `review_reason`. Add
`job.review_reason = None` inside `_complete_tv_job` (it is the single funnel
both sweeps' success paths go through), so no completed job ever carries a
stale failure reason. Do NOT touch the state machine's transition methods.

## Files allowed to change

| File | What |
|------|------|
| `backend/app/services/finalization_coordinator.py` | The two guards + cosmetic clear |
| the existing test file covering `_finalize_tv_if_resolved` (locate with `grep -rln "_finalize_tv_if_resolved" backend/tests/`) | New tests |

Do **not** modify: `encode_hook.py`, `job_manager.py`, `organizer.py`, `routes.py`,
`job_state_machine.py`, any frontend file.

## Tests required

`grep -rln "_finalize_tv_if_resolved" backend/tests/` currently resolves only to
`backend/tests/unit/test_episode_ordering_service.py` — use it (or, if its fixtures
do not exercise the sweep loop well, a sibling that constructs the coordinator the
same way; state which file you picked and why). Required tests:

### Test 1 — a title completed by a concurrent save during the encode await is skipped
- Job with 2 TV titles. Title A organizes successfully and triggers
  `encode_hook.after_organize` — mock it with an `AsyncMock` wrapping an
  `asyncio.Event` the test controls (so the await blocks until the test says so).
- Title B: after the sweep enters the encode await, update the DB (new session or
  the same, per the file's existing fixture pattern) to
  `state=COMPLETED, organized_to=<path>` — simulating a concurrent save.
- Release the event.
- Assert: title B was NOT re-organized (its organizer call count is 0), the job
  ends COMPLETED, no "failed to organize" reason is set.

### Test 2 — a job completed concurrently is not demoted
- Same setup as Test 1, but additionally set the JOB row to `state=COMPLETED`
  during the encode await (the concurrent save completed the whole job).
- Release the event.
- Assert: job.state is still COMPLETED (not REVIEW_NEEDED),
  `job.review_reason` is None, and no transition to review happened.

### Test 3 — normal single-save path unchanged (regression)
- The file's existing sweep tests must still pass unmodified. If the refresh
  calls break a mock-based fixture (e.g. a MagicMock session without `refresh`),
  add `refresh=AsyncMock()` to that fixture's session mock — that is a fixture
  adaptation, not a test-logic change. Do not weaken any existing assertion.

### Test 4 — sweep B (process_matched_titles) refreshes titles the same way
- Same stale-during-encode-await setup, but drive it through
  `process_matched_titles` instead of `_finalize_tv_if_resolved`.
- Assert: the concurrently-completed title is not re-organized and no encode is
  re-queued for it.

## Constraints

- **Do not make the encode await fire-and-forget.** The await-after-commit ordering
  is deliberate (line ~2403 comment: holding the session across the encode left a
  title at REVIEW with a stale file_exists flag). Keep the await; guard its
  resumption.
- **Do not change `session.refresh` semantics globally** — only the named call
  sites (per-title in BOTH sweep loops + per-job in sweep A's terminal block).
- **Do not touch the movie path** (SPEC-046 covers it; already committed).
- **Do not modify `organizer.py`** — the organizer already moves (not copies); the
  bug is the sweep's stale view, not the organizer.

## Verification (the agent runs these after the change)

```bash
cd backend && uv sync --frozen
uv run pytest tests/unit/ -q -x
# Quote the summary line. Also run ruff check + ruff format --check on the
# changed files and quote both.
```

## Report anything in this spec you found to be wrong about the code

Before implementing, read the actual sweeps (A: ~2253-2490, B: ~2561-2744) and verify:
1. The loop variable name and the exact line of the skip-check in each sweep.
2. That `job` is the same ORM object the decision block uses (so
   `session.refresh(job)` affects it) — if the decision block re-fetches the job,
   adjust the guard to match the real code.
3. Whether the test file's session mocks already have a `refresh` method.
4. In sweep B, whether its per-title block even HAS a skip-check (it may organize
   titles that are already COMPLETED — check the loop at ~2600-2680). If it has
   none, the D1 refresh there must ALSO skip already-organized titles (add the
   same `COMPLETED + organized_to` test), and say so in the report.

Items already verified by the orchestrator (do not re-litigate, just confirm the
lines still match): two sweeps, both await `encode_hook.after_organize` mid-loop
(A at ~2437, B at ~2701); `_complete_tv_job` clears `error_message` but not
`review_reason`; sweep B's terminal block (line ~2735) cannot demote a job to
review but CAN re-complete it.

Fold corrections into the implementation and list each one in the final report.
