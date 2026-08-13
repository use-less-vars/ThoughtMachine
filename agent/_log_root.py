"""Shared canonical log-root resolution.

Every structured-logging consumer (lifecycle streams, event log,
``_AgentLogger`` files, ``tm-logs``) resolves its log directory through
:func:`get_log_root`, which honors the ``THOUGHTMACHINE_VAULT_ROOT``
environment variable *at call time*::

    THOUGHTMACHINE_VAULT_ROOT set  ->  $THOUGHTMACHINE_VAULT_ROOT/logs
    unset                          ->  ~/.thoughtmachine/logs

The helper is side-effect-free (it never creates directories) and is
evaluated dynamically on every call, so setting the variable mid-process
takes effect everywhere immediately -- there is no import-time binding.
"""

from __future__ import annotations

import os
from pathlib import Path


def get_log_root() -> Path:
    """Return the canonical vault log directory as a :class:`pathlib.Path`.

    ``THOUGHTMACHINE_VAULT_ROOT`` overrides the default ``~/.thoughtmachine``
    home; the log directory is always ``<root>/logs``.  Never creates
    directories and never raises.
    """
    override = os.environ.get("THOUGHTMACHINE_VAULT_ROOT")
    if override:
        return Path(override) / "logs"
    return Path(os.path.expanduser("~")) / ".thoughtmachine" / "logs"
