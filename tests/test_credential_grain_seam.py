"""The ``{{credential:<name>}}`` seam is gated by the ``credential`` grain.

``agent/core/tool_executor.py`` resolves ``{{credential:<name>}}`` placeholders
AFTER the declaration-based category gate
(``check_required_categories(tool_class.get_required_categories(...))``).  That
gate can only judge what a tool ASKS for, and a tool that declares no
``credential:use`` requirement was let through it -- so the injector handed the
secret to a session whose ``credential`` grain was ``banned``.  The grain existed
as vocabulary only (``SessionPermissions`` / ``RESOURCE_CATALOG`` /
``GRANT_LEVEL_RANKS``); nothing enforced it at the one place a secret value
crosses into tool code.

These are regression tests for the fix at that seam, driving the REAL
``ToolExecutor._execute_single_tool`` (no re-implementation of the rule):

1. ``credential: banned`` + a placeholder  -> refusal, the injector never runs.
2. ``credential: use`` + a placeholder     -> injection happens exactly once.
3. no resolvable permission profile        -> fail closed (refusal, no injector).
4. no gate function available to consult   -> fail closed (refusal, no injector).
5. no placeholder in the args              -> untouched (no gate, no injector).

The refusal asserts the house ``check_required_categories`` wording
(``Permission denied: Tool requires credential:use``) so operators keep seeing
ONE denial format, and asserts that neither the injector nor a
``CREDENTIAL_USE`` audit event fires on a refused call.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, ClassVar, Dict, List

# Pytest prepends the test directory to ``sys.path``; make the repo root win so
# ``import security`` resolves to ``<repo>/security`` (not ``tests/security``).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

WORKSPACE_ID = "ws-audit"
SESSION_ID = "sess-audit-1"
CREDENTIAL_NAME = "demo_key"
RAW_VALUE = "planted-value-must-not-be-logged"
PLACEHOLDER_ARGS = {
    "workspace_id": WORKSPACE_ID,
    "api_key": "{{credential:%s}}" % CREDENTIAL_NAME,
}


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _write_hermetic_vault(tmp_path: Path, monkeypatch) -> None:
    """Point the injector's ``vault_root()`` at ``tmp_path`` and plant a secret."""
    import agent.credentials.injector as injector_mod

    monkeypatch.setattr(injector_mod, "vault_root", lambda: tmp_path)
    scope = tmp_path / "credentials" / WORKSPACE_ID
    scope.mkdir(parents=True)
    (scope / CREDENTIAL_NAME).write_text(RAW_VALUE + "\n", encoding="utf-8")


def _install_injector_spy(monkeypatch) -> Dict[str, Any]:
    """Record injector constructions and ``inject()`` calls.

    The executor binds ``CredentialInjector`` with a function-local
    ``from agent.credentials import CredentialInjector``, so patching the
    package attribute is what intercepts it.
    """
    import agent.credentials as credentials_pkg
    from agent.credentials import injector as injector_mod

    real_injector = injector_mod.CredentialInjector
    record: Dict[str, Any] = {"constructed": 0, "injects": []}

    class SpyCredentialInjector(real_injector):  # type: ignore[misc, valid-type]
        def __init__(self, workspace_id: str) -> None:
            super().__init__(workspace_id)
            record["constructed"] += 1

        def inject(self, tool_args: dict) -> dict:
            record["injects"].append(dict(tool_args))
            return super().inject(tool_args)

    monkeypatch.setattr(
        credentials_pkg, "CredentialInjector", SpyCredentialInjector
    )
    return record


def _install_permission_profile(monkeypatch, profile) -> None:
    """Pin the permission side of the seam.

    ``workspace_id`` + ``session_id`` make the executor ask the gate for the
    disk-authoritative record; both sides are pinned here so the test never
    reads a real vault permission store and never depends on its contents.
    """
    from agent.core import tool_executor as executor_mod
    from thoughtmachine.workspace_capabilities import WorkspaceCapabilities

    monkeypatch.setattr(
        executor_mod,
        "get_workspace_capabilities",
        lambda _ws_id: WorkspaceCapabilities(),
    )
    monkeypatch.setattr(
        executor_mod,
        "get_effective_permissions",
        lambda *a, **k: None if profile is None else dict(profile),
    )


def _credential_echo_tool_class():
    """A tool that declares NO categories and reports whether injection ran."""
    from tools.base import ToolBase

    class CredentialEchoTool(ToolBase):
        tool: str = "CredentialEchoTool"
        required_categories: ClassVar[List[str]] = []
        workspace_id: str = ""
        api_key: str = ""

        def execute(self) -> str:
            return "injected" if self.api_key == RAW_VALUE else "unresolved"

    return CredentialEchoTool


def _execute(tmp_path, monkeypatch, *, profile, arguments):
    """Run one real ``_execute_single_tool`` call; return (result, spy, events)."""
    from agent.core.tool_executor import ToolExecutor
    from agent.events import EventBus

    _write_hermetic_vault(tmp_path, monkeypatch)
    spy = _install_injector_spy(monkeypatch)
    _install_permission_profile(monkeypatch, profile)
    tool_class = _credential_echo_tool_class()

    class FakeConfig:
        workspace_path = None
        tool_output_token_limit = None
        session_permissions = None

    class FakeState:
        security_config = None

    bus = EventBus()
    captured: List[Any] = []
    bus.subscribe(None, lambda event: captured.append(event))

    executor = ToolExecutor(
        tool_classes=[tool_class],
        config=FakeConfig(),
        state=FakeState(),
        logger=None,
        security_available=True,
        agent=None,
        event_bus=bus,
    )
    result = executor._execute_single_tool(
        tool_class,
        dict(arguments),
        "CredentialEchoTool",
        0,
        lambda: False,
        lambda: None,
        lambda: 0,
        session_id=SESSION_ID,
        workspace_id=WORKSPACE_ID,
    )
    return result, spy, captured


def _credential_use_events(captured):
    from agent.events import EventType

    return [
        event
        for event in captured
        if getattr(event, "type", None) == EventType.CREDENTIAL_USE
    ]


# ---------------------------------------------------------------------------
# 1. A banned grain refuses the placeholder before the injector runs
# ---------------------------------------------------------------------------


def test_banned_credential_grain_refuses_placeholder(tmp_path, monkeypatch):
    """``credential: banned`` must deny a placeholder-carrying call."""
    result, spy, captured = _execute(
        tmp_path,
        monkeypatch,
        profile={"credential": "banned"},
        arguments=PLACEHOLDER_ARGS,
    )

    assert result["success"] is False
    assert "credential:use" in result["result"]
    # The injector is never even constructed on the refusal path, so the secret
    # cannot reach the tool (or the result) by any route.
    assert spy["constructed"] == 0
    assert spy["injects"] == []
    assert RAW_VALUE not in json.dumps(result, default=str)
    assert _credential_use_events(captured) == []


# ---------------------------------------------------------------------------
# 2. A granted grain still injects, exactly once
# ---------------------------------------------------------------------------


def test_granted_credential_use_injects_once(tmp_path, monkeypatch):
    """``credential: use`` is the positive control: injection still works."""
    result, spy, captured = _execute(
        tmp_path,
        monkeypatch,
        profile={"credential": "use"},
        arguments=PLACEHOLDER_ARGS,
    )

    assert "success" not in result
    assert result["result"] == "injected"
    assert spy["constructed"] == 1
    assert len(spy["injects"]) == 1
    # One audit event, name only, never the value.
    events = _credential_use_events(captured)
    assert len(events) == 1
    assert events[0].data["credential_name"] == CREDENTIAL_NAME
    assert RAW_VALUE not in json.dumps(events[0].data, default=str)


# ---------------------------------------------------------------------------
# 3./4. No usable profile / no gate function -> fail CLOSED
# ---------------------------------------------------------------------------


def test_unresolved_permission_profile_fails_closed(tmp_path, monkeypatch):
    """An unresolved (``None``) profile must refuse, not crash or allow."""
    result, spy, captured = _execute(
        tmp_path,
        monkeypatch,
        profile=None,
        arguments=PLACEHOLDER_ARGS,
    )

    assert result["success"] is False
    assert "credential:use" in result["result"]
    assert "unavailable" in result["result"]
    assert spy["constructed"] == 0
    assert spy["injects"] == []
    assert RAW_VALUE not in json.dumps(result, default=str)
    assert _credential_use_events(captured) == []


def test_missing_gate_function_fails_closed(tmp_path, monkeypatch):
    """A gate that cannot judge the placeholder must deny, not allow.

    ``SessionPermissions`` is also unbound here so the upstream
    declaration-based gate (which would otherwise call the very function this
    test removes) is skipped: the call reaches the credential seam with no
    ``check_required_categories`` available at all.
    """
    from agent.core import tool_executor as executor_mod

    monkeypatch.setattr(executor_mod, "SessionPermissions", None)
    monkeypatch.setattr(executor_mod, "check_required_categories", None)

    result, spy, captured = _execute(
        tmp_path,
        monkeypatch,
        profile={"credential": "use"},
        arguments=PLACEHOLDER_ARGS,
    )

    assert result["success"] is False
    assert "credential:use" in result["result"]
    assert "unavailable" in result["result"]
    assert spy["constructed"] == 0
    assert spy["injects"] == []
    assert RAW_VALUE not in json.dumps(result, default=str)
    assert _credential_use_events(captured) == []


# ---------------------------------------------------------------------------
# 5. A placeholder-free call is not gated at all
# ---------------------------------------------------------------------------


def test_call_without_placeholder_is_not_gated(tmp_path, monkeypatch):
    """Nothing to resolve means nothing to gate -- and no injector work."""
    result, spy, captured = _execute(
        tmp_path,
        monkeypatch,
        profile={"credential": "banned"},
        arguments={"workspace_id": WORKSPACE_ID, "api_key": "plain"},
    )

    assert "success" not in result
    assert result["result"] == "unresolved"
    assert spy["constructed"] == 0
    assert spy["injects"] == []
    assert _credential_use_events(captured) == []
