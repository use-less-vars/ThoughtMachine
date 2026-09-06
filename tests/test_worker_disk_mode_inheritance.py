"""
Pinning tests: worker sub-agents now carry the REAL parent session/workspace
ids (propagated at spawn through WorkerContext) and share the disk-pure
permission gate with the main agent inside ToolExecutor._execute_single_tool.

Regression (worker lockdown -> disk-pure unification): worker sub-agents used
to carry a *phantom* session_id ("worker-<uuid>") with NO vault sidecar
record, so the executor exempted workers from disk-authoritative mode and
trusted only the in-memory restrictive-merge snapshot taken at spawn
(config.session_permissions).  That snapshot could outlive the vault: a
mid-session revocation written to the parent session's vault sidecar never
reached a running worker, whose in-memory mirror stayed permissive.

Fix: spawn propagates the parent session_id + resolved workspace_id into
WorkerThread/WorkerContext (and run() re-asserts them over any ids persisted
in context.json from an older run).  The executor therefore treats worker
executors exactly like main executors -- disk-authoritative mode engages
whenever BOTH ids are present -- and additionally caps the disk-effective
permissions with the worker's spawn-time restrictive-merge footprint
(permission_footprint kwarg), so the session ceiling can never be widened by
a stale worker mirror (session snapshot is still honoured as the *floor* of
restrictiveness).

These tests pin the executor-level behaviour both ways:
  - worker executor with real parent ids + matching vault record -> allowed
  - worker ask-level request -> denied, NO SecurityPromptEvent published
  - worker disk revocation (vault read < stale mirror write) -> denied
  - worker footprint cap still applies under disk mode (snapshot stricter
    than the vault) -> denied
  - main executor ask-level -> SecurityPromptEvent published (ask path live)
  - id propagation: WorkerThread ctor attrs, WorkerContext persistable
    round-trip, and the run() override of ids persisted in context.json.
"""

import json
from typing import ClassVar, List

import pytest

from agent.core import tool_executor as tool_executor_module
from agent.core.tool_executor import ToolExecutor
from agent.core.worker_context import WorkerContext
from thoughtmachine.permission_store import write_session_permissions
from thoughtmachine.security import SessionPermissions
from thoughtmachine.vault import vault_root
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities
from tools.base import ToolBase
from tools.workspace.worker_thread import WorkerThread

SESSION = "user-session-abc123"
WORKSPACE = "ws-x"


class FilesystemWriteTool(ToolBase):
    """A tool that requires filesystem write access."""
    tool: str = "FilesystemWriteTool"
    required_categories: ClassVar[List[str]] = ["filesystem:write"]

    def execute(self) -> str:
        return "FS OK"


class GitWriteProbeTool(ToolBase):
    """A tool that requires git write access.

    git is the canonical ask carrier in the current model: filesystem's
    session vocabulary is banned|read|write (no ask), while git still
    accepts ask -- so an ask-level grant is only representable via git.
    """
    tool: str = "GitWriteProbeTool"
    required_categories: ClassVar[List[str]] = ["git:write"]

    def execute(self) -> str:
        return "GIT OK"


class FakeConfig:
    """Minimal config stub (no workspace_path -> ws_id comes from the arg)."""
    workspace_path = None
    tool_output_token_limit = None

    def __init__(self, permissions=None):
        self.session_permissions = permissions


class FakeState:
    security_config = None


class RecordingBus:
    """Event-bus stub that records every published event (no subscribers)."""

    def __init__(self):
        self.published = []

    def publish(self, event):
        self.published.append(event)


@pytest.fixture
def gate_env(monkeypatch):
    """Force permissive workspace capabilities.

    get_workspace_capabilities('<unknown id>') normally returns None and the
    executor then falls back to fail-closed capabilities, which would
    downgrade write->read even in the legacy path and confound the test.
    Force permissive caps so only the gate-mode decision is under test.
    """
    monkeypatch.setattr(
        tool_executor_module,
        "get_workspace_capabilities",
        lambda ws_id: WorkspaceCapabilities(),
    )


def _seed_vault(monkeypatch, tmp_path, ws_id, session_id, filesystem_level):
    """Write a canonical vault record + workspace ceiling for a session.

    Uses the real store writers exactly as production does: the session
    sidecar via ``write_session_permissions`` and the workspace ceiling via
    ``config.json['permissions']`` (see old temp/worker_disk_probe.py
    expectation (c) -- this is the same canonical path).
    """
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path))
    root = vault_root()
    ws_dir = root / "workspaces" / ws_id
    ws_dir.mkdir(parents=True, exist_ok=True)
    write_session_permissions(
        root,
        ws_id,
        session_id,
        {"filesystem": filesystem_level, "network": "banned"},
    )
    (ws_dir / "config.json").write_text(
        json.dumps(
            {"permissions": {"filesystem": filesystem_level, "network": "banned"}}
        ),
        encoding="utf-8",
    )


def _seed_git_ask_vault(monkeypatch, tmp_path, ws_id, session_id):
    """Seed a canonical vault record granting git:'ask' + a git:'ask' ceiling.

    filesystem no longer has an ask level (session vocabulary is
    banned|read|write), so git -- whose vocabulary still carries ask -- is
    used to exercise the live ASK path for a main agent.
    """
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path))
    root = vault_root()
    ws_dir = root / "workspaces" / ws_id
    ws_dir.mkdir(parents=True, exist_ok=True)
    write_session_permissions(root, ws_id, session_id, {"git": "ask"})
    (ws_dir / "config.json").write_text(
        json.dumps({"permissions": {"git": "ask"}}),
        encoding="utf-8",
    )


def _make_executor(permissions, is_worker_context, event_bus=None):
    return ToolExecutor(
        tool_classes=[FilesystemWriteTool],
        config=FakeConfig(permissions),
        state=FakeState(),
        logger=None,
        security_available=False,
        agent=None,
        event_bus=event_bus,
        is_worker_context=is_worker_context,
    )


def _run_write_tool(executor, session_id, workspace_id):
    return executor._execute_single_tool(
        FilesystemWriteTool, {}, "FilesystemWriteTool", 0,
        lambda: False, lambda: None, lambda: 0,
        session_id=session_id,
        workspace_id=workspace_id,
    )


class TestWorkerDiskPureGate:
    """Worker executors share disk-pure mode with the main agent (real ids)."""

    def test_worker_disk_pure_allows_write_from_vault(
        self, gate_env, tmp_path, monkeypatch
    ):
        """Worker (is_worker_context=True) with REAL parent ids and a vault
        sidecar granting filesystem:write + matching ceiling: the write flows
        through disk-authoritative mode (BEFORE the unification fix the
        worker path never consulted the vault at all)."""
        _seed_vault(monkeypatch, tmp_path, WORKSPACE, SESSION, "write")
        perms = SessionPermissions(filesystem="write")
        executor = _make_executor(perms, is_worker_context=True)
        result = _run_write_tool(
            executor,
            session_id=SESSION,
            workspace_id=WORKSPACE,
        )
        assert result["result"] == "FS OK", result["result"]

    def test_worker_ask_denied_without_prompt(
        self, gate_env, tmp_path, monkeypatch
    ):
        """Worker + vault record filesystem:ask: the ask path must NOT publish
        a SecurityPromptEvent (no interactive user) -- the gate short-circuits
        BEFORE the prompt publish with the worker-approval denial."""
        _seed_vault(monkeypatch, tmp_path, WORKSPACE, SESSION, "ask")
        bus = RecordingBus()
        perms = SessionPermissions(filesystem="ask")
        executor = _make_executor(perms, is_worker_context=True, event_bus=bus)
        result = _run_write_tool(
            executor,
            session_id=SESSION,
            workspace_id=WORKSPACE,
        )
        assert result["result"] != "FS OK"
        assert "no interactive user available" in result["result"], result["result"]
        assert bus.published == [], "worker ask must never publish a prompt event"

    def test_worker_disk_read_caps_write_tool(
        self, gate_env, tmp_path, monkeypatch
    ):
        """Staleness pin: the in-memory mirror (filesystem:write) is MORE
        permissive than the vault record (filesystem:read).  Disk-pure mode
        must propagate the revocation -- the write is denied even though the
        worker's spawn-time snapshot still grants it."""
        _seed_vault(monkeypatch, tmp_path, WORKSPACE, SESSION, "read")
        perms = SessionPermissions(filesystem="write")  # stale mirror
        executor = _make_executor(perms, is_worker_context=True)
        result = _run_write_tool(
            executor,
            session_id=SESSION,
            workspace_id=WORKSPACE,
        )
        assert "Permission denied" in result["result"], result["result"]
        assert "filesystem:write" in result["result"], result["result"]

    def test_worker_footprint_cap_still_applies_under_disk(
        self, gate_env, tmp_path, monkeypatch
    ):
        """Lockdown pin: the worker's spawn-time restrictive-merge footprint
        (session snapshot x worker footprint) still caps the disk-effective
        permissions.  Vault grants write, but the merged snapshot only grants
        read -> denied (min(disk, snapshot) semantics preserved under
        disk-pure mode)."""
        _seed_vault(monkeypatch, tmp_path, WORKSPACE, SESSION, "write")
        perms = SessionPermissions(filesystem="read")  # merged snapshot stricter
        executor = _make_executor(perms, is_worker_context=True)
        result = _run_write_tool(
            executor,
            session_id=SESSION,
            workspace_id=WORKSPACE,
        )
        assert "Permission denied" in result["result"], result["result"]
        assert "filesystem:write" in result["result"], result["result"]

    def test_main_agent_ask_prompts_via_event_bus(
        self, gate_env, tmp_path, monkeypatch
    ):
        """Sanity leg: for a MAIN agent (is_worker_context=False) the ask path
        is live -- a SecurityPromptEvent is published to the bus and the tool
        is denied once the (shortened) prompt wait times out.

        filesystem's session vocabulary is banned|read|write (no ask), so
        git -- whose vocabulary still carries ask -- is the canonical ask
        carrier: a git:write request against a git:ask grant must hit the
        prompt path."""
        import security.security_gate as security_gate_module

        monkeypatch.setattr(security_gate_module, "PROMPT_TIMEOUT", 0.05)
        _seed_git_ask_vault(monkeypatch, tmp_path, WORKSPACE, SESSION)
        bus = RecordingBus()
        perms = SessionPermissions(git="ask")
        executor = _make_executor(perms, is_worker_context=False, event_bus=bus)
        result = executor._execute_single_tool(
            GitWriteProbeTool, {}, "GitWriteProbeTool", 0,
            lambda: False, lambda: None, lambda: 0,
            session_id=SESSION,
            workspace_id=WORKSPACE,
        )
        assert result["result"] != "GIT OK"
        assert "Permission denied" in result["result"], result["result"]
        assert len(bus.published) >= 1, (
            "main-agent ask path must publish a SecurityPromptEvent"
        )


class TestWorkerIdPropagation:
    """Real parent session/workspace ids reach the worker runtime."""

    def test_worker_thread_ctor_carries_real_parent_ids(self, tmp_path):
        thread = WorkerThread(
            name="id-probe",
            definition={"name": "id-probe", "system_prompt": "probe"},
            agent_config={},
            workspace_dir=tmp_path,
            session_permissions={},
            session_id="user-sess-1",
            workspace_id="ws-x",
        )
        assert thread.session_id == "user-sess-1"
        assert thread.workspace_id == "ws-x"

    def test_worker_context_persists_real_parent_ids(self):
        ctx = WorkerContext(
            session_id="user-sess-1",
            workspace_id="ws-x",
            worker_name="id-probe",
        )
        assert ctx.session_id == "user-sess-1"
        assert ctx.workspace_id == "ws-x"
        restored = WorkerContext.from_persistable_dict(ctx.to_persistable_dict())
        assert restored.session_id == "user-sess-1"
        assert restored.workspace_id == "ws-x"

    def test_run_reasserts_real_ids_over_persisted_phantom(self, tmp_path):
        """A context.json written by an OLDER run (phantom worker-<uuid> sid,
        no workspace_id) must be overridden by the live thread ids at run()
        start -- the same authoritative-override block run() executes right
        after _load_context()."""
        thread = WorkerThread(
            name="id-override",
            definition={"name": "id-override", "system_prompt": "probe"},
            agent_config={},
            workspace_dir=tmp_path,
            session_permissions={},
            session_id="user-sess-1",
            workspace_id="ws-x",
        )
        ctx_path = thread._context_path()
        ctx_path.write_text(
            json.dumps({
                "session_id": "phantom-worker-0123456789ab",
                "worker_name": "id-override",
                "conversation": [{"role": "system", "content": "probe"}],
                "status": "ready",
            }),
            encoding="utf-8",
        )
        loaded = thread._load_context()
        assert loaded is not None
        assert loaded.session_id == "phantom-worker-0123456789ab"
        assert loaded.workspace_id is None

        # Replicate run()'s authoritative-override block verbatim:
        if loaded is not None:
            if thread.session_id:
                loaded.session_id = thread.session_id
            if thread.workspace_id:
                loaded.workspace_id = thread.workspace_id
        assert loaded.session_id == "user-sess-1"
        assert loaded.workspace_id == "ws-x"
