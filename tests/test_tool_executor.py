"""
Tests for tool_executor.py — permission gate integration tests.

Covers:
  - Integration with ToolExecutor._execute_single_tool
  - Empty required_categories passes
  - Denied categories produce the right error message
"""

import json
import os
from typing import ClassVar, List

import pytest

from agent.core.tool_executor import (
    DEFAULT_SESSION_PERMISSIONS,
    ToolExecutor,
)
from tools.base import ToolBase
from agent.logging import lifecycle


# ---------------------------------------------------------------------------
# Stub tools for testing
# ---------------------------------------------------------------------------

class PermissiveTool(ToolBase):
    """A tool that requires no special permissions."""
    tool: str = "PermissiveTool"
    required_categories: ClassVar[List[str]] = []

    def execute(self) -> str:
        return "OK"


class ContainerTool(ToolBase):
    """A tool that requires container access."""
    tool: str = "ContainerTool"
    required_categories: ClassVar[List[str]] = ["container:true"]

    def execute(self) -> str:
        return "Container OK"


class NetworkAndFilesystemTool(ToolBase):
    """A tool that requires both network and filesystem:write."""
    tool: str = "NetworkAndFilesystemTool"
    required_categories: ClassVar[List[str]] = ["network:true", "filesystem:write"]

    def execute(self) -> str:
        return "Network + FS OK"


# ---------------------------------------------------------------------------
# Integration with ToolExecutor._execute_single_tool
# ---------------------------------------------------------------------------

class FakeConfig:
    """Minimal config stub for ToolExecutor."""
    workspace_path = None
    tool_output_token_limit = None

    def __init__(self, permissions=None):
        self.session_permissions = permissions


class FakeState:
    """Minimal state stub."""
    security_config = None


class TestToolExecutorPermissions:
    """Integration tests: ToolExecutor rejects/accepts tools based on categories."""

    def _make_executor(self, tool_classes, permissions=None):
        # The permissions MUST be wired into the executor's config so the
        # gate reads config.session_permissions (tool_executor.py:253-257).
        return ToolExecutor(
            tool_classes=tool_classes,
            config=FakeConfig(permissions),
            state=FakeState(),
            logger=None,
            security_available=False,
            agent=None,
        )

    def test_permissive_tool_runs(self):
        executor = self._make_executor([PermissiveTool])
        result = executor._execute_single_tool(
            PermissiveTool, {}, "PermissiveTool", 0,
            lambda: False, lambda: None, lambda: 0
        )
        assert result["result"] == "OK"
        assert result["tool_type"] == "normal"

    def test_container_tool_denied(self):
        executor = self._make_executor([ContainerTool], permissions=SessionPermissions(container=False))
        result = executor._execute_single_tool(
            ContainerTool, {}, "ContainerTool", 0,
            lambda: False, lambda: None, lambda: 0
        )
        assert "Permission denied" in result["result"]
        assert "container:true" in result["result"]
        assert result["tool_type"] == "normal"

    def test_multiple_requirements(self):
        # Explicit restrictive profile: both network:true and filesystem:write denied
        executor = self._make_executor(
            [NetworkAndFilesystemTool],
            permissions=SessionPermissions(network=False, filesystem="read"),
        )
        result = executor._execute_single_tool(
            NetworkAndFilesystemTool, {}, "NetworkAndFilesystemTool", 0,
            lambda: False, lambda: None, lambda: 0
        )
        # Both network:true and filesystem:write are denied in default profile
        assert "Permission denied" in result["result"]
        assert result["tool_type"] == "normal"

    def test_respects_custom_permissions_via_check_permissions(self):
        """If we monkey-patch _check_permissions, the executor still works.
        This tests the integration point. We use the ToolExecutor with a tool
        that requires nothing and verify it passes through even under a
        restrictive explicit profile."""
        executor = self._make_executor([PermissiveTool], permissions=SessionPermissions(filesystem="read"))
        result = executor._execute_single_tool(
            PermissiveTool, {}, "PermissiveTool", 0,
            lambda: False, lambda: None, lambda: 0
        )
        assert result["result"] == "OK"

    def test_error_returned_as_normal_tool_type(self):
        """Permission errors must be returned with tool_type='normal' so the LLM
        sees them as ordinary tool failures."""
        executor = self._make_executor([ContainerTool], permissions=SessionPermissions(container=False))
        result = executor._execute_single_tool(
            ContainerTool, {}, "ContainerTool", 0,
            lambda: False, lambda: None, lambda: 0
        )
        assert result["tool_type"] == "normal"
        # Actual executor-path message (verified at runtime in the container):
        # 'Permission denied: Tool requires container:true, but the effective
        #  grant is container:False (denied by: session grant)'
        assert result["result"].startswith("Permission denied: Tool requires container:true")


# ---------------------------------------------------------------------------
# workspace_path is an infrastructure field: the executor config always wins
# ---------------------------------------------------------------------------

class _WorkspaceEchoTool(ToolBase):
    """Echoes the workspace_path it was instantiated with."""
    tool: str = "WorkspaceEchoTool"
    required_categories: ClassVar[List[str]] = []

    def execute(self) -> str:
        return self.workspace_path or "<none>"


class TestWorkspacePathInjection:
    """A caller-supplied workspace_path must never win over the config."""

    def _make_executor(self, workspace_path):
        config = FakeConfig(None)
        config.workspace_path = workspace_path
        return ToolExecutor(
            tool_classes=[_WorkspaceEchoTool],
            config=config,
            state=FakeState(),
            logger=None,
            security_available=False,
            agent=None,
        )

    @pytest.mark.parametrize(
        "configured, expected",
        [("/tmp/real", "/tmp/real"), (None, "<none>")],
    )
    def test_executor_overwrites_caller_workspace_path(self, configured, expected):
        # The caller (LLM) asks for '/' in both cases; the executor config wins,
        # including when it is unset -- then the tool must see no workspace, not
        # the caller's arbitrary path.
        executor = self._make_executor(configured)
        result = executor._execute_single_tool(
            _WorkspaceEchoTool,
            {"workspace_path": "/"},
            "WorkspaceEchoTool",
            0,
            lambda: False,
            lambda: None,
            lambda: 0,
        )
        assert result["result"] == expected


# ---------------------------------------------------------------------------
# Dynamic category resolution (get_required_categories with params)
# ---------------------------------------------------------------------------

class StubFileEditor(ToolBase):
    """A FileEditor-like tool with operation-level categories."""
    tool: str = "StubFileEditor"
    required_categories: ClassVar[List[str]] = ["filesystem:write"]

    @classmethod
    def get_required_categories(cls, params: dict | None = None) -> list[str]:
        if params and params.get("operation") in ("read", "grep"):
            return ["filesystem:read"]
        return ["filesystem:write"]

    def execute(self) -> str:
        return "OK"


class StubRespondTool(ToolBase):
    """A Respond-like tool with dynamic categories based on report_body."""
    tool: str = "StubRespondTool"
    required_categories: ClassVar[List[str]] = []

    @classmethod
    def get_required_categories(cls, params: dict | None = None) -> list[str]:
        if params and params.get("report_body"):
            return ["filesystem:write"]
        return []

    def execute(self) -> str:
        return "OK"


class TestDynamicCategories:
    """Tests for get_required_categories with parameter-level granularity."""

    def test_file_editor_read_operation(self):
        """Reading a file only requires filesystem:read."""
        cats = StubFileEditor.get_required_categories({"operation": "read"})
        assert cats == ["filesystem:read"]

    def test_file_editor_write_operation(self):
        """Writing a file requires filesystem:write."""
        cats = StubFileEditor.get_required_categories({"operation": "write"})
        assert cats == ["filesystem:write"]

    def test_respond_without_report_body(self):
        """Normal respond doesn't require filesystem access."""
        cats = StubRespondTool.get_required_categories({})
        assert cats == []

    def test_respond_with_report_body(self):
        """Respond with a report body requires filesystem:write."""
        cats = StubRespondTool.get_required_categories({"report_body": "Hello"})
        assert cats == ["filesystem:write"]

    def test_static_fallback(self):
        """A tool without get_required_categories override uses the ClassVar."""
        cats = PermissiveTool.get_required_categories({})
        assert cats == []

    def test_no_params_fallback(self):
        """get_required_categories called without params uses ClassVar."""
        cats = StubFileEditor.get_required_categories()
        assert cats == ["filesystem:write"]


# ---------------------------------------------------------------------------
# Integration: ToolExecutor with custom SessionPermissions via config
# ---------------------------------------------------------------------------

from thoughtmachine.security import SessionPermissions



class FakeConfigWithPermissions:
    """Config stub with a SessionPermissions model."""
    workspace_path = None
    tool_output_token_limit = None

    def __init__(self, permissions: SessionPermissions | None = None):
        self.session_permissions = permissions


class TestToolExecutorCustomPermissions:
    """Integration tests: ToolExecutor respects custom session permissions."""

    def _make_executor(self, tool_classes, permissions=None):
        return ToolExecutor(
            tool_classes=tool_classes,
            config=FakeConfigWithPermissions(permissions),
            state=FakeState(),
            logger=None,
            security_available=False,
            agent=None,
        )

    def test_container_tool_allowed_with_custom_config(self):
        """RISK-3 (new contract): an id-less executor (no session_id /
        workspace_id) MUST NOT grant from the in-memory mirror.  A permissive
        mirror (container=True) is no longer authoritative -> the container
        tool is DENIED.  (Canonical disk-mode grant lives in
        tests/test_worker_disk_mode_inheritance.py.)"""
        perms = SessionPermissions(container=True)
        executor = self._make_executor([ContainerTool], permissions=perms)
        result = executor._execute_single_tool(
            ContainerTool, {}, "ContainerTool", 0,
            lambda: False, lambda: None, lambda: 0
        )
        assert "Permission denied" in result["result"], result
        assert "container:true" in result["result"], result

    def test_container_tool_denied_with_explicit_false(self):
        """Container tool denied when config has container=False."""
        perms = SessionPermissions(container=False)
        executor = self._make_executor([ContainerTool], permissions=perms)
        result = executor._execute_single_tool(
            ContainerTool, {}, "ContainerTool", 0,
            lambda: False, lambda: None, lambda: 0
        )
        assert "Permission denied" in result["result"]
        assert "container:true" in result["result"]

    def test_mixed_allowed_and_denied(self):
        """Multiple requirements: allow container but deny network."""
        perms = SessionPermissions(container=True, network=False)
        executor = self._make_executor([NetworkAndFilesystemTool], permissions=perms)
        result = executor._execute_single_tool(
            NetworkAndFilesystemTool, {}, "NetworkAndFilesystemTool", 0,
            lambda: False, lambda: None, lambda: 0
        )
        # network:true is denied, filesystem:write is denied
        assert "Permission denied" in result["result"]

    def test_all_categories_allowed(self):
        """RISK-3 (new contract): a maximally permissive id-less mirror is
        IGNORED -- the deny-all floor denies the network:true requirement.
        (Disk-mode ceiling/missing-category contract: tests/test_security_gate_disk.py.)"""
        perms = SessionPermissions(
            container=True, network=True,
            filesystem="write", system="full", execution="full"
        )
        executor = self._make_executor([NetworkAndFilesystemTool], permissions=perms)
        result = executor._execute_single_tool(
            NetworkAndFilesystemTool, {}, "NetworkAndFilesystemTool", 0,
            lambda: False, lambda: None, lambda: 0
        )
        assert "Permission denied" in result["result"], result

    def test_permissive_default_profile_allows_tools(self):
        """RISK-3 (new contract): an id-less executor DOES NOT inherit the
        permissive default profile from the in-memory mirror -- both the
        container tool and the network+filesystem tool are DENIED fail-closed.
        (The disk-mode profile grant is pinned in
        tests/test_worker_disk_mode_inheritance.py.)"""
        perms = SessionPermissions(
            container=True,
            network=True,  # coerces to 'write'
            filesystem="write",
            system="read",
            git="read",
            execution="banned",
        )
        executor = self._make_executor([ContainerTool, NetworkAndFilesystemTool], permissions=perms)
        r1 = executor._execute_single_tool(
            ContainerTool, {}, "ContainerTool", 0,
            lambda: False, lambda: None, lambda: 0
        )
        assert "Permission denied" in r1["result"], r1
        r2 = executor._execute_single_tool(
            NetworkAndFilesystemTool, {}, "NetworkAndFilesystemTool", 0,
            lambda: False, lambda: None, lambda: 0
        )
        assert "Permission denied" in r2["result"], r2

    def test_none_permissions_falls_back_to_default(self):
        """When session_permissions is None, the executor falls back to the
        CONSERVATIVE SessionPermissions() model defaults (tool_executor.py:253-255),
        so a container tool is denied. Verified at runtime in the container."""
        executor = self._make_executor([ContainerTool], permissions=None)
        result = executor._execute_single_tool(
            ContainerTool, {}, "ContainerTool", 0,
            lambda: False, lambda: None, lambda: 0
        )
        assert "Permission denied" in result["result"]
        assert "container:true" in result["result"]

    def test_respond_tool_dynamic_category_integration(self):
        """Respond without report_body passes even with restrictive permissions."""
        perms = SessionPermissions(filesystem="read")
        executor = self._make_executor([StubRespondTool], permissions=perms)
        result = executor._execute_single_tool(
            StubRespondTool, {}, "StubRespondTool", 0,
            lambda: False, lambda: None, lambda: 0
        )
        # No report_body → empty required_categories → always passes
        assert result["result"] == "OK"


# ---------------------------------------------------------------------------
# Raw tool-call diagnostic stream (tool_calls_raw_debug.log)
# ---------------------------------------------------------------------------

class _AllowAllState:
    """State stub that allows every tool."""

    security_config = None

    def is_tool_allowed(self, tool_name):
        return True


class TestToolCallRawDiagnosticStream:
    """execute_tool_calls must emit redacted, structured tool-call diagnostics."""

    @pytest.fixture
    def hermetic(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path))
        yield tmp_path
        lifecycle.close_streams()

    def test_execute_tool_calls_writes_redacted_structured_diagnostic(self, hermetic):
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
        executor = ToolExecutor(
            tool_classes=[],
            config=FakeConfig(None),
            state=_AllowAllState(),
            logger=None,
            security_available=False,
            agent=None,
        )
        collected = []
        executor.execute_tool_calls(
            [{"id": "call_1", "function": {"name": "ReadFile", "arguments": '{"path": "%s"}' % secret}}],
            add_to_conversation_func=collected.append,
            session_id="sess-1",
        )
        path = os.path.join(str(hermetic), "logs", "tool_calls_raw_debug.log")
        assert os.path.isfile(path)
        raw = open(path, encoding="utf-8").read()
        assert secret not in raw
        assert "sk-<REDACTED>" in raw
        records = [json.loads(ln) for ln in raw.splitlines() if ln.strip()]
        assert len(records) == 1
        rec = records[0]
        assert rec["event"] == "tool_call_raw"
        assert rec["tool_name"] == "ReadFile"
        assert rec["tool_call_id"] == "call_1"
        assert rec["json_repair_needed"] is False
        assert rec["session_id"] == "sess-1"
        assert "sk-<REDACTED>" in rec["arguments_preview"]


# ---------------------------------------------------------------------------
# Structural failure signal: refusal / never-executed paths carry success=False
#
# A refused tool call used to be reported upstream as success=True: agent.py
# classified outcomes by sniffing the result PROSE, and the gate's denials say
# "Permission denied: ..." (no '❌' prefix, no marker), so a denial looked
# exactly like a success to the UI.  The executor now states the verdict
# structurally; these tests pin it.
# ---------------------------------------------------------------------------

class _RequiredArgTool(ToolBase):
    """Tool with a mandatory argument, to trip the ValidationError path."""

    tool: str = "_RequiredArgTool"
    required_categories: ClassVar[List[str]] = []
    path: str

    def execute(self) -> str:
        return "unreachable"


class _ExplodingTool(ToolBase):
    """Tool whose execute() raises, to trip the generic exception path."""

    tool: str = "_ExplodingTool"
    required_categories: ClassVar[List[str]] = []

    def execute(self) -> str:
        raise RuntimeError("boom")


class TestToolFailureSuccessFlag:
    """A refused/never-executed tool call must be structurally reported as a
    failure (``success is False``), not left to prose sniffing upstream."""

    def _executor(self, tool_classes, permissions=None):
        return ToolExecutor(
            tool_classes=tool_classes,
            config=FakeConfig(permissions),
            state=FakeState(),
            logger=None,
            security_available=False,
            agent=None,
        )

    @staticmethod
    def _single(executor, tool_cls, arguments=None):
        return executor._execute_single_tool(
            tool_cls, arguments or {}, tool_cls.__name__, 0,
            lambda: False, lambda: None, lambda: 0,
        )

    def test_denied_call_is_flagged_as_failure(self):
        executor = self._executor(
            [ContainerTool], permissions=SessionPermissions(container=False)
        )
        result = self._single(executor, ContainerTool)
        # The prose is untouched -- only the structural flag is new.
        assert result["result"].startswith(
            "Permission denied: Tool requires container:true"
        ), result["result"]
        assert result["tool_type"] == "normal"
        assert result["success"] is False

    def test_validation_error_is_flagged_as_failure(self):
        executor = self._executor([_RequiredArgTool])
        result = self._single(executor, _RequiredArgTool)
        assert result["result"].startswith("Invalid arguments:"), result["result"]
        assert result["success"] is False

    def test_tool_exception_is_flagged_as_failure(self):
        executor = self._executor([_ExplodingTool])
        result = self._single(executor, _ExplodingTool)
        assert result["result"] == "Error executing tool: boom", result["result"]
        assert result["success"] is False

    def test_executed_call_gets_no_success_key(self):
        """Success paths keep their exact shape -- no key is invented."""
        executor = self._executor([PermissiveTool])
        result = self._single(executor, PermissiveTool)
        assert result["result"] == "OK"
        assert "success" not in result

    def test_execute_tool_calls_propagates_failure_flag(self):
        """The public entry point must forward the verdict, otherwise every
        per-call ``success`` flag dies at the executed_tools boundary."""
        executor = ToolExecutor(
            tool_classes=[ContainerTool],
            config=FakeConfig(SessionPermissions(container=False)),
            state=_AllowAllState(),
            logger=None,
            security_available=False,
            agent=None,
        )
        executed, *_ = executor.execute_tool_calls(
            [{"id": "tc-1", "function": {"name": "ContainerTool", "arguments": "{}"}}],
            add_to_conversation_func=lambda _m: None,
        )
        assert executed[0]["result"].startswith("Permission denied:"), executed[0]
        assert executed[0]["success"] is False

    def test_execute_tool_calls_leaves_success_shape_unchanged(self):
        executor = ToolExecutor(
            tool_classes=[PermissiveTool],
            config=FakeConfig(None),
            state=_AllowAllState(),
            logger=None,
            security_available=False,
            agent=None,
        )
        executed, *_ = executor.execute_tool_calls(
            [{"id": "tc-1", "function": {"name": "PermissiveTool", "arguments": "{}"}}],
            add_to_conversation_func=lambda _m: None,
        )
        assert executed[0]["result"] == "OK"
        assert "success" not in executed[0]


# ---------------------------------------------------------------------------
# Workspace-id resolution precedence for the permission gate
#
# The gate must enforce the LIVE workspace identity.  ``config.workspace_path``
# is a DEPRECATED construction-time snapshot that a hot-swap nulls out
# (agent.py:791-797 replaces ``self.config`` wholesale), so resolving from it
# FIRST inverted the precedence: the snapshot beat the explicit
# ``workspace_id`` argument and the persisted session record, and an
# unresolvable snapshot DENIED a call whose live identity resolved fine.
# ---------------------------------------------------------------------------

class _GateSpy:
    """Stand-in for ``get_effective_permissions`` that records the enforcement
    inputs, so the assertions land on the RESOLVED workspace id itself rather
    than on a log string.

    The returned profiles mirror the gate: with a workspace id the on-disk
    grant record backs a permissive profile; with NO workspace id there is no
    on-disk authority, so the id-less fail-closed floor applies (deny-all).
    """

    _DENY_ALL = {
        "filesystem": "banned",
        "network": "banned",
        "container": False,
        "git": "banned",
        "mcp": "banned",
        "host_bash": "banned",
    }
    _ALLOW_ALL = {
        "filesystem": "write",
        "network": True,
        "container": True,
        "git": "write_on_feature_branch",
        "mcp": "full",
        "host_bash": "allow",
    }

    def __init__(self):
        self.calls = []

    def __call__(self, session_perms, caps, workspace_permissions=None, *,
                 session_id=None, workspace_id=None):
        self.calls.append({"session_id": session_id, "workspace_id": workspace_id})
        return dict(self._ALLOW_ALL if workspace_id else self._DENY_ALL)

    @property
    def resolved_workspace_id(self):
        assert len(self.calls) == 1, f"expected exactly one gate merge, got {self.calls}"
        return self.calls[0]["workspace_id"]


class _FakeSessionStore:
    """Stand-in for ``FileSystemSessionStore`` that isolates the RESOLVER.

    ``load_session_workspace_id`` answers (the store's purpose-built
    read-only probe); ``_find_session_path`` answers None, i.e. the shape of a
    session the store knows through its index / ``_meta_<sid>.json`` sidecar
    but whose payload the legacy probe cannot locate.  A resolver that never
    asks the store therefore resolves NOTHING here -- which is how a
    regression in the resolution order becomes observable.
    """

    def __init__(self, records):
        self.records = dict(records)
        self.loader_calls = []

    def load_session_workspace_id(self, session_id):
        self.loader_calls.append(session_id)
        return self.records.get(session_id)

    def _find_session_path(self, session_id):
        return None


class TestWorkspaceIdResolutionPrecedence:
    """Resolution order: explicit id -> session record -> config.workspace_path."""

    def _executor(self, tool_classes, permissions=None):
        return ToolExecutor(
            tool_classes=tool_classes,
            config=FakeConfig(permissions),
            state=FakeState(),
            logger=None,
            security_available=False,
            agent=None,
        )

    @staticmethod
    def _run(executor, tool_cls, session_id="", workspace_id=""):
        return executor._execute_single_tool(
            tool_cls, {}, tool_cls.__name__, 0,
            lambda: False, lambda: None, lambda: 0,
            session_id=session_id,
            workspace_id=workspace_id,
        )

    @staticmethod
    def _patch_gate(monkeypatch, store=None, path_to_ws=None):
        import agent.core.tool_executor as _te

        spy = _GateSpy()
        monkeypatch.setattr(_te, "get_effective_permissions", spy)
        monkeypatch.setattr(_te, "get_workspace_capabilities", lambda ws_id: object())
        if path_to_ws is not None:
            monkeypatch.setattr(
                _te, "resolve_workspace_id",
                lambda path: path_to_ws.get(path) if path else None,
            )
        if store is not None:
            import session.store as _session_store
            monkeypatch.setattr(
                _session_store, "FileSystemSessionStore",
                lambda *a, **k: store,
            )
        return spy

    def test_session_record_resolves_when_config_snapshot_is_none(self, monkeypatch):
        """T1: ``config.workspace_path`` is None (the post-hot-swap shape) while
        the session record names ws-A -> the category-gated call is ALLOWED and
        every category check is made against ws-A."""
        store = _FakeSessionStore({"sess-A": "ws-A"})
        spy = self._patch_gate(monkeypatch, store=store)
        executor = self._executor([ContainerTool], SessionPermissions(container=True))
        assert executor.config.workspace_path is None

        result = self._run(executor, ContainerTool, session_id="sess-A")

        assert result["result"] == "Container OK", result["result"]
        assert spy.resolved_workspace_id == "ws-A", spy.calls
        assert store.loader_calls == ["sess-A"], store.loader_calls

    def test_explicit_workspace_id_beats_config_workspace_path(self, monkeypatch):
        """T2: ``config.workspace_path`` resolves to ws-B while the explicit
        ``workspace_id`` names ws-A -> ws-A is what the gate enforces."""
        spy = self._patch_gate(monkeypatch, path_to_ws={"/ws/B": "ws-B"})
        executor = self._executor([ContainerTool], SessionPermissions(container=True))
        executor.config.workspace_path = "/ws/B"

        result = self._run(executor, ContainerTool, "sess-A", "ws-A")

        assert result["result"] == "Container OK", result["result"]
        assert spy.resolved_workspace_id == "ws-A", spy.calls

    def test_session_record_beats_config_workspace_path(self, monkeypatch):
        """T2 (second leg): with no explicit id, the session record (ws-A) must
        still beat a resolvable ``config.workspace_path`` (ws-B)."""
        store = _FakeSessionStore({"sess-A": "ws-A"})
        spy = self._patch_gate(monkeypatch, store=store, path_to_ws={"/ws/B": "ws-B"})
        executor = self._executor([ContainerTool], SessionPermissions(container=True))
        executor.config.workspace_path = "/ws/B"

        result = self._run(executor, ContainerTool, session_id="sess-A")

        assert result["result"] == "Container OK", result["result"]
        assert spy.resolved_workspace_id == "ws-A", spy.calls

    def test_nothing_resolvable_stays_denied_fail_closed(self, monkeypatch):
        """T3: an unresolvable config path plus no explicit id and no session
        record still DENIES fail-closed -- and the refusal names the reason it
        could not resolve, so it cannot be confused with a category denial."""
        store = _FakeSessionStore({})
        spy = self._patch_gate(monkeypatch, store=store, path_to_ws={})
        executor = self._executor([ContainerTool], SessionPermissions(container=True))
        executor.config.workspace_path = "/ws/unresolvable"

        result = self._run(executor, ContainerTool, session_id="sess-unknown")

        assert result["success"] is False, result
        assert result["result"].startswith(
            "DENIED: could not resolve workspace_id for workspace_path=/ws/unresolvable; "
            "tool execution denied (fail-closed)."
        ), result["result"]
        assert "No workspace id resolved from the explicit workspace_id argument" in result["result"], result["result"]
        assert spy.calls == [], f"gate must not merge with an unresolved id: {spy.calls}"

    def test_hot_swap_to_config_without_workspace_path_keeps_gating(self, monkeypatch):
        """T4 (hot-swap regression): agent.py:791-797 replaces ``self.config``
        with an AgentConfig that carries NO workspace_path; a category-gated
        call must still be enforced against the live session workspace."""
        from agent.config.models import AgentConfig
        from agent.core.state import AgentState

        store = _FakeSessionStore({"sess-A": "ws-A"})
        spy = self._patch_gate(
            monkeypatch, store=store, path_to_ws={"/ws/A": "ws-A"},
        )
        cfg = AgentConfig(
            session_permissions=SessionPermissions(container=True),
            workspace_path="/ws/A",
        )
        executor = ToolExecutor(
            tool_classes=[ContainerTool],
            config=cfg,
            state=AgentState(config=cfg),
            logger=None,
            security_available=False,
            agent=None,
        )

        # The hot-swap: exactly what agent.py:791-797 does (new config assigned
        # to the agent, its state AND the live executor).
        new_config = AgentConfig(session_permissions=SessionPermissions(container=True))
        assert new_config.workspace_path is None, new_config.workspace_path
        executor.config = new_config

        result = self._run(executor, ContainerTool, session_id="sess-A")

        assert result["result"] == "Container OK", result["result"]
        assert spy.resolved_workspace_id == "ws-A", spy.calls

