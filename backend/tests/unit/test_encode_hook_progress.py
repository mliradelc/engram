"""Tests for the encoding hook's encode progress: ffmpeg -progress parsing, the
in-memory encoding registry, and publishing over the event loop.

``encode_hook`` is loaded by file path, never through ``app.services`` (same
convention as ``test_encode_hook_concurrency.py``).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

SERVICES = Path(__file__).resolve().parents[2] / "app" / "services"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SERVICES / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


encode_hook = _load("encode_hook")

DURATION = 100.0
PROBE = json.dumps(
    {
        "streams": [{"codec_type": "video", "codec_name": "av1"}],
        "format": {"duration": f"{DURATION:.6f}"},
    }
)

# Recorded shape of ffmpeg 5.1 `-progress pipe:1` output (first block has no speed).
RECORDED = """\
frame=0
fps=0.00
total_size=48
out_time_us=N/A
out_time_ms=N/A
speed=N/A
progress=continue
frame=200
fps=5.40
total_size=2331365
out_time_us=8000000
out_time_ms=8000000
speed=0.216x
progress=continue
frame=1200
fps=5.50
total_size=9331365
out_time_us=50000000
out_time_ms=50000000
speed=0.25x
progress=continue
frame=2400
out_time_us=100000000
speed=0.25x
progress=end
"""


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.delenv("ENGRAM_ENCODE_MAX_CONCURRENT", raising=False)
    monkeypatch.setattr(encode_hook, "_gate_sem", None)
    monkeypatch.setattr(encode_hook, "_gate_limit", None)
    monkeypatch.setattr(encode_hook, "_gate_busy", 0)
    monkeypatch.setattr(encode_hook, "_ENCODING", {})
    monkeypatch.setattr(encode_hook, "_progress_publisher", None)
    monkeypatch.setattr(
        encode_hook, "_FAILED", encode_hook.deque(maxlen=encode_hook.FAILURE_BUFFER_SIZE)
    )
    monkeypatch.setattr(encode_hook, "_RETRY_PENDING", set())


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_recorded_stream_percent_and_eta():
    samples = list(encode_hook.iter_progress(RECORDED.splitlines(True), DURATION))
    assert len(samples) == 4

    first, second, third, last = samples
    # speed=N/A and out_time=N/A: nothing known, and no made-up ETA.
    assert first == {"percent": None, "eta": None, "speed": None, "done": False}

    assert second["percent"] == 8.0
    assert second["eta"] == round((100 - 8) / 0.216)  # 426 s
    assert second["speed"] == "0.216x"
    assert second["done"] is False

    assert third["percent"] == 50.0
    assert third["eta"] == 200  # 50 s of media left at 0.25x

    assert last == {"percent": 100.0, "eta": 0, "speed": "0.25x", "done": True}


def test_speed_na_yields_no_eta_but_keeps_percent():
    lines = ["out_time_us=25000000\n", "speed=N/A\n", "progress=continue\n"]
    (sample,) = encode_hook.iter_progress(lines, DURATION)
    assert sample["percent"] == 25.0
    assert sample["eta"] is None
    assert sample["speed"] is None


def test_missing_speed_yields_no_eta():
    (sample,) = encode_hook.iter_progress(["out_time_us=1000000\n", "progress=continue\n"], 10.0)
    assert sample["percent"] == 10.0
    assert sample["eta"] is None


@pytest.mark.parametrize(
    "out_time, speed, duration, expected",
    [
        (120.0, 1.0, 100.0, (100.0, 0)),  # overshoot clamps; eta never negative
        (0.0, 2.0, 100.0, (0.0, 50)),
        (10.0, 0.5, None, (None, None)),  # unknown duration: no percent, no eta
        (10.0, 0.5, 0.0, (None, None)),
        (None, 1.0, 100.0, (None, None)),
        (10.0, None, 100.0, (10.0, None)),
    ],
)
def test_compute_progress_clamps(out_time, speed, duration, expected):
    assert encode_hook.compute_progress(out_time, speed, duration) == expected


@pytest.mark.parametrize("raw, value", [("0.216x", 0.216), ("1x", 1.0), ("N/A", None),
                                        ("", None), (None, None), ("0x", None), ("fast", None)])
def test_parse_speed(raw, value):
    assert encode_hook._parse_speed(raw) == value


def test_out_time_ms_is_a_fallback_for_out_time_us():
    lines = ["out_time_ms=5000000\n", "speed=1x\n", "progress=continue\n"]
    (sample,) = encode_hook.iter_progress(lines, 10.0)
    assert sample["percent"] == 50.0 and sample["eta"] == 5


def test_garbage_lines_are_ignored():
    lines = ["\n", "not a key value\n", "out_time_us=junk\n", "progress=continue\n"]
    (sample,) = encode_hook.iter_progress(lines, 10.0)
    assert sample["percent"] is None


def test_duration_comes_from_format():
    assert encode_hook._duration_seconds(json.loads(PROBE)) == DURATION
    assert encode_hook._duration_seconds({}) is None
    assert encode_hook._duration_seconds({"format": {"duration": "N/A"}}) is None


# ---------------------------------------------------------------------------
# Registry / snapshot
# ---------------------------------------------------------------------------


class ProgressRun:
    """Fake ``subprocess.run``: ffmpeg writes a progress stream to its stdout fd.

    While "encoding" it records ``encoding_snapshot()`` after each block, and can
    pause on an event so a test can inspect the registry mid-encode.
    """

    def __init__(self, blocks: list[str], returncode: int = 0, raises=None, gate=None):
        self.blocks = blocks
        self.returncode = returncode
        self.raises = raises
        self.gate = gate
        self.seen: list[list[dict]] = []
        self.encode_kwargs: dict = {}

    def __call__(self, cmd, **kw):
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, PROBE, "")
        self.cmd, self.encode_kwargs = list(cmd), kw
        fd = kw["stdout"]
        assert isinstance(fd, int), "progress must go to a pipe, not be captured"
        for block in self.blocks:
            os.write(fd, block.encode())
            # Let the reader thread catch up before sampling the registry (well
            # inside the 2 s publish throttle, which a test relies on).
            time.sleep(0.1)
            self.seen.append(encode_hook.encoding_snapshot())
        if self.gate is not None:
            self.gate.wait(5)
        if self.raises is not None:
            raise self.raises
        if self.returncode == 0:
            Path(cmd[-1]).write_bytes(b"x")
        return subprocess.CompletedProcess(cmd, self.returncode, None, "ffmpeg: boom")


def _blocks() -> list[str]:
    # Split the recorded stream into its progress blocks.
    out, cur = [], []
    for line in RECORDED.splitlines(True):
        cur.append(line)
        if line.startswith("progress="):
            out.append("".join(cur))
            cur = []
    return out


def _src(tmp_path: Path) -> Path:
    p = tmp_path / "Show - S01E01.mkv"
    p.write_bytes(b"src")
    return p


def test_command_reports_progress_on_stdout(tmp_path, monkeypatch):
    fake = ProgressRun(_blocks())
    monkeypatch.setattr(subprocess, "run", fake)
    encode_hook.transcode_file(_src(tmp_path), tmp_path / "out")
    i = fake.cmd.index("-progress")
    assert fake.cmd[i + 1] == "pipe:1" and "-nostats" in fake.cmd
    assert fake.cmd[-1].endswith(".partial.mkv")  # output stays last
    # stderr is still captured for the failure log line; stdin is detached.
    assert fake.encode_kwargs["stderr"] == subprocess.PIPE
    assert fake.encode_kwargs["stdin"] == subprocess.DEVNULL
    assert fake.encode_kwargs["timeout"] == encode_hook.ENCODE_TIMEOUT_SECONDS


def test_snapshot_empty_before_populated_during_empty_after_success(tmp_path, monkeypatch):
    assert encode_hook.encoding_snapshot() == []
    fake = ProgressRun(_blocks())
    monkeypatch.setattr(subprocess, "run", fake)
    src = _src(tmp_path)
    out = encode_hook.transcode_file(src, tmp_path / "out")
    assert out == tmp_path / "out" / src.name

    during = fake.seen[2]
    assert len(during) == 1
    row = during[0]
    assert row["file"] == src.name
    assert row["destination_folder"] == str(tmp_path / "out")
    assert row["percent"] == 50.0
    assert row["eta"] == 200
    assert row["speed"] == "0.25x"
    assert row["codec"] == "av1"
    assert isinstance(row["started_at"], float)
    assert encode_hook.encoding_snapshot() == []


@pytest.mark.parametrize(
    "kwargs",
    [{"returncode": 1}, {"raises": subprocess.TimeoutExpired("ffmpeg", 1)}],
    ids=["nonzero-returncode", "timeout"],
)
def test_snapshot_empty_after_failed_encode(tmp_path, monkeypatch, kwargs):
    fake = ProgressRun(_blocks()[:2], **kwargs)
    monkeypatch.setattr(subprocess, "run", fake)
    assert encode_hook.transcode_file(_src(tmp_path), tmp_path / "out") is None
    assert fake.seen[-1] and fake.seen[-1][0]["percent"] == 8.0
    assert encode_hook.encoding_snapshot() == []


def test_failed_encode_still_logs_stderr(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(subprocess, "run", ProgressRun([], returncode=1))
    with caplog.at_level("ERROR", logger=encode_hook.logger.name):
        encode_hook.transcode_file(_src(tmp_path), tmp_path / "out")
    assert any("ffmpeg: boom" in r.getMessage() for r in caplog.records)


def test_snapshot_returns_a_copy_sorted_by_start(monkeypatch):
    encode_hook._ENCODING["/b"] = {"file": "b", "started_at": 2.0, "percent": 1.0}
    encode_hook._ENCODING["/a"] = {"file": "a", "started_at": 1.0, "percent": 1.0}
    snap = encode_hook.encoding_snapshot()
    assert [r["file"] for r in snap] == ["a", "b"]
    snap[0]["percent"] = 99.0
    assert encode_hook._ENCODING["/a"]["percent"] == 1.0


def test_queued_file_is_not_listed(tmp_path, monkeypatch):
    """Limit 1: the second file waits for a permit and must not show as encoding."""
    monkeypatch.setenv("ENGRAM_ENCODE_MAX_CONCURRENT", "1")
    gate = threading.Event()
    fake = ProgressRun(_blocks()[:2], gate=gate)
    monkeypatch.setattr(subprocess, "run", fake)
    a = tmp_path / "a.mkv"
    b = tmp_path / "b.mkv"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    threads = [
        threading.Thread(target=encode_hook.transcode_file, args=(p, tmp_path / "out"))
        for p in (a, b)
    ]
    for t in threads:
        t.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and encode_hook._gate_busy == 0:
        time.sleep(0.01)
    time.sleep(0.2)  # let the second thread reach the semaphore
    assert len(encode_hook.encoding_snapshot()) == 1
    gate.set()
    for t in threads:
        t.join(timeout=10)
    assert encode_hook.encoding_snapshot() == []


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


def test_no_loop_skips_publishing_and_still_encodes(tmp_path, monkeypatch):
    called = []

    async def publisher(**kw):
        called.append(kw)

    encode_hook.set_progress_publisher(publisher)
    monkeypatch.setattr(subprocess, "run", ProgressRun(_blocks()))
    assert encode_hook.transcode_file(_src(tmp_path), tmp_path / "out") is not None
    assert called == []


def test_progress_published_on_loop_throttled_and_terminated(tmp_path, monkeypatch):
    received: list[dict] = []

    async def publisher(**kw):
        received.append(kw)

    encode_hook.set_progress_publisher(publisher)
    loop = asyncio.new_event_loop()
    runner = threading.Thread(target=loop.run_forever, daemon=True)
    runner.start()
    try:
        monkeypatch.setattr(subprocess, "run", ProgressRun(_blocks()))
        src = _src(tmp_path)
        assert encode_hook.transcode_file(src, tmp_path / "out", loop=loop) is not None
        # Let scheduled coroutines run.
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result(2)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        runner.join(2)
        loop.close()

    # Registration sample + first progress sample (the rest fall inside the 2 s
    # throttle window) + the terminal "done" message.
    assert received[0]["percent"] == 0.0 and received[0]["done"] is False
    assert received[-1]["done"] is True and received[-1]["percent"] == 100.0
    assert len(received) == 3
    for msg in received:
        assert msg["file"] == src.name
        assert msg["destination_folder"] == str(tmp_path / "out")
        assert msg["codec"] == "av1"
        assert set(msg) == {"file", "destination_folder", "percent", "eta", "speed",
                            "codec", "done"}


def test_publisher_failure_never_breaks_the_encode(tmp_path, monkeypatch):
    def publisher(**kw):
        raise RuntimeError("socket gone")

    encode_hook.set_progress_publisher(publisher)
    loop = asyncio.new_event_loop()
    try:
        monkeypatch.setattr(subprocess, "run", ProgressRun(_blocks()))
        assert encode_hook.transcode_file(_src(tmp_path), tmp_path / "out", loop=loop) is not None
    finally:
        loop.close()
    assert encode_hook.encoding_snapshot() == []


@pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg/ffprobe not installed"
)
def test_real_ffmpeg_progress_stream_is_parsed(tmp_path, monkeypatch):
    """A real encode emits -progress on the pipe and the reader turns it into samples."""
    src = tmp_path / "real.mkv"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=24",
         "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)],
        check=True,
    )
    monkeypatch.setenv("ENGRAM_ENCODE_PRESET", "12")
    monkeypatch.setenv("ENGRAM_ENCODE_THREADS", "2")
    received: list[dict] = []

    async def publisher(**kw):
        received.append(kw)

    encode_hook.set_progress_publisher(publisher)
    monkeypatch.setattr(encode_hook, "PROGRESS_PUBLISH_INTERVAL", 0.0)  # see every sample
    loop = asyncio.new_event_loop()
    runner = threading.Thread(target=loop.run_forever, daemon=True)
    runner.start()
    try:
        out = encode_hook.transcode_file(src, tmp_path / "out", loop=loop)
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result(2)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        runner.join(2)
        loop.close()
    if out is None:
        pytest.skip("this ffmpeg build cannot encode libsvtav1")
    progress = [m for m in received if not m["done"]]
    # Registration (0%) plus at least the progress=end sample (100%).
    assert any(m["percent"] == 100.0 for m in progress)
    assert received[-1]["done"] is True
    assert encode_hook.encoding_snapshot() == []


def test_after_organize_forwards_loop(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGRAM_ENCODE_HOOK", "1")
    monkeypatch.setenv("ENGRAM_CODEC_CLASSIFIER", "0")
    monkeypatch.setenv("ENGRAM_ENCODE_STAGING", str(tmp_path / "out"))
    # The fixture file stands in for a rip in Engram's staging root.
    monkeypatch.setattr(encode_hook, "_DOMAIN_ROOTS", (str(tmp_path),))
    seen = {}

    def fake_transcode(src, staging, plan=None, *, loop=None):
        seen["loop"] = loop
        return None

    monkeypatch.setattr(encode_hook, "transcode_file", fake_transcode)
    sentinel = object()
    encode_hook.after_organize(str(_src(tmp_path)), loop=sentinel)
    assert seen["loop"] is sentinel


def test_after_organize_refuses_a_library_path(tmp_path, monkeypatch, caplog):
    """A file in the library (``/media/tv``) is never Engram's to encode.

    Regression for the hook re-encoding files it never produced: the domain is
    the staging root plus ``ENGRAM_ENCODE_STAGING``, and a library path is refused
    before classification, encoding, or any marker write.
    """
    media = tmp_path / "media"
    (media / "_staging").mkdir(parents=True)
    show = media / "tv" / "Show" / "Season 01"
    show.mkdir(parents=True)
    src = show / "Show - S01E01.mkv"
    src.write_bytes(b"legacy rip")
    monkeypatch.setenv("ENGRAM_ENCODE_HOOK", "1")
    monkeypatch.setenv("ENGRAM_CODEC_CLASSIFIER", "0")
    monkeypatch.setenv("ENGRAM_ENCODE_STAGING", str(tmp_path / "out"))
    monkeypatch.setattr(encode_hook, "_DOMAIN_ROOTS", (str(media / "_staging"),))
    calls = []
    monkeypatch.setattr(encode_hook, "transcode_file", lambda *a, **k: calls.append(a))

    with caplog.at_level("WARNING", logger=encode_hook.logger.name):
        encode_hook.after_organize(str(src))

    assert calls == []
    assert not encode_hook._marker_path(src).exists()
    assert any(
        "refusing to encode" in r.getMessage() and "outside Engram's domain" in r.getMessage()
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# Failure ring buffer
# ---------------------------------------------------------------------------


def test_failed_encode_lands_in_the_failure_buffer(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", ProgressRun([], returncode=1))
    src = _src(tmp_path)
    assert encode_hook.transcode_file(src, tmp_path / "out") is None

    status = encode_hook.encoding_status()
    assert status["active"] == []
    assert len(status["failed"]) == 1
    row = status["failed"][0]
    assert row["file"] == src.name
    assert row["source"] == str(src)
    assert row["destination_folder"] == str(tmp_path / "out")
    assert "ffmpeg exited 1" in row["reason"] and "boom" in row["reason"]
    assert isinstance(row["failed_at"], float)


def test_timeout_reason_is_recorded(tmp_path, monkeypatch):
    fake = ProgressRun([], raises=subprocess.TimeoutExpired("ffmpeg", 1))
    monkeypatch.setattr(subprocess, "run", fake)
    encode_hook.transcode_file(_src(tmp_path), tmp_path / "out")
    assert encode_hook.failures_snapshot()[0]["reason"] == "Encode timed out"


def test_failure_buffer_caps_at_twenty_newest_first(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", ProgressRun([], returncode=1))
    for i in range(25):
        p = tmp_path / f"ep{i:02d}.mkv"
        p.write_bytes(b"x")
        encode_hook.transcode_file(p, tmp_path / "out")
    failed = encode_hook.failures_snapshot()
    assert len(failed) == encode_hook.FAILURE_BUFFER_SIZE == 20
    assert failed[0]["file"] == "ep24.mkv"
    assert failed[-1]["file"] == "ep05.mkv"


def test_failure_buffer_keeps_one_row_per_source(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", ProgressRun([], returncode=1))
    src = _src(tmp_path)
    encode_hook.transcode_file(src, tmp_path / "out")
    encode_hook.transcode_file(src, tmp_path / "out")
    assert len(encode_hook.failures_snapshot()) == 1


def test_successful_encode_clears_its_failure(tmp_path, monkeypatch):
    src = _src(tmp_path)
    monkeypatch.setattr(subprocess, "run", ProgressRun([], returncode=1))
    encode_hook.transcode_file(src, tmp_path / "out")
    assert encode_hook.failures_snapshot()
    monkeypatch.setattr(subprocess, "run", ProgressRun([]))
    assert encode_hook.transcode_file(src, tmp_path / "out") is not None
    assert encode_hook.failures_snapshot() == []


def test_failure_is_recorded_before_the_terminal_publish(tmp_path, monkeypatch):
    """A client re-fetching GET /api/encoding on ``done`` must see the failed row."""
    seen_on_done = []

    async def publisher(**kw):
        if kw["done"]:
            seen_on_done.append(encode_hook.failures_snapshot())

    encode_hook.set_progress_publisher(publisher)
    monkeypatch.setattr(subprocess, "run", ProgressRun([], returncode=1))
    loop = asyncio.new_event_loop()
    runner = threading.Thread(target=loop.run_forever, daemon=True)
    runner.start()
    try:
        encode_hook.transcode_file(_src(tmp_path), tmp_path / "out", loop=loop)
        time.sleep(0.2)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        runner.join(timeout=5)
        loop.close()
    assert seen_on_done and len(seen_on_done[0]) == 1


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


@pytest.fixture
def hook_env(tmp_path, monkeypatch):
    """Hook on, and Engram's domain pointed at a fixture staging root.

    Returns that root.  The retry worker runs ``after_organize``, which refuses
    anything outside ``_DOMAIN_ROOTS``, so the fixture must be inside the domain
    for a retry to reach the encoder at all.
    """
    monkeypatch.setenv("ENGRAM_ENCODE_HOOK", "1")
    monkeypatch.setenv("ENGRAM_CODEC_CLASSIFIER", "0")
    monkeypatch.setenv("ENGRAM_ENCODE_STAGING", str(tmp_path / "out"))
    staging_root = tmp_path / "_staging"
    staging_root.mkdir()
    monkeypatch.setattr(encode_hook, "_DOMAIN_ROOTS", (str(staging_root),))
    return staging_root


def _staged_src(root: Path, name: str = "Show - S01E01.mkv") -> Path:
    p = root / name
    p.write_bytes(b"src")
    return p


def _write_done_marker(src: Path) -> Path:
    st = src.stat()
    marker = encode_hook._marker_path(src)
    marker.write_text(json.dumps({"size": st.st_size, "mtime": int(st.st_mtime), "av1": "x"}))
    assert encode_hook._already_done(src)
    return marker


def _wait_for(pred, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def test_retry_deletes_marker_and_starts_an_encode(hook_env, monkeypatch):
    src = _staged_src(hook_env)
    marker = _write_done_marker(src)
    calls = []
    done = threading.Event()

    def fake_transcode(s, staging, plan=None, *, loop=None):
        calls.append({"src": s, "marker_present": marker.exists(), "loop": loop})
        done.set()
        return None

    monkeypatch.setattr(encode_hook, "transcode_file", fake_transcode)
    sentinel = object()
    assert encode_hook.retry_encode(str(src), loop=sentinel, roots=[hook_env]) is None
    assert done.wait(5), "retry never reached the encoder"
    assert not marker.exists()
    assert calls == [{"src": src.resolve(), "marker_present": False, "loop": sentinel}]
    assert _wait_for(lambda: not encode_hook._RETRY_PENDING)


def test_retry_goes_through_the_shared_gate_and_registry(hook_env, monkeypatch):
    """A retried file takes an encode permit and shows in the active registry."""
    monkeypatch.setenv("ENGRAM_ENCODE_MAX_CONCURRENT", "1")
    src = _staged_src(hook_env)
    _write_done_marker(src)
    gate = threading.Event()
    monkeypatch.setattr(subprocess, "run", ProgressRun(_blocks()[:2], gate=gate))

    encode_hook.retry_encode(src, roots=[hook_env])
    assert _wait_for(lambda: encode_hook._gate_busy == 1)
    assert [r["file"] for r in encode_hook.encoding_snapshot()] == [src.name]
    gate.set()
    assert _wait_for(lambda: not encode_hook._RETRY_PENDING)
    assert encode_hook.encoding_snapshot() == []
    assert encode_hook._gate_busy == 0
    assert encode_hook._already_done(src)  # a fresh marker for the new revision


def test_retry_moves_the_row_out_of_failed(hook_env, monkeypatch):
    src = _staged_src(hook_env)
    encode_hook._record_failure(src.resolve(), encode_hook._staging_dir() / src.name, "boom")
    assert len(encode_hook.failures_snapshot()) == 1
    started = threading.Event()
    release = threading.Event()

    def fake_transcode(s, staging, plan=None, *, loop=None):
        started.set()
        release.wait(5)
        return None

    monkeypatch.setattr(encode_hook, "transcode_file", fake_transcode)
    encode_hook.retry_encode(src, roots=[hook_env])
    assert started.wait(5)
    assert encode_hook.failures_snapshot() == []
    release.set()
    assert _wait_for(lambda: not encode_hook._RETRY_PENDING)


@pytest.mark.parametrize("name", ["missing.mkv", "notes.txt"])
def test_retry_refuses_missing_or_non_mkv(hook_env, name):
    path = hook_env / name
    if name.endswith(".txt"):
        path.write_text("x")
    with pytest.raises(encode_hook.RetryNotFound) as exc:
        encode_hook.retry_encode(path, roots=[hook_env])
    assert exc.value.status == 404
    assert not encode_hook._RETRY_PENDING


def test_retry_refuses_a_directory(hook_env):
    d = hook_env / "Season 01.mkv"
    d.mkdir()
    with pytest.raises(encode_hook.RetryNotFound):
        encode_hook.retry_encode(d, roots=[hook_env])


def test_retry_refuses_paths_outside_the_roots(hook_env, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    src = _staged_src(outside)
    with pytest.raises(encode_hook.RetryNotFound):
        encode_hook.retry_encode(src, roots=[hook_env])
    # ``..`` cannot climb out of a root either.
    with pytest.raises(encode_hook.RetryNotFound):
        encode_hook.retry_encode(hook_env / ".." / "elsewhere" / src.name, roots=[hook_env])


def test_retry_refuses_a_sibling_prefix_root(hook_env, tmp_path):
    sibling = tmp_path / "_staging-old"
    sibling.mkdir()
    src = _staged_src(sibling)
    with pytest.raises(encode_hook.RetryNotFound):
        encode_hook.retry_encode(src, roots=[hook_env])


def test_retry_without_roots_fails_closed(hook_env, tmp_path, monkeypatch):
    """No roots: only Engram's domain is allowed, never a file outside it."""
    elsewhere = tmp_path / "media" / "_staging"
    elsewhere.mkdir(parents=True)
    monkeypatch.setattr(encode_hook, "_DOMAIN_ROOTS", (str(elsewhere),))
    monkeypatch.setenv("ENGRAM_ENCODE_STAGING", str(tmp_path / "staging" / "av1-out"))
    src = _staged_src(hook_env)
    with pytest.raises(encode_hook.RetryNotFound):
        encode_hook.retry_encode(src)
    assert not encode_hook._RETRY_PENDING


def test_retry_refused_while_the_file_is_encoding(hook_env, monkeypatch):
    src = _staged_src(hook_env)
    marker = _write_done_marker(src)
    key = str(encode_hook._staging_dir() / src.name)
    encode_hook._ENCODING[key] = {"file": src.name, "started_at": 1.0}
    called = []
    monkeypatch.setattr(encode_hook, "transcode_file", lambda *a, **k: called.append(a))
    with pytest.raises(encode_hook.RetryConflict) as exc:
        encode_hook.retry_encode(src, roots=[hook_env])
    assert exc.value.status == 409
    assert marker.exists(), "a refused retry must not touch the marker"
    assert called == []


def test_second_retry_refused_while_the_first_is_queued(hook_env, monkeypatch):
    src = _staged_src(hook_env)
    release = threading.Event()
    started = threading.Event()
    calls = []

    def fake_transcode(s, staging, plan=None, *, loop=None):
        calls.append(s)
        started.set()
        release.wait(5)
        return None

    monkeypatch.setattr(encode_hook, "transcode_file", fake_transcode)
    encode_hook.retry_encode(src, roots=[hook_env])
    assert started.wait(5)
    with pytest.raises(encode_hook.RetryConflict):
        encode_hook.retry_encode(src, roots=[hook_env])
    release.set()
    assert _wait_for(lambda: not encode_hook._RETRY_PENDING)
    assert len(calls) == 1
    # Once finished, the file can be retried again.
    encode_hook.retry_encode(src, roots=[hook_env])
    assert _wait_for(lambda: len(calls) == 2)
    assert _wait_for(lambda: not encode_hook._RETRY_PENDING)


def test_retry_refused_when_the_hook_is_disabled(hook_env, monkeypatch):
    monkeypatch.setenv("ENGRAM_ENCODE_HOOK", "0")
    src = _staged_src(hook_env)
    with pytest.raises(encode_hook.RetryConflict):
        encode_hook.retry_encode(src, roots=[hook_env])
    assert not encode_hook._RETRY_PENDING


def test_retry_worker_never_raises(hook_env, monkeypatch):
    src = _staged_src(hook_env)

    def boom(*a, **k):
        raise RuntimeError("encoder exploded")

    monkeypatch.setattr(encode_hook, "after_organize", boom)
    encode_hook.retry_encode(src, roots=[hook_env])
    assert _wait_for(lambda: not encode_hook._RETRY_PENDING)


def test_default_retry_roots_skip_empty_and_honour_only_in_domain_paths(monkeypatch, tmp_path):
    """Empty/unset library paths are dropped; a configured one counts only inside the domain."""
    domain = tmp_path / "media" / "_staging"
    inside = domain / "tv"
    outside = tmp_path / "media" / "tv"
    staging = tmp_path / "staging" / "av1-out"
    for d in (inside, outside, staging):
        d.mkdir(parents=True)
    monkeypatch.setattr(encode_hook, "_DOMAIN_ROOTS", (str(domain),))
    monkeypatch.setenv("ENGRAM_ENCODE_STAGING", str(staging))

    roots = encode_hook.default_retry_roots([str(inside), str(outside), "", None])

    assert roots == [domain, inside, staging]
    assert outside not in roots


def test_domain_roots_are_staging_only_never_the_library(monkeypatch, tmp_path):
    """The domain is the staging root plus ENGRAM_ENCODE_STAGING; /media/tv and
    /media/movies are the library and stay out even when configured as library paths.
    """
    assert encode_hook._DOMAIN_ROOTS == ("/media/_staging",)
    assert not {"/media/tv", "/media/movies"} & set(encode_hook._DOMAIN_ROOTS)

    media = tmp_path / "media"
    domain, tv, movies = media / "_staging", media / "tv", media / "movies"
    staging = tmp_path / "staging" / "av1-out"
    for d in (domain, tv, movies, staging):
        d.mkdir(parents=True)
    monkeypatch.setattr(encode_hook, "_DOMAIN_ROOTS", (str(domain),))
    monkeypatch.setenv("ENGRAM_ENCODE_STAGING", str(staging))

    roots = encode_hook.domain_roots([str(tv), str(movies)])

    assert roots == [domain, staging]
    assert encode_hook.in_domain(domain / "Show - S01E01.mkv", [str(tv), str(movies)])
    assert encode_hook.in_domain(staging / "Show - S01E01.mkv")
    assert not encode_hook.in_domain(tv / "Show - S01E01.mkv", [str(tv), str(movies)])
    assert not encode_hook.in_domain(movies / "Film (2010).mkv", [str(tv), str(movies)])
