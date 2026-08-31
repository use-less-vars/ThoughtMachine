"""Workspace summary endpoint tests (backend layer).

Covers ``GET /api/workspace/{ws_id}/summary``: a read-only dashboard payload
combining configuration (permissions ceiling, capabilities, dockerfile,
worker templates) with live state (active workers, open sessions,
containers) and global registries (tool list, resource catalog).

The payload key set is exact - no extra keys, no secrets - and an unknown
workspace id yields HTTP 404.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from thoughtmachine.workspace_capabilities import WorkspaceCapabilities

from web_ui.backend import workspace_routes


# ── helpers ───────────────────────────────────────────────────────────────────


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _patch_registry(monkeypatch, ws_id="ws-1"):
    """Patch WorkspaceRegistry.get_default with an entry for *ws_id* (and
    None for anything else, exercising the 404 branch)."""
    entry = SimpleNamespace(id=ws_id, label="L", root_path="/tmp/ws-root")
    fake = SimpleNamespace(get_workspace=lambda wid: entry if wid == ws_id else None)
    monkeypatch.setattr(
        workspace_routes.WorkspaceRegistry,
        "get_default",
        classmethod(lambda cls: fake),
    )
    return entry


def _patch_summary_deps(monkeypatch, tmp_path):
    """Patch every external dependency the summary touches onto tmp_path /
    fakes so the payload is fully deterministic."""
    monkeypatch.setattr(workspace_routes, "_workspace_dir", lambda ws_id: tmp_path)
    monkeypatch.setattr(workspace_routes, "ensure_workspace_dirs", lambda ws_id: None)

    import web_ui.backend.config_manager as config_manager

    monkeypatch.setattr(
        config_manager,
        "_load_workspace_permission_ceiling",
        lambda ws: {"fs_read": "read", "fs_write": "write"},
    )

    # Capability loader returns None -> fully-permissive default.
    monkeypatch.setattr(workspace_routes, "load_workspace_capabilities",
                        lambda ws_id: None)

    import session.session_registry as sreg

    fake_sessions = SimpleNamespace(get_all=lambda: {
        "s1": {"session_id": "s1", "workspace_id": "ws-1", "is_open": True,
               "name": "n1", "mode": "m1", "created_at": "2024-01-01T00:00:00Z"},
        "s2": {"session_id": "s2", "workspace_id": "ws-1", "is_open": False,
               "name": "n2", "mode": "m2", "created_at": "2024-01-02T00:00:00Z"},
        "s3": {"session_id": "s3", "workspace_id": "ws-other", "is_open": True,
               "name": "n3", "mode": "m3", "created_at": "2024-01-03T00:00:00Z"},
    })
    monkeypatch.setattr(
        sreg.SessionRegistry, "get_default", classmethod(lambda cls: fake_sessions)
    )

    monkeypatch.setattr(
        workspace_routes,
        "_collect_active_workers",
        lambda ws_id: [{"worker_name": "w1", "instance_id": 1,
                        "status": "ready", "elapsed": 1.5}],
    )
    monkeypatch.setattr(
        workspace_routes,
        "_containers_for_workspace",
        lambda entry: [{"id": "c1", "name": "tm-res-x", "type": "resource",
                        "workspace_id": "ws-1", "status": "running"}],
    )

    import session.tool_presets as tool_presets

    monkeypatch.setattr(tool_presets, "_ALL_TOOLS",
                        ["file_read", "shell_exec", "alpha"])

    catalog_path = tmp_path / "resource_catalog.json"
    monkeypatch.setattr(workspace_routes, "_RESOURCE_CATALOG_PATH", catalog_path)
    _write_json(catalog_path, [{"name": "fs", "kind": "mount"}])


def _all_keys(obj):
    """Recursively collect every dict key in *obj*."""
    keys = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.append(k)
            keys.extend(_all_keys(v))
    elif isinstance(obj, list):
        for item in obj:
            keys.extend(_all_keys(item))
    return keys


# ── GET /api/workspace/{ws_id}/summary ───────────────────────────────────────


def test_summary_full_payload(tmp_path, monkeypatch):
    """A registered workspace yields the exact key set with all sections
    populated and no secret-looking keys anywhere in the payload."""
    _patch_registry(monkeypatch)
    _patch_summary_deps(monkeypatch, tmp_path)

    _write_json(tmp_path / "config.json",
                {"purpose": "general", "allow_host_resources": True})
    (tmp_path / "Dockerfile").write_text("FROM x\n", encoding="utf-8")
    _write_json(tmp_path / "workers.json",
                [{"name": "w1", "system_prompt": "p1"}])

    result = asyncio.run(workspace_routes.get_workspace_overview("ws-1"))

    assert set(result.keys()) == {
        "workspace_id", "label", "root_path", "allow_host_resources",
        "permissions", "capabilities", "dockerfile", "worker_templates",
        "active_workers", "active_sessions", "active_containers",
        "tools", "resource_catalog",
    }, result.keys()

    assert result["workspace_id"] == "ws-1"
    assert result["label"] == "L"
    assert result["root_path"] == "/tmp/ws-root"
    assert result["allow_host_resources"] is True
    assert result["permissions"] == {"fs_read": "read", "fs_write": "write"}
    assert result["capabilities"] == WorkspaceCapabilities.default().to_dict()
    assert result["dockerfile"] == {
        "path": str(tmp_path / "Dockerfile"),
        "content": "FROM x\n",
    }
    assert result["worker_templates"] == [{"name": "w1", "system_prompt": "p1"}]
    assert result["active_workers"] == [
        {"worker_name": "w1", "instance_id": 1, "status": "ready", "elapsed": 1.5}
    ]
    assert result["active_sessions"] == [{
        "session_id": "s1", "workspace_id": "ws-1", "name": "n1", "mode": "m1",
        "started_at": "2024-01-01T00:00:00Z",
    }]
    assert result["active_containers"] == [
        {"id": "c1", "name": "tm-res-x", "type": "resource",
         "workspace_id": "ws-1", "status": "running"}
    ]
    assert result["tools"] == ["alpha", "file_read", "shell_exec"]
    assert result["resource_catalog"] == [{"name": "fs", "kind": "mount"}]

    # No secret-looking keys anywhere in the payload.
    keys = set(_all_keys(result))
    for banned in ("api_key", "apiKey", "password", "secret", "token",
                   "credential", "private_key"):
        assert banned not in keys, f"secret-looking key leaked: {banned}"


def test_summary_missing_assets_and_empty_sections(tmp_path, monkeypatch):
    """Missing dockerfile/resource catalog/workers config and no live state
    degrade gracefully: content None / empty lists, no exceptions."""
    _patch_registry(monkeypatch)
    _patch_summary_deps(monkeypatch, tmp_path)

    # Override the full-payload fixtures with empty ones.
    import session.session_registry as sreg

    monkeypatch.setattr(
        sreg.SessionRegistry, "get_default",
        classmethod(lambda cls: SimpleNamespace(get_all=lambda: {})),
    )
    monkeypatch.setattr(workspace_routes, "_collect_active_workers",
                        lambda ws_id: [])
    monkeypatch.setattr(workspace_routes, "_containers_for_workspace",
                        lambda entry: None)
    import session.tool_presets as tool_presets

    monkeypatch.setattr(tool_presets, "_ALL_TOOLS", ["shell_exec"])

    # _patch_summary_deps seeded a resource catalog file for the full-payload
    # test; remove it here so the "missing asset" branch is exercised.
    (tmp_path / "resource_catalog.json").unlink()

    # No config.json -> allow_host_resources False; no Dockerfile; no
    # resource catalog file; no workers.json.
    result = asyncio.run(workspace_routes.get_workspace_overview("ws-1"))

    assert result["allow_host_resources"] is False
    assert result["dockerfile"] == {
        "path": str(tmp_path / "Dockerfile"), "content": None,
    }
    assert result["worker_templates"] == []
    assert result["active_workers"] == []
    assert result["active_sessions"] == []
    assert result["active_containers"] == []
    assert result["tools"] == ["shell_exec"]
    assert result["resource_catalog"] == []


def test_summary_unknown_workspace_404(monkeypatch):
    """Unregistered workspace id -> HTTP 404 before any other work."""
    _patch_registry(monkeypatch)  # get_workspace returns None for "nope"

    with pytest.raises(HTTPException) as exc:
        asyncio.run(workspace_routes.get_workspace_overview("nope"))
    assert exc.value.status_code == 404
    assert "nope" in str(exc.value.detail)
