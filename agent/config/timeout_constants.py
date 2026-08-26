"""
Backward-compatible re-export shim for the shared timeout constants.

The canonical, dependency-free definitions live in
``thoughtmachine.timeout_constants``. The ``thoughtmachine`` package imports
nothing, so importing that module can never trigger a circular import — unlike
importing this one at module level from ``thoughtmachine.security``, which used
to pull in ``agent.config.models`` -> ``tools`` -> ``tools.docker_code_runner``
-> ``thoughtmachine.security`` (a cycle that silently dropped DockerCodeRunner
from TOOL_CLASSES on certain first-import orders).

Keeping this module as a thin re-export preserves every existing importer
(``agent.config.models``, ``agent.core.state``, ``docker_executor``,
``tools.docker_code_runner``, ``tools.workspace.check_system``, tests).
"""

from thoughtmachine.timeout_constants import (
    IDLE_TIMEOUT_SECONDS,
    SOFT_BUDGET_FALLBACK_SECONDS,
    SHIPPED_SOFT_BUDGET_SECONDS,
)

__all__ = [
    "IDLE_TIMEOUT_SECONDS",
    "SOFT_BUDGET_FALLBACK_SECONDS",
    "SHIPPED_SOFT_BUDGET_SECONDS",
]
