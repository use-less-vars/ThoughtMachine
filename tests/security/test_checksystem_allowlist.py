"""
Tests for CheckSystem allowlist enforcement — verifying ALL queries are
checked against the vault allowlist with no bypass.

Background: The original code only checked 5 of 12 queries (my_config,
dockerfile, network_diagnostics, event_log, capabilities), leaving the other 7
(effective_permissions, container_status, workspace_info, workers,
running_workers, event_bus_status, mcp_servers) and worker/<name> prefix
queries with no allowlist enforcement whatsoever.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from tools.workspace.check_system import CheckSystem


# The full set of allowed queries from resources/checksystem_allowlist.json
ALL_ALLOWED_QUERIES = [
    "capabilities",
    "container_status",
    "dockerfile",
    "effective_permissions",
    "event_bus_status",
    "event_log",
    "mcp_servers",
    "my_config",
    "network_diagnostics",
    "running_workers",
    "workers",
    "workspace_info",
]

FULL_ALLOWLIST = list(ALL_ALLOWED_QUERIES)

# Minimal kwargs needed for constructing CheckSystem instances
BASE_KWARGS = {
    "session_permissions": {"filesystem": "read", "network": "write"},
}


def _parse_result(output: str) -> dict:
    """Safely parse execute() output as dict.

    Some handlers (e.g. ``_query_event_log``) return a plain string
    that is not valid JSON.  In that case we wrap it so callers can
    still check ``status`` and ``error`` keys.
    """
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return {"raw": output}


# ══════════════════════════════════════════════════════════════════════════
#  _check_path_allowed unit tests (core logic, no execute() needed)
# ══════════════════════════════════════════════════════════════════════════


class TestCheckPathAllowed:
    """Direct unit tests for _check_path_allowed()."""

    def test_allowed_query_returns_true(self):
        tool = CheckSystem(query="workspace_info", allowlist=FULL_ALLOWLIST)
        assert tool._check_path_allowed("workspace_info") is True

    def test_all_12_allowed_queries_pass(self):
        """Every entry in the allowlist should be accepted."""
        tool = CheckSystem(query="workspace_info", allowlist=FULL_ALLOWLIST)
        for q in ALL_ALLOWED_QUERIES:
            assert tool._check_path_allowed(q) is True, f"'{q}' should be allowed"

    def test_blocked_query_returns_false(self):
        tool = CheckSystem(query="unknown_query", allowlist=FULL_ALLOWLIST)
        assert tool._check_path_allowed("unknown_query") is False

    def test_allowlist_none_degraded_allows_all(self):
        """When allowlist is None (vault unavailable), every query is allowed."""
        tool = CheckSystem(query="anything", allowlist=None)
        assert tool._check_path_allowed("anything") is True
        assert tool._check_path_allowed("../../../etc/passwd") is True
        assert tool._check_path_allowed("") is True
        assert tool._check_path_allowed("workers/foo") is True

    def test_empty_allowlist_blocks_all(self):
        """An empty allowlist means nothing passes."""
        tool = CheckSystem(query="anything", allowlist=[])
        assert tool._check_path_allowed("anything") is False
        assert tool._check_path_allowed("workspace_info") is False

    def test_worker_prefix_check_resolves_to_workers(self):
        """worker/<name> should check 'workers' (base) against the allowlist."""
        tool = CheckSystem(query="worker/default", allowlist=FULL_ALLOWLIST)
        assert tool._check_path_allowed("workers") is True

    def test_worker_prefix_blocked_when_workers_not_allowed(self):
        """worker/<name> blocked when 'workers' not in allowlist."""
        restricted = [q for q in FULL_ALLOWLIST if q != "workers"]
        tool = CheckSystem(query="worker/default", allowlist=restricted)
        assert tool._check_path_allowed("workers") is False


# ══════════════════════════════════════════════════════════════════════════
#  execute() integration tests (mock vault to control allowlist loading)
# ══════════════════════════════════════════════════════════════════════════


class TestExecuteAllowlistEnforcement:
    """Test that execute() enforces allowlist for ALL queries."""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make(**overrides) -> CheckSystem:
        """Create a CheckSystem with base kwargs + overrides."""
        params = dict(BASE_KWARGS)
        params.update(overrides)
        return CheckSystem(**params)

    # ------------------------------------------------------------------
    # Degraded mode (allowlist = None, vault unavailable)
    # ------------------------------------------------------------------

    def test_degraded_mode_allows_any_query(self):
        """When vault is unavailable (allowlist=None), all queries are attempted."""
        with patch.object(
            CheckSystem, "_load_allowlist_from_vault", return_value=None
        ):
            tool = self._make(query="some_random_query")
            result = _parse_result(tool.execute())
            # Should be "Unknown query", not "denied"
            assert result.get("status") != "denied"
            assert "Unknown query" in result.get("error", "")

    def test_degraded_mode_allows_valid_queries(self):
        """Degraded mode queries still execute their handlers."""
        with patch.object(
            CheckSystem, "_load_allowlist_from_vault", return_value=None
        ):
            tool = self._make(query="workspace_info")
            result = _parse_result(tool.execute())
            assert result.get("status") != "denied"

    # ------------------------------------------------------------------
    # Allowed queries (with vault)
    # ------------------------------------------------------------------

    def test_all_allowed_queries_not_denied(self):
        """Every query in the allowlist should pass the allowlist check."""
        for query in ALL_ALLOWED_QUERIES:
            with patch.object(
                CheckSystem, "_load_allowlist_from_vault", return_value=FULL_ALLOWLIST
            ):
                tool = self._make(query=query)
                result = _parse_result(tool.execute())
                assert result.get("status") != "denied", (
                    f"Query '{query}' was incorrectly denied"
                )

    # ------------------------------------------------------------------
    # Blocked queries
    # ------------------------------------------------------------------

    def test_blocked_query_rejected(self):
        """A query not in the allowlist is denied."""
        with patch.object(
            CheckSystem, "_load_allowlist_from_vault", return_value=FULL_ALLOWLIST
        ):
            tool = self._make(query="unknown_query")
            result = _parse_result(tool.execute())
            assert result.get("status") == "denied"
            assert "unknown_query" in result.get("error", "")

    def test_path_like_query_rejected(self):
        """A path-like string is rejected (not in allowlist)."""
        with patch.object(
            CheckSystem, "_load_allowlist_from_vault", return_value=FULL_ALLOWLIST
        ):
            tool = self._make(query="../../etc/passwd")
            result = _parse_result(tool.execute())
            assert result.get("status") == "denied"

    def test_empty_string_query_rejected(self):
        """An empty query string should be rejected."""
        with patch.object(
            CheckSystem, "_load_allowlist_from_vault", return_value=FULL_ALLOWLIST
        ):
            tool = self._make(query="")
            result = _parse_result(tool.execute())
            assert result.get("status") == "denied"

    # ------------------------------------------------------------------
    # worker/<name> prefix handling
    # ------------------------------------------------------------------

    def test_worker_prefix_allowed(self):
        """worker/<name> is handled when 'workers' is in the allowlist."""
        with patch.object(
            CheckSystem, "_load_allowlist_from_vault", return_value=FULL_ALLOWLIST
        ):
            tool = self._make(query="worker/test_worker")
            result = _parse_result(tool.execute())
            # Should not be denied — it will try to look up the worker
            assert result.get("status") != "denied", (
                "worker/test_worker was incorrectly denied"
            )

    def test_worker_prefix_blocked_when_workers_not_allowed(self):
        """worker/<name> denied when 'workers' not in allowlist."""
        restricted = [q for q in FULL_ALLOWLIST if q != "workers"]
        with patch.object(
            CheckSystem, "_load_allowlist_from_vault", return_value=restricted
        ):
            tool = self._make(query="worker/some_name")
            result = _parse_result(tool.execute())
            assert result.get("status") == "denied"
            err = result.get("error", "").lower()
            assert "worker" in err, f"Error should mention 'worker': {err}"

    # ------------------------------------------------------------------
    # Edge: previously-unchecked queries are now enforced
    # ------------------------------------------------------------------

    @pytest.mark.parametrize(
        "previously_unchecked_query",
        [
            "effective_permissions",
            "container_status",
            "workspace_info",
            "workers",
            "running_workers",
            "event_bus_status",
            "mcp_servers",
        ],
    )
    def test_previously_unchecked_queries_now_blocked(self, previously_unchecked_query):
        """The 7 previously-unchecked queries are now subject to allowlist."""
        # Create an allowlist that explicitly excludes this query
        restricted = [q for q in FULL_ALLOWLIST if q != previously_unchecked_query]
        with patch.object(
            CheckSystem, "_load_allowlist_from_vault", return_value=restricted
        ):
            tool = self._make(query=previously_unchecked_query)
            result = _parse_result(tool.execute())
            assert result.get("status") == "denied", (
                f"'{previously_unchecked_query}' should be denied when not in allowlist"
            )

    # ------------------------------------------------------------------
    # Vault-loaded allowlist (end-to-end)
    # ------------------------------------------------------------------

    def test_vault_loaded_allowlist_allows_valid(self):
        """Allowlist loaded via vault still allows valid queries."""
        with patch(
            "tools.workspace.check_system.get_checksystem_allowlist",
            return_value=FULL_ALLOWLIST,
        ):
            tool = self._make(query="workspace_info")
            result = _parse_result(tool.execute())
            assert result.get("status") != "denied"

    def test_vault_loaded_allowlist_blocks_invalid(self):
        """Allowlist loaded via vault blocks invalid queries."""
        with patch(
            "tools.workspace.check_system.get_checksystem_allowlist",
            return_value=FULL_ALLOWLIST,
        ):
            tool = self._make(query="some_malicious_query")
            result = _parse_result(tool.execute())
            assert result.get("status") == "denied"


# ══════════════════════════════════════════════════════════════════════════
#  Worker/<name> allowlist edge cases
# ══════════════════════════════════════════════════════════════════════════


class TestWorkerPrefixAllowlist:
    """Dedicated tests for worker/<name> → 'workers' allowlist resolution."""

    def test_workers_is_in_full_allowlist(self):
        """Sanity: 'workers' is one of the 12 allowed queries."""
        assert "workers" in FULL_ALLOWLIST

    def test_worker_prefix_checked_before_handler(self):
        """The allowlist check for worker/<name> fires before the handler runs."""
        with patch.object(
            CheckSystem, "_load_allowlist_from_vault",
            return_value=[q for q in FULL_ALLOWLIST if q != "workers"],
        ):
            tool = self._make_tool("worker/my_agent")
            result = _parse_result(tool.execute())
            assert result.get("status") == "denied"

    def _make_tool(self, query: str) -> CheckSystem:
        return CheckSystem(query=query, **BASE_KWARGS)
