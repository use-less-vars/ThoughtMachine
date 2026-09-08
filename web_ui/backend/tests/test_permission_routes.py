"""Integration tests (TestClient) for the Step-5 permission REST endpoints.

Covers the session-scoped and workspace-scoped permission routes:

* ``GET/PUT /api/session/{session_id}/permissions``
* ``GET/PUT /api/workspace/{workspace_id}/permissions``

Session tests isolate the permission layer from the real session store by
monkeypatching ``session_routes._get_store`` with a stub store whose
``load_session()`` returns lightweight session objects with the attributes the
permission routes rely on (``session_id`` / ``workspace_id``) for registered
ids and ``None`` otherwise.

All vault-backed state (session permission sidecars, legacy session records,
workspace ``config.json`` ceilings, capabilities) is redirected to a per-test
temporary vault via ``THOUGHTMACHINE_VAULT_ROOT``; every function in the
permission chain reads that env var per call, so no module attribute patching
is needed.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import web_ui.backend.session_routes as session_routes
import web_ui.backend.workspace_routes as workspace_routes
from web_ui.backend.server import app
from thoughtmachine.permission_store import session_grants_path
from thoughtmachine.security import PERMISSION_SCHEMA, SAFE_DEFAULTS
from security.resource_catalog import coerce_resource_permissions

client = TestClient(app)

SESSION_SCHEMA_KEYS = sorted(PERMISSION_SCHEMA.keys())

# Canonical 10-grain workspace catalog defaults (agent/config/resource_catalog).
CATALOG_DEFAULTS = {
    "git_read": "read",
    "git_write": "ask",
    "host_bash": "banned",
    "container": "ask",
    "network": "ask",
    "filesystem": "read",
    "system": "read",
    "git": "read",
    "execution": "banned",
    "mcp": "banned",
}

CODING_PRESET = {
    "git_read": "read",
    "git_write": "ask",
    "host_bash": "banned",
    "container": "ask",
    "network": "ask",
    "filesystem": "write",
    "system": "read",
    "git": "read",
    "execution": "banned",
    "mcp": "banned",
}


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _assert_iso_utc(value: str) -> None:
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None


class _FakeSession:
    """Minimal session object: exposes the attrs the permission routes read."""

    def __init__(self, session_id: str, workspace_id: str):
        self.session_id = session_id
        self.workspace_id = workspace_id
        self.id = session_id


class _StubSessionStore:
    """Deterministic stand-in for the session store.

    ``load_session`` returns a registered fake session (or ``None``).  The
    real ``FileSystemSessionStore`` is a process-wide singleton rooted at the
    real home directory, so it cannot see per-test temporary vaults; the route
    under test only consumes ``session_id`` / ``workspace_id``, which the fake
    provides.
    """

    def __init__(self):
        self.sessions = {}

    def register(self, session_id: str, workspace_id: str) -> None:
        self.sessions[session_id] = _FakeSession(session_id, workspace_id)

    def load_session(self, session_id: str, workspace_id=None):
        return self.sessions.get(session_id)


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """Point the whole vault-backed chain at an empty temp vault."""
    root = tmp_path / "vault"
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(root))
    return root


@pytest.fixture
def stub_store(monkeypatch):
    """Route ``session_routes._get_store()`` to an isolated stub store."""
    store = _StubSessionStore()
    monkeypatch.setattr(session_routes, "_get_store", lambda: store)
    return store


class TestSessionPermissionsRoutes:
    SID = "sess-perm-001"
    WS = "ws-sess-a"

    def _register(self, stub_store, sid=SID, ws=WS):
        stub_store.register(sid, ws)

    # --- 404 handling ------------------------------------------------------

    def test_get_permissions_unknown_session_404(self, vault, stub_store):
        resp = client.get(f"/api/session/{self.SID}/permissions")
        assert resp.status_code == 404
        assert "Session not found" in str(resp.json().get("detail"))

    def test_put_permissions_unknown_session_404(self, vault, stub_store):
        resp = client.put(
            f"/api/session/{self.SID}/permissions",
            json={"filesystem": "write"},
        )
        assert resp.status_code == 404
        assert "Session not found" in str(resp.json().get("detail"))

    def test_get_permissions_existing_session_without_source_404(
        self, vault, stub_store
    ):
        self._register(stub_store)
        resp = client.get(f"/api/session/{self.SID}/permissions")
        assert resp.status_code == 404
        assert (
            f"no session permissions stored for session {self.SID}"
            in str(resp.json().get("detail"))
        )

    # --- validation (422) --------------------------------------------------

    def test_put_unknown_session_permission_key_422(self, vault, stub_store):
        self._register(stub_store)
        resp = client.put(
            f"/api/session/{self.SID}/permissions",
            json={"bogus": "read"},
        )
        assert resp.status_code == 422
        errors = resp.json().get("detail", {}).get("errors", [])
        assert any(
            "unknown session permission key: bogus" in err for err in errors
        )

    def test_put_invalid_session_permission_value_422(self, vault, stub_store):
        self._register(stub_store)
        resp = client.put(
            f"/api/session/{self.SID}/permissions",
            json={"network": "frobnicate"},
        )
        assert resp.status_code == 422
        errors = resp.json().get("detail", {}).get("errors", [])
        assert errors and any("network" in err for err in errors)

    # --- happy path roundtrip ---------------------------------------------

    def test_put_get_permissions_roundtrip(self, vault, stub_store):
        self._register(stub_store)
        body = {
            "filesystem": "write",
            "network": "write",
            "container": True,
            "git": "read",
            "mcp": "connect",
        }
        put = client.put(f"/api/session/{self.SID}/permissions", json=body)
        assert put.status_code == 200, put.text
        put_data = put.json()

        expected_raw = dict(SAFE_DEFAULTS)
        expected_raw.update(body)
        expected_canonical = coerce_resource_permissions(dict(expected_raw))
        assert sorted(put_data["raw"].keys()) == SESSION_SCHEMA_KEYS
        assert put_data["raw"] == expected_raw
        assert put_data["effective"] == expected_raw
        _assert_iso_utc(put_data["resolved_at"])
        # The session responses are raw/effective/resolved_at only.
        for forbidden in ("live_ok", "restart_required", "message"):
            assert forbidden not in put_data

        # Sidecar persisted atomically under the temp vault.
        sidecar = session_grants_path(vault, self.WS, self.SID)
        assert sidecar.exists()
        assert (
            json.loads(sidecar.read_text(encoding="utf-8")) == expected_canonical
        )

        get = client.get(f"/api/session/{self.SID}/permissions")
        assert get.status_code == 200, get.text
        get_data = get.json()
        assert get_data["raw"] == expected_canonical
        assert get_data["effective"] == expected_raw
        _assert_iso_utc(get_data["resolved_at"])

    def test_put_get_permissions_host_bash_ask_roundtrip(self, vault, stub_store):
        self._register(stub_store)
        body = {"host_bash": "ask"}
        put = client.put(f"/api/session/{self.SID}/permissions", json=body)
        assert put.status_code == 200, put.text
        put_data = put.json()

        expected_raw = dict(SAFE_DEFAULTS)
        expected_raw.update(body)
        expected_canonical = coerce_resource_permissions(dict(expected_raw))
        assert sorted(put_data["raw"].keys()) == SESSION_SCHEMA_KEYS
        assert put_data["raw"] == expected_raw
        assert put_data["effective"] == expected_raw
        assert put_data["effective"]["host_bash"] == "ask"

        # Sidecar holds the canonical 6-grain map with the new host_bash level.
        sidecar = session_grants_path(vault, self.WS, self.SID)
        assert sidecar.exists()
        assert (
            json.loads(sidecar.read_text(encoding="utf-8")) == expected_canonical
        )
        assert json.loads(sidecar.read_text(encoding="utf-8"))["host_bash"] == "ask"

        get = client.get(f"/api/session/{self.SID}/permissions")
        assert get.status_code == 200, get.text
        get_data = get.json()
        assert get_data["raw"] == expected_canonical
        assert get_data["effective"] == expected_raw

    def test_put_get_permissions_host_bash_allow_roundtrip(self, vault, stub_store):
        self._register(stub_store)
        body = {"host_bash": "allow"}
        put = client.put(f"/api/session/{self.SID}/permissions", json=body)
        assert put.status_code == 200, put.text
        put_data = put.json()

        expected_raw = dict(SAFE_DEFAULTS)
        expected_raw.update(body)
        expected_canonical = coerce_resource_permissions(dict(expected_raw))
        assert sorted(put_data["raw"].keys()) == SESSION_SCHEMA_KEYS
        assert put_data["raw"] == expected_raw
        assert put_data["effective"] == expected_raw
        assert put_data["effective"]["host_bash"] == "allow"

        # Sidecar holds the canonical 6-grain map with the new host_bash level.
        sidecar = session_grants_path(vault, self.WS, self.SID)
        assert sidecar.exists()
        assert (
            json.loads(sidecar.read_text(encoding="utf-8")) == expected_canonical
        )
        assert (
            json.loads(sidecar.read_text(encoding="utf-8"))["host_bash"]
            == "allow"
        )

        get = client.get(f"/api/session/{self.SID}/permissions")
        assert get.status_code == 200, get.text
        get_data = get.json()
        assert get_data["raw"] == expected_canonical
        assert get_data["effective"] == expected_raw

    def test_put_host_bash_write_422(self, vault, stub_store):
        # host_bash is a session-storable grain with its own banned/ask/allow
        # vocabulary; the legacy 'write' level must be rejected.
        self._register(stub_store)
        resp = client.put(
            f"/api/session/{self.SID}/permissions",
            json={"host_bash": "write"},
        )
        assert resp.status_code == 422
        errors = resp.json().get("detail", {}).get("errors", [])
        assert errors and any("host_bash" in err for err in errors)

    def test_put_non_object_body_422(self, vault, stub_store):
        self._register(stub_store)
        resp = client.put(
            f"/api/session/{self.SID}/permissions",
            json=["host_bash", "ask"],
        )
        assert resp.status_code == 422

    def test_put_empty_body_resets_to_safe_defaults(self, vault, stub_store):
        self._register(stub_store)
        resp = client.put(f"/api/session/{self.SID}/permissions", json={})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["raw"] == SAFE_DEFAULTS
        assert sorted(data["raw"].keys()) == SESSION_SCHEMA_KEYS
        assert data["effective"] == SAFE_DEFAULTS

    # --- workspace ceiling capping ----------------------------------------

    def test_ceiling_caps_session_grants(self, vault, stub_store):
        self._register(stub_store)
        _write_json(
            vault / "workspaces" / self.WS / "config.json",
            {"permissions": {"filesystem": "read", "network": "banned",
                             "docker": "banned"}},
        )
        body = {
            "filesystem": "write",
            "network": "write",
            "container": True,
        }
        put = client.put(f"/api/session/{self.SID}/permissions", json=body)
        assert put.status_code == 200, put.text
        data = put.json()
        # Raw grants are stored uncapped ...
        assert data["raw"]["filesystem"] == "write"
        assert data["raw"]["network"] == "write"
        assert data["raw"]["container"] is True
        # ... while effective permissions are capped by the workspace ceiling.
        assert data["effective"]["filesystem"] == "read"
        assert data["effective"]["network"] == "banned"
        assert data["effective"]["container"] is False
        # GET reflects the same capped effective profile.
        get = client.get(f"/api/session/{self.SID}/permissions")
        assert get.status_code == 200, get.text
        assert get.json()["effective"]["filesystem"] == "read"
        # The ceiling config was written (atomic workspace config write).
        config_path = vault / "workspaces" / self.WS / "config.json"
        assert config_path.exists()
        assert json.loads(config_path.read_text(encoding="utf-8")) == {
            "permissions": {
                "filesystem": "read",
                "network": "banned",
                "docker": "banned",
            }
        }

    def test_ceiling_caps_git_raw_kept(self, vault, stub_store):
        self._register(stub_store)
        _write_json(
            vault / "workspaces" / self.WS / "config.json",
            {"permissions": {"git": "read"}},
        )
        put = client.put(
            f"/api/session/{self.SID}/permissions", json={"git": "write"}
        )
        assert put.status_code == 200, put.text
        data = put.json()
        assert data["raw"]["git"] == "write"
        assert data["effective"]["git"] == "read"

    def test_no_ceiling_lets_raw_stand(self, vault, stub_store):
        self._register(stub_store)
        put = client.put(
            f"/api/session/{self.SID}/permissions", json={"git": "write"}
        )
        assert put.status_code == 200, put.text
        data = put.json()
        assert data["raw"]["git"] == "write"
        assert data["effective"]["git"] == "write"

    def test_put_git_write_on_feature_branch_persists(self, vault, stub_store):
        # Regression: git="write_on_feature_branch" is a git-only level that the
        # PUT validation must persist verbatim instead of clamping to "read".
        self._register(stub_store)
        put = client.put(
            f"/api/session/{self.SID}/permissions",
            json={"git": "write_on_feature_branch"},
        )
        assert put.status_code == 200, put.text
        data = put.json()
        assert data["raw"]["git"] == "write_on_feature_branch"
        assert data["effective"]["git"] == "write_on_feature_branch"

        get = client.get(f"/api/session/{self.SID}/permissions")
        assert get.status_code == 200, get.text
        assert get.json()["raw"]["git"] == "write_on_feature_branch"
        assert get.json()["effective"]["git"] == "write_on_feature_branch"

    # --- legacy record fallback / corruption ------------------------------

    def test_get_falls_back_to_legacy_record_permissions(self, vault, stub_store):
        self._register(stub_store)
        _write_json(
            vault / "workspaces" / self.WS / "sessions" / f"{self.SID}.json",
            {
                "session_id": self.SID,
                "metadata": {
                    "session_config": {
                        "session_permissions": {
                            "filesystem": "write",
                            "network": "write",
                        }
                    }
                },
            },
        )
        resp = client.get(f"/api/session/{self.SID}/permissions")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["raw"] == {"filesystem": "write", "network": "write"}
        assert data["effective"]["filesystem"] == "write"
        assert data["effective"]["network"] == "write"

    def test_get_corrupt_sidecar_500_fail_closed(self, vault, stub_store):
        self._register(stub_store)
        sidecar = session_grants_path(vault, self.WS, self.SID)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text("{ not valid json !!!", encoding="utf-8")
        resp = client.get(f"/api/session/{self.SID}/permissions")
        assert resp.status_code == 500
        assert "permission store read failed" in str(resp.json().get("detail"))


class TestWorkspacePermissionsRoutes:
    WS = "ws-perm-b"

    def test_get_fresh_workspace_returns_catalog_defaults(self, vault):
        resp = client.get(f"/api/workspace/{self.WS}/permissions")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["workspace_id"] == self.WS
        assert data["purpose"] == "general"
        assert data["allow_host_resources"] is False
        assert data["raw"] == {}
        # With no saved permissions the GET falls back to the purpose preset,
        # i.e. the catalog defaults for "general".
        assert data["permissions"] == CATALOG_DEFAULTS
        assert data["effective"] == CATALOG_DEFAULTS
        _assert_iso_utc(data["resolved_at"])

    def test_put_then_get_roundtrip_persists_config(self, vault):
        perms = {
            "filesystem": "write",
            "network": "read",
            "git_write": "read",
        }
        put = client.put(
            f"/api/workspace/{self.WS}/permissions",
            json={"permissions": perms, "allow_host_resources": True},
        )
        assert put.status_code == 200, put.text
        data = put.json()
        assert data["workspace_id"] == self.WS
        assert data["purpose"] == "general"
        assert data["permissions"] == perms
        assert data["raw"] == perms
        assert data["effective"] == perms
        assert data["allow_host_resources"] is True
        risk = data["risk"]
        assert {"level", "score", "factors", "granted_count"} <= set(risk)
        _assert_iso_utc(data["resolved_at"])

        # config.json written atomically under the temp vault.
        config_path = vault / "workspaces" / self.WS / "config.json"
        assert config_path.exists()
        assert json.loads(config_path.read_text(encoding="utf-8")) == {
            "permissions": perms,
            "allow_host_resources": True,
        }

        # GET returns exactly the saved map (no default merging).
        get = client.get(f"/api/workspace/{self.WS}/permissions")
        assert get.status_code == 200, get.text
        get_data = get.json()
        assert get_data["permissions"] == perms
        assert get_data["raw"] == perms
        assert get_data["allow_host_resources"] is True

    def test_put_unknown_resource_422(self, vault):
        resp = client.put(
            f"/api/workspace/{self.WS}/permissions",
            json={"permissions": {"bogus_resource": "read"}},
        )
        assert resp.status_code == 422
        errors = resp.json().get("detail", {}).get("errors", [])
        assert errors and "unknown resource 'bogus_resource'" in errors[0]

    def test_put_invalid_level_422(self, vault):
        resp = client.put(
            f"/api/workspace/{self.WS}/permissions",
            json={"permissions": {"network": "superuser"}},
        )
        assert resp.status_code == 422
        errors = resp.json().get("detail", {}).get("errors", [])
        assert errors and "invalid level 'superuser' for resource 'network'" in errors[0]

    def test_put_ceiling_host_bash_levels_accepted(self, vault):
        # The workspace ceiling stores host_bash as a session-storable grain
        # with its own banned/ask/allow vocabulary.
        for level in ("banned", "ask", "allow"):
            put = client.put(
                f"/api/workspace/{self.WS}/permissions",
                json={"permissions": {"host_bash": level}},
            )
            assert put.status_code == 200, put.text
            data = put.json()
            assert data["permissions"] == {"host_bash": level}
            assert data["raw"] == {"host_bash": level}
            get = client.get(f"/api/workspace/{self.WS}/permissions")
            assert get.status_code == 200, get.text
            assert get.json()["permissions"] == {"host_bash": level}

    def test_put_ceiling_host_bash_write_422(self, vault):
        resp = client.put(
            f"/api/workspace/{self.WS}/permissions",
            json={"permissions": {"host_bash": "write"}},
        )
        assert resp.status_code == 422
        errors = resp.json().get("detail", {}).get("errors", [])
        assert errors and "invalid level 'write' for resource 'host_bash'" in errors[0]

    def test_put_empty_permissions_then_get_falls_back_to_preset(self, vault):
        put = client.put(
            f"/api/workspace/{self.WS}/permissions", json={"permissions": {}}
        )
        assert put.status_code == 200, put.text
        assert put.json()["raw"] == {}
        assert put.json()["permissions"] == {}
        assert put.json()["allow_host_resources"] is False

        get = client.get(f"/api/workspace/{self.WS}/permissions")
        assert get.status_code == 200, get.text
        get_data = get.json()
        assert get_data["raw"] == {}
        assert get_data["permissions"] == CATALOG_DEFAULTS
        assert get_data["effective"] == CATALOG_DEFAULTS
        assert get_data["allow_host_resources"] is False

    def test_put_empty_permissions_keeps_allow_host_resources(self, vault):
        first = client.put(
            f"/api/workspace/{self.WS}/permissions",
            json={"permissions": {"filesystem": "write"},
                  "allow_host_resources": True},
        )
        assert first.status_code == 200, first.text
        second = client.put(
            f"/api/workspace/{self.WS}/permissions", json={"permissions": {}}
        )
        assert second.status_code == 200, second.text

        get = client.get(f"/api/workspace/{self.WS}/permissions")
        assert get.status_code == 200, get.text
        get_data = get.json()
        assert get_data["allow_host_resources"] is True
        assert get_data["raw"] == {}
        assert get_data["permissions"] == CATALOG_DEFAULTS

    def test_purpose_preset_resolves_when_no_permissions_saved(self, vault):
        _write_json(
            vault / "workspaces" / self.WS / "config.json",
            {"purpose": "coding"},
        )
        resp = client.get(f"/api/workspace/{self.WS}/permissions")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["purpose"] == "coding"
        assert data["raw"] == {}
        assert data["permissions"] == CODING_PRESET
        assert data["effective"] == CODING_PRESET


class TestWorkspaceEffectivePermissionsRoutes:
    """GET /api/workspace/{ws_id}/effective_permissions session resolution.

    The effective-permissions endpoint resolves a session's grants sidecar
    first (``permission_store.read_session_permissions``), then falls back to
    the saved session's embedded metadata; an unknown session (or one with no
    stored source) yields the read-only default rather than a 404.
    """

    SID = "sess-eff-001"
    WS = "ws-eff-a"

    def test_effective_reads_sidecar_after_session_put(self, vault, stub_store):
        stub_store.register(self.SID, self.WS)
        put = client.put(
            f"/api/session/{self.SID}/permissions", json={"filesystem": "write"}
        )
        assert put.status_code == 200, put.text
        sidecar = session_grants_path(vault, self.WS, self.SID)
        assert sidecar.exists()

        resp = client.get(
            f"/api/workspace/{self.WS}/effective_permissions",
            params={"session_id": self.SID},
        )
        assert resp.status_code == 200, resp.text
        effective = resp.json()["effective_permissions"]
        assert effective["filesystem"] == "write"

    def test_effective_sidecar_supersedes_stale_metadata(self, vault):
        # Legacy record on disk says "read"; the sidecar (written later) says
        # "write".  The sidecar must win while it exists, and the legacy
        # metadata must take over once the sidecar is gone.
        _write_json(
            vault / "workspaces" / self.WS / "sessions" / f"{self.SID}.json",
            {
                "session_id": self.SID,
                "metadata": {
                    "session_config": {
                        "session_permissions": {"filesystem": "read"}
                    }
                },
            },
        )
        sidecar = session_grants_path(vault, self.WS, self.SID)
        _write_json(sidecar, {"filesystem": "write"})

        resp = client.get(
            f"/api/workspace/{self.WS}/effective_permissions",
            params={"session_id": self.SID},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["effective_permissions"]["filesystem"] == "write"

        sidecar.unlink()
        resp = client.get(
            f"/api/workspace/{self.WS}/effective_permissions",
            params={"session_id": self.SID},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["effective_permissions"]["filesystem"] == "read"

    def test_effective_unknown_session_returns_read_only_default(self, vault):
        resp = client.get(
            f"/api/workspace/{self.WS}/effective_permissions",
            params={"session_id": "no-such-session"},
        )
        assert resp.status_code == 200, resp.text
        effective = resp.json()["effective_permissions"]
        assert effective["filesystem"] == "read"
        assert effective["network"] == "banned"
        assert effective["container"] is False

