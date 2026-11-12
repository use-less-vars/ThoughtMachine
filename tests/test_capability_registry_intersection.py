"""Defence in depth: the runtime tool list is the INTERSECTION of
``enabled_tools`` and the capability registry's resolved set.

The capability registry decides what a worker *declares*; ``enabled_tools``
carries that decision to the agent.  If a future bug widens ``enabled_tools``,
the agent must still be unable to construct a tool the frozen registry did not
resolve.  That is what ``AgentConfig.resolved_tool_names`` +
``get_filtered_tool_classes(allowed_names=...)`` provide:

I1. ``allowed_names=None`` (the default) is INERT: the result is exactly the
    ``enabled_tools``-only filtering that existed before the registry.
I2. ``allowed_names`` withdraws: in ``enabled_tools`` but not allowed -> gone.
I3. ``allowed_names`` never grants: allowed but not in ``enabled_tools`` ->
    still gone (an allowlist cannot resurrect a tool).
I4. Name-alias aware in both directions (``GitInfoTool`` / ``git_read``).
I5. ``WorkerThread._build_agent_config`` carries the registry's resolved set
    onto the config it returns, and ``None`` when the registry is unavailable.
I6. An empty intersection is loud rather than a silently toolless agent.

Hermetic: pure config filtering plus a real but *never started* ``WorkerThread``
(no thread, no container, no network, no LLM call).
"""

from __future__ import annotations

import copy

import pytest

from agent.config.models import AgentConfig
from tools import SIMPLIFIED_TOOL_CLASSES
from tools.git_info_tool import GitReadTool
from tools.git_write_tool import GitWriteTool
from tools.workspace.worker_thread import WorkerThread as RealWorkerThread

_BY_NAME = {cls.__name__: cls for cls in SIMPLIFIED_TOOL_CLASSES}

FILE_EDITOR = _BY_NAME["FileEditor"]
GLOB_TOOL = _BY_NAME["GlobTool"]
DATE_TIME = _BY_NAME["DateTimeTool"]

PARENT_CFG = {"provider": "openai", "model": "gpt-4o"}


def _cfg(**kwargs):
    """A minimal ``AgentConfig`` whose tool set is exactly what we pass."""
    kwargs.setdefault("mode", "custom")
    return AgentConfig(**kwargs)


# ---------------------------------------------------------------------------
# I1. ``allowed_names=None`` is inert
# ---------------------------------------------------------------------------


def test_allowed_names_none_matches_enabled_tools_only_filtering():
    """The new keyword must not change any pre-registry behaviour."""
    cfg = _cfg(enabled_tools=["FileEditor", "GlobTool"])

    assert cfg.get_filtered_tool_classes() == cfg.get_filtered_tool_classes(
        allowed_names=None
    )
    assert set(cfg.get_filtered_tool_classes(allowed_names=None)) == {
        FILE_EDITOR,
        GLOB_TOOL,
    }


def test_resolved_tool_names_defaults_to_none():
    """``None`` is the documented "no narrowing" default."""
    assert _cfg(enabled_tools=["FileEditor"]).resolved_tool_names is None


# ---------------------------------------------------------------------------
# I2. The allowlist withdraws
# ---------------------------------------------------------------------------


def test_enabled_but_not_allowed_is_dropped():
    cfg = _cfg(enabled_tools=["FileEditor", "GlobTool", "DateTimeTool"])

    kept = cfg.get_filtered_tool_classes(
        allowed_names=frozenset({"FileEditor", "DateTimeTool"})
    )

    assert set(kept) == {FILE_EDITOR, DATE_TIME}
    assert GLOB_TOOL not in kept


def test_allowlist_accepts_a_plain_iterable():
    """Any container of names works -- not just a frozenset."""
    cfg = _cfg(enabled_tools=["FileEditor", "GlobTool"])

    kept = cfg.get_filtered_tool_classes(allowed_names=["FileEditor"])

    assert kept == [FILE_EDITOR]


# ---------------------------------------------------------------------------
# I3. The allowlist never grants
# ---------------------------------------------------------------------------


def test_allowed_but_not_enabled_is_never_granted():
    cfg = _cfg(enabled_tools=["FileEditor"])

    kept = cfg.get_filtered_tool_classes(
        allowed_names=frozenset({"FileEditor", "GlobTool", "DateTimeTool"})
    )

    assert kept == [FILE_EDITOR]
    assert GLOB_TOOL not in kept
    assert DATE_TIME not in kept


def test_empty_enabled_tools_stays_empty_even_if_allowed():
    """An empty ``enabled_tools`` means "no tools", not "all allowed"."""
    cfg = _cfg(enabled_tools=[])

    assert cfg.get_filtered_tool_classes(allowed_names=frozenset(_BY_NAME)) == []


# ---------------------------------------------------------------------------
# I4. Legacy / stable names match through the allowlist path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "enabled_name,allowed_name",
    [
        ("GitInfoTool", "git_read"),  # legacy enabled, stable allowed
        ("git_read", "GitInfoTool"),  # stable enabled, legacy allowed
        ("git_read", "git_read"),
    ],
)
def test_git_read_matches_through_both_name_spellings(enabled_name, allowed_name):
    cfg = _cfg(enabled_tools=[enabled_name, "host_bash"])

    kept = cfg.get_filtered_tool_classes(allowed_names=frozenset({allowed_name}))

    assert kept == [GitReadTool]


def test_allowlist_alone_does_not_grant_git_write():
    """Matching a legacy alias must not drag in the sibling git tool."""
    cfg = _cfg(enabled_tools=["git_read"])

    kept = cfg.get_filtered_tool_classes(
        allowed_names=frozenset({"git_read", "git_write"})
    )

    assert kept == [GitReadTool]
    assert GitWriteTool not in kept


# ---------------------------------------------------------------------------
# I5. ``_build_agent_config`` carries the registry's resolved set
# ---------------------------------------------------------------------------


def _definition(name, tools):
    return {
        "status": "ready",
        "permission_footprint": {},
        "system_prompt": "intersection fixture",
        "name": name,
        "tools": list(tools),
    }


def _worker(tmp_path, definition, agent_config=None):
    return RealWorkerThread(
        name="intersect-worker",
        definition=copy.deepcopy(definition),
        agent_config=dict(agent_config or PARENT_CFG),
        workspace_dir=tmp_path,
        session_permissions={},
    )


def test_build_agent_config_carries_resolved_tool_names(tmp_path):
    thread = _worker(tmp_path, _definition("intersect", ["FileEditor", "DateTimeTool"]))

    cfg = thread._build_agent_config()

    assert cfg is not None
    registry = thread._resolve_capability_registry()
    assert cfg.resolved_tool_names == frozenset(registry.names)
    assert cfg.resolved_tool_names, "an empty allowlist would disable every tool"
    # Same object identity semantics: the config can only ever narrow.
    assert set(cfg.enabled_tools) <= set(cfg.resolved_tool_names)


def test_build_agent_config_resolved_tool_names_is_none_without_registry(tmp_path):
    """Registry unavailable -> ``None`` (legacy behaviour), never a guess."""
    thread = _worker(tmp_path, _definition("intersect", ["FileEditor"]))

    def _boom():
        raise RuntimeError("registry unavailable")

    thread._resolve_capability_registry = _boom  # type: ignore[method-assign]

    cfg = thread._build_agent_config()

    assert cfg is not None
    assert cfg.resolved_tool_names is None
    # The legacy derivation still produced a usable tool list.
    assert cfg.enabled_tools == ["FileEditor"]


def test_build_agent_config_mirrors_runtime_tool_classes(tmp_path):
    """``_tool_classes`` is the SAME list the agent will hand to its executor."""
    thread = _worker(tmp_path, _definition("intersect", ["FileEditor", "DateTimeTool"]))

    cfg = thread._build_agent_config()

    expected = cfg.get_filtered_tool_classes(allowed_names=cfg.resolved_tool_names)
    assert set(thread._tool_classes) == {cls.__name__ for cls in expected}
    assert set(thread._tool_classes.values()) == set(expected)


# ---------------------------------------------------------------------------
# I6. An empty intersection is loud
# ---------------------------------------------------------------------------


def test_empty_intersection_logs_a_warning(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "agent.config.models.log",
        lambda level, source, message: calls.append((level, source, message)),
    )
    cfg = _cfg(enabled_tools=["FileEditor"])

    kept = cfg.get_filtered_tool_classes(allowed_names=frozenset({"GlobTool"}))

    assert kept == []
    warnings = [c for c in calls if c[0] == "WARNING"]
    assert len(warnings) == 1, calls
    assert "EMPTY" in warnings[0][2]
    assert "FileEditor" in warnings[0][2]


def test_partial_intersection_does_not_warn(monkeypatch):
    """A narrowing is normal; only a total wipe-out is worth a warning."""
    calls = []
    monkeypatch.setattr(
        "agent.config.models.log",
        lambda level, source, message: calls.append((level, source, message)),
    )
    cfg = _cfg(enabled_tools=["FileEditor", "GlobTool"])

    kept = cfg.get_filtered_tool_classes(allowed_names=frozenset({"FileEditor"}))

    assert kept == [FILE_EDITOR]
    assert [c for c in calls if c[0] == "WARNING"] == []
