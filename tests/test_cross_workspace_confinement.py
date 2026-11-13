"""Cross-workspace confinement regression tests.

Two independent leaks are pinned here:

1. ``ToolExecutor._execute_single_tool`` injects the AUTHORITATIVE
   ``workspace_id`` (the id the agent resolved from its live session record)
   into the tool arguments, overwriting anything the model put there.  A
   spoofed ``workspace_id`` in the tool-call arguments must never win.
2. ``CheckSystem(query="workers")`` with no resolvable workspace identity
   must NOT enumerate sibling ``workspaces/*/workers.json`` files
   (``_scan_workspace_dirs_for_workers`` is now confined to the caller's own
   workspace; with no identity the query fails closed with a ``reason``).
"""

import json
from typing import ClassVar, List, Optional
from unittest.mock import patch

from agent.core.tool_executor import ToolExecutor
from tools.base import ToolBase
from tools.workspace.check_system import CheckSystem


# --- 1. spoofed workspace_id in tool arguments never wins -------------------

def test_spoofed_workspace_id_in_tool_args_does_not_win(monkeypatch):
    """The gate-injected workspace_id overrides a caller-supplied one."""
    import agent.core.tool_executor as te
    from thoughtmachine.security import SessionPermissions

    recorded = {}

    class ProbeTool(ToolBase):
        tool: str = "probe_workspace_id"
        required_categories: ClassVar[List[str]] = []
        # Deliberately declared, so a hostile ``workspace_id`` in the arguments
        # survives ghost-field stripping and only the injection can displace it.
        workspace_id: Optional[str] = None

        def execute(self) -> str:
            recorded["workspace_id"] = self.workspace_id
            return "ok"

    class FakeConfig:
        workspace_path = None
        tool_output_token_limit = None

        def __init__(self, permissions=None):
            self.session_permissions = permissions

    class FakeState:
        security_config = None

    monkeypatch.setattr(
        te, "get_effective_permissions",
        lambda *a, **k: {"filesystem": "read", "container": True},
        raising=False,
    )
    monkeypatch.setattr(
        te, "get_workspace_capabilities", lambda ws_id: object(), raising=False
    )

    executor = ToolExecutor(
        tool_classes=[ProbeTool],
        config=FakeConfig(SessionPermissions(container=True, filesystem="read")),
        state=FakeState(),
        logger=None,
        security_available=False,
        agent=None,
    )
    result = executor._execute_single_tool(
        ProbeTool,
        {"workspace_id": "ws-foreign"},
        ProbeTool.__name__,
        0,
        lambda: False,
        lambda: None,
        lambda: 0,
        session_id="sess-A",
        workspace_id="ws-own",
    )

    # The probe must actually have run: a refusal/early return would leave the
    # recorded value unset and make the two assertions below vacuous.
    assert recorded, f"probe tool never executed: {result}"
    assert result.get("success", True) is not False, f"tool execution refused: {result}"
    assert recorded.get("workspace_id") == "ws-own"
    assert recorded.get("workspace_id") != "ws-foreign"


def test_spoofed_workspace_id_never_survives_an_unresolved_identity(monkeypatch):
    """Even with NO resolved workspace id, a caller-supplied one is discarded."""
    import agent.core.tool_executor as te
    from thoughtmachine.security import SessionPermissions

    recorded = {}

    class ProbeTool(ToolBase):
        tool: str = "probe_workspace_id"
        required_categories: ClassVar[List[str]] = []
        workspace_id: Optional[str] = None

        def execute(self) -> str:
            recorded["workspace_id"] = self.workspace_id
            return "ok"

    class FakeConfig:
        workspace_path = None
        tool_output_token_limit = None

        def __init__(self, permissions=None):
            self.session_permissions = permissions

    class FakeState:
        security_config = None

    monkeypatch.setattr(
        te, "get_effective_permissions",
        lambda *a, **k: {"filesystem": "read", "container": True},
        raising=False,
    )
    monkeypatch.setattr(
        te, "get_workspace_capabilities", lambda ws_id: object(), raising=False
    )

    executor = ToolExecutor(
        tool_classes=[ProbeTool],
        config=FakeConfig(SessionPermissions(container=True, filesystem="read")),
        state=FakeState(),
        logger=None,
        security_available=False,
        agent=None,
    )
    result = executor._execute_single_tool(
        ProbeTool,
        {"workspace_id": "ws-foreign"},
        ProbeTool.__name__,
        0,
        lambda: False,
        lambda: None,
        lambda: 0,
        session_id="sess-A",
        workspace_id="",
    )

    assert recorded, f"probe tool never executed: {result}"
    assert result.get("success", True) is not False, f"tool execution refused: {result}"
    # The LLM-supplied id is not authoritative: it is replaced by the executor's
    # (here empty) resolution, never passed through untouched.
    assert recorded.get("workspace_id") != "ws-foreign"
    assert not recorded.get("workspace_id")


# --- 2. workers lookups never scan sibling workspaces -----------------------

FOREIGN_WORKER = "foreign-worker-marker"


def _write_foreign_workers(vault) -> None:
    ws_dir = vault / "workspaces" / "ws-other"
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "workers.json").write_text(
        json.dumps([
            {
                "name": FOREIGN_WORKER,
                "description": "another tenant's worker",
                "system_prompt": "secret",
                "tools": [],
                "permission_footprint": {},
            }
        ]),
        encoding="utf-8",
    )


def test_workers_query_without_workspace_id_never_reads_other_workspaces(
    tmp_path, monkeypatch
):
    """No resolvable workspace identity => empty list + reason, no foreign data."""
    vault = tmp_path / "vault"
    (vault / "workspaces").mkdir(parents=True, exist_ok=True)
    _write_foreign_workers(vault)
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))

    with patch.object(
        CheckSystem, "_load_allowlist_from_vault", return_value=["workers"]
    ), patch("thoughtmachine.vault.vault_root", return_value=vault):
        tool = CheckSystem(
            query="workers",
            session_permissions={"filesystem": "read", "network": "write"},
        )
        output = tool.execute()

    assert FOREIGN_WORKER not in output
    payload = json.loads(output)
    assert payload["workers"] == []
    assert payload["count"] == 0
    assert payload.get("reason")


def test_own_workspace_workers_still_visible(tmp_path, monkeypatch):
    """Control: the caller's OWN workers.json is still returned."""
    vault = tmp_path / "vault"
    ws_dir = vault / "workspaces" / "ws-own"
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "workers.json").write_text(
        json.dumps([{"name": "own-worker", "description": "d",
                     "system_prompt": "p", "tools": [],
                     "permission_footprint": {}}]),
        encoding="utf-8",
    )
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))

    with patch.object(
        CheckSystem, "_load_allowlist_from_vault", return_value=["workers"]
    ), patch("thoughtmachine.vault.vault_root", return_value=vault):
        tool = CheckSystem(
            query="workers",
            workspace_id="ws-own",
            session_permissions={"filesystem": "read", "network": "write"},
        )
        payload = json.loads(tool.execute())

    names = [w.get("name") for w in payload.get("workers", [])]
    assert "own-worker" in names
