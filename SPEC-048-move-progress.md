# SPEC-048 — Verified move with byte-accurate transfer progress

Status: NOT STARTED. Branch: `feat/encode-timeout-24h` (current HEAD `6baa8b5`).
Deployed baseline: `v0.45.2-fork.7`.

## The problem this fixes (measured, 2026-11-26)

A Blu-ray title (39,531,675,648 B) was confirmed on the review screen and organized into the
curator staging root.  The organize step took long enough that the *client* (openresty in front of
Encram) timed out with **504 Gateway Time-out** after ~25 minutes.  The transfer itself completed
correctly on the NAS, but:

1. The user saw a 504 error and believed the save failed.
2. No progress was reported at any point during the move — the UI sat on the review screen.
3. Nothing verified the move afterwards: `organized_to` was recorded, but the byte count/size of
   the destination was never checked against the source before the source was unlinked.

Root cause in code: `move_media_file()` (`backend/app/core/organizer.py:466`) does
`os.replace()` for same-device moves (instant, no progress needed), but for a cross-device move
(`errno.EXDEV` — exactly the `/staging` → CIFS case) it falls through to a single blocking
`shutil.copy2()` call with no progress callback and no post-copy size verification.

## Deliverables

### D1 — Byte-progressing, verified copy in `move_media_file()`

File: `backend/app/core/organizer.py`

Change the copy fallback so it:

- copies in fixed-size chunks (8 MiB) rather than one `shutil.copy2()`;
- invokes an optional `progress_callback(bytes_done: int, total_bytes: int)` at most once per
  1% or once per 2 s, whichever is *less* frequent, and always once at 100%;
- emits a callback with `bytes_done == 0` and the real `total_bytes` **before** the first byte is
  written, so the UI can render 0/39.5 GB immediately;
- **verifies the destination byte length equals the source byte length before unlinking the
  source.**  On mismatch: unlink the partial destination, raise `OSError` naming both sizes.  The
  existing rollback logic for a failed `os.unlink(src)` must be kept exactly as it is.
- preserves `shutil.copy2`'s metadata behaviour (use `shutil.copyfileobj` over opened fds and then
  `shutil.copystat(src, dest)`), and preserves the retry/`EXDEV`-shortcut logic above it
  unchanged for the same-device rename path.

Signature becomes:

```python
def move_media_file(
    src: Path,
    dest: Path,
    *,
    attempts: int = 3,
    delay: float = 0.5,
    progress_callback: Callable[[int, int], None] | None = None,
) -> None:
```

`progress_callback=None` must behave exactly as today (all existing callers keep working).

### D2 — Thread the callback through the four organize entry points

Files: `backend/app/core/organizer.py`, `backend/app/services/finalization_coordinator.py`

Add an optional keyword `progress_callback` to:

- `organize_movie(...)`
- `organize_tv_episode(...)`
- `organize_tv_extras(...)`
- `organize_movie_extras(...)`
- and the `movie_organizer.organize` / `tv_organizer.organize` wrapper methods that delegate to
  them (around lines 861, 1152, 1183).

Each must pass it to `move_media_file` (and `_move_extras`, which should accept and forward it too).
Defaults are `None`; do not change any existing positional signature — new parameter is
keyword-only.

In `finalization_coordinator.py`, all four organize call sites (movie path ~line 1950,
`organize_movie_extras` ~2121, `apply_review` TV path ~2352/2370, batch/extras path ~1595/1609,
`process_matched_titles` ~2642/2658) must pass a callback that emits the D3 event.  Build that
callback once per job with a small helper, e.g.

```python
def _move_progress_reporter(self, job_id: int, title_index: int, source_name: str):
    def report(done: int, total: int) -> None:
        ...  # throttled emit, see D3
    return report
```

Because the organizers run under `asyncio.to_thread`, the callback fires on a worker thread and
must hand the event to the loop with `asyncio.run_coroutine_threadsafe(...)`, exactly as
`job_manager.py:3421` does for `broadcast_backup_progress`.  Capture the running loop *before*
entering `to_thread` (there is no running loop inside the worker thread).

### D3 — New WebSocket message `move_progress`

Files: `backend/app/api/websocket.py`, `backend/app/services/event_broadcaster.py`

Add `broadcast_move_progress(...)` to `ConnectionManager` and `EventBroadcaster`, modelled
precisely on `broadcast_backup_progress` (websocket.py:308, event_broadcaster.py:91) — flat
message, every key always present, docstring explaining why.

```json
{
  "type": "move_progress",
  "job_id": 15,
  "title_index": 1,
  "source_name": "The Matrix_t01.mkv",
  "current_bytes": 12345678,
  "total_bytes": 39531675648,
  "speed": "42.1 MB/s",
  "eta": 912,
  "done": false
}
```

- `speed` / `eta` are computed by the backend from elapsed time between samples (unlike
  `backup_progress`, where MakeMKV gives no byte rate — here we have real bytes and real time, so
  they are populated; null only for the first sample).
- `done: true` is sent exactly once per title, after the destination is verified, with
  `current_bytes == total_bytes`.
- Throttle to at most one message per title per 2 s (the copy is done in chunks, so a naive
  callback would flood the socket for a 40 GB file).

### D4 — Frontend: progress bar with GB transferred

Files: `frontend/src/types/index.ts`, `frontend/src/app/hooks/useJobManagement.ts`,
`frontend/src/app/components/DiscCard.tsx`

- Add `MoveProgressMessage` interface beside `BackupProgressMessage` (index.ts:205) with a
  docstring in the same voice.
- `useJobManagement.ts`: add `applyMoveProgress(...)` pure helper (beside `applyBackupProgress` at
  line 63, exported for test) and a `case 'move_progress'` in the listener switch (beside
  `'backup_progress'` at line 688).  State lives in a new `moveProgress: Map<number, MoveProgress>`
  keyed by `job_id`; delete the entry on `done`.
- `DiscCard.tsx`: when a move is in flight for this disc, render a third progress block below the
  backing-up block (line ~713) using `SvProgressBar` with `color="green"` and label
  `VERIFIED MOVE`, plus a readout line showing bytes transferred in GB:
  `14.7 GB / 36.8 GB · 42.1 MB/s · 15m 12s left`, and on completion the text
  `✓ VERIFIED — 36.8 GB transferred`.  Format with 1 decimal place; use binary-vs-decimal
  consistently (pick GB = 10^9 and say so in a comment).
- Add a `moveProgress` field to the `Disc` view type and populate it in
  `types/adapters.ts` — **no**, do not put transient progress in the REST adapter: the adapter
  rebuilds from the job list on every fetch and would blank the bar.  Pass it to `DiscCard` as a
  separate prop (or a sibling map read in the parent) so a WS message survives a refetch.

### D5 — Client-side timeout is not a failure

The review POST already blocks for the duration of the move. Two changes:

1. In the frontend, the save/review request must not surface a generic network error while move
   progress is still arriving. If a request fails but `moveProgress` for that job is non-null or
   completed within the last 30 s, show "Still working — check the progress bar" rather than an
   error toast.
2. Document in `docs/` (and in the docstring of the review route) that a 504 from a proxying
   gateway during organize does **not** mean the save failed, because the move is verified
   server-side. Point at the verification.

## Constraints (non-negotiable)

- **Do not change the emptiness/settle/cleanup semantics.** `staging_cleanup_policy` behaviour and
  the `on_success` rmtree must be untouched.
- **Do not touch `resolve_conflict`, `FILE_EXISTS` handling, or any naming format.**
- **Do not change the LLM/prompt code.** This spec is organizer + transport + UI only.
- **Do not add a database column or migration.** Progress is in-memory only; a restart loses it,
  which is correct — the move is not resumable.
- **Do not weaken verification to make a test pass.** If a test asserts the old single-call copy,
  rewrite that test to assert chunked copy + verification.
- Keep the module free of new third-party dependencies.

## Tests (required)

Backend (`backend/tests/`, follow existing layout):

1. `move_media_file` same-device: still one `os.replace`, no progress callback fired.
2. `move_media_file` cross-device (monkeypatch `os.replace` to raise `OSError(errno.EXDEV)`):
   - callback receives an initial `(0, total)` sample, at least one intermediate, and a final
     `(total, total)`;
   - source is unlinked, destination exists, sizes equal;
   - with a monkeypatched copy that writes fewer bytes than the source, it raises `OSError`
     mentioning both sizes and leaves **neither** file at `dest`.
3. Throttling: a 100-chunk copy emits far fewer callbacks than 100 (assert a ceiling).
4. `broadcast_move_progress` message shape: every documented key present, flat, no `data` envelope.
5. `apply_review` on a movie with a stub `move_media_file` that fires the callback three times
   emits at least one `move_progress` and one `done` for that job.

Frontend (vitest, beside existing `DiscCard.test.tsx` / hook tests):

6. `applyMoveProgress` folds bytes into a percentage, coalesces a null `speed`/`eta`, and removes
   the entry on `done`.
7. `DiscCard` renders the `VERIFIED MOVE` bar and the GB readout for a disc with move progress.

## Verification before you commit

Run and quote the summary lines:

```
cd backend && uv sync --frozen && uv run pytest tests/unit/ -q
cd frontend && npm ci && npx vitest run
npx tsc --noEmit -p frontend   # or the repo's typecheck target
```

Both must be at or above the count at current HEAD with zero failures — confirm with your own run
before and after; do not adjust a test to reach a number.

## Report back

- The exact `progress_callback` signature you implemented.
- Which of the five organize entry points you threaded it through, with line numbers.
- Anything in this spec you found to be **wrong about the code** — read the files, do not trust my
  line numbers. Say so explicitly rather than silently deviating.
- The measured callback cadence for a simulated 40 GB copy (how many messages, at what interval).
- Do not commit until both suites are green; then commit with
  `git config user.email '8040628+mliradelc@users.noreply.github.com'` set repo-locally.

## Out of scope (state it in your report, do not implement)

- Resumable moves.
- Progress for the pre-rip whole-disc backup (already exists).
- Progress for the AV1/x265 encode (already exists).
- Any change to how the curator picks up staging contents.
