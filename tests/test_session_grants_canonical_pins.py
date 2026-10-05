"""B-pins: session grants are canonical and disk-authoritative.

Pins the permission-simplification contract on the canonical six-resource
grant set (``container | network | filesystem | git | mcp | host_bash``):

B1  The on-disk sidecar (P1) is the SOLE grants source; the session metadata
    (P2) is no longer a grants holder.  A present-but-empty sidecar yields the
    DEFAULT grants (capped by the workspace ceiling); an ABSENT source makes
    ``read_session_permissions`` raise and the disk-mode gate fail CLOSED
    (all-banned).
B2  ``SessionManager`` metadata writes (``save_config_to_session`` /
    ``save_session``) STRIP ``session_permissions`` from ``session_config``.
B3  ``WebAgentBridge.save_session`` strips ``session_permissions`` from the
    persisted session record while KEEPING the rest of the config.
B4  The disk-mode security gate fails CLOSED when the grant-store read raises
    (``read_session_permissions`` patched to raise -> all-banned).
B5  ``migrate_session_permissions`` is idempotent and additive.
"""

import json
import sys
from pathlib import Path

# Add project root so that imports work (same pattern as sibling tests).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from agent.config.session_config import SessionConfig
from session.models import Session
from session.store import FileSystemSessionStore
from security.security_gate import get_effective_permissions
from thoughtmachine import permission_store as ps
from thoughtmachine.permission_store import (
    PermissionStoreError,
    migrate_session_permissions,
    read_session_permissions,
    session_grants_path,
)
from thoughtmachine.security import SessionPermissions
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities
from web_ui.backend.bridge import WebAgentBridge
from web_ui.backend.config_manager import ConfigManager
from web_ui.backend.session_manager import SessionManager

FULL_PERMS = {
    "container": False,
    "network": "ask",
    "filesystem": "write",
    "git": "read",
    "mcp": "banned",
    "host_bash": "banned",
}


def _store(vault):
    return FileSystemSessionStore(
        sessions_dir=str(vault / "sessions"),
        state_dir=str(vault / "state"),
    )


def _write_ceiling(vault, ws_id):
    ws_dir = vault / "workspaces" / ws_id
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "config.json").write_text(
        json.dumps(
            {"purpose": "coding", "permissions": {"filesystem": "write", "git": "write"}}
        )
    )


def _gate(sid, ws_id):
    return get_effective_permissions(
        SessionPermissions(),
        WorkspaceCapabilities(
            git_available=True,
            filesystem_write=True,
            allow_docker=True,
            allow_network=True,
        ),
        None,
        session_id=sid,
        workspace_id=ws_id,
    )


# ── B1: sidecar is the sole grants source ───────────────────────────────────

class TestSidecarSoleGrantsSource:
    def test_present_empty_sidecar_yields_defaults(self, hermetic_vault):
        vault = hermetic_vault
        ws_id = "ws-b1-empty"
        _write_ceiling(vault, ws_id)
        sid = "sess-b1-empty"
        # Present-but-empty sidecar == a present empty grant set.
        path = session_grants_path(vault, ws_id, sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")

        assert read_session_permissions(vault, ws_id, sid) == {}

        effective = _gate(sid, ws_id)
        # Defaults: filesystem/git are read; the write ceiling never RAISES them.
        assert effective["filesystem"] == "read"
        assert effective["git"] == "read"
        # Deny-by-default resources stay banned.
        assert effective["network"] == "banned"
        assert effective["host_bash"] == "banned"
        assert effective["container"] is False

    def test_absent_source_raises_and_gate_fails_closed(self, hermetic_vault):
        vault = hermetic_vault
        ws_id = "ws-b1-absent"
        _write_ceiling(vault, ws_id)
        sid = "sess-b1-absent"

        # No sidecar and no matching session record -> fail closed on read.
        with pytest.raises(PermissionStoreError):
            read_session_permissions(vault, ws_id, sid)

        effective = _gate(sid, ws_id)
        assert effective["filesystem"] == "banned"
        assert effective["git"] == "banned"
        assert effective["network"] == "banned"
        assert effective["host_bash"] == "banned"
        assert effective["mcp"] == "banned"
        assert effective["container"] is False


# ── B2: SessionManager metadata writes strip session_permissions ─────────────

class TestSessionManagerStripsGrantFromMetadata:
    def test_save_config_to_session_strips_session_permissions(self, tmp_path):
        store = _store(tmp_path)
        mgr = SessionManager(store, ConfigManager())
        session = Session(
            session_id="sess-b2-config",
            user_history=[],
            workspace_id=None,
            metadata={
                "source": "web_ui",
                "session_config": {"mode": "agent", "session_permissions": dict(FULL_PERMS)},
            },
        )
        mgr.save_config_to_session(
            session, SessionConfig(mode="agent", session_permissions={"git": "write"})
        )

        reloaded = store.load_session("sess-b2-config")
        assert reloaded is not None
        stored = reloaded.metadata["session_config"]
        assert "session_permissions" not in stored

    def test_save_session_strips_session_permissions(self, tmp_path):
        store = _store(tmp_path)
        mgr = SessionManager(store, ConfigManager())
        session = Session(
            session_id="sess-b2-save",
            user_history=[],
            workspace_id=None,
            metadata={
                "source": "web_ui",
                "session_config": {"mode": "agent", "session_permissions": dict(FULL_PERMS)},
            },
        )
        mgr.save_session(session, session_config=SessionConfig(mode="agent"))

        reloaded = store.load_session("sess-b2-save")
        stored = reloaded.metadata["session_config"]
        assert "session_permissions" not in stored


# ── B3: bridge save strips the grant but keeps the rest of the config ───────

class TestBridgeSaveStripsGrantFromMetadata:
    def test_bridge_save_keeps_config_drops_grant(self, tmp_path):
        store = _store(tmp_path)
        bridge = WebAgentBridge(event_callback=lambda e: None, session_store=store)
        bridge.apply_config(
            {
                "session_permissions": {"filesystem": "banned"},
                "temperature": 0.42,
            }
        )
        saved = bridge.save_session()
        assert saved is not None

        path = store._find_session_path(saved.session_id)
        assert path is not None, "session file not found on disk"
        raw = json.loads(path.read_text())
        stored = raw["metadata"]["session_config"]

        # The grant is NOT persisted in the session record (P2 is not a grants
        # holder) ...
        assert "session_permissions" not in stored
        # ... but the rest of the config still round-trips.
        assert stored.get("temperature") == 0.42


# ── B4: disk-mode gate fails CLOSED when the grant read raises ──────────────

class TestDiskModeGateFailsClosed:
    def test_gate_all_banned_when_read_raises(self, hermetic_vault, monkeypatch):
        vault = hermetic_vault
        ws_id = "ws-b4"
        _write_ceiling(vault, ws_id)
        sid = "sess-b4"

        def _boom(*a, **k):
            raise PermissionStoreError("simulated unreadable grant store")

        # The gate lazily imports the symbol at call time, so patch the
        # attribute on the module the lazy import resolves.
        monkeypatch.setattr(ps, "read_session_permissions", _boom)

        effective = _gate(sid, ws_id)
        assert effective["filesystem"] == "banned"
        assert effective["git"] == "banned"
        assert effective["network"] == "banned"
        assert effective["mcp"] == "banned"
        assert effective["host_bash"] == "banned"
        assert effective["container"] is False

    def test_missing_grant_store_does_not_silently_allow(self, hermetic_vault, monkeypatch):
        """An absent/unreadable permission store must NOT silently allow a tool.

        Disk-mode gate reads fail CLOSED: driving a REAL tool execution whose
        required category (``mcp:connect``) the fail-closed deny-all does not
        grant, the caller receives a non-empty denial -- never a silent allow.

        Disclosed residual: the deny-all / fail-closed CAUSE (the unreadable
        vault store) is internal-only -- it is surfaced only as a
        ``logger.warning`` plus the deny-all sentinel inside
        ``security/security_gate.py``; the operator-facing text does NOT name
        it, only the resulting ``mcp:banned`` denial.
        """
        from agent.config.models import AgentConfig
        from agent.core.state import AgentState
        from agent.core.tool_executor import ToolExecutor
        from tools.mcp_server_connect import MCPServerConnect

        vault = hermetic_vault
        ws_id = "ws-b4"
        _write_ceiling(vault, ws_id)
        sid = "sess-b4"

        def _boom(*a, **k):
            raise PermissionStoreError("simulated unreadable grant store")

        # Same idiom as the gate-only test above: the gate lazily imports the
        # symbol at call time, so patch it on the module the lazy import
        # resolves.  ws_id is passed explicitly so the executor reaches its
        # disk-mode branch (session_id AND ws_id both non-empty).
        monkeypatch.setattr(ps, "read_session_permissions", _boom)

        cfg = AgentConfig(session_permissions=SessionPermissions())
        executor = ToolExecutor(
            tool_classes=[MCPServerConnect],
            config=cfg,
            state=AgentState(config=cfg),
            logger=None,
            security_available=True,
            agent=None,
        )
        result = executor._execute_single_tool(
            MCPServerConnect,
            {"server_name": "x"},
            "MCPServerConnect",
            0,
            lambda: False,
            lambda: None,
            lambda: 0,
            session_id=sid,
            workspace_id=ws_id,
        )["result"]

        # DENIED (no silent allow): a non-empty denial naming the required
        # category and the fail-closed ``banned`` value it resolved to.
        assert result  # never empty / never a silent success
        assert result.startswith("Permission denied:")
        assert "mcp:connect" in result
        assert "mcp:banned" in result


# ── B5: migrate_session_permissions is idempotent and additive ──────────────

class TestMigrateSessionPermissions:
    def test_migrate_writes_sidecar_then_is_idempotent(self, hermetic_vault):
        vault = hermetic_vault
        ws_id = "ws-b5"
        _write_ceiling(vault, ws_id)
        store = _store(vault)
        legacy = {
            "git_write": "write",  # legacy grain, folds onto canonical git
            "filesystem": "write",
            "root_shell": "read",  # non-catalog -> dropped on normalise
        }
        sess = Session(
            workspace_id=ws_id,
            metadata={
                "name": "migrate-me",
                "session_config": {"mode": "agent", "session_permissions": legacy},
            },
        )
        store.save_session(sess, workspace_id=ws_id)

        sidecar = session_grants_path(vault, ws_id, sess.session_id)
        assert not sidecar.exists()

        # First migration writes the sidecar (additive) -> True.
        assert migrate_session_permissions(vault, ws_id, sess.session_id) is True
        assert sidecar.exists()
        doc = json.loads(sidecar.read_text())
        # Canonical shape: git_write folded to git=write, non-catalog dropped.
        assert doc.get("git") == "write"
        assert doc.get("filesystem") == "write"
        assert "git_write" not in doc and "root_shell" not in doc

        # Second migration is a no-op: the sidecar is authoritative -> False.
        assert migrate_session_permissions(vault, ws_id, sess.session_id) is False

    def test_migrate_no_legacy_returns_false(self, hermetic_vault):
        vault = hermetic_vault
        ws_id = "ws-b5-nolegacy"
        _write_ceiling(vault, ws_id)
        store = _store(vault)
        sess = Session(
            workspace_id=ws_id,
            metadata={"name": "no-legacy", "session_config": {"mode": "agent"}},
        )
        store.save_session(sess, workspace_id=ws_id)

        assert migrate_session_permissions(vault, ws_id, sess.session_id) is False
        assert not session_grants_path(vault, ws_id, sess.session_id).exists()

    def test_migrate_without_record_raises(self, hermetic_vault):
        vault = hermetic_vault
        ws_id = "ws-b5-missing"
        _write_ceiling(vault, ws_id)
        with pytest.raises(PermissionStoreError):
            migrate_session_permissions(vault, ws_id, "does-not-exist")


# ── B6: presenter metadata dump is not a grants holder ──────────────────────

class TestPresenterMetadataStripsGrant:
    """The presenter's ``metadata['agent_config']`` dump must exclude
    ``session_permissions`` (the sidecar is the sole grants source) while
    keeping the rest of the config.  Exercises the real
    ``SessionLifecycle._build_session_from_current_state`` path."""

    def test_build_session_drops_grant_keeps_config(self, hermetic_vault):
        from types import SimpleNamespace

        from agent.config import AgentConfig
        from agent.presenter.session_lifecycle import SessionLifecycle

        sid = "sess-b6-presenter"
        frozen = AgentConfig(
            temperature=0.42,
            session_permissions={"git": "write", "filesystem": "write"},
        )

        class _FakeBridge:
            current_config = SimpleNamespace(workspace_path=None)
            current_session_id = sid
            user_history = []
            session_name = None
            total_input = 0
            total_output = 0
            context_length = 0
            _external_file_path = None

            def __init__(self):
                self.current_session = Session(
                    session_id=sid, user_history=[], metadata={"name": "x"}
                )

            def create_agent_config(self, mode=None):
                return frozen

        lc = SessionLifecycle(_FakeBridge(), None)
        built = lc._build_session_from_current_state()
        assert built is not None

        stored_ac = built.metadata["agent_config"]
        # The grant is NOT persisted in the presenter metadata ...
        assert "session_permissions" not in stored_ac
        # ... but the rest of the config is (proves the exclude set is exact).
        assert stored_ac.get("temperature") == 0.42

        # And the stripped form is what actually lands on disk.
        lc.session_store.save_session(built)
        reloaded = lc.session_store.load_session(built.session_id)
        assert reloaded is not None
        disk_ac = reloaded.metadata["agent_config"]
        assert "session_permissions" not in disk_ac
        assert disk_ac.get("temperature") == 0.42


# ── B7: direct-call git-write grant is coerced before the gate reads it ─────

class TestGitWriteDirectCallCoercesGrant:
    """A direct caller's ``agent_config['session_permissions']`` is coerced
    through the canonical coercer before the git-write gate reads it, so an
    invalid / non-canonical value fails CLOSED instead of being trusted."""

    FLAG = 'Error: git:write denied: session git_write permission is not "write"'

    @staticmethod
    def _tool(raw):
        from tools.git_write_tool import GitWriteTool

        return GitWriteTool(
            operation="commit",
            message="agent commit",
            agent_config={"session_permissions": raw},
        )

    @pytest.mark.parametrize(
        "raw, allowed, restricted",
        [
            ({"git": "write"}, True, False),
            ({"git": "write_on_feature_branch"}, True, True),
        ],
    )
    def test_canonical_grants_are_accepted(self, raw, allowed, restricted):
        t = self._tool(raw)
        assert t._git_write_allowed() is allowed
        assert t._git_write_restricted_to_feature_branch() is restricted

    @pytest.mark.parametrize(
        "raw",
        [
            {"git": "full"},      # non-canonical level the gate must NOT trust
            {"git": "root"},      # unknown level
            {"git": 123},         # wrong type
            {"filesystem": "write"},  # partial map -> git defaults to "read"
            "nope",               # non-dict
            42,                   # non-dict
            None,                 # non-dict
        ],
    )
    def test_non_canonical_or_partial_grant_fails_closed(self, raw):
        t = self._tool(raw)
        assert t._git_write_allowed() is False
        assert t._git_write_restricted_to_feature_branch() is False
        # execute() short-circuits on the gate for a non-writable grant.
        assert t.execute() == self.FLAG



# ── Fail-closed surfacing: the deny NAMES the outcome (no silent deny) ─────

class TestFailClosedSurfaceIsNamed:
    """The executor's gate-unavailable fail-closed deny must NAME the outcome.

    When the security gate cannot be resolved, ``ToolExecutor._execute_single_tool``
    denies the tool AND the message the caller receives says so explicitly
    (``fail-closed``) rather than silently denying.  Dropping the wording (or
    reverting the branch to a bare/un-named deny) makes this test RED.
    """

    def test_tool_result_names_fail_closed_when_gate_unavailable(self, monkeypatch):
        import agent.core.tool_executor as te
        from agent.config.models import AgentConfig
        from agent.core.state import AgentState
        from agent.core.tool_executor import ToolExecutor
        from tools.file_preview_tool import FilePreviewTool

        # The gate genuinely cannot be (re)bound at first entry -> the executor
        # must deny (fail CLOSED) instead of executing the tool ungated.
        monkeypatch.setattr(te, "GATE_AVAILABLE", False)
        monkeypatch.setattr(te, "_ensure_gate_imported", lambda: False)

        cfg = AgentConfig(session_permissions=SessionPermissions())
        executor = ToolExecutor(
            tool_classes=[FilePreviewTool],
            config=cfg,
            state=AgentState(config=cfg),
            logger=None,
            security_available=False,
            agent=None,
        )
        # A REAL tool execution through the executor's gate entry.
        result = executor._execute_single_tool(
            FilePreviewTool,
            {"filename": "x.txt"},
            "FilePreviewTool",
            0,
            lambda: False,
            lambda: None,
            lambda: 0,
        )["result"]

        # The operator sees the fail-closed outcome NAMED.
        assert "fail-closed" in result, result
        assert "security gate unavailable" in result, result


# ── Q2-hop: workspace stored ceiling reaches the executor's config ──────────


class TestWorkspaceCeilingHopIntoExecutor:
    """workspace config.json stored ceiling -> resolve_full_config
    (apply_workspace_ceiling @ config_manager.py:973) -> session_config_from_merged
    -> SessionConfig.to_agent_config -> AgentConfig -> ToolExecutor.config.

    The executor consults ``self.config.session_permissions`` (tool_executor.py,
    the read preceding the gate call).  Neutering the upstream cap call at
    config_manager.py:973 leaves the executor reading the UNCAPPED grant -> RED.
    """

    def test_workspace_ceiling_reaches_executor_config(self, hermetic_vault):
        from agent.config.models import AgentConfig
        from agent.core.state import AgentState
        from agent.core.tool_executor import ToolExecutor
        from web_ui.backend.config_manager import (
            resolve_full_config,
            session_config_from_merged,
        )

        vault = hermetic_vault
        ws_id = "ws-q2-hop"
        sid = "sess-q2-hop"

        # The workspace's OWN stored ceiling RESTRICTS git to read...
        ws_dir = vault / "workspaces" / ws_id
        ws_dir.mkdir(parents=True, exist_ok=True)
        (ws_dir / "config.json").write_text(
            json.dumps({"purpose": "general", "permissions": {"git": "read"}})
        )

        # ...while the session's stored grant (P1 sidecar) asks for MORE.
        sidecar = session_grants_path(vault, ws_id, sid)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(json.dumps({"git": "write", "filesystem": "write"}))

        # Upstream route: merge every layer + apply the workspace ceiling last.
        merged = resolve_full_config(workspace_id=ws_id, session_id=sid)
        assert merged["session_permissions"]["git"] == "read"

        # The EXECUTOR's own config is built from that merged value.
        agent_cfg = session_config_from_merged(merged).to_agent_config()
        executor = ToolExecutor(
            tool_classes=[],
            config=agent_cfg,
            state=AgentState(config=agent_cfg),
            logger=None,
            security_available=True,
            agent=None,
        )

        # Exactly what the executor consults (tool_executor.py
        # ``session_perms_obj = self.config.session_permissions``).
        read = executor.config.session_permissions
        if hasattr(read, "to_dict"):
            read = read.to_dict()
        elif hasattr(read, "model_dump"):
            read = read.model_dump()
        assert read["git"] == "read", read

