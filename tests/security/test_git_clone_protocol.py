"""
Contract tests for the GitWriteTool clone-URL protocol allowlist (Task 2).

Verifies:
1.  Disallowed transports (``ext::`` shell executors, ``file://`` local
    access, ``ftp://``, empty strings, whitespace-padded URLs) are rejected
    by ``_validate_clone_url`` with a ``ValueError``.
2.  Allowed transports (``https://``, ``http://``, ``git://``, ``ssh://``,
    scp-like ``user@host:path``) pass validation without raising.
3.  ``execute(operation='clone', ...)`` surfaces the protocol ``ValueError``
    (returned as the error string) *before* any git subprocess would run
    (the atomic ``network:outbound`` gate is pre-approved via
    ``effective_permissions``).

**Docker note:** ``tests/security/conftest.py`` fixes ``sys.path`` so the
real ``tools/`` and ``security/`` packages are imported instead of the
pytest-injected ``tests/`` directory.
"""

import pytest

from tools.git_write_tool import ALLOWED_GIT_PROTOCOLS, GitWriteTool

# URLs that must be rejected. Every one of these would either be interpreted
# by `git clone` as a non-allowlisted transport (ext::, file://, ftp://) or
# is malformed/whitespace-padded.
INVALID_CLONE_URLS = [
    'ext::sh -c "echo pwned"',  # git ext:: transport — arbitrary command execution
    "file:///etc/passwd",  # local file read via file:// transport
    "ftp://evil.com/repo",  # non-allowlisted scheme
    "",  # empty string
    "  https://github.com/user/repo.git",  # leading whitespace (not stripped)
    "https://github.com/user/repo.git  ",  # trailing whitespace (not stripped)
]

# URLs that must be accepted by the protocol check (a real clone may still
# fail later at the subprocess level — that is out of scope here).
VALID_CLONE_URLS = [
    "https://github.com/user/repo.git",
    "HTTPS://github.com/user/repo.git",  # scheme comparison is case-insensitive
    "http://github.com/user/repo.git",
    "git://example.com/repo.git",
    "ssh://git@example.com/repo.git",
    "git@github.com:user/repo.git",  # scp-like syntax
]


class TestAllowlistConstant:
    def test_allowed_protocols_constant(self):
        """The module-level allowlist covers exactly the four schemes."""
        assert ALLOWED_GIT_PROTOCOLS == ["https://", "http://", "git://", "ssh://"]


class TestValidateCloneUrlRejects:
    @pytest.mark.parametrize("clone_url", INVALID_CLONE_URLS)
    def test_raises_value_error(self, clone_url):
        with pytest.raises(ValueError, match="Unsupported git protocol"):
            GitWriteTool._validate_clone_url(clone_url)

    def test_error_message_contains_url(self):
        """The ValueError message carries the offending URL."""
        url = "file:///etc/passwd"
        with pytest.raises(ValueError) as exc_info:
            GitWriteTool._validate_clone_url(url)
        assert str(exc_info.value) == f"Unsupported git protocol: {url}"


class TestValidateCloneUrlAccepts:
    @pytest.mark.parametrize("clone_url", VALID_CLONE_URLS)
    def test_returns_true(self, clone_url):
        """Accepted URLs return True instead of raising (no git subprocess)."""
        assert GitWriteTool._validate_clone_url(clone_url) is True


class TestExecuteSurfacesProtocolErrorBeforeSubprocess:
    @pytest.mark.parametrize(
        "clone_url",
        [
            'ext::sh -c "echo pwned"',
            "file:///etc/passwd",
            "ftp://evil.com/repo",
        ],
    )
    def test_execute_raises_value_error(self, clone_url):
        """execute() returns the protocol error before any git clone.

        The atomic network:outbound check is pre-approved (effective
        permissions "write"), so execution reaches the clone-URL validation,
        which must reject instead of handing the URL to a git subprocess.
        GitWriteTool.execute() catches the ValueError and returns it as the
        error string ("Error: Unsupported git protocol: <url>").
        """
        tool = GitWriteTool(
            operation="clone",
            clone_url=clone_url,
            effective_permissions={"network": "write"},
            agent_config={"session_permissions": {"git_write": "write"}},
        )
        result = tool.execute()
        assert isinstance(result, str)
        assert "Unsupported git protocol" in result

    def test_execute_without_network_permission_denied_by_gate(self):
        """Without network:outbound the atomic gate denies before validation."""
        tool = GitWriteTool(
            operation="clone",
            clone_url='ext::sh -c "echo pwned"',
            effective_permissions={"network": "banned"},
            agent_config={"session_permissions": {"git_write": "write"}},
        )
        result = tool.execute()
        assert isinstance(result, str)
        assert "Atomic permission check failed" in result
