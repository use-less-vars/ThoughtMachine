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


# The ONE tool the 'timeout' restriction grants on top of
# ``['Respond', 'SummarizeTool']``.
#
# Rationale (do not "improve" this into a read/salvage list): the soft-timeout
# restriction is a RESOURCE-MANAGEMENT mechanism, not a salvage mechanism.
# When a worker overruns it must YIELD and compact so the parent's context
# stays dense -- handing it read tools lets it keep investigating, which
# defeats the purpose of the restriction. A bare
# ``['Respond', 'SummarizeTool']`` would instead force it to summarise away
# exact detail it can no longer re-derive. The correct escape hatch is the
# OPPOSITE of a read tool: a WRITE-only tool it can use to dump verbatim
# working memory to disk first; the content then SURVIVES the summary because
# it is no longer only in the context window. One tool only, and nothing that
# lets the agent keep investigating.
#
# ``WorkingDocument`` (``tool_name() == 'WorkingDocument'``) resolves its
# permission categories dynamically: ``get_required_categories()`` returns
# ``['filesystem:write']`` (the ``required_categories`` ClassVar itself is
# empty). So it stays subject to the footprint / effective-permission gate at
# call time -- a session without ``filesystem:write`` simply cannot use the
# hatch. That is correct fail-closed behaviour: the timeout allow-list
# narrows what may be CALLED, it never grants a permission.
#
# This must stay an EXPLICIT literal. Deriving it at runtime would mean
# ``agent/core/state.py`` importing ``tools/`` to introspect categories, and
# state.py is dependency-free by contract (it is imported from every state
# consumer, including thoughtmachine.security-adjacent paths). Rot is caught
# by tests/test_state_timeout.py instead, which checks that every member is a
# genuinely registered tool that declares no read, container/exec or
# arbitrary-write category.
TIMEOUT_WRITE_ESCAPE_TOOLS: frozenset[str] = frozenset({'WorkingDocument'})
