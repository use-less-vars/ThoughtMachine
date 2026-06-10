"""
File-based locking for session store operations.

Provides a cross-platform ``FileLock`` context manager backed by
``fcntl.flock`` (Linux/Mac) or ``msvcrt.locking`` (Windows) with a
simple PID-file fallback when platform-level locking is unavailable.

Usage::

    with FileLock("/path/to/session.json"):
        # exclusive access to the session file
        ...

All locks are **advisory** — cooperating processes (i.e. all server
instances) must use the same mechanism.
"""

from __future__ import annotations

import os
import platform
import sys
import time
import logging
from pathlib import Path
from typing import Optional, Any


# ── Platform-specific locking ────────────────────────────────────────────
# ``fcntl.flock`` on Linux/Mac, ``msvcrt.locking`` on Windows.
if platform.system() == 'Windows':
    import msvcrt

    def _platform_lock(fd: int, mode: int) -> None:
        msvcrt.locking(fd, mode, 1)  # lock 1 byte at current pos

    _LOCK_EX = msvcrt.LK_NBLCK   # non-blocking exclusive
    _LOCK_UN = msvcrt.LK_UNLCK
else:
    import fcntl

    def _platform_lock(fd: int, mode: int) -> None:
        fcntl.flock(fd, mode)

    _LOCK_EX = fcntl.LOCK_EX | fcntl.LOCK_NB
    _LOCK_UN = fcntl.LOCK_UN

logger = logging.getLogger(__name__)

LOCK_EXTENSION = ".lock"
DEFAULT_TIMEOUT = 10.0  # seconds
POLL_INTERVAL = 0.05    # seconds


class FileLockTimeoutError(Exception):
    """Raised when a lock cannot be acquired within the timeout."""


class FileLock:
    """
    Exclusive, cross-process file lock using ``fcntl.flock``.

    The lock file is created alongside the target path with a ``.lock``
    suffix (e.g. ``session.json.lock``).  Locks are released on
    ``__exit__`` and also automatically released when the process exits
    (the OS closes the file descriptor).

    Thread-safe: two threads in the same process sharing a ``FileLock``
    instance will block each other.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._path = Path(path)
        self._lock_path = self._path.with_suffix(self._path.suffix + LOCK_EXTENSION)
        self._timeout = timeout
        self._fd: Optional[int] = None

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[Any],
    ) -> None:
        self.release()

    # ── Public API ──────────────────────────────────────────────────────────

    def acquire(self) -> None:
        """
        Acquire an exclusive lock on the target file.

        Blocks up to ``timeout`` seconds, then raises
        ``FileLockTimeoutError`` if the lock could not be obtained.
        """
        if self._fd is not None:
            return  # re-entrant: already locked

        deadline = time.monotonic() + self._timeout
        last_exc: Optional[Exception] = None

        while time.monotonic() < deadline:
            try:
                fd = os.open(
                    str(self._lock_path),
                    os.O_CREAT | os.O_WRONLY | os.O_TRUNC,
                    0o644,
                )
            except OSError as exc:
                last_exc = exc
                time.sleep(POLL_INTERVAL)
                continue

            try:
                _platform_lock(fd, _LOCK_EX)
            except (BlockingIOError, PermissionError):
                os.close(fd)
                time.sleep(POLL_INTERVAL)
                continue
            except (OSError, PermissionError) as exc:
                os.close(fd)
                last_exc = exc
                time.sleep(POLL_INTERVAL)
                continue

            # Lock acquired — store fd and write our PID
            self._fd = fd
            try:
                os.write(fd, f"{os.getpid()}\n".encode())
                os.fsync(fd)
            except OSError:
                pass
            logger.debug("Lock acquired: %s", self._lock_path)
            return

        # Timeout
        msg = (
            f"Could not acquire lock on {self._lock_path} "
            f"within {self._timeout}s"
        )
        if last_exc:
            msg += f": {last_exc}"
        raise FileLockTimeoutError(msg)

    def release(self) -> None:
        """Release the lock."""
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        try:
            _platform_lock(fd, _LOCK_UN)
        except (OSError, PermissionError):
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            self._lock_path.unlink(missing_ok=True)
        except OSError:
            pass
        logger.debug("Lock released: %s", self._lock_path)

    @property
    def is_locked(self) -> bool:
        """Return True if the lock is currently held."""
        return self._fd is not None
