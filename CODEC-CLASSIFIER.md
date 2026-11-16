# Codec classifier

The post-organize AV1 hook (`backend/app/services/av1_hook.py`) used to apply one fixed
CRF/preset to every file. It now asks `backend/app/services/codec_classifier.py` to pick a codec
pipeline from the **media itself**. The Engram identification context is a hint only; where the
probes and the context disagree, the probes win.

Everything the hook did before is unchanged: fail-open, never in place (encode to a temp name,
verify, then `replace()`), track-preserving (`-map 0:v:0 -map 0:a? -map 0:s? -c:a copy -c:s copy`,
swap aborted on any audio/subtitle count mismatch), idempotent size+mtime marker.

## Class table

Evaluated top to bottom; first match wins. `sd` = width < 1200, `hd` = 1200-2999, `uhd` = 3000+.

| Class | Detected by | Action | Settings |
|---|---|---|---|
| `passthrough` | source codec already AV1; or `is_extra` and shorter than 180 s | none (file left as filed, marker written) | n/a |
| `interlaced` | idet says interlaced with a dominant field order, or `field_order` says so when idet is inconclusive | **none by default**; opt in to deinterlace | opt-in: `bwdif=send_frame`, then AV1 CRF 24, preset 6 (8 at uhd), film-grain 0 |
| `hdr` | `color_transfer` = `smpte2084` (PQ) or `arib-std-b67` (HLG) | AV1, or x265 if the title is also grainy | AV1 CRF 24, preset 6 (8 at uhd), film-grain 4 (0 at uhd), **no luminance-qp-bias**; x265 CRF 20 |
| `animation` | animation score >= 0.60 and grain below threshold | AV1 | CRF 27, preset 6 (8 uhd), film-grain 0, variance-boost strength 1 |
| `live_action_grainy` | normalised grain >= 0.40 | **x265** | CRF 20 (sd) / 21, preset medium (fast at uhd), `aq-mode=3` |
| `live_action_clean` | everything else, **and the fallback for an empty/unusable probe** | AV1 | CRF 23 (sd) / 25, preset 6 (8 uhd), film-grain 4 (0 at uhd), variance-boost strength 2, luminance-qp-bias 25 |

Every output is `yuv420p10le`. Every AV1 command carries `tune=0`, `film-grain-denoise=0`
(this SVT build defaults denoising **on**, which would stack on top of real grain) and
`lp=<ENGRAM_AV1_THREADS>`. "Optional" AV1 parameters: `enable-variance-boost=1`,
`variance-boost-strength`, `tf-strength=1`, `sharpness=1`, `luminance-qp-bias` (SDR only),
`tile-columns` (1 at hd, 2 at uhd). AV1 preset 2-4 is never emitted (i3-7100T, software only).

## Detection signals

All probes are cheap and best-effort; a failed probe just leaves its key out.

* **Interlacing**: `ffmpeg -vf idet` on five 3-second samples spread across the title (12/30/48/66/84 %
  of the duration, not the first seconds, which are often a logo or black). Interlaced if at least 30 %
  of decided frames are TFF/BFF **and** one field order dominates (>= 75 %). A coin-flip TFF/BFF
  split is idet misreading sharp progressive detail (observed on synthetic test patterns), so it is
  not trusted over the container's `field_order`. With fewer than 20 decided frames idet is ignored
  and `field_order` (`tt/bb/tb/bt` = interlaced) decides.
* **Resolution, frame rate, colour**: `ffprobe` width/height, `avg_frame_rate`, `color_transfer`.
  Frame-rate class (film24 / pal25 / ntsc30 / high / other) is logged as evidence; it does not
  change settings.
* **Grain**: native-resolution 192x192 centre crop, two frames per sample. Median over 8x8 blocks
  of the mean absolute residual from the 4-neighbour mean, scaled `residual / 3` and clamped to
  0..1. Median, so a few edge blocks do not dominate. Near-black frames are discarded.
* **Animation**: frame downscaled to 160x90. Score = 0.45 x flat-block ratio (4x4 blocks with
  range <= 3) + 0.35 x palette concentration (top 32 of 4096 colour bins) + 0.20 x hard-edge ratio.
  Near-black pixels/blocks (letterbox bars) are excluded. Measured grain over threshold vetoes it.
* **HDR metadata**: mastering display and MaxCLL/MaxFALL are read from the first frame and passed
  to the encoder **explicitly** (`mastering-display`/`content-light` for SVT, `master-display`/`max-cll`
  for x265). ffmpeg only passes these through from 7.0; production is 5.1.x. Verified that explicit
  values override the source's on both encoders. Missing metadata is omitted, never invented.
  The hook also refuses the swap if the output's `color_transfer` differs from the source's.
* **Context hints** (`content_type`, `is_extra`, `duration_seconds`, `video_resolution`): `is_extra`
  plus short duration gives passthrough; `tv` lowers the animation threshold by 0.05 and `movie`
  lowers the grain threshold by 0.05, so a hint can only tip a borderline measurement, never
  overrule a clear one; `video_resolution` is used only when the probe has no dimensions. Where the
  context contradicts the probe, the probe wins and the disagreement is logged in `hints=`.
* **TMDB genre**: deliberately not used. Engram stores no genre, and a network call on the
  encode path buys little over measurement.

Each file produces one log line, e.g.
`codec_classifier: class=live_action_grainy grain=0.80 animation=0.38 interlaced=no hdr=no res=hd fps_class=film24 -> x265 crf=21 preset=medium`.

## Rationale

Sources: JET Encoding Guide (SVT-AV1), Encode Wiki (SVT-AV1, graining, preserve colour and HDR),
the Doom9 SVT-AV1 thread; the Netflix grain-synthesis post is cited by the spec but returned HTTP
403 here, so it informed nothing beyond the spec's summary. Full URLs are in the module docstring.

* 10-bit everywhere: banding, even from 8-bit sources (Encode Wiki).
* `tune=0` (Encode Wiki, and what the hook already shipped). **JET keeps `tune=1`**; we do not.
* Variance boost on, `tf-strength=1`, `sharpness=1`, tiles per JET. Strength 1 for animation (Encode Wiki).
* Film grain: 4 on clean live action (JET: dithering for gradients; Encode Wiki: ~8 is for *normally
  grainy* film, which here goes to x265 instead). **Animation uses 0**, where Encode Wiki suggests
  ~4 for hand-drawn: 10-bit already covers banding, and Doom9 reports synthesis does little on
  grain-free content. Revisit if anime shows banding.
* Grainy live action goes to x265. With `film-grain-denoise=0` SVT keeps real grain and pays for
  it; JET puts the AV1/x265 crossover near CRF 20. Denoise-then-synthesise was rejected: it
  removes detail and JET calls SVT's denoiser poor.
* Interlaced is **not** encoded by default. Deinterlacing is irreversible, and telecined film
  needs IVTC rather than a deinterlacer; guessing wrong bakes in combing. The original is already in
  the library, so skipping loses nothing.
* AV1 preset 6 floor; UHD gets 8 and no grain synthesis (SVT warns synthesis above preset 6 is a
  large compute cost).

## Known limitations

* **Thresholds are provisional.** Grain/animation thresholds were fitted on synthetic sources
  (noise over gradients, test patterns, Mandelbrot). Grain scaled monotonically with noise
  strength, and test patterns scored 0.77-0.80 against 0.38-0.56 for textured content, but **no real
  disc rip was probed**. Expect to retune `GRAIN_THRESHOLD` / `ANIMATION_THRESHOLD` after looking at
  the `codec_classifier:` log lines from a handful of real titles.
* DVD MPEG-2 compression noise and mosquito noise read as grain; a clean DVD may land in `grainy`
  (x265 is the slower encode, so that is the costly mistake) and noisy anime may miss `animation`.
* Telecined or mixed progressive/interlaced film is usually reported as interlaced and skipped.
  idet can also misread fast, sharp progressive motion. Both fail safe: nothing is encoded.
* Passthrough writes a marker, so flipping `ENGRAM_CODEC_INTERLACED=deinterlace` later does not
  revisit already-skipped files (delete the `*.engram-av1.json` marker to retry).
* HDR10+ / Dolby Vision dynamic metadata is dropped by both encoders, as before.
* x265 output is HEVC in an `.mkv` named like the source. The hook only writes it to the staging
  directory; confirm the curator/media server accepts HEVC there before enabling grainy-class
  output.
* **Not verified against production.** Tests ran on ffmpeg 7.1.5 / SVT-AV1 2.3.0. Production is
  ffmpeg 5.1.9 on Debian 12, whose SVT-AV1 is much older and may lack variance-boost, `tf-strength`,
  `luminance-qp-bias`, or HDR params. Two safeguards, neither exercised on the real build:
  before each encode the hook runs a 6-frame synthetic test encode (cached per command) and
  degrades to *essential parameters only* (tune, film-grain, film-grain-denoise, lp), then to the
  fixed legacy command if that is rejected. An encoder that **silently ignores** an unknown
  parameter is not detectable this way, so on the old build "optional" settings may simply not apply.
  Run one synthetic encode on the real image before relying on this.
* Probing costs a few seconds per file (about 5 idet runs plus 5 pixel runs); negligible against an encode.

## Configuration and disabling

| Variable | Effect |
|---|---|
| `ENGRAM_AV1_HOOK=1` | enables the hook (unchanged) |
| `ENGRAM_CODEC_CLASSIFIER=0` | **kill switch**: restores the original fixed command exactly (`-c:v libsvtav1 -crf 22 -preset 6 -svtav1-params tune=0:lp=4`, no `-pix_fmt`); the classifier is not even loaded |
| `ENGRAM_CODEC_INTERLACED=deinterlace` | encode interlaced sources through `bwdif` instead of leaving them alone |
| `ENGRAM_AV1_THREADS` | SVT `lp` / x265 `pools`; applies on every path |
| `ENGRAM_AV1_CRF`, `ENGRAM_AV1_PRESET` | **fallback path only** (kill switch, classifier failure, or encoder rejecting the class settings). They no longer influence classified encodes |
| `ENGRAM_AV1_STAGING` | output directory (unchanged) |
| `ENGRAM_AV1_MAX_CONCURRENT` | max simultaneous ffmpeg encodes process-wide, any codec (default `1` = serial; `0`, negative or non-integer = unlimited). Extra encodes block (INFO log) until a slot frees; the wait does not count against the encode timeout |

Unset `ENGRAM_CODEC_CLASSIFIER` means the classifier is on. To revert without a rebuild, set
`ENGRAM_CODEC_CLASSIFIER=0` in the container environment and restart the service.

`av1_hook.after_organize(final_path, *, context=None)`: `context` is optional and keyword-only;
`finalization_coordinator.py` passes content_type, detected_title, tmdb_id/year, video_resolution,
duration_seconds, is_extra and the language lists at both call sites.
