"""Branch B (RED-first): ``build_image`` requires the VAULT requirements.txt.

``ContainerManager.build_image`` (infra/container_manager.py) must stage
``requirements.txt`` from the vault-managed workspace dir
(``<vault_root>/workspaces/<workspace_id>/requirements.txt``) and REFUSE loudly
when that vault copy is missing, instead of silently falling back to a
workspace-root ``<workspace_path>/requirements.txt``.

These tests pin the vault-only contract:

- A: vault copy present -> the staged bytes are the VAULT bytes.
- B: vault copy MISSING (workspace copy present) -> ``RuntimeError`` telling the
  operator to run the vault bootstrap (the fallback must NOT be used). This is
  the RED test before the fix.
- C: both present -> the staged bytes are the VAULT bytes, never the workspace's.
"""

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


def _grant_disk_container(monkeypatch, grants=None):
    """Wave 9a-0b: build_image's container-grant gate reads the session grants
    from the vault permission store (disk is the source of truth), so stub that
    read to grant a container envelope (ceiling ``None`` == no workspace cap)."""
    import security.security_gate as _sg
    _grants = {"container": True} if grants is None else dict(grants)
    monkeypatch.setattr(
        _sg,
        "_read_disk_permission_sources",
        lambda workspace_id, session_id: (_grants, None),
    )


class TestBuildImageRequiresVaultRequirements:
    """Vault-only requirements resolution for ``ContainerManager.build_image``."""

    @staticmethod
    def _manager(mock_client, ws_path, vault_root, workspace_id):
        manager = ContainerManager.__new__(ContainerManager)  # skip docker.from_env()
        manager.client = mock_client
        manager.workspace_path = ws_path
        manager.vault_root = vault_root
        manager.workspace_id = workspace_id
        # Grant the session container access so build_image's session
        # container-grant gate admits the build (fail-closed otherwise).
        manager.session_permissions = {"container": True}
        return manager

    @staticmethod
    def _mock_dex(captured):
        """Stub docker_executor: fixed auto-tag + a build that snapshots the ctx."""
        dex = MagicMock()
        dex._compute_image_tag.side_effect = lambda ws: "agent-executor-test"
        dex._resolve_container_config_via_gate.return_value = ("none", "ro")

        def _capture_build(client, build_path, dockerfile, tag, **kw):
            # Inspect the temp context WHILE it exists (build_image removes it
            # before returning).
            captured["listing"] = sorted(os.listdir(build_path))
            staged_req = os.path.join(build_path, "requirements.txt")
            captured["requirements"] = (
                open(staged_req, "rb").read() if os.path.exists(staged_req) else None
            )
            return "sha256:abc", ["Step 1"]

        dex._run_image_build.side_effect = _capture_build
        return dex

    @patch("infra.container_manager.DOCKER_AVAILABLE", True)
    def test_vault_requirements_present_stages_vault_bytes(self, tmp_path, monkeypatch):
        """A: vault requirements present -> staged bytes == vault bytes."""
        _seed_ws_capabilities(monkeypatch, tmp_path, "ws-1")
        _grant_disk_container(monkeypatch)
        vault_root = tmp_path / "vault"
        vault_ws = vault_root / "workspaces" / "ws-1"
        vault_ws.mkdir(parents=True)
        (vault_ws / "Dockerfile").write_text("FROM python:3.12-slim\n")
        (vault_ws / "requirements.txt").write_text("vault-pin==1.0\n")

        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "requirements.txt").write_text("workspace-pin==9.9\n")

        mock_client = MagicMock()
        manager = self._manager(
            mock_client, str(ws), vault_root=str(vault_root), workspace_id="ws-1"
        )
        captured = {}

        with patch("infra.container_manager._load_docker_executor") as mock_load:
            dex = self._mock_dex(captured)
            mock_load.return_value = dex
            result = manager.build_image()

        assert result["image_tag"] == "agent-executor-test"
        assert captured["requirements"] == b"vault-pin==1.0\n"
        assert captured["listing"] == ["Dockerfile", "requirements.txt"]

    @patch("infra.container_manager.DOCKER_AVAILABLE", True)
    def test_missing_vault_requirements_refuses_instead_of_fallback(self, tmp_path):
        """B (RED): vault requirements MISSING -> refuse, never fall back.

        A workspace-root ``requirements.txt`` is present, but the vault-managed
        copy is not: the build must raise a RuntimeError pointing the operator at
        the vault bootstrap, NOT silently stage the workspace copy.
        """
        vault_root = tmp_path / "vault"
        vault_ws = vault_root / "workspaces" / "ws-1"
        vault_ws.mkdir(parents=True)
        (vault_ws / "Dockerfile").write_text("FROM python:3.12-slim\n")
        # NOTE: no vault requirements.txt on purpose.

        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "requirements.txt").write_text("workspace-pin==9.9\n")

        mock_client = MagicMock()
        manager = self._manager(
            mock_client, str(ws), vault_root=str(vault_root), workspace_id="ws-1"
        )

        with patch("infra.container_manager._load_docker_executor") as mock_load:
            dex = self._mock_dex({})
            mock_load.return_value = dex
            with pytest.raises(RuntimeError, match="Vault requirements not found") as ei:
                manager.build_image()
            assert "Run the vault bootstrap" in str(ei.value)
            dex._run_image_build.assert_not_called()

    @patch("infra.container_manager.DOCKER_AVAILABLE", True)
    def test_both_present_stages_vault_not_workspace(self, tmp_path, monkeypatch):
        """C: both present -> staged bytes are the VAULT bytes (no fallback)."""
        _seed_ws_capabilities(monkeypatch, tmp_path, "ws-1")
        _grant_disk_container(monkeypatch)
        vault_root = tmp_path / "vault"
        vault_ws = vault_root / "workspaces" / "ws-1"
        vault_ws.mkdir(parents=True)
        (vault_ws / "Dockerfile").write_text("FROM python:3.12-slim\n")
        (vault_ws / "requirements.txt").write_text("vault-pin==1.0\n")

        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "requirements.txt").write_text("workspace-pin==9.9\n")

        mock_client = MagicMock()
        manager = self._manager(
            mock_client, str(ws), vault_root=str(vault_root), workspace_id="ws-1"
        )
        captured = {}

        with patch("infra.container_manager._load_docker_executor") as mock_load:
            dex = self._mock_dex(captured)
            mock_load.return_value = dex
            manager.build_image()

        assert captured["requirements"] == b"vault-pin==1.0\n"
        assert captured["requirements"] != b"workspace-pin==9.9\n"
