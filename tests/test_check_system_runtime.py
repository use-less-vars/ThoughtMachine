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
    # Bind the module object CURRENT at call time: security-gate suites in this
    # repo purge/replace sys.modules entries for tools.workspace.check_system
    # while running, so a collection-time import can point at a stale module
    # whose globals a string-based patch no longer reaches. Same idiom as
    # tests/test_session_load_ceiling.py (patch the live module, then build
    # the tool from that same module object).
    import tools.workspace.check_system as cs_module

    mock_perms = MagicMock(return_value={"filesystem": "read", "network": "deny"})
    with patch.object(
        cs_module.CheckSystem, "_load_allowlist_from_vault",
        return_value=["effective_permissions"],
    ), patch.object(cs_module, "get_effective_permissions", mock_perms), patch.object(
        cs_module, "GATE_AVAILABLE", True
    ):
        tool = cs_module.CheckSystem(query="effective_permissions", **BASE_KWARGS)
        result = _parse_result(tool.execute())
    assert result["effective_permissions"] == {"filesystem": "read", "network": "deny"}
    assert result["source"] == "gate"
    # No injected disk-effective dict here (direct construction): the legacy
    # 2-arg mirror merge is the documented fallback.
    assert result["permission_origin"] == "mirror-merge"
    call_args = mock_perms.call_args
    session_obj = call_args[0][0]
    assert getattr(session_obj, "filesystem", None) == "read"


def test_effective_permissions_prefers_injected_disk_merge():
    """A non-empty injected effective_permissions (ToolExecutor's per-call DISK
    merge) wins over the stale session-start mirror in session_permissions.

    Regression: after the operator downgraded filesystem write -> read on disk,
    CheckSystem kept reporting filesystem:write because _query_permissions rebuilt
    SessionPermissions from the never-refreshed in-memory mirror. The injected
    dict is computed fresh each call from disk grants + ceiling, so it must be
    reported verbatim and the mirror merge must NOT run.
    """
    stale_mirror = {"filesystem": "write", "network": "banned"}
    disk_effective = {"filesystem": "read", "network": "banned"}
    with patch.object(
        CheckSystem, "_load_allowlist_from_vault", return_value=["effective_permissions"]
    ), patch(
        "tools.workspace.check_system.get_effective_permissions",
        side_effect=AssertionError(
            "mirror merge must not run when the disk-effective dict is injected"
        ),
    ):
        tool = CheckSystem(
            query="effective_permissions",
            session_permissions=stale_mirror,
            effective_permissions=disk_effective,
        )
        result = _parse_result(tool.execute())
    assert result["effective_permissions"] == disk_effective
    assert result["permission_origin"] == "executor-disk"
    assert result["permission_fetch_error"] is None


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


def test_effective_permissions_reports_canonical_git_only():
    """executor-disk dict folds the git grains into a single canonical git level."""
    with patch.object(
        CheckSystem, "_load_allowlist_from_vault", return_value=["effective_permissions"]
    ):
        tool = CheckSystem(
            query="effective_permissions",
            effective_permissions={
                "filesystem": "read",
                "network": "banned",
                "git": "write",
                "git_read": "write",
                "git_write": "write",
            },
        )
        result = _parse_result(tool.execute())
    assert result["effective_permissions"] == {
        "filesystem": "read",
        "network": "banned",
        "git": "write",
    }
    assert result["permission_origin"] == "executor-disk"


def test_effective_permissions_session_mirror_reports_canonical_git_without_gate():
    """session-mirror fallback still reports a canonical git level (display-only)."""
    with patch.object(
        CheckSystem, "_load_allowlist_from_vault", return_value=["effective_permissions"]
    ), patch("tools.workspace.check_system.GATE_AVAILABLE", False):
        tool = CheckSystem(
            query="effective_permissions",
            session_permissions={
                "filesystem": "read",
                "git": "write_on_feature_branch",
            },
        )
        result = _parse_result(tool.execute())
    assert result["effective_permissions"] == {
        "filesystem": "read",
        "git": "write_on_feature_branch",
    }
    assert result["permission_origin"] == "session-mirror"

