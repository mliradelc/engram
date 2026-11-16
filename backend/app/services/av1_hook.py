"""Post-organize AV1 transcode hook.

Engram files rips as untouched MakeMKV output (MPEG-2 for DVD, MPEG-2/VC-1 for
BD). This module re-encodes a just-organized file to AV1 and moves the result to
the curator's staging directory, so the existing CT104 curator imports it and
remains the single filing authority.

Design constraints, each deliberate:

* **Never in-place.** The source file is left untouched until the encode is
  verified. A failed encode must not cost the rip.
* **Track-preserving.** Every audio and subtitle stream is copied through
  (``-map 0:a? -map 0:s? -c:a copy -c:s copy``), matching ARM's ``--all-audio``
  / ``--all-subtitles`` contract. A track-count mismatch aborts the swap.
* **Idempotent.** A sidecar marker records the source's size+mtime, so a retried
  job does not re-encode an already-converted file.
* **Fail-open.** Any error logs and returns; the rip stays in the library as
  Engram filed it. Losing a transcode is recoverable, losing a rip is not.
* **Off by default.** Enabled only when ``ENGRAM_AV1_HOOK=1`` in the container
  environment, so a rebuild without the setting behaves exactly as upstream.

Codec choice is made per file by ``codec_classifier`` (see CODEC-CLASSIFIER.md):
the media is probed and classified, and the Engram identification context is a
hint only.  ``ENGRAM_CODEC_CLASSIFIER=0`` restores the original fixed CRF/preset
command byte-for-byte.  The same fixed command is the fallback whenever the
classifier or its encoder pre-check fails, so a classifier fault never costs the
transcode, let alone the rip.

Configuration (environment):
    ENGRAM_AV1_HOOK          "1" to enable; anything else disables.
    ENGRAM_AV1_STAGING       destination dir (default /staging/av1-out)
    ENGRAM_AV1_THREADS       encoder thread count, SVT ``lp`` / x265 ``pools``
                             (default 4; applies on every path)
    ENGRAM_CODEC_CLASSIFIER  "0" disables the classifier (fixed CRF/preset).
    ENGRAM_CODEC_INTERLACED  "deinterlace" to encode interlaced sources through
                             bwdif; anything else leaves them untouched.
    ENGRAM_AV1_CRF           fixed-path SVT-AV1 CRF (default 22)      } fallback
    ENGRAM_AV1_PRESET        fixed-path SVT-AV1 preset (default 6)    } path only
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

MARKER_SUFFIX = ".engram-av1.json"
ENCODE_TIMEOUT_SECONDS = 6 * 60 * 60  # a 45-min BD episode is well inside this


def _cfg(name: str, default: str) -> str:
    return os.environ.get(name, default)


def classifier_enabled() -> bool:
    """Kill switch: ``ENGRAM_CODEC_CLASSIFIER=0`` restores the fixed CRF/preset path."""
    return _cfg("ENGRAM_CODEC_CLASSIFIER", "1").strip() != "0"


def _load_classifier():
    """Import ``codec_classifier`` without depending on ``app.services`` importing.

    ``app.services.__init__`` pulls in the ASR stack; a codec decision must not
    depend on that being importable, and this module is also imported directly.
    Returns the module, or None (-> fixed path).
    """
    mod = sys.modules.get("codec_classifier") or sys.modules.get("app.services.codec_classifier")
    if mod is not None:
        return mod
    try:
        path = Path(__file__).with_name("codec_classifier.py")
        spec = importlib.util.spec_from_file_location("codec_classifier", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["codec_classifier"] = mod  # dataclasses needs it registered
        spec.loader.exec_module(mod)
        return mod
    except Exception:  # noqa: BLE001 - fail-open: fall back to the fixed path
        sys.modules.pop("codec_classifier", None)
        logger.exception("AV1 hook: could not load codec_classifier; using fixed settings")
        return None


def is_enabled() -> bool:
    return _cfg("ENGRAM_AV1_HOOK", "0") == "1"


def _probe(path: Path) -> dict:
    """Return codec/dimension/track counts for a media file, or {} on failure."""
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_entries",
        "stream=index,codec_type,codec_name,width,height,pix_fmt,color_transfer"
        ":stream_disposition=default",
        str(path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if out.returncode != 0:
            return {}
        return json.loads(out.stdout or "{}")
    except (subprocess.SubprocessError, ValueError):
        return {}


def _track_counts(probe: dict) -> tuple[int, int]:
    streams = probe.get("streams", [])
    audio = sum(1 for s in streams if s.get("codec_type") == "audio")
    subs = sum(1 for s in streams if s.get("codec_type") == "subtitle")
    return audio, subs


def _video_stream(probe: dict) -> dict:
    for s in probe.get("streams", []):
        if s.get("codec_type") == "video":
            return s
    return {}


def _video_codec(probe: dict) -> str | None:
    return _video_stream(probe).get("codec_name")


def _marker_path(src: Path) -> Path:
    return src.with_name(src.name + MARKER_SUFFIX)


def _already_done(src: Path) -> bool:
    """True when a marker matches this exact source revision."""
    marker = _marker_path(src)
    if not marker.exists():
        return False
    try:
        data = json.loads(marker.read_text())
        st = src.stat()
        return data.get("size") == st.st_size and data.get("mtime") == int(st.st_mtime)
    except (OSError, ValueError):
        return False


def _write_marker(src: Path, dest: Path | None, codec_class: str | None = None) -> None:
    """Record that ``src`` has been dealt with.  ``dest`` is None for a passthrough."""
    try:
        st = src.stat()
        data = {
            "size": st.st_size,
            "mtime": int(st.st_mtime),
            "av1": str(dest) if dest is not None else None,
        }
        if codec_class:
            data["codec_class"] = codec_class
        _marker_path(src).write_text(json.dumps(data))
    except OSError:
        logger.warning("AV1 hook: could not write marker for %s", src)


def _legacy_video_args(threads: str) -> list[str]:
    """The original fixed-settings video arguments.  Kill-switch / fallback path.

    Deliberately unchanged from the pre-classifier hook (including the absence of
    ``-pix_fmt`` and ``film-grain-denoise``): ``ENGRAM_CODEC_CLASSIFIER=0`` must
    reproduce the old command exactly.
    """
    crf = _cfg("ENGRAM_AV1_CRF", "22")
    preset = _cfg("ENGRAM_AV1_PRESET", "6")
    return [
        "-c:v", "libsvtav1",
        "-crf", crf,
        "-preset", preset,
        "-svtav1-params", f"tune=0:lp={threads}",
    ]


@lru_cache(maxsize=32)
def _encoder_accepts(video_args: tuple[str, ...]) -> bool:
    """True if this ffmpeg/encoder build accepts ``video_args`` on a tiny synthetic clip.

    Encoders differ by build (an older SVT-AV1 may reject newer parameters), and
    a rejected parameter would otherwise surface only after minutes of encoding.
    Cached per process, so it costs well under a second once per distinct command.
    """
    cmd = [
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=128x128:rate=24:duration=0.5",
        *video_args, "-frames:v", "6", "-f", "null", "-",
    ]
    try:
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (subprocess.SubprocessError, OSError):
        return False
    return run.returncode == 0


def _select_video_args(plan, threads: str) -> tuple[list[str], str, object | None]:
    """Choose the video arguments, degrading gracefully.

    Order: the plan's full parameter set, then only its essential parameters
    (drops tuning extras an older encoder may not know), then the fixed legacy
    command.  Returns (args, expected output codec_name, plan-or-None).
    """
    if plan is not None:
        for optional in (True, False):
            args = plan.video_args(threads, optional=optional)
            if _encoder_accepts(tuple(args)):
                if not optional:
                    logger.warning(
                        "AV1 hook: encoder rejected tuning extras for class=%s; "
                        "using essential parameters only", plan.class_name)
                return args, plan.output_codec_name or "av1", plan
        logger.warning("AV1 hook: encoder rejected class=%s settings; using fixed settings",
                       plan.class_name)
    return _legacy_video_args(threads), "av1", None


def transcode_to_av1(src: Path, staging_dir: Path, plan=None) -> Path | None:
    """Encode ``src`` in ``staging_dir``.  Returns the output path or None.

    With ``plan`` (a ``codec_classifier.CodecPlan``) the codec and settings come
    from the classifier; with ``plan=None`` the fixed legacy settings are used.
    Despite the name, a plan may select HEVC (grainy live action); the output is
    verified against whatever codec was actually requested.
    """
    threads = _cfg("ENGRAM_AV1_THREADS", "4")

    staging_dir.mkdir(parents=True, exist_ok=True)
    dest = staging_dir / src.name

    # Encode to a temp name, then rename, so a partial file is never mistaken
    # for a finished one by the curator's settle check.
    tmp = staging_dir / f".{src.stem}.partial.mkv"

    if plan is None:
        video_args, expect_codec, active = _legacy_video_args(threads), "av1", None
    else:
        video_args, expect_codec, active = _select_video_args(plan, threads)

    cmd = [
        "ffmpeg", "-v", "error", "-y",
        "-i", str(src),
        "-map", "0:v:0",
        "-map", "0:a?",
        "-map", "0:s?",
        *video_args,
        "-c:a", "copy",
        "-c:s", "copy",
        str(tmp),
    ]
    if active is not None:
        logger.info("AV1 hook: encoding %s -> %s (class=%s)", src.name, dest.name,
                    active.class_name)
    else:
        logger.info("AV1 hook: encoding %s -> %s (crf=%s preset=%s)", src.name, dest.name,
                    _cfg("ENGRAM_AV1_CRF", "22"), _cfg("ENGRAM_AV1_PRESET", "6"))
    try:
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=ENCODE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        logger.error("AV1 hook: encode timed out for %s", src.name)
        tmp.unlink(missing_ok=True)
        return None

    if run.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        logger.error("AV1 hook: encode failed for %s: %s", src.name, (run.stderr or "")[-500:])
        tmp.unlink(missing_ok=True)
        return None

    # Verify before publishing: codec must be what was asked for and track counts must match.
    src_probe = _probe(src)
    out_probe = _probe(tmp)
    if _video_codec(out_probe) != expect_codec:
        logger.error("AV1 hook: output is not %s for %s", expect_codec, src.name)
        tmp.unlink(missing_ok=True)
        return None
    if active is not None:
        out_v, src_v = _video_stream(out_probe), _video_stream(src_probe)
        if out_v.get("pix_fmt") != active.pix_fmt:
            logger.error("AV1 hook: output pix_fmt %s is not %s for %s",
                         out_v.get("pix_fmt"), active.pix_fmt, src.name)
            tmp.unlink(missing_ok=True)
            return None
        # An HDR title that loses its transfer tag plays back washed out: refuse the swap.
        if active.hdr and src_v.get("color_transfer") != out_v.get("color_transfer"):
            logger.error("AV1 hook: HDR transfer not preserved for %s (src=%s out=%s)",
                         src.name, src_v.get("color_transfer"), out_v.get("color_transfer"))
            tmp.unlink(missing_ok=True)
            return None
    if _track_counts(out_probe) != _track_counts(src_probe):
        logger.error(
            "AV1 hook: track mismatch for %s (src=%s out=%s) — keeping original",
            src.name, _track_counts(src_probe), _track_counts(out_probe),
        )
        tmp.unlink(missing_ok=True)
        return None

    tmp.replace(dest)
    logger.info(
        "AV1 hook: %s -> %s (%.1f MB -> %.1f MB)",
        src.name, dest.name, src.stat().st_size / 1e6, dest.stat().st_size / 1e6,
    )
    return dest


def _classify(src: Path, context: dict | None):
    """Return a CodecPlan, or None to use the fixed settings.  Never raises."""
    if not classifier_enabled():
        return None
    mod = _load_classifier()
    if mod is None:
        return None
    try:
        deinterlace = _cfg("ENGRAM_CODEC_INTERLACED", "").strip().lower() == "deinterlace"
        plan = mod.plan_for_file(str(src), context, deinterlace=deinterlace)
        logger.info("AV1 hook: codec_classifier: %s (%s)", plan.describe(), src.name)
        return plan
    except Exception:  # noqa: BLE001 - fail-open: fixed settings
        logger.exception("AV1 hook: classifier failed for %s; using fixed settings", src.name)
        return None


def after_organize(final_path: str | None, *, context: dict | None = None) -> None:
    """Entry point called once a title has been filed.

    ``context`` is the optional Engram identification context (content_type,
    is_extra, duration_seconds, video_resolution, ...).  The classifier treats it
    as a hint only; media probes win.  Never raises: a hook failure must not fail
    the job.
    """
    if not is_enabled():
        return
    if not final_path:
        return

    try:
        src = Path(final_path)
        if not src.is_file():
            logger.warning("AV1 hook: filed path is not a file: %s", final_path)
            return
        if src.suffix.lower() != ".mkv":
            return
        if _already_done(src):
            logger.info("AV1 hook: already converted, skipping %s", src.name)
            return

        plan = _classify(src, context)
        if plan is not None and plan.is_passthrough:
            logger.info("AV1 hook: leaving %s untouched (%s)", src.name, plan.reason)
            _write_marker(src, None, plan.class_name)
            return

        staging = Path(_cfg("ENGRAM_AV1_STAGING", "/staging/av1-out"))
        dest = transcode_to_av1(src, staging, plan)
        if dest is not None:
            _write_marker(src, dest, plan.class_name if plan is not None else None)
    except Exception:  # noqa: BLE001 - fail-open by design
        logger.exception("AV1 hook: unexpected error for %s", final_path)
