"""Post-organize encoding hook (formerly the "AV1 hook").

Engram files rips as untouched MakeMKV output (MPEG-2 for DVD, MPEG-2/VC-1 for
BD). This module re-encodes a just-organized file (AV1, or x265 where the codec
classifier routes it) and moves the result to the curator's staging
directory, so the existing CT104 curator imports it and
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
* **Unmeasured is not classified.** A classifier probe that could not run,
  timed out or was killed (``ProbeInterrupted``) records a failed row and encodes
  nothing; it never falls back to the fixed settings, which would encode a
  source nobody measured (and without its deinterlace filter).
* **Off by default.** Enabled only when ``ENGRAM_ENCODE_HOOK=1`` in the container
  environment, so a rebuild without the setting behaves exactly as upstream.

Codec choice is made per file by ``codec_classifier`` (see CODEC-CLASSIFIER.md):
the media is probed and classified, and the Engram identification context is a
hint only.  ``ENGRAM_CODEC_CLASSIFIER=0`` restores the original fixed CRF/preset
encoding arguments byte-for-byte.  The same fixed command is the fallback whenever the
classifier or its encoder pre-check fails, so a classifier fault never costs the
transcode, let alone the rip -- except an interrupted probe, which is not a
classifier fault (see "Unmeasured is not classified").

Configuration (environment):
    ENGRAM_ENCODE_HOOK       "1" to enable; anything else disables.
    ENGRAM_ENCODE_STAGING    destination dir (default /staging/encode-out)
    ENGRAM_ENCODE_THREADS    encoder thread count, SVT ``lp`` / x265 ``pools``
                             (default 4; applies on every path)
    ENGRAM_CODEC_CLASSIFIER  "0" disables the classifier (fixed CRF/preset).
                             (Deinterlacing is not configurable: an interlaced or
                             undecided source always gets bwdif, a progressive one
                             never does.  The old ENGRAM_CODEC_INTERLACED is ignored.)
    ENGRAM_ENCODE_CRF        fixed-path SVT-AV1 CRF (default 22)      } fallback
    ENGRAM_ENCODE_PRESET     fixed-path SVT-AV1 preset (default 6)    } path only
    ENGRAM_ENCODE_MAX_CONCURRENT
                             max simultaneous ffmpeg encodes across the whole
                             process, any codec (default 1 = strictly serial;
                             0, negative or non-integer = unlimited).

Encode concurrency: every job's organize sweep is its own asyncio task, so
several sweeps can reach ``transcode_file`` at once.  On a small host that
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

Failures and retry: an encode that fails (``transcode_file`` returns None)
is recorded in a bounded in-memory ring buffer (``FAILURE_BUFFER_SIZE``,
``failures_snapshot()``), served next to the active rows by
``encoding_status()``.  ``retry_encode`` re-runs ``after_organize`` for such a
source after stripping its marker's done record; it goes through the same
``_encode_slot`` permit and ``_ENCODING`` registry as any other encode, so the
concurrency cap holds and the panel shows the retried file.

Codec-class override: an operator may pin one of :data:`OFFERED_CLASSES` for a
source (``set_override``) or force one on a retry.  The pin lives in the marker
under ``override`` (with the source's size/mtime, so a changed source makes it
stale and ignored), survives a completed encode and a retry, and is never held
only in memory.  ``hdr`` and ``interlaced`` are never accepted from an operator.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Deliberately still "av1" although the hook also writes x265, and so is the
# marker's "av1" JSON key (the output path).  Every file already encoded carries
# a marker with this suffix; renaming it (or the key) would make every one of
# them look unconverted and re-trigger a re-encode of the whole library.
# Guarded by test_marker_suffix_is_frozen.
MARKER_SUFFIX = ".engram-av1.json"
ENCODE_TIMEOUT_SECONDS = 6 * 60 * 60  # a 45-min BD episode is well inside this
PROGRESS_PUBLISH_INTERVAL = 2.0  # seconds between encode_progress messages per file
FAILURE_BUFFER_SIZE = 20  # recently-failed encodes kept for the panel's Retry rows


def _cfg(name: str, default: str) -> str:
    return os.environ.get(name, default)


STALE_ENV_PREFIX = "ENGRAM_AV1_"
CURRENT_ENV_PREFIX = "ENGRAM_ENCODE_"


class StaleEncodeEnvError(RuntimeError):
    """Pre-rename ``ENGRAM_AV1_*`` variables are set; the hook refuses to run on them."""


def stale_env_keys(environ: dict[str, str] | None = None) -> list[str]:
    """Sorted ``ENGRAM_AV1_*`` keys present in ``environ`` (``os.environ`` by default)."""
    env = os.environ if environ is None else environ
    return sorted(k for k in env if k.startswith(STALE_ENV_PREFIX))


def check_stale_env(environ: dict[str, str] | None = None) -> None:
    """Raise ``StaleEncodeEnvError`` if any pre-rename ``ENGRAM_AV1_*`` key is set.

    Every ``ENGRAM_AV1_*`` setting became ``ENGRAM_ENCODE_*``.  The hook defaults
    to off, so a deployment still on the old names (``ENGRAM_AV1_HOOK=1`` with no
    ``ENGRAM_ENCODE_HOOK``) would otherwise run with encoding silently disabled.
    Called from the app's startup, not at import, so importing the module (and
    the fast tests) never depends on the environment.
    """
    stale = stale_env_keys(environ)
    if not stale:
        return
    renames = ", ".join(f"{k} -> {CURRENT_ENV_PREFIX}{k[len(STALE_ENV_PREFIX) :]}" for k in stale)
    message = (
        "Encoding hook: refusing to start: renamed environment variable(s) still set: "
        f"{renames}. Rename them in the deployment's .env (see CODEC-CLASSIFIER.md, "
        '"Renaming ENGRAM_AV1_* to ENGRAM_ENCODE_*"); the old names are ignored, '
        "so leaving them would run with encoding disabled."
    )
    for key in stale:
        logger.error(
            "Encoding hook: stale variable %s was renamed to %s%s",
            key,
            CURRENT_ENV_PREFIX,
            key[len(STALE_ENV_PREFIX) :],
        )
    raise StaleEncodeEnvError(message)


def _max_concurrent() -> int:
    """Parse ``ENGRAM_ENCODE_MAX_CONCURRENT``.  0 means unlimited; never raises.

    Invalid or negative values mean unlimited (the pre-gate behaviour) rather than
    an error: a typo in an env var must not stop transcoding or the job.
    """
    raw = _cfg("ENGRAM_ENCODE_MAX_CONCURRENT", "1").strip()
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
        logger.info("Encoding hook: waiting for encode slot (%d/%d busy) for %s", busy, limit, name)
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
# value: {file, destination_folder, percent, eta, speed, codec, codec_class, forced,
#         started_at}
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

# Newest last.  Entries: {file, source, destination_folder, reason, codec_class,
# forced, failed_at}.
# ``source`` is the absolute source path a Retry posts back.  Guarded by
# ``_encoding_lock`` like the active registry.
_FAILED: deque[dict[str, Any]] = deque(maxlen=FAILURE_BUFFER_SIZE)


def _forget_failure_locked(source: str) -> None:
    """Drop every buffered failure for ``source``.  Caller holds ``_encoding_lock``."""
    for stale in [e for e in _FAILED if e.get("source") == source]:
        _FAILED.remove(stale)


def _record_failure(
    src: Path, dest: Path, reason: str, *, codec_class: str | None = None, forced: bool = False
) -> None:
    """Remember a failed encode for the panel, one row per source.  Never raises."""
    try:
        entry = {
            "file": src.name,
            "source": str(src),
            "destination_folder": str(dest.parent),
            "reason": reason,
            "codec_class": codec_class,
            "forced": forced,
            "failed_at": time.time(),
        }
        with _encoding_lock:
            _forget_failure_locked(str(src))
            _FAILED.append(entry)
    except Exception:  # noqa: BLE001 - fail-open: the buffer is cosmetic
        logger.debug("Encoding hook: could not record failure for %s", src, exc_info=True)


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


def iter_progress(lines: Iterable[str], duration_seconds: float | None) -> Iterator[dict[str, Any]]:
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
        logger.debug("Encoding hook: could not publish encode progress", exc_info=True)


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
                logger.debug("Encoding hook: progress parse failed for %s", key, exc_info=True)
                for _ in stream:
                    pass
    except Exception:  # noqa: BLE001 - fail-open: progress is cosmetic
        logger.debug("Encoding hook: progress reader failed for %s", key, exc_info=True)


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
        target=_consume_progress,
        args=(read_fd, key, duration, loop),
        name="encode-progress",
        daemon=True,
    )
    try:
        reader.start()
    except Exception:  # noqa: BLE001 - no progress, but the encode still runs
        os.close(read_fd)
        logger.debug("Encoding hook: could not start progress reader", exc_info=True)
    try:
        return subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=write_fd,
            stderr=subprocess.PIPE,
            text=True,
            timeout=ENCODE_TIMEOUT_SECONDS,
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
        logger.exception("Encoding hook: could not load codec_classifier; using fixed settings")
        return None


def is_enabled() -> bool:
    return _cfg("ENGRAM_ENCODE_HOOK", "0") == "1"


def _probe(path: Path) -> dict:
    """Return codec/dimension/track counts for a media file, or {} on failure."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
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
    """True when the marker's *done record* matches this exact source revision.

    Reads only the top-level ``size``/``mtime``.  The ``override`` pin carries its
    own size/mtime (for staleness) and must never count as "done", or a pin set
    on a fresh rip would make the hook skip the very encode it is meant to steer.

    A record of the old "interlaced, left untouched" skip (class ``interlaced``,
    no output) is not done: interlaced sources are now deinterlaced and encoded,
    so such a file is encoded the next time the hook sees it.
    """
    data = _read_marker(src)
    if not data:
        return False
    if data.get("codec_class") == "interlaced" and data.get("av1") is None:
        return False
    try:
        st = src.stat()
    except OSError:
        return False
    return data.get("size") == st.st_size and data.get("mtime") == int(st.st_mtime)


# Serialises every read-modify-write of a marker.  The marker now holds a user
# choice (the pin) next to the done record, so a PUT racing an encode's final
# ``_write_marker`` must not lose either half.
_marker_lock = threading.RLock()

# Top-level keys of the done record.  ``override`` is the only other key.
_DONE_FIELDS = ("size", "mtime", "av1", "codec_class", "forced")


def _read_marker(src: Path) -> dict[str, Any]:
    """The marker as a dict; {} when missing, unreadable or not a JSON object."""
    try:
        data = json.loads(_marker_path(src).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _store_marker(src: Path, data: dict[str, Any]) -> None:
    """Atomically replace the marker with ``data`` (temp + ``os.replace``).

    An empty ``data`` deletes the marker instead.  Raises ``OSError``.
    """
    marker = _marker_path(src)
    if not data:
        marker.unlink(missing_ok=True)
        return
    tmp = marker.with_name(marker.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data))
        os.replace(tmp, marker)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _override_fresh(override: Any, st: os.stat_result) -> bool:
    """True when a pin was set on this exact source revision (D5)."""
    return (
        isinstance(override, dict)
        and override.get("size") == st.st_size
        and override.get("mtime") == int(st.st_mtime)
    )


def _write_marker(
    src: Path, dest: Path | None, codec_class: str | None = None, *, forced: bool = False
) -> None:
    """Record that ``src`` has been dealt with.  ``dest`` is None for a passthrough.

    Merges: a fresh ``override`` pin already in the marker is kept, so a
    successful encode does not erase the operator's choice.  A stale pin (the
    source changed since it was set) is dropped here.
    """
    try:
        with _marker_lock:
            st = src.stat()
            data: dict[str, Any] = {
                "size": st.st_size,
                "mtime": int(st.st_mtime),
                "av1": str(dest) if dest is not None else None,
            }
            if codec_class:
                data["codec_class"] = codec_class
            if forced:
                data["forced"] = True
            override = _read_marker(src).get("override")
            if _override_fresh(override, st):
                data["override"] = override
            _store_marker(src, data)
    except OSError:
        logger.warning("Encoding hook: could not write marker for %s", src)


def _strip_done(src: Path) -> None:
    """Drop the done record, keeping only the ``override`` pin.  Raises ``OSError``.

    A marker left with nothing in it is deleted.  This is what a retry does
    instead of unlinking the marker, which would delete the pin it is applying.
    """
    with _marker_lock:
        override = _read_marker(src).get("override")
        _store_marker(src, {"override": override} if isinstance(override, dict) else {})


def _store_override(src: Path, codec_class: str) -> dict[str, Any]:
    """Persist a pin for the current revision of ``src``.  Raises ``OSError``."""
    with _marker_lock:
        st = src.stat()
        override = {
            "codec_class": codec_class,
            "set_at": time.time(),
            "size": st.st_size,
            "mtime": int(st.st_mtime),
        }
        data = _read_marker(src)
        data["override"] = override
        _store_marker(src, data)
        return override


# ---------------------------------------------------------------------------
# Forced codec class (operator override)
# ---------------------------------------------------------------------------

# The only classes the API/UI may offer or accept.  ``interlaced`` is decided
# from the source by the classifier's idet probe, and ``hdr`` is unreachable
# for DVD / 1080p Blu-ray (both SDR) and invites a washed-out publish, so
# neither is ever accepted from an operator.  ``codec_classifier.CLASSES``
# keeps all six internally.
OFFERED_CLASSES: tuple[str, ...] = (
    "passthrough",
    "animation",
    "live_action_grainy",
    "live_action_clean",
)
_HDR_TRANSFERS = frozenset({"smpte2084", "arib-std-b67"})  # PQ, HLG


def forced_class_problem(codec_class: str | None, probe: dict) -> str | None:
    """Why ``codec_class`` may not be forced on a source with ``probe``; None if it may."""
    if codec_class not in OFFERED_CLASSES:
        return (
            f"Codec class {codec_class!r} cannot be forced; "
            f"choose one of: {', '.join(OFFERED_CLASSES)}"
        )
    if codec_class == "passthrough":
        return None
    video = _video_stream(probe)
    if not video:
        return "Could not probe the source's video stream"
    if str(video.get("codec_name") or "").lower() == "av1":
        return "Source is already AV1; re-encoding would add a second lossy generation"
    if str(video.get("color_transfer") or "").lower() in _HDR_TRANSFERS:
        return "Source is HDR; only passthrough may be forced on it"
    return None


def forced_plan(
    src: Path, codec_class: str, probe: dict | None = None, interlace: dict | None = None
):
    """A ``CodecPlan`` for ``codec_class`` with the source's own properties.

    The class picks codec / CRF / preset.  Resolution and bit depth come from
    ``probe`` (probed from ``src`` when not given).  Deinterlacing comes from
    ``interlace`` -- the classifier's cheap idet probe, run on ``src`` here
    unless given -- so an interlaced source gets ``bwdif`` whichever class was
    forced or pinned.  ``passthrough`` encodes nothing and skips the idet probe.
    Returns None when the classifier module cannot be loaded.  Raises
    ``ValueError`` for a class outside :data:`OFFERED_CLASSES`, and
    :class:`ProbeInterrupted` when the idet probe could not run or was killed.
    """
    if codec_class not in OFFERED_CLASSES:
        raise ValueError(f"codec class {codec_class!r} cannot be forced")
    mod = _load_classifier()
    if mod is None:
        return None
    video = _video_stream(probe if probe is not None else _probe(src))
    facts: dict[str, Any] = {}
    if codec_class != "passthrough":
        if interlace is None:
            try:
                interlace = mod.probe_interlace(str(src))
            except Exception as exc:
                _reraise_if_aborted(mod, exc)
                raise
        facts.update(interlace)
    # The full probe's stream fields win over the interlace probe's copies.
    facts.update({k: video[k] for k in ("width", "height", "pix_fmt") if video.get(k)})
    return mod.forced_class_plan(
        codec_class,
        facts,
        reason=f"forced by operator ({codec_class})",
    )


class ProbeInterrupted(RuntimeError):
    """The classifier could not measure the source: a probe never ran, timed out
    or was killed (``codec_classifier.ProbeAborted``).

    Not a reason to fall back to the fixed settings: that command has no
    deinterlace filter and the class it stands in for was never measured.  The
    hook records a failed row (with Retry) and encodes nothing.
    """


def _reraise_if_aborted(mod, exc: BaseException) -> None:
    """Raise :class:`ProbeInterrupted` from ``exc`` if it is the classifier's ProbeAborted."""
    aborted = getattr(mod, "ProbeAborted", None)
    if isinstance(aborted, type) and isinstance(exc, aborted):
        raise ProbeInterrupted(str(exc)) from exc


def _plan_forced(plan) -> bool:
    return plan is not None and bool((getattr(plan, "evidence", None) or {}).get("forced"))


def _would_encode_in_place(src: Path, staging_dir: Path) -> bool:
    """True when the encode's destination is the source itself.

    ``dest = staging_dir / src.name`` and ``tmp.replace(dest)``: for a source
    sitting directly in ``ENGRAM_ENCODE_STAGING`` that overwrites the original.
    """
    try:
        return (staging_dir / src.name).resolve(strict=False) == src.resolve(strict=False)
    except (OSError, RuntimeError):
        return True  # cannot tell: fail closed


def _legacy_video_args(threads: str) -> list[str]:
    """The original fixed-settings video arguments.  Kill-switch / fallback path.

    Deliberately unchanged from the pre-classifier hook (including the absence of
    ``-pix_fmt`` and ``film-grain-denoise``): ``ENGRAM_CODEC_CLASSIFIER=0`` must
    reproduce the old command exactly.
    """
    crf = _cfg("ENGRAM_ENCODE_CRF", "22")
    preset = _cfg("ENGRAM_ENCODE_PRESET", "6")
    return [
        "-c:v",
        "libsvtav1",
        "-crf",
        crf,
        "-preset",
        preset,
        "-svtav1-params",
        f"tune=0:lp={threads}",
    ]


_ENCODER_PROBE_INPUT = ("-f", "lavfi", "-i", "testsrc=size=128x128:rate=24:duration=0.5")
_ENCODER_PROBE_TIMEOUT = 60
# How each encoder wrapper reports an encoder parameter it would not take.  All
# three are *warnings* and ffmpeg still exits 0 and encodes without the
# parameter, which is why a return code can never detect them.  Measured on
# ffmpeg 7.1.5: libsvtav1 (SVT-AV1 2.3.0) "Error parsing option tf-strength: 1.";
# libx265 "Unknown option: bogus." and "Invalid value for aq-mode: banana.".
_REJECTED_PARAM_RE = re.compile(
    r"Error parsing option ([^\s:]+):|Unknown option: ([^\s.]+)\.|Invalid value for ([^\s:]+):"
)
_SVT_VERSION_RE = re.compile(r"SVT-AV1 Encoder Lib (v[\w.+-]+)")
_EXPLODE = ("-err_detect:v", "explode")


def _encoder_probe(video_args: tuple[str, ...], *, loglevel: str) -> subprocess.CompletedProcess:
    """Encode six synthetic frames with ``video_args``.

    Raises :class:`ProbeInterrupted` when the probe could not run, timed out or
    was killed: that says nothing about the encoder, and treating it as a
    rejection would silently degrade the encode to the fixed settings.
    """
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-v",
        loglevel,
        "-y",
        *_ENCODER_PROBE_INPUT,
        *video_args,
        "-frames:v",
        "6",
        "-f",
        "null",
        "-",
    ]
    try:
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=_ENCODER_PROBE_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise ProbeInterrupted(f"encoder check timed out after {_ENCODER_PROBE_TIMEOUT}s") from exc
    except (subprocess.SubprocessError, OSError) as exc:
        raise ProbeInterrupted(f"encoder check could not run: {exc}") from exc
    if run.returncode < 0 or run.returncode == _FFMPEG_INTERRUPTED_RC:
        raise ProbeInterrupted(f"encoder check interrupted (ffmpeg exited {run.returncode})")
    return run


@lru_cache(maxsize=32)
def _encoder_accepts(video_args: tuple[str, ...]) -> bool:
    """True if this ffmpeg/encoder build accepts ``video_args`` on a tiny synthetic clip.

    Encoders differ by build (an older SVT-AV1 may reject newer parameters), and
    a rejected parameter would otherwise surface only after minutes of encoding.
    Cached per process, so it costs well under a second once per distinct command.
    Only a *fatal* rejection shows here; parameters an encoder merely ignores
    are :func:`_rejected_params`'s.  Raises :class:`ProbeInterrupted` (never
    cached) when the check itself was interrupted.
    """
    return _encoder_probe(video_args, loglevel="error").returncode == 0


@dataclass(frozen=True)
class _EncoderParamCheck:
    rejected: frozenset[str]
    encoder: str  # e.g. "SVT-AV1 v2.3.0", for the log line


@lru_cache(maxsize=32)
def _rejected_params(video_args: tuple[str, ...]) -> _EncoderParamCheck:
    """The encoder-parameter keys in ``video_args`` this host's encoder refuses.

    Read from the encoder's own warnings, so it follows whatever SVT-AV1 /
    x265 is installed: nothing here lists which parameters a version knows, so
    an upgrade that adds one starts emitting it and a downgrade that loses one
    stops, with a WARNING either way.  ``-err_detect:v explode`` is left out of
    this probe so every rejected key is reported in one run rather than the
    first one only.  Cached per command; raises :class:`ProbeInterrupted`
    (never cached) when the check was interrupted.
    """
    args = list(video_args)
    for i in range(len(args) - 1):
        if (args[i], args[i + 1]) == _EXPLODE:
            del args[i : i + 2]
            break
    run = _encoder_probe(tuple(args), loglevel="warning")
    stderr = run.stderr or ""
    rejected = frozenset(
        next(g for g in m.groups() if g) for m in _REJECTED_PARAM_RE.finditer(stderr)
    )
    version = _SVT_VERSION_RE.search(stderr)
    if version:
        encoder = f"SVT-AV1 {version.group(1)}"
    elif "libx265" in video_args:
        encoder = "x265"
    else:
        encoder = "the encoder"
    return _EncoderParamCheck(rejected, encoder)


def _drop_rejected_params(plan, threads: str):
    """``plan`` without the parameters this host's encoder would ignore.

    Only the rejected keys go: the rest of the tuning (and the plan's filters)
    is kept, so one unknown parameter does not cost all the others.
    """
    if plan is None or getattr(plan, "is_passthrough", True):
        return plan
    check = _rejected_params(tuple(plan.video_args(threads)))
    if not check.rejected:
        return plan
    required = plan.svt_required if plan.codec == "av1" else plan.x265_required
    lost = sorted(check.rejected & {k for k, _ in required})
    logger.warning(
        "Encoding hook: %s rejects %s; not emitting %s for class=%s",
        check.encoder,
        ", ".join(sorted(check.rejected)),
        "them" if len(check.rejected) > 1 else "it",
        plan.class_name,
    )
    if lost:
        logger.error(
            "Encoding hook: %s cannot apply required setting(s) %s for class=%s; "
            "the encode runs without them",
            check.encoder,
            ", ".join(lost),
            plan.class_name,
        )
    return plan.without_params(check.rejected)


def _select_video_args(plan, threads: str) -> tuple[list[str], str, object | None]:
    """Choose the video arguments, degrading gracefully.

    First the parameters this host's encoder would silently ignore are dropped
    (:func:`_rejected_params`).  Then, in order: the plan's full parameter set,
    only its essential parameters (drops tuning extras an older encoder fails
    on), then the fixed legacy command.  Returns (args, expected output
    codec_name, plan-or-None).  Raises :class:`ProbeInterrupted` when an
    encoder check was interrupted: that is not a verdict on the encoder.

    The plan's video filters (``bwdif`` on an interlaced source) belong to the
    source, not to the settings, so they survive the fall to the fixed command:
    an encoder rejecting a tuning parameter must not leave combing in.
    """
    if plan is not None:
        plan = _drop_rejected_params(plan, threads)
        for optional in (True, False):
            args = plan.video_args(threads, optional=optional)
            if _encoder_accepts(tuple(args)):
                if not optional:
                    logger.warning(
                        "Encoding hook: encoder rejected tuning extras for class=%s; "
                        "using essential parameters only",
                        plan.class_name,
                    )
                return args, plan.output_codec_name or "av1", plan
        logger.warning(
            "Encoding hook: encoder rejected class=%s settings; using fixed settings",
            plan.class_name,
        )
        filters = list(getattr(plan, "video_filters", None) or [])
        if filters:
            return ["-vf", ",".join(filters), *_legacy_video_args(threads)], "av1", None
    return _legacy_video_args(threads), "av1", None


# Encoder chatter that is never the failure.  Only *info* banners: an
# ``Svt[error]:`` / ``x265 [error]:`` line is a genuine encoder fault and must survive.
_STDERR_NOISE = re.compile(
    r"^svt\[(info|warn)\]"
    r"|-{10,}"
    r"|^x265 \[(info|warning)\]"
    r"|options: cpuid="
    r"|frame=.*\bfps=",
    re.IGNORECASE,
)
_STDERR_ERROR = re.compile(
    r"error|invalid|no such file|permission denied|cannot|failed|unable|unknown"
    r"|not found|unrecognized",
    re.IGNORECASE,
)
# fftools/ffmpeg.c exits 255 when it caught SIGINT/SIGTERM (it reports
# "received signal" only at info level, which ``-v error`` hides).
_FFMPEG_INTERRUPTED_RC = 255


def _failure_reason(returncode: int | None, stderr: str | None) -> str:
    """Pick the panel's failure reason from ffmpeg's exit status and stderr.

    The *last* stderr line is the wrong choice: when ffmpeg is interrupted the
    tail is SVT-AV1's startup banner, which reads like a codec fault.  Banner
    noise is dropped, the first error-looking line wins, else the first line
    that survived.  A signal death (negative return code) is named as such.
    """
    if returncode is not None and returncode < 0:
        num = -returncode
        try:
            name = f" ({signal.Signals(num).name})"
        except ValueError:
            name = ""
        head = f"ffmpeg killed by signal {num}{name}"
    elif returncode == _FFMPEG_INTERRUPTED_RC:
        head = f"ffmpeg exited {returncode} (interrupted by a signal)"
    else:
        head = f"ffmpeg exited {returncode}"

    lines = [ln.strip() for ln in (stderr or "").splitlines()]
    lines = [ln for ln in lines if ln and not _STDERR_NOISE.search(ln)]
    pick = next((ln for ln in lines if _STDERR_ERROR.search(ln)), lines[0] if lines else "")
    return f"{head}: {pick[:200]}" if pick else head


def _encode_and_verify(
    cmd: list[str],
    src: Path,
    tmp: Path,
    *,
    key: str,
    duration: float | None,
    loop,
    expect_codec: str,
    active,
) -> str | None:
    """Run the encode and verify ``tmp``.

    Returns None when ``tmp`` may be published, otherwise a short human-readable
    failure reason (shown on the panel's failed row).  Called with the encode
    permit held.  On any failure ``tmp`` is removed.
    """
    try:
        run = _run_encode(cmd, key=key, duration=duration, loop=loop)
    except subprocess.TimeoutExpired:
        logger.error("Encoding hook: encode timed out for %s", src.name)
        tmp.unlink(missing_ok=True)
        return "Encode timed out"

    if run.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        stderr = (run.stderr or "").strip()
        # The log keeps the raw tail; only the panel's reason is cleaned.
        logger.error("Encoding hook: encode failed for %s: %s", src.name, stderr[-500:])
        tmp.unlink(missing_ok=True)
        return _failure_reason(run.returncode, stderr)

    # Verify before publishing: codec must be what was asked for and track counts must match.
    src_probe = _probe(src)
    out_probe = _probe(tmp)
    if _video_codec(out_probe) != expect_codec:
        logger.error("Encoding hook: output is not %s for %s", expect_codec, src.name)
        tmp.unlink(missing_ok=True)
        return f"Output is not {expect_codec}"
    if active is not None:
        out_v, src_v = _video_stream(out_probe), _video_stream(src_probe)
        if out_v.get("pix_fmt") != active.pix_fmt:
            logger.error(
                "Encoding hook: output pix_fmt %s is not %s for %s",
                out_v.get("pix_fmt"),
                active.pix_fmt,
                src.name,
            )
            tmp.unlink(missing_ok=True)
            return f"Output pixel format is not {active.pix_fmt}"
        # An HDR title that loses its transfer tag plays back washed out: refuse the swap.
        if active.hdr and src_v.get("color_transfer") != out_v.get("color_transfer"):
            logger.error(
                "Encoding hook: HDR transfer not preserved for %s (src=%s out=%s)",
                src.name,
                src_v.get("color_transfer"),
                out_v.get("color_transfer"),
            )
            tmp.unlink(missing_ok=True)
            return "HDR transfer not preserved"
    if _track_counts(out_probe) != _track_counts(src_probe):
        logger.error(
            "Encoding hook: track mismatch for %s (src=%s out=%s) — keeping original",
            src.name,
            _track_counts(src_probe),
            _track_counts(out_probe),
        )
        tmp.unlink(missing_ok=True)
        return "Audio/subtitle track count mismatch"
    return None


def transcode_file(src: Path, staging_dir: Path, plan=None, *, loop=None) -> Path | None:
    """Encode ``src`` in ``staging_dir``.  Returns the output path or None.

    With ``plan`` (a ``codec_classifier.CodecPlan``) the codec and settings come
    from the classifier; with ``plan=None`` the fixed legacy settings are used.
    Despite the name, a plan may select HEVC (grainy live action); the output is
    verified against whatever codec was actually requested.

    ``loop`` is the caller's running event loop; with it, progress is published
    over the WebSocket.  None (e.g. a direct call) skips publishing silently.
    """
    threads = _cfg("ENGRAM_ENCODE_THREADS", "4")

    # Never in-place, on every path (classifier, forced, and the kill switch).
    if _would_encode_in_place(src, staging_dir):
        logger.error(
            "Encoding hook: refusing to encode %s in place (destination is the source)", src
        )
        return None

    staging_dir.mkdir(parents=True, exist_ok=True)
    dest = staging_dir / src.name

    # Encode to a temp name, then rename, so a partial file is never mistaken
    # for a finished one by the curator's settle check.
    tmp = staging_dir / f".{src.stem}.partial.mkv"

    if plan is None:
        video_args, expect_codec, active = _legacy_video_args(threads), "av1", None
    else:
        try:
            video_args, expect_codec, active = _select_video_args(plan, threads)
        except ProbeInterrupted as exc:
            # Not a verdict on the encoder, so not a reason for the fixed command.
            logger.error("Encoding hook: not encoding %s: %s", src.name, exc)
            _record_failure(
                src,
                dest,
                f"Encoder check interrupted, nothing encoded: {exc}",
                codec_class=plan.class_name,
                forced=_plan_forced(plan),
            )
            return None

    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-nostdin",
        "-nostats",
        "-progress",
        "pipe:1",
        "-y",
        "-i",
        str(src),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-map",
        "0:s?",
        *video_args,
        "-c:a",
        "copy",
        "-c:s",
        "copy",
        str(tmp),
    ]
    codec_class = active.class_name if active is not None else None
    forced = _plan_forced(active)
    if active is not None:
        logger.info(
            "Encoding hook: encoding %s -> %s (class=%s%s)",
            src.name,
            dest.name,
            active.class_name,
            ", forced" if forced else "",
        )
    else:
        logger.info(
            "Encoding hook: encoding %s -> %s (class=fixed crf=%s preset=%s)",
            src.name,
            dest.name,
            _cfg("ENGRAM_ENCODE_CRF", "22"),
            _cfg("ENGRAM_ENCODE_PRESET", "6"),
        )
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
            "codec_class": codec_class,
            "forced": forced,
            "started_at": time.time(),
        }
        with _encoding_lock:
            _ENCODING[key] = entry
        _publish(loop, dict(entry))
        failure: str | None = "Encode did not complete"
        try:
            failure = _encode_and_verify(
                cmd,
                src,
                tmp,
                key=key,
                duration=duration,
                loop=loop,
                expect_codec=expect_codec,
                active=active,
            )
        finally:
            # Recorded before the terminal publish, so a client that re-fetches
            # GET /api/encoding on ``done`` already sees the failed row.
            if failure is not None:
                _record_failure(src, dest, failure, codec_class=codec_class, forced=forced)
            with _encoding_lock:
                final = _ENCODING.pop(key, entry)
            _publish(loop, dict(final), done=True)
        if failure is not None:
            return None

    tmp.replace(dest)
    with _encoding_lock:
        _forget_failure_locked(str(src))
    logger.info(
        "Encoding hook: %s -> %s (%.1f MB -> %.1f MB)",
        src.name,
        dest.name,
        src.stat().st_size / 1e6,
        dest.stat().st_size / 1e6,
    )
    return dest


def _classify(src: Path, context: dict | None):
    """Return a CodecPlan, or None to use the fixed settings.

    Raises only :class:`ProbeInterrupted`: an unmeasured source is not handed
    to the fixed settings.
    """
    if not classifier_enabled():
        return None
    mod = _load_classifier()
    if mod is None:
        return None
    try:
        # Deinterlacing is the source's (codec_classifier.needs_deinterlace);
        # no setting turns it on or off.
        plan = mod.plan_for_file(str(src), context)
        logger.info("Encoding hook: codec_classifier: %s (%s)", plan.describe(), src.name)
        return plan
    except Exception as exc:  # noqa: BLE001 - fail-open: fixed settings
        _reraise_if_aborted(mod, exc)
        logger.exception("Encoding hook: classifier failed for %s; using fixed settings", src.name)
        return None


def _pinned_class(src: Path) -> str | None:
    """The marker's pinned class when the pin matches this source revision.  Never raises."""
    override = _read_marker(src).get("override")
    if not isinstance(override, dict):
        return None
    try:
        st = src.stat()
    except OSError:
        return None
    if not _override_fresh(override, st):
        logger.info(
            "Encoding hook: ignoring stale codec-class pin for %s (source changed)", src.name
        )
        return None
    cls = override.get("codec_class")
    return cls if isinstance(cls, str) else None


def after_organize(
    final_path: str | None,
    *,
    context: dict | None = None,
    loop=None,
    forced_class: str | None = None,
) -> None:
    """Entry point called once a title has been filed.

    ``context`` is the optional Engram identification context (content_type,
    is_extra, duration_seconds, video_resolution, ...).  The classifier treats it
    as a hint only; media probes win.  ``loop`` is the caller's running event
    loop, captured in async context *before* ``asyncio.to_thread``; it is used
    only to publish encode progress and may be None.  Never raises: a hook failure must not fail
    the job.

    ``forced_class`` (one of :data:`OFFERED_CLASSES`) replaces the classifier's
    decision and takes precedence over a pin stored in the marker; without it a
    fresh pin applies.  Either is ignored under ``ENGRAM_CODEC_CLASSIFIER=0``,
    which keeps the fixed command.  A forced class still probes the source for
    its resolution, and is refused (nothing encoded, no marker) on an AV1 or
    HDR source unless it is ``passthrough``.
    """
    if not is_enabled():
        return
    if not final_path:
        return

    try:
        src = Path(final_path)
        if not src.is_file():
            logger.warning("Encoding hook: filed path is not a file: %s", final_path)
            return
        if src.suffix.lower() != ".mkv":
            return
        if not in_domain(src):
            logger.warning(
                "Encoding hook: refusing to encode %s — outside Engram's domain "
                "(only rips in staging and their encoded output are Engram's)",
                src,
            )
            return
        if _already_done(src):
            logger.info("Encoding hook: already converted, skipping %s", src.name)
            return

        staging = _staging_dir()
        if _would_encode_in_place(src, staging):
            logger.error(
                "Encoding hook: refusing to encode %s in place (it is in ENGRAM_ENCODE_STAGING)",
                src,
            )
            return

        cls = forced_class or _pinned_class(src)
        if cls is not None and not classifier_enabled():
            logger.warning(
                "Encoding hook: ignoring forced class=%s for %s: ENGRAM_CODEC_CLASSIFIER=0 "
                "keeps the fixed settings",
                cls,
                src.name,
            )
            cls = None

        forced = cls is not None
        try:
            if forced:
                probe = _probe(src)
                problem = forced_class_problem(cls, probe)
                if problem is not None:
                    logger.error(
                        "Encoding hook: refusing forced class=%s for %s: %s", cls, src.name, problem
                    )
                    return
                plan = forced_plan(src, cls, probe)
                if plan is None:
                    logger.error(
                        "Encoding hook: classifier unavailable; cannot force class=%s for %s",
                        cls,
                        src.name,
                    )
                    return
                logger.info("Encoding hook: codec class forced: %s (%s)", plan.describe(), src.name)
            else:
                plan = _classify(src, context)
        except ProbeInterrupted as exc:
            # Visible, retryable, and nothing encoded: no marker is written, so
            # the next organize pass (or Retry) probes the source again.
            logger.error("Encoding hook: not encoding %s: %s", src.name, exc)
            _record_failure(
                src,
                staging / src.name,
                f"Source probe interrupted, nothing encoded: {exc}",
                codec_class=cls,
                forced=forced,
            )
            return

        if plan is not None and plan.is_passthrough:
            logger.info("Encoding hook: leaving %s untouched (%s)", src.name, plan.reason)
            _write_marker(src, None, plan.class_name, forced=forced)
            return

        dest = transcode_file(src, staging, plan, loop=loop)
        if dest is not None:
            _write_marker(src, dest, plan.class_name if plan is not None else None, forced=forced)
    except Exception:  # noqa: BLE001 - fail-open by design
        logger.exception("Encoding hook: unexpected error for %s", final_path)


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


class RetryInvalid(RetryError):
    """The request is well-formed but unsafe: a class that may not be forced on
    this source, a class outside :data:`OFFERED_CLASSES`, or a source inside
    ``ENGRAM_ENCODE_STAGING`` (where the encode would overwrite it)."""

    status = 422


# Destination keys of accepted retries that have not finished yet.  A queued
# retry is not in ``_ENCODING`` (that lists permit holders only), so without
# this a double-click would start two encodes of one file.
_RETRY_PENDING: set[str] = set()


DEFAULT_ENCODE_STAGING = "/staging/encode-out"


def _staging_dir() -> Path:
    # Retiring output left under an older default is a manual runbook step
    # (CODEC-CLASSIFIER.md), never something the app does on its own.
    return Path(_cfg("ENGRAM_ENCODE_STAGING", DEFAULT_ENCODE_STAGING))


# Engram's domain: the only places a file may be encoded from.  A rip lands in
# the curator's staging root (``library_tv_path``/``library_movies_path``); an
# encode's output lands under ``ENGRAM_ENCODE_STAGING``.  Nothing else on the NAS is
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
        logger.exception("Encoding hook: retry failed for %s", src)
    finally:
        with _encoding_lock:
            _RETRY_PENDING.discard(key)


def _resolve_source(
    path: str | os.PathLike[str], roots: Iterable[str | os.PathLike[str]] | None
) -> Path:
    """Resolve ``path`` to an existing ``.mkv`` inside ``roots`` or raise ``RetryNotFound``.

    ``roots`` None means Engram's domain only (fail closed).
    """
    try:
        src = Path(path).resolve(strict=False)
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        raise RetryNotFound(f"Not a file: {path}") from exc
    allowed = [Path(r) for r in roots] if roots is not None else default_retry_roots(())
    if not _within(src, allowed):
        logger.warning("Encoding hook: refusing request outside Engram's domain: %s", src)
        raise RetryNotFound(f"Not a file in Engram's domain: {path}")
    if not src.is_file() or src.suffix.lower() != ".mkv":
        raise RetryNotFound(f"Not an existing .mkv file: {path}")
    return src


def _refuse_in_place(src: Path) -> None:
    if _would_encode_in_place(src, _staging_dir()):
        raise RetryInvalid(
            f"{src.name} is in ENGRAM_ENCODE_STAGING; encoding it would overwrite the source"
        )


def _validate_forced(src: Path, codec_class: str) -> None:
    """Raise ``RetryInvalid`` unless ``codec_class`` may be forced on ``src``.  Probes."""
    if codec_class not in OFFERED_CLASSES:  # before the probe: no ffprobe for junk input
        raise RetryInvalid(forced_class_problem(codec_class, {}))
    problem = forced_class_problem(codec_class, _probe(src))
    if problem is not None:
        raise RetryInvalid(problem)


def retry_encode(
    path: str | os.PathLike[str],
    *,
    loop=None,
    roots: Iterable[str | os.PathLike[str]] | None = None,
    codec_class: str | None = None,
) -> None:
    """Force a re-encode of ``path`` in the background.

    Validates synchronously and raises ``RetryNotFound`` (404),
    ``RetryConflict`` (409) or ``RetryInvalid`` (422) so the route stays thin.
    Once accepted it strips the marker's done record (so ``_already_done`` no
    longer short-circuits) while keeping its ``override`` pin, and runs
    ``after_organize`` on its own daemon thread, returning immediately: an
    encode takes over an hour, and a dedicated thread keeps it off the default
    executor.  The encode is the ordinary ``after_organize`` ->
    ``transcode_file`` path, so it takes the same ``_encode_slot`` permit and
    shows in ``_ENCODING`` like any other; there is no second code path.

    ``codec_class`` (one of :data:`OFFERED_CLASSES`) is persisted as the pin
    before the encode is queued, so the choice is on disk, never only in
    memory: the worker reads it back from the marker.  Refused with 409 under
    ``ENGRAM_CODEC_CLASSIFIER=0``.

    ``roots``: the source must resolve inside one of these.  The route passes
    ``default_retry_roots(library paths)``.  None means the encode staging parent
    only, so a caller that forgets the roots fails closed.

    May run ffprobe (with ``codec_class``); call it off the event loop.
    """
    src = _resolve_source(path, roots)
    if not is_enabled():
        raise RetryConflict("The encoding hook is disabled (ENGRAM_ENCODE_HOOK is not 1)")
    _refuse_in_place(src)
    if codec_class is not None:
        if not classifier_enabled():
            raise RetryConflict(
                "The codec classifier is disabled (ENGRAM_CODEC_CLASSIFIER=0); "
                "a codec class cannot be forced"
            )
        _validate_forced(src, codec_class)

    key = str(_staging_dir() / src.name)
    with _encoding_lock:
        if key in _ENCODING or key in _RETRY_PENDING:
            raise RetryConflict(f"Already encoding {src.name}")
        _RETRY_PENDING.add(key)
        # Leave the failed list now; a new failure re-adds the row.
        _forget_failure_locked(str(src))

    try:
        with _marker_lock:
            if codec_class is not None:
                _store_override(src, codec_class)
            _strip_done(src)
        threading.Thread(
            target=_run_retry, args=(src, key, loop), name="encode-retry", daemon=True
        ).start()
    except Exception:
        with _encoding_lock:
            _RETRY_PENDING.discard(key)
        raise
    logger.info(
        "Encoding hook: retry queued for %s%s",
        src.name,
        f" (class={codec_class}, forced)" if codec_class else "",
    )


# ---------------------------------------------------------------------------
# Codec-class pin (override)
# ---------------------------------------------------------------------------


def override_status(
    path: str | os.PathLike[str],
    *,
    roots: Iterable[str | os.PathLike[str]] | None = None,
) -> dict[str, Any]:
    """``GET /api/encoding/override`` payload for ``path``.

    Outside the domain this answers ``in_domain: false`` without reading any
    marker.  Inside it, a missing file raises ``RetryNotFound``.  ``override``
    is the stored pin plus ``stale`` (True when the source changed since it
    was set; a stale pin is ignored and dropped by the next encode).
    """
    status: dict[str, Any] = {
        "file": str(path),
        "in_domain": False,
        "done": False,
        "applied_class": None,
        "forced": False,
        "override": None,
        "classifier_enabled": classifier_enabled(),
        "classes": list(OFFERED_CLASSES),
    }
    try:
        _resolve_source(path, roots)
    except RetryNotFound:
        try:
            resolved = Path(path).resolve(strict=False)
            allowed = [Path(r) for r in roots] if roots is not None else default_retry_roots(())
            inside = _within(resolved, allowed)
        except (OSError, ValueError, TypeError, RuntimeError):
            inside = False
        if inside:
            raise  # in the domain but not an existing .mkv
        return status
    src = Path(path).resolve(strict=False)
    status["file"] = str(src)
    status["in_domain"] = True
    data = _read_marker(src)
    done = _already_done(src)
    status["done"] = done
    if done:
        status["applied_class"] = data.get("codec_class")
        status["forced"] = bool(data.get("forced"))
    override = data.get("override")
    if isinstance(override, dict):
        try:
            stale = not _override_fresh(override, src.stat())
        except OSError:
            stale = True
        status["override"] = {
            "codec_class": override.get("codec_class"),
            "set_at": override.get("set_at"),
            "size": override.get("size"),
            "mtime": override.get("mtime"),
            "stale": stale,
        }
    return status


def set_override(
    path: str | os.PathLike[str],
    codec_class: str,
    *,
    roots: Iterable[str | os.PathLike[str]] | None = None,
) -> dict[str, Any]:
    """Pin ``codec_class`` for ``path``.  Records only: never starts an encode (D2).

    The pin applies to the next encode of this source revision (a retry, or
    the first ``after_organize`` if the file has not been encoded yet).
    Raises ``RetryNotFound`` (404) or ``RetryInvalid`` (422).  Probes the source.
    """
    src = _resolve_source(path, roots)
    _refuse_in_place(src)
    _validate_forced(src, codec_class)
    _store_override(src, codec_class)
    logger.info("Encoding hook: codec class pinned: class=%s for %s", codec_class, src.name)
    return override_status(src, roots=roots)


def clear_override(
    path: str | os.PathLike[str],
    *,
    roots: Iterable[str | os.PathLike[str]] | None = None,
) -> bool:
    """Remove the pin for ``path``; the classifier decides again.  Returns whether one existed.

    A marker that held nothing but the pin is deleted.  Raises ``RetryNotFound``
    (404) outside the domain.
    """
    src = _resolve_source(path, roots)
    with _marker_lock:
        data = _read_marker(src)
        if "override" not in data:
            return False
        data.pop("override")
        _store_marker(src, data)
    logger.info("Encoding hook: codec class pin cleared for %s", src.name)
    return True
