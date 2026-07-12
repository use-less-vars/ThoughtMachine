"""
Tests for restrictive permission merge in WorkerThread._build_agent_config().

Verifies that worker-level permissions are merged *restrictively* with
session permissions — the session acts as a ceiling that the worker
cannot exceed, though the worker may impose stricter limits.

Key behavioural change (2025-04):
  Old: union merge (worker could elevate, e.g. container: true over false)
  New: restrictive merge (session is ceiling — the strictest value wins)
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from tools.workspace.worker import (
    _restrictive_merge,
    _WORKER_BLOCKLIST,
    DEFAULT_WORKER_SYSTEM_PROMPT,
    WorkerThread,
)
from tools import SIMPLIFIED_TOOL_CLASSES


class TestRestrictiveMerge:
    """Direct tests of the _restrictive_merge helper."""

    def test_session_ceiling_wins_string(self):
        """When session is stricter than worker, session value prevails."""
        result = _restrictive_merge(
            {"execution": "deny"},
            {"execution": "allow"},
        )
        assert result == {"execution": "deny"}

    def test_worker_can_be_stricter_string(self):
        """Worker can reduce a permission (make it stricter)."""
        result = _restrictive_merge(
            {"execution": "allow"},
            {"execution": "deny"},
        )
        assert result == {"execution": "deny"}

    def test_equal_values_string(self):
        """Equal values are preserved."""
        result = _restrictive_merge(
            {"filesystem": "read"},
            {"filesystem": "read"},
        )
        assert result == {"filesystem": "read"}

    def test_filesystem_hierarchy(self):
        """'none' < 'read' < 'write'; the stricter of any pair wins."""
        pairs = [
            ({"filesystem": "none"}, {"filesystem": "read"}, "none"),
            ({"filesystem": "none"}, {"filesystem": "write"}, "none"),
            ({"filesystem": "read"}, {"filesystem": "write"}, "read"),
            ({"filesystem": "write"}, {"filesystem": "none"}, "none"),
        ]
        for session, worker, expected in pairs:
            result = _restrictive_merge(session, worker)
            assert result == {"filesystem": expected}, (
                f"Expected filesystem={expected} for session={session}, worker={worker}"
            )

    def test_container_string_values(self):
        """String 'deny'/'allow' for container."""
        result = _restrictive_merge(
            {"container": "deny"},
            {"container": "allow"},
        )
        assert result == {"container": "deny"}

    def test_session_false_ceiling_bool(self):
        """Session False (deny) overrides worker True (allow)."""
        result = _restrictive_merge(
            {"container": False},
            {"container": True},
        )
        assert result == {"container": False}

    def test_worker_false_overrides_session_true(self):
        """Worker False (deny) overrides session True (allow)."""
        result = _restrictive_merge(
            {"container": True},
            {"container": False},
        )
        assert result == {"container": False}

    def test_both_true(self):
        """Both True -> True."""
        result = _restrictive_merge(
            {"container": True},
            {"container": True},
        )
        assert result == {"container": True}

    def test_both_false(self):
        """Both False -> False."""
        result = _restrictive_merge(
            {"container": False},
            {"container": False},
        )
        assert result == {"container": False}

    def test_session_bool_worker_string(self):
        """Session bool False overrides worker string 'allow'."""
        result = _restrictive_merge(
            {"container": False},
            {"container": "allow"},
        )
        assert result == {"container": False}

    def test_session_string_worker_bool(self):
        """Session string 'deny' / worker bool True -> True."""
        result = _restrictive_merge(
            {"container": "deny"},
            {"container": True},
        )
        assert result == {"container": True}

    def test_session_missing_key(self):
        """Key only in worker is used as-is."""
        result = _restrictive_merge(
            {"filesystem": "read"},
            {"execution": "deny"},
        )
        assert result == {"filesystem": "read", "execution": "deny"}

    def test_worker_missing_key(self):
        """Key only in session is used as-is."""
        result = _restrictive_merge(
            {"filesystem": "read"},
            {},
        )
        assert result == {"filesystem": "read"}

    def test_both_empty(self):
        """Empty inputs -> empty result."""
        assert _restrictive_merge({}, {}) == {}

    def test_unknown_key_passthrough(self):
        """Unknown key (e.g. 'git', 'system') passes through with session wins."""
        result = _restrictive_merge(
            {"git": "read"},
            {"git": "write"},
        )
        assert result == {"git": "read"}


@pytest.fixture
def _mock_agent_config():
    """Patch AgentConfig inside worker.py so _build_agent_config returns a mock.

    Injects a stub module into sys.modules so the lazy import inside
    _build_agent_config (``from agent.config.models import AgentConfig``)
    succeeds without triggering the full ``agent`` package import chain
    (which requires optional dependencies like fast_json_repair).
    """
    mock_module = MagicMock()
    mock_cfg = MagicMock()
    mock_cfg.session_permissions = {}
    mock_module.AgentConfig = MagicMock(return_value=mock_cfg)

    old_module = sys.modules.get("agent.config.models")
    sys.modules["agent.config.models"] = mock_module

    yield mock_module.AgentConfig

    # Restore the original module so other tests are not affected
    if old_module is not None:
        sys.modules["agent.config.models"] = old_module
    else:
        del sys.modules["agent.config.models"]


def make_worker_thread(
    definition: dict,
    session_permissions: dict | None = None,
) -> WorkerThread:
    """Create a WorkerThread with minimal required params for testing."""
    return WorkerThread(
        name="test-worker",
        definition=definition,
        agent_config={
            "provider": "test-provider",
            "model": "test-model",
        },
        workspace_dir=MagicMock(),
        session_permissions=session_permissions or {},
        project_root="/tmp",
    )


class TestWorkerPermissionsMergeIntegration:
    """Integration tests via _build_agent_config."""

    def test_session_ceiling_enforced(self, _mock_agent_config):
        """Session False denies even if worker requests True."""
        session_perms = {"container": False, "filesystem": "read"}
        definition = {
            "name": "test-worker",
            "permission_footprint": {"container": True, "execution": "full"},
        }
        wt = make_worker_thread(definition, session_perms)

        with patch.object(wt, "_agent_config_dict", {"provider": "test", "model": "test"}):
            wt._build_agent_config()

        call_kwargs = _mock_agent_config.call_args[1]
        merged = call_kwargs.get("session_permissions", {})
        assert merged == {
            "container": False,
            "filesystem": "read",
            "execution": "full",
        }, f"Expected container=False (ceiling), got {merged}"

    def test_worker_can_strengthen(self, _mock_agent_config):
        """Worker can make a permission stricter than the session."""
        session_perms = {"container": True, "filesystem": "write"}
        definition = {
            "name": "test-worker",
            "permission_footprint": {"filesystem": "read", "execution": "deny"},
        }
        wt = make_worker_thread(definition, session_perms)

        with patch.object(wt, "_agent_config_dict", {"provider": "test", "model": "test"}):
            wt._build_agent_config()

        call_kwargs = _mock_agent_config.call_args[1]
        merged = call_kwargs.get("session_permissions", {})
        assert merged == {
            "container": True,
            "filesystem": "read",
            "execution": "deny",
        }, f"Expected filesystem=read (worker stricter), got {merged}"

    def test_backward_compat_worker_permissions_key(self, _mock_agent_config):
        """'worker_permissions' key (backward compat) is used when 'permission_footprint' not present."""
        session_perms = {"container": False, "filesystem": "read"}
        definition = {
            "name": "test-worker",
            "worker_permissions": {"filesystem": "none", "execution": "deny"},
        }
        wt = make_worker_thread(definition, session_perms)

        with patch.object(wt, "_agent_config_dict", {"provider": "test", "model": "test"}):
            wt._build_agent_config()

        call_kwargs = _mock_agent_config.call_args[1]
        merged = call_kwargs.get("session_permissions", {})
        assert merged == {
            "container": False,
            "filesystem": "none",
            "execution": "deny",
        }, f"Expected filesystem=none (worker stricter), got {merged}"

    def test_no_permission_footprint(self, _mock_agent_config):
        """When worker has neither permission_footprint nor worker_permissions, session permissions pass through unchanged."""
        session_perms = {"container": False, "filesystem": "read"}
        definition = {"name": "test-worker"}
        wt = make_worker_thread(definition, session_perms)

        with patch.object(wt, "_agent_config_dict", {"provider": "test", "model": "test"}):
            wt._build_agent_config()

        call_kwargs = _mock_agent_config.call_args[1]
        merged = call_kwargs.get("session_permissions", {})
        assert merged == {
            "container": False,
            "filesystem": "read",
        }, f"Expected unchanged session permissions, got {merged}"

    def test_worker_fills_gap(self, _mock_agent_config):
        """Worker adds a category not in session permissions."""
        session_perms = {"filesystem": "read"}
        definition = {
            "name": "test-worker",
            "permission_footprint": {"execution": "allow"},
        }
        wt = make_worker_thread(definition, session_perms)

        with patch.object(wt, "_agent_config_dict", {"provider": "test", "model": "test"}):
            wt._build_agent_config()

        call_kwargs = _mock_agent_config.call_args[1]
        merged = call_kwargs.get("session_permissions", {})
        assert merged == {
            "filesystem": "read",
            "execution": "allow",
        }, f"Expected worker to fill gap, got {merged}"

    def test_default_tool_set(self, _mock_agent_config):
        """When definition has no 'tools', enabled_tools = SIMPLIFIED_TOOL_CLASSES minus blocklist."""
        definition = {"name": "test-worker"}
        wt = make_worker_thread(definition)

        with patch.object(wt, "_agent_config_dict", {"provider": "test", "model": "test"}):
            wt._build_agent_config()

        call_kwargs = _mock_agent_config.call_args[1]
        enabled = call_kwargs.get("enabled_tools", [])
        expected = [
            cls.__name__ for cls in SIMPLIFIED_TOOL_CLASSES
            if cls.__name__ not in _WORKER_BLOCKLIST
        ]
        assert enabled == expected, (
            f"Expected enabled_tools to match SIMPLIFIED_TOOL_CLASSES minus blocklist, "
            f"got {set(enabled) ^ set(expected)} difference"
        )

    def test_default_system_prompt(self, _mock_agent_config):
        """When definition has no system_prompt, DEFAULT_WORKER_SYSTEM_PROMPT is used."""
        definition = {"name": "test-worker"}
        wt = make_worker_thread(definition)

        with patch.object(wt, "_agent_config_dict", {"provider": "test", "model": "test"}):
            wt._build_agent_config()

        call_kwargs = _mock_agent_config.call_args[1]
        prompt = call_kwargs.get("system_prompt", "")
        assert prompt == DEFAULT_WORKER_SYSTEM_PROMPT, (
            f"Expected DEFAULT_WORKER_SYSTEM_PROMPT, got {prompt[:80]!r}..."
        )

