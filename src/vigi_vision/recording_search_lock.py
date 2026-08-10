"""Bounded local OS lock used by recording-search runs."""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, BinaryIO, final

from vigi_vision.durable_io import is_safe_path

if os.name == "nt":
    import msvcrt
else:
    msvcrt = None

if os.name != "nt":
    import fcntl
else:
    fcntl = None

if TYPE_CHECKING:
    from pathlib import Path


@final
class LocalInvestigationLock:
    """One bounded OS-backed lock file."""

    __slots__ = ("_handle", "_held", "path")

    def __init__(self, path: Path) -> None:
        """Create an unopened lock handle."""
        self.path = path
        self._handle: BinaryIO | None = None
        self._held = False

    @property
    def held(self) -> bool:
        """Return whether this instance owns the lock."""
        return self._held

    def try_acquire(self, timeout_seconds: float) -> bool:
        """Try to acquire the lock within a bounded interval."""
        if self._held:
            return True
        if timeout_seconds < 0 or not is_safe_path(self.path.parent):
            raise OSError
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not is_safe_path(self.path.parent, require_target=True):
            raise OSError
        self.path.touch(exist_ok=True)
        deadline = time.monotonic() + timeout_seconds
        while True:
            handle: BinaryIO | None = None
            try:
                handle = self.path.open("r+b")
                _ = handle.seek(0)
                if handle.read(1) == b"":
                    _ = handle.seek(0)
                    _ = handle.write(b"0")
                    _ = handle.flush()
                _ = handle.seek(0)
                if _try_os_lock(handle):
                    self._handle = handle
                    self._held = True
                    return True
            except OSError:
                pass
            finally:
                if handle is not None and not self._held:
                    handle.close()
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)

    def release(self) -> None:
        """Release and close the owned lock handle."""
        handle = self._handle
        if handle is None:
            return
        try:
            _unlock_os_lock(handle)
        finally:
            handle.close()
            self._handle = None
            self._held = False


def _try_os_lock(handle: BinaryIO) -> bool:
    if os.name == "nt":
        if msvcrt is None:
            raise OSError
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    if fcntl is None:
        raise OSError
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock_os_lock(handle: BinaryIO) -> None:
    if os.name == "nt":
        if msvcrt is None:
            raise OSError
        _ = handle.seek(0)
        _ = msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    if fcntl is None:
        raise OSError
    _ = fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
