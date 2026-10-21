"""CheckSystem: ``version`` and ``containers`` are always allowed.

The tool schema advertises ``version`` and ``containers`` unconditionally, so a
vault allowlist that omits them makes the tool's displayed contract diverge from
its enforcement (displayed != enforced).  ``SAFE_QUERIES`` is a module-level
short-circuit inside the single gate point (``_check_path_allowed``), consulted
before the vault allowlist is applied, so neither an empty-but-present allowlist
nor a vault read failure can deny them.

Every other query must still go through the allowlist decision.
"""

import json
from unittest.mock import patch

import pytest

from tools.workspace.check_system import SAFE_QUERIES, CheckSystem


def _tool(**kwargs):
    return CheckSystem(query="version", **kwargs)


def test_safe_queries_constant_holds_both_names():
    assert SAFE_QUERIES == frozenset({"version", "containers"})


@pytest.mark.parametrize("query", ["version", "containers"])
def test_empty_allowlist_allows_safe_queries(query):
    """An empty (but present) allowlist must not deny the safe queries."""
    assert _tool(allowlist=[])._check_path_allowed(query) is True


@pytest.mark.parametrize("query", ["version", "containers"])
def test_populated_allowlist_without_safe_queries_allows_them(query):
    """A real allowlist that omits the safe queries must not deny them either."""
    tool = _tool(allowlist=["runtime_state", "vault_status", "workers"])
    assert tool._check_path_allowed(query) is True


@pytest.mark.parametrize("query", ["version", "containers"])
def test_no_allowlist_still_allows_safe_queries(query):
    """No allowlist (vault unavailable) keeps working — the degraded path is not
    replaced, only bypassed for the two safe names."""
    assert _tool(allowlist=None)._check_path_allowed(query) is True


def test_non_safe_query_still_goes_through_the_allowlist():
    """The short-circuit must not disable the gate for everything else."""
    tool = _tool(allowlist=["runtime_state"])
    assert tool._check_path_allowed("runtime_state") is True
    assert tool._check_path_allowed("vault_status") is False
    assert tool._check_path_allowed("containers_x") is False


def test_execute_denies_non_safe_query_not_on_allowlist(monkeypatch):
    """End-to-end: a junk query against an empty allowlist is denied."""
    monkeypatch.setattr(
        CheckSystem, "_load_allowlist_from_vault", lambda self: []
    )
    tool = CheckSystem(query="definitely_not_a_query")
    payload = json.loads(tool.execute())

    assert payload["status"] == "denied"
    assert "not allowed by system configuration" in payload["error"]


def test_execute_allows_version_despite_empty_allowlist(monkeypatch):
    """End-to-end: ``version`` reaches its handler with an empty allowlist."""
    monkeypatch.setattr(
        CheckSystem, "_load_allowlist_from_vault", lambda self: []
    )
    monkeypatch.setattr(
        CheckSystem,
        "_query_version",
        lambda self: {"revision": "deadbeef", "branch": "fix/branch", "dirty": False},
    )
    payload = json.loads(CheckSystem(query="version").execute())

    assert payload == {"revision": "deadbeef", "branch": "fix/branch", "dirty": False}


def test_execute_allows_containers_despite_empty_allowlist(monkeypatch):
    """End-to-end: ``containers`` reaches its handler with an empty allowlist."""
    monkeypatch.setattr(
        CheckSystem, "_load_allowlist_from_vault", lambda self: []
    )
    monkeypatch.setattr(
        CheckSystem,
        "_query_containers",
        lambda self, workspace_path: {"containers": [], "count": 0, "status": "ok"},
    )
    payload = json.loads(CheckSystem(query="containers").execute())

    assert payload["status"] == "ok"
    assert payload["count"] == 0


def test_execute_version_reports_real_shape_without_allowlist(monkeypatch):
    """With no vault allowlist at all, ``version`` still returns its own shape."""
    monkeypatch.setattr(
        CheckSystem, "_load_allowlist_from_vault", lambda self: None
    )
    with patch.dict(
        "sys.modules",
        {},
    ):
        payload = json.loads(CheckSystem(query="version").execute())

    assert set(payload) == {"revision", "branch", "dirty"}
