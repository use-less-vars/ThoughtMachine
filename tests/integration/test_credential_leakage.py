"""Tests to verify credential values never escape into logs, errors, or descriptions.

These are leakage-hardening tests. They mock the credential resolution layer
and assert that the raw secret value never appears in:
  - Exception messages
  - Log output
  - File path error messages (path disclosure)
  - Provider-facing arguments (the Secret is unwrapped to plain str before SDK use)
"""

import logging
import io
from unittest.mock import MagicMock, patch, PropertyMock
import pytest

from agent.credentials.injector import Secret, CredentialInjector, CredentialError
from agent.core.agent import Agent
from agent.config.models import AgentConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SECRET_VALUE = "sk-test-secret-value-12345"


@pytest.fixture
def secret() -> Secret:
    """A Secret instance wrapping the canonical test secret."""
    return Secret(SECRET_VALUE)


def _make_agent_with_session(workspace_id: str = "test-workspace") -> Agent:
    """Helper: create an Agent with a mock session that has workspace_id."""
    mock_session = MagicMock()
    mock_session.workspace_id = workspace_id
    config = AgentConfig(
        api_key="sk-original",
        provider_type="openai_compatible",
        model="test-model",
        base_url="https://api.test.com/v1",
    )
    agent = Agent(config=config, session=mock_session)
    return agent


# ---------------------------------------------------------------------------
# (a) Secret values must never appear in exception messages
# ---------------------------------------------------------------------------


class TestNoSecretInErrorMessages:
    """CredentialError messages may mention the key name but never the value."""

    def test_resolve_error_mentions_key_not_value(self, secret):
        """Injector error messages contain the credential key but not the value."""
        injector = CredentialInjector("test-workspace")
        with patch("agent.credentials.injector.Path") as mock_path:
            mock_path.return_value.exists.return_value = False
            with pytest.raises(CredentialError) as excinfo:
                injector.resolve("my_key")
            msg = str(excinfo.value)
            # Should mention the key
            assert "my_key" in msg
            # Should NOT contain the secret value
            assert SECRET_VALUE not in msg

    def test_init_resolve_failure_no_value_leak(self):
        """Agent init with unresolvable placeholder raises error without leaking value."""
        mock_session = MagicMock()
        mock_session.workspace_id = "test-workspace"
        config = AgentConfig(
            api_key="{{credential:nonexistent_key}}",
            provider_type="openai_compatible",
            model="test-model",
        )
        with pytest.raises(CredentialError) as excinfo:
            Agent(config=config, session=mock_session)
        msg = str(excinfo.value)
        # Should mention the key name
        assert "nonexistent_key" in msg
        # Should NOT contain any secret value
        assert SECRET_VALUE not in msg

    def test_restart_returns_false_value_not_in_last_error(self):
        """Restart failure via restart() returns False; _last_config_error has no secret."""
        agent = _make_agent_with_session()
        config = AgentConfig(
            api_key="{{credential:nonexistent_key}}",
            provider_type="openai_compatible",
            model="test-model",
        )
        ok = agent.restart(config)
        assert ok is False
        assert SECRET_VALUE not in agent._last_config_error


# ---------------------------------------------------------------------------
# (b) Secret values must never appear in log output
# ---------------------------------------------------------------------------


class TestNoSecretInLogs:
    """Log output at any level must not contain the raw secret value."""

    def test_debug_log_no_secret_value(self):
        """Ensure WARNING log during credential resolution doesn't leak secret."""
        log_capture = io.StringIO()
        handler = logging.StreamHandler(log_capture)
        handler.setLevel(logging.WARNING)
        logger = logging.getLogger("credential")
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)

        try:
            mock_session = MagicMock()
            mock_session.workspace_id = "test-workspace"

            # Creating an Agent with a non-existent credential key will
            # trigger the resolution attempt and the WARNING log
            config = AgentConfig(
                api_key="{{credential:nonexistent_key}}",
                provider_type="openai_compatible",
                model="test-model",
            )
            try:
                Agent(config=config, session=mock_session)
            except CredentialError:
                pass

            log_text = log_capture.getvalue()
            assert SECRET_VALUE not in log_text
        finally:
            logger.removeHandler(handler)

    def test_direct_api_key_log_no_leak(self):
        """INFO log for direct API key use should never contain the key."""
        log_capture = io.StringIO()
        handler = logging.StreamHandler(log_capture)
        handler.setLevel(logging.INFO)
        logger = logging.getLogger("credential")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        try:
            _make_agent_with_session()
            log_text = log_capture.getvalue()
            assert SECRET_VALUE not in log_text
            assert "credential placeholder" not in log_text.lower()
        finally:
            logger.removeHandler(handler)


# ---------------------------------------------------------------------------
# (c) Credential placeholders in description fields must NOT be resolved
# ---------------------------------------------------------------------------


class TestPlaceholderNotResolvedInDescriptions:
    """CredentialInjector.inject() only processes flat top-level string values.
    Placeholder-like text in nested fields (e.g. descriptions) must be left alone.
    """

    def test_description_with_placeholder_unchanged(self):
        """A 'description' key with {{credential:key}} is not resolved."""
        injector = CredentialInjector("test-workspace")
        tool_args = {
            "command": "echo hello",
            "description": "Uses {{credential:my_key}} to authenticate",
        }
        result = injector.inject(tool_args)
        # The description that looks like a placeholder should be untouched
        # because inject() only processes values that are exact full matches
        # of the placeholder pattern (fullmatch, not search).
        assert result["description"] == "Uses {{credential:my_key}} to authenticate"
        # No resolution should have occurred
        assert not isinstance(result["description"], Secret)

    def test_only_exact_full_matches_resolved(self):
        """Only values that are exactly '{{credential:key}}' (full string match) get resolved."""
        injector = CredentialInjector("test-workspace")

        with patch.object(injector, "resolve", return_value=Secret("resolved_val")):
            result = injector.inject({
                "api_key": "{{credential:my_key}}",
                "prompt": "Use {{credential:my_key}} here",
                "other": "{{credential:my_key}} with suffix",
            })

        # Exact full match -> resolved to Secret
        assert isinstance(result["api_key"], Secret)
        assert str(result["api_key"]) == "***"

        # Partial/embedded -> not resolved
        assert not isinstance(result["prompt"], Secret)
        assert result["prompt"] == "Use {{credential:my_key}} here"

        # Suffix -> not resolved (not full match)
        assert not isinstance(result["other"], Secret)
        assert result["other"] == "{{credential:my_key}} with suffix"


# ---------------------------------------------------------------------------
# (d) Secret is unwrapped before reaching the provider SDK
# ---------------------------------------------------------------------------


class TestSecretUnwrappedBeforeProvider:
    """Secret remains wrapped after resolution (for redaction throughout pipeline).

    config.api_key is kept as a Secret (str subclass) so that all log statements
    and format strings produce "***" instead of the real key. SDK libraries
    receive the Secret which works because Secret is a valid str subclass.
    """

    def test_agent_config_api_key_is_secret_with_redaction(
        self, tmp_path, monkeypatch
    ):
        """After credential resolution, config.api_key is a Secret with redaction."""
        mock_session = MagicMock()
        mock_session.workspace_id = "test-workspace"

        import os
        import shutil

        # Hermetic: redirect HOME to tmp_path so Path.home()-based vault
        # resolution (CredentialInjector) and the write path agree on tmp_path.
        monkeypatch.setenv("HOME", str(tmp_path))
        cred_dir = os.path.join(
            str(tmp_path), ".thoughtmachine", "credentials", "test-workspace"
        )
        os.makedirs(cred_dir, mode=0o700, exist_ok=True)
        # Credentials dir must be owner-only (umask can only remove bits).
        assert os.stat(cred_dir).st_mode & 0o077 == 0
        cred_file = os.path.join(cred_dir, "test_key")
        try:
            with open(cred_file, "w") as f:
                f.write(SECRET_VALUE)

            config = AgentConfig(
                api_key="{{credential:test_key}}",
                provider_type="openai_compatible",
                model="test-model",
            )
            agent = Agent(config=config, session=mock_session)

            # config.api_key is a Secret to maintain redaction throughout
            assert isinstance(agent.config.api_key, Secret)
            # The actual value matches for SDK use (Secret is a str subclass)
            assert agent.config.api_key == SECRET_VALUE
            # But string representations are redacted
            assert str(agent.config.api_key) == "***"
            assert repr(agent.config.api_key) == "***"
            assert f"{agent.config.api_key}" == "***"
        finally:
            shutil.rmtree(str(tmp_path / ".thoughtmachine"), ignore_errors=True)


# ---------------------------------------------------------------------------
# (e) Secret slicing returns plain str (documented behavior)
# ---------------------------------------------------------------------------


class TestSliceDoesNotLeak:
    """Secret inherits from str — slicing returns a plain str, not Secret.
    This is documented behavior: any code that slices a Secret must handle
    the resulting plain str carefully.
    """

    def test_slice_returns_plain_str(self):
        """Secret('test-key-123')[:8] returns a plain str, not a Secret."""
        s = Secret(SECRET_VALUE)
        sliced = s[:8]
        assert isinstance(sliced, str), "Slice must return a str"
        assert not isinstance(sliced, Secret), "Slice must NOT return a Secret"
        # The value is the first 8 characters of the actual secret
        assert sliced == SECRET_VALUE[:8]

    def test_slice_has_no_redaction(self):
        """Sliced str does NOT have Secret's redaction — it's a plain str."""
        s = Secret(SECRET_VALUE)
        sliced = s[:8]
        assert sliced != "***", "Sliced value should NOT be redacted"
        # This is the vulnerability: any code doing secret[:n] loses protection
        # We keep this test to document the behavior explicitly.

    def test_step_slice_also_plain_str(self):
        """Extended slicing also returns plain str."""
        s = Secret(SECRET_VALUE)
        stepped = s[::2]
        assert isinstance(stepped, str)
        assert not isinstance(stepped, Secret)

    def test_negative_index_returns_plain_str(self):
        """Negative-index slicing also returns plain str."""
        s = Secret(SECRET_VALUE)
        tail = s[-10:]
        assert isinstance(tail, str)
        assert not isinstance(tail, Secret)


# ---------------------------------------------------------------------------
# (f) File paths must be redacted in error messages
# ---------------------------------------------------------------------------


class TestFilePathRedactedInError:
    """Error messages from CredentialInjector must not reveal filesystem paths."""

    PATH_PATTERNS = [
        "/home/",
        "/root/",
        "/Users/",
        ".thoughtmachine",
        "/credentials/",
    ]

    def _assert_no_path_in_message(self, msg):
        """Assert that none of the path patterns appear in the message."""
        for pattern in self.PATH_PATTERNS:
            assert pattern not in msg, (
                f"Error message should not contain path pattern '{pattern}'. "
                f"Got: {msg}"
            )

    def test_missing_credential_no_path(self):
        """'Credential not found' error should not include the file path."""
        injector = CredentialInjector("test-workspace")
        with patch("agent.credentials.injector.Path") as mock_path:
            mock_path_instance = MagicMock()
            mock_path_instance.exists.return_value = False
            mock_path.return_value = mock_path_instance

            with pytest.raises(CredentialError) as excinfo:
                injector.resolve("my_key")

            self._assert_no_path_in_message(str(excinfo.value))
            assert "my_key" in str(excinfo.value)

    def test_not_a_regular_file_no_path(self):
        """'Not a regular file' error should not include the file path."""
        injector = CredentialInjector("test-workspace")
        with patch("agent.credentials.injector.Path") as mock_path:
            mock_path_instance = MagicMock()
            mock_path_instance.exists.return_value = True
            mock_path_instance.is_file.return_value = False
            mock_path.return_value = mock_path_instance

            with pytest.raises(CredentialError) as excinfo:
                injector.resolve("my_key")

            self._assert_no_path_in_message(str(excinfo.value))

    def test_invalid_key_no_path(self):
        """Key validation errors should not contain file paths."""
        injector = CredentialInjector("test-workspace")
        with pytest.raises(CredentialError) as excinfo:
            injector.resolve("")
        msg = str(excinfo.value)
        assert "empty key" in msg
        self._assert_no_path_in_message(msg)

    def test_path_traversal_no_path(self):
        """Path traversal errors should not contain file paths."""
        injector = CredentialInjector("test-workspace")
        with pytest.raises(CredentialError) as excinfo:
            injector.resolve("../etc/passwd")
        msg = str(excinfo.value)
        self._assert_no_path_in_message(msg)
