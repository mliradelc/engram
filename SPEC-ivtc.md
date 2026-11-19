# SPEC: IVTC for telecined sources (measured, not guessed)

Repo `/home/hermes/.hermes/engram-work`, branch `feat/remote-asr`, HEAD `67dec98`.
Backend baseline: **5471 passed, 10 skipped, 1 xfailed**. Do not regress it.

Read `~/.hermes/skills/devops/engram-ripper-ops/references/interlacing-and-ivtc.md` first — it
carries the measurements below.

## Why

The principal approved handling telecined film properly: *"it's better to follow your guidance."*
His sources are DVDs and Blu-rays, including 1080i. Three source kinds exist and each needs a
different treatment. Measured on CT111 with synthetic sources:

| Source | `fieldmatch,decimate` | `bwdif` |
|---|---|---|
| Progressive | — | softens for nothing |
| True interlaced 25p | **→ 20 fps, 1 frame in 5 destroyed** | correct |
| Telecined 29.97 | **→ 23.976 fps, correct** | 29.97, artefacts baked in |

So IVTC must **never** run blind. The failing case is catastrophic and silent: a genuine
interlaced DVD-era TV episode would come out stuttering at 20 fps.

## The discriminator: `idet`'s Repeated Fields line

The current `_probe_idet` parses only the detection rows:

```
Single frame detection: TFF: … BFF: … Progressive: … Undetermined: …
```

**It ignores the line that matters.** Measured:

```
progressive 24p : Repeated Fields: Neither: 144  Top: 0  Bottom: 0
interlaced 25i  : Repeated Fields: Neither: 150  Top: 0  Bottom: 0
telecined 29.97 : Repeated Fields: Neither: 108  Top: 36 Bottom: 36   <-- pulldown
```

Proof it is causal: after `fieldmatch,decimate` the telecined sample reads **Top 0 Bottom 0**;
after `bwdif` it still reads **Top 36**, because interpolating a field does not remove a duplicate
one.

## Implement

1. **Capture repeated fields.** Extend `_probe_idet` and its regex to also parse
   `Repeated Fields: Neither: N Top: N Bottom: N`, accumulating across samples like the other
   counters. Keep the existing "last summary instance wins" behaviour and the TFF/BFF counters
   unchanged — other logic depends on them.

2. **Add a telecine verdict** alongside `interlaced_verdict`, returning
   `(is_telecined, repeated_fraction, basis)` where the fraction is
   `(top + bottom) / (neither + top + bottom)`.
   Threshold: **≥ 0.15** is telecined. Rationale to record in a comment: 3:2 pulldown duplicates
   2 of every 5 fields, i.e. ~0.40 of frames carry a repeat; clean progressive and clean interlaced
   both measure 0.00, so 0.15 sits in an empty region and does not need tuning.

3. **Three-way decision in `needs_deinterlace`** (rename if clearer), returning the filter to
   apply — not just a boolean:
   - telecined → `fieldmatch,decimate`
   - interlaced and not telecined → `DEINTERLACE_FILTER` (`bwdif=…:deint=all`)
   - progressive → no filter
   - undecided → `DEINTERLACE_FILTER` (keep the deliberate bias to deinterlacing)

   **Telecine wins over plain interlacing**: a telecined source is also interlaced, so the check
   order matters.

4. **`fieldmatch,decimate` takes no `deint=` argument.** It is a separate constant; do not append
   the bwdif options to it. Both must reach the encoder through the one decision function, so the
   forced/pinned/retry paths cannot diverge.

5. **Frame rate.** `fieldmatch,decimate` legitimately changes 29.97 → 23.976 because that is the
   true original rate. Do **not** add `-r`/`-fps_mode`/`-vsync` to force anything; let the filters
   set it. `bwdif` keeps `mode=send_frame` so 1080i/25 stays 25.

6. Update the docstring and `CODEC-CLASSIFIER.md`.

## Tests

Use synthetic sources via ffmpeg (available in the container and in CI's ffmpeg):

- **Telecined sample** (`testsrc2` at `24000/1001` + `telecine=pattern=23`) → verdict telecined,
  plan carries `fieldmatch` and `decimate`, and the encode output is ~23.976 fps.
- **True interlaced** (`rate=50` + `interlace=scan=tff`) → plan carries `bwdif`, **not**
  `fieldmatch`, and output stays 25 fps. This is the regression guard for the 20 fps disaster.
- **Progressive** → neither filter.
- **Repeated-field parsing**: a captured idet line with Top/Bottom > 0 parses; a clean line yields
  0; the accumulator sums across samples.
- **Precedence**: a probe that is both interlaced and telecined chooses IVTC.
- **Undecided probe** → `bwdif`.
- No `-r`, `-fps_mode` or `-vsync` in any generated command.

If a test needs a real ffmpeg run, keep the clip short (≤3 s) and small (≤320×240) so the suite
stays fast; the existing synthetic-1080i test already pays this cost, so follow its pattern for
skip-if-no-ffmpeg and temp paths.

## Verify

```
cd backend && uv run pytest tests/unit/ -q      # >= 5471 passed
```

One focused backend commit, specific files, never `git add -A`. Do not push, tag, or touch CT111.

## Report

Files changed, test count, the exact repeated-field threshold you used and why, and anything in
this spec you found wrong.
