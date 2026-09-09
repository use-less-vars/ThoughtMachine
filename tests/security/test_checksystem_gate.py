"""
Canonical contract tests for CheckSystem availability (permission-grain migration).

System checks are ALWAYS available: the security gate's effective dict has no
``system`` category (canonical session resources are filesystem/network/
container/git/mcp/host_bash), so CheckSystem declares NO required categories.
Declaring a legacy ``system:read`` category would make ``check_required_categories``
deny EVERY query (unknown category -> fail closed).

Verifies:
1. No CheckSystem query resolves to a permission category (get_required_categories
   is ``[]`` for every query).
2. The permission gate never blocks a CheckSystem query: with a session that has
   no ``system`` grain, ``check_required_categories([], effective)`` still allows.
3. Access control for CheckSystem is the VAULT QUERY ALLOWLIST, enforced inside
   ``execute()``: a query not in the allowlist is denied with ``status: denied``
   (no permission gate involved).

Uses the benign ``workspace_info`` query. (Historical note: the pre-migration
file asserted a ``system:read`` static ClassVar gate; the removal of that gate is
the canonical behaviour under the 6-key effective-permission model.)

Docker note: ``tests/security/conftest.py`` fixes ``sys.path`` so the real
``security/`` package is imported instead of the pytest-injected ``tests/`` dir.
"""

import json
from unittest.mock import patch

from security.security_gate import check_required_categories, get_effective_permissions
from thoughtmachine.security import SessionPermissions
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities
from tools.workspace.check_system import CheckSystem

BENIGN_QUERY = "workspace_info"
QUERY_ARGS = {"query": BENIGN_QUERY}


def _parse_result(result: str) -> dict:
    return json.loads(result)


class TestCheckSystemAvailability:
    """CheckSystem is always available: no query maps to a permission category."""

    def test_no_query_requires_a_permission_category(self):
        """Every CheckSystem operation resolves to NO required categories."""
        assert CheckSystem.get_required_categories(QUERY_ARGS) == []
        assert CheckSystem.get_required_categories({}) == []
        assert CheckSystem.get_required_categories({"query": "capabilities"}) == []
        assert CheckSystem.get_required_categories({"query": "my_config"}) == []
        assert CheckSystem.get_required_categories(
            {"query": "effective_permissions"}
        ) == []
        assert CheckSystem.get_required_categories({"query": "runtime_state"}) == []

    def test_permission_gate_never_blocks_check_system(self):
        """A session without any 'system' grain still passes the gate (no
        required categories means the gate allows unconditionally)."""
        session = SessionPermissions(filesystem="read")  # no 'system' field set
        workspace = WorkspaceCapabilities()
        effective = get_effective_permissions(session, workspace)
        assert "system" not in effective  # canonical 6-key effective dict

        required = CheckSystem.get_required_categories(QUERY_ARGS)
        ok, _msg = check_required_categories(
            required,
            effective,
            "CheckSystem",
            QUERY_ARGS,
            "check system workspace_info",
        )
        assert required == []
        assert ok is True

    def test_query_denied_only_by_vault_allowlist(self):
        """The vault query allowlist is the access control: queries outside it
        are denied by execute() with status 'denied' (not by a permission gate)."""
        with patch.object(
            CheckSystem, "_load_allowlist_from_vault", return_value=["my_config"]
        ):
            tool = CheckSystem(query=BENIGN_QUERY)
            result = _parse_result(tool.execute())
        assert result["status"] == "denied"
        assert BENIGN_QUERY in result["error"]

    def test_allowlisted_query_runs_without_system_grain(self):
        """An allowlisted query executes fine even though the session carries no
        'system' permission anywhere in the chain."""
        with patch.object(
            CheckSystem, "_load_allowlist_from_vault", return_value=[BENIGN_QUERY]
        ), patch("tools.workspace.check_system.resolve_workspace_id", return_value=None):
            tool = CheckSystem(query=BENIGN_QUERY)
            result = _parse_result(tool.execute())
        assert "status" not in result or result["status"] != "denied"
        # workspace_info still resolves (no workspace -> empty metadata, no error).
        assert "error" not in result
        assert "workspace_id" in result
