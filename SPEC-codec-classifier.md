# SPEC: Content-aware codec pipeline classifier for the Engram AV1 hook

## Context

Engram (fork at `/opt/engram/src`, container `engram` on CT111, image
`engram-remote-asr:local`) files rips into the library as untouched MakeMKV output, then runs a
post-organize hook (`backend/app/services/encode_hook.py`) that re-encodes the file to AV1.

That hook is **content-blind**: CRF and preset are fixed env vars applied to every file. A
grainy 1080p film, a flat cel-animation episode, and an interlaced 1080i concert all get the
same treatment, which is wrong for all three.

**Task: replace the fixed settings with a classifier that picks a codec pipeline from the
content.** You decide how many classes and what settings each gets, grounded in what the
encoding community actually recommends.

## Hard constraints — read these first

These are measured facts about the live system. Do not contradict them or design around them.

1. **ffmpeg is 5.1.9** (Debian 12). `libsvtav1` and `libx265` are both present and both produce
   10-bit. Verified working command forms:
   - `-c:v libsvtav1 -crf N -preset N -pix_fmt yuv420p10le -svtav1-params "k=v:k=v"` → `av1,yuv420p10le`
   - `-c:v libx265 -crf N -preset medium -pix_fmt yuv420p10le -x265-params "log-level=error"` → `hevc,yuv420p10le`
2. **`film-grain-denoise` defaults to ON** in this SVT build and must be set to `0` explicitly
   unless you deliberately want denoising. Verified: omitting it yields
   `film grain synth / denoising / level: 1 / 1 / 6`; setting `film-grain-denoise=0` yields
   `1 / 0 / 6`. Stacking synthetic grain on real grain is a defect.
3. **No genre data exists in Engram.** `grep -rn "genre" backend/app` returns nothing. `DiscJob`
   has `tmdb_id`, `tmdb_name`, `tmdb_year`, `content_type`, `detected_title`,
   `detected_season`. `DiscTitle` has `video_resolution`, `duration_seconds`, `is_extra`,
   `audio_languages`, `subtitle_languages`, `match_confidence`, `match_details`. **Do not
   design a classifier that depends on TMDB genres** — they are not fetched. If you want genre,
   you must fetch it yourself and you must make that fetch optional and fail-open.
4. **Available ffmpeg filters** (verified present): `idet`, `signalstats`, `scdet`,
   `blackdetect`. These are the basis for deriving content class from the media itself.
5. **The host CPU is an i3-7100T (Kaby Lake), 4 threads, no AV1 hardware.** Every encode is
   software. Preset 6 is the practical floor at DVD resolution; preset 4 is not viable at 1080p.
   Do not propose preset 2-4 for 1080p content.
6. **The hook must stay fail-open.** A classifier error must never fail the rip or lose the file.
7. **Never in-place.** Encode to a temp name, verify, then `replace()`. A failed encode must not
   cost the rip.
8. **Track-preserving.** `-map 0:v:0 -map 0:a? -map 0:s? -c:a copy -c:s copy`, and abort the
   swap on any audio/subtitle count mismatch. This is a standing principal requirement.

## What the classifier may use

At the two call sites in `backend/app/services/finalization_coordinator.py` (lines ~2409 and ~2668),
`disc_title` (a `DiscTitle`) and `job` (a `DiscJob`) are both in scope. Currently the hook is
called with only `org_result.get("final_path")`. **Extend the signature** to pass the
identification context the classifier needs. Suggested payload:

```python
encode_hook.after_organize(
    final_path,
    context={
        "content_type": job.content_type.value if job.content_type else None,
        "detected_title": job.detected_title,
        "tmdb_id": job.tmdb_id,
        "tmdb_year": job.tmdb_year,
        "video_resolution": disc_title.video_resolution,
        "duration_seconds": disc_title.duration_seconds,
        "is_extra": disc_title.is_extra,
        "audio_languages": disc_title.audio_languages,
        "subtitle_languages": disc_title.subtitle_languages,
    },
)
```

Keep the context argument **optional and keyword-only** with a default of `None`, so the
existing call shape and any other caller keep working.

## Import discipline (learned from the live tree)

`app.services.__init__` imports `job_manager`, which pulls in the whole matcher/ASR stack
(`chardet`, `rich`, `ctranslate2`, `librosa`, ...). That means:

- **`codec_classifier.py` must be importable standalone**, with only the standard library and
  `subprocess`/`json`. Do not import from `app.services`, `app.matcher`, or `app.models` at
  module level. If you need a type, use a plain dict.
- **`encode_hook.py` must keep working when imported directly** by the tests
  (`import encode_hook`), so put the classifier import behind a try/except or import it lazily
  inside the function if it would otherwise create a cycle.
- The test file must import the classifier by file path or as a top-level module, not through
  `app.services`.

This keeps the classifier testable in isolation and stops a codec decision from depending on
whether the ASR stack happens to be importable.

## What to build

### 1. A classifier module: `backend/app/services/codec_classifier.py`

It must derive a content class from **two sources**, in this priority order:

**a) Media probes (authoritative, always available).** Run cheap ffprobe/ffmpeg probes on the
source file before encoding:
- `field_order` and/or `idet` on a sample → interlaced vs progressive. This is the single most
  important signal: interlaced content must not be encoded as if it were progressive.
- `width`/`height` → resolution class.
- `r_frame_rate` / `avg_frame_rate` → film (23.976/24) vs PAL (25) vs NTSC (29.97) vs other.
- `color_transfer` / `color_primaries` → SDR vs HDR (PQ/HLG). UHD Blu-ray is PQ.
- A grain estimate. Use `signalstats` or a cheap temporal-difference measure on a sample of
  frames. Graininess is the strongest predictor of whether AV1 will struggle and whether
  `film-grain` synthesis is appropriate.
- An animation-vs-live-action estimate. Reasonable cheap heuristics: flat-region ratio,
  edge density, colour-count / palette concentration, and very low temporal noise in static
  areas. Cel animation has large flat areas, few distinct colours, and hard edges; live action
  has high spatial entropy. **Probe a sample of frames spread across the title, not the first
  N seconds** — the first seconds are often a logo or black.

**b) Identification context (a hint, never the sole basis).** `content_type` (tv/movie),
`is_extra`, `duration_seconds`, `video_resolution`. Use these to disambiguate and to set
defaults, but the media probes win when they disagree — the identification can be wrong and the
file cannot.

**c) Optional TMDB genre enrichment.** If you add it: it must be a single cached TMDB call,
optional, fail-open, and it must never block the encode. The TMDB client already exists at
`backend/app/matcher/tmdb_client.py` (`fetch_movie_details`, `fetch_show_details`); `_tmdb_get_json`
is the low-level helper. If the key is absent or the call fails, fall through to the media
probes silently.

### 2. The class table

**You decide the number of classes and every setting.** Ground each choice in what the
community actually recommends. Start from these sources and cite them in a module docstring:

- JET Encoding Guide (SVT-AV1 parameters, presets, tune, film grain)
  https://jaded-encoding-thaumaturgy.github.io/JET-guide/master/encoding/svtav1/
- Encode Wiki, SVT-AV1 (option reference, pitfalls, HDR metadata)
  https://encode.wiki/tools/software-encoders/svt-av1
- Encode Wiki, film grain / graining
  https://encode.wiki/tools/filtering/graining
- Encode Wiki, preserve colour and HDR
  https://encode.wiki/guides/preserve-color-and-hdr
- Doom9 SVT-AV1 settings thread (grain recipes, preset 5/CRF 26 reports)
  https://forum.doom9.org/showthread.php?t=185159
- Netflix Tech Blog, AV1 film grain synthesis at scale
  https://netflixtechblog.com/av1-film-grain-synthesis-the-awakening-ee09cfdff40b

Known community positions you should reflect (verify them yourself, but do not contradict them
without saying why):
- 10-bit output always, even from 8-bit source.
- `tune=0` for viewing; the mainline default of 1 is PSNR and looks softer.
- `enable-variance-boost=1` — off by default in mainline, protects skin/sky/dark areas.
- `tf-strength=1` (default 3 causes keyframe blocking).
- `film-grain` 4-6 for mild grain, 8-10 for heavy grain, **0 for clean digital/CGI/animation**.
- AV1 is weaker than x265 on fine-detail retention at high bitrate on grainy 1080p film.
  A defensible design therefore uses **x265 for the hardest live-action cases** and AV1
  everywhere else. You may disagree — but if you do, justify it in the docstring.
- For interlaced content, the correct answer is usually to **deinterlace** (or better, leave
  the source alone and flag it) — not to encode interlaced frames with a progressive encoder.
  Consider whether a class should skip transcoding entirely and pass the file through.

A shape like the following is a starting point, **not a requirement** — you may merge, split or
rename classes, and you must justify the final set:

| Class | Detected by | Codec | Approx settings |
|---|---|---|---|
| animation | flat regions, low colour count, hard edges | AV1 | CRF ~26-28, preset 6, film-grain 0 |
| live-action film, grainy | high spatial entropy + high grain estimate | x265 | CRF ~20-22, preset medium, 10-bit |
| live-action, clean | high entropy, low grain | AV1 | CRF ~24-26, preset 6, film-grain 4 |
| interlaced / broadcast | field_order or idet says interlaced | AV1 + deinterlace, or passthrough | decide and justify |
| HDR (PQ/HLG) | color_transfer smpte2084 or arib-std-b67 | AV1 | 10-bit mandatory, no luma-bias on PQ |
| passthrough | very short extras, already-AV1, unknown | none | skip |

### 3. Wire it into the hook

Modify `encode_hook.py` to:
- Call the classifier, log the chosen class and the evidence that drove it
  (e.g. `codec_classifier: class=live_action_grainy grain=0.42 interlaced=no -> x265 crf=20`).
- Build the ffmpeg command from the class's settings.
- Keep every existing guarantee: fail-open, never-in-place, track-preserving, idempotent marker.
- Keep an `ENGRAM_CODEC_CLASSIFIER=0` kill switch that restores the current fixed CRF/preset
  behaviour exactly, so the change can be reverted without a rebuild.
- Retain the existing env overrides as **per-class defaults the classifier may override**, or
  document clearly that they now apply only to the fallback path.

### 4. Tests

Add `tests/test_codec_classifier.py` (match the existing test layout — check how the repo's
tests are organised first, do not assume). Cover at minimum:
- Each class is reachable from a synthetic probe dict.
- An unknown/empty probe falls back to a safe default class and never raises.
- Interlaced detection wins over an animation hint.
- HDR forces 10-bit and suppresses luma-bias.
- The classifier returns a complete, valid settings object for every class.
- `film-grain-denoise=0` is present in every AV1 param string.

Run the suite. Report the actual pass/fail counts.

## Deliverables

1. `backend/app/services/codec_classifier.py` — new module, with a docstring citing the sources above
   and justifying the class set.
2. `backend/app/services/encode_hook.py` — modified to use it, kill switch preserved.
3. `backend/app/services/finalization_coordinator.py` — both call sites pass context.
4. `tests/test_codec_classifier.py` — passing tests.
5. A short `CODEC-CLASSIFIER.md` at repo root: the class table, what each class detects, the
   settings and their rationale, the known limitations, and how to disable it.

## Do not

- Do not rebuild or restart the container. Do not touch `/opt/engram/.env`. Do not encode any
  real disc content. Another agent handles deployment.
- Do not modify anything outside `/opt/engram/src`.
- Do not change the hook's fail-open, never-in-place, or track-preserving behaviour.
- Do not invent ffmpeg options that ffmpeg 5.1 does not support — test the command you emit.
- Do not add a dependency that is not already in the image.

## Definition of done

- Tests pass (report real numbers).
- Every emitted ffmpeg command is verified to parse and produce the intended codec/pix_fmt on a
  synthetic source inside the container (you may use `docker exec engram` for probes only —
  no rebuild, no restart).
- `CODEC-CLASSIFIER.md` exists and matches the code.
- `git status` shows only the intended files, and the work is committed with a clear message.