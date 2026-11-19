# SPEC: encoding panel — filename wrapping and a useful failure reason

Repo `/home/hermes/.hermes/engram-work`, branch `feat/remote-asr`, HEAD `9cd51e3`.
Baselines: backend **5490 passed / 10 skipped / 1 xfailed**, frontend **840 passed**, lint + build clean.

Two defects were visible when the panel was rendered for the principal. Fix both.

---

## 1. Long filenames truncate to `The Fifth Rac…`

**Where:** `frontend/src/app/components/EncodeProgressPanel.tsx`, the failed-row name div:

```tsx
<div title={f.source} style={{ ...ellipsis, fontSize: 13, color: sv.ink }}>
  {f.file}
</div>
```

`ellipsis` is `overflow:hidden; text-overflow:ellipsis; white-space:nowrap`. The row is
`display:flex` with the controls `flexShrink: 0`, so the name column gets whatever is left and
cuts off mid-word.

**Fix:** let the filename take up to **two lines** and only ellipsise past that.
- Replace the single-line `ellipsis` on the name with a two-line clamp:
  `display: -webkit-box; WebkitLineClamp: 2; WebkitBoxOrient: "vertical"; overflow: hidden;`
  plus `wordBreak: "break-word"` so a long show name breaks at a sensible point.
- Keep `title={f.source}` so the full path is still available on hover.
- Set `alignItems: "flex-start"` on the row so the icon and controls align to the top when the
  name wraps to two lines, instead of floating in the vertical middle.
- The active-encode row has the same name treatment — apply the same two-line clamp there so the
  two row types look consistent.

Add a regression test: a failure whose filename is long (e.g. 90 characters) renders the whole
name in the DOM (assert on `textContent`, which is not affected by CSS clamping) and the name
element carries the clamp style. The test must not assert a rendered pixel width.

## 2. The failure reason shows ffmpeg's startup banner, not the failure

**Where:** `backend/app/services/encode_hook.py`, `_encode_and_verify`:

```python
stderr = (run.stderr or "").strip()
logger.error("Encoding hook: encode failed for %s: %s", src.name, stderr[-500:])
tmp.unlink(missing_ok=True)
last = stderr.splitlines()[-1][-200:] if stderr else ""
return f"ffmpeg exited {run.returncode}" + (f": {last}" if last else "")
```

Taking the **last** line is the bug. When the process is killed, SVT-AV1's multi-line startup
banner is still the tail of stderr, so the user sees
`ffmpeg exited 255: Svt[info]: -------------------------------------------` — which is not an error
at all and actively misleads: it made a killed encode look like a codec fault.

**Fix:** choose the most informative line rather than the last one.

- Collect stderr lines, strip each, drop blanks.
- **Discard SVT/x265 banner noise**: any line matching (case-insensitive) `^Svt\[` or
  containing `-------------------------------------------`, or matching `^x265 \[info\]` / the
  `options: cpuid=` / `frame=.* fps=` progress chatter.
- Prefer the **first** remaining line that looks like an error — matches one of
  `error`, `Error`, `Invalid`, `No such file`, `Permission denied`, `Cannot`, `failed`, `Unable`,
  `Unknown`, `not found`, `Unrecognized`.
- If no line qualifies, fall back to the **first** surviving line, not the last.
- Truncate to 200 chars as now.
- Also improve the process-killed case: if the return code is negative or the container was
  recreated, say so plainly. When `run.returncode` is negative (killed by signal N), report
  `ffmpeg killed by signal N` rather than `exited -9`.
- Keep logging the last 500 chars of raw stderr at ERROR — the log should stay complete; only the
  **panel's** reason is cleaned.

Add `_failure_reason(returncode, stderr) -> str` as a small pure helper so it is unit-testable
without running ffmpeg, and use it from `_encode_and_verify`.

## Tests (backend)

- `_failure_reason` with stderr that is *only* the SVT banner and rc=255 → the reason mentions
  `exited 255` and contains **no** `Svt[info]` and no run of dashes.
- stderr containing `Svt[info]: ...` then `Error: Invalid data found when processing input` →
  the reason includes the `Invalid data` line, not the banner.
- `Permission denied` from ffmpeg → surfaced.
- Empty stderr, rc=1 → `ffmpeg exited 1` and nothing after the colon.
- rc=-9 → mentions `signal` / killed, not a bare negative number.
- A banner-only line followed by nothing else → falls back sensibly, does not return the dashes.

## Tests (frontend)

- Long filename: full text present in DOM; clamp style applied; `title` carries the full path.
- Existing failure-row tests must keep passing unchanged.

## Verify

```
cd backend && uv run pytest tests/unit/ -q     # >= 5490 passed
cd frontend && npm run test:unit && npm run lint && npm run build
```

Two commits: backend reason fix, then frontend wrap fix. Specific files only, never `git add -A`.
Do not push, tag, or touch CT111. Do not add `SPEC-*.md` to a commit.

## Report

Files changed, both test counts, the exact reason string produced for a banner-only failure, and
anything in this spec you found wrong.
