# SPEC: per-title encode retry and codec-profile reclassification (Engram UI)

**Repo:** `/home/hermes/.hermes/engram-work` — branch `feat/remote-asr`, at `9b7f84e`.
Durable clone; do NOT work under `~/.hermes/cache/scratch` (it is pruned).

## Current state (all verified, do not re-derive)

| Commit | What |
|---|---|
| `af64311` | tests assert the staging-only domain |
| `6b278c9` | domain gate: `_DOMAIN_ROOTS`, `domain_roots()`, `in_domain()`; `POST /api/encoding/dismiss` |
| `a6b9d95` | whisper chunk isolation (`extract_audio_chunk` always extracts) |
| `9b7f84e` | `organize_tv_extras` honours `conflict_resolution` |

Suite is green: **5296 passed, 10 skipped, 1 xfailed.** Tag `v0.45.0-fork.4` is built and
deployed to CT111 from GHCR. Baseline is trustworthy — keep it that way.

## The use case (from the principal)

> "I want to retry or reclasify the type of encoding profile for a given title in engram's UI."

Plus, decided in review:

- **The principal will never rip 4K.** His library is DVD (720x576) and Blu-ray 1080p, measured
  SDR: `color_transfer=bt709` or `unknown`. **Normal Blu-ray is SDR by spec** — HDR needs Ultra
  HD Blu-ray. So `hdr` is dead weight and must **not** be offered.
- **Four classes in the UI**, not six:
  `passthrough`, `animation`, `live_action_grainy`, `live_action_clean`.
  - `interlaced` is **never offered**: it is decided from the source by the classifier's `idet`
    probe (`INTERLACED_FRACTION`, `IDET_MIN_FRAMES`, `IDET_PARITY_DOMINANCE`).
  - `hdr` is **never offered** and must be **refused** if posted.
- **Re-encode replaces the target file.** The hook already does this (`dest = staging_dir /
  src.name`, then `tmp.replace(dest)`). No new naming.
- **A Dismiss button for failed rows ships.**
- **A pin persists until cleared.**
- 30-day retention of source rips is a **separate spec**, not this one.

## Hard constraints

1. **The domain gate stays.** No new endpoint may encode, mark or write beside a file outside
   `domain_roots()`. A library path must be refused exactly as `retry_encode` refuses it now.
2. **One encode path.** A reclassified encode goes through `after_organize` / `transcode_to_av1`
   with the same `_encode_slot` permit, `_ENCODING` registry and `_encode_and_verify`.
   No second ffmpeg path.
3. **Never in-place.** Source untouched until the encode verifies.
4. **`ENGRAM_CODEC_CLASSIFIER=0` still works** — the fixed CRF/preset kill switch is documented
   and must not be broken by an override.
5. **Every encode is logged with the class it used**, so the panel can show it.

## Design decisions to implement

**D0. Class list, verbatim.** Only these four may ever be offered or accepted:

```
passthrough | animation | live_action_grainy | live_action_clean
```

`interlaced` and `hdr` are **absent from the enum offered by the API and the UI**, and posting
either returns 422. Do not add a "HDR" or "Interlaced" option "for completeness" — the principal
rips DVD and Blu-ray only, so both are unreachable, and offering them invites a bad encode.
Internally `codec_classifier.CLASSES` may keep them; the UI/API surface must not.

**D1. The override lives in the marker**, under its own key:
```jsonc
{
  "size": …, "mtime": …, "av1": …, "codec_class": "animation",   // done record
  "forced": true,
  "override": { "codec_class": "animation", "set_at": 1763381234.5, "size": …, "mtime": … }
}
```
The traps that make a naive version wrong — handle all three:
- `_already_done()` must read **only** the top-level size/mtime, or a pin would never apply.
- `_write_marker()` must **merge**, keeping `override`, or a successful encode erases the pin.
- `retry_encode()` currently **unlinks** the marker; it must **strip the done fields** and keep
  `override`, or it deletes the very pin it is applying.

Writes must be atomic (temp + `os.replace`) now that the file holds a user choice.

**D2. Setting a pin must not start an encode.** `PUT /override` records only.

**D3. A forced class still probes the file** — `plan_for_class` needs resolution/HDR/grain.
`forced_class` on `after_organize` takes precedence over a stored pin.

**D4. Refusals (422).** Keep these; each is a real hazard, not ceremony:
- class not in the four offered → 422.
- `hdr` posted at all → 422 (principal will never have HDR; offering it invites a
  washed-out publish, since `_encode_and_verify` only checks colour transfer when the plan is
  marked HDR).
- class `hdr` on a non-HDR source, or any non-passthrough class on an HDR source → 422.
- any non-passthrough class on a source already AV1 → 422 (a second lossy generation).
- source inside `ENGRAM_AV1_STAGING` → 422. There `dest == src` and `tmp.replace(dest)`
  overwrites the original. This closes a hole that exists today on the
  `ENGRAM_CODEC_CLASSIFIER=0` path: add the `dest.resolve() == src.resolve()` guard on **every**
  path, not just the forced one.

**D5. A stale pin is ignored.** If the pin's size/mtime no longer match, the classifier decides,
`GET` reports `stale: true`, and the next `_write_marker` drops it.

## API

| Route | Input | Output |
|---|---|---|
| `POST /api/encoding/retry` | `{file, codec_class?}` | 202 / 404 / 409 / 422 |
| `GET /api/encoding/override?file=` | path | `{file, in_domain, done, applied_class, forced, override, classifier_enabled, classes}` |
| `PUT /api/encoding/override` | `{file, codec_class}` | same shape |
| `DELETE /api/encoding/override?file=` | path | `{cleared}` |
| `POST /api/encoding/dismiss` | `{file?}` | `{dismissed}` — already exists |
| `GET /api/encoding` | — | rows gain `codec_class`, `forced` |

All mutating routes keep `Depends(require_localhost_or_lan)`. `classes` returns only the four.
Outside the domain: `GET` returns `in_domain: false` without reading a marker; `PUT`/`DELETE`
return 404.

## Frontend

The deployed panel is **122 lines** and has **no failed rows and no retry** — the failed-row
work lives on another branch. Build against what is actually in this tree:
`frontend/src/app/components/EncodeProgressPanel.tsx`, `hooks/useJobManagement.ts`
(which already reads `.active`/`.failed` correctly — no bug there), `types/index.ts`, `App.tsx`.

Deliver:
- `EncodeClassSelect` — a `<select>` with "Auto (classifier)" + the four classes, `sv.*` styling,
  `data-testid="encode-class-select"`.
- `EncodeClassBadge` — class name plus a FORCED chip; `encode-class-badge`, `encode-class-forced`.
- Failed rows rendered in the panel with the badge, the selector and **Retry** and **Dismiss**
  buttons (`encode-failed-row`, `encode-retry`, `encode-dismiss`).
- Pass the class through on retry; `applyEncodeProgress` must preserve `codec_class`/`forced`
  across WebSocket updates, which do not carry them.

## Tests

- `forced_plan` keeps resolution; forced class never yields `hdr` or `interlaced` from the UI list.
- Pin applied, not skipped; second `after_organize` does not re-encode (no loop).
- Pin survives a restart (read from marker, not memory).
- Stale pin ignored and dropped.
- Clear returns to classifier; pin-only marker deleted.
- **Library path refused** by retry, PUT and `after_organize(forced_class=…)`; no marker written.
- In-place refused when `dest == src`.
- `hdr` refused with 422.
- Kill switch: `ENGRAM_CODEC_CLASSIFIER=0` → `plan=None`, and a retry with a class → 409.
- Frontend: selector renders four options + Auto; picking `animation` calls `onRetry(f, "animation")`;
  Dismiss calls the route; badge shows FORCED.

Run `cd backend && uv run pytest tests/unit/ -q` (must stay at 5296+ passed) and
`cd frontend && npm run test:unit && npm run lint && npm run build`.

## Commit and report

Separate commits: backend, then frontend. `git add` specific files — never `-A`.
Do not push, do not tag, do not touch CT111.
Report: what you changed, the test counts, and anything in the spec you found to be wrong.
