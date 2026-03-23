"""kqueue-based file watcher for chat.db changes on macOS."""

from __future__ import annotations

import asyncio
import logging
import os
import select
from pathlib import Path

log = logging.getLogger(__name__)


class ChatDBWatcher:
    """Watch chat.db (+ WAL/SHM) for writes using kqueue.

    Integrates with asyncio via loop.add_reader on the kqueue fd.
    Falls back to pure polling if kqueue init fails.
    """

    def __init__(self, chat_db_path: str | Path) -> None:
        self._db_path = Path(chat_db_path).expanduser()
        self._kq: select.kqueue | None = None
        self._fds: list[int] = []
        self._event = asyncio.Event()
        self._debounce_handle: asyncio.TimerHandle | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._active = False

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Initialize kqueue watches. Call once after event loop is running."""
        self._loop = loop or asyncio.get_event_loop()
        try:
            self._kq = select.kqueue()
        except (OSError, AttributeError):
            log.warning("kqueue not available — falling back to pure polling")
            return

        # Watch the main db plus WAL/SHM if they exist
        paths = [self._db_path]
        for suffix in ("-wal", "-shm"):
            p = self._db_path.parent / (self._db_path.name + suffix)
            if p.exists():
                paths.append(p)

        events = []
        for p in paths:
            try:
                fd = os.open(str(p), os.O_EVTONLY)
                self._fds.append(fd)
                events.append(select.kevent(
                    fd,
                    filter=select.KQ_FILTER_VNODE,
                    flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                    fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND,
                ))
            except OSError as e:
                log.debug("Could not watch %s: %s", p, e)

        if not events:
            log.warning("No files to watch — falling back to pure polling")
            self._cleanup_kqueue()
            return

        try:
            self._kq.control(events, 0, 0)
            self._loop.add_reader(self._kq.fileno(), self._on_kqueue_readable)
            self._active = True
            log.info("kqueue watcher active on %d file(s)", len(self._fds))
        except OSError as e:
            log.warning("kqueue registration failed: %s — falling back to pure polling", e)
            self._cleanup_kqueue()

    def _on_kqueue_readable(self) -> None:
        """Called when kqueue fd is readable — drain events and debounce."""
        if not self._kq:
            return
        try:
            # Drain pending events (non-blocking)
            self._kq.control([], 8, 0)
        except OSError:
            pass

        # Debounce: reset timer on each write, fire after 0.25s of quiet
        if self._debounce_handle:
            self._debounce_handle.cancel()
        if self._loop:
            self._debounce_handle = self._loop.call_later(0.25, self._fire)

    def _fire(self) -> None:
        """Set the event after debounce period."""
        self._event.set()

    async def wait(self, timeout: float) -> None:
        """Wait for a file change or fall back to timeout.

        Returns when chat.db changes (fast path) or after timeout (poll fallback).
        """
        self._event.clear()
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    def stop(self) -> None:
        """Clean up kqueue resources."""
        if self._debounce_handle:
            self._debounce_handle.cancel()
        if self._active and self._kq and self._loop:
            try:
                self._loop.remove_reader(self._kq.fileno())
            except (OSError, ValueError):
                pass
        self._cleanup_kqueue()
        self._active = False
        log.debug("kqueue watcher stopped")

    def _cleanup_kqueue(self) -> None:
        """Close kqueue and watched file descriptors."""
        for fd in self._fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds.clear()
        if self._kq:
            try:
                self._kq.close()
            except OSError:
                pass
            self._kq = None
