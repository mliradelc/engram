# SPEC — Encode progress panel in Engram

Status: planned, not implemented
Scope: the AV1/x265 post-organize hook only. No changes to ripping, matching, organizing.
Source: GitLab work item gitlab.git.nrw/mliradel/personal-workbench#5

## Goal

Show, for every file currently being encoded: the file, its progress, its ETA, and the
folder it will land in. Nothing else — no charts, no history, no per-title estimates.

## Why the current code cannot answer this

`encode_hook.transcode_to_av1()` runs ffmpeg via
`subprocess.run(cmd, capture_output=True, ...)`. Output is captured and discarded until the
process exits, so at any moment during an 80-minute encode the application knows only
"started" and "finished". There is no intermediate state to display and no place it is stored.

## Design

### 1. Progress source — ffmpeg's own progress stream

Add `-progress pipe:1 -nostats` to the ffmpeg command line and read stdout line by line
instead of capturing it. ffmpeg 5.1.9 (confirmed on CT111) emits a repeating key=value block.
Measured on a real title from the library, the fields present are:

```
frame=200
fps=5.40
total_size=2331365
out_time_us=8000000
out_time_ms=8000000
speed=0.216x
progress=continue
```

- `out_time_us` is the encoded position — the numerator of progress.
- `speed` is the multiplier — the basis for ETA.
- `progress=end` is the terminal marker.

Duration comes from the existing `_probe(src)` call (`format.duration`), which the function
already performs, so progress needs no new probe.

    percent = out_time_us / 1e6 / duration_seconds
    eta     = (duration_seconds - out_time_us / 1e6) / speed

Both clamped: percent to 0..100, eta to >= 0. `speed` is absent on the first block and can
read `N/A`; treat either as "unknown" and omit the ETA rather than showing a wrong one.

**Note on the existing code:** `_probe(src)` is currently called *after* the encode, in the
verification block (encode_hook.py ~line 350). For progress you need the duration *before* the
encode starts. Move/copy that probe to before the `subprocess` call — do not remove the
post-encode verification probe, which is a separate correctness check.

**Note on `-progress pipe:1` and stderr:** the encode currently reads `run.stderr` for the
failure log line. With `-progress pipe:1`, stdout carries the progress stream and stderr still
carries `-v error` output. Keep reading stderr (on a thread or via `stderr=subprocess.PIPE`
drained after wait) so the existing `AV1 hook: encode failed for %s: %s` log keeps its content.
Do not let a full stderr pipe deadlock the encode.

### 2. State — one in-memory registry, no schema change

A module-level dict in `encode_hook.py`, guarded by a `threading.Lock`:

```python
_ENCODING: dict[str, dict] = {}
# key: destination path (str) — unique per file, survives queueing
# value: {file, destination_folder, percent, eta_seconds, speed, codec, started_at}
```

An in-memory registry is deliberate. Encoding is a process-local activity: if the container
restarts, the encode is dead and the row would be a lie. A DB table would need a migration,
a cleanup-on-boot path, and would still be wrong for exactly that case. Nothing here needs to
survive a restart.

The entry is written **after** the `_encode_slot` permit is acquired and removed in a
`finally`, so a queued file does not appear as encoding, and a failed or timed-out encode
cannot leak a phantom row. This mirrors the permit discipline already in the gate.

Public accessor: `encoding_snapshot() -> list[dict]` returning a copy (never the live dict),
sorted deterministically (by `started_at`).

### 3. Delivery — WebSocket, mirroring `backup_progress`

`websocket.py` already has the exact precedent: `broadcast_backup_progress` sends a flat
message with `speed` and `eta` always present, with a docstring explaining why it is a
distinct type rather than reusing `rip_progress`. Add the parallel:

```python
async def broadcast_encode_progress(self, *, file, destination_folder,
                                    percent, eta, speed=None, codec=None) -> None:
    await self.broadcast({
        "type": "encode_progress",
        "file": file,
        "destination_folder": destination_folder,
        "percent": percent,
        "eta": eta,
        "speed": speed,
        "codec": codec,
    })
```

Flat, like every other message the class emits. Throttle to **one message per 2 seconds per
file** — ffmpeg emits several blocks per second, and the frontend only needs a smooth bar.

The hook runs in a worker thread (`asyncio.to_thread`), so publishing needs
`asyncio.run_coroutine_threadsafe(manager.broadcast_encode_progress(...), loop)` with the
loop captured at call time. `finalization_coordinator` already imports `ws_manager`; pass the
running loop into `after_organize` as a keyword so the hook stays import-free of the API layer
and no circular import is introduced.

Concretely: `after_organize(final_path, *, context=None, loop=None)` and
`transcode_to_av1(src, staging_dir, plan=None, *, loop=None)`. Both call sites in
`finalization_coordinator.py` (~lines 2431 and 2694, inside `asyncio.to_thread`) must pass
`loop=asyncio.get_running_loop()` — call `get_running_loop()` in the async context, *before*
`to_thread`, not inside the thread. `run_coroutine_threadsafe` must not be called with a
`None` loop; if `loop` is `None` (e.g. a direct unit-test call), skip publishing silently and
keep encoding.

### 4. Late-joining clients — one snapshot endpoint

A browser opened mid-encode sees no WebSocket history. Add:

```
GET /api/encoding  ->  [ {file, destination_folder, percent, eta, speed, codec, started_at} ]
```

Reading `encode_hook.encoding_snapshot()`. The frontend calls it on mount, then applies
`encode_progress` messages on top. This is the only new route. Add it in
`backend/app/api/routes.py` near the other GET routes (~line 500, beside `@router.get("/jobs")`).

### 5. Frontend — a small panel

- Extend `types/index.ts` with `EncodeProgressMessage` beside `BackupProgressMessage`
  (line ~204) and add it to the `WebSocketMessage` union (line ~304).
- Add a `case 'encode_progress':` in `useJobManagement.ts` (beside `case 'backup_progress':`,
  ~line 507), folding into a `Map<string, EncodeProgress>` keyed by file. Expose it from the
  hook's return object (~line 560) so `App.tsx` can render it.
- New component `frontend/src/app/components/EncodeProgressPanel.tsx` (+ a `.test.tsx`).
  Render a compact card list. Per file, exactly four things: **name**, **progress bar with
  percent**, **ETA**, **destination folder**. Empty state: "Nothing encoding".
- Remove an entry on `percent >= 100`.
- Fetch `/api/encoding` on mount to seed the map (late-joining client).

## What is deliberately excluded

- No encode history or throughput graphs (`av1-measurement.json` already holds the history).
- No queue position or "N files waiting" — the semaphore does not expose it and it was not asked for.
- No per-title ETA for the whole disc; only the file in flight.
- No persistence across restarts.

## Files touched

```
backend/app/services/encode_hook.py            Popen + progress parse + registry + snapshot
backend/app/api/websocket.py                broadcast_encode_progress
backend/app/api/routes.py                   GET /api/encoding
backend/app/services/finalization_coordinator.py   pass event loop into after_organize
frontend/src/types/index.ts                 message type
frontend/src/app/hooks/useJobManagement.ts  reducer case
frontend/src/app/components/EncodeProgressPanel.tsx   new component
```

## Verification

1. Unit: feed a recorded `-progress` stream into the parser; assert percent/eta maths and
   that `speed=N/A` yields eta `None`.
2. Unit: `encoding_snapshot()` is empty before, populated during, empty after a failed encode.
3. Live: start a real encode on CT111, curl `/api/encoding` twice 30 s apart, confirm percent
   rises and ETA falls. Confirm the panel updates in the browser without a reload.
4. Regression: the existing encode-slot probe (4 threads at limit 1 -> max 1) must still pass.

## Deployment notes

- `ENGRAM_AV1_*` variables must be added to the compose `environment:` block as well as
  `/opt/engram/.env` — the file alone does not reach the container.
- Rebuild requires the bundle transfer path to CT111; the fork has no push credential.
- Do not rebuild while an encode is running.

## Working constraints for the implementer

- Working clone: `/home/hermes/.hermes/engram-work`, branch `feat/remote-asr`.
  Work here. **Do not `git reset --hard`, do not switch branches, do not touch `organizer.py`**
  (it carries an unrelated uncommitted fix) and do not delete the `*.bak-*` files.
  Never work under `~/.hermes/cache/scratch` — that tree is pruned automatically and has
  already destroyed a clone mid-run.
- Test runner: `cd backend && uv run pytest tests/unit/ -q`. The venv is managed by `uv`.
  `tests/unit/test_encode_hook_concurrency.py` currently passes 10/10 — it must still pass.
- No `node_modules` in `frontend/`; do not attempt `npm install` or run vitest. Write the
  frontend code and its test file, but backend tests are the gate.
- Style: match the surrounding code — type hints, `logger` not `print`, the existing
  `# noqa: BLE001 - fail-open by design` convention in the hook. The hook must never raise.
- Commit on `feat/remote-asr` with a conventional-commit message once backend tests pass.
