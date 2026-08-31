"""PUT /api/workspace/{ws_id}/permissions persistence tests.

Covers the permission-map update path: validated writes merge into
``config.json`` (preserving purpose / capabilities / domain_allowlist) and
are reflected by ``GET /api/workspace/{ws_id}/summary``, while unknown
resources and invalid levels are rejected with HTTP 422 and leave the saved
config untouched.

All workspace filesystem access is redirected to the pytest tmp_path.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from web_ui.backend import workspace_routes


# ── helpers ───────────────────────────────────────────────────────────────────


def _use_tmp_workspace(monkeypatch, tmp_path):
    """Point workspace_routes at the pytest tmp_path."""
    monkeypatch.setattr(workspace_routes, "_workspace_dir", lambda ws_id: tmp_path)
    monkeypatch.setattr(workspace_routes, "ensure_workspace_dirs", lambda ws_id: None)


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _read_config(tmp_path):
    return json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))


def _put(ws_id, permissions, allow_host_resources=None):
    body = workspace_routes.WorkspacePermissionsBody(
        permissions=permissions,
        **({"allow_host_resources": allow_host_resources}
           if allow_host_resources is not None else {}),
    )
    return asyncio.run(workspace_routes.put_workspace_permissions(ws_id, body))


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
    fakes so the payload is fully deterministic.  The permission ceiling is
    left REAL (config_manager reads ``_workspace_dir`` from
    thoughtmachine.workspace_capabilities at call time, so redirecting that
    one attribute makes it read tmp_path/config.json)."""
    monkeypatch.setattr(workspace_routes, "_workspace_dir", lambda ws_id: tmp_path)
    monkeypatch.setattr(workspace_routes, "ensure_workspace_dirs", lambda ws_id: None)

    import thoughtmachine.workspace_capabilities as wcap

    monkeypatch.setattr(wcap, "_workspace_dir", lambda ws_id: tmp_path)

    # Capability loader returns None -> fully-permissive default.
    monkeypatch.setattr(workspace_routes, "load_workspace_capabilities",
                        lambda ws_id: None)

    import session.session_registry as sreg

    fake_sessions = SimpleNamespace(get_all=lambda: {})
    monkeypatch.setattr(
        sreg.SessionRegistry, "get_default", classmethod(lambda cls: fake_sessions)
    )

    monkeypatch.setattr(workspace_routes, "_collect_active_workers",
                        lambda ws_id: [])
    monkeypatch.setattr(workspace_routes, "_containers_for_workspace",
                        lambda entry: [])

    import session.tool_presets as tool_presets

    monkeypatch.setattr(tool_presets, "_ALL_TOOLS", [])

    catalog_path = tmp_path / "resource_catalog.json"
    monkeypatch.setattr(workspace_routes, "_RESOURCE_CATALOG_PATH", catalog_path)
    _write_json(catalog_path, [])


# ── PUT /api/workspace/{ws_id}/permissions ────────────────────────────────────


def test_update_workspace_permissions_persists_and_reflects_in_summary(
        tmp_path, monkeypatch):
    """A valid update merges into config.json (other keys preserved) and the
    saved map becomes the permission ceiling reported by the summary."""
    _use_tmp_workspace(monkeypatch, tmp_path)
    _write_json(tmp_path / "config.json", {
        "purpose": "general",
        "capabilities": {"fs": True, "net": False},
        "domain_allowlist": ["example.com"],
    })

    saved = {"git_read": "write", "filesystem": "ask"}
    result = _put("ws-1", saved)
    assert result["permissions"] == saved
    assert result["purpose"] == "general"
    assert result["allow_host_resources"] is False
    assert "risk" in result

    cfg = _read_config(tmp_path)
    assert cfg["permissions"] == saved
    assert cfg["purpose"] == "general"
    assert cfg["capabilities"] == {"fs": True, "net": False}
    assert cfg["domain_allowlist"] == ["example.com"]

    # The saved map is the ceiling for the summary endpoint.
    _patch_registry(monkeypatch)
    _patch_summary_deps(monkeypatch, tmp_path)
    summary = asyncio.run(workspace_routes.get_workspace_overview("ws-1"))
    assert summary["permissions"] == saved


def test_update_workspace_permissions_rejects_unknown_resource(
        tmp_path, monkeypatch):
    """An unknown resource name is a 422 and nothing is persisted."""
    _use_tmp_workspace(monkeypatch, tmp_path)
    _write_json(tmp_path / "config.json", {"purpose": "general"})

    with pytest.raises(HTTPException) as exc:
        _put("ws-1", {"nonexistent_resource": "read"})
    assert exc.value.status_code == 422
    assert isinstance(exc.value.detail, dict)
    assert "unknown resource 'nonexistent_resource'" in exc.value.detail["errors"]

    assert _read_config(tmp_path) == {"purpose": "general"}


def test_update_workspace_permissions_rejects_invalid_level(tmp_path, monkeypatch):
    """An invalid permission level for a known resource is a 422 and nothing
    is persisted."""
    _use_tmp_workspace(monkeypatch, tmp_path)
    _write_json(tmp_path / "config.json", {"purpose": "general"})

    with pytest.raises(HTTPException) as exc:
        _put("ws-1", {"git": "superuser"})
    assert exc.value.status_code == 422
    assert isinstance(exc.value.detail, dict)
    assert any(
        "invalid level 'superuser' for resource 'git'" in err
        for err in exc.value.detail["errors"]
    )

    assert _read_config(tmp_path) == {"purpose": "general"}
