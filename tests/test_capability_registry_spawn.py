"""Spawn-time capability freeze for worker templates.

A worker's tool set is a *declaration*, resolved once at spawn into a frozen
capability set, and that set must be:

C1. exactly the declared subset (never wider, never silently narrower),
C2. never wider than the parent session's ``enabled_tools``,
C3. fail-loud when the declaration names a tool this build does not ship
    (no "spawn succeeded, tool quietly missing"),
C4. blocklist-safe: ``_WORKER_BLOCKLIST`` members can never be constructed by
    a worker, whether declared explicitly or inherited from the implicit
    "all simplified tools" default,
C5. name-alias aware: a template that declares a tool's stable *tool name*
    (e.g. ``git_read``) must resolve, not be classified "not shipped".

Hermetic by construction: the spawn plumbing (workspace dir, worker registry,
``WorkerThread``) is mocked; the ``WorkerThread`` instances used to read
``AgentConfig.enabled_tools`` are real but never started (no threads, no
containers, no network, no LLM calls).

NOTE ON SCOPE: the per-tool permission-footprint gate
(``check_required_categories``) is already pinned by
tests/test_worker_permission_hardening.py; it is neutralised in the harness
below so a footprint mismatch can never silently strip a tool and mask the
capability-resolution behaviour under test.

These tests describe the *target* contract. Where the capability-registry
wiring has not landed in ``tools/workspace/worker_thread.py`` yet, the
corresponding test is expected to fail for that reason alone.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import tools as tools_pkg
from tools.workspace.worker import Worker
from tools.workspace.worker_thread import _WORKER_BLOCKLIST
from tools.workspace.worker_thread import WorkerThread as RealWorkerThread

SESSION_ID = "sess-capability-freeze"
WS_ID = "ws_capability_freeze"

#: The parent agent config every spawn in this suite runs under.
PARENT_CFG = {"provider": "openai", "model": "gpt-4o"}

#: Clock fields the spawn hop reports in the envelope alongside the outcome.
BUDGET_KEYS = frozenset({
    "requested_timeout_seconds",
    "effective_timeout_seconds",
    "timeout_clamped",
    "timeout_clamped_by",
    "timeout_source",
})

#: Fields every definition carries, so a missing key can never be mistaken for
#: the behaviour under test.
BASE_DEFINITION = {
    "status": "ready",
    "permission_footprint": {},
    "system_prompt": "capability-freeze fixture",
}


def _definition(name, tools):
    definition = dict(BASE_DEFINITION)
    definition["name"] = name
    definition["tools"] = list(tools)
    return definition


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@pytest.fixture
def spawn_harness(monkeypatch):
    """Registry snapshot + patched spawn plumbing (mirrors the max-workers suite)."""
    from tools.workspace.worker_registry import WorkerRegistry

    registry = WorkerRegistry.get_instance()
    with registry._registry_lock:
        old_registry = dict(registry._worker_registry)
        registry._worker_registry.clear()

    monkeypatch.setattr(
        "tools.workspace.worker.resolve_workspace_id",
        lambda ws: WS_ID,
    )
    mock_dir = MagicMock()
    mock_file = MagicMock()
    mock_file.exists.return_value = True
    mock_dir.__truediv__.return_value = mock_file
    monkeypatch.setattr("tools.workspace.worker._workspace_dir", lambda ws: mock_dir)

    # The spawned thread is a stub: we assert on the constructor kwargs, never
    # on a live worker (no thread is started, no provider is contacted).
    thread_cls = MagicMock()
    monkeypatch.setattr("tools.workspace.worker.WorkerThread", thread_cls)

    # Out of scope here (covered elsewhere): a footprint/effective-permission
    # mismatch must not be able to strip a tool and mask capability resolution.
    monkeypatch.setattr(
        "tools.workspace.worker.check_required_categories",
        MagicMock(return_value=(True, None)),
    )

    harness = SimpleNamespace(
        registry=registry,
        mock_dir=mock_dir,
        mock_file=mock_file,
        thread_cls=thread_cls,
    )

    def set_definitions(definitions):
        mock_file.read_text.return_value = json.dumps(definitions)

    harness.set_definitions = set_definitions
    harness.set_definitions([_definition("hw1", [])])

    yield harness

    with registry._registry_lock:
        registry._worker_registry.clear()
        registry._worker_registry.update(old_registry)


def _spawn_worker(harness, name, agent_config=None, session_permissions=None, force=False):
    """Run ``Worker(action="spawn")`` through the harness; return the decoded envelope."""
    tool = Worker(
        action="spawn",
        worker_name=name,
        session_id=SESSION_ID,
        workspace_path="/tmp/test_ws",
        agent_config=agent_config or dict(PARENT_CFG),
        session_permissions=session_permissions,
        force=force,
    )
    return json.loads(tool.execute())


def _worker_enabled_tools(tmp_path, definition, agent_config):
    """``enabled_tools`` on the *real* worker AgentConfig for *definition*.

    Constructs a genuine ``WorkerThread`` (imported from worker_thread, so the
    harness' ``tools.workspace.worker.WorkerThread`` stub does not shadow it)
    and reads the config the worker would actually run with. Nothing is started.
    """
    thread = RealWorkerThread(
        name="cap-worker",
        definition=copy.deepcopy(definition),
        agent_config=dict(agent_config),
        workspace_dir=tmp_path,
        session_permissions={},
    )
    cfg = thread._build_agent_config()
    assert cfg is not None, "WorkerThread._build_agent_config() returned None"
    return list(getattr(cfg, "enabled_tools", None) or [])


def _granted_tool_classes(harness):
    """The tool_classes mapping handed to the spawned WorkerThread."""
    return harness.thread_cls.call_args.kwargs.get("tool_classes") or {}


# ---------------------------------------------------------------------------
# C1. Declared subset is frozen at spawn
# ---------------------------------------------------------------------------


def test_subset_frozen_at_spawn(tmp_path, spawn_harness):
    """A declared template tool list is granted verbatim -- no more, no less."""
    declared = ["FileEditor", "DateTimeTool"]
    definition = _definition("frozen", declared)
    spawn_harness.set_definitions([definition])

    result = _spawn_worker(spawn_harness, "frozen")
    assert result.get("spawned") is True, result

    granted = _granted_tool_classes(spawn_harness)
    assert set(granted) == set(declared), granted
    # Each granted entry is the real class for that name, not a placeholder.
    for tool_name, cls in granted.items():
        assert getattr(cls, "__name__", None) == tool_name or cls.tool_name() == tool_name

    enabled = _worker_enabled_tools(tmp_path, definition, PARENT_CFG)
    assert set(enabled) == set(declared), enabled
    assert len(enabled) == len(declared), "duplicate tools leaked into enabled_tools"


# ---------------------------------------------------------------------------
# C2. Never wider than the parent
# ---------------------------------------------------------------------------


def test_subset_never_wider_than_parent(tmp_path):
    """The parent's enabled_tools is a ceiling the declaration cannot exceed."""
    definition = _definition("narrow", ["FileEditor", "DateTimeTool", "Respond"])
    parent_cfg = dict(PARENT_CFG, enabled_tools=["FileEditor", "GlobTool"])

    enabled = _worker_enabled_tools(tmp_path, definition, parent_cfg)

    assert set(enabled) == {"FileEditor"}, enabled
    assert "DateTimeTool" not in enabled
    assert "Respond" not in enabled
    assert set(enabled) <= set(parent_cfg["enabled_tools"])


# ---------------------------------------------------------------------------
# C3. Fail loud on a tool this build does not ship
# ---------------------------------------------------------------------------


def test_spawn_fails_loudly_on_missing_tool(spawn_harness):
    """An unshippable tool name aborts the spawn with a structured error."""
    missing = ["AlphaToolNotShipped", "ZetaToolNotShipped"]
    definition = _definition("loud", ["FileEditor", *missing])
    spawn_harness.set_definitions([definition])

    result = _spawn_worker(spawn_harness, "loud")

    # Failure envelope: these three keys and no success payload alongside; the
    # TimeBudget clock fields are tolerated because the spawn hop reports its
    # budget in the envelope too.
    assert BUDGET_KEYS <= set(result), result
    assert set(result) - BUDGET_KEYS == {
        "error",
        "worker_name",
        "missing_tools",
    }, result
    assert result["worker_name"] == "loud"
    assert result.get("spawned") is not True
    assert result["missing_tools"] == sorted(missing)
    assert "FileEditor" not in result["missing_tools"]

    message = result["error"]
    assert "loud" in message, message
    for name in missing:
        assert name in message, message
    assert "FileEditor" not in message
    # Wording-agnostic ("shippable" / "not shipped" / "does not ship"): the
    # contract is that the message names the build, not an exact phrase.
    assert re.search(r"ship+(ed|able)?", message.lower()), message

    # Nothing was constructed, so no half-capable worker can ever start.
    spawn_harness.thread_cls.assert_not_called()


# ---------------------------------------------------------------------------
# C4. Blocklisted tools are never constructible
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "declared",
    [["Worker", "DateTimeTool"], []],
    ids=["explicit", "implicit"],
)
def test_blocklisted_tool_never_constructible(tmp_path, spawn_harness, caplog, declared):
    """Blocklisted tools are excluded from both explicit and implicit tool sets."""
    definition = _definition("safe", declared)
    spawn_harness.set_definitions([definition])

    caplog.set_level(logging.WARNING)
    result = _spawn_worker(spawn_harness, "safe")

    # Exclusion is not a spawn failure: the worker runs with the rest.
    assert result.get("spawned") is True, result

    granted = _granted_tool_classes(spawn_harness)
    for blocked in _WORKER_BLOCKLIST:
        assert blocked not in granted, granted

    enabled = _worker_enabled_tools(tmp_path, definition, PARENT_CFG)
    for blocked in _WORKER_BLOCKLIST:
        assert blocked not in enabled, enabled

    if declared:
        # An explicit blocklisted declaration must be reported to the operator,
        # through either channel (a warning record or the spawn envelope).
        notice = caplog.text + json.dumps(result)
        assert "excluded for worker safety" in notice, notice


# ---------------------------------------------------------------------------
# C4 (warning half). The implicit default is announced, with its size
# ---------------------------------------------------------------------------


def test_implicit_tools_list_warns_with_size(tmp_path, spawn_harness, caplog):
    """An empty declaration means "all simplified tools" -- and says so."""
    definition = _definition("implicit", [])
    spawn_harness.set_definitions([definition])

    caplog.set_level(logging.WARNING)
    result = _spawn_worker(spawn_harness, "implicit")
    assert result.get("spawned") is True, result

    # The implicit default is "every simplified tool minus the blocklist". The
    # granted set is compared as a SUBSET of it: the parent AgentConfig allowlist
    # may drop individual entries downstream, which is the parent's business and
    # not the capability-resolution contract under test here (exact-set equality
    # for explicit declarations is pinned by test_subset_frozen_at_spawn).
    available = {
        cls.__name__ for cls in tools_pkg.simplified_tool_classes()
    } - set(_WORKER_BLOCKLIST)
    enabled = _worker_enabled_tools(tmp_path, definition, PARENT_CFG)
    assert enabled, "empty declaration granted no tools at all"
    assert set(enabled) <= available, sorted(set(enabled) - available)
    assert not set(enabled) & set(_WORKER_BLOCKLIST)

    implicit_records = [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING
        and "implicit" in record.getMessage().lower()
    ]
    assert implicit_records, (
        "spawning a worker with an empty tool list granted "
        f"{len(enabled)} tools without any warning naming the implicit grant"
    )
    # The announced size is the resolved capability count; accept the declared
    # default size too, since the parent allowlist may filter entries downstream.
    sizes = {len(enabled), len(available)}
    assert any(
        any(str(size) in record.getMessage() for size in sizes)
        for record in implicit_records
    ), [record.getMessage() for record in implicit_records]


# ---------------------------------------------------------------------------
# C5. A stable tool name is not an unshippable name
# ---------------------------------------------------------------------------


def test_tool_name_alias_is_not_unshippable(spawn_harness):
    """Declaring the stable tool name (``git_read``) must resolve, not be dropped.

    ``_TOOL_REGISTRY`` is keyed on ``cls.__name__`` (``GitReadTool``) while the
    tool's stable name is ``git_read``; a template that declares the stable
    name is currently classified "not shipped" and the tool vanishes. Asserting
    only "spawn did not fail" would pass while the tool is silently dropped, so
    the granted mapping is inspected directly.
    """
    definition = _definition("alias", ["git_read"])
    definition["permission_footprint"] = {"git": "read"}
    spawn_harness.set_definitions([definition])

    result = _spawn_worker(
        spawn_harness, "alias", session_permissions={"git": "read"}
    )

    assert "error" not in result, result
    assert result.get("spawned") is True, result

    granted = _granted_tool_classes(spawn_harness)
    assert granted, "declared tool 'git_read' was silently dropped at spawn"
    names = {cls.tool_name() for cls in granted.values()}
    assert "git_read" in names, names
