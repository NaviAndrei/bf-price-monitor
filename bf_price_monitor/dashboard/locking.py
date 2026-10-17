"""Serialise every writer to the dashboard clone.

Two layers: a process-wide thread lock (requests run in a threadpool) and an
OS byte lock on a file inside ``.git`` (a second server or CLI on the same
clone). The OS drops that lock if the holder dies, so nothing goes stale.
The second layer never waits: it raises ``CloneBusy`` straight away.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import TracebackType
from typing import IO

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

_THREAD_LOCK = threading.Lock()


class CloneBusy(Exception):
    """Another process is using the clone right now."""


def _os_lock(fh: IO[bytes]) -> None:
    fh.seek(0)
    if sys.platform == "win32":
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _os_unlock(fh: IO[bytes]) -> None:
    fh.seek(0)
    if sys.platform == "win32":
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class CloneLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._fh: IO[bytes] | None = None

    def __enter__(self) -> CloneLock:
        _THREAD_LOCK.acquire()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fh = self._path.open("a+b")
            try:
                _os_lock(fh)
            except OSError as exc:
                fh.close()
                raise CloneBusy(
                    "clone busy: another dashboard process is using it"
                ) from exc
        except BaseException:
            _THREAD_LOCK.release()
            raise
        self._fh = fh
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        fh, self._fh = self._fh, None
        try:
            if fh is not None:
                try:
                    _os_unlock(fh)
                finally:
                    fh.close()
        finally:
            _THREAD_LOCK.release()
