"""Egress gating for image builds (bug/docker-build-egress-not-gated).

Ruling 2026-10-12: a build's effective network mode follows the session's
effective network grain.

    banned / read / ask / none  -> networkmode="none"
    outbound / write / bridge   -> no networkmode kwarg (Docker default)

Tests mock client.api.build / _run_image_build — testing OUR wiring, not
Docker's behavior.
"""
from unittest.mock import MagicMock

import docker_executor as dex


def _fake_build(calls):
    def _inner(**kwargs):
        calls.append(kwargs)
        return [{"aux": {"ID": "sha256:img"}}]
    return _inner


class TestRunImageBuildKwarg:
    def test_networkmode_is_passed(self):
        calls = []
        client = MagicMock()
        client.api.build.side_effect = _fake_build(calls)
        dex._run_image_build(
            client, "/ctx", "Dockerfile", "tag:1", networkmode="none"
        )
        assert calls[0].get("networkmode") == "none"

    def test_networkmode_absent_by_default(self):
        calls = []
        client = MagicMock()
        client.api.build.side_effect = _fake_build(calls)
        dex._run_image_build(client, "/ctx", "Dockerfile", "tag:1")
        assert "networkmode" not in calls[0]

    def test_labels_still_pass_through(self):
        calls = []
        client = MagicMock()
        client.api.build.side_effect = _fake_build(calls)
        dex._run_image_build(
            client, "/ctx", "Dockerfile", "tag:1",
            labels={"k": "v"}, networkmode="none",
        )
        assert calls[0].get("labels") == {"k": "v"}
        assert calls[0].get("networkmode") == "none"


class TestContainerManagerBuildResolvesNetwork:
    """ContainerManager.build_image resolves net via the SSOT gate, then passes
    it to _run_image_build as the networkmode kwarg.

    Mocks the gate AND _run_image_build. Tests the wiring from
    gate result -> networkmode kwarg. Real gate logic is tested elsewhere.
    """

    def _make_manager(self, tmp_path, perms):
        """Build a ContainerManager without docker.from_env().

        Mirrors the pattern used by tests/tools/test_container_control.py and
        tests/test_build_image_requires_vault_requirements.py: __new__ + manual
        attribute set. If perms is None, session_permissions is deliberately
        left unset — to lock in the fail-closed path in build_image's gate call.
        """
        from infra import container_manager as cm
        ws_id = "test-ws"
        vault_root = tmp_path / "vault"
        (vault_root / "workspaces" / ws_id).mkdir(parents=True)
        (vault_root / "workspaces" / ws_id / "Dockerfile").write_text(
            "FROM python:3.12\n", encoding="utf-8"
        )
        (vault_root / "workspaces" / ws_id / "requirements.txt").write_text(
            "\n", encoding="utf-8"
        )
        mgr = cm.ContainerManager.__new__(cm.ContainerManager)
        mgr.workspace_path = str(tmp_path / "ws")
        mgr.workspace_id = ws_id
        mgr.vault_root = str(vault_root)
        mgr.client = MagicMock()
        if perms is not None:
            mgr.session_permissions = perms
        from docker.errors import ImageNotFound
        mgr.client.images.get.side_effect = ImageNotFound("nope")
        return mgr

    def test_banned_passes_none(self, tmp_path, monkeypatch):
        mgr = self._make_manager(tmp_path, {"network": "banned"})
        monkeypatch.setattr(
            dex, "_resolve_container_config_via_gate",
            lambda *a, **k: ("none", "ro"),
        )
        captured = {}
        def _spy(client, build_path, dockerfile, tag, **kwargs):
            captured.update(kwargs)
            return "sha256:x", ["ok"]
        monkeypatch.setattr(dex, "_run_image_build", _spy)
        mgr.build_image(tag="tag:banned")
        assert captured.get("networkmode") == "none", captured

    def test_outbound_omits(self, tmp_path, monkeypatch):
        mgr = self._make_manager(tmp_path, {"network": "outbound"})
        monkeypatch.setattr(
            dex, "_resolve_container_config_via_gate",
            lambda *a, **k: ("bridge", "rw"),
        )
        captured = {}
        def _spy(client, build_path, dockerfile, tag, **kwargs):
            captured.update(kwargs)
            return "sha256:x", ["ok"]
        monkeypatch.setattr(dex, "_run_image_build", _spy)
        mgr.build_image(tag="tag:outbound")
        assert captured.get("networkmode") is None, captured

    def test_missing_session_permissions_fail_closed(self, tmp_path, monkeypatch):
        """If session_permissions is absent (e.g. __new__ without init), the build
        must run with networkmode='none'. Locks in the fail-closed path.

        Does NOT monkeypatch the gate — the real gate short-circuits on
        session_permissions=None and returns ('none','ro').
        """
        mgr = self._make_manager(tmp_path, perms=None)
        captured = {}
        def _spy(client, build_path, dockerfile, tag, **kwargs):
            captured.update(kwargs)
            return "sha256:x", ["ok"]
        monkeypatch.setattr(dex, "_run_image_build", _spy)
        mgr.build_image(tag="tag:missing-perms")
        assert captured.get("networkmode") == "none", captured
