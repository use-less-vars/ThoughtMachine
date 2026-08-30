"""Tests: CheckSystem uses RUNTIME-injected state (agent_config, permissions).

Covers the runtime-truth guarantees:
- 'my_config' reflects the agent_config injected by ToolExecutor, never disk config.
- 'effective_permissions' is computed from the runtime session permissions via
  get_effective_permissions (gate), never from stale file state.
- 'runtime_state' returns a redacted snapshot (token limits, worker caps,
  container status, permission levels, allowlist, vault status) and NEVER
  exposes api_key / secret material.

Style mirrors tests/tools/test_check_system.py (allowlist pinned via patching,
no live vault/Docker daemon needed).
"""

import json
from unittest.mock import MagicMock, patch

from tools.workspace.check_system import CheckSystem


def _parse_result(result: str) -> dict:
    return json.loads(result)


BASE_KWARGS = {
    "session_permissions": {"filesystem": "read", "network": "write"},
}


def test_my_config_uses_runtime_not_disk():
    """my_config reflects the injected agent_config, never disk config files."""
    injected = {
        "provider": "openai",
        "model": "gpt-test",
        "api_key": "sk-secret",
        "temperature": 0.3,
    }
    with patch.object(
        CheckSystem, "_load_allowlist_from_vault", return_value=["my_config"]
    ), patch(
        "agent.config.config_manager.load_global_defaults",
        create=True,
        side_effect=AssertionError("my_config must come from injected runtime config"),
    ):
        tool = CheckSystem(query="my_config", agent_config=injected, **BASE_KWARGS)
        result = _parse_result(tool.execute())
    assert result["provider"] == "openai"
    assert result["model"] == "gpt-test"
    assert result["temperature"] == 0.3
    # The real secret never appears in the JSON output.
    assert "sk-secret" not in json.dumps(result)


def test_effective_permissions_uses_runtime():
    """effective_permissions comes from get_effective_permissions(session, caps)."""
    mock_perms = MagicMock(return_value={"filesystem": "read", "network": "deny"})
    with patch.object(
        CheckSystem, "_load_allowlist_from_vault", return_value=["effective_permissions"]
    ), patch("tools.workspace.check_system.get_effective_permissions", mock_perms):
        tool = CheckSystem(query="effective_permissions", **BASE_KWARGS)
        result = _parse_result(tool.execute())
    assert result["effective_permissions"] == {"filesystem": "read", "network": "deny"}
    assert result["source"] == "gate"
    call_args = mock_perms.call_args
    session_obj = call_args[0][0]
    assert getattr(session_obj, "filesystem", None) == "read"


def test_runtime_state_returns_no_secrets(tmp_path):
    """runtime_state exposes only redacted, aggregate state — never api_key."""
    injected = {
        "provider": "openai",
        "model": "gpt-test",
        "api_key": "sk-secret",
        "max_workers_per_session": 2,
        "worker_timeout_seconds": 60,
        "worker_max_retries": 3,
    }
    with patch.object(
        CheckSystem, "_load_allowlist_from_vault", return_value=["runtime_state"]
    ), patch("thoughtmachine.vault.vault_root", return_value=tmp_path / "vault"):
        tool = CheckSystem(query="runtime_state", agent_config=injected, **BASE_KWARGS)
        text = tool.execute()
    result = _parse_result(text)
    assert result["status"] == "ok"
    for key in (
        "token_limits",
        "worker_limits",
        "container_status",
        "effective_permission_levels",
        "allowlist_count",
        "vault_status",
    ):
        assert key in result, f"missing key {key}"
    assert result["worker_limits"]["max_workers_per_session"] == 2
    assert result["worker_limits"]["worker_timeout_seconds"] == 60
    assert result["worker_limits"]["worker_max_retries"] == 3
    assert "sk-secret" not in text
    assert '"api_key"' not in text
