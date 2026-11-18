"""Content-aware codec pipeline classifier for the Engram post-organize hook.

The encoding hook used to apply one fixed CRF/preset to every file.  This module
looks at the *media itself* (ffprobe/ffmpeg probes) and picks a codec pipeline
for it.  The Engram identification context (tv/movie, is_extra, duration,
resolution) is a **hint only**: where it disagrees with the probes the probes
win, because the identification can be wrong and the file cannot.

Import discipline: stdlib only at module level (no ``app.*`` imports), so this
file is importable and testable on its own, independent of the ASR stack that
``app.services`` drags in.  Its public entry points raise exactly one exception,
:class:`ProbeAborted`, when a probe could not run or was killed (an unmeasured
source must fail visibly, not be classified); everything else is handled.

Class set (six, evaluated in this order; first match wins)
----------------------------------------------------------
``passthrough``          already AV1, or a very short extra  -> leave file alone
``interlaced``           idet / field_order says interlaced, -> bwdif (or IVTC
                         or interlacing cannot be decided      when telecined), then AV1
``hdr``                PQ / HLG transfer                  -> AV1 10-bit, or
                         x265 when the HDR title is also heavily grained
``animation``            flat regions, small palette        -> AV1, no grain synth
``live_action_grainy``   high measured grain                -> x265 10-bit
``live_action_clean``    everything else (the safe default) -> AV1 10-bit,
                         light grain synthesis

Why this set: each class differs in a decision that changes the *command*, not
just a number.  Interlacing decides the deinterlace filter; HDR decides
metadata handling and forbids luma-bias; animation decides grain synthesis and
variance-boost strength; grain decides the codec.  Finer genre splits (sitcom
vs. drama, CGI vs. anime) are not separable by cheap probes and would be
unvalidated settings dressed up as precision, so they are not attempted.

Settings and the sources behind them
------------------------------------
Sources read while designing this (and where they disagree, what was chosen):

* JET Encoding Guide, SVT-AV1:
  https://jaded-encoding-thaumaturgy.github.io/JET-guide/master/encoding/svtav1/
  CRF 20-30 (x265 may beat AV1 below ~20), ``enable-variance-boost=1``,
  ``tf-strength`` 3 -> 1 (keyframe blocking), leave ``film-grain-denoise`` off
  (built-in denoiser is poor), ``film-grain>=4`` even on clean sources for
  gradient dithering, ``tile-columns`` 1 at 1080p / 2 at 4K, luminance-qp-bias
  on SDR/HLG but **not PQ**.  It keeps ``tune=1``; we do not (below).
* Encode Wiki, SVT-AV1: https://encode.wiki/tools/software-encoders/svt-av1
  ``tune=0`` for watched video (default 1 optimises PSNR), grain ~8 normal
  live action / 10-15 very noisy / ~4 hand-drawn animation / off for clean
  CGI, variance-boost strength 1 for flat animation, always feed 10-bit,
  synthetic grain stacked on real grain is a defect, SVT-AV1 is 4:2:0 only.
* Encode Wiki, graining: https://encode.wiki/tools/filtering/graining
  synthesis is the cheapest way to keep grain but SVT encodes the *original*
  frames (``film-grain-denoise=0``), so heavy real grain still costs bitrate.
* Encode Wiki, preserve colour and HDR:
  https://encode.wiki/guides/preserve-color-and-hdr
  keep HDR 10-bit, tags pass through ffmpeg, mastering-display / content-light
  pass through only from ffmpeg 7.0 -- hence this module reads them from the
  source and passes them explicitly (production ffmpeg is 5.1.x).
* Doom9 SVT-AV1 thread: https://forum.doom9.org/showthread.php?t=185159
  preset 5 / CRF 26 reports; "CRF 26 too blurry for anything but anime";
  grain 4 for 2D/3D animation, 8 normal, 10-15 noisy; synthesis does little on
  grain-free sources; some decoders mishandle synthesised grain.
* Netflix Tech Blog, AV1 film grain synthesis:
  https://netflixtechblog.com/av1-film-grain-synthesis-the-awakening-ee09cfdff40b
  (cited by the task spec; the page returned HTTP 403 to the fetcher, so it
  informed nothing here beyond the spec's own summary.)

Where this module deliberately departs from a source:

* ``tune=0`` (Encode Wiki, and the hook's existing behaviour) rather than
  JET's ``tune=1``.  Viewing-quality tune, kept consistent with what the hook
  already shipped.
* Animation uses ``film-grain=0`` (spec / Doom9: synthesis does little on
  grain-free content) rather than Encode Wiki's ~4.  10-bit output already
  covers the gradient-banding argument for 4; 0 also avoids the SVT compute
  warning and decoder-compat risk.  Revisit if banding shows up in anime.
* Clean live action uses ``film-grain=4`` (JET's dither argument), not the 8
  recommended for *normally grainy* film: the class is defined as low grain.
* Grainy live action goes to **x265**, not AV1: with ``film-grain-denoise=0``
  SVT keeps real grain and pays for it, and the community puts the AV1 /
  x265 crossover for detail retention around CRF 20.  AV1 with denoise on was
  rejected -- it removes detail and the denoiser is called poor by JET.
* Output bit depth follows the source (8-bit stays 8-bit, 10-bit stays
  10-bit), not Encode Wiki's "always feed 10-bit": the principal's rule is to
  preserve the source's bit depth.  HDR and unknown-depth sources get 10-bit.
* An operator-forced class decides codec / CRF / preset only.  Resolution, bit
  depth and deinterlacing are the source's (:func:`forced_class_plan`): an
  interlaced source gets ``bwdif`` (a telecined one IVTC) under any forced class.

Deinterlacing (the principal's rule): **an interlaced source is deinterlaced,
a progressive source is not.**  It is decided per source file from the probe
by :func:`deinterlace_filter` (three-way: IVTC / bwdif / nothing), on every
path (automatic, forced, pinned, retry, API), never by a class, preset, pin or
setting; there is no opt-in flag.

* Telecined (3:2 pulldown: 23.976 film stored as 29.97 interlaced) ->
  ``fieldmatch,decimate`` (:data:`IVTC_FILTER`), checked FIRST because a
  telecined source is also interlaced.  Proven, never assumed, by idet's
  ``Repeated Fields`` line (:func:`telecine_verdict`): progressive and true
  interlaced sources both measure 0 repeated fields, hard telecine ~0.40.  IVTC
  restores the true 23.976 fps; ``bwdif`` would keep 29.97 with the duplicated
  fields baked in.  IVTC on *true* interlaced video is catastrophic (25 fps ->
  20 fps, one frame in five destroyed), hence the proof and the NTSC-rate gate.
* Interlaced, not telecined -> ``bwdif`` (:data:`DEINTERLACE_FILTER`), then an ordinary AV1
  encode.  ``deint=all``: every frame of a source judged interlaced is
  deinterlaced, including frames the container tags progressive (with
  ``deint=interlaced`` those passed through still combed).
* ``mode=send_frame``: one frame out per frame in, so 1080i/25 stays 25 fps and
  480i/29.97 stays 29.97; no command carries ``-r`` / ``-fps_mode`` / ``-vsync``.
* Progressive -> no ``bwdif`` at all.
* **Undecided -> deinterlace.**  When neither idet nor ``field_order`` can tell
  (or the probe failed on the file), the source is treated as interlaced: bwdif on
  a progressive frame is visually near-lossless, combing in an encode is
  permanent.  The original stays in the library either way.  Undecided never
  means IVTC: only a measured telecine verdict selects ``fieldmatch``.
* **Unmeasured -> no encode.**  Undecided is a *measurement* that could not
  tell.  A probe that never ran, timed out or was killed is not a measurement
  at all, and raises :class:`ProbeAborted` instead (see there for why).


Presets: preset 6 is the floor for AV1 (i3-7100T, software only); UHD also uses 6.
Preset 2-4 is never emitted.  x265 uses ``slow`` at HD, ``medium`` at SD, ``fast`` at UHD.

Calibration honesty: the grain/animation thresholds below were fitted on
synthetic sources (ffmpeg ``noise`` over gradients; test patterns).  They are
provisional until checked against real disc rips; see CODEC-CLASSIFIER.md.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import statistics
import subprocess
import tempfile
from dataclasses import dataclass, field
from fractions import Fraction

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Class names and thresholds
# --------------------------------------------------------------------------

PASSTHROUGH = "passthrough"
INTERLACED = "interlaced"
HDR = "hdr"
ANIMATION = "animation"
LIVE_ACTION_GRAINY = "live_action_grainy"
LIVE_ACTION_CLEAN = "live_action_clean"

CLASSES = (PASSTHROUGH, INTERLACED, HDR, ANIMATION, LIVE_ACTION_GRAINY, LIVE_ACTION_CLEAN)
# What a progressive source with no grain/animation measurement falls back to.
# (A probe too empty to decide interlacing goes to INTERLACED: see needs_deinterlace.)
DEFAULT_CLASS = LIVE_ACTION_CLEAN

# Output bit depth follows the source ("preserve bit"): never reduce a 10-bit
# source to 8-bit, never pad an 8-bit source to 10-bit.  Chroma is always 4:2:0
# (SVT-AV1 supports nothing else).  ``PIX_FMT`` is what a source of unknown
# depth gets: 10-bit cannot truncate, so it is the safe answer when we cannot
# prove the source is 8-bit.  HDR is always 10-bit.
PIX_FMT_8BIT = "yuv420p"
PIX_FMT_10BIT = "yuv420p10le"
PIX_FMT = PIX_FMT_10BIT
OUTPUT_PIX_FMTS = (PIX_FMT_8BIT, PIX_FMT_10BIT)
_PIX_DEPTH_RE = re.compile(r"(\d+)(?:le|be)$")

# The one deinterlacer, used by the automatic path and the forced/pinned path
# alike (both read this constant; there is no second variant).
#   mode=send_frame  one frame out per frame in: 1080i/25 stays 25 fps, never 50p
#                    (the frame rate follows the content and is not changed).
#   parity=auto      field order from each frame's flag (TFF when unflagged).
#   deint=all        deinterlace EVERY frame.  ``deint=interlaced`` only touches
#                    frames whose flag says interlaced, so a combed source whose
#                    frames are tagged progressive (common on DVD) passed through
#                    still combed.  The filter is only added once the *source* has
#                    been judged interlaced, so "all" is exactly the rule.
DEINTERLACE_FILTER = "bwdif=mode=send_frame:parity=auto:deint=all"

# Inverse telecine for 3:2-pulldown sources (see telecine_verdict).  fieldmatch
# rebuilds the original progressive frames from matching fields; decimate drops
# the one duplicate in every five, so 29.97 -> 23.976, the film's true rate.
# Deliberately a separate constant with NO bwdif options: ``deint=`` / ``mode=``
# mean nothing to fieldmatch/decimate.  No ``-r`` / ``-fps_mode`` / ``-vsync``:
# the filters set the rate.
IVTC_FILTER = "fieldmatch,decimate"
_DEINT_FILTERS = (DEINTERLACE_FILTER, IVTC_FILTER)

SHORT_EXTRA_SECONDS = 180  # an *extra* shorter than this is not worth an encode
INTERLACED_FRACTION = 0.30  # (tff+bff) / (tff+bff+prog) over idet-decided frames
IDET_MIN_FRAMES = 20  # fewer decided frames than this and idet is not trusted
IDET_PARITY_DOMINANCE = 0.75  # real interlace has ONE field order; a TFF/BFF coin-flip
# is idet misreading sharp progressive detail (measured on synthetic test patterns).
# Below it idet's "interlaced" is not trusted *on its own*: the container's
# field_order decides, and with no usable field_order the source is undecided,
# which deinterlaces (see interlaced_verdict).  It never declares progressive.
GRAIN_THRESHOLD = 0.40  # normalised grain at/above this -> grainy
ANIMATION_THRESHOLD = 0.60  # animation score at/above this -> animation
HINT_NUDGE = 0.05  # how far a tv/movie hint may move a threshold (never a verdict)
# Telecine: (top + bottom) / (neither + top + bottom) from idet's "Repeated
# Fields" counter.  3:2 pulldown duplicates 2 fields in every 10 (2 of every 5
# frames carry a repeat), so hard telecine measures ~0.40 (measured: 36+36 of
# 180).  Clean progressive and clean true-interlaced sources both measure 0.00
# (as do static and noise-only interlaced clips), so 0.15 sits in an empty
# region and needs no tuning.  It also means a disc that is mostly video with
# some telecined film stays on bwdif: under 0.15 means under ~37 % of the
# sampled material is telecined, and IVTC would decimate the video parts.
TELECINE_FRACTION = 0.15

_INTERLACED_FIELD_ORDERS = {"tt", "bb", "tb", "bt"}
_HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}  # PQ, HLG
_PQ_TRANSFER = "smpte2084"

# Pixel-probe geometry (see probe_media).
SAMPLE_FRACTIONS = (0.12, 0.30, 0.48, 0.66, 0.84)  # spread across the title
CROP = 192  # native-resolution centre crop used for the grain estimate
SMALL_W, SMALL_H = 160, 90  # downscaled frame used for the animation estimate
FRAMES_PER_SAMPLE = 2
IDET_SECONDS = 3
PROBE_TIMEOUT = 60

# --------------------------------------------------------------------------
# Plan (the settings object)
# --------------------------------------------------------------------------


@dataclass
class CodecPlan:
    """Everything needed to build the video part of an ffmpeg command."""

    class_name: str
    codec: str  # "av1" | "hevc" | "none"
    reason: str = ""
    crf: int | None = None
    preset: str | None = None
    source_pix_fmt: str | None = None  # the probed source's pix_fmt; decides pix_fmt
    svt_required: list[tuple[str, str]] = field(default_factory=list)
    svt_optional: list[tuple[str, str]] = field(default_factory=list)
    x265_required: list[tuple[str, str]] = field(default_factory=list)
    x265_optional: list[tuple[str, str]] = field(default_factory=list)
    video_filters: list[str] = field(default_factory=list)
    hdr: bool = False
    evidence: dict = field(default_factory=dict)

    @property
    def is_passthrough(self) -> bool:
        return self.codec == "none"

    @property
    def pix_fmt(self) -> str:
        """Output pixel format, derived from the source, never from the class."""
        return output_pix_fmt(self.source_pix_fmt, hdr=self.hdr)

    @property
    def output_codec_name(self) -> str | None:
        """ffprobe ``codec_name`` the verified output must carry."""
        return {"av1": "av1", "hevc": "hevc"}.get(self.codec)

    def video_args(self, threads: str | int = 4, *, optional: bool = True) -> list[str]:
        """ffmpeg arguments for the video stream (everything after the maps)."""
        if self.is_passthrough:
            return []
        args: list[str] = []
        if self.video_filters:
            args += ["-vf", ",".join(self.video_filters)]
        if self.codec == "av1":
            params = list(self.svt_required) + (list(self.svt_optional) if optional else [])
            params.append(("lp", str(threads)))
            args += ["-c:v", "libsvtav1", "-crf", str(self.crf), "-preset", str(self.preset)]
            args += ["-pix_fmt", self.pix_fmt]
            args += ["-svtav1-params", ":".join(f"{k}={v}" for k, v in params)]
        else:
            params = list(self.x265_required) + (list(self.x265_optional) if optional else [])
            params.append(("pools", str(threads)))
            args += ["-c:v", "libx265", "-crf", str(self.crf), "-preset", str(self.preset)]
            args += ["-pix_fmt", self.pix_fmt]
            args += ["-x265-params", ":".join(f"{k}={v}" for k, v in params)]
        return args

    def describe(self) -> str:
        """One log line: class, the evidence that drove it, and the outcome."""
        ev = self.evidence
        bits = [f"class={self.class_name}"]
        for key in ("grain", "animation", "interlaced", "telecined", "hdr", "res", "fps_class"):
            if key in ev and ev[key] is not None:
                val = ev[key]
                bits.append(f"{key}={val:.2f}" if isinstance(val, float) else f"{key}={val}")
        if ev.get("hints"):
            bits.append("hints=" + ",".join(ev["hints"]))
        if self.is_passthrough:
            return " ".join(bits) + f" -> passthrough ({self.reason})"
        tail = f"{'x265' if self.codec == 'hevc' else 'av1'} crf={self.crf} preset={self.preset}"
        return " ".join(bits) + " -> " + tail


def source_bit_depth(pix_fmt: str | None) -> int | None:
    """Bits per component of an ffmpeg ``pix_fmt``; None when it cannot be told.

    ``yuv420p10le`` / ``p010le`` / ``gray10le`` -> 10; ``yuv420p`` / ``yuvj420p``
    / ``nv12`` (no depth suffix) -> 8.
    """
    if not pix_fmt:
        return None
    name = str(pix_fmt).strip().lower()
    m = _PIX_DEPTH_RE.search(name)
    if m:
        depth = int(m.group(1))
        return depth if 8 <= depth <= 16 else None
    if name.startswith(("yuv", "yuvj", "nv", "gray")):
        return 8
    return None


def output_pix_fmt(source_pix_fmt: str | None, *, hdr: bool = False) -> str:
    """4:2:0 at the source's depth: 8-bit stays 8-bit, deeper becomes 10-bit.

    Unknown depth and HDR get 10-bit (cannot truncate).  A 12-bit source is
    clamped to 10-bit: SVT-AV1 has no 12-bit profile.
    """
    if hdr:
        return PIX_FMT_10BIT
    depth = source_bit_depth(source_pix_fmt)
    if depth == 8:
        return PIX_FMT_8BIT
    return PIX_FMT_10BIT


def validate_plan(plan: CodecPlan) -> list[str]:
    """Return a list of problems with a plan (empty means valid)."""
    problems: list[str] = []
    if plan.class_name not in CLASSES:
        problems.append(f"unknown class {plan.class_name!r}")
    if plan.codec not in ("av1", "hevc", "none"):
        problems.append(f"bad codec {plan.codec!r}")
    if plan.is_passthrough:
        return problems
    if plan.pix_fmt not in OUTPUT_PIX_FMTS:
        problems.append(f"pix_fmt {plan.pix_fmt!r} is not 4:2:0 8/10-bit")
    if plan.hdr and plan.pix_fmt != PIX_FMT_10BIT:
        problems.append("HDR output must be 10-bit")
    src_depth = source_bit_depth(plan.source_pix_fmt)
    if src_depth is not None and (source_bit_depth(plan.pix_fmt) or 0) < min(src_depth, 10):
        problems.append("output is shallower than the source")
    if plan.crf is None or not 0 <= plan.crf <= 51:
        problems.append("crf missing or out of range")
    if plan.codec == "av1":
        keys = dict(plan.svt_required + plan.svt_optional)
        if keys.get("film-grain-denoise") != "0":
            problems.append("film-grain-denoise=0 missing")
        if plan.preset is None or not plan.preset.isdigit() or int(plan.preset) < 5:
            problems.append("AV1 preset must be 5..13 (software-only host)")
        if plan.hdr and "luminance-qp-bias" in keys:
            problems.append("luma-bias must be suppressed on HDR")
    elif plan.preset is None or plan.preset not in X265_PRESETS:
        problems.append("bad x265 preset")
    return problems


X265_PRESETS = {
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
}

# --------------------------------------------------------------------------
# Plan construction
# --------------------------------------------------------------------------


def resolution_class(width: int | None, height: int | None, hint: str | None = None) -> str:
    """``sd`` | ``hd`` | ``uhd``.  Width first: letterboxed 1080p has a short height."""
    if width:
        return "uhd" if width >= 3000 else ("hd" if width >= 1200 else "sd")
    if height:
        return "uhd" if height >= 1800 else ("hd" if height >= 700 else "sd")
    if hint:
        h = str(hint).lower()
        if "4k" in h or "2160" in h or "uhd" in h:
            return "uhd"
        if "1080" in h or "720" in h:
            return "hd"
    return "sd" if hint else "hd"


def _av1_base(res: str, *, grain: int, vb_strength: int, luma_bias: bool) -> tuple[list, list]:
    required = [
        ("tune", "0"),
        ("film-grain", str(grain)),
        ("film-grain-denoise", "0"),  # SVT default is ON in this build; see spec
    ]
    optional = [
        ("enable-variance-boost", "1"),
        ("variance-boost-strength", str(vb_strength)),
        ("tf-strength", "1"),
        ("sharpness", "1"),
    ]
    if luma_bias:
        optional.append(("luminance-qp-bias", "25"))
    if res == "hd":
        optional.append(("tile-columns", "1"))
    elif res == "uhd":
        optional.append(("tile-columns", "2"))
    return required, optional


def _hdr_metadata_params(hdr_meta: dict | None) -> tuple[list, list]:
    """(svt params, x265 params) carrying the source's own mastering metadata."""
    svt: list[tuple[str, str]] = []
    x265: list[tuple[str, str]] = []
    if not hdr_meta:
        return svt, x265
    md = hdr_meta.get("mastering_display")
    if md:
        try:
            g, b, r, wp = md["green"], md["blue"], md["red"], md["white_point"]
            lmax, lmin = md["max_luminance"], md["min_luminance"]
            svt.append(
                (
                    "mastering-display",
                    f"G({_fmt(g[0])},{_fmt(g[1])})B({_fmt(b[0])},{_fmt(b[1])})"
                    f"R({_fmt(r[0])},{_fmt(r[1])})WP({_fmt(wp[0])},{_fmt(wp[1])})"
                    f"L({_fmt(lmax)},{_fmt(lmin)})",
                )
            )
            c = 50000
            x265.append(
                (
                    "master-display",
                    f"G({round(g[0] * c)},{round(g[1] * c)})B({round(b[0] * c)},{round(b[1] * c)})"
                    f"R({round(r[0] * c)},{round(r[1] * c)})WP({round(wp[0] * c)},{round(wp[1] * c)})"
                    f"L({round(lmax * 10000)},{round(lmin * 10000)})",
                )
            )
        except (KeyError, TypeError, IndexError):
            pass
    cl = hdr_meta.get("content_light")
    if cl:
        try:
            pair = f"{int(cl['max_content'])},{int(cl['max_average'])}"
            svt.append(("content-light", pair))
            x265.append(("max-cll", pair))
        except (KeyError, TypeError, ValueError):
            pass
    return svt, x265


def _fmt(x: float) -> str:
    return f"{x:.4f}".rstrip("0").rstrip(".") or "0"


def _x265_preset(res: str) -> str:
    """slow for HD grainy BD (accepted time cost on the i3-7100T), medium SD, fast UHD."""
    return {"sd": "medium", "hd": "slow"}.get(res, "fast")


def _x265(
    res: str,
    crf: int,
    *,
    hdr: bool,
    hdr_meta: dict | None,
    evidence: dict,
    class_name: str,
    reason: str,
) -> CodecPlan:
    optional = [("aq-mode", "3")]  # aq-mode 3 biases bits toward dark scenes
    if hdr:
        optional += [("hdr10-opt", "1"), ("repeat-headers", "1")]
        optional += _hdr_metadata_params(hdr_meta)[1]
    return CodecPlan(
        class_name=class_name,
        codec="hevc",
        reason=reason,
        crf=crf,
        preset=_x265_preset(res),
        # no-sao / strong-intra-smoothing=0 are required, not optional: the hook's
        # fallback drops optional params, and without these two smoothing filters
        # off the grainy class defeats its own purpose (x265 3.5 default tools:
        # b-intra strong-intra-smoothing deblock sao).
        x265_required=[("log-level", "error"), ("no-sao", "1"), ("strong-intra-smoothing", "0")],
        x265_optional=optional,
        hdr=hdr,
        evidence=evidence,
    )


def plan_for_class(
    class_name: str,
    *,
    res: str = "hd",
    hdr_meta: dict | None = None,
    grainy_hdr: bool = False,
    evidence: dict | None = None,
    reason: str = "",
    source_pix_fmt: str | None = None,
) -> CodecPlan:
    """Build the settings for one class.  Complete and valid for every class.

    ``source_pix_fmt`` (the probed source's) decides the output bit depth; the
    class never does.
    """
    plan = _plan_for_class(
        class_name,
        res=res,
        hdr_meta=hdr_meta,
        grainy_hdr=grainy_hdr,
        evidence=evidence,
        reason=reason,
    )
    plan.source_pix_fmt = source_pix_fmt
    return plan


def _plan_for_class(
    class_name: str,
    *,
    res: str,
    hdr_meta: dict | None,
    grainy_hdr: bool,
    evidence: dict | None,
    reason: str,
) -> CodecPlan:
    ev = dict(evidence or {})
    preset = "6"
    # UHD gets no film-grain synthesis: the synthesis/grain-table compute cost at
    # 4K is too high for this software-only CPU, independent of the preset number.
    grain_ok = res != "uhd"

    if class_name == PASSTHROUGH:
        return CodecPlan(
            class_name=class_name, codec="none", reason=reason or class_name, evidence=ev
        )

    if class_name == INTERLACED:
        # bwdif, then a conservative AV1 encode.  Film grain synthesis is off:
        # grain on interlaced sources is unmeasured (field combing inflates the
        # grain estimate).
        req, opt = _av1_base(res, grain=0, vb_strength=2, luma_bias=True)
        return CodecPlan(
            INTERLACED,
            "av1",
            reason or "interlaced source, deinterlaced",
            24,
            preset,
            svt_required=req,
            svt_optional=opt,
            video_filters=[DEINTERLACE_FILTER],
            evidence=ev,
        )

    if class_name == ANIMATION:
        req, opt = _av1_base(res, grain=0, vb_strength=1, luma_bias=True)
        return CodecPlan(
            class_name,
            "av1",
            reason or "flat/low-palette content",
            27,
            preset,
            svt_required=req,
            svt_optional=opt,
            evidence=ev,
        )

    if class_name == LIVE_ACTION_GRAINY:
        crf = 20 if res == "sd" else 21
        return _x265(
            res,
            crf,
            hdr=False,
            hdr_meta=None,
            evidence=ev,
            class_name=class_name,
            reason=reason or "heavy real grain",
        )

    if class_name == HDR:
        if grainy_hdr:
            return _x265(
                res,
                20,
                hdr=True,
                hdr_meta=hdr_meta,
                evidence=ev,
                class_name=class_name,
                reason=reason or "grainy HDR",
            )
        req, opt = _av1_base(res, grain=4 if grain_ok else 0, vb_strength=2, luma_bias=False)
        svt_meta = _hdr_metadata_params(hdr_meta)[0]
        return CodecPlan(
            class_name,
            "av1",
            reason or "HDR",
            24,
            preset,
            svt_required=req,
            svt_optional=opt + svt_meta,
            hdr=True,
            evidence=ev,
        )

    # live_action_clean (also the safe default for any unknown name)
    crf = 23 if res == "sd" else 24  # JET: SD sources want a lower CRF
    req, opt = _av1_base(res, grain=4 if grain_ok else 0, vb_strength=2, luma_bias=True)
    return CodecPlan(
        LIVE_ACTION_CLEAN,
        "av1",
        reason or "default",
        crf,
        preset,
        svt_required=req,
        svt_optional=opt,
        evidence=ev,
    )


def deinterlaced_plan(
    res: str,
    evidence: dict,
    source_pix_fmt: str | None = None,
    reason: str = "",
    video_filter: str = DEINTERLACE_FILTER,
) -> CodecPlan:
    """The plan for an interlaced (or undecided) source: bwdif, then AV1.

    ``video_filter`` is :func:`deinterlace_filter`'s answer; a telecined source
    passes :data:`IVTC_FILTER` and gets ``fieldmatch,decimate`` instead.
    """
    plan = plan_for_class(
        INTERLACED, res=res, evidence=evidence, reason=reason, source_pix_fmt=source_pix_fmt
    )
    return _with_deint_filter(plan, video_filter)


def _with_deint_filter(plan: CodecPlan, video_filter: str | None) -> CodecPlan:
    """Put exactly the decided deinterlace filter (or none) in front of the plan's own."""
    rest = [f for f in plan.video_filters if f not in _DEINT_FILTERS]
    plan.video_filters = ([video_filter] if video_filter else []) + rest
    return plan


def forced_class_plan(class_name: str, probe: dict | None, *, reason: str = "") -> CodecPlan:
    """Settings for an operator-forced class, with the source's own properties.

    The class picks codec / CRF / preset.  It does **not** decide what belongs to
    the source: resolution, bit depth and deinterlacing come from ``probe``
    (the flat dict :func:`probe_interlace` returns, plus width/height/pix_fmt).
    An interlaced source gets ``bwdif`` (a telecined one ``fieldmatch,decimate``)
    whichever class was forced, so a pin cannot produce combed output; the
    decision is :func:`deinterlace_filter`, the same one the automatic path uses
    (an undecided source deinterlaces).
    ``hdr`` / ``interlaced`` are not forceable and are the caller's to refuse;
    this function does not re-check them.
    """
    p = probe if isinstance(probe, dict) else {}
    width = int(_num(p.get("width"), 0) or 0) or None
    height = int(_num(p.get("height"), 0) or 0) or None
    res = resolution_class(width, height)
    ev: dict = {"res": res, "forced": True}
    plan = plan_for_class(
        class_name, res=res, evidence=ev, reason=reason, source_pix_fmt=p.get("pix_fmt")
    )
    if plan.is_passthrough:
        return plan  # nothing is encoded, so there is nothing to deinterlace
    vf, inter_ev = deinterlace_filter(p)
    plan.evidence.update(inter_ev)
    return _with_deint_filter(plan, vf)


# --------------------------------------------------------------------------
# Classification (pure: probe dict + context dict -> plan)
# --------------------------------------------------------------------------


def _num(value, default=None):
    try:
        if value is None or isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def fps_class(fps: float | None) -> str | None:
    if not fps:
        return None
    if abs(fps - 23.976) < 0.05 or abs(fps - 24) < 0.05:
        return "film24"
    if abs(fps - 25) < 0.05:
        return "pal25"
    if abs(fps - 29.97) < 0.05 or abs(fps - 30) < 0.05:
        return "ntsc30"
    if fps >= 49:
        return "high"
    return "other"


def interlaced_verdict(probe: dict) -> tuple[bool | None, float | None, str]:
    """(interlaced?, idet fraction, basis).  ``None`` means undecided.

    idet on a spread of samples beats the container's ``field_order`` flag:
    a progressive film flagged ``tt`` is common on DVD, and the reverse exists.
    ``field_order`` is the tie-breaker when idet is too thin to trust or is
    combed-looking with no consistent field order.  This function only reports;
    :func:`needs_deinterlace` turns ``None`` into "deinterlace".
    """
    idet = probe.get("idet")
    if isinstance(idet, dict):
        tff, bff, prog = (int(_num(idet.get(k), 0) or 0) for k in ("tff", "bff", "progressive"))
        decided = tff + bff + prog
        if decided >= IDET_MIN_FRAMES:
            frac = (tff + bff) / decided
            if frac >= INTERLACED_FRACTION:
                if max(tff, bff) / (tff + bff) >= IDET_PARITY_DOMINANCE:
                    return True, frac, "idet"
                # Combed-looking but with no consistent field order (idet can
                # misread sharp progressive detail this way, and a real source
                # can switch field order at edits).  Not trusted on its own and
                # never read as progressive: field_order decides, else undecided.
                basis = "idet_mixed_parity"
                fo = str(probe.get("field_order") or "").lower()
                if fo in _INTERLACED_FIELD_ORDERS:
                    return True, frac, basis + "+field_order"
                if fo == "progressive":
                    return False, frac, basis + "+field_order"
                return None, frac, basis
            return False, frac, "idet"
    fo = str(probe.get("field_order") or "").lower()
    if fo in _INTERLACED_FIELD_ORDERS:
        return True, None, "field_order"
    if fo == "progressive":
        return False, None, "field_order"
    return None, None, "none"


def telecine_verdict(probe: dict) -> tuple[bool, float | None, str]:
    """(telecined?, repeated-field fraction, basis).  Never ``None``: unproven is False.

    The discriminator is idet's ``Repeated Fields`` counter, not TFF/BFF: hard
    3:2 pulldown and true interlacing both look mostly TFF, but only pulldown
    *duplicates* fields.  Measured: progressive and true interlaced 0 of N,
    telecined 29.97 36+36 of 180 (~0.40); after ``fieldmatch,decimate`` the
    count drops to 0, after ``bwdif`` it stays, so the signal is causal.

    Telecine is only ever declared when it is proven: fewer than
    :data:`IDET_MIN_FRAMES` counted frames, a fraction under
    :data:`TELECINE_FRACTION`, or a frame rate that 3:2 pulldown cannot produce
    (anything but ~29.97/30, or unknown; PAL 25 in particular, where IVTC
    destroys one frame in five) all answer False, which falls back to the
    ordinary interlaced/progressive decision.
    """
    idet = probe.get("idet")
    if not isinstance(idet, dict):
        return False, None, "no_repeated_fields"
    neither, top, bottom = (
        int(_num(idet.get(k), 0) or 0)
        for k in ("repeated_neither", "repeated_top", "repeated_bottom")
    )
    counted = neither + top + bottom
    if counted < IDET_MIN_FRAMES:
        return False, None, "repeated_fields_too_few"
    frac = (top + bottom) / counted
    if frac < TELECINE_FRACTION:
        return False, frac, "repeated_fields"
    fpc = fps_class(_num(probe.get("fps")))
    if fpc != "ntsc30":
        return False, frac, f"repeated_fields_but_fps_{fpc or 'unknown'}"
    return True, frac, "repeated_fields"


def deinterlace_filter(probe: dict | None) -> tuple[str | None, dict]:
    """THE deinterlace decision: (filter to apply or None, evidence).  Never raises.

    Three-way, in this order:

    1. telecined (:func:`telecine_verdict`) -> :data:`IVTC_FILTER`.  First,
       because a telecined source is also interlaced and would otherwise get
       bwdif, which keeps the repeated fields and the 29.97 rate.
    2. interlaced, or **undecided** (:func:`interlaced_verdict` is None) ->
       :data:`DEINTERLACE_FILTER` (bwdif): on a progressive frame it is visually
       near-lossless, while combing left in an encode is permanent.
    3. progressive -> None.

    Undecided never selects IVTC: ``fieldmatch,decimate`` on true interlaced
    video turns 25 fps into 20, so it runs only on a measured telecine verdict.
    It depends on the source's probe only, never on a class, preset, pin or
    setting.  Every path (automatic, forced, pinned, retry, API) reaches the
    encoder through this function.
    """
    p = probe if isinstance(probe, dict) else {}
    try:
        inter, frac, basis = interlaced_verdict(p)
    except Exception:  # noqa: BLE001 - an unreadable probe is an undecided one
        inter, frac, basis = None, None, "error"
    try:
        tele, tfrac, tbasis = telecine_verdict(p)
    except Exception:  # noqa: BLE001 - unproven telecine is no telecine
        tele, tfrac, tbasis = False, None, "error"
    ev: dict = {
        "interlaced": "yes" if inter else ("no" if inter is False else "unknown"),
        "interlaced_basis": basis,
        "telecined": "yes" if tele else "no",
        "telecine_basis": tbasis,
    }
    if frac is not None:
        ev["interlaced_frac"] = round(frac, 3)
    if tfrac is not None:
        ev["repeated_frac"] = round(tfrac, 3)
    if tele:
        return IVTC_FILTER, ev
    if inter is not False:
        return DEINTERLACE_FILTER, ev
    return None, ev


def needs_deinterlace(probe: dict | None) -> tuple[bool, dict]:
    """(any deinterlace filter?, evidence): :func:`deinterlace_filter` as a boolean."""
    vf, ev = deinterlace_filter(probe)
    return vf is not None, ev


def _classify(probe: dict, context: dict) -> CodecPlan:
    ev: dict = {}
    hints: list[str] = []

    width = int(_num(probe.get("width"), 0) or 0) or None
    height = int(_num(probe.get("height"), 0) or 0) or None
    res = resolution_class(width, height, context.get("video_resolution"))
    ev["res"] = res
    ev["fps_class"] = fps_class(_num(probe.get("fps")))
    if not (width or height) and context.get("video_resolution"):
        hints.append("resolution_from_context")
    elif context.get("video_resolution"):
        ctx_res = resolution_class(None, None, context["video_resolution"])
        if ctx_res != res and ctx_res != "hd":  # "hd" is the no-information default
            hints.append(f"context_resolution_{ctx_res}_overruled_by_probe_{res}")

    duration = _num(probe.get("duration"))
    if duration is None:
        duration = _num(context.get("duration_seconds"))

    # 1. passthrough: already AV1, or a very short extra.
    if str(probe.get("codec_name") or "").lower() == "av1":
        ev["hints"] = hints
        return plan_for_class(PASSTHROUGH, res=res, evidence=ev, reason="source already AV1")
    if context.get("is_extra") and duration is not None and duration < SHORT_EXTRA_SECONDS:
        ev["hints"] = hints
        return plan_for_class(
            PASSTHROUGH, res=res, evidence=ev, reason=f"short extra ({duration:.0f}s)"
        )

    # 2. telecined / interlaced (or undecided) wins over everything below,
    #    including an animation hint: it is IVTC'd or deinterlaced, then encoded.
    vf, inter_ev = deinterlace_filter(probe)
    ev.update(inter_ev)
    ev["hints"] = hints
    transfer = str(probe.get("color_transfer") or "").lower()
    is_hdr = transfer in _HDR_TRANSFERS
    if vf and not is_hdr:
        if vf == IVTC_FILTER:
            reason = "telecined source, inverse-telecined"
        elif ev["interlaced"] == "yes":
            reason = "interlaced source, deinterlaced"
        else:
            reason = "interlacing undecided, deinterlaced to be safe"
        return deinterlaced_plan(res, ev, reason=reason, video_filter=vf)

    # 3. HDR.  (Deinterlacing never drops HDR handling: an HDR source that also
    #    needs it keeps the HDR plan and gets the decided filter in front.)
    ev["hdr"] = transfer if is_hdr else "no"

    grain = _num(probe.get("grain"))
    anim = _num(probe.get("animation"))
    ev["grain"] = grain
    ev["animation"] = anim

    ctype = str(context.get("content_type") or "").lower()
    grain_thr, anim_thr = GRAIN_THRESHOLD, ANIMATION_THRESHOLD
    # The hint only moves a threshold a little; a measurement far from the
    # threshold is unaffected, which is what "hint, never the sole basis" means.
    if ctype == "tv":
        anim_thr -= HINT_NUDGE
    elif ctype == "movie":
        grain_thr -= HINT_NUDGE
    if ctype in ("tv", "movie"):
        nudged = (anim is not None and anim_thr <= anim < ANIMATION_THRESHOLD) or (
            grain is not None and grain_thr <= grain < GRAIN_THRESHOLD
        )
        if nudged:
            hints.append(f"content_type={ctype}_tipped_borderline")

    grainy = grain is not None and grain >= grain_thr
    if is_hdr:
        meta = probe.get("hdr_meta") if isinstance(probe.get("hdr_meta"), dict) else None
        plan = plan_for_class(
            HDR,
            res=res,
            hdr_meta=meta,
            grainy_hdr=grainy,
            evidence=ev,
            reason=f"transfer={transfer}",
        )
        return _with_deint_filter(plan, vf)

    # 4. animation, 5. grainy live action, 6. default.
    # Cel animation has flat fields; measurable grain contradicts that, so a high
    # animation score does not count when grain is also over threshold.
    if anim is not None and anim >= anim_thr and not grainy:
        return plan_for_class(ANIMATION, res=res, evidence=ev)
    if grainy:
        return plan_for_class(LIVE_ACTION_GRAINY, res=res, evidence=ev)
    reason = "default" if grain is None and anim is None else "low grain"
    return plan_for_class(LIVE_ACTION_CLEAN, res=res, evidence=ev, reason=reason)


def _undecided_plan(error: str, reason: str) -> CodecPlan:
    """What a failure falls back to: nothing is known, so it deinterlaces."""
    return deinterlaced_plan(
        "hd", {"error": error, "interlaced": "unknown", "interlaced_basis": "error"}, reason=reason
    )


def classify(probe: dict | None, context: dict | None = None) -> CodecPlan:
    """Pick a plan from a probe dict and an optional identification context.

    Never raises: an error yields the undecided-source plan (deinterlace, then
    AV1) with the error recorded.
    """
    try:
        p = probe if isinstance(probe, dict) else {}
        plan = _classify(p, context if isinstance(context, dict) else {})
        # Bit depth belongs to the source, whichever class was chosen.
        plan.source_pix_fmt = p.get("pix_fmt")
        return plan
    except Exception as exc:  # noqa: BLE001 - classifier must never break the hook
        logger.warning("codec_classifier: classify failed (%s); deinterlacing to be safe", exc)
        return _undecided_plan(str(exc), "classify error")


# --------------------------------------------------------------------------
# Probes (ffprobe / ffmpeg).
#
# A probe has three outcomes, and only two of them say anything about the file:
#
#   ok       ran and exited 0: its output is evidence.
#   failed   ran and exited non-zero on its own (ffmpeg could not open, seek or
#            decode): a property of the *source*, so a missing key, as before.
#   aborted  never ran (missing binary, OSError), timed out, or was killed by a
#            signal: a property of the *host*.  Its output is not evidence --
#            ffmpeg catches SIGTERM, prints the idet summary for the truncated
#            span it saw and exits 255, so a killed idet reads like a short,
#            valid measurement.  An aborted probe raises :class:`ProbeAborted`.
#
# Why aborting raises instead of yielding an "undecided" verdict: see
# :class:`ProbeAborted`.
# --------------------------------------------------------------------------

PROBE_OK = "ok"
PROBE_FAILED = "failed"
PROBE_ABORTED = "aborted"

# fftools/ffmpeg.c exits 255 when it caught SIGINT/SIGTERM and shut down
# "normally" (``Exiting normally, received signal 15.`` at -v info).  Measured on
# ffmpeg 7.1.5: SIGTERM -> 255, SIGKILL -> -9.  Its genuine errors use other
# codes (missing input 254, unknown filter 8, generic 1).  ffprobe installs no
# handler, so it dies with a negative return code.  encode_hook relies on the
# same 255 convention for the encode itself.
FFMPEG_INTERRUPTED_RC = 255


class ProbeAborted(RuntimeError):
    """A probe could not run, timed out, or was killed: nothing is known.

    Raised (rather than reporting "undecided") because no default is correct
    for a source nobody measured.  The undecided interlace answer is bwdif,
    which is wrong for a telecined source (it keeps the repeated fields and the
    29.97 rate; IVTC is deliberately never chosen without measurement), and the
    same kill takes out the pixel probes, so grain/animation are blind too and
    the class collapses to ``live_action_clean``.  The cause is the host (a
    disk-space guard, the OOM killer, a timeout under load), so a retry measures
    the truth; the original stays in the library.  The encode hook turns this
    into a failed row on the panel, never into an encode.
    """

    def __init__(self, run: ProbeRun):
        self.run = run
        super().__init__(f"{run.name} probe {run.detail}")


@dataclass(frozen=True)
class ProbeRun:
    """One probe subprocess and which of the three outcomes it had."""

    name: str  # the program, for messages ("ffmpeg", "ffprobe")
    outcome: str  # PROBE_OK | PROBE_FAILED | PROBE_ABORTED
    returncode: int | None = None
    stdout: bytes = b""
    stderr: bytes = b""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome == PROBE_OK

    @property
    def aborted(self) -> bool:
        return self.outcome == PROBE_ABORTED


def _signal_name(num: int) -> str:
    try:
        return f"{num} ({signal.Signals(num).name})"
    except ValueError:
        return str(num)


def _run(cmd: list[str], timeout: float | None = None) -> ProbeRun:
    """Run one probe and say which of ok / failed / aborted it was.  Never raises."""
    timeout = PROBE_TIMEOUT if timeout is None else timeout
    name = os.path.basename(str(cmd[0])) if cmd else "probe"
    try:
        cp = subprocess.run(cmd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return ProbeRun(name, PROBE_ABORTED, detail=f"timed out after {timeout}s")
    except (subprocess.SubprocessError, OSError) as exc:
        return ProbeRun(name, PROBE_ABORTED, detail=f"could not run: {exc}")
    rc = cp.returncode
    out, err = cp.stdout or b"", cp.stderr or b""
    if rc is not None and rc < 0:
        detail = f"killed by signal {_signal_name(-rc)}"
        return ProbeRun(name, PROBE_ABORTED, rc, out, err, detail)
    if rc == FFMPEG_INTERRUPTED_RC and name.startswith("ffmpeg"):
        detail = f"exited {rc} (interrupted by a signal)"
        return ProbeRun(name, PROBE_ABORTED, rc, out, err, detail)
    if rc != 0:
        return ProbeRun(name, PROBE_FAILED, rc, out, err, f"exited {rc}")
    return ProbeRun(name, PROBE_OK, rc, out, err)


def _checked(run: ProbeRun) -> ProbeRun:
    """``run`` unless it was aborted, in which case raise :class:`ProbeAborted`."""
    if run.aborted:
        raise ProbeAborted(run)
    return run


def _parse_rate(text: str | None) -> float | None:
    try:
        if not text or text in ("0/0", "N/A"):
            return None
        return float(Fraction(text))
    except (ValueError, ZeroDivisionError):
        return None


def _probe_stream(path: str) -> dict:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-print_format",
        "json",
        "-show_entries",
        "stream=codec_name,width,height,field_order,r_frame_rate,avg_frame_rate,"
        "color_transfer,color_primaries,color_space,pix_fmt:format=duration",
        path,
    ]
    run = _checked(_run(cmd))
    if not run.ok:
        return {}
    try:
        data = json.loads(run.stdout or b"{}")
    except ValueError:
        return {}
    streams = data.get("streams") or [{}]
    s = streams[0] if streams else {}
    out = {
        "codec_name": s.get("codec_name"),
        "width": s.get("width"),
        "height": s.get("height"),
        "field_order": s.get("field_order"),
        "pix_fmt": s.get("pix_fmt"),
        "color_transfer": s.get("color_transfer"),
        "color_primaries": s.get("color_primaries"),
        "color_space": s.get("color_space"),
        "fps": _parse_rate(s.get("avg_frame_rate")) or _parse_rate(s.get("r_frame_rate")),
        "duration": _num((data.get("format") or {}).get("duration")),
    }
    return {k: v for k, v in out.items() if v is not None}


def _frac(text) -> float | None:
    try:
        return float(Fraction(str(text)))
    except (ValueError, ZeroDivisionError):
        return None


def _probe_hdr_meta(path: str) -> dict | None:
    """Mastering display + content light from the first frame, as plain numbers."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-read_intervals",
        "%+#1",
        "-show_frames",
        "-show_entries",
        "frame=side_data_list",
        "-print_format",
        "json",
        path,
    ]
    run = _checked(_run(cmd))
    if not run.ok:
        return None
    try:
        frames = json.loads(run.stdout or b"{}").get("frames") or []
    except ValueError:
        return None
    meta: dict = {}
    for frame in frames[:1]:
        for sd in frame.get("side_data_list") or []:
            kind = sd.get("side_data_type", "")
            if kind == "Mastering display metadata":
                try:
                    pt = {k: _frac(sd[k]) for k in sd if k != "side_data_type"}
                    meta["mastering_display"] = {
                        "red": (pt["red_x"], pt["red_y"]),
                        "green": (pt["green_x"], pt["green_y"]),
                        "blue": (pt["blue_x"], pt["blue_y"]),
                        "white_point": (pt["white_point_x"], pt["white_point_y"]),
                        "max_luminance": pt["max_luminance"],
                        "min_luminance": pt["min_luminance"],
                    }
                    md = meta["mastering_display"]
                    flat = [md["max_luminance"], md["min_luminance"]]
                    flat += [v for k in ("red", "green", "blue", "white_point") for v in md[k]]
                    if any(v is None for v in flat):
                        del meta["mastering_display"]
                except (KeyError, TypeError):
                    meta.pop("mastering_display", None)
            elif kind == "Content light level metadata":
                if sd.get("max_content") is not None and sd.get("max_average") is not None:
                    meta["content_light"] = {
                        "max_content": sd["max_content"],
                        "max_average": sd["max_average"],
                    }
    return meta or None


_IDET_RE = re.compile(
    rb"Multi frame detection:\s*TFF:\s*(\d+)\s*BFF:\s*(\d+)\s*Progressive:\s*(\d+)"
    rb"\s*Undetermined:\s*(\d+)"
)
# The telecine signal (see telecine_verdict): fields idet found duplicated
# from the previous frame.
_REPEAT_RE = re.compile(rb"Repeated Fields:\s*Neither:\s*(\d+)\s*Top:\s*(\d+)\s*Bottom:\s*(\d+)")
_REPEAT_KEYS = ("repeated_neither", "repeated_top", "repeated_bottom")


def _parse_idet(stderr: bytes) -> tuple[dict | None, dict | None]:
    """(detection counts, repeated-field counts) from ONE idet run's stderr.

    ffmpeg 7.x prints the idet summary twice; the first is an empty throw-away
    filter instance (all zeros), so the real one is the LAST of each line.
    """
    found = _IDET_RE.findall(stderr or b"")
    det = (
        dict(
            zip(
                ("tff", "bff", "progressive", "undetermined"),
                (int(v) for v in found[-1]),
                strict=True,
            )
        )
        if found
        else None
    )
    reps = _REPEAT_RE.findall(stderr or b"")
    rep = dict(zip(_REPEAT_KEYS, (int(v) for v in reps[-1]), strict=True)) if reps else None
    return det, rep


def _probe_idet(path: str, times: list[float]) -> dict | None:
    """idet over each sample, summed: TFF/BFF/progressive/undetermined plus the
    repeated-field counters (``repeated_neither/top/bottom``)."""
    total = {
        "tff": 0,
        "bff": 0,
        "progressive": 0,
        "undetermined": 0,
        **dict.fromkeys(_REPEAT_KEYS, 0),
    }
    seen = False
    failed = 0
    for t in times:
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-nostats",
            "-hide_banner",
            "-v",
            "info",
            "-ss",
            f"{t:.2f}",
            "-i",
            path,
            "-an",
            "-sn",
            "-dn",
            "-t",
            str(IDET_SECONDS),
            "-vf",
            "idet",
            "-f",
            "null",
            "-",
        ]
        # A killed idet still prints a plausible summary (of the truncated span),
        # so an aborted sample raises and a failed one is skipped, never parsed.
        run = _checked(_run(cmd))
        if not run.ok:
            failed += 1
            logger.warning(
                "codec_classifier: idet sample at %.2fs %s for %s; sample ignored",
                t,
                run.detail,
                path,
            )
            continue
        det, rep = _parse_idet(run.stderr)
        if det:
            seen = True
            for key, val in det.items():
                total[key] += val
        # Only counted alongside a detection row, so both cover the same samples.
        if det and rep:
            for key, val in rep.items():
                total[key] += val
    if failed and not seen:
        logger.warning(
            "codec_classifier: idet failed on every sample of %s; interlacing is decided "
            "by field_order, or deinterlaced as undecided",
            path,
        )
    return total if seen else None


def _frames(raw: bytes, size: int) -> list[bytes]:
    return [raw[i : i + size] for i in range(0, len(raw) - size + 1, size)]


def grain_of_gray(frame: bytes, w: int, h: int) -> float | None:
    """Median over 8x8 blocks of the mean |pixel - mean(4 neighbours)|, in 8-bit levels.

    White noise of sigma s gives ~0.9*s; a clean gradient or flat field ~0; edges
    inflate a few blocks but not the median.  Returns None for a (near-)black frame.
    """
    if len(frame) < w * h or w < 24 or h < 24:
        return None
    if sum(frame[::17]) / max(1, len(frame[::17])) < 12:
        return None  # black / fade: says nothing about grain
    rows = [frame[y * w : (y + 1) * w] for y in range(h)]
    block_sums: dict[tuple[int, int], int] = {}
    block_n: dict[tuple[int, int], int] = {}
    for y in range(1, h - 1):
        up, cur, dn = rows[y - 1], rows[y], rows[y + 1]
        by = y >> 3
        for x in range(1, w - 1):
            r = cur[x] - ((up[x] + dn[x] + cur[x - 1] + cur[x + 1]) >> 2)
            key = (by, x >> 3)
            block_sums[key] = block_sums.get(key, 0) + (r if r >= 0 else -r)
            block_n[key] = block_n.get(key, 0) + 1
    means = [block_sums[k] / block_n[k] for k in block_sums if block_n[k] >= 32]
    return statistics.median(means) if means else None


def normalise_grain(median_abs_residual: float) -> float:
    return max(0.0, min(1.0, median_abs_residual / 3.0))


def animation_of_rgb(frame: bytes, w: int, h: int) -> float | None:
    """Animation score 0..1 from flat-block ratio, palette concentration, hard edges.

    Near-black pixels/blocks (letterbox bars, fades) are excluded from every ratio.
    """
    if len(frame) < w * h * 3:
        return None
    gray = bytearray(w * h)
    hist: dict[int, int] = {}
    lit = 0
    for i in range(w * h):
        r, g, b = frame[3 * i], frame[3 * i + 1], frame[3 * i + 2]
        lum = (r + 2 * g + b) >> 2
        gray[i] = lum
        if lum >= 20:
            lit += 1
            q = ((r >> 4) << 8) | ((g >> 4) << 4) | (b >> 4)
            hist[q] = hist.get(q, 0) + 1
    if lit < w * h * 0.15:
        return None
    palette = sum(sorted(hist.values(), reverse=True)[:32]) / lit
    flat = nonflat = hard = 0
    for by in range(0, h - 3, 4):
        for bx in range(0, w - 3, 4):
            vals = [gray[(by + dy) * w + bx + dx] for dy in range(4) for dx in range(4)]
            if sum(vals) / 16 < 20:
                continue
            rng = max(vals) - min(vals)
            if rng <= 3:
                flat += 1
            else:
                nonflat += 1
                if rng >= 60:
                    hard += 1
    blocks = flat + nonflat
    if blocks == 0:
        return None
    flat_ratio = flat / blocks
    hard_ratio = hard / nonflat if nonflat else 0.0
    return 0.45 * flat_ratio + 0.35 * palette + 0.20 * hard_ratio


def _probe_pixels(path: str, width: int, height: int, times: list[float]) -> tuple[list, list]:
    grains: list[float] = []
    anims: list[float] = []
    cw, ch = min(width, CROP), min(height, CROP)
    for t in times:
        with tempfile.TemporaryDirectory(prefix="codecprobe-") as tmp:
            gpath, spath = os.path.join(tmp, "g.raw"), os.path.join(tmp, "s.raw")
            fc = (
                f"[0:v:0]split=2[a][b];[a]crop={cw}:{ch},format=gray[g];"
                f"[b]scale={SMALL_W}:{SMALL_H}:flags=area,format=rgb24[s]"
            )
            cmd = [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-v",
                "error",
                "-ss",
                f"{t:.2f}",
                "-i",
                path,
                "-an",
                "-sn",
                "-dn",
                "-filter_complex",
                fc,
                "-map",
                "[g]",
                "-frames:v",
                str(FRAMES_PER_SAMPLE),
                "-f",
                "rawvideo",
                gpath,
                "-map",
                "[s]",
                "-frames:v",
                str(FRAMES_PER_SAMPLE),
                "-f",
                "rawvideo",
                spath,
            ]
            run = _checked(_run(cmd))
            if not run.ok:
                continue
            try:
                with open(gpath, "rb") as fh:
                    graw = fh.read()
                with open(spath, "rb") as fh:
                    sraw = fh.read()
            except OSError:
                continue
        for fr in _frames(graw, cw * ch):
            g = grain_of_gray(fr, cw, ch)
            if g is not None:
                grains.append(g)
        for fr in _frames(sraw, SMALL_W * SMALL_H * 3):
            a = animation_of_rgb(fr, SMALL_W, SMALL_H)
            if a is not None:
                anims.append(a)
    return grains, anims


def sample_times(duration: float | None) -> list[float]:
    """Sample points spread across the title (not the first seconds: logos, black)."""
    if not duration or duration < 30:
        return [0.0]
    return [duration * f for f in SAMPLE_FRACTIONS]


def probe_media(path: str) -> dict:
    """Run every probe on ``path`` and return the probe dict.

    A probe that fails on the file leaves its key missing.  A probe that could
    not run, timed out or was killed raises :class:`ProbeAborted`: that is the
    only exception that leaves this function.
    """
    probe: dict = {}
    try:
        probe = _probe_stream(path)
        times = sample_times(probe.get("duration"))
        transfer = str(probe.get("color_transfer") or "").lower()
        if transfer in _HDR_TRANSFERS:
            meta = _probe_hdr_meta(path)
            if meta:
                probe["hdr_meta"] = meta
        idet = _probe_idet(path, times)
        if idet:
            probe["idet"] = idet
        w, h = probe.get("width"), probe.get("height")
        if w and h and w >= 24 and h >= 24:
            grains, anims = _probe_pixels(path, int(w), int(h), times)
            if grains:
                probe["grain"] = normalise_grain(statistics.median(grains))
            if anims:
                probe["animation"] = statistics.median(anims)
            probe["pixel_frames"] = len(grains)
    except ProbeAborted:
        raise
    except Exception:  # noqa: BLE001 - probes are best-effort
        logger.warning("codec_classifier: probing failed for %s", path, exc_info=True)
    return probe


def probe_interlace(path: str) -> dict:
    """The cheap probes a forced class needs: stream info plus idet.

    Raises only :class:`ProbeAborted` (see :func:`probe_media`).

    One ffprobe (dimensions, ``pix_fmt``, ``field_order``, duration) and the
    same idet sampling :func:`probe_media` runs (``IDET_SECONDS`` at each of
    :data:`SAMPLE_FRACTIONS`), but none of the pixel probes, which are the
    expensive part and only matter to *choosing* a class.
    """
    probe: dict = {}
    try:
        probe = _probe_stream(path)
        idet = _probe_idet(path, sample_times(probe.get("duration")))
        if idet:
            probe["idet"] = idet
    except ProbeAborted:
        raise
    except Exception:  # noqa: BLE001 - probes are best-effort
        logger.warning("codec_classifier: interlace probe failed for %s", path, exc_info=True)
    return probe


def plan_for_file(path: str, context: dict | None = None) -> CodecPlan:
    """Probe ``path`` and classify it.

    Raises :class:`ProbeAborted` when a probe could not run or was killed, so
    the caller fails visibly instead of encoding with a class nobody measured.
    Any other error yields the undecided-source plan.
    """
    try:
        return classify(probe_media(str(path)), context)
    except ProbeAborted:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("codec_classifier: plan_for_file failed (%s)", exc)
        return _undecided_plan(str(exc), "probe error")
