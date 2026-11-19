# SPEC: reclassify — frontend + bit-depth + interlaced gap

Repo `/home/hermes/.hermes/engram-work`, branch `feat/remote-asr`, HEAD `06314d5`.
Backend is done and green: **5342 passed, 10 skipped, 1 xfailed** (verified independently).
Read `SPEC-reclassify-v2.md` for the original intent. This spec covers what remains.

Do **not** regress the backend suite. Run `cd backend && uv run pytest tests/unit/ -q` before the
frontend work and again at the end.

---

## Part A — D6: preserve bit depth (backend)

The principal's rule, verbatim: *"the fps should match the content, dont change it"* and, on being
asked about 10-bit sources, *"preserve bit"*.

Frame rate is already safe — verified: no `-r`, `-fps_mode` or `-vsync` in any encode path, and
`bwdif` is `mode=send_frame` (one frame out per frame in). **Bit depth is not safe.**

`video_args()` emits `-pix_fmt self.pix_fmt`, and every class currently implies `yuv420p`, so a
`yuv420p10le` source is silently reduced to 8-bit. `/media/movies` already contains `yuv420p10le`
files, so this is live.

Implement: derive the output pix_fmt from the source probe.
- any `p10le` / 10-bit source → `yuv420p10le`
- 8-bit source → `yuv420p`

Never upsample 8-bit to 10-bit. Keep 4:2:0 subsampling. Compute it from the probe in `CodecPlan`,
not from a per-class constant. Leave `_legacy_video_args()` unchanged (no `-pix_fmt`) — kill-switch
fidelity is deliberate.

Tests: 10-bit source plans `yuv420p10le`; 8-bit still plans `yuv420p`; no class ever emits a
pix_fmt shallower than its source; `_legacy_video_args()` still has no `-pix_fmt`.

## Part B — the interlaced gap you found

You correctly identified that a forced class skips the idet probe, so forcing `animation` on
interlaced DVD content would encode without deinterlacing. Fix it, and note the principal rips
**DVDs**, which are frequently interlaced — this is not a hypothetical.

Rule: **deinterlacing is a property of the source, not a preference.** So:
- Before applying any forced/pinned class, run the cheap idet probe.
- If the source is interlaced, `bwdif` is added to `video_filters` regardless of which class was
  forced. The class still selects codec/CRF/preset; it does not get to decide deinterlacing.
- A pin therefore cannot produce a combed output.

Test: a forced class on an interlaced source still gets `bwdif`; on a progressive source it does not.

## Part C — frontend

The deployed panel is ~122 lines with no failed rows and no retry. Build against what is in **this
tree**: `frontend/src/app/components/EncodeProgressPanel.tsx`,
`frontend/src/app/hooks/useJobManagement.ts` (already reads `.active`/`.failed` correctly — no bug),
`frontend/src/app/types/index.ts`, `App.tsx`.

- `EncodeClassSelect` — `<select>` with "Auto (classifier)" plus the four classes
  (`passthrough`, `animation`, `live_action_grainy`, `live_action_clean`), `sv.*` styling,
  `data-testid="encode-class-select"`. No HDR option, no Interlaced option.
- `EncodeClassBadge` — class name plus a FORCED chip; `encode-class-badge`, `encode-class-forced`.
- Failed rows: `encode-failed-row` containing the badge, the selector, **Retry** (`encode-retry`)
  and **Dismiss** (`encode-dismiss`).
- Retry passes the chosen class, or omits it for Auto.
- `applyEncodeProgress` must preserve `codec_class`/`forced` across WebSocket updates, which do not
  carry them.
- Pinned state read from `GET /encoding/override?file=`.

Backend notes you need: `stale` is inside the `override` object, not top level. A retry's 202
includes `codec_class` only when one was sent.

## Verify

```
cd backend && uv run pytest tests/unit/ -q        # >= 5342 passed
cd frontend && npm run test:unit && npm run lint && npm run build
```

Commit: Part A+B as one backend commit, Part C as a second. `git add` specific files, never `-A`.
Do not push, do not tag, do not touch CT111.

Report: files changed, both test counts, and any claim in this spec you found to be wrong.
