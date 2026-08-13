"""Stdlib logging console layer — human-readable, secret-free console output.

This module installs a single ``StreamHandler`` on the ``thoughtmachine``
logger namespace so that lifecycle events (and other stdlib loggers under
``thoughtmachine.*``) are visible on stderr with a compact, human-readable
format.  It deliberately does NOT touch:

- the py-logger file handlers in ``agent/logging/__init__.py`` (each
  ``agent_<session>`` logger keeps its ``NullHandler``),
- the print-based console in ``agent/presenters/unified.py``.
"""

from __future__ import annotations

import logging
import os
import sys
import threading

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_DATEFMT = "%H:%M:%S"

_configured = False
_configured_lock = threading.Lock()


def get_console_logger(name: str = "") -> logging.Logger:
    """Return a logger under the ``thoughtmachine`` namespace."""
    if name:
        return logging.getLogger(f"thoughtmachine.{name}")
    return logging.getLogger("thoughtmachine")


def configure_console_logging(level: "int | str | None" = None) -> None:
    """Install the console handler on ``thoughtmachine`` (idempotent).

    Args:
        level: Optional explicit level (int or level name).  Defaults to
            ``TM_LOG_CONSOLE_LEVEL`` env var, else ``WARNING``.
    """
    global _configured
    with _configured_lock:
        if _configured:
            return
        logger = get_console_logger()
        # Skip if a StreamHandler is already attached to this logger.
        if any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
            _configured = True
            return
        if level is None:
            level = os.environ.get("TM_LOG_CONSOLE_LEVEL", "").strip() or "WARNING"
        if isinstance(level, int):
            numeric = level
        else:
            try:
                numeric = logging.getLevelName(str(level).upper())
                if not isinstance(numeric, int):
                    numeric = logging.WARNING
            except Exception:
                numeric = logging.WARNING
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
        logger.addHandler(handler)
        logger.setLevel(numeric)
        logger.propagate = True
        _configured = True
