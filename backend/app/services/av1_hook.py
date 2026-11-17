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
encoding arguments byte-for-byte.  The same fixed command is the fallback whenever the
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
    ENGRAM_AV1_MAX_CONCURRENT
                             max simultaneous ffmpeg encodes across the whole
                             process, any codec (default 1 = strictly serial;
                             0, negative or non-integer = unlimited).

Encode concurrency: every job's organize sweep is its own asyncio task, so
several sweeps can reach ``transcode_to_av1`` at once.  On a small host that
starved the CPU and memory (four parallel encodes took the API down), so the
ffmpeg encode (plus verification probe of its output) is gated by a
process-wide ``threading.Semaphore``.  ``after_organize`` already runs in a
worker thread, so blocking is correct.  The gate sits above the codec choice
and is therefore codec-agnostic; probing/classification never holds a permit.

Encode progress: ffmpeg runs with ``-progress pipe:1 -nostats``; a reader thread
parses the key=value stream into percent/ETA, keeps it in an in-memory registry
(``encoding_snapshot()``, served by ``GET /api/encoding``) and, when the caller
passed its event loop, publishes throttled ``encode_progress`` WebSocket
messages through a publisher the coordinator registers with
``set_progress_publisher``.  The registry is deliberately process-local: a
restart kills the encode, so a persisted row would only ever be a lie.

Failures and retry: an encode that fails (``transcode_to_av1`` returns None)
is recorded in a bounded in-memory ring buffer (``FAILURE_BUFFER_SIZE``,
``failures_snapshot()``), served next to the active rows by
``encoding_status()``.  ``retry_encode`` re-runs ``after_organize`` for such a
source after deleting its marker; it goes through the same ``_encode_slot``
permit and ``_ENCODING`` registry as any other encode, so the concurrency cap
holds and the panel shows the retried file.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MARKER_SUFFIX = ".engram-av1.json"
ENCODE_TIMEOUT_SECONDS = 6 * 60 * 60  # a 45-min BD episode is well inside this
PROGRESS_PUBLISH_INTERVAL = 2.0  # seconds between encode_progress messages per file
FAILURE_BUFFER_SIZE = 20  # recently-failed encodes kept for the panel's Retry rows


def _cfg(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _max_concurrent() -> int:
    """Parse ``ENGRAM_AV1_MAX_CONCURRENT``.  0 means unlimited; never raises.

    Invalid or negative values mean unlimited (the pre-gate behaviour) rather than
    an error: a typo in an env var must not stop transcoding or the job.
    """
    raw = _cfg("ENGRAM_AV1_MAX_CONCURRENT", "1").strip()
    try:
        value = int(raw)
    except ValueError:
        return 0
    return max(value, 0)


_gate_lock = threading.Lock()
_gate_limit: int | None = None
_gate_sem: threading.Semaphore | None = None
_gate_busy = 0  # encodes currently holding a permit (for the queued log line)


def _get_gate(limit: int) -> threading.Semaphore:
    """Return the process-wide semaphore for ``limit``, rebuilding if the limit changed."""
    global _gate_limit, _gate_sem
    with _gate_lock:
        if _gate_sem is None or _gate_limit != limit:
            _gate_sem = threading.Semaphore(limit)
            _gate_limit = limit
        return _gate_sem


@contextmanager
def _encode_slot(name: str) -> Iterator[None]:
    """Hold an encode permit for the duration of the block, blocking if none is free.

    Acquired BEFORE the encode timeout starts, so queue wait never consumes the
    encode budget.  Always released, including on exceptions.  No-op when unlimited.
    """
    global _gate_busy
    limit = _max_concurrent()
    if limit == 0:
        yield
        return
    sem = _get_gate(limit)
    if not sem.acquire(blocking=False):
        with _gate_lock:
            busy = _gate_busy
        logger.info("AV1 hook: waiting for encode slot (%d/%d busy) for %s", busy, limit, name)
        sem.acquire()
    with _gate_lock:
        _gate_busy += 1
    try:
        yield
    finally:
        with _gate_lock:
            _gate_busy -= 1
        sem.release()


# ---------------------------------------------------------------------------
# Encode progress: parser, registry, publishing
# ---------------------------------------------------------------------------

_ENCODING: dict[str, dict[str, Any]] = {}
# key: destination path (str) - unique per file, survives queueing
# value: {file, destination_folder, percent, eta, speed, codec, started_at}
_encoding_lock = threading.Lock()

# Coroutine function ``(**fields) -> Awaitable`` broadcasting one progress sample.
# Registered by the finalization coordinator so this module never imports the API layer.
_progress_publisher: Callable[..., Any] | None = None


def set_progress_publisher(publisher: Callable[..., Any] | None) -> None:
    """Register the coroutine function used to broadcast ``encode_progress``."""
    global _progress_publisher
    _progress_publisher = publisher


def encoding_snapshot() -> list[dict[str, Any]]:
    """Copy of every encode currently holding a permit, oldest first."""
    with _encoding_lock:
        rows = [dict(v) for v in _ENCODING.values()]
    rows.sort(key=lambda r: (r.get("started_at") or 0.0, r.get("file") or ""))
    return rows


# ---------------------------------------------------------------------------
# Failure ring buffer
# ---------------------------------------------------------------------------

# Newest last.  Entries: {file, source, destination_folder, reason, failed_at}.
# ``source`` is the absolute source path a Retry posts back.  Guarded by
# ``_encoding_lock`` like the active registry.
_FAILED: deque[dict[str, Any]] = deque(maxlen=FAILURE_BUFFER_SIZE)


def _forget_failure_locked(source: str) -> None:
    """Drop every buffered failure for ``source``.  Caller holds ``_encoding_lock``."""
    for stale in [e for e in _FAILED if e.get("source") == source]:
        _FAILED.remove(stale)


def _record_failure(src: Path, dest: Path, reason: str) -> None:
    """Remember a failed encode for the panel, one row per source.  Never raises."""
    try:
        entry = {
            "file": src.name,
            "source": str(src),
            "destination_folder": str(dest.parent),
            "reason": reason,
            "failed_at": time.time(),
        }
        with _encoding_lock:
            _forget_failure_locked(str(src))
            _FAILED.append(entry)
    except Exception:  # noqa: BLE001 - fail-open: the buffer is cosmetic
        logger.debug("AV1 hook: could not record failure for %s", src, exc_info=True)


def failures_snapshot() -> list[dict[str, Any]]:
    """Copy of the recently-failed encodes, newest first."""
    with _encoding_lock:
        rows = [dict(e) for e in _FAILED]
    rows.reverse()
    return rows


def encoding_status() -> dict[str, list[dict[str, Any]]]:
    """``GET /api/encoding`` payload: in-flight encodes and recent failures."""
    return {"active": encoding_snapshot(), "failed": failures_snapshot()}


def dismiss_failure(source: str | None = None) -> int:
    """Drop buffered failure rows for ``source`` (all of them when None).

    Returns the number of rows removed.  The buffer is process-local and purely
    cosmetic, so a stale row — one whose source is no longer in Engram's domain,
    or a failure the operator has decided not to retry — can simply be cleared
    instead of waiting for the ring buffer to age out.
    """
    with _encoding_lock:
        if source is None:
            removed = len(_FAILED)
            _FAILED.clear()
            return removed
        stale = [e for e in _FAILED if e.get("source") == source]
        for entry in stale:
            _FAILED.remove(entry)
        return len(stale)


def _parse_speed(raw: str | None) -> float | None:
    """``"0.216x"`` -> 0.216; absent, ``N/A``, zero or garbage -> None (unknown)."""
    if not raw:
        return None
    try:
        value = float(raw.strip().rstrip("x"))
    except ValueError:
        return None
    return value if value > 0 else None


def _parse_out_time_seconds(block: dict[str, str]) -> float | None:
    # out_time_ms is microseconds too (a long-standing ffmpeg quirk): fallback only.
    for key in ("out_time_us", "out_time_ms"):
        raw = block.get(key)
        if raw is None:
            continue
        try:
            return max(int(raw), 0) / 1e6
        except ValueError:
            continue
    return None


def compute_progress(
    out_time_seconds: float | None, speed: float | None, duration_seconds: float | None
) -> tuple[float | None, int | None]:
    """Return ``(percent, eta_seconds)``; either is None when it cannot be known.

    percent is clamped to 0..100 and eta to >= 0.  ETA needs a known speed: a
    missing or ``N/A`` speed omits it rather than showing a wrong one.
    """
    if out_time_seconds is None or not duration_seconds or duration_seconds <= 0:
        return None, None
    percent = min(max(out_time_seconds / duration_seconds * 100.0, 0.0), 100.0)
    eta = None
    if speed is not None and speed > 0:
        eta = int(round(max(duration_seconds - out_time_seconds, 0.0) / speed))
    return round(percent, 1), eta


def iter_progress(
    lines: Iterable[str], duration_seconds: float | None
) -> Iterator[dict[str, Any]]:
    """Parse an ffmpeg ``-progress`` stream, yielding one sample per ``progress=`` line.

    Each sample is ``{percent, eta, speed, done}``.  ``speed`` is ffmpeg's raw
    string (e.g. ``"0.216x"``) or None when unknown.  ``done`` marks
    ``progress=end``, where percent is forced to 100 and eta to 0.
    """
    block: dict[str, str] = {}
    for line in lines:
        key, sep, value = line.strip().partition("=")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if key != "progress":
            block[key] = value
            continue
        speed = _parse_speed(block.get("speed"))
        percent, eta = compute_progress(_parse_out_time_seconds(block), speed, duration_seconds)
        done = value == "end"
        if done:
            percent, eta = 100.0, 0
        yield {
            "percent": percent,
            "eta": eta,
            "speed": block.get("speed") if speed is not None else None,
            "done": done,
        }
        block = {}


def _publish(loop, entry: dict[str, Any], *, done: bool = False) -> None:
    """Fire-and-forget one ``encode_progress`` message.  Never raises.

    ``done`` marks the file's last message (encode finished, failed or timed
    out); it carries percent 100 so a client drops the row.
    """
    publisher = _progress_publisher
    if loop is None or publisher is None:
        return
    try:
        if loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(
            publisher(
                file=entry["file"],
                destination_folder=entry["destination_folder"],
                percent=100.0 if done else entry["percent"],
                eta=0 if done else entry["eta"],
                speed=entry["speed"],
                codec=entry["codec"],
                done=done,
            ),
            loop,
        )
    except Exception:  # noqa: BLE001 - fail-open: progress is cosmetic
        logger.debug("AV1 hook: could not publish encode progress", exc_info=True)


def _consume_progress(fd: int, key: str, duration: float | None, loop) -> None:
    """Reader thread: drain ffmpeg's progress pipe into the registry.  Never raises.

    Reads to EOF even after an internal error, so ffmpeg can never block on a
    full stdout pipe.  Publishes at most once per ``PROGRESS_PUBLISH_INTERVAL``.
    """
    last_publish: float | None = None
    try:
        with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as stream:
            try:
                for sample in iter_progress(stream, duration):
                    with _encoding_lock:
                        entry = _ENCODING.get(key)
                        if entry is None:
                            continue
                        if sample["percent"] is not None:
                            entry["percent"] = sample["percent"]
                        entry["eta"] = sample["eta"]
                        entry["speed"] = sample["speed"]
                        snapshot = dict(entry)
                    now = time.monotonic()
                    if last_publish is None or now - last_publish >= PROGRESS_PUBLISH_INTERVAL:
                        last_publish = now
                        _publish(loop, snapshot)
            except Exception:  # noqa: BLE001 - fail-open: keep draining the pipe
                logger.debug("AV1 hook: progress parse failed for %s", key, exc_info=True)
                for _ in stream:
                    pass
    except Exception:  # noqa: BLE001 - fail-open: progress is cosmetic
        logger.debug("AV1 hook: progress reader failed for %s", key, exc_info=True)


def _run_encode(
    cmd: list[str], *, key: str, duration: float | None, loop
) -> subprocess.CompletedProcess:
    """Run ffmpeg with its ``-progress`` stream on a pipe drained by a reader thread.

    stdout goes to our own pipe (progress); stderr is captured by ``run`` and
    drained concurrently by ``communicate``, so neither stream can deadlock the
    encode and the failure log keeps its ``-v error`` text.  ``run`` keeps the
    timeout/kill semantics; ``subprocess.TimeoutExpired`` propagates as before.
    """
    read_fd, write_fd = os.pipe()
    reader = threading.Thread(
        target=_consume_progress, args=(read_fd, key, duration, loop),
        name="av1-progress", daemon=True,
    )
    try:
        reader.start()
    except Exception:  # noqa: BLE001 - no progress, but the encode still runs
        os.close(read_fd)
        logger.debug("AV1 hook: could not start progress reader", exc_info=True)
    try:
        return subprocess.run(
            cmd, stdin=subprocess.DEVNULL, stdout=write_fd, stderr=subprocess.PIPE,
            text=True, timeout=ENCODE_TIMEOUT_SECONDS,
        )
    finally:
        os.close(write_fd)  # the reader hits EOF once ffmpeg has exited as well
        if reader.is_alive():
            reader.join(timeout=5)


def _duration_seconds(probe: dict) -> float | None:
    try:
        value = float((probe.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


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
        ":stream_disposition=default:format=duration",
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


def _encode_and_verify(
    cmd: list[str], src: Path, tmp: Path, *, key: str, duration: float | None, loop,
    expect_codec: str, active,
) -> str | None:
    """Run the encode and verify ``tmp``.

    Returns None when ``tmp`` may be published, otherwise a short human-readable
    failure reason (shown on the panel's failed row).  Called with the encode
    permit held.  On any failure ``tmp`` is removed.
    """
    try:
        run = _run_encode(cmd, key=key, duration=duration, loop=loop)
    except subprocess.TimeoutExpired:
        logger.error("AV1 hook: encode timed out for %s", src.name)
        tmp.unlink(missing_ok=True)
        return "Encode timed out"

    if run.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        stderr = (run.stderr or "").strip()
        logger.error("AV1 hook: encode failed for %s: %s", src.name, stderr[-500:])
        tmp.unlink(missing_ok=True)
        last = stderr.splitlines()[-1][-200:] if stderr else ""
        return f"ffmpeg exited {run.returncode}" + (f": {last}" if last else "")

    # Verify before publishing: codec must be what was asked for and track counts must match.
    src_probe = _probe(src)
    out_probe = _probe(tmp)
    if _video_codec(out_probe) != expect_codec:
        logger.error("AV1 hook: output is not %s for %s", expect_codec, src.name)
        tmp.unlink(missing_ok=True)
        return f"Output is not {expect_codec}"
    if active is not None:
        out_v, src_v = _video_stream(out_probe), _video_stream(src_probe)
        if out_v.get("pix_fmt") != active.pix_fmt:
            logger.error("AV1 hook: output pix_fmt %s is not %s for %s",
                         out_v.get("pix_fmt"), active.pix_fmt, src.name)
            tmp.unlink(missing_ok=True)
            return f"Output pixel format is not {active.pix_fmt}"
        # An HDR title that loses its transfer tag plays back washed out: refuse the swap.
        if active.hdr and src_v.get("color_transfer") != out_v.get("color_transfer"):
            logger.error("AV1 hook: HDR transfer not preserved for %s (src=%s out=%s)",
                         src.name, src_v.get("color_transfer"), out_v.get("color_transfer"))
            tmp.unlink(missing_ok=True)
            return "HDR transfer not preserved"
    if _track_counts(out_probe) != _track_counts(src_probe):
        logger.error(
            "AV1 hook: track mismatch for %s (src=%s out=%s) — keeping original",
            src.name, _track_counts(src_probe), _track_counts(out_probe),
        )
        tmp.unlink(missing_ok=True)
        return "Audio/subtitle track count mismatch"
    return None


def transcode_to_av1(src: Path, staging_dir: Path, plan=None, *, loop=None) -> Path | None:
    """Encode ``src`` in ``staging_dir``.  Returns the output path or None.

    With ``plan`` (a ``codec_classifier.CodecPlan``) the codec and settings come
    from the classifier; with ``plan=None`` the fixed legacy settings are used.
    Despite the name, a plan may select HEVC (grainy live action); the output is
    verified against whatever codec was actually requested.

    ``loop`` is the caller's running event loop; with it, progress is published
    over the WebSocket.  None (e.g. a direct call) skips publishing silently.
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
        "ffmpeg", "-v", "error", "-nostdin", "-nostats", "-progress", "pipe:1", "-y",
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
    # Duration for progress: probed before the encode and outside the permit
    # (probing never holds one).  The post-encode verification probes are separate.
    duration = _duration_seconds(_probe(src))
    key = str(dest)

    # Permit first, then the subprocess with its own timeout; released before publishing.
    with _encode_slot(src.name):
        # Registered only once the permit is held (a queued file is not encoding)
        # and removed in ``finally`` so a failed or timed-out encode leaves no row.
        entry: dict[str, Any] = {
            "file": src.name,
            "destination_folder": str(dest.parent),
            "percent": 0.0,
            "eta": None,
            "speed": None,
            "codec": expect_codec,
            "started_at": time.time(),
        }
        with _encoding_lock:
            _ENCODING[key] = entry
        _publish(loop, dict(entry))
        failure: str | None = "Encode did not complete"
        try:
            failure = _encode_and_verify(
                cmd, src, tmp, key=key, duration=duration, loop=loop,
                expect_codec=expect_codec, active=active,
            )
        finally:
            # Recorded before the terminal publish, so a client that re-fetches
            # GET /api/encoding on ``done`` already sees the failed row.
            if failure is not None:
                _record_failure(src, dest, failure)
            with _encoding_lock:
                final = _ENCODING.pop(key, entry)
            _publish(loop, dict(final), done=True)
        if failure is not None:
            return None

    tmp.replace(dest)
    with _encoding_lock:
        _forget_failure_locked(str(src))
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


def after_organize(
    final_path: str | None, *, context: dict | None = None, loop=None
) -> None:
    """Entry point called once a title has been filed.

    ``context`` is the optional Engram identification context (content_type,
    is_extra, duration_seconds, video_resolution, ...).  The classifier treats it
    as a hint only; media probes win.  ``loop`` is the caller's running event
    loop, captured in async context *before* ``asyncio.to_thread``; it is used
    only to publish encode progress and may be None.  Never raises: a hook failure must not fail
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
        if not in_domain(src):
            logger.warning(
                "AV1 hook: refusing to encode %s — outside Engram's domain "
                "(only rips in staging and their AV1 output are Engram's)", src,
            )
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
        dest = transcode_to_av1(src, staging, plan, loop=loop)
        if dest is not None:
            _write_marker(src, dest, plan.class_name if plan is not None else None)
    except Exception:  # noqa: BLE001 - fail-open by design
        logger.exception("AV1 hook: unexpected error for %s", final_path)


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


class RetryError(Exception):
    """A retry request was refused.  ``status`` is the HTTP code the route answers."""

    status = 400


class RetryNotFound(RetryError):
    """The path is not an existing ``.mkv`` inside the allowed roots."""

    status = 404


class RetryConflict(RetryError):
    """The file is already encoding, a retry for it is queued, or the hook is off."""

    status = 409


# Destination keys of accepted retries that have not finished yet.  A queued
# retry is not in ``_ENCODING`` (that lists permit holders only), so without
# this a double-click would start two encodes of one file.
_RETRY_PENDING: set[str] = set()


def _staging_dir() -> Path:
    return Path(_cfg("ENGRAM_AV1_STAGING", "/staging/av1-out"))


# Engram's domain: the only places a file may be encoded from.  A rip lands in
# the curator's staging root (``library_tv_path``/``library_movies_path``); an
# encode's output lands under ``ENGRAM_AV1_STAGING``.  Nothing else on the NAS is
# Engram's to touch — the library holds files Engram never produced (legacy rips,
# downloads, anime releases), and re-encoding those silently rewrites the
# principal's collection with a different codec and a lossy generation.
_DOMAIN_ROOTS = ("/media/_staging",)


def domain_roots(library_paths: Iterable[str | None] = ()) -> list[Path]:
    """Roots a file must live under for Engram to encode it.

    Deliberately *not* ``/media/tv`` or ``/media/movies``: those are the library,
    which is read-only as far as the hook is concerned.  ``library_paths`` are
    honoured only when they resolve inside :data:`_DOMAIN_ROOTS`, so a
    misconfigured ``library_tv_path` cannot widen the domain back to the library.
    """
    roots: list[Path] = [Path(p) for p in _DOMAIN_ROOTS if Path(p).is_dir()]
    for raw in library_paths:
        if not raw:
            continue
        candidate = Path(raw)
        if candidate.is_dir() and _within(candidate, roots):
            roots.append(candidate)
    staging = _staging_dir()
    if staging.is_dir():
        roots.append(staging)
    return roots


def in_domain(path: str | os.PathLike[str], library_paths: Iterable[str | None] = ()) -> bool:
    """True when ``path`` is a file Engram is allowed to encode."""
    try:
        return _within(Path(path).resolve(strict=False), domain_roots(library_paths))
    except (OSError, ValueError, TypeError, RuntimeError):
        return False


def default_retry_roots(library_paths: Iterable[str | None]) -> list[Path]:
    """Roots a retried source may live under.  See :func:`domain_roots`.

    Kept as the name the route calls; the policy it encodes is now "Engram's
    domain only", not "anywhere Jellyfin reads from".
    """
    return domain_roots(library_paths)


def _within(path: Path, roots: Iterable[Path]) -> bool:
    """True if resolved ``path`` sits inside one of the resolved ``roots``."""
    for root in roots:
        try:
            path.relative_to(Path(root).resolve(strict=False))
            return True
        except (ValueError, OSError):
            continue
    return False


def _run_retry(src: Path, key: str, loop) -> None:
    """Worker-thread body of a retry.  Never raises."""
    try:
        after_organize(str(src), loop=loop)
    except Exception:  # noqa: BLE001 - after_organize is fail-open; belt and braces
        logger.exception("AV1 hook: retry failed for %s", src)
    finally:
        with _encoding_lock:
            _RETRY_PENDING.discard(key)


def retry_encode(
    path: str | os.PathLike[str],
    *,
    loop=None,
    roots: Iterable[str | os.PathLike[str]] | None = None,
) -> None:
    """Force a re-encode of ``path`` in the background.

    Validates synchronously and raises ``RetryNotFound`` (404) or
    ``RetryConflict`` (409) so the route stays thin.  Once accepted it deletes
    the marker (so ``_already_done`` no longer short-circuits) and runs
    ``after_organize`` on its own daemon thread, returning immediately: an
    encode takes over an hour, and a dedicated thread keeps it off the default
    executor.  The encode is the ordinary ``after_organize`` ->
    ``transcode_to_av1`` path, so it takes the same ``_encode_slot`` permit and
    shows in ``_ENCODING`` like any other; there is no second code path.

    ``roots``: the source must resolve inside one of these.  The route passes
    ``default_retry_roots(library paths)``.  None means the AV1 staging parent
    only, so a caller that forgets the roots fails closed.
    """
    try:
        src = Path(path).resolve(strict=False)
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        raise RetryNotFound(f"Not a file: {path}") from exc
    allowed = [Path(r) for r in roots] if roots is not None else default_retry_roots(())
    if not _within(src, allowed):
        logger.warning("AV1 hook: refusing retry outside Engram's domain: %s", src)
        raise RetryNotFound(f"Not a file in Engram's domain: {path}")
    if not src.is_file() or src.suffix.lower() != ".mkv":
        raise RetryNotFound(f"Not an existing .mkv file: {path}")
    if not is_enabled():
        raise RetryConflict("The AV1 hook is disabled (ENGRAM_AV1_HOOK is not 1)")

    key = str(_staging_dir() / src.name)
    with _encoding_lock:
        if key in _ENCODING or key in _RETRY_PENDING:
            raise RetryConflict(f"Already encoding {src.name}")
        _RETRY_PENDING.add(key)
        # Leave the failed list now; a new failure re-adds the row.
        _forget_failure_locked(str(src))

    try:
        _marker_path(src).unlink(missing_ok=True)
        threading.Thread(
            target=_run_retry, args=(src, key, loop), name="av1-retry", daemon=True
        ).start()
    except Exception:
        with _encoding_lock:
            _RETRY_PENDING.discard(key)
        raise
    logger.info("AV1 hook: retry queued for %s", src.name)
