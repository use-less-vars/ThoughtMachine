"""Tests for the resource-catalog ``execution_mode`` field.

Covers the git-execution-mode refactor: the per-resource catalog field is
renamed ``default_execution_context`` -> ``execution_mode`` with the vocabulary
``"container"`` / ``"host"``; the git execution mode is driven by the catalog
entry (not the legacy session/agent-config ``git_execution_mode`` key); a stale
config value is migrated honestly (a WARNING that names the value) and popped
on read; and the loader rejects any ``execution_mode`` outside the vocabulary.

Hermetic: no docker daemon, no real git binary, no vault writes.
"""

import json
import logging
import os
import sys

import pytest

from tools.git_info_tool import GitReadTool, resolve_git_execution_mode

_EXPECTED_NAMES = {"git", "filesystem", "container", "host_bash", "tty", "jtag"}
_EXECUTION_MODES = {"container", "host"}


# ---------------------------------------------------------------------------
# (1) GET /api/resource-catalog carries execution_mode, not the legacy key
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def server_module():
    """Fresh import of web_ui.backend.server (temp HOME + prefix purge)."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_home:
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = tmp_home
        try:
            for prefix in (
                "web_ui.backend",
                "agent.config.provider_profile",
                "thoughtmachine.bootstrap",
                "session",
            ):
                for mod in list(sys.modules):
                    if mod == prefix or mod.startswith(prefix + "."):
                        del sys.modules[mod]
            import web_ui.backend.server as server_mod

            yield server_mod
        finally:
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home


@pytest.fixture(scope="module")
def client(server_module):
    from starlette.testclient import TestClient

    with TestClient(server_module.app) as test_client:
        yield test_client


def test_endpoint_entries_carry_execution_mode_only(client):
    resp = client.get("/api/resource-catalog")
    assert resp.status_code == 200
    body = resp.json()
    assert {entry["name"] for entry in body} == _EXPECTED_NAMES
    for entry in body:
        assert "default_execution_context" not in entry, entry["name"]
        assert entry["execution_mode"] in _EXECUTION_MODES, entry["name"]


# ---------------------------------------------------------------------------
# (2) _use_container_mode() is driven by the git catalog execution_mode
# ---------------------------------------------------------------------------
def test_use_container_mode_true_when_catalog_is_container(tmp_path):
    tool = GitReadTool(operation="status")
    object.__setattr__(tool, "_resolved_workspace_path", str(tmp_path))
    object.__setattr__(tool, "_resolved_workspace_id", "test-ws")
    assert tool._use_container_mode() is True


# ---------------------------------------------------------------------------
# (3) A stale git_execution_mode config key has NO effect and is popped on read
# ---------------------------------------------------------------------------
def test_config_git_execution_mode_has_no_effect_and_is_popped():
    config = {"git_execution_mode": "host"}
    mode = resolve_git_execution_mode(config, {}, "/ws", "ws-1")
    assert mode == "containerized"                # catalog default wins
    assert "git_execution_mode" not in config     # popped on read


# ---------------------------------------------------------------------------
# (4) Honest migration: stale value warns and does not crash
# ---------------------------------------------------------------------------
def test_stale_config_value_warns_and_reader_proceeds(caplog):
    config = {"git_execution_mode": "host"}
    with caplog.at_level(logging.WARNING):
        mode = resolve_git_execution_mode(config, {}, "/ws", "ws-1")
    assert mode == "containerized"
    records = caplog.records
    assert any(record.levelno == logging.WARNING for record in records)
    messages = " ".join(record.getMessage() for record in records)
    assert "host" in messages


# ---------------------------------------------------------------------------
# (5) The loader rejects an out-of-vocabulary execution_mode
# ---------------------------------------------------------------------------
def test_loader_rejects_invalid_execution_mode(tmp_path, monkeypatch):
    import agent.config.resource_catalog as resource_catalog

    bad_catalog = [
        {
            "name": "git",
            "display_name": "Git",
            "description": "Git repository operations (read and write) via the git tools.",
            "permission_grain_set": ["banned", "read", "ask", "write"],
            "execution_mode": "containerized",
            "container_image": None,
            "dockerfile_reference": "docker/resource/git_overlay.Dockerfile",
            "tools": ["git_read", "git_write"],
        }
    ]
    catalog_path = tmp_path / "resource_catalog.json"
    catalog_path.write_text(json.dumps(bad_catalog), encoding="utf-8")
    monkeypatch.setattr(resource_catalog, "_CATALOG_PATH", catalog_path)

    with pytest.raises(Exception) as excinfo:
        resource_catalog.load_resource_catalog()
    assert "execution_mode" in str(excinfo.value) or "containerized" in str(excinfo.value)
