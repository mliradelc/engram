# SPEC: Encode Library — a menu of actual and past encodings, grouped by series/film

> Author: James (Hermes) for the principal (Max). Implemented by Claude Code.
> Repo: `/home/hermes/.hermes/engram-work` · branch `sync/upstream-0.45.2`
> Target image: `ghcr.io/mliradelc/engram` (fork of `mliradelc/engram`), deployed in CT111.

## The principal's request, verbatim

> "I want a menu where I can see the actual and past encodings, and each item I can
> re-encode and select the pipeline I want. Each item should be grouped in the series or
> film that their belong and show where is the folder."

Five requirements:

1. **A menu** — a first-class screen, not the small dashboard card.
2. **Actual + past** encodings — in-flight *and* historical.
3. **Per item: re-encode** — trigger a new encode from the item.
4. **Per item: select the pipeline** — choose the codec class / encoder pipeline.
5. **Group by series or film, and show the folder** — grouped tree, folder path visible.

## What already exists (do not rebuild it)

| Piece | Location | State |
|---|---|---|
| Live encode registry | `GET /api/encoding` → `{active, failed}` | Works. `active` rows: `file, destination_folder, percent, eta, speed, codec, codec_class, forced, started_at`. `failed` is a 20-row **in-memory ring buffer**, process-local. |
| Retry / re-encode | `POST /api/encoding/retry {file, codec_class?}` → 202 | Works. Domain-gated. `codec_class` forces a class. |
| Class pin | `GET/PUT/DELETE /api/encoding/override?file=` | Works. Records only, never starts an encode. |
| Dismiss failures | `POST /api/encoding/dismiss {file?}` | Works. |
| Dashboard encode card | `frontend/src/app/components/EncodeProgressPanel.tsx` | Live-only + failed rows. Reuse its visual idiom. |
| Class badge / select | `EncodeClassBadge.tsx`, `EncodeClassSelect.tsx`, `encodeClasses.ts` | Reuse as-is. |
| Disc job history | `GET /api/jobs`, `HistoryPage.tsx` | Terminal disc jobs. **Not** the same thing as encodes. |

**The four pipeline classes** (`CODEC-CLASSIFIER.md`, `encode_hook.py` `OFFERED_CLASSES`):
`passthrough`, `animation`, `live_action_grainy`, `live_action_clean`.
`hdr` and `interlaced` are decided by the classifier and are **not forceable** — do not
offer them in the selector. `passthrough` cannot be forced on an HDR source (422).

## The critical data-source problem: history is not persisted

Today the **only** durable record of an encode is the sidecar marker written next to the
source: `<source>.engram-av1.json`, e.g.

```json
{"size": 469996597, "mtime": 1794639863, "av1": "/staging/av1-out/Stargate SG-1 Disc 1 Extra t05.mkv"}
```

Fields: `size`, `mtime`, `av1` (output path, `null` for passthrough), `codec_class`
(absent = `passthrough`), `forced`, and an optional `override` block.
Marker name is **frozen** (`MARKER_SUFFIX`, guarded by `test_marker_suffix_is_frozen`).

Consequences you must handle:

- **A marker can be stale.** It records `size` + `mtime` of the *source at encode time*. If
  the source changed since, the record is no longer "done". Surface that as a distinct
  state — `stale` — rather than lying that the file is encoded.
- **A marker can point at a deleted output.** `av1` may name a file that has since been
  pruned from staging. Check existence and mark `output missing`.
- **A marker on a file the curator already imported** has an `av1` path under
  `/staging/av1-out/` that will not match the library file. Report the recorded output path
  as-is; do not try to reconcile it against the library.
- **There is no timestamp in the marker.** Use the marker file's own `mtime` as
  "encoded at" and say so in a tooltip — do not invent precision. (If you add a field to
  the marker, it must be additive and optional so every existing marker stays valid;
  prefer the marker's mtime and skip the schema change for this iteration.)

## Build

### 1. Backend: a read-only scan endpoint

`GET /api/encoding/library`

Walk the encode domain (reuse `encode_hook.default_retry_roots([config.library_tv_path,
config.library_movies_path])` — **never** invent a new root list) for `*.mkv` files that
have a sibling `.engram-av1.json`, and return:

```json
{
  "root_tv": "/media/Videos/TV",
  "root_movies": "/media/Videos/Movies",
  "groups": [
    {
      "kind": "series" | "film",
      "title": "Stargate SG-1 (1997) [tmdbid-4629]",
      "folder": "/media/Videos/TV/Stargate SG-1 (1997) [tmdbid-4629]",
      "entries": [
        {
          "source": "/media/.../Season 03/Stargate SG-1 - S03E09 - Rules of Engagement.mkv",
          "folder": "/media/.../Season 03",
          "season": 3,
          "episode": 9,
          "output": "/staging/av1-out/Stargate SG-1 - S03E09 - Rules of Engagement.mkv",
          "codec_class": "live_action_grainy",
          "forced": false,
          "state": "encoded" | "passthrough" | "stale" | "output_missing" | "encoding" | "failed",
          "encoded_at": 1794639863,
          "source_bytes": 469996597,
          "output_bytes": 120000000
        }
      ]
    }
  ],
  "totals": {"groups": 12, "entries": 340, "encoded": 300, "passthrough": 20,
             "stale": 12, "output_missing": 8}
}
```

Requirements:

- **Grouping.** TV: the show folder (one level under `library_tv_path`), `kind: "series"`.
  Movies: the film folder, `kind: "film"`. Anything you cannot place goes in a trailing
  group `kind: "other"` with `title: "Ungrouped"` rather than being silently dropped.
- **Season/episode** parsed from the library's own naming (`S03E09`); `null` when absent.
- **Overlay live state.** Merge `encoding_status()["active"]` into `state` so a file
  currently encoding shows `encoding` with its progress; merge `failed` so a failed source
  shows `failed`. A live row wins over the marker.
- **Cost.** This walks the whole library. Do it off the event loop (`asyncio.to_thread`),
  one `os.scandir` pass per directory (not `Path.rglob` with per-file `stat` storms), and
  cache the marker scan for a short TTL (10 s) so the UI's polling does not re-walk the
  library on every tick. Expose the TTL as a module constant.
- **Safety.** Read-only. No writes, no moves, no encodes. Gate it with
  `Depends(require_localhost_or_lan)` like the other `/encoding` routes.
- **Path exposure.** These are absolute server paths already exposed by
  `/api/encoding`'s `destination_folder`; keep it consistent with that existing precedent.

### 2. Frontend: a "Pipelines / Encode Library" screen

New route `ROUTES.ENCODE_LIBRARY = "/encodes"` in `src/config/routes.ts`, registered in
`App.tsx`'s `<Routes>`, with a nav entry (mirror however `History` is surfaced — check
`DashboardSideRail.tsx` before inventing a nav pattern).

Screen requirements:

1. **Grouped list.** Collapsible sections per series/film. Header shows the title, the
   **folder path** (monospace, selectable, truncated in the middle with a `title=` full
   path), and a count. Series sections nest one level by season.
2. **Every row shows:** episode/file name, class badge, state pill, output size vs source
   size (and the % saved), and "encoded at". `stale` and `output_missing` must be visually
   distinct — they are the two states where the marker is lying to the user.
3. **Per-row actions:**
   - **Pipeline selector** — reuse `EncodeClassSelect.tsx` (the four forceable classes +
     Auto). 
   - **Re-encode** — `POST /api/encoding/retry {file, codec_class}`. On success, toast and
     update the row optimistically; the WS/live poll will take over. Surface 409/422
     bodies verbatim in the toast — they explain *why* (already encoding / HDR / staging).
4. **Live section pinned at top** — anything currently encoding, with percent/ETA/speed,
   updating from the existing `encode_progress` WebSocket feed. Do not add a second poller;
   read the state `App.tsx` already maintains.
5. **Filter/search** — free-text over title, filename and folder path; plus a state filter
   (All / Encoded / Stale / Missing / Failed). Group headers collapse to match.
6. **Idle-safe.** Empty library renders a real empty state, not a spinner forever.

Style: follow the existing `synapse` design tokens (`sv`, `SvPanel`) and the
`EncodeProgressPanel` card idiom. **Dark theme only** — the app is dark-only; do not
introduce a light variant.

### 3. Tests (required before you call it done)

- **Backend:** grouping TV vs film vs ungrouped; season/episode parsing incl. the
  `S00E01` extras case; `stale` detection (size/mtime mismatch); `output_missing`;
  live-state overlay wins over marker; a marker that is not valid JSON is skipped, not a
  500; the endpoint is read-only (assert no write calls).
- **Frontend:** vitest + RTL — renders groups, shows folder paths, fires
  `onReencode` with the chosen class, respects the state filter, and empty state.
- Existing suites must not regress. The repo's baseline is documented in the skill: run
  `pytest` in `backend/` and `npm run test:unit` + `npm run lint` in `frontend/`.

## Constraints and hazards — read before writing code

- **Do not rename `MARKER_SUFFIX` or the `av1` key.** Guarded by
  `test_marker_suffix_is_frozen`; renaming it makes every already-encoded file look
  unconverted and triggers a full library re-encode.
- **An encode is running right now** (Great Barrier Reef S01E02, x265, roughly 7 hours
  remaining at the time of writing). Do not restart the container, do not rebuild the
  image, do not touch `/staging`. Work only in the git tree and run tests locally.
- **A re-encode overwrites the previous encode silently** (same source name → same
  destination → `tmp.replace(dest)`). Say so in the UI: the Re-encode button must warn
  once ("this replaces the existing encode") rather than implying a second version is kept.
- **No hardware encoding.** CT111's iGPU cannot do AV1. Do not offer QSV as a pipeline.
- `passthrough` writes `av1: null` — it is a legitimate "don't encode this" choice, not a
  failure. Render it as its own state, not as a broken row.

## Deliverable

Working code + tests in the tree. Then:

1. Run the suites and report **real** counts, with any pre-existing failures called out
   separately from ones you introduced.
2. Run the frontend dev server and **capture a screenshot of the new screen with real
   data** — the principal judges UI from a render, never from a description. Dark theme,
   at least one series group expanded with its folder path visible.
3. Do **not** build the image or deploy. Report the diff summary and stop for review.
