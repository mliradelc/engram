# SPEC: per-title encode retry and codec-profile reclassification in the Engram UI

**Status: PLANNING ONLY. Do not write, edit, or commit any code in this run.**

Deliver a written implementation plan as your final answer. Read the code, verify every
claim against the real files, and produce a plan a reviewer can approve or reject. Do not
modify the working tree.

## Repository

`/opt/engram/src` — the fork of `mliradelc/engram`, branch `feat/remote-asr`.
Backend: FastAPI + SQLModel, Python. Frontend: React + TypeScript + Vite.

## The use case (verbatim from the principal)

> "I want to retry or reclasify the type of encoding profile for a given title in engram's UI."

Two distinct capabilities, both per-title, both driven from the UI:

1. **Retry** an encode that failed, or re-encode a title deliberately.
2. **Reclassify** — override the codec classifier's verdict for one title and re-encode it
   with a different profile (e.g. force `animation` for a title the classifier called
   `live_action_clean`, or force `passthrough` to leave it untouched).

## What already exists (verified — do not re-derive, but do confirm)

`backend/app/services/encode_hook.py`:

- `after_organize(final_path, *, context=None, loop=None)` — the only entry point that
  encodes. Runs the classifier, then `transcode_to_av1`.
- `transcode_to_av1(src, staging_dir, plan=None, *, loop=None)` — `plan` is a
  `codec_classifier.CodecPlan`; `plan=None` means the fixed legacy CRF/preset path.
- `retry_encode(path, *, loop=None, roots=None)` — validates, deletes the marker, and runs
  `after_organize` on a daemon thread. Raises `RetryNotFound` (404) / `RetryConflict` (409).
- `dismiss_failure(source=None)` — clears buffered failure rows (added 2026-11-17).
- `domain_roots()` / `in_domain(path, library_paths=())` — **Engram's domain is
  `/media/_staging` plus `ENGRAM_AV1_STAGING` only.** `/media/tv` and `/media/movies` are
  deliberately excluded: the library is read-only to the hook. `after_organize` and
  `retry_encode` both enforce this. Any new endpoint MUST keep that invariant.
- `_ENCODING` (in-flight registry), `_FAILED` (failure ring buffer, `FAILURE_BUFFER_SIZE=20`),
  `_RETRY_PENDING`, `_encoding_lock`, `_encode_slot` (semaphore, `ENGRAM_AV1_MAX_CONCURRENT`).
- `_already_done(src)` / `_write_marker(src, dest, codec_class)` — a sidecar
  `<file>.engram-av1.json` recording size+mtime+codec_class. **This marker is the only
  persisted record of what profile a file was encoded with.**

`backend/app/services/codec_classifier.py`:

- `CLASSES = (passthrough, interlaced, hdr, animation, live_action_grainy, live_action_clean)`
- `plan_for_class(class_name, ...)` builds a `CodecPlan` for a named class.
- `plan_for_file(path, context=None, *, deinterlace=...)` probes and classifies.
- `CodecPlan` carries `class_name, codec ("av1"|"hevc"|"none"), reason, crf, preset,
  pix_fmt, svt_required/optional, x265_required/optional, video_filters, hdr, evidence`.
  `is_passthrough` == `codec == "none"`. `describe()` renders the one-line log summary.

`backend/app/api/routes.py` (around line 500):

- `GET /api/encoding` → `{active: [...], failed: [...]}` via `encode_hook.encoding_status()`.
  `active` rows: `file, destination_folder, percent, eta, speed, codec, started_at`.
  `failed` rows: `file, source, destination_folder, reason, failed_at`.
- `POST /api/encoding/retry` — body `{file}` (absolute source path), 202/404/409.
- `POST /api/encoding/dismiss` — body `{file?}`, returns `{dismissed: N}`.
- Both mutating routes are guarded by `Depends(require_localhost_or_lan)`.

Frontend:

- `frontend/src/app/components/EncodeProgressPanel.tsx` (122 lines) — renders in-flight
  encodes only: file, progress bar, ETA, destination. "Nothing encoding" when idle.
  It does **not** render the `failed` rows at all, and has no retry control.
- `frontend/src/app/hooks/useJobManagement.ts` — seeds `encodes` from
  `GET /api/encoding` (line ~278) and folds `encode_progress` WS messages. Note: the hook
  reads the response as `EncodeProgress[]`, i.e. it consumes only the `active` array.
- `frontend/src/types/index.ts` — `EncodeProgress` interface (line ~232).
- Panel is mounted in `frontend/src/app/App.tsx` (~line 522).

## Hard constraints

1. **The domain gate stays.** No new endpoint may encode, move, or write a marker beside a
   file outside `domain_roots()`. Reclassifying a library file must be refused exactly as
   `retry_encode` refuses it today.
2. **One encode path.** A reclassified encode must go through `after_organize` /
   `transcode_to_av1` with the same `_encode_slot` permit, the same `_ENCODING` registry and
   the same verification (`_encode_and_verify`: codec, pix_fmt, HDR transfer, track counts).
   Do not add a second ffmpeg code path.
3. **Never in-place.** The source is untouched until the encode verifies. A failed or killed
   encode must leave the original intact and record a failure row.
4. **The override must be visible and reversible.** If a title is forced to a class, the UI
   must show that it is overridden (not classifier-decided), and the override must be
   clearable so the classifier takes over again.
5. **`ENGRAM_CODEC_CLASSIFIER=0` must still work** — the fixed CRF/preset path is the
   documented kill switch and must not be broken by an override mechanism.
6. Do not break the existing tests. Backend baseline: `cd backend && uv run pytest
   tests/unit/ -q` (~5260 passed, 10 skipped, 1 xfailed, ~6 min). Frontend: `cd frontend &&
   npm test` (vitest).

## Questions the plan must answer

1. **Where does the override live?** The hook is stateless and the marker is the only
   per-file record. Options to evaluate honestly: (a) a new SQLite table keyed by source
   path; (b) fields inside the existing `.engram-av1.json` marker; (c) an in-memory dict
   (survives nothing — say so). Recommend one and state what happens to the override after
   the encode publishes, and after a container restart. Note that the marker lives *beside
   the source*, so writing an override into it is a write to the source's directory — check
   that this is acceptable within the domain rule, and whether the marker is re-written or
   replaced on a successful encode (read `_write_marker` and `_already_done` carefully: the
   marker's size+mtime match is what suppresses a re-encode).
2. **How does a reclassify reach `transcode_to_av1`?** `after_organize` currently calls
   `_classify(src, context)` internally and has no parameter for a forced plan. Propose the
   minimal signature change (e.g. `forced_class: str | None = None`) and show every call
   site that must be updated — including `_run_retry` and any caller in
   `finalization_coordinator.py`.
3. **What is the API surface?** Propose the exact route(s), request/response shapes, and
   status codes for: retry-with-override, set/clear an override without encoding, and
   listing overrides. Reuse `require_localhost_or_lan`. Keep the existing `/encoding/retry`
   backward compatible.
4. **What does the UI look like?** The panel currently shows only in-flight encodes. Propose
   where the failed rows and the per-title class selector belong — extend
   `EncodeProgressPanel`, or a new component — and how the user picks a class (the six
   `CLASSES` names, with `passthrough` meaning "leave untouched"). Include the override
   indicator and the clear action. Match the existing visual language in the panel
   (`sv.*` tokens from `./synapse`, monospace, `data-testid` attributes). State whether
   `useJobManagement` needs to change its `GET /api/encoding` consumption (it currently
   types the response as an array, but the endpoint returns an object — verify this and say
   whether it is a live bug).
5. **What tests?** Name the specific test files to add or update, backend and frontend, and
   the assertions that would have caught the class of bug this feature invites (wrong file
   encoded, override lost on restart, library file accepted).
6. **What are the risks and the rollback?** Especially: an override that makes a title
   encode forever, a stale override after the file changed, and the interaction with
   `_already_done`.

## Output format

A single markdown plan with these sections, in this order:

1. **Summary** — 3-5 sentences, what will be built.
2. **Design decisions** — each with the alternatives considered and why the chosen one wins.
3. **Backend changes** — file by file, with the function signatures and route shapes.
4. **Frontend changes** — file by file, component sketch (props, state, testids).
5. **Data model** — the exact schema/format of a stored override.
6. **Tests** — file names and the assertions.
7. **Risks and rollback.**
8. **Open questions for the principal** — anything you could not settle from the code.

Be concrete: real paths, real function names, real line numbers where you verified them.
Where you are unsure, say "unverified" rather than guessing.
