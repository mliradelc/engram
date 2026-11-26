"""Bridge organizer move progress (worker thread) onto ``move_progress`` WS messages.

The organizers run under ``asyncio.to_thread``, so their ``progress_callback``
fires on a worker thread with no running event loop. ``MoveProgressReporter``
is built in async context (capturing the loop there), handed to the organizer
as its callback, and hands each sample back to the loop with
``run_coroutine_threadsafe``, exactly as the disc-backup progress does.

One reporter per title (per organize call): ``done`` is per title, and so is
the 2 s throttle. Progress is in memory only; a restart loses it, which is
right because a move is not resumable.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from concurrent.futures import Future

logger = logging.getLogger(__name__)

# At most one message per title per this many seconds (the organizer already
# throttles; this bounds the socket even if a caller's callback does not).
MIN_INTERVAL_S = 2.0

_monotonic = time.monotonic


def format_speed(bytes_per_second: float) -> str:
    """Decimal units (1 MB = 10^6 bytes), matching the dashboard's GB readout."""
    if bytes_per_second >= 1e9:
        return f"{bytes_per_second / 1e9:.1f} GB/s"
    if bytes_per_second >= 1e6:
        return f"{bytes_per_second / 1e6:.1f} MB/s"
    if bytes_per_second >= 1e3:
        return f"{bytes_per_second / 1e3:.1f} KB/s"
    return f"{bytes_per_second:.0f} B/s"


class MoveProgressReporter:
    """Thread-safe ``(bytes_done, total_bytes)`` callback that emits ``move_progress``.

    ``__call__`` runs on the organizer's worker thread. ``finish`` runs on the
    loop after the organize call has returned successfully, which is after
    ``move_media_file`` verified the destination, and sends the single
    ``done: true`` message. A same-device rename reports no samples, so it gets
    no ``done`` either: there was no transfer to show.
    """

    def __init__(
        self,
        job_id: int,
        title_index: int | None,
        source_name: str,
        *,
        broadcaster,
        loop: asyncio.AbstractEventLoop | None = None,
        note_activity: Callable[[int], None] | None = None,
    ) -> None:
        self.job_id = job_id
        self.title_index = title_index
        self.source_name = source_name
        self._broadcaster = broadcaster
        # Captured here, in async context: the worker thread has no running loop.
        self._loop = loop or asyncio.get_running_loop()
        self._note_activity = note_activity
        self._last_sent_time: float | None = None
        self._last_sent_bytes = 0
        self._latest_total = 0
        self._seen = False
        self._done_sent = False
        self._pending: list[Future] = []
        self.sent = 0  # messages handed to the loop (diagnostics / tests)

    def __call__(self, done: int, total: int) -> None:
        self._seen = True
        self._latest_total = total
        # A long move is progress: keep the stale-job watchdog from advancing it.
        if self._note_activity is not None:
            with contextlib.suppress(Exception):
                self._note_activity(self.job_id)
        if done >= total and total > 0:
            # The 100% sample is the organizer saying "verified"; ``finish``
            # turns it into the single done message once the call returns.
            return
        now = _monotonic()
        if self._last_sent_time is not None and now - self._last_sent_time < MIN_INTERVAL_S:
            return
        speed: str | None = None
        eta: int | None = None
        if self._last_sent_time is not None:
            elapsed = now - self._last_sent_time
            rate = (done - self._last_sent_bytes) / elapsed if elapsed > 0 else 0.0
            if rate > 0:
                speed = format_speed(rate)
                eta = int((total - done) / rate)
        self._last_sent_time = now
        self._last_sent_bytes = done
        self._submit(done, total, speed, eta, False)

    def _submit(self, current: int, total: int, speed, eta, done: bool) -> None:
        coro = self._broadcaster.broadcast_move_progress(
            self.job_id,
            self.title_index,
            self.source_name,
            current,
            total,
            speed=speed,
            eta_seconds=eta,
            done=done,
        )
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        except Exception:
            # Loop closed, or a broadcaster that returned no coroutine: reporting
            # must never fail the move it is reporting on.
            logger.debug("Could not schedule move_progress", exc_info=True)
            if asyncio.iscoroutine(coro):
                coro.close()
            return
        self._pending.append(fut)
        self.sent += 1

    async def finish(self) -> None:
        """Send ``done: true`` once, after every earlier sample has gone out."""
        if self._done_sent or not self._seen:
            return
        self._done_sent = True
        for fut in self._pending:
            with contextlib.suppress(Exception):
                await asyncio.wrap_future(fut)
        self._pending.clear()
        total = self._latest_total
        try:
            await self._broadcaster.broadcast_move_progress(
                self.job_id,
                self.title_index,
                self.source_name,
                total,
                total,
                speed=None,
                eta_seconds=0,
                done=True,
            )
            self.sent += 1
        except Exception:
            logger.debug("Could not send the final move_progress", exc_info=True)


async def finish_if_success(reporter: MoveProgressReporter, result: dict | None) -> None:
    """Send the reporter's ``done`` only when the organize call reports a real move."""
    if result and result.get("success") and not result.get("skipped"):
        await reporter.finish()
