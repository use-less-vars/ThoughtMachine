"""
Workspace-scope tests for ``POST /api/config/reset``.

The endpoint's contract is "always reset the config file"; re-configuring the
in-memory config of a *cached* session bridge is an optional extra.  That extra
must respect the PR #157 boundary: a bridge cached for ANOTHER workspace — or
one that holds no session at all — is not this caller's to reconfigure, because
doing so would apply a config change to a live session of a different
workspace.

Same style as ``test_config_mode.py`` (module-level ``TestClient`` against the
real app), with the reset target file redirected into ``tmp_path``.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import web_ui.backend.config_routes as config_routes
import web_ui.backend.server as server
from session.models import Session

# Shared client for all tests in this module
client = TestClient(server.app)


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures / helpers
# ══════════════════════════════════════════════════════════════════════════════


def _live_server():
    """Resolve ``web_ui.backend.server`` from ``sys.modules`` at CALL time.

    ``reset_config`` imports the server module lazily inside the handler, so
    the route always reads the *live* module object.  A peer test in the
    top-level ``tests/`` tree purges and re-imports
    ``web_ui.backend.server``, which replaces the ``sys.modules`` entry with a
    new module object; the module-level ``import ... as server`` above keeps
    pointing at the pre-purge object.  Patching the registry through this
    resolver keeps our fixture and the route on the same object regardless of
    test order.  (Observed, not inferred: without it the full-suite run fails
    with "a bridge cached for the requested workspace was not updated".)
    """
    return importlib.import_module("web_ui.backend.server")


@pytest.fixture(autouse=True)
def isolated_config_file(monkeypatch, tmp_path):
    """Point the reset target files at ``tmp_path``.

    ``reset_config`` expands and rewrites ``_CONFIG_PATH`` and also unlinks
    ``custom_system_prompt.txt`` from ``_vault.vault_root()``, so BOTH are
    redirected into the pytest temp dir: the test never touches a real vault.

    The vault-root redirect is not optional.  ``_vault.vault_root()`` is a
    process-global resolver, and whether the resulting unlink is allowed
    depends on whatever vault root is in force at call time -- in the
    full-suite run the top-level ``tests/conftest.py`` hermetic setup makes
    ``vault_root()`` point at a real temp vault, the unlink is blocked by the
    hermetic vault guard, and the endpoint turns that into a 500.  This module
    therefore pins the root itself instead of inheriting it.  (The hermetic
    fixture in ``tests/conftest.py`` is not visible from
    ``web_ui/backend/tests``, so the resolver is patched here.)
    """
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    # Patch the shared resolver the handler itself calls
    # (``_vault.vault_root()``), which also covers ``_get_custom_prompt_path``.
    monkeypatch.setattr(config_routes._vault, "vault_root", lambda: vault)
    # The prompt file lives in the real vault, which the hermetic guard forbids
    # test code to unlink: stub the state bridge so the reset stays local.
    target = tmp_path / "agent_config.json"

    class _FakeStateBridge:
        def __init__(self, config_path=None):
            self.config_path = config_path

        def reset_config_to_factory(self):
            Path(self.config_path).write_text("{}", encoding="utf-8")
            return {}

    monkeypatch.setattr(config_routes, "_CONFIG_PATH", str(target))
    monkeypatch.setattr(config_routes, "StateBridge", _FakeStateBridge, raising=False)
    return target


class _FakeBridge:
    """Stand-in for ``WebAgentBridge``.

    Only the attributes the reset endpoint touches: ``session`` /
    ``workspace_id`` (the workspace-scoped cached-bridge lookup) and
    ``_config`` (the best-effort in-memory update).
    """

    def __init__(self, session=None, workspace_id=None):
        self.session = session
        self.workspace_id = workspace_id
        self._config = "untouched"


def _cache_bridge(monkeypatch, session_id, workspace_id, has_session=True):
    """Cache a fake bridge for *session_id* and return it."""
    bridge = _FakeBridge(
        session=Session(workspace_id=workspace_id) if has_session else None,
        workspace_id=workspace_id,
    )
    # Patch the registry on the LIVE server module (see ``_live_server``), not
    # on the collection-time object, which a peer test's purge can orphan.
    monkeypatch.setitem(_live_server()._session_bridges, session_id, bridge)
    return bridge


def _assert_config_file_reset(target: Path):
    """The endpoint's contract: the config file was reset regardless."""
    assert target.exists(), "the endpoint must always reset the config file"
    assert json.loads(target.read_text()) == {}, (
        "the reset should leave an empty overlay behind"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Tests: POST /api/config/reset
# ══════════════════════════════════════════════════════════════════════════════


class TestResetConfigCachedBridgeScope:
    """The cached-bridge update is workspace-scoped (PR #157 discipline)."""

    def test_same_workspace_bridge_is_still_updated(self, monkeypatch, isolated_config_file):
        """Control: a bridge cached for the requested workspace is reconfigured."""
        bridge = _cache_bridge(monkeypatch, "sess-same", "ws-a")

        response = client.post(
            "/api/config/reset",
            json={"session_id": "sess-same", "workspace_id": "ws-a"},
        )

        assert response.status_code == 200, response.text
        assert bridge._config != "untouched", (
            "a bridge cached for the requested workspace was not updated"
        )
        _assert_config_file_reset(isolated_config_file)

    def test_bridge_of_another_workspace_is_not_updated(self, monkeypatch, isolated_config_file):
        """A ws-a bridge must not be reconfigured by a ws-b reset request."""
        bridge = _cache_bridge(monkeypatch, "sess-other", "ws-a")

        response = client.post(
            "/api/config/reset",
            json={"session_id": "sess-other", "workspace_id": "ws-b"},
        )

        assert response.status_code == 200, response.text
        assert bridge._config == "untouched", (
            "reset reconfigured a bridge belonging to another workspace"
        )
        assert isinstance(response.json(), dict), "the factory config must still be returned"
        _assert_config_file_reset(isolated_config_file)

    def test_sessionaless_cached_bridge_is_not_updated(self, monkeypatch, isolated_config_file):
        """A cached entry holding no session is a miss, not a target."""
        bridge = _cache_bridge(monkeypatch, "sess-empty", "ws-a", has_session=False)
        assert bridge.session is None, "precondition: sessionaless cached bridge"

        response = client.post(
            "/api/config/reset",
            json={"session_id": "sess-empty", "workspace_id": "ws-a"},
        )

        assert response.status_code == 200, response.text
        assert bridge._config == "untouched", (
            "reset reconfigured a cached bridge that holds no session"
        )
        _assert_config_file_reset(isolated_config_file)
