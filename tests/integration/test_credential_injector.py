"""Integration tests for the CredentialInjector."""

import os

import pytest

from agent.credentials import CredentialError, Secret, CredentialInjector


class TestCredentialInjector:
    """Verify credential injection pipeline."""

    @pytest.fixture
    def vault_credentials(self, tmp_path):
        """Create a temporary vault credentials directory with test files."""
        # Simulate ~/.thoughtmachine/credentials/test-workspace/
        cred_dir = tmp_path / ".thoughtmachine" / "credentials" / "test-workspace"
        cred_dir.mkdir(parents=True, exist_ok=True)

        # Write test credential files (one file per credential, plain text)
        (cred_dir / "github_token").write_text("ghp_1234567890abcdef")
        (cred_dir / "amazon_api_key").write_text("AKIAIOSFODNN7EXAMPLE")
        (cred_dir / "multi_line").write_text("line1\nline2\n")  # trailing newline

        # Monkey-patch the home directory for CredentialInjector
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(tmp_path)
        yield cred_dir
        if old_home is not None:
            os.environ["HOME"] = old_home
        else:
            del os.environ["HOME"]

    def test_resolve_simple(self, vault_credentials):
        injector = CredentialInjector("test-workspace")
        secret = injector.resolve("github_token")
        assert isinstance(secret, Secret)
        assert secret == "ghp_1234567890abcdef"  # str comparison works
        assert repr(secret) == "***"
        assert str(secret) == "***"
        assert f"{secret}" == "***"  # __format__ redaction

    def test_resolve_missing(self, vault_credentials):
        injector = CredentialInjector("test-workspace")
        with pytest.raises(CredentialError, match="not found"):
            injector.resolve("nonexistent_cred")

    def test_resolve_traversal_key(self, vault_credentials):
        injector = CredentialInjector("test-workspace")
        with pytest.raises(CredentialError, match="Invalid credential key"):
            injector.resolve("../../../etc/passwd")
        with pytest.raises(CredentialError, match="Invalid credential key"):
            injector.resolve("../other/file")
        with pytest.raises(CredentialError, match="Invalid credential key"):
            injector.resolve("/absolute/path")
        with pytest.raises(CredentialError, match="Invalid credential key"):
            injector.resolve("with/slash")

    def test_resolve_trailing_newline_stripped(self, vault_credentials):
        injector = CredentialInjector("test-workspace")
        secret = injector.resolve("multi_line")
        # Should strip only the final trailing newline, not internal ones
        assert secret == "line1\nline2"

    def test_inject_flat_dict(self, vault_credentials):
        injector = CredentialInjector("test-workspace")
        args = {
            "api_key": "{{credential:github_token}}",
            "region": "us-west-2",
            "count": 3,
        }
        result = injector.inject(args)
        assert result["api_key"] == "ghp_1234567890abcdef"
        assert isinstance(result["api_key"], Secret)
        assert result["region"] == "us-west-2"  # unchanged
        assert result["count"] == 3  # unchanged (non-string)
        # Original unchanged
        assert args["api_key"] == "{{credential:github_token}}"

    def test_inject_missing_credential(self, vault_credentials):
        injector = CredentialInjector("test-workspace")
        args = {"key": "{{credential:does_not_exist}}"}
        with pytest.raises(CredentialError):
            injector.inject(args)

    def test_no_placeholder_passthrough(self, vault_credentials):
        injector = CredentialInjector("test-workspace")
        args = {"msg": "hello world", "num": 42}
        result = injector.inject(args)
        assert result == args
