"""
audit_logger.py — Structured audit logging with log rotation for the
ThoughtMachine agent.

Provides two main entry points:

1. ``audit_event(event_type, details)`` — write a single timestamped,
   structured line to the rotating audit log.
2. ``get_logger(name)`` — return a callable that captures *name* in every
   event, for components that want their own tagged logger.

Uses Python's ``RotatingFileHandler`` (10 MB max per file, 3 backups)
instead of raw file-append to avoid unbounded log growth.

The log file path is controlled by the ``CONTAINER_AUDIT_LOG_PATH``
environment variable (default: ``/tmp/container_audit.log``).
"""

from __future__ import annotations

import os
import time
from logging.handlers import RotatingFileHandler
from typing import Callable

# ── File path from env var with fallback ──────────────────────────────────
_AUDIT_LOG_PATH = os.environ.get(
    "CONTAINER_AUDIT_LOG_PATH",
    "/tmp/container_audit.log",
)

# ── Module-level rotating handler ─────────────────────────────────────────
_handler: RotatingFileHandler | None = None


def _get_handler() -> RotatingFileHandler:
    """Return (and lazily initialise) the module-level rotating handler."""
    global _handler
    if _handler is None:
        _handler = RotatingFileHandler(
            filename=_AUDIT_LOG_PATH,
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=3,
            delay=False,
        )
    return _handler


def audit_event(
    event_type: str,
    details: str,
) -> None:
    """Write a timestamped, structured audit line to the rotating log file.

    The line format is::

        <epoch> | <EVENT_TYPE> | <details>

    Example::

        1718012345.678901 | CONTAINER_CREATE | image=agent-exec-abc123 network=bridge

    Args:
        event_type: Uppercase tag identifying the event kind
            (e.g. ``"CONTAINER_CREATE"``, ``"NETWORK_DECISION"``).
        details: Free-form details string.

    The log file path is set via the ``CONTAINER_AUDIT_LOG_PATH`` env var
    (default: ``/tmp/container_audit.log``).  The file is automatically
    rotated when it reaches 10 MB, keeping up to 3 backup files.
    """
    line = f"{time.time()} | {event_type} | {details}\n"
    try:
        handler = _get_handler()
        handler.terminator = ""  # we include \n in the line ourselves
        handler.stream.write(line)
        handler.flush()
    except OSError:
        pass  # best-effort: don't crash the agent if the audit log is unwritable


def get_logger(name: str) -> Callable:
    """Return a callable that writes audit events tagged with *name*.

    The returned callable has the signature::

        logger(event_type: str, details: str) -> None

    and delegates to :func:`audit_event` with a ``name:`` prefix in *details*.

    Example::

        audit = get_logger("container_mgr")
        audit("CONTAINER_CREATE", "network=bridge")
        # writes → 1718012345.678901 | CONTAINER_CREATE | container_mgr: network=bridge
    """
    # Capture name in closure
    def _log(event_type: str, details: str) -> None:
        audit_event(event_type, f"{name}: {details}")

    return _log
