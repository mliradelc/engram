# Codec classifier

The post-organize encoding hook (`backend/app/services/encode_hook.py`) used to apply one fixed
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
| `interlaced` | idet says interlaced with a dominant field order; or `field_order` says so when idet is inconclusive; **or interlacing cannot be decided at all** | **deinterlace, then encode** (no flag needed) | `bwdif=mode=send_frame:parity=auto:deint=all` (telecined: `fieldmatch,decimate`), then AV1 CRF 24, preset 6, film-grain 0 |
| `hdr` | `color_transfer` = `smpte2084` (PQ) or `arib-std-b67` (HLG) | AV1, or x265 if the title is also grainy | AV1 CRF 24, preset 6 (8 at uhd), film-grain 4 (0 at uhd), **no luminance-qp-bias**; x265 CRF 20 |
| `animation` | animation score >= 0.60 and grain below threshold | AV1 | CRF 27, preset 6 (8 uhd), film-grain 0, variance-boost strength 1 |
| `live_action_grainy` | normalised grain >= 0.40 | **x265** | CRF 20 (sd) / 21, preset medium (fast at uhd), `aq-mode=3` |
| `live_action_clean` | everything else (a progressive source with no grain/animation measurement lands here; a probe too empty to decide interlacing goes to `interlaced`) | AV1 | CRF 23 (sd) / 25, preset 6 (8 uhd), film-grain 4 (0 at uhd), variance-boost strength 2, luminance-qp-bias 25 |

Every output is `yuv420p10le`. Every AV1 command carries `tune=0`, `film-grain-denoise=0`
(this SVT build defaults denoising **on**, which would stack on top of real grain) and
`lp=<ENGRAM_ENCODE_THREADS>`. "Optional" AV1 parameters: `enable-variance-boost=1`,
`variance-boost-strength`, `tf-strength=1`, `sharpness=1`, `luminance-qp-bias` (SDR only),
`tile-columns` (1 at hd, 2 at uhd). AV1 preset 2-4 is never emitted (i3-7100T, software only).

## Deinterlacing: the rule

**An interlaced source is deinterlaced; a progressive source is not; a telecined source is
inverse-telecined.** It is decided per source file, from the probe, by one function
(`codec_classifier.deinterlace_filter`, which returns the filter or none; `needs_deinterlace` is its
boolean form), on every path: automatic classification, a forced class, a pinned class, a retry, and
the API. No class, preset, pin or setting can turn it on or off; there is no opt-in flag any more
(the old `ENGRAM_CODEC_INTERLACED` is ignored).

Three-way, checked in this order:

1. **Telecined** (3:2 pulldown, 23.976 film stored as 29.97 interlaced) -> `fieldmatch,decimate`
   (`IVTC_FILTER`). Checked first because a telecined source is *also* interlaced. Output is 23.976,
   the film's true rate; `bwdif` would keep 29.97 with the duplicated fields baked in.
2. **Interlaced, not telecined** (or **undecided**) -> `bwdif`.
3. **Progressive** -> no filter.

* **Telecine is proven, never assumed.** The signal is idet's `Repeated Fields: Neither/Top/Bottom`
  line (TFF/BFF cannot tell telecine from true interlacing; both read mostly TFF). Telecined if
  `(top + bottom) / (neither + top + bottom) >= 0.15` over at least 20 counted frames **and** the
  stream is ~29.97/30 fps. Measured: progressive 0/144, true interlaced 25i 0/150, telecined 29.97
  72/180 = 0.40 (3:2 pulldown repeats 2 fields in 10). 0.15 sits in the empty gap. After
  `fieldmatch,decimate` the telecined count drops to 0; after `bwdif` it stays, so the signal is
  causal. The 29.97 gate exists because IVTC on true interlaced video is catastrophic and silent:
  25i through `fieldmatch,decimate` comes out at **20 fps with one frame in five destroyed**. 3:2
  pulldown cannot produce any other rate, so a PAL (or unknown-rate) source with repeated fields
  stays on `bwdif`. An undecided probe never selects IVTC.
* `fieldmatch,decimate` carries no `deint=` / `mode=` options; it is a separate constant.
* **Interlaced** -> `bwdif` then an ordinary encode. Under a forced or pinned class the class still
  picks codec/CRF/preset and `bwdif` is put in front of it. An interlaced HDR source keeps its HDR
  plan with `bwdif` in front.
* **Progressive** -> no `bwdif` and no `fieldmatch` in the command at all.
* **Undecided** (no usable idet and no usable `field_order`, idet combed with no consistent field
  order and no usable `field_order`, or the probe failed on the file / the classifier failed) ->
  **deinterlaced**. `bwdif` on a progressive frame is visually near-lossless; combing baked into
  an encode is permanent.
* **Unmeasured** (a probe could not run, timed out, or was killed) is *not* undecided: nothing is
  encoded and the panel shows a failed row with Retry. See "Detection signals".
* The filter is one shared constant, `DEINTERLACE_FILTER = bwdif=mode=send_frame:parity=auto:deint=all`:
  * `deint=all` deinterlaces every frame of a source judged interlaced. The previous
    `deint=interlaced` only touched frames *flagged* interlaced, so a combed source whose frames
    are tagged progressive (common on DVD) came out still combed. Verified on a synthetic
    field-paired 1080i/25 file tagged progressive: `deint=interlaced` left all 50 frames combed,
    `deint=all` left none.
  * `mode=send_frame` emits one frame per input frame: 1080i/25 stays 25 fps (not 50p), 480i/29.97
    stays 29.97. No command carries `-r`, `-fps_mode` or `-vsync`; the frame rate follows the content
    (IVTC's 29.97 -> 23.976 is the filters restoring the true rate, not a forced one).
* If the encoder rejects the class settings and the hook falls to the fixed command, the
  deinterlace filter (`bwdif` or `fieldmatch,decimate`) is kept. The `ENGRAM_CODEC_CLASSIFIER=0` kill switch is unchanged and byte-identical (no `-vf`).
* A marker left by the old "interlaced, left untouched" skip (`codec_class: interlaced`, `av1: null`)
  no longer counts as done, so such a file is encoded the next time the hook or a retry reaches it.

## Detection signals

Every probe has one of three outcomes, and only the first two say anything about the file:

* **ok** (exit 0): its output is evidence.
* **failed** (ffmpeg/ffprobe exited non-zero on its own: cannot open, seek or decode): a property
  of the source, so the key is left out (and an idet that fails on every sample is logged at
  WARNING). Interlacing then falls to `field_order`, or to undecided, as above.
* **aborted** (binary missing, timed out after 60 s, or killed by a signal): a property of the
  *host*, raised as `ProbeAborted`. ffmpeg catches SIGTERM, prints the idet summary of the
  truncated span and exits **255** (measured, ffmpeg 7.1.5; SIGKILL gives -9, ffprobe -15), so a
  killed idet looked like a short valid measurement; before this, a killed idet or pixel probe
  classified a progressive source `live_action_clean` with no error. The hook now records a
  failed row ("Source probe interrupted, nothing encoded: ...") and encodes nothing; it does not
  fall back to the fixed settings, which would encode an unmeasured source without its
  deinterlace filter. No marker is written, so the next organize pass or a Retry re-probes.

  Why raise rather than "undecided": there is no conservative default for an unmeasured source.
  Undecided means `bwdif`, which is wrong for a telecined source (IVTC is never chosen without
  measurement, by design), and the same kill blinds the grain/animation probes, so the class
  would be a guess too. The cause is transient, a retry gets the truth, and the original stays
  in the library.

* **Interlacing**: `ffmpeg -vf idet` on five 3-second samples spread across the title (12/30/48/66/84 %
  of the duration, not the first seconds, which are often a logo or black, and a Blu-ray may open on a
  progressive title sequence). Interlaced if at least 30 % of decided frames are TFF/BFF **and** one
  field order dominates (>= 75 %). A coin-flip TFF/BFF split can be idet misreading sharp progressive
  detail (observed on synthetic test patterns), so on its own it is not trusted: `field_order`
  decides (`tt/bb/tb/bt` = interlaced, `progressive` = progressive), and with no usable
  `field_order` the source is undecided, which deinterlaces. It is never read as progressive by
  itself. With fewer than 20 decided frames idet is ignored and `field_order` decides the same way.
  Fewer than 30 % combed frames is progressive whatever `field_order` says (progressive film
  flagged `tt` is common on DVD). A file under 30 s, or with no duration, is sampled from the start only.
  The same idet runs also accumulate the `Repeated Fields` counters that decide telecine (above).
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

* Bit depth follows the source: 8-bit stays 8-bit, 10-bit stays 10-bit, always 4:2:0. This
  departs from Encode Wiki's "always feed 10-bit" on purpose: the rule is to preserve the
  source. HDR and sources whose depth cannot be read get 10-bit (it cannot truncate); 12-bit
  is clamped to 10 (SVT-AV1 has no 12-bit profile). The kill-switch command still has no
  `-pix_fmt`.
* `tune=0` (Encode Wiki, and what the hook already shipped). **JET keeps `tune=1`**; we do not.
* Variance boost on, `tf-strength=1`, `sharpness=1`, tiles per JET. Strength 1 for animation (Encode Wiki).
* Film grain: 4 on clean live action (JET: dithering for gradients; Encode Wiki: ~8 is for *normally
  grainy* film, which here goes to x265 instead). **Animation uses 0**, where Encode Wiki suggests
  ~4 for hand-drawn: 10-bit already covers banding, and Doom9 reports synthesis does little on
  grain-free content. Revisit if anime shows banding (more likely now that 8-bit sources stay
  8-bit).
* Grainy live action goes to x265. With `film-grain-denoise=0` SVT keeps real grain and pays for
  it; JET puts the AV1/x265 crossover near CRF 20. Denoise-then-synthesise was rejected: it
  removes detail and JET calls SVT's denoiser poor.
* Interlaced is **deinterlaced and encoded** (see "Deinterlacing: the rule"). This replaces the
  earlier "not encoded by default" behaviour, which skipped every interlaced DVD and 1080i Blu-ray.
* A **forced or pinned** class decides codec, CRF and preset only. Deinterlacing is the source's:
  before a forced class is applied, the same idet sampling runs (stream probe + idet, none of the
  pixel probes), and an interlaced source gets `bwdif` (a telecined one `fieldmatch,decimate`)
  whichever class was chosen. A pin cannot produce combed output. `passthrough` encodes nothing and skips the probe.
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
* **Mixed telecine/video** (a 29.97 disc that is part film, part native video, e.g. a
  documentary or video-edited credits) is a single per-file decision. At >= 0.15 repeated fields
  (roughly >= 37 % of the sampled material telecined) the whole file is IVTC'd and its video
  sections are decimated to 23.976 (judder there); below it the whole file gets `bwdif` and the
  film sections keep their pulldown. Only hard telecine is seen; soft (flag-based) telecine decodes
  as progressive and correctly gets no filter. The telecine threshold was checked only on synthetic
  sources (`testsrc2` + `telecine=pattern=23`), not on a real NTSC DVD.
* idet can misread fast, sharp progressive motion; that errs toward deinterlacing, which is
  near-lossless on a progressive frame.
* Already-skipped interlaced files are not swept automatically: they are encoded when the hook next
  sees them, or on a retry from the encode panel.
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
| `ENGRAM_ENCODE_HOOK=1` | enables the hook (unchanged) |
| `ENGRAM_CODEC_CLASSIFIER=0` | **kill switch**: restores the original fixed command exactly (`-c:v libsvtav1 -crf 22 -preset 6 -svtav1-params tune=0:lp=4`, no `-pix_fmt`); the classifier is not even loaded |
| ~~`ENGRAM_CODEC_INTERLACED`~~ | **removed, ignored.** Deinterlacing follows the source, not a setting |
| `ENGRAM_ENCODE_THREADS` | SVT `lp` / x265 `pools`; applies on every path |
| `ENGRAM_ENCODE_CRF`, `ENGRAM_ENCODE_PRESET` | **fallback path only** (kill switch, classifier failure, or encoder rejecting the class settings). They no longer influence classified encodes |
| `ENGRAM_ENCODE_STAGING` | output directory (default `/staging/encode-out`; see the runbook below if you have output under the old default) |
| `ENGRAM_ENCODE_MAX_CONCURRENT` | max simultaneous ffmpeg encodes process-wide, any codec (default `1` = serial; `0`, negative or non-integer = unlimited). Extra encodes block (INFO log) until a slot frees; the wait does not count against the encode timeout |

Unset `ENGRAM_CODEC_CLASSIFIER` means the classifier is on. To revert without a rebuild, set
`ENGRAM_CODEC_CLASSIFIER=0` in the container environment and restart the service.

`encode_hook.after_organize(final_path, *, context=None)`: `context` is optional and keyword-only;
`finalization_coordinator.py` passes content_type, detected_title, tmdb_id/year, video_resolution,
duration_seconds, is_extra and the language lists at both call sites.

### Renaming ENGRAM_AV1_* to ENGRAM_ENCODE_*

Every `ENGRAM_AV1_*` variable was renamed to `ENGRAM_ENCODE_*` (same suffix:
`ENGRAM_AV1_HOOK` → `ENGRAM_ENCODE_HOOK`, `_STAGING`, `_CRF`, `_PRESET`, `_THREADS`,
`_MAX_CONCURRENT`). The old names are **not read**. A deployment whose `.env` still uses them
runs with **encoding disabled**, because the hook is off unless `ENGRAM_ENCODE_HOOK=1`. Rename
them in the deployment's `.env` (back it up first), then recreate the container:

```bash
sed -i.bak 's/^\(\(export \)\?\)ENGRAM_AV1_/\1ENGRAM_ENCODE_/' /opt/engram/.env
```

So this cannot happen silently, the backend **refuses to start** while any `ENGRAM_AV1_*` key
is in its environment; the error names each stale key and its replacement. Do not skip the
rename: removing the old keys alone also leaves encoding off.

### Marker name stays `.engram-av1.json`

The per-file marker keeps its `.engram-av1.json` suffix and its `"av1"` key (the output path) even
though the hook also writes x265. Every file already encoded carries that marker; renaming the
suffix or the key would make each of them look unconverted and re-encode the whole library.
`test_marker_suffix_is_frozen` guards it.

## Runbook: retiring the old `/staging/av1-out` directory

The `ENGRAM_ENCODE_STAGING` default was `/staging/av1-out` until the hook was renamed; it is now
`/staging/encode-out`. A deployment that set `ENGRAM_ENCODE_STAGING` explicitly (for example to a
NAS mount) is unaffected by the new default, but may still have stale output under the old directory.

On the reference deployment that directory sits **inside the container's 24 GB rootfs**, not on the
NAS mount, and holds about **831 MB** of output written before `ENGRAM_ENCODE_STAGING` was pointed at
the NAS (files dated 2026-11-13/14). Nothing writes there any more.

This is a **manual, operator-run step**. Do not automate it into the app: a startup routine that
deletes a directory because its default name changed is a data-loss bug.

1. **Confirm the hook is idle.** `curl -s localhost:8000/api/encoding` must return `"active": []`.
   Do not continue while an encode is running or a retry is queued.
2. **Confirm what is live.** `docker exec engram printenv ENGRAM_ENCODE_STAGING` should name the
   NAS path, not `/staging/av1-out`. If it is unset or still points at `/staging/av1-out`, stop
   here: that directory is the live target, not a leftover.
3. **Measure.** `docker exec engram du -sh /staging/av1-out` and `ls -la` it; expect about 831 MB.
4. **Keep anything worth keeping.** Move it with `docker exec engram mv <file> <live target>/`.
   (`docker exec` needs the container running, so do this before stopping it.)
5. **Stop the container**, remove the old directory (`rm -r /staging/av1-out` on the volume that
   backs it, or `rmdir` if step 4 emptied it), and **start it again**.
6. **Confirm the reclaim.** Free space on the rootfs should rise by about the size from step 3.
