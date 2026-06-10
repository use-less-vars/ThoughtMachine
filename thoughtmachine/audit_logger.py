"""
audit_logger.py — Reusable, structured audit logging for the ThoughtMachine agent.

Provides two main entry points:

1. ``audit_event(event_type, details, log_file)`` — write a single
   timestamped, structured line to the log file.
2. ``get_logger(name)`` — return a callable that captures *name* in every
   event, for components that want their own log file.

All existing ``open("/tmp/container_audit.log", "a")`` calls in the
codebase should be replaced by calls to this module.
"""

from __future__ import annotations

import time
import os
from typing import Callable


def audit_event(
    event_type: str,
    details: str,
    log_file: str = "/tmp/container_audit.log",
) -> None:
    """Write a timestamped, structured audit line to *log_file*.

    The line format is::

        <epoch> | <EVENT_TYPE> | <details>

    Example::

        1718012345.678901 | CONTAINER_CREATE | image=agent-exec-abc123 network=bridge

    Args:
        event_type: Uppercase tag identifying the event kind
            (e.g. ``"CONTAINER_CREATE"``, ``"NETWORK_DECISION"``).
        details: Free-form details string.
        log_file: Path to the audit log file (default:
            ``/tmp/container_audit.log``).
    """
    line = f"{time.time()} | {event_type} | {details}\n"
    try:
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        with open(log_file, "a") as f:
            f.write(line)
    except OSError:
        pass  # best-effort: don't crash the agent if the audit log is unwritable


def get_logger(name: str, log_file: str = "/tmp/container_audit.log") -> Callable:
    """Return a callable that writes audit events tagged with *name*.

    The returned callable has the signature::

        logger(event_type: str, details: str) -> None

    and delegates to :func:`audit_event` with the configured *log_file*
    and a ``name:`` prefix in *details*.

    Example::

        audit = get_logger("container_mgr")
        audit("CONTAINER_CREATE", "network=bridge")
        # writes → 1718012345.678901 | CONTAINER_CREATE | container_mgr: network=bridge
    """
    log_file_val = log_file

    def _log(event_type: str, details: str) -> None:
        audit_event(event_type, f"{name}: {details}", log_file=log_file_val)

    return _log
