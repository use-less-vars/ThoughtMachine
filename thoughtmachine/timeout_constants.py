"""
Shared timeout constants — single source of truth for the timeout-unification
constants introduced in PART 2a of the stabilization plan.

- ``IDLE_TIMEOUT_SECONDS``       — unified idle/cleanup constant (used by the
  four idle sites: docker idle close, container pooling, etc.).
- ``SOFT_BUDGET_FALLBACK_SECONDS`` — engine-code fallback used when the
  ``agent_soft_budget_seconds`` config key is absent.
- ``SHIPPED_SOFT_BUDGET_SECONDS``  — value shipped in
  ``resources/default_config.json`` under ``agent_soft_budget_seconds``.

This module is intentionally dependency-free so it can be imported from
anywhere in the engine (models, state, tools, infra) without circular-import
risk. In particular, ``thoughtmachine.security`` imports it at module level,
and the ``thoughtmachine`` package itself imports nothing, so this module can
never participate in an import cycle.

Moved here from ``agent/config/timeout_constants.py`` (which is now a
re-export shim) so that ``thoughtmachine.security`` no longer needs to import
``agent.config`` at module level — that import chain (agent.config ->
agent.config.models -> tools -> tools.docker_code_runner -> thoughtmachine.security)
was the circular import that silently dropped DockerCodeRunner from
TOOL_CLASSES on Debian-style first-import orders.
"""

IDLE_TIMEOUT_SECONDS = 600

SOFT_BUDGET_FALLBACK_SECONDS = 300

SHIPPED_SOFT_BUDGET_SECONDS = 300
