"""Session container-grant gate for ``ContainerManager.build_image``.

``ContainerManager.build_image`` (infra/container_manager.py) is agent-reachable,
so it refuses a build FAIL-CLOSED when the session has no container access. The
gate sits after the vault-requirements guard and before the build context is
staged; it raises ``RuntimeError`` with one of two messages:

* policy failure (the session policy could not be resolved at all) --
  ``refusing to build: session policy could not be resolved (...)``; and
* grant denied (the policy resolved but the effective ``container`` grant is
  false) --
  ``refusing to build: session container grant denied (read it in the session
  permission config: session_permissions['container'])``.

These tests pin the gate's wiring and its two distinct refusal messages WITHOUT
exercising Docker: the ``docker_executor`` module is stubbed, so only OUR wiring
is under test.

Five tests:

1. ``test_without_session_container_grant_refuses`` -- permissive workspace
   capabilities, but the SESSION carries no container grant -> the grant-denied
   refusal (never the policy-unresolved one) and no build.
2. ``test_workspace_docker_not_allowed_refuses`` -- the session GRANTS container
   but the WORKSPACE capabilities do not (``allow_docker`` False) -> the same
   grant-denied refusal and no build (locks the workspace axis too).
3. ``test_granted_build_proceeds`` -- session grant + permissive capabilities ->
   the build runs exactly once and ``build_image`` returns its dict.
4. ``test_unresolvable_policy_fails_closed`` -- the policy resolver RAISES ->
   the DISTINCT policy-unresolved refusal and no build. This is the FIRST-EVER
   coverage of the fail-closed policy-unresolved path.
5. ``test_network_axis_unchanged_by_gate`` -- the gate does not perturb the
   network axis: with the SSOT network resolver stubbed to ``("none", "ro")``,
   the build still runs and forwards ``networkmode="none"``.
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from infra.container_manager import ContainerManager


def _seed_ws_capabilities(monkeypatch, tmp_path, workspace_id="default"):
    """Seed a fully-permissive workspace ``capabilities.json`` for
    ``workspace_id`` so ``ContainerManager.build_image``'s session
    container-grant gate admits the build.

    The gate routes through ``ContainerManager._compute_config``, which reads
    the workspace capabilities from the GLOBAL vault root (honouring
    ``THOUGHTMACHINE_VAULT_ROOT``), so the capabilities file is seeded under a
    dedicated tmp vault root via ``ensure_workspace_dirs``.
    """
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path / "caps-vault"))
    from thoughtmachine.workspace_capabilities import ensure_workspace_dirs

    ensure_workspace_dirs(workspace_id)


class TestBuildImageSessionGrantGate:
    """Session container-grant gate on ``ContainerManager.build_image``."""

    @staticmethod
    def _manager(mock_client, ws_path, vault_root, workspace_id,
                 session_permissions=None):
        manager = ContainerManager.__new__(ContainerManager)  # skip docker.from_env()
        manager.client = mock_client
        manager.workspace_path = ws_path
        manager.vault_root = vault_root
        manager.workspace_id = workspace_id
        if session_permissions is not None:
            manager.session_permissions = session_permissions
        return manager

    @staticmethod
    def _mock_dex(captured):
        """Stub docker_executor: fixed auto-tag + a build that snapshots kwargs."""
        dex = MagicMock()
        dex._compute_image_tag.side_effect = lambda ws: "agent-executor-test"
        dex._resolve_container_config_via_gate.return_value = ("none", "ro")

        def _capture_build(client, build_path, dockerfile, tag, **kw):
            captured["kwargs"] = kw
            return "sha256:abc", ["Step 1"]

        dex._run_image_build.side_effect = _capture_build
        return dex

    @staticmethod
    def _seed_vault(tmp_path, workspace_id="ws-1"):
        """Create the vault-managed Dockerfile + requirements.txt for a build."""
        vault_root = tmp_path / "vault"
        vault_ws = vault_root / "workspaces" / workspace_id
        vault_ws.mkdir(parents=True)
        (vault_ws / "Dockerfile").write_text("FROM python:3.12-slim\n")
        (vault_ws / "requirements.txt").write_text("vault-pin==1.0\n")
        return vault_root

    @patch("infra.container_manager.DOCKER_AVAILABLE", True)
    def test_without_session_container_grant_refuses(self, tmp_path, monkeypatch):
        """Permissive caps but NO session container grant -> grant-denied refusal.

        The policy resolves (permissive workspace capabilities), yet the SESSION
        denies container access: the gate must raise the grant-denied message --
        NOT the policy-unresolved one -- and must not attempt a build.
        """
        _seed_ws_capabilities(monkeypatch, tmp_path, "ws-1")
        vault_root = self._seed_vault(tmp_path, "ws-1")

        manager = self._manager(
            MagicMock(), str(tmp_path / "ws"), str(vault_root), "ws-1",
            session_permissions={"container": False},
        )
        captured = {}

        with patch("infra.container_manager._load_docker_executor") as mock_load:
            dex = self._mock_dex(captured)
            mock_load.return_value = dex
            with pytest.raises(RuntimeError) as ei:
                manager.build_image()

        message = str(ei.value)
        assert "build" in message
        assert "session container grant denied" in message
        assert "session_permissions['container']" in message
        assert "could not be resolved" not in message
        dex._run_image_build.assert_not_called()
        assert captured == {}

    @patch("infra.container_manager.DOCKER_AVAILABLE", True)
    def test_workspace_docker_not_allowed_refuses(self, tmp_path, monkeypatch):
        """Session GRANTS container but the WORKSPACE forbids docker -> refusal.

        ``allow_docker`` is False in the workspace capabilities, so the effective
        container grant is False even though the session asked for it: the gate
        refuses (grant-denied) and no build runs. Locks the workspace axis.
        """
        _seed_ws_capabilities(monkeypatch, tmp_path, "ws-1")
        caps_path = (
            tmp_path / "caps-vault" / "workspaces" / "ws-1" / "capabilities.json"
        )
        caps = json.loads(caps_path.read_text())
        caps["allow_docker"] = False
        caps_path.write_text(json.dumps(caps))

        vault_root = self._seed_vault(tmp_path, "ws-1")

        manager = self._manager(
            MagicMock(), str(tmp_path / "ws"), str(vault_root), "ws-1",
            session_permissions={"container": True},
        )
        captured = {}

        with patch("infra.container_manager._load_docker_executor") as mock_load:
            dex = self._mock_dex(captured)
            mock_load.return_value = dex
            with pytest.raises(RuntimeError) as ei:
                manager.build_image()

        message = str(ei.value)
        assert "session container grant denied" in message
        assert "session_permissions['container']" in message
        assert "could not be resolved" not in message
        dex._run_image_build.assert_not_called()
        assert captured == {}

    @patch("infra.container_manager.DOCKER_AVAILABLE", True)
    def test_granted_build_proceeds(self, tmp_path, monkeypatch):
        """Session grant + permissive caps -> the build runs exactly once."""
        _seed_ws_capabilities(monkeypatch, tmp_path, "ws-1")
        vault_root = self._seed_vault(tmp_path, "ws-1")

        manager = self._manager(
            MagicMock(), str(tmp_path / "ws"), str(vault_root), "ws-1",
            session_permissions={"container": True},
        )
        captured = {}

        with patch("infra.container_manager._load_docker_executor") as mock_load:
            dex = self._mock_dex(captured)
            mock_load.return_value = dex
            result = manager.build_image()

        assert dex._run_image_build.call_count == 1
        assert set(result) == {"image_tag", "build_log"}
        assert result["image_tag"] == "agent-executor-test"

    @patch("infra.container_manager.DOCKER_AVAILABLE", True)
    def test_unresolvable_policy_fails_closed(self, tmp_path, monkeypatch):
        """A RAISING policy resolver -> the DISTINCT policy-unresolved refusal.

        FIRST-EVER coverage of the fail-closed policy-unresolved path: when
        ``_compute_config`` itself cannot resolve the session policy (here it
        raises), ``build_image`` must raise the policy-unresolved message --
        which is deliberately DISTINCT from the ordinary grant-denied one -- and
        must not attempt a build. A "could not be resolved" refusal must never
        be conflated with an honest "container grant denied".
        """
        _seed_ws_capabilities(monkeypatch, tmp_path, "ws-1")
        vault_root = self._seed_vault(tmp_path, "ws-1")

        manager = self._manager(
            MagicMock(), str(tmp_path / "ws"), str(vault_root), "ws-1",
            session_permissions={"container": True},
        )
        captured = {}

        with patch("infra.container_manager._load_docker_executor") as mock_load:
            dex = self._mock_dex(captured)
            mock_load.return_value = dex
            with patch.object(
                ContainerManager, "_compute_config",
                side_effect=RuntimeError("policy resolver exploded"),
            ):
                with pytest.raises(RuntimeError) as ei:
                    manager.build_image()

        message = str(ei.value)
        assert "session policy could not be resolved" in message
        assert "could not be resolved" in message
        assert "session container grant denied" not in message
        dex._run_image_build.assert_not_called()
        assert captured == {}

    @patch("infra.container_manager.DOCKER_AVAILABLE", True)
    def test_network_axis_unchanged_by_gate(self, tmp_path, monkeypatch):
        """The gate leaves the network axis intact: ``none`` is forwarded.

        With the SSOT network resolver stubbed to ``("none", "ro")`` (as
        ``tests/docker/test_build_networkmode.py`` does), a granted build still
        runs and forwards ``networkmode="none"`` -- the session-grant gate must
        not perturb the egress gating applied afterwards.
        """
        _seed_ws_capabilities(monkeypatch, tmp_path, "ws-1")
        vault_root = self._seed_vault(tmp_path, "ws-1")

        manager = self._manager(
            MagicMock(), str(tmp_path / "ws"), str(vault_root), "ws-1",
            session_permissions={"container": True},
        )
        captured = {}

        with patch("infra.container_manager._load_docker_executor") as mock_load:
            dex = self._mock_dex(captured)
            mock_load.return_value = dex
            result = manager.build_image()

        assert dex._run_image_build.call_count == 1
        assert captured["kwargs"].get("networkmode") == "none"
        assert os.path.basename(result["image_tag"]) == "agent-executor-test"
