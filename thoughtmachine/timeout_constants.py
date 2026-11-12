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


# Tools that STAY available when the soft time budget hits CRITICAL
# (``restriction_reason == 'timeout'``). They must be strictly read-only:
# the timeout restriction exists to stop the agent from starting new work,
# not to blind it while it writes up a response.
#
# This is an EXPLICIT, hand-maintained frozenset of canonical registry tool
# names (``ToolBase.tool_name()``). It is deliberately NOT derived from the
# tool registry at runtime: this module is dependency-free by contract (see
# the module docstring) and the engine state must not import ``tools`` to
# decide what it may run. Consequences of hand-maintenance are covered by
# tests/test_state_timeout.py, which fails if any name here stops resolving
# to a registered tool or if a tool with a write-ish ``required_categories``
# ends up in the set.
#
# Deliberately NOT included:
# - ``FileEditor``: one tool for read AND write/delete operations.
# - ``KnowledgeBaseTool``: read and write operations share one tool.
# - ``Worker``: declares ``required_categories: []`` (so a naive category
#   check passes) but spawning nested workers under a timeout restriction is
#   a footgun, not a read.
# - task-list mutators (``WorkingDocument``, ``ProgressReport``): they write.
# - ``Respond`` / ``SummarizeTool``: granted explicitly by AgentState on this
#   axis (SummarizeTool so compaction still works).
TIMEOUT_READ_ONLY_TOOLS: frozenset[str] = frozenset({
    'CheckSystem',        # vault/system inspection, required_categories: []
    'DirectoryTreeTool',  # filesystem:read
    'FilePreviewTool',    # filesystem:read
    'FileSearchTool',     # filesystem:read
    'FileSummaryTool',    # filesystem:read
    'GlobTool',           # filesystem:read
    'ReadFile',           # filesystem:read
    'git_read',           # git:read
})
