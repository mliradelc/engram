# SPEC: apply the verified findings from the Gemini codec review

Read `GEMINI-REVIEW-ADJUDICATION.md` at the repo root first. It records which review claims
were verified correct, which were wrong, and which were already handled. **Only the five
changes listed under "Changes applied" are in scope.** Do not implement the claims marked
WRONG or "none" — in particular, do not add a deinterlace filter (it already exists) and do
not change the tile-columns logic (it is already correct).

Repository: /home/hermes/src/engram-codec-classifier/src
Test command: `cd backend && .venv/bin/python -m pytest tests/unit/test_codec_classifier.py -q --no-header`

## The five changes

### 1. x265 anti-smoothing params (highest priority)

In `backend/app/services/codec_classifier.py`, `_x265()` currently builds:

```python
optional = [("aq-mode", "3")]
...
x265_required=[("log-level", "error")], x265_optional=optional,
```

Add `no-sao=1` and `strong-intra-smoothing=0` to **required**, not optional. Rationale: the
hook's fallback path drops optional params, and these must survive that drop — without them the
grainy class silently defeats its own purpose.

Verified on the production image: x265 3.5 defaults to
`tools: b-intra strong-intra-smoothing deblock sao`; with these two params it becomes
`tools: b-intra deblock`.

### 2. x265 preset for the grainy class

Currently `preset="fast" if res == "uhd" else "medium"`. Change to:
- `sd`  -> `medium`
- `hd`  -> `slow`
- `uhd` -> `fast`

Keep it a single expression or a small helper, whichever reads better. Note `slow` is a real
time cost on this host (i3-7100T) — that is an accepted trade for grain retention on 1080p BD.

### 3. SVT-AV1 UHD preset 8 -> 6

`plan_for_class()` has `preset = "8" if res == "uhd" else "6"`. Change to always `"6"`.

There is a coupled rule to rework:

```python
grain_ok = preset == "6"
# SVT-AV1 warns that film grain at preset >6 is a large compute overhead,
# so UHD (preset 8) gets no synthesis.
```

Since UHD now also uses preset 6, decide the grain rule on resolution rather than on the preset
string. Keep the intent: UHD does not get film-grain synthesis (the compute cost is the stated
reason, and it is still true at 4K on this CPU). Update the comment so it states the real
reason. Also update `deinterlaced_plan()`, which has the same `"8" if res == "uhd" else "6"`.

### 4. Clean HD CRF 25 -> 24

In the `live_action_clean` branch: `crf = 23 if res == "sd" else 25` becomes
`crf = 23 if res == "sd" else 24`. SD stays 23.

### 5. Tests

Update `backend/tests/unit/test_codec_classifier.py` for every changed value, and add:
- A regression test asserting `no-sao=1` and `strong-intra-smoothing=0` are present in the
  **required** x265 params of every plan whose codec is `hevc` — including the HDR-grainy path
  and after the hook's optional-params fallback.
- A test that no plan emits AV1 preset 8 (i.e. UHD resolves to preset 6).
- A test that the clean HD plan is CRF 24 and the SD plan is CRF 23.

## Constraints

- Do not change the class set, the classifier thresholds, or the interlaced passthrough default.
- Do not touch `av1_hook.py` or `finalization_coordinator.py`.
- Keep the module stdlib-only at import time.
- Every emitted ffmpeg command must remain valid on **ffmpeg 5.1.9** (the production image), not
  the dev box's 7.x. You may probe with `docker exec engram` if useful, but do not rebuild or
  restart the container.
- Run the test file and report the real pass/fail count. Do not report success without running it.

## Definition of done

- `pytest tests/unit/test_codec_classifier.py` passes; report the number.
- `ruff check` clean on the changed files.
- Report the final table of class -> codec/crf/preset/params so it can be checked against the spec.
