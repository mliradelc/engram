"""Verified, byte-progressing organize moves (SPEC-048).

A Blu-ray title moved from local staging onto a CIFS library share is a
cross-device move: ``os.replace`` fails with EXDEV and the organizer copies.
That copy used to be one blocking ``shutil.copy2`` with no progress and no
check that the destination was whole before the source was deleted. These
tests pin the chunked copy, its progress samples and throttle, the size
verification, the ``move_progress`` wire shape, and the review path's events.
"""

import asyncio
import errno
import importlib
import itertools
import json
import os
from unittest.mock import AsyncMock, patch

import pytest

from app.api.websocket import ConnectionManager
from app.core.organizer import move_media_file
from app.models import DiscJob, JobState
from app.models.app_config import AppConfig
from app.models.disc_job import ContentType, DiscTitle, TitleState
from app.services import move_progress as move_progress_module
from app.services.event_broadcaster import EventBroadcaster
from app.services.move_progress import MoveProgressReporter
from tests.unit.conftest import _unit_session_factory
from tests.unit.test_movie_review_conflict import (
    _make_coord,
    _patch_session_and_ws,  # noqa: F401 (autouse fixture)
    conflict_default,  # noqa: F401 (pytest fixture)
)

organizer_module = importlib.import_module("app.core.organizer")


def _cross_device(monkeypatch) -> dict:
    calls = {"replace": 0}

    def fake_replace(_src, _dst):
        calls["replace"] += 1
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "replace", fake_replace)
    return calls


def _fake_clock(monkeypatch, step: float) -> None:
    """Every read of the organizer's clock advances it by ``step`` seconds."""
    ticks = itertools.count()
    monkeypatch.setattr(organizer_module, "_monotonic", lambda: next(ticks) * step)


def _files(tmp_path, size: int):
    src = tmp_path / "staging" / "The Matrix_t01.mkv"
    src.parent.mkdir()
    src.write_bytes(os.urandom(size))
    dest_dir = tmp_path / "library"
    dest_dir.mkdir()
    return src, dest_dir / "The Matrix (1999).mkv"


class TestSameDevice:
    def test_one_replace_and_no_progress(self, tmp_path, monkeypatch):
        src, dest = _files(tmp_path, 4096)
        real_replace = os.replace
        calls = {"n": 0}

        def counting_replace(a, b):
            calls["n"] += 1
            return real_replace(a, b)

        monkeypatch.setattr(os, "replace", counting_replace)
        samples: list[tuple[int, int]] = []

        move_media_file(src, dest, progress_callback=lambda d, t: samples.append((d, t)))

        assert calls["n"] == 1
        assert samples == []
        assert dest.exists() and not src.exists()


class TestCrossDevice:
    def test_progress_samples_and_a_verified_move(self, tmp_path, monkeypatch):
        size = 100 * 1024
        src, dest = _files(tmp_path, size)
        payload = src.read_bytes()
        calls = _cross_device(monkeypatch)
        monkeypatch.setattr(organizer_module, "COPY_CHUNK_BYTES", 1024)
        _fake_clock(monkeypatch, 1.0)
        samples: list[tuple[int, int]] = []

        move_media_file(src, dest, delay=0, progress_callback=lambda d, t: samples.append((d, t)))

        assert calls["replace"] == 1  # EXDEV short-circuits the retry budget
        assert samples[0] == (0, size)
        assert samples[-1] == (size, size)
        assert [s for s in samples if s[1] != size] == []
        intermediate = [d for d, _ in samples[1:-1]]
        assert intermediate, "expected at least one sample between 0 and 100%"
        assert all(0 < d < size for d in intermediate)
        assert intermediate == sorted(intermediate)
        # Exactly one 100% sample: it is the "verified" signal.
        assert sum(1 for d, _ in samples if d == size) == 1
        assert not src.exists()
        assert dest.stat().st_size == size
        assert dest.read_bytes() == payload

    def test_preserves_metadata_like_copy2(self, tmp_path, monkeypatch):
        src, dest = _files(tmp_path, 2048)
        os.utime(src, (1_000_000_000, 1_000_000_000))
        _cross_device(monkeypatch)

        move_media_file(src, dest, delay=0)

        assert int(dest.stat().st_mtime) == 1_000_000_000

    def test_short_copy_raises_and_leaves_nothing_at_dest(self, tmp_path, monkeypatch):
        size = 10_000
        src, dest = _files(tmp_path, size)
        _cross_device(monkeypatch)
        real_copyfileobj = organizer_module.shutil.copyfileobj

        def short_copy(fsrc, fdst, length=0):
            # A share that silently drops the tail of the write.
            fdst.write(fsrc.read(size - 1234))

        monkeypatch.setattr(organizer_module.shutil, "copyfileobj", short_copy)
        samples: list[tuple[int, int]] = []

        with pytest.raises(OSError) as excinfo:
            move_media_file(
                src, dest, delay=0, progress_callback=lambda d, t: samples.append((d, t))
            )

        monkeypatch.setattr(organizer_module.shutil, "copyfileobj", real_copyfileobj)
        message = str(excinfo.value)
        assert str(size) in message and str(size - 1234) in message
        assert not dest.exists()
        assert src.exists() and src.stat().st_size == size
        # Never claimed complete.
        assert (size, size) not in samples

    def test_failed_source_unlink_still_rolls_back(self, tmp_path, monkeypatch):
        """The #642 rollback is untouched by the new copy path."""
        src, dest = _files(tmp_path, 4096)
        _cross_device(monkeypatch)
        real_unlink = os.unlink

        def fake_unlink(path):
            if str(path) == str(src):
                raise PermissionError(13, "in use")
            real_unlink(path)

        monkeypatch.setattr(os, "unlink", fake_unlink)

        with pytest.raises(OSError, match="safe to retry"):
            move_media_file(src, dest, attempts=1, delay=0)

        assert src.exists() and not dest.exists()

    def test_a_raising_callback_never_fails_the_move(self, tmp_path, monkeypatch):
        src, dest = _files(tmp_path, 4096)
        _cross_device(monkeypatch)

        def boom(_d, _t):
            raise RuntimeError("socket gone")

        move_media_file(src, dest, delay=0, progress_callback=boom)

        assert dest.exists() and not src.exists()


class TestThrottle:
    def test_a_100_chunk_copy_reports_far_fewer_than_100_samples(self, tmp_path, monkeypatch):
        size = 100 * 1024
        src, dest = _files(tmp_path, size)
        _cross_device(monkeypatch)
        monkeypatch.setattr(organizer_module, "COPY_CHUNK_BYTES", 1024)
        # Half a second per clock read: the 2 s gate, not the 1% gate, binds.
        _fake_clock(monkeypatch, 0.5)
        samples: list[tuple[int, int]] = []

        move_media_file(src, dest, delay=0, progress_callback=lambda d, t: samples.append((d, t)))

        assert 3 <= len(samples) <= 30

    def test_simulated_40_gb_copy_cadence(self, monkeypatch):
        """The measured case: 39,531,675,648 B in 8 MiB chunks at 42.1 MB/s."""
        total = 39_531_675_648
        chunk = organizer_module.COPY_CHUNK_BYTES
        clock = {"t": 0.0}
        monkeypatch.setattr(organizer_module, "_monotonic", lambda: clock["t"])
        times: list[float] = []

        throttle = organizer_module._ProgressThrottle(lambda d, t: times.append(clock["t"]), total)
        throttle.start()
        done = 0
        while done < total:
            n = min(chunk, total - done)
            done += n
            clock["t"] += n / 42.1e6
            throttle.update(done)
        throttle.finish()

        gaps = [b - a for a, b in itertools.pairwise(times)]
        # (0, total), ~99 one-percent samples, and the verified (total, total).
        assert 95 <= len(times) <= 102
        assert min(gaps[:-1]) >= organizer_module.PROGRESS_MIN_INTERVAL_S


class TestWireShape:
    async def test_manager_emits_a_flat_message_with_every_key(self):
        mgr = ConnectionManager()
        with patch.object(mgr, "broadcast", new=AsyncMock()) as bcast:
            await mgr.broadcast_move_progress(
                15,
                title_index=1,
                source_name="The Matrix_t01.mkv",
                current_bytes=12_345_678,
                total_bytes=39_531_675_648,
                speed="42.1 MB/s",
                eta=912,
                done=False,
            )

        assert bcast.await_args.args[0] == {
            "type": "move_progress",
            "job_id": 15,
            "title_index": 1,
            "source_name": "The Matrix_t01.mkv",
            "current_bytes": 12_345_678,
            "total_bytes": 39_531_675_648,
            "speed": "42.1 MB/s",
            "eta": 912,
            "done": False,
        }
        assert "data" not in bcast.await_args.args[0]

    async def test_nulls_are_sent_not_omitted(self):
        mgr = ConnectionManager()
        with patch.object(mgr, "broadcast", new=AsyncMock()) as bcast:
            await mgr.broadcast_move_progress(
                3, title_index=None, source_name="x.mkv", current_bytes=0, total_bytes=10
            )

        msg = bcast.await_args.args[0]
        assert msg["speed"] is None and msg["eta"] is None and msg["done"] is False
        assert "title_index" in msg and msg["title_index"] is None

    async def test_broadcaster_forwards_with_the_manager_parameter_names(self):
        ws = AsyncMock()
        await EventBroadcaster(ws).broadcast_move_progress(
            15, 1, "a.mkv", 5, 10, speed="1.0 MB/s", eta_seconds=5, done=True
        )

        ws.broadcast_move_progress.assert_awaited_once_with(
            15,
            title_index=1,
            source_name="a.mkv",
            current_bytes=5,
            total_bytes=10,
            speed="1.0 MB/s",
            eta=5,
            done=True,
        )


class TestReporter:
    async def test_throttles_times_speed_and_sends_one_done(self, monkeypatch):
        clock = {"t": 100.0}
        monkeypatch.setattr(move_progress_module, "_monotonic", lambda: clock["t"])
        broadcaster = AsyncMock()
        heartbeats: list[int] = []
        reporter = MoveProgressReporter(
            15, 1, "a.mkv", broadcaster=broadcaster, note_activity=heartbeats.append
        )
        total = 1_000_000_000

        def worker():
            reporter(0, total)
            clock["t"] += 1.0
            reporter(10_000_000, total)  # inside the 2 s window: dropped
            clock["t"] += 9.0
            reporter(421_000_000, total)  # 421 MB over 10 s
            reporter(total, total)  # the verified sample: becomes done

        await asyncio.to_thread(worker)
        await reporter.finish()
        await reporter.finish()  # idempotent

        sent = [c.args + (c.kwargs,) for c in broadcaster.broadcast_move_progress.await_args_list]
        assert len(sent) == 3
        first, second, last = sent
        assert first[3] == 0 and first[5]["speed"] is None and first[5]["eta_seconds"] is None
        assert second[3] == 421_000_000 and second[5]["speed"] == "42.1 MB/s"
        assert second[5]["eta_seconds"] == int((total - 421_000_000) / 42.1e6)
        assert last[3] == last[4] == total and last[5]["done"] is True
        assert all(not s[5]["done"] for s in sent[:-1])
        assert heartbeats and set(heartbeats) == {15}

    async def test_a_rename_sends_nothing(self):
        broadcaster = AsyncMock()
        reporter = MoveProgressReporter(15, 1, "a.mkv", broadcaster=broadcaster)
        await reporter.finish()
        broadcaster.broadcast_move_progress.assert_not_awaited()


class TestApplyReviewEmitsMoveProgress:
    async def test_movie_review_reports_progress_then_done(
        self,
        tmp_path,
        monkeypatch,
        conflict_default,  # noqa: F811
    ):
        staged = tmp_path / "The Matrix_t01.mkv"
        staged.write_bytes(b"x" * 3000)
        async with _unit_session_factory() as session:
            job = DiscJob(
                drive_id="/dev/sr0",
                volume_label="THE_MATRIX",
                content_type=ContentType.MOVIE,
                state=JobState.REVIEW_NEEDED,
                detected_title="The Matrix",
                tmdb_name="The Matrix",
                tmdb_year=1999,
                staging_path=str(tmp_path),
            )
            session.add(job)
            await session.commit()
            await session.refresh(job)
            title = DiscTitle(
                job_id=job.id,
                title_index=1,
                duration_seconds=8160,
                file_size_bytes=3000,
                is_selected=True,
                output_filename=str(staged),
                state=TitleState.REVIEW,
                match_details=json.dumps({}),
            )
            session.add(title)
            await session.commit()
            await session.refresh(title)
            job_id, title_id = job.id, title.id

        def stub_move(src, dest, *, progress_callback=None, **_kw):
            total = src.stat().st_size
            for done in (0, total // 2, total):
                progress_callback(done, total)
            os.replace(src, dest)

        monkeypatch.setattr(organizer_module, "move_media_file", stub_move)
        coord = _make_coord()
        coord._broadcaster.broadcast_move_progress = AsyncMock()
        library = tmp_path / "lib"

        with patch(
            "app.services.config_service.get_config_sync",
            return_value=AppConfig(
                library_movies_path=str(library), naming_movie_format="{title} ({year})"
            ),
        ):
            await coord.apply_review(job_id, title_id)

        calls = coord._broadcaster.broadcast_move_progress.await_args_list
        mine = [c for c in calls if c.args[0] == job_id]
        progress = [c for c in mine if not c.kwargs["done"]]
        done = [c for c in mine if c.kwargs["done"]]
        assert len(progress) >= 1
        assert len(done) == 1
        assert done[0].args[1] == 1 and done[0].args[2] == "The Matrix_t01.mkv"
        assert done[0].args[3] == done[0].args[4] == 3000
        assert (library / "The Matrix (1999)" / "The Matrix (1999).mkv").exists()
        async with _unit_session_factory() as session:
            assert (await session.get(DiscJob, job_id)).state == JobState.COMPLETED
