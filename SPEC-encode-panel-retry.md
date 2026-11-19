# SPEC — Make the encode panel visible, and add retry-encode

Status: the panel already ships but is effectively invisible; retry does not exist.
Source: work item #5 follow-up (Max: "I don't see like a frontend panel for encoding").

## Problem 1 — the panel exists but cannot be found

`EncodeProgressPanel` is committed, imported in `App.tsx` (line ~522), present in the served
bundle (`encode-progress-panel` is in `assets/index-DUs06I08.js`), and `GET /api/encoding`
returns `[]`. So it renders. It is simply invisible in practice:

- `fontSize: 12`, `fontFamily: sv.mono`, `padding: "10px 12px"`, a 1px ghost border — it reads
  as page chrome, not as a feature.
- It sits below the armed-drive cards and the job list, so on a normal dashboard it is below
  the fold.
- When idle it shows a single faint "Nothing encoding" line, which is easy to scroll past.

**Requirement:** the panel must be obvious. Give it the same visual weight as the other
dashboard cards (a titled section with a real header row, the same card background/border
treatment used by `ArmedDriveCard`/`DiscCard`), and place it **above** the job list so it is
seen without scrolling. Keep the empty state, but make it read as a deliberate state rather
than a stray line.

**Do not** hide the panel when idle. Max's complaint is that he cannot see it; hiding it when
idle would make it appear only during an encode, which is worse.

## Problem 2 — retry encoding for a file that already exists

Today `encode_hook.after_organize()` returns early when `_already_done(src)` is true: a marker
file beside the source records `{size, mtime}` and the hook refuses to re-encode the same
revision. There is no way to force a re-encode from the UI, so a failed or unwanted encode is
permanently stuck.

### Backend

Add one route:

```
POST /api/encoding/retry   {"file": "<absolute path of the source .mkv>"}
  -> 202 {"queued": true, "file": "..."}
  -> 404 {"detail": "..."}   file does not exist
  -> 409 {"detail": "..."}   already encoding this file
```

Behaviour:

1. Resolve and validate the path. It **must** be an existing regular file whose suffix is
   `.mkv`. Reject anything else with 404. Do not accept a path outside the configured library
   roots — compare against `library_tv_path` / `library_movies_path` / `ENGRAM_AV1_STAGING`
   parents, because this route is reachable from the LAN and must not become an arbitrary
   "encode any file on the box" primitive.
2. If the destination path is already in `_ENCODING`, return 409 (the panel already shows it).
3. Delete the marker (`_marker_path(src).unlink(missing_ok=True)`) so `_already_done` no longer
   short-circuits.
4. Re-run the encode off the request thread: `asyncio.get_running_loop()` captured in the
   handler, then `asyncio.to_thread(encode_hook.after_organize, path, loop=loop)`. Return 202
   immediately — an encode is ~80 minutes and must not block the HTTP call.

Add `encode_hook.retry_encode(path, *, loop=None) -> None` holding steps 2–4 so the route stays
thin and the logic is unit-testable without FastAPI.

**Reuse the existing gate.** `retry_encode` must go through the same `_encode_slot` permit and
the same `_ENCODING` registry as a normal encode, so `ENGRAM_AV1_MAX_CONCURRENT` still bounds
total concurrency and the panel shows the retried file. Do not add a second code path.

### Frontend

- Add a **Retry** control per row in the panel. It is only meaningful for a file that is *not*
  currently encoding, so the panel needs a second list.
- Extend `GET /api/encoding` to also return recently-failed encodes, or add
  `GET /api/encoding/failures`. Prefer **extending the existing payload** with a sibling key so
  the panel keeps one fetch:

  ```
  GET /api/encoding -> {"active": [...], "failed": [...]}
  ```

  This is a breaking change to the current `[...]` shape — update
  `encodesFromSnapshot` and the existing tests accordingly. `failed` entries carry
  `{file, destination_folder, reason, failed_at}` and come from an in-memory ring buffer
  (last 20) filled where `transcode_to_av1` returns `None`.
- Render failed rows with the reason and a **Retry** button that POSTs to
  `/api/encoding/retry` and optimistically moves the row back to `active`.
- Show a toast on 409 ("Already encoding") and on a failed POST.

### Explicitly out of scope

- No retry for files that were never encoded (there is no source path to act on).
- No bulk "retry all".
- No change to the codec classifier or the encode settings.

## Files touched

```
backend/app/services/encode_hook.py            retry_encode(), failure ring buffer, snapshot shape
backend/app/api/routes.py                   POST /api/encoding/retry
frontend/src/app/components/EncodeProgressPanel.tsx   card treatment, failed rows, Retry button
frontend/src/app/hooks/useJobManagement.ts  snapshot shape, retry action
frontend/src/types/index.ts                 EncodeFailure, retry payloads
backend/tests/unit/test_encode_hook_progress.py          retry + failure-buffer tests
backend/tests/unit/test_encode_progress_api.py        route tests
```

## Verification

1. Unit: `retry_encode` on a path with a marker deletes the marker and starts an encode.
2. Unit: `retry_encode` on a non-existent path raises/returns the 404 condition.
3. Unit: a second `retry_encode` while the file is in `_ENCODING` is refused (409 condition).
4. Unit: a failed encode lands in the ring buffer and appears under `failed`; the buffer caps at 20.
5. Route: `POST /api/encoding/retry` returns 202/404/409 for the three cases.
6. Regression: `tests/unit/test_encode_hook_concurrency.py` still 10/10; full `tests/unit` green.
7. Frontend: panel renders as a card above the job list, empty state visible, failed row shows
   Retry. (Build with `npm ci && npx tsc --noEmit` in the container if node_modules can be
   installed; otherwise state plainly that this was not verified.)

## Working constraints

- Clone: `/home/hermes/.hermes/engram-work`, branch `feat/remote-asr`. Do not `git reset --hard`,
  do not switch branches, do not touch `backend/app/core/organizer.py`, do not delete `*.bak-*`.
- Tests: `cd backend && uv run pytest tests/unit/ -q`. Baseline is 5260 passed / 10 skipped / 1 xfailed.
- Commit on `feat/remote-asr` with a conventional-commit message.
