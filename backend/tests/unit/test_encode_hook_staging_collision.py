"""Rips from two different TV discs collide in the encode staging root.

``transcode_file`` computes ``dest = staging_dir / src.name`` with no per-job
component, so two discs whose first track carries the same disc label (identical
episode names, or two "Reef Diary" tracks from the same box set) resolve to the
*same* staging path.  The second encode's ``tmp.replace(dest)`` silently destroys
the first one's output, and the marker written for the first source then points
at a file that belongs to a different disc.

The fix: the destination must include a per-rip component so it is unique per
job, while staying under ``ENGRAM_ENCODE_STAGING`` for the curator to find.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.services import encode_hook


def _rip(directory: Path, name: str = "Reef Diary - Builders.mkv") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"src")
    return path


@pytest.fixture()
def env(tmp_path, monkeypatch):
    domain = tmp_path / "_staging"
    domain.mkdir(parents=True)
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(encode_hook, "_DOMAIN_ROOTS", (str(domain),))
    monkeypatch.setenv("ENGRAM_ENCODE_HOOK", "1")
    monkeypatch.setenv("ENGRAM_ENCODE_STAGING", str(out))
    return {"domain": domain, "out": out}


def test_two_rips_with_the_same_track_name_get_different_destinations(env):
    """The regression: same file name, two different source jobs."""
    job_a = _rip(env["domain"] / "disc-a")
    job_b = _rip(env["domain"] / "disc-b")

    dest_a = encode_hook.encode_destination(job_a, env["out"])
    dest_b = encode_hook.encode_destination(job_b, env["out"])

    assert dest_a != dest_b, (
        "two rips resolving to the same staging path would overwrite each other"
    )
    assert dest_a.name == job_a.name
    assert dest_b.name == job_b.name


def test_destination_stays_under_the_encode_staging_root(env):
    """The curator scans ENGRAM_ENCODE_STAGING; output must remain inside it."""
    src = _rip(env["domain"] / "disc-a")
    dest = encode_hook.encode_destination(src, env["out"])
    assert env["out"] in dest.parents
    assert dest.parent != env["out"], (
        "output directly in the staging root is overwritten by the next job "
        "with the same file name"
    )


def test_destination_is_stable_for_the_same_source(env):
    """A retry of the same source must land on the same path, not a new one."""
    src = _rip(env["domain"] / "disc-a")
    first = encode_hook.encode_destination(src, env["out"])
    second = encode_hook.encode_destination(src, env["out"])
    assert first == second


def test_destination_does_not_depend_on_the_process(env):
    """A per-process temp dir or PID would break resume after a restart."""
    src = _rip(env["domain"] / "disc-a")
    dest = encode_hook.encode_destination(src, env["out"])
    text = str(dest)
    assert str(os.getpid()) not in dest.name
    assert "tmp" not in dest.parent.name.lower()


def test_in_place_guard_still_accepts_a_legacy_root_level_destination(env):
    """The old guard must keep working for files already sitting in the root."""
    src = _rip(env["out"])
    assert encode_hook._would_encode_in_place(src, env["out"]) is True
