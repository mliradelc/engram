"""Tests for the AV1 hook's encode progress: ffmpeg -progress parsing, the
in-memory encoding registry, and publishing over the event loop.

``av1_hook`` is loaded by file path, never through ``app.services`` (same
convention as ``test_av1_hook_concurrency.py``).
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


av1_hook = _load("av1_hook")

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
    monkeypatch.delenv("ENGRAM_AV1_MAX_CONCURRENT", raising=False)
    monkeypatch.setattr(av1_hook, "_gate_sem", None)
    monkeypatch.setattr(av1_hook, "_gate_limit", None)
    monkeypatch.setattr(av1_hook, "_gate_busy", 0)
    monkeypatch.setattr(av1_hook, "_ENCODING", {})
    monkeypatch.setattr(av1_hook, "_progress_publisher", None)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_recorded_stream_percent_and_eta():
    samples = list(av1_hook.iter_progress(RECORDED.splitlines(True), DURATION))
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
    (sample,) = av1_hook.iter_progress(lines, DURATION)
    assert sample["percent"] == 25.0
    assert sample["eta"] is None
    assert sample["speed"] is None


def test_missing_speed_yields_no_eta():
    (sample,) = av1_hook.iter_progress(["out_time_us=1000000\n", "progress=continue\n"], 10.0)
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
    assert av1_hook.compute_progress(out_time, speed, duration) == expected


@pytest.mark.parametrize("raw, value", [("0.216x", 0.216), ("1x", 1.0), ("N/A", None),
                                        ("", None), (None, None), ("0x", None), ("fast", None)])
def test_parse_speed(raw, value):
    assert av1_hook._parse_speed(raw) == value


def test_out_time_ms_is_a_fallback_for_out_time_us():
    lines = ["out_time_ms=5000000\n", "speed=1x\n", "progress=continue\n"]
    (sample,) = av1_hook.iter_progress(lines, 10.0)
    assert sample["percent"] == 50.0 and sample["eta"] == 5


def test_garbage_lines_are_ignored():
    lines = ["\n", "not a key value\n", "out_time_us=junk\n", "progress=continue\n"]
    (sample,) = av1_hook.iter_progress(lines, 10.0)
    assert sample["percent"] is None


def test_duration_comes_from_format():
    assert av1_hook._duration_seconds(json.loads(PROBE)) == DURATION
    assert av1_hook._duration_seconds({}) is None
    assert av1_hook._duration_seconds({"format": {"duration": "N/A"}}) is None


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
            self.seen.append(av1_hook.encoding_snapshot())
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
    av1_hook.transcode_to_av1(_src(tmp_path), tmp_path / "out")
    i = fake.cmd.index("-progress")
    assert fake.cmd[i + 1] == "pipe:1" and "-nostats" in fake.cmd
    assert fake.cmd[-1].endswith(".partial.mkv")  # output stays last
    # stderr is still captured for the failure log line; stdin is detached.
    assert fake.encode_kwargs["stderr"] == subprocess.PIPE
    assert fake.encode_kwargs["stdin"] == subprocess.DEVNULL
    assert fake.encode_kwargs["timeout"] == av1_hook.ENCODE_TIMEOUT_SECONDS


def test_snapshot_empty_before_populated_during_empty_after_success(tmp_path, monkeypatch):
    assert av1_hook.encoding_snapshot() == []
    fake = ProgressRun(_blocks())
    monkeypatch.setattr(subprocess, "run", fake)
    src = _src(tmp_path)
    out = av1_hook.transcode_to_av1(src, tmp_path / "out")
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
    assert av1_hook.encoding_snapshot() == []


@pytest.mark.parametrize(
    "kwargs",
    [{"returncode": 1}, {"raises": subprocess.TimeoutExpired("ffmpeg", 1)}],
    ids=["nonzero-returncode", "timeout"],
)
def test_snapshot_empty_after_failed_encode(tmp_path, monkeypatch, kwargs):
    fake = ProgressRun(_blocks()[:2], **kwargs)
    monkeypatch.setattr(subprocess, "run", fake)
    assert av1_hook.transcode_to_av1(_src(tmp_path), tmp_path / "out") is None
    assert fake.seen[-1] and fake.seen[-1][0]["percent"] == 8.0
    assert av1_hook.encoding_snapshot() == []


def test_failed_encode_still_logs_stderr(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(subprocess, "run", ProgressRun([], returncode=1))
    with caplog.at_level("ERROR", logger=av1_hook.logger.name):
        av1_hook.transcode_to_av1(_src(tmp_path), tmp_path / "out")
    assert any("ffmpeg: boom" in r.getMessage() for r in caplog.records)


def test_snapshot_returns_a_copy_sorted_by_start(monkeypatch):
    av1_hook._ENCODING["/b"] = {"file": "b", "started_at": 2.0, "percent": 1.0}
    av1_hook._ENCODING["/a"] = {"file": "a", "started_at": 1.0, "percent": 1.0}
    snap = av1_hook.encoding_snapshot()
    assert [r["file"] for r in snap] == ["a", "b"]
    snap[0]["percent"] = 99.0
    assert av1_hook._ENCODING["/a"]["percent"] == 1.0


def test_queued_file_is_not_listed(tmp_path, monkeypatch):
    """Limit 1: the second file waits for a permit and must not show as encoding."""
    monkeypatch.setenv("ENGRAM_AV1_MAX_CONCURRENT", "1")
    gate = threading.Event()
    fake = ProgressRun(_blocks()[:2], gate=gate)
    monkeypatch.setattr(subprocess, "run", fake)
    a = tmp_path / "a.mkv"
    b = tmp_path / "b.mkv"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    threads = [
        threading.Thread(target=av1_hook.transcode_to_av1, args=(p, tmp_path / "out"))
        for p in (a, b)
    ]
    for t in threads:
        t.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and av1_hook._gate_busy == 0:
        time.sleep(0.01)
    time.sleep(0.2)  # let the second thread reach the semaphore
    assert len(av1_hook.encoding_snapshot()) == 1
    gate.set()
    for t in threads:
        t.join(timeout=10)
    assert av1_hook.encoding_snapshot() == []


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


def test_no_loop_skips_publishing_and_still_encodes(tmp_path, monkeypatch):
    called = []

    async def publisher(**kw):
        called.append(kw)

    av1_hook.set_progress_publisher(publisher)
    monkeypatch.setattr(subprocess, "run", ProgressRun(_blocks()))
    assert av1_hook.transcode_to_av1(_src(tmp_path), tmp_path / "out") is not None
    assert called == []


def test_progress_published_on_loop_throttled_and_terminated(tmp_path, monkeypatch):
    received: list[dict] = []

    async def publisher(**kw):
        received.append(kw)

    av1_hook.set_progress_publisher(publisher)
    loop = asyncio.new_event_loop()
    runner = threading.Thread(target=loop.run_forever, daemon=True)
    runner.start()
    try:
        monkeypatch.setattr(subprocess, "run", ProgressRun(_blocks()))
        src = _src(tmp_path)
        assert av1_hook.transcode_to_av1(src, tmp_path / "out", loop=loop) is not None
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

    av1_hook.set_progress_publisher(publisher)
    loop = asyncio.new_event_loop()
    try:
        monkeypatch.setattr(subprocess, "run", ProgressRun(_blocks()))
        assert av1_hook.transcode_to_av1(_src(tmp_path), tmp_path / "out", loop=loop) is not None
    finally:
        loop.close()
    assert av1_hook.encoding_snapshot() == []


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
    monkeypatch.setenv("ENGRAM_AV1_PRESET", "12")
    monkeypatch.setenv("ENGRAM_AV1_THREADS", "2")
    received: list[dict] = []

    async def publisher(**kw):
        received.append(kw)

    av1_hook.set_progress_publisher(publisher)
    monkeypatch.setattr(av1_hook, "PROGRESS_PUBLISH_INTERVAL", 0.0)  # see every sample
    loop = asyncio.new_event_loop()
    runner = threading.Thread(target=loop.run_forever, daemon=True)
    runner.start()
    try:
        out = av1_hook.transcode_to_av1(src, tmp_path / "out", loop=loop)
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
    assert av1_hook.encoding_snapshot() == []


def test_after_organize_forwards_loop(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGRAM_AV1_HOOK", "1")
    monkeypatch.setenv("ENGRAM_CODEC_CLASSIFIER", "0")
    monkeypatch.setenv("ENGRAM_AV1_STAGING", str(tmp_path / "out"))
    seen = {}

    def fake_transcode(src, staging, plan=None, *, loop=None):
        seen["loop"] = loop
        return None

    monkeypatch.setattr(av1_hook, "transcode_to_av1", fake_transcode)
    sentinel = object()
    av1_hook.after_organize(str(_src(tmp_path)), loop=sentinel)
    assert seen["loop"] is sentinel
