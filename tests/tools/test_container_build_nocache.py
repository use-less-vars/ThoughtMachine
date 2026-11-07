"""Agent-facing NO-CACHE build surface for ContainerBuildTool.

DISPATCH-A-NOCACHE-v1, branch ``bug/build-tool-nocache`` (base ``b27fee83``).

Correctness argument: the build-drift gate (existing image tag re-used ONLY when
its ``thoughtmachine.build_hash`` label matches the freshly computed hash) can
only be *exercised* by forcing a no-cache rebuild. Before this change NO
agent-facing surface could request one -- ``ContainerBuildTool`` had no
``nocache`` field and ``ContainerManager.build_image`` had no ``nocache``
parameter -- so ``docker_executor._run_image_build`` always received its default
``nocache=False``. These tests pin the new plumbing end to end:

- T1: ``ContainerBuildTool(nocache=True)`` -> the flag reaches ``_run_image_build``.
- T2: ``ContainerBuildTool(nocache=False)`` (and the default) -> unchanged, flag
      arrives as ``False``.
- T3: other callers (``DockerCodeRunner(build=True)`` -> ``manager.build_image(tag=...)``
      with no ``nocache``) are unaffected: the default still flows through as ``False``.
"""

from unittest.mock import MagicMock, patch

from infra.container_manager import ContainerManager
from tools.container_control import ContainerBuildTool


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


def _real_manager(tmp_path):
    """A real ``ContainerManager`` (skipping ``docker.from_env()``) with a valid
    vault Dockerfile + requirements so ``build_image`` reaches the build call."""
    vault_root = tmp_path / "vault"
    ws_dir = vault_root / "workspaces" / "ws-1"
    ws_dir.mkdir(parents=True)
    (ws_dir / "Dockerfile").write_text("FROM python:3.12-slim\n")
    (ws_dir / "requirements.txt").write_text("pin==1.0\n")
    ws = tmp_path / "ws"
    ws.mkdir()

    manager = ContainerManager.__new__(ContainerManager)  # skip docker.from_env()
    manager.client = MagicMock()
    manager.workspace_path = str(ws)
    manager.vault_root = str(vault_root)
    manager.workspace_id = "ws-1"
    manager.session_permissions = {"container": True}
    return manager


def _mock_dex(captured):
    """Stub docker_executor whose ``_run_image_build`` records its kwargs."""
    dex = MagicMock()
    dex._compute_image_tag.side_effect = lambda ws: "agent-executor-test"
    dex._resolve_container_config_via_gate.return_value = ("none", "ro")
    dex.compute_executor_build_hash.return_value = "hash-xyz"

    def _capture(client, build_path, dockerfile, tag, **kw):
        captured.update(kw)
        return "sha256:abc", ["Step 1"]

    dex._run_image_build.side_effect = _capture
    return dex


@patch("infra.container_manager.DOCKER_AVAILABLE", True)
class TestContainerBuildToolNocache:
    """T1/T2: the tool's ``nocache`` field is forwarded to ``_run_image_build``."""

    def test_t1_nocache_true_reaches_run_image_build(self, tmp_path, monkeypatch):
        _seed_ws_capabilities(monkeypatch, tmp_path, "ws-1")
        _grant_disk_container(monkeypatch)
        manager = _real_manager(tmp_path)
        captured = {}
        with patch("infra.container_manager._load_docker_executor") as mock_load, \
             patch("tools.container_control.ContainerManager") as mock_cls:
            dex = _mock_dex(captured)
            mock_load.return_value = dex
            mock_cls.return_value = manager

            tool = ContainerBuildTool(
                workspace_path="/tmp/test_ws",
                session_permissions={"container": True},
                nocache=True,
            )
            result = tool.execute()

        assert '"success": true' in result
        assert dex._run_image_build.call_count == 1
        assert captured.get("nocache") is True

    def test_t2_nocache_false_is_unchanged(self, tmp_path, monkeypatch):
        _seed_ws_capabilities(monkeypatch, tmp_path, "ws-1")
        _grant_disk_container(monkeypatch)
        manager = _real_manager(tmp_path)
        captured = {}
        with patch("infra.container_manager._load_docker_executor") as mock_load, \
             patch("tools.container_control.ContainerManager") as mock_cls:
            dex = _mock_dex(captured)
            mock_load.return_value = dex
            mock_cls.return_value = manager

            tool = ContainerBuildTool(
                workspace_path="/tmp/test_ws",
                session_permissions={"container": True},
                nocache=False,
            )
            tool.execute()

        assert captured.get("nocache") is False

    def test_t2b_default_field_is_false(self):
        tool = ContainerBuildTool(
            workspace_path="/tmp/test_ws",
            session_permissions={"container": True},
        )
        assert tool.nocache is False


@patch("infra.container_manager.DOCKER_AVAILABLE", True)
class TestBuildImageNocacheDefault:
    """T3: the OTHER caller (DockerCodeRunner -> build_image(tag=...)) is unaffected."""

    def test_t3_manager_default_forwarded_as_false(self, tmp_path, monkeypatch):
        _seed_ws_capabilities(monkeypatch, tmp_path, "ws-1")
        _grant_disk_container(monkeypatch)
        manager = _real_manager(tmp_path)
        captured = {}
        with patch("infra.container_manager._load_docker_executor") as mock_load:
            dex = _mock_dex(captured)
            mock_load.return_value = dex
            # DockerCodeRunner(build=True) calls exactly this: no nocache kwarg.
            manager.build_image(tag="agent-executor-test")

        assert dex._run_image_build.call_count == 1
        assert captured.get("nocache") is False


@patch("infra.container_manager.DOCKER_AVAILABLE", True)
class TestNocacheBypassesReuseGate:
    """``nocache=True`` must bypass the build-hash reuse gate.

    The gate reuses the existing image whenever its ``thoughtmachine.build_hash``
    label matches the freshly computed hash, so a forced rebuild has to be
    excluded from the gate explicitly. Otherwise ``ContainerBuildTool(nocache=True)``
    would be short-circuited on a drifted daemon cache and the rebuild silently
    skipped.
    """

    _LABEL = "thoughtmachine.build_hash"

    @staticmethod
    def _seed_matching_image(manager, dex, tag="agent-executor-test"):
        dex.EXECUTOR_BUILD_HASH_LABEL = TestNocacheBypassesReuseGate._LABEL
        image = MagicMock()
        image.labels = {TestNocacheBypassesReuseGate._LABEL: "hash-xyz"}
        manager.client.images.get.return_value = image

    def test_matching_hash_without_nocache_still_skips_build(self, tmp_path, monkeypatch):
        _seed_ws_capabilities(monkeypatch, tmp_path, "ws-1")
        _grant_disk_container(monkeypatch)
        manager = _real_manager(tmp_path)
        captured = {}
        with patch("infra.container_manager._load_docker_executor") as mock_load:
            dex = _mock_dex(captured)
            mock_load.return_value = dex
            self._seed_matching_image(manager, dex)
            result = manager.build_image(tag="agent-executor-test")

        assert dex._run_image_build.call_count == 0
        assert result == {"image_tag": "agent-executor-test", "build_log": ""}

    def test_matching_hash_with_nocache_actually_rebuilds(self, tmp_path, monkeypatch):
        _seed_ws_capabilities(monkeypatch, tmp_path, "ws-1")
        _grant_disk_container(monkeypatch)
        manager = _real_manager(tmp_path)
        captured = {}
        with patch("infra.container_manager._load_docker_executor") as mock_load:
            dex = _mock_dex(captured)
            mock_load.return_value = dex
            self._seed_matching_image(manager, dex)
            result = manager.build_image(tag="agent-executor-test", nocache=True)

        assert dex._run_image_build.call_count == 1
        assert captured.get("nocache") is True
        assert result["image_tag"] == "agent-executor-test"

    def test_mismatched_hash_without_nocache_rebuilds(self, tmp_path, monkeypatch):
        _seed_ws_capabilities(monkeypatch, tmp_path, "ws-1")
        _grant_disk_container(monkeypatch)
        manager = _real_manager(tmp_path)
        captured = {}
        with patch("infra.container_manager._load_docker_executor") as mock_load:
            dex = _mock_dex(captured)
            mock_load.return_value = dex
            self._seed_matching_image(manager, dex)
            manager.client.images.get.return_value.labels = {self._LABEL: "stale-hash"}
            manager.build_image(tag="agent-executor-test")

        assert dex._run_image_build.call_count == 1
        assert captured.get("nocache") is False

