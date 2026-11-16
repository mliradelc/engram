"""Tests for the AV1 hook's process-wide encode concurrency gate.

``av1_hook`` is loaded by file path, never through ``app.services`` (same
convention as ``test_codec_classifier.py``): that package's ``__init__`` imports
the whole ASR stack.
"""

from __future__ import annotations

import importlib.util
import json
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

PROBE_OK = json.dumps({"streams": [{"codec_type": "video", "codec_name": "av1"}]})


class FakeRun:
    """Stand-in for ``subprocess.run`` recording ffmpeg enter/exit times."""

    def __init__(self, duration: float = 0.15, returncode: int = 0, raises=None):
        self.duration = duration
        self.returncode = returncode
        self.raises = raises
        self.spans: list[tuple[float, float]] = []
        self.timeouts: list[float] = []
        self._lock = threading.Lock()

    def __call__(self, cmd, **kw):
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, PROBE_OK, "")
        start = time.monotonic()
        self.timeouts.append(kw.get("timeout"))
        time.sleep(self.duration)
        if self.raises is not None:
            raise self.raises
        if self.returncode == 0:
            Path(cmd[-1]).write_bytes(b"x")
        end = time.monotonic()
        with self._lock:
            self.spans.append((start, end))
        return subprocess.CompletedProcess(cmd, self.returncode, "", "boom")


@pytest.fixture(autouse=True)
def _reset_gate(monkeypatch):
    monkeypatch.delenv("ENGRAM_AV1_MAX_CONCURRENT", raising=False)
    monkeypatch.setattr(av1_hook, "_gate_sem", None)
    monkeypatch.setattr(av1_hook, "_gate_limit", None)
    monkeypatch.setattr(av1_hook, "_gate_busy", 0)


def _sources(tmp_path: Path, n: int) -> list[Path]:
    out = []
    for i in range(n):
        p = tmp_path / f"ep{i}.mkv"
        p.write_bytes(b"src")
        out.append(p)
    return out


def _run_parallel(tmp_path: Path, n: int) -> list:
    srcs = _sources(tmp_path, n)
    staging = tmp_path / "out"
    results: list = [None] * n

    def work(i: int) -> None:
        results[i] = av1_hook.transcode_to_av1(srcs[i], staging)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results


def _overlaps(spans: list[tuple[float, float]]) -> bool:
    spans = sorted(spans)
    return any(b[0] < a[1] for a, b in zip(spans, spans[1:], strict=False))


def test_limit_one_never_overlaps(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGRAM_AV1_MAX_CONCURRENT", "1")
    fake = FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)
    results = _run_parallel(tmp_path, 3)
    assert all(r is not None for r in results)
    assert len(fake.spans) == 3
    assert not _overlaps(fake.spans)


def test_limit_zero_is_unlimited(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGRAM_AV1_MAX_CONCURRENT", "0")
    fake = FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)
    _run_parallel(tmp_path, 3)
    assert _overlaps(fake.spans)


@pytest.mark.parametrize("value", ["abc", "-5", ""])
def test_invalid_value_is_unlimited_and_does_not_raise(tmp_path, monkeypatch, value):
    monkeypatch.setenv("ENGRAM_AV1_MAX_CONCURRENT", value)
    assert av1_hook._max_concurrent() == 0
    fake = FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)
    results = _run_parallel(tmp_path, 2)
    assert all(r is not None for r in results)
    assert _overlaps(fake.spans)


def test_limit_two_allows_two_at_once(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGRAM_AV1_MAX_CONCURRENT", "2")
    fake = FakeRun(duration=0.2)
    monkeypatch.setattr(subprocess, "run", fake)
    _run_parallel(tmp_path, 3)
    assert _overlaps(fake.spans)
    peak = max(sum(1 for s in fake.spans if s[0] <= t < s[1]) for t, _ in fake.spans)
    assert peak == 2


@pytest.mark.parametrize(
    "fake",
    [
        FakeRun(duration=0, returncode=1),
        FakeRun(duration=0, raises=subprocess.TimeoutExpired("ffmpeg", 1)),
    ],
    ids=["nonzero-returncode", "timeout"],
)
def test_permit_released_on_failure(tmp_path, monkeypatch, fake):
    monkeypatch.setenv("ENGRAM_AV1_MAX_CONCURRENT", "1")
    monkeypatch.setattr(subprocess, "run", fake)
    src = _sources(tmp_path, 1)[0]
    assert av1_hook.transcode_to_av1(src, tmp_path / "out") is None
    # A leaked permit would make this block forever.
    sem = av1_hook._get_gate(1)
    assert sem.acquire(timeout=2)
    sem.release()
    assert av1_hook._gate_busy == 0


def test_queue_wait_does_not_consume_encode_timeout(tmp_path, monkeypatch, caplog):
    """Second caller waits longer than the encode timeout yet still succeeds."""
    monkeypatch.setenv("ENGRAM_AV1_MAX_CONCURRENT", "1")
    monkeypatch.setattr(av1_hook, "ENCODE_TIMEOUT_SECONDS", 0.4)

    class TimeoutAware(FakeRun):
        def __call__(self, cmd, **kw):
            if cmd[0] == "ffprobe":
                return super().__call__(cmd, **kw)
            started = time.monotonic()
            result = super().__call__(cmd, **kw)
            if time.monotonic() - started > kw["timeout"]:
                raise subprocess.TimeoutExpired(cmd, kw["timeout"])
            return result

    fake = TimeoutAware(duration=0.3)
    monkeypatch.setattr(subprocess, "run", fake)
    with caplog.at_level("INFO", logger=av1_hook.logger.name):
        results = _run_parallel(tmp_path, 2)
    # Total elapsed for the second caller (~0.6s) exceeds the 0.4s budget.
    assert all(r is not None for r in results)
    assert fake.timeouts == [0.4, 0.4]
    assert any("waiting for encode slot (1/1 busy)" in r.getMessage() for r in caplog.records)


def test_disabled_hook_never_touches_gate(tmp_path, monkeypatch):
    monkeypatch.delenv("ENGRAM_AV1_HOOK", raising=False)
    src = _sources(tmp_path, 1)[0]
    av1_hook.after_organize(str(src))
    assert av1_hook._gate_sem is None
