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

Configuration (environment):
    ENGRAM_AV1_HOOK      "1" to enable; anything else disables.
    ENGRAM_AV1_STAGING   destination dir (default /staging/av1-out)
    ENGRAM_AV1_CRF       SVT-AV1 CRF (default 22)
    ENGRAM_AV1_PRESET    SVT-AV1 preset (default 6)
    ENGRAM_AV1_THREADS   SVT-AV1 lp value (default 4)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

MARKER_SUFFIX = ".engram-av1.json"
ENCODE_TIMEOUT_SECONDS = 6 * 60 * 60  # a 45-min BD episode is well inside this


def _cfg(name: str, default: str) -> str:
    return os.environ.get(name, default)


def is_enabled() -> bool:
    return _cfg("ENGRAM_AV1_HOOK", "0") == "1"


def _probe(path: Path) -> dict:
    """Return codec/dimension/track counts for a media file, or {} on failure."""
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_entries",
        "stream=index,codec_type,codec_name,width,height:stream_disposition=default",
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


def _video_codec(probe: dict) -> str | None:
    for s in probe.get("streams", []):
        if s.get("codec_type") == "video":
            return s.get("codec_name")
    return None


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


def _write_marker(src: Path, dest: Path) -> None:
    try:
        st = src.stat()
        _marker_path(src).write_text(json.dumps({
            "size": st.st_size,
            "mtime": int(st.st_mtime),
            "av1": str(dest),
        }))
    except OSError:
        logger.warning("AV1 hook: could not write marker for %s", src)


def transcode_to_av1(src: Path, staging_dir: Path) -> Path | None:
    """Encode ``src`` to AV1 in ``staging_dir``. Returns the output path or None."""
    crf = _cfg("ENGRAM_AV1_CRF", "22")
    preset = _cfg("ENGRAM_AV1_PRESET", "6")
    threads = _cfg("ENGRAM_AV1_THREADS", "4")

    staging_dir.mkdir(parents=True, exist_ok=True)
    dest = staging_dir / src.name

    # Encode to a temp name, then rename, so a partial file is never mistaken
    # for a finished one by the curator's settle check.
    tmp = staging_dir / f".{src.stem}.partial.mkv"

    cmd = [
        "ffmpeg", "-v", "error", "-y",
        "-i", str(src),
        "-map", "0:v:0",
        "-map", "0:a?",
        "-map", "0:s?",
        "-c:v", "libsvtav1",
        "-crf", crf,
        "-preset", preset,
        "-svtav1-params", f"tune=0:lp={threads}",
        "-c:a", "copy",
        "-c:s", "copy",
        str(tmp),
    ]
    logger.info("AV1 hook: encoding %s -> %s (crf=%s preset=%s)", src.name, dest.name, crf, preset)
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

    # Verify before publishing: codec must be AV1 and track counts must match.
    src_probe = _probe(src)
    out_probe = _probe(tmp)
    if _video_codec(out_probe) != "av1":
        logger.error("AV1 hook: output is not AV1 for %s", src.name)
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


def after_organize(final_path: str | None) -> None:
    """Entry point called once a title has been filed.

    Never raises: a hook failure must not fail the job.
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

        staging = Path(_cfg("ENGRAM_AV1_STAGING", "/staging/av1-out"))
        dest = transcode_to_av1(src, staging)
        if dest is not None:
            _write_marker(src, dest)
    except Exception:  # noqa: BLE001 - fail-open by design
        logger.exception("AV1 hook: unexpected error for %s", final_path)
