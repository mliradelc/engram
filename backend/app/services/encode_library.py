"""Read-only view of every encode Engram has recorded (``GET /api/encoding/library``).

The only durable record of an encode is the sidecar marker the hook writes next
to the source (``<source>.engram-av1.json``, see ``encode_hook.MARKER_SUFFIX``).
This module walks Engram's encode domain for ``*.mkv`` files that carry one,
turns each marker into a row, groups the rows by series / film folder, and
overlays the hook's live registry (in-flight and recently-failed encodes).

Design constraints, each deliberate:

* **Read-only.**  Nothing here writes, moves, deletes or encodes.  Markers are
  parsed directly (not through ``encode_hook._read_marker``'s callers that
  might write) and a marker that is not a JSON object is skipped.
* **Same domain as the hook.**  The roots are whatever the route passes, which
  is ``encode_hook.default_retry_roots(...)``: never a second root list.
* **Cheap.**  One ``os.scandir`` pass per directory; only files that have a
  marker are ``stat``-ed.  The marker scan is cached for
  :data:`LIBRARY_SCAN_TTL_SECONDS` so the UI's refreshes do not re-walk the
  library; the live overlay is applied fresh on every call.
* **Honest states.**  A marker records the source's size/mtime at encode time
  and the output path; it carries no timestamp.  A source that changed since
  is ``stale``, an output that no longer exists is ``output_missing``, and
  "encoded at" is the marker file's own mtime.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.encode_hook import MARKER_SUFFIX

# Seconds a marker scan is reused before the library is walked again.
LIBRARY_SCAN_TTL_SECONDS = 10.0

# Every value ``state`` can take.  ``pending`` is a marker holding only a
# codec-class pin (no done record yet: a pin set before the first encode, or a
# retry that stripped the record and has not finished).
STATES: tuple[str, ...] = (
    "encoded",
    "passthrough",
    "stale",
    "output_missing",
    "pending",
    "encoding",
    "failed",
)

_EPISODE_RE = re.compile(r"(?<![A-Za-z0-9])[Ss](\d{1,3})[Ee](\d{1,4})(?!\d)")


def parse_season_episode(name: str) -> tuple[int | None, int | None]:
    """``"Show - S03E09 - Title.mkv"`` -> ``(3, 9)``; ``(None, None)`` when absent.

    ``S00E01`` (a TV special / extra) is season 0, not "no season".
    """
    m = _EPISODE_RE.search(name)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


@dataclass(frozen=True)
class MarkerRecord:
    """One source ``.mkv`` with a marker, as read from disk (before the live overlay)."""

    source: str
    folder: str
    output: str | None
    codec_class: str | None
    forced: bool
    state: str
    note: str | None
    encoded_at: int | None
    source_bytes: int | None
    output_bytes: int | None


# ---------------------------------------------------------------------------
# Disk scan
# ---------------------------------------------------------------------------


def _iter_marked_sources(roots: Iterable[Path]) -> Iterator[tuple[str, os.DirEntry, os.DirEntry]]:
    """Yield ``(directory, source_entry, marker_entry)`` for every marked ``.mkv``.

    Iterative, one ``os.scandir`` per directory, symlinked directories not
    followed (no loops), and a directory reachable from two overlapping roots
    (``/media/_staging`` and ``/media/_staging/tv``) is walked once.
    """
    seen: set[str] = set()
    # Reversed so the first root is popped (walked) first.
    stack: list[str] = [str(root) for root in roots][::-1]
    while stack:
        directory = stack.pop()
        try:
            real = os.path.realpath(directory)
        except (OSError, ValueError):
            continue
        if real in seen:
            continue
        seen.add(real)
        try:
            with os.scandir(directory) as it:
                entries = list(it)
        except OSError:
            continue
        files: dict[str, os.DirEntry] = {}
        subdirs: list[str] = []
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    subdirs.append(entry.path)
                elif entry.is_file():
                    files[entry.name] = entry
            except OSError:
                continue
        for name, entry in files.items():
            if not name.lower().endswith(".mkv"):
                continue
            marker = files.get(name + MARKER_SUFFIX)
            if marker is not None:
                yield directory, entry, marker
        stack.extend(sorted(subdirs, reverse=True))


def _read_marker_file(path: str) -> dict[str, Any] | None:
    """The marker as a dict, or None when unreadable / not JSON / not an object."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _record(directory: str, src: os.DirEntry, marker: os.DirEntry) -> MarkerRecord | None:
    data = _read_marker_file(marker.path)
    if data is None:
        return None
    try:
        src_st = src.stat()
        marker_st = marker.stat()
    except OSError:
        return None

    raw_output = data.get("av1")
    output = raw_output if isinstance(raw_output, str) and raw_output else None
    raw_class = data.get("codec_class")
    codec_class = raw_class if isinstance(raw_class, str) and raw_class else None
    has_done = "size" in data or "mtime" in data or "av1" in data
    output_bytes: int | None = None
    note: str | None = None

    if not has_done:
        state = "pending"
        override = data.get("override")
        pinned = override.get("codec_class") if isinstance(override, dict) else None
        note = f"Pinned to {pinned}; not encoded yet" if pinned else "Not encoded yet"
    elif data.get("size") != src_st.st_size or data.get("mtime") != int(src_st.st_mtime):
        state = "stale"
        note = "The source changed after this encode; the record no longer applies"
    elif codec_class == "interlaced" and output is None:
        # The old "interlaced, left untouched" skip: no longer counts as done.
        state = "stale"
        note = "Skipped as interlaced by an older version; will be encoded on the next pass"
    elif output is None:
        state = "passthrough"
        codec_class = codec_class or "passthrough"
    else:
        try:
            output_bytes = os.stat(output).st_size
            state = "encoded"
        except OSError:
            state = "output_missing"
            note = "The recorded output no longer exists (pruned, or imported elsewhere)"

    return MarkerRecord(
        source=src.path,
        folder=directory,
        output=output,
        codec_class=codec_class,
        forced=bool(data.get("forced")),
        state=state,
        note=note,
        encoded_at=int(marker_st.st_mtime) if has_done else None,
        source_bytes=src_st.st_size,
        output_bytes=output_bytes,
    )


def scan_markers(roots: Iterable[Path]) -> list[MarkerRecord]:
    """Every marked source under ``roots``.  Blocking: call it off the event loop."""
    # Each real directory is walked once, so a source cannot be listed twice.
    records: list[MarkerRecord] = []
    for directory, src, marker in _iter_marked_sources(roots):
        rec = _record(directory, src, marker)
        if rec is not None:
            records.append(rec)
    return records


# The TTL clock; a module attribute so tests can move it without freezing asyncio's.
_monotonic = time.monotonic

_cache_lock = threading.Lock()
_cache: dict[tuple[str, ...], tuple[float, list[MarkerRecord]]] = {}


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def cached_scan(roots: Iterable[Path], *, now: float | None = None) -> list[MarkerRecord]:
    """:func:`scan_markers`, reused for :data:`LIBRARY_SCAN_TTL_SECONDS` per root set."""
    root_list = [Path(r) for r in roots]
    key = tuple(str(r) for r in root_list)
    t = _monotonic() if now is None else now
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and t - hit[0] < LIBRARY_SCAN_TTL_SECONDS:
            return hit[1]
    records = scan_markers(root_list)
    with _cache_lock:
        _cache[key] = (t, records)
    return records


# ---------------------------------------------------------------------------
# Grouping and live overlay
# ---------------------------------------------------------------------------


def _relative_parts(path: str, root: str | None) -> tuple[str, ...] | None:
    """``path``'s components below ``root``, or None when it is not under it."""
    if not root:
        return None
    candidates = [(path, root)]
    try:
        candidates.append((os.path.realpath(path), os.path.realpath(root)))
    except (OSError, ValueError):
        pass
    for p, r in candidates:
        try:
            return Path(p).relative_to(Path(r)).parts
        except ValueError:
            continue
    return None


def _placement(source: str, tv_root: str | None, movies_root: str | None) -> tuple[str, str, str]:
    """``(kind, title, folder)`` for a source; ``("other", "Ungrouped", "")`` if unplaceable."""
    for kind, root in (("series", tv_root), ("film", movies_root)):
        parts = _relative_parts(source, root)
        # Needs a folder under the root *and* a file inside it.
        if parts is not None and len(parts) >= 2:
            return kind, parts[0], str(Path(root) / parts[0])
    return "other", "Ungrouped", ""


_KIND_ORDER = {"series": 0, "film": 1, "other": 2}


def _norm_path(raw: str) -> str:
    """A path key for matching a live encode against a marker's source.

    ``realpath`` so a bind-mounted or symlinked root still lines up with the path
    the hook reported; falls back to the raw string when the path cannot be
    resolved (it may not exist yet, e.g. a pruned source).
    """
    try:
        return os.path.realpath(raw)
    except (OSError, ValueError):
        return raw


def build_library(
    records: Iterable[MarkerRecord],
    *,
    tv_root: str | None,
    movies_root: str | None,
    status: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """The ``GET /api/encoding/library`` payload.  Pure: no disk access.

    ``status`` is ``encode_hook.encoding_status()``.  A live row wins over the
    marker: an in-flight encode shows ``encoding`` with its progress, and a
    buffered failure shows ``failed`` with its reason.

    Live rows and marker records are matched by absolute **source path**.  Both
    carry one, so the match is exact.  A bare file-name match is used only when
    exactly one live row claims that name *and* one record claims it: two
    different shows can hold an identically named episode, and matching on the
    name alone would report one show's encode against the other's row.  Records
    are also matched on their realpath, so a symlinked or bind-mounted root still
    lines up with the path the hook reported.
    """
    status = status or {}
    active_by_source: dict[str, dict[str, Any]] = {}
    live_by_name: dict[str, list[dict[str, Any]]] = {}
    for row in status.get("active") or []:
        name = str(row.get("file") or "")
        if not name:
            continue
        if row.get("source"):
            active_by_source[_norm_path(str(row["source"]))] = row
        live_by_name.setdefault(name, []).append(row)

    failed: dict[str, dict[str, Any]] = {}
    for row in status.get("failed") or []:
        src = row.get("source")
        if src:
            failed.setdefault(_norm_path(str(src)), row)

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for rec in records:
        name = os.path.basename(rec.source)
        season, episode = parse_season_episode(name)
        entry: dict[str, Any] = {
            "source": rec.source,
            "file": name,
            "folder": rec.folder,
            "season": season,
            "episode": episode,
            "output": rec.output,
            "codec_class": rec.codec_class,
            "forced": rec.forced,
            "state": rec.state,
            "note": rec.note,
            "encoded_at": rec.encoded_at,
            "source_bytes": rec.source_bytes,
            "output_bytes": rec.output_bytes,
            "progress": None,
            "failure": None,
        }
        live = active_by_source.get(_norm_path(rec.source))
        if live is None:
            candidates = live_by_name.get(name) or []
            # Only an unambiguous name match: one live row, and it is not already
            # claimed by a different record.
            if len(candidates) == 1 and candidates[0].get("source") is None:
                live = candidates[0]
        fail = failed.get(_norm_path(rec.source))
        if fail is None:
            try:
                fail = failed.get(os.path.realpath(rec.source))
            except (OSError, ValueError):
                fail = None
        if live is not None:
            entry["state"] = "encoding"
            entry["progress"] = {
                "percent": live.get("percent"),
                "eta": live.get("eta"),
                "speed": live.get("speed"),
                "destination_folder": live.get("destination_folder"),
                "started_at": live.get("started_at"),
            }
            if live.get("codec_class"):
                entry["codec_class"] = live["codec_class"]
                entry["forced"] = bool(live.get("forced"))
        elif fail is not None:
            entry["state"] = "failed"
            entry["failure"] = {"reason": fail.get("reason"), "failed_at": fail.get("failed_at")}

        kind, title, folder = _placement(rec.source, tv_root, movies_root)
        group = groups.get((kind, title))
        if group is None:
            group = {"kind": kind, "title": title, "folder": folder, "entries": []}
            groups[(kind, title)] = group
        group["entries"].append(entry)

    ordered = sorted(
        groups.values(), key=lambda g: (_KIND_ORDER[g["kind"]], g["title"].casefold())
    )
    for group in ordered:
        group["entries"].sort(
            key=lambda e: (
                e["season"] if e["season"] is not None else 10**6,
                e["episode"] if e["episode"] is not None else 10**6,
                e["file"].casefold(),
            )
        )

    totals: dict[str, int] = {"groups": len(ordered), "entries": 0}
    for state in STATES:
        totals[state] = 0
    for group in ordered:
        for e in group["entries"]:
            totals["entries"] += 1
            totals[e["state"]] = totals.get(e["state"], 0) + 1

    return {
        "root_tv": tv_root or None,
        "root_movies": movies_root or None,
        "scan_ttl_seconds": LIBRARY_SCAN_TTL_SECONDS,
        "groups": ordered,
        "totals": totals,
    }


def library_snapshot(
    roots: Iterable[Path],
    *,
    tv_root: str | None,
    movies_root: str | None,
    status: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Cached scan + fresh overlay.  Blocking: call it off the event loop."""
    return build_library(
        cached_scan(roots), tv_root=tv_root, movies_root=movies_root, status=status
    )
