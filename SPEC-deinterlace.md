# SPEC: deinterlace when — and only when — the source is interlaced

Repo `/home/hermes/.hermes/engram-work`, branch `feat/remote-asr`, HEAD `5dda86c`.
Backend suite baseline: **5448 passed, 10 skipped, 1 xfailed**. Do not regress it.

## The principal's rule, verbatim

> "Remember that my sources are dvds and blurays. Blurays can come in 1080i as well.
> The rule is simple: Do the source come interlaced? If yes deinterlace, if no, don't deinterlace"

**This is a decision, not a suggestion.** Deinterlacing is determined by the *source*, never by a
class, a preset, a pin, or an operator toggle.

## What is wrong today

`codec_classifier.classify()` currently *refuses to encode* interlaced sources:

```python
if inter:
    if deinterlace:
        return deinterlaced_plan(res, ev)
    return plan_for_class(INTERLACED, res=res, evidence=ev,
                          reason="interlaced source left untouched (deinterlace not enabled)")
```

and `plan_for_class` treats `INTERLACED` as a no-op (line ~446). So today **every interlaced
DVD and every 1080i Blu-ray is skipped entirely** unless `ENGRAM_CODEC_INTERLACED=deinterlace` is
set by hand. That contradicts the rule above.

The module docstring (line ~86) documents the old behaviour — "Interlaced material is **not
encoded** by default" — and must be rewritten, not preserved.

## Required behaviour

1. **Interlaced source → deinterlace, then encode.** No opt-in flag is required any more.
2. **Progressive source → no `bwdif`.** Not "harmless anyway" — do not add it.
3. The choice is made **per source file**, from the probe, on **every** path: automatic
   classification, forced/pinned class, retry, and the API. One function decides; nothing may
   bypass it.
4. `INTERLACED` stops being a "leave it alone" class. Remove it from the no-op branch so an
   interlaced source gets a real plan with `bwdif` and an ordinary AV1 encode.

## The `deint=` defect — the reason this is not a one-line change

`DEINTERLACE_FILTER` is currently:

```
bwdif=mode=send_frame:parity=auto:deint=interlaced
```

`deint=interlaced` only deinterlaces frames whose **field order is flagged as interlaced**. When
`idet` detects combing in frames the container tags progressive, `bwdif` passes them through
untouched — the source was interlaced, yet the output is still combed. That violates the rule.

**Change the filter so that a source judged interlaced is deinterlaced on every frame:**

```
bwdif=mode=send_frame:parity=auto:deint=all
```

Apply this everywhere the deinterlacer is used — the automatic path **and** the forced/pinned
path. Both must use one shared constant; do not leave two variants.

Keep `mode=send_frame`. It emits one frame per input frame, so 1080i/25 stays 25 fps instead of
becoming 50p. `send_field` would double the frame rate and is wrong here — this is the frame-rate
rule the principal set separately ("the fps should match the content, don't change it").

## Detection quality for 1080i

1080i must be caught reliably, and a wrong "not interlaced" verdict silently leaves combing in.
Review and, where needed, strengthen:

- `IDET_SECONDS = 3` and `SAMPLE_FRACTIONS` — make sure samples spread across the file, not just
  the head, since a Blu-ray may open on a progressive title sequence.
- `IDET_MIN_FRAMES = 20` and `INTERLACED_FRACTION = 0.30` — confirm these still hold for a
  field-paired 1080i source.
- `IDET_PARITY_DOMINANCE = 0.75` — a genuine telecined source can alternate field order; check
  whether this rejects real interlacing. If it does, **fall back to the field_order flag rather
  than declaring progressive**: when in doubt the rule says deinterlace, so err toward
  interlacing, not away from it.
- `field_order` is a secondary signal; keep it as the tie-breaker it already is.

If the probe cannot decide at all, **treat the source as interlaced and deinterlace** — a
deinterlaced progressive frame is visually near-lossless, while combing is permanent. State this
choice explicitly in the docstring and cover it with a test.

## Tests

- Interlaced source, no env var set → a real plan with `bwdif` (not the no-op `INTERLACED` class).
- Progressive source → no `bwdif` in the command.
- The filter string is `deint=all`, and the same constant is used by both the automatic and the
  forced path (assert they are identical, so a future edit cannot diverge them).
- `mode=send_frame` is present and `send_field` is absent.
- A forced/pinned class on an interlaced source still gets `bwdif`.
- An undecidable probe deinterlaces.
- A real 1080i-style sample: synthesise one (interlace two progressive frames by taking alternate
  field lines) and assert the verdict is interlaced and the plan carries `bwdif`. A synthetic
  field-paired source is enough; do not hunt for a Blu-ray.
- Frame rate is not altered: no `-r`, `-fps_mode` or `-vsync` in any command.

## Verify

```
cd backend && uv run pytest tests/unit/ -q     # >= 5448 passed
```

Commit as one focused backend commit. `git add` specific files, never `-A`. Update the module
docstring and `CODEC-CLASSIFIER.md` so the documented behaviour matches the code. Do not push, do
not tag, do not touch CT111 or the live container.

## Report

Files changed, the test count, whether you changed any detection threshold and why, and anything
in this spec you found to be wrong about the code.
