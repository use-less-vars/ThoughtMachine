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
environment variable.  When that variable is unset the log lives in the
portable vault log root (``$THOUGHTMACHINE_VAULT_ROOT/logs`` or
``~/.thoughtmachine/logs``), resolved lazily through
:func:`agent._log_root.get_log_root`.  No POSIX-only path such as ``/tmp``
is hardcoded anywhere, so this module imports cleanly on Windows.

Logging is strictly best-effort: if the audit sink cannot be created or
written, the problem is reported once and later events are dropped.  The
agent itself never crashes because of audit logging.
"""

from __future__ import annotations

import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

# ── Path resolution ──────────────────────────────────────────────────────────
_ENV_LOG_PATH = "CONTAINER_AUDIT_LOG_PATH"
_DEFAULT_LOG_NAME = "container_audit.log"
_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_BACKUP_COUNT = 3

# ── Module-level rotating handler ─────────────────────────────────────────────
_handler: RotatingFileHandler | None = None
# Once the sink proves unusable we stop retrying and stop warning.
_sink_failed: bool = False
_warned: bool = False


def _default_log_path() -> str:
    """Return the portable default audit log path (never raises).

    ``agent._log_root`` is imported lazily on purpose: it keeps this module
    importable in isolation (no import-time dependency on the ``agent``
    package or on anything heavy) and it lets the vault root be honoured at
    call time rather than at import time.
    """
    try:
        from agent._log_root import get_log_root

        return str(Path(get_log_root()) / _DEFAULT_LOG_NAME)
    except Exception:
        # Import failed (e.g. partially initialised ``agent`` package during a
        # circular import): reproduce get_log_root()'s logic without importing.
        root = os.environ.get("THOUGHTMACHINE_VAULT_ROOT")
        base = Path(root) if root else Path(os.path.expanduser("~")) / ".thoughtmachine"
        return str(base / "logs" / _DEFAULT_LOG_NAME)


def _resolve_log_path() -> str:
    """Return the audit log path: env override wins, else the portable default."""
    override = os.environ.get(_ENV_LOG_PATH, "").strip()
    if override:
        return override
    return _default_log_path()


def _warn_once(message: str) -> None:
    """Report an audit-sink failure once per process; never raises."""
    global _warned
    if _warned:
        return
    _warned = True
    try:
        from agent.logging import log

        log("WARNING", "audit", message)
        return
    except Exception:
        pass
    try:
        logging.getLogger(__name__).warning(message)
    except Exception:
        pass


def _get_handler() -> RotatingFileHandler | None:
    """Return (and lazily initialise) the module-level rotating handler.

    Returns ``None`` when the sink is unavailable; the parent directory is
    created on demand because :func:`agent._log_root.get_log_root` is
    side-effect-free.
    """
    global _handler, _sink_failed
    if _handler is not None:
        return _handler
    if _sink_failed:
        return None

    path = _resolve_log_path()
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        _handler = RotatingFileHandler(
            filename=path,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            delay=False,
        )
    except Exception as exc:
        _sink_failed = True
        _warn_once(f"audit log sink unavailable ({path}): {exc}; audit events are dropped")
        return None
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

    The log file path is set via the ``CONTAINER_AUDIT_LOG_PATH`` env var;
    when unset it defaults to ``container_audit.log`` inside the vault log
    root (``$THOUGHTMACHINE_VAULT_ROOT/logs`` or ``~/.thoughtmachine/logs``).
    The file is automatically rotated when it reaches 10 MB, keeping up to 3
    backup files.  Callers are never expected to handle failures.
    """
    line = f"{time.time()} | {event_type} | {details}\n"
    try:
        handler = _get_handler()
        if handler is None:
            return
        handler.terminator = ""  # we include \n in the line ourselves
        handler.stream.write(line)
        handler.flush()
    except Exception:
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
