"""
Contract tests for the KnowledgeBaseTool path-traversal remediation (Task 1).

Verifies:
1.  Traversal domains (``..``, absolute paths, ``~`` home shortcuts) are
    rejected by ``_resolve_domain_path`` with a ``ValueError`` containing
    ``'escapes'``/``'traversal'``, and the public ``execute()`` API surfaces the
    same error message instead of reading outside the knowledge base.
2.  A legitimate custom domain still resolves and reads back successfully.
3.  Read operations (read/search/list) now require ``filesystem:read`` at the
    permission gate (previously they required zero permissions).

**Docker note:** ``tests/security/conftest.py`` fixes ``sys.path`` so the real
``security/`` package is imported instead of the pytest-injected ``tests/`` dir.
"""

import pytest

from security.security_gate import check_required_categories, get_effective_permissions
from thoughtmachine.security import SessionPermissions
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities
from tools.knowledge_base import KnowledgeBaseTool

TRAVERSAL_DOMAINS = [
    "../../../../../.thoughtmachine/credentials/providers",
    "/etc/passwd",
    "~/.ssh/id_rsa",
]


def _kb_root(tmp_path):
    """Knowledge base root used by KnowledgeBaseTool for a given workspace."""
    return tmp_path / ".thoughtmachine" / "knowledge"


def _make_tool(tmp_path, domain, mode="read"):
    """Build a workspace-scope KnowledgeBaseTool rooted in tmp_path."""
    return KnowledgeBaseTool(
        mode=mode,
        domain=domain,
        workspace_path=str(tmp_path),
    )


class TestTraversalDomainsRejected:
    @pytest.mark.parametrize("domain", TRAVERSAL_DOMAINS)
    def test_resolver_raises_value_error(self, tmp_path, domain):
        """The path resolver rejects escaping domains with a ValueError."""
        tool = _make_tool(tmp_path, domain)
        domain_name = domain.lower().replace(" ", "_")
        with pytest.raises(ValueError) as exc_info:
            tool._resolve_domain_path(_kb_root(tmp_path), domain_name)
        message = str(exc_info.value)
        assert "escapes" in message or "traversal" in message

    @pytest.mark.parametrize("domain", TRAVERSAL_DOMAINS)
    def test_execute_blocks_without_reading(self, tmp_path, domain):
        """execute() surfaces the guard error; nothing outside the KB is read.

        The mode handlers convert the resolver's ValueError into an error
        string (existing convention for unknown domains), so we assert the
        surfaced message rather than an exception from execute().
        """
        tool = _make_tool(tmp_path, domain)
        result = tool.execute()
        assert isinstance(result, str)
        assert "escapes" in result or "traversal" in result


class TestLegitimateDomain:
    def test_custom_domain_roundtrip(self, tmp_path):
        """A legitimate domain file is created and read back successfully."""
        kb = _kb_root(tmp_path)
        (kb / "project").mkdir(parents=True, exist_ok=True)
        (kb / "project" / "my_domain.md").write_text(
            "# My Domain\n\nSecret KB content: 42\n", encoding="utf-8"
        )

        tool = _make_tool(tmp_path, "my_domain")
        result = tool.execute()
        assert "Secret KB content: 42" in result


class TestReadRequiresFilesystemPermission:
    @pytest.mark.parametrize("mode", ["read", "search", "list"])
    def test_read_modes_require_filesystem_read(self, mode):
        assert KnowledgeBaseTool.get_required_categories({"mode": mode}) == ["filesystem:read"]

    def test_write_modes_unchanged(self):
        assert KnowledgeBaseTool.get_required_categories({"mode": "append"}) == ["filesystem:write"]
        assert KnowledgeBaseTool.get_required_categories({"mode": "update"}) == ["filesystem:write"]
        assert KnowledgeBaseTool.get_required_categories({"mode": "create_domain"}) == ["filesystem:write"]

    def test_status_mode_unchanged(self):
        assert KnowledgeBaseTool.get_required_categories({"mode": "status"}) == []

    def test_gate_denies_read_when_filesystem_banned(self):
        """filesystem=banned ⇒ the permission gate denies a KB read."""
        session = SessionPermissions(filesystem="banned")
        workspace = WorkspaceCapabilities()
        effective = get_effective_permissions(session, workspace)

        required = KnowledgeBaseTool.get_required_categories({"mode": "read", "domain": "my_domain"})
        ok, msg = check_required_categories(
            required,
            effective,
            "KnowledgeBaseTool",
            {"mode": "read", "domain": "my_domain"},
            "read knowledge base domain my_domain",
        )
        assert ok is False
        assert "Permission denied" in msg

    def test_gate_allows_read_when_filesystem_read(self):
        """filesystem=read ⇒ the permission gate permits a KB read."""
        session = SessionPermissions(filesystem="read")
        workspace = WorkspaceCapabilities()
        effective = get_effective_permissions(session, workspace)

        required = KnowledgeBaseTool.get_required_categories({"mode": "read", "domain": "my_domain"})
        ok, _msg = check_required_categories(
            required,
            effective,
            "KnowledgeBaseTool",
            {"mode": "read", "domain": "my_domain"},
            "read knowledge base domain my_domain",
        )
        assert ok is True
