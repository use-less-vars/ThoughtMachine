"""JsonlStreamWriter — a tiny, never-raising JSON-lines file writer with
size-based rotation.

Used by the lifecycle event streams (``session.log``, ``worker_*.log``,
``container.log``) which live in the canonical vault log directory
``~/.thoughtmachine/logs``.  All public methods are best-effort and never
raise, so lifecycle logging can never break the caller's control flow.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Optional

#: Default rotation threshold: 5 MB per file with 1 backup kept.
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_KEEP_BACKUPS = 1


class JsonlStreamWriter:
    """Append-only JSON-lines writer with size-based rotation (thread-safe)."""

    def __init__(
        self,
        path: str,
        max_bytes: int = DEFAULT_MAX_BYTES,
        keep_backups: int = DEFAULT_KEEP_BACKUPS,
    ):
        self.path = os.path.abspath(path)
        self.max_bytes = max_bytes
        self.keep_backups = max(0, int(keep_backups))
        self._lock = threading.RLock()
        self._file: Optional[object] = None

    # -- internals -----------------------------------------------------------

    def _ensure_open(self) -> None:
        """Open the file in append mode if not already open."""
        if self._file is None:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self._file = open(self.path, "a", encoding="utf-8")

    def _maybe_rotate(self) -> None:
        """Rotate when the current file reaches ``max_bytes``.

        Shifts ``<path>`` -> ``<path>.1``, dropping the previous backup.
        """
        if not os.path.exists(self.path):
            return
        try:
            if os.path.getsize(self.path) < self.max_bytes:
                return
        except OSError:
            return
        # Close the open handle first so the file can be moved.
        if self._file is not None:
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None
        for i in range(self.keep_backups, 0, -1):
            backup_i = f"{self.path}.{i}"
            if os.path.exists(backup_i):
                try:
                    os.remove(backup_i)
                except OSError:
                    pass
        if self.keep_backups >= 1:
            try:
                os.replace(self.path, f"{self.path}.1")
            except OSError:
                pass

    # -- public API ----------------------------------------------------------

    @property
    def file_path(self) -> str:
        return self.path

    def write(self, record: dict, redact_line: bool = False) -> None:
        """Serialize *record* to a single JSON line and append it.

        Common envelope fields (timestamp, level, logger, pid, thread_id,
        and empty session_id / worker_id / query_id / correlation_id /
        container_id) are injected when absent.  When *redact_line* is
        True, the fully serialized line is passed through
        :func:`agent.logging.redaction.redact` before writing, so secrets
        embedded anywhere in the record (e.g. raw content previews) never
        hit disk.  Never raises.
        """
        try:
            out = dict(record or {})
            out.setdefault(
                "timestamp",
                datetime.now(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
            )
            out.setdefault("level", "INFO")
            out.setdefault("logger", "thoughtmachine.lifecycle")
            out.setdefault("pid", os.getpid())
            out.setdefault("thread_id", threading.get_ident())
            out.setdefault("session_id", "")
            out.setdefault("worker_id", "")
            out.setdefault("query_id", "")
            out.setdefault("correlation_id", "")
            out.setdefault("container_id", "")
            line = json.dumps(out, default=str, ensure_ascii=False) + "\n"
            if redact_line:
                # Local import: redaction is a leaf module, but keeping the
                # import here avoids any import-time coupling for streams.
                from agent.logging.redaction import redact

                line = redact(line)
                if not line.endswith("\n"):
                    line += "\n"
            with self._lock:
                # Rotate first: rotation closes the handle, so re-opening
                # afterwards guarantees the current line is never dropped.
                self._maybe_rotate()
                self._ensure_open()
                if self._file is not None:
                    self._file.write(line)
                    self._file.flush()
        except Exception:
            pass

    def flush(self) -> None:
        """Best-effort flush of any open handle."""
        try:
            with self._lock:
                if self._file is not None:
                    self._file.flush()
        except Exception:
            pass

    def close(self) -> None:
        """Best-effort close of any open handle."""
        try:
            with self._lock:
                if self._file is not None:
                    self._file.close()
                    self._file = None
        except Exception:
            pass
