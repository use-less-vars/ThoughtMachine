"""
Contract tests for the CheckSystem permission gate (Task 5).

CheckSystem inspects the runtime environment and can run host subprocesses
(``_query_event_log`` shells out to ``tail``), so it must always be gated
behind ``system:read``.

Verifies:
1. Every CheckSystem operation resolves to the ``system:read`` category.
2. With ``system: banned`` session permissions, a CheckSystem query is
   DENIED by the permission gate (``check_required_categories`` returns
   ``ok=False`` with a "Permission denied" message).
3. With ``system: read`` session permissions, the same query is ALLOWED.

Uses the benign ``workspace_info`` query: the gate check happens before any
query logic runs. (Historical note: ``_query_network_diagnostics`` previously
called a nonexistent ``DockerExecutor.run_command`` - BUG003 - which was fixed
by rewriting it to use the real ``ContainerManager.start/exec/stop`` API.)

Docker note: ``tests/security/conftest.py`` fixes ``sys.path`` so the real
``security/`` package is imported instead of the pytest-injected ``tests/`` dir.
"""

from security.security_gate import check_required_categories, get_effective_permissions
from thoughtmachine.security import SessionPermissions
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities
from tools.workspace.check_system import CheckSystem

BENIGN_QUERY = "workspace_info"
QUERY_ARGS = {"query": BENIGN_QUERY}


class TestCheckSystemPermissionGate:
    """CheckSystem requires system:read for every query (static ClassVar gate)."""

    def test_all_queries_require_system_read(self):
        """Every CheckSystem operation resolves to the system:read category."""
        assert CheckSystem.get_required_categories(QUERY_ARGS) == ["system:read"]
        assert CheckSystem.get_required_categories({}) == ["system:read"]
        assert CheckSystem.get_required_categories({"query": "capabilities"}) == ["system:read"]
        assert CheckSystem.get_required_categories({"query": "my_config"}) == ["system:read"]

    def test_gate_denies_query_when_system_banned(self):
        """system=banned => the permission gate denies a CheckSystem query."""
        session = SessionPermissions(system="banned")
        workspace = WorkspaceCapabilities()
        effective = get_effective_permissions(session, workspace)

        required = CheckSystem.get_required_categories(QUERY_ARGS)
        ok, msg = check_required_categories(
            required,
            effective,
            "CheckSystem",
            QUERY_ARGS,
            "check system workspace_info",
        )
        assert ok is False
        assert "Permission denied" in msg

    def test_gate_allows_query_when_system_read(self):
        """system=read => the permission gate permits a CheckSystem query."""
        session = SessionPermissions(system="read")
        workspace = WorkspaceCapabilities()
        effective = get_effective_permissions(session, workspace)

        required = CheckSystem.get_required_categories(QUERY_ARGS)
        ok, _msg = check_required_categories(
            required,
            effective,
            "CheckSystem",
            QUERY_ARGS,
            "check system workspace_info",
        )
        assert ok is True
