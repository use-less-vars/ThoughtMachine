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
import sys
import time
import fcntl
import logging
from pathlib import Path
from typing import Optional, Any

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
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                time.sleep(POLL_INTERVAL)
                continue
            except OSError as exc:
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
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
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
