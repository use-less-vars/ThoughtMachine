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
risk.
"""

IDLE_TIMEOUT_SECONDS = 600

SOFT_BUDGET_FALLBACK_SECONDS = 300

SHIPPED_SOFT_BUDGET_SECONDS = 600
