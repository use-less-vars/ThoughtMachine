"""
Tests for the per-workspace container control tools (label-scoped listing).

Covers
------
- ContainerManager.list_containers(): daemon query scoped by the exact
  ``thoughtmachine.workspace_id`` label that ``start()`` applies; the exact
  {container_id, name, image, status, uptime_seconds, workspace_id, note}
  dict shape; uptime from ``attrs['State']['StartedAt']`` with None guards;
  daemon-error resilience.
- ContainerListTool: the zero-parameter tool wrapper — success JSON with
  ``containers``/``count``, required_categories, error JSON on failure.

No live Docker daemon is needed: the daemon-side label filter is simulated in
the mock (``containers.list`` only returns containers whose workspace label
matches the requested filter), which is exactly how "containers from another
workspace (or unlabeled) do NOT appear" is enforced in production.
"""

import glob
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from tools.container_control import ContainerBuildTool, ContainerListTool, ContainerLogsTool
from infra.container_manager import (
    ContainerManager,
    NotFound,
    EXEC_OUTPUT_LIMIT_BYTES,
    _TRUNCATION_NOTICE,
)


# ══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _parse_result(result: str) -> dict:
    """Parse JSON tool result into a dict."""
    return json.loads(result)


def _iso_dt(seconds_ago: int) -> str:
    """ISO-8601 timestamp ``seconds_ago`` in the past, Z-suffixed (Docker style)."""
    dt = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return dt.isoformat().replace("+00:00", "Z")


def _framed_logs(stdout_payloads, stderr_payloads):
    """Build a docker multiplexed log stream (8-byte frames) for mocking.

    Each payload is emitted as one frame: byte 0 = stream id (1 = stdout,
    2 = stderr), bytes 1-3 unused, bytes 4-7 = big-endian payload length.
    """
    out = bytearray()
    for payload in stdout_payloads:
        out += b"\x01\x00\x00\x00" + len(payload).to_bytes(4, "big") + payload
    for payload in stderr_payloads:
        out += b"\x02\x00\x00\x00" + len(payload).to_bytes(4, "big") + payload
    return bytes(out)


def _mock_container(cid, name, status, labels, started_seconds_ago=None, image_tags=None):
    """Build a fake docker Container object with the fields list_containers uses."""
    container = MagicMock()
    container.id = cid
    container.name = name
    container.status = status
    container.labels = labels
    attrs = {}
    if started_seconds_ago is not None:
        attrs["State"] = {"StartedAt": _iso_dt(started_seconds_ago)}
    else:
        attrs["State"] = {}
    container.attrs = attrs
    image = MagicMock()
    image.tags = list(image_tags or [])
    container.image = image
    return container


def _daemon_that_filters_by_workspace_label(all_containers):
    """Simulate the daemon: containers.list returns only label-matching containers."""
    def fake_list(**kwargs):
        label = kwargs["filters"]["label"]
        prefix = "thoughtmachine.workspace_id="
        assert label.startswith(prefix), label
        want = label[len(prefix):]
        return [
            c for c in all_containers
            if c.labels.get("thoughtmachine.workspace_id") == want
        ]
    return fake_list


# ══════════════════════════════════════════════════════════════════════════════
#  ContainerManager.list_containers
# ══════════════════════════════════════════════════════════════════════════════

class TestContainerManagerListContainers:
    """Tests for ContainerManager.list_containers() with a mocked daemon."""

    @staticmethod
    def _manager(mock_client, workspace_id="ws-1"):
        manager = ContainerManager.__new__(ContainerManager)  # skip docker.from_env()
        manager.client = mock_client
        manager.workspace_id = "default" if workspace_id is None else workspace_id
        return manager

    def test_returns_only_workspace_containers_with_exact_fields(self):
        """Two workspace containers appear; other-workspace and unlabeled do not."""
        mock_client = MagicMock()
        all_containers = [
            _mock_container("abc123", "tm-1", "running",
                            {"thoughtmachine.workspace_id": "ws-1"},
                            started_seconds_ago=3600, image_tags=["agent-executor:latest"]),
            _mock_container("def456", "tm-2", "exited",
                            {"thoughtmachine.workspace_id": "ws-1"},
                            started_seconds_ago=7200, image_tags=["agent-executor:latest"]),
            _mock_container("999999", "other-workspace", "running",
                            {"thoughtmachine.workspace_id": "ws-2"},
                            started_seconds_ago=60, image_tags=["other:tag"]),
            _mock_container("777777", "unlabeled", "created",
                            {},
                            started_seconds_ago=30, image_tags=["other:tag"]),
        ]
        mock_client.containers.list.side_effect = _daemon_that_filters_by_workspace_label(
            all_containers
        )

        result = self._manager(mock_client, workspace_id="ws-1").list_containers()

        mock_client.containers.list.assert_called_once_with(
            all=True,
            filters={"label": "thoughtmachine.workspace_id=ws-1"},
        )
        assert len(result) == 2
        for entry in result:
            assert set(entry.keys()) == {
                "container_id", "name", "image", "status", "uptime_seconds",
                "workspace_id", "note",
            }
        by_id = {e["container_id"]: e for e in result}
        assert set(by_id) == {"abc123", "def456"}
        assert by_id["abc123"] == {
            "container_id": "abc123",
            "name": "tm-1",
            "image": "agent-executor:latest",
            "status": "running",
            "uptime_seconds": by_id["abc123"]["uptime_seconds"],
            "workspace_id": "ws-1",
            "note": "",
        }
        # uptime == now - StartedAt (≈3600s, allowing a little clock drift)
        assert isinstance(by_id["abc123"]["uptime_seconds"], int)
        assert 3590 <= by_id["abc123"]["uptime_seconds"] <= 3610
        assert by_id["def456"]["status"] == "exited"
        assert by_id["def456"]["uptime_seconds"] is not None

    def test_missing_started_at_yields_none_uptime(self):
        """attrs['State']['StartedAt'] missing -> uptime_seconds None (guarded)."""
        mock_client = MagicMock()
        container = _mock_container("abc123", "tm-1", "created",
                                    {"thoughtmachine.workspace_id": "ws-1"},
                                    started_seconds_ago=None)
        container.attrs = {}  # no State at all
        mock_client.containers.list.return_value = [container]

        result = self._manager(mock_client).list_containers()

        assert len(result) == 1
        assert result[0]["uptime_seconds"] is None
        assert result[0]["image"] is None

    def test_image_none_when_no_tags(self):
        """Container image without tags -> image None (guarded)."""
        mock_client = MagicMock()
        container = _mock_container("abc123", "tm-1", "running",
                                    {"thoughtmachine.workspace_id": "ws-1"},
                                    started_seconds_ago=10, image_tags=[])
        mock_client.containers.list.return_value = [container]

        result = self._manager(mock_client).list_containers()

        assert result[0]["image"] is None

    def test_daemon_error_returns_empty_list(self):
        """containers.list raising -> [] (list_containers never raises)."""
        mock_client = MagicMock()
        mock_client.containers.list.side_effect = Exception("daemon unreachable")

        result = self._manager(mock_client).list_containers()

        assert result == []

    def test_none_workspace_id_uses_default_label_value(self):
        """workspace_id=None -> label value 'default' (same source start() uses)."""
        mock_client = MagicMock()
        mock_client.containers.list.return_value = []

        result = self._manager(mock_client, workspace_id=None).list_containers()

        mock_client.containers.list.assert_called_once_with(
            all=True,
            filters={"label": "thoughtmachine.workspace_id=default"},
        )
        assert result == []


# ══════════════════════════════════════════════════════════════════════════════
#  ContainerListTool
# ══════════════════════════════════════════════════════════════════════════════

class TestContainerListTool:
    """Tests for the ContainerListTool wrapper (mocked ContainerManager)."""

    def test_execute_returns_success_json_with_containers(self):
        expected = [
            {"container_id": "abc123", "name": "tm-1", "image": "agent-executor:latest",
             "status": "running", "uptime_seconds": 123},
            {"container_id": "def456", "name": "tm-2", "image": None,
             "status": "exited", "uptime_seconds": None},
        ]
        with patch("tools.container_control.ContainerManager") as mock_cls:
            mock_manager = MagicMock()
            mock_manager.list_containers.return_value = expected
            mock_cls.return_value = mock_manager

            tool = ContainerListTool(
                workspace_path="/tmp/test_ws",
                session_permissions={"container": True},
            )
            result = _parse_result(tool.execute())

        mock_cls.assert_called_once()  # _make_manager() built exactly one manager
        assert result["success"] is True
        assert result["count"] == 2
        assert result["containers"] == expected
        assert "error" not in result
        assert result["duration"] >= 0

    def test_required_categories(self):
        assert ContainerListTool.required_categories == ["container:true"]

    def test_no_tool_specific_input_fields(self):
        """ContainerListTool declares no parameters beyond the tool marker."""
        assert set(ContainerListTool.__annotations__) == {"tool"}

    def test_execute_returns_error_json_on_runtime_error(self):
        with patch("tools.container_control.ContainerManager") as mock_cls:
            mock_manager = MagicMock()
            mock_manager.list_containers.side_effect = RuntimeError("docker unavailable")
            mock_cls.return_value = mock_manager

            tool = ContainerListTool(
                workspace_path="/tmp/test_ws",
                session_permissions={"container": True},
            )
            result = _parse_result(tool.execute())

        assert result["success"] is False
        assert "docker unavailable" in result["error"]
        assert result["duration"] >= 0

    def test_execute_returns_error_json_on_unexpected_error(self):
        with patch("tools.container_control.ContainerManager") as mock_cls:
            mock_manager = MagicMock()
            mock_manager.list_containers.side_effect = ValueError("boom")
            mock_cls.return_value = mock_manager

            tool = ContainerListTool(
                workspace_path="/tmp/test_ws",
                session_permissions={"container": True},
            )
            result = _parse_result(tool.execute())

        assert result["success"] is False
        assert "Unexpected error: boom" in result["error"]
        assert result["duration"] >= 0


# ══════════════════════════════════════════════════════════════════════════════
#  ContainerManager.build_image
# ══════════════════════════════════════════════════════════════════════════════

class TestContainerManagerBuildImage:
    """Tests for ContainerManager.build_image() with a mocked docker_executor."""

    @staticmethod
    def _manager(mock_client, ws_path, vault_root=None, workspace_id="default"):
        manager = ContainerManager.__new__(ContainerManager)  # skip docker.from_env()
        manager.client = mock_client
        manager.workspace_path = ws_path
        manager.vault_root = vault_root
        manager.workspace_id = workspace_id
        return manager

    @staticmethod
    def _mock_dex():
        """Stub docker_executor module: fixed auto-tag + mocked _run_image_build."""
        dex = MagicMock()
        dex._compute_image_tag.side_effect = lambda ws: "agent-executor-test"
        return dex

    @patch("infra.container_manager.DOCKER_AVAILABLE", True)
    def test_build_success_with_auto_tag_and_exact_keys(self, tmp_path):
        """Dockerfile present, tag omitted -> auto-tag; result has EXACT keys."""
        (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\n")
        mock_client = MagicMock()
        manager = self._manager(mock_client, str(tmp_path), vault_root=str(tmp_path))
        captured = {}

        with patch("infra.container_manager._load_docker_executor") as mock_load:
            dex = self._mock_dex()

            def _capture_build(client, build_path, dockerfile, tag, **kw):
                # Inspect the temp context WHILE it exists (build_image
                # removes it before returning).
                captured["path"] = build_path
                captured["listing"] = sorted(os.listdir(build_path))
                return "sha256:abc", ["Step 1: FROM x", "Step 2: RUN y"]

            dex._run_image_build.side_effect = _capture_build
            mock_load.return_value = dex

            result = manager.build_image()

        assert set(result.keys()) == {"image_tag", "build_log"}
        assert result["image_tag"] == "agent-executor-test"
        assert result["build_log"] == "Step 1: FROM x\nStep 2: RUN y"
        dex._run_image_build.assert_called_once()
        args = dex._run_image_build.call_args.args
        assert args[0] is mock_client
        assert args[2] == "Dockerfile"
        assert args[3] == "agent-executor-test"
        # Build context = temp dir with ONLY the Dockerfile, NOT the workspace.
        assert captured["path"] != str(tmp_path)
        assert captured["listing"] == ["Dockerfile"]

    @patch("infra.container_manager.DOCKER_AVAILABLE", True)
    def test_missing_vault_dockerfile_raises(self, tmp_path):
        """No vault Dockerfile in workspace -> clear RuntimeError, executor never used."""
        mock_client = MagicMock()
        manager = self._manager(mock_client, str(tmp_path), vault_root=str(tmp_path))

        with patch("infra.container_manager._load_docker_executor") as mock_load:
            with pytest.raises(RuntimeError, match="Vault Dockerfile not found"):
                manager.build_image()

        mock_load.assert_not_called()

    @patch("infra.container_manager.DOCKER_AVAILABLE", True)
    def test_vault_dockerfile_with_explicit_tag(self, tmp_path):
        """Explicit tag + vault Dockerfile -> used as-is, auto-tag skipped."""
        (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\n")
        mock_client = MagicMock()
        manager = self._manager(mock_client, str(tmp_path), vault_root=str(tmp_path))

        with patch("infra.container_manager._load_docker_executor") as mock_load:
            dex = self._mock_dex()
            dex._run_image_build.return_value = ("sha256:def", ["Step 1"])
            mock_load.return_value = dex

            result = manager.build_image(tag="my-tag:1")

        assert result["image_tag"] == "my-tag:1"
        dex._run_image_build.assert_called_once()
        args = dex._run_image_build.call_args.args
        assert args[0] is mock_client
        assert args[1] != str(tmp_path)
        assert args[2] == "Dockerfile"
        assert args[3] == "my-tag:1"
        dex._compute_image_tag.assert_not_called()

    @patch("infra.container_manager.DOCKER_AVAILABLE", True)
    def test_build_log_truncated_at_100kb(self, tmp_path):
        """Build log > EXEC_OUTPUT_LIMIT_BYTES (100 KiB) -> truncated + notice."""
        (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\n")
        mock_client = MagicMock()
        manager = self._manager(mock_client, str(tmp_path), vault_root=str(tmp_path))

        with patch("infra.container_manager._load_docker_executor") as mock_load:
            dex = self._mock_dex()
            dex._run_image_build.return_value = (
                "sha256:abc", ["x" * 50000, "y" * 50000, "z" * 5000],
            )
            mock_load.return_value = dex

            result = manager.build_image()

        assert "truncated" in result["build_log"]
        assert len(result["build_log"]) <= EXEC_OUTPUT_LIMIT_BYTES + len(_TRUNCATION_NOTICE)

    @patch("infra.container_manager.DOCKER_AVAILABLE", True)
    def test_build_failure_raises_runtime_error(self, tmp_path):
        """_run_image_build raising -> RuntimeError propagates unchanged."""
        (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\n")
        mock_client = MagicMock()
        manager = self._manager(mock_client, str(tmp_path), vault_root=str(tmp_path))

        with patch("infra.container_manager._load_docker_executor") as mock_load:
            dex = self._mock_dex()
            dex._run_image_build.side_effect = RuntimeError("Docker build failed: boom")
            mock_load.return_value = dex

            with pytest.raises(RuntimeError, match="boom"):
                manager.build_image()

    @patch("infra.container_manager.DOCKER_AVAILABLE", True)
    def test_planted_non_vault_dockerfile_not_used(self, tmp_path):
        """Vault-gating: a planted non-vault Dockerfile is NOT a fallback."""
        (tmp_path / "Dockerfile.custom").write_text("FROM python:3.12-slim\n")
        mock_client = MagicMock()
        manager = self._manager(mock_client, str(tmp_path), vault_root=str(tmp_path))

        with patch("infra.container_manager._load_docker_executor") as mock_load:
            with pytest.raises(RuntimeError, match="Vault Dockerfile not found"):
                manager.build_image(tag="x:1")

        mock_load.assert_not_called()  # planted file ignored, no build attempted

    @patch("infra.container_manager.DOCKER_AVAILABLE", True)
    def test_build_image_context_excludes_workspace_files(self, tmp_path):
        """Security: build context contains ONLY the vault Dockerfile.

        The resolved vault Dockerfile is copied into a temporary build
        directory; the workspace tree (with sensitive_file.txt) is never part
        of the build context.
        """
        vault_root = tmp_path / "vault"
        vault_ws = vault_root / "workspaces" / "ws-1"
        vault_ws.mkdir(parents=True)
        (vault_ws / "Dockerfile").write_text("FROM python:3.12-slim\n")
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "Dockerfile").write_text("FROM python:3.11-slim\n")
        (ws / "sensitive_file.txt").write_text("TOP SECRET\n")
        mock_client = MagicMock()
        manager = self._manager(
            mock_client, str(ws), vault_root=str(vault_root), workspace_id="ws-1"
        )

        captured = {}

        def _capture_build(client, build_path, dockerfile, tag, **kw):
            # Inspect the context WHILE it exists (build_image removes the
            # temp dir before returning).
            captured["path"] = build_path
            captured["dockerfile"] = dockerfile
            captured["tag"] = tag
            assert build_path != str(ws), "workspace leaked as build context"
            assert build_path != str(vault_ws), "vault workspace leaked as context"
            assert sorted(os.listdir(build_path)) == ["Dockerfile"]
            assert "sensitive_file.txt" not in os.listdir(build_path)
            with open(os.path.join(build_path, "Dockerfile")) as fh:
                assert fh.read() == "FROM python:3.12-slim\n"
            return "sha256:abc123", ["Step 1/2 : FROM python:3.12-slim"]

        with patch("infra.container_manager._load_docker_executor") as mock_load:
            dex = self._mock_dex()
            dex._run_image_build.side_effect = _capture_build
            mock_load.return_value = dex

            result = manager.build_image()

        assert captured["dockerfile"] == "Dockerfile"
        assert captured["tag"] == "agent-executor-test"
        assert result["image_tag"] == "agent-executor-test"
        assert "Step 1/2" in result["build_log"]
        assert not os.path.exists(captured["path"])  # temp build dir cleaned up
        assert (ws / "sensitive_file.txt").exists()  # workspace file untouched

    @patch("infra.container_manager.DOCKER_AVAILABLE", True)
    def test_build_image_tempdir_cleaned_up(self, tmp_path):
        """Temporary build directory is removed after a successful build."""
        (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\n")
        before = set(glob.glob(os.path.join(tempfile.gettempdir(), "tm_build_*")))
        mock_client = MagicMock()
        manager = self._manager(mock_client, str(tmp_path), vault_root=str(tmp_path))

        with patch("infra.container_manager._load_docker_executor") as mock_load:
            dex = self._mock_dex()
            dex._run_image_build.return_value = ("sha256:x", ["Step 1"])
            mock_load.return_value = dex
            manager.build_image()

        after = set(glob.glob(os.path.join(tempfile.gettempdir(), "tm_build_*")))
        assert after == before


# ══════════════════════════════════════════════════════════════════════════════
#  ContainerBuildTool
# ══════════════════════════════════════════════════════════════════════════════

class TestContainerBuildTool:
    """Tests for the ContainerBuildTool wrapper (mocked ContainerManager)."""

    def test_execute_returns_success_json(self):
        with patch("tools.container_control.ContainerManager") as mock_cls:
            mock_manager = MagicMock()
            mock_manager.build_image.return_value = {
                "image_tag": "agent-executor-abc",
                "build_log": "Step 1/2",
            }
            mock_cls.return_value = mock_manager

            tool = ContainerBuildTool(
                workspace_path="/tmp/test_ws",
                session_permissions={"container": True},
            )
            result = _parse_result(tool.execute())

        mock_cls.assert_called_once()  # _make_manager() built exactly one manager
        mock_manager.build_image.assert_called_once_with(tag=None)
        assert set(result.keys()) == {"success", "image_tag", "build_log", "duration"}
        assert result["success"] is True
        assert result["image_tag"] == "agent-executor-abc"
        assert result["build_log"] == "Step 1/2"
        assert "error" not in result
        assert result["duration"] >= 0

    def test_execute_returns_error_json_on_runtime_error(self):
        with patch("tools.container_control.ContainerManager") as mock_cls:
            mock_manager = MagicMock()
            mock_manager.build_image.side_effect = RuntimeError(
                "Vault Dockerfile not found at /x/Dockerfile"
            )
            mock_cls.return_value = mock_manager

            tool = ContainerBuildTool(
                workspace_path="/tmp/test_ws",
                session_permissions={"container": True},
            )
            result = _parse_result(tool.execute())

        assert result["success"] is False
        assert "Vault Dockerfile not found at /x/Dockerfile" in result["error"]
        assert result["duration"] >= 0

    def test_execute_returns_error_json_on_unexpected_error(self):
        with patch("tools.container_control.ContainerManager") as mock_cls:
            mock_manager = MagicMock()
            mock_manager.build_image.side_effect = ValueError("boom")
            mock_cls.return_value = mock_manager

            tool = ContainerBuildTool(
                workspace_path="/tmp/test_ws",
                session_permissions={"container": True},
            )
            result = _parse_result(tool.execute())

        assert result["success"] is False
        assert "Unexpected error: boom" in result["error"]
        assert result["duration"] >= 0

    def test_required_categories(self):
        assert ContainerBuildTool.required_categories == ["container:true"]

    def test_params_default_to_none(self):
        tool = ContainerBuildTool(
            workspace_path="/tmp/test_ws",
            session_permissions={"container": True},
        )
        assert tool.tag is None

    def test_dockerfile_path_rejected(self):
        """Vault-gating: dockerfile_path is no longer a tool parameter."""
        with pytest.raises(ValidationError):
            ContainerBuildTool(
                workspace_path="/tmp/test_ws",
                session_permissions={"container": True},
                dockerfile_path="Dockerfile.custom",
            )

    def test_dockerfile_path_absent_from_schema(self):
        """Vault-gating: dockerfile_path absent from the tool's parameter schema."""
        assert "dockerfile_path" not in ContainerBuildTool.model_fields
        assert "dockerfile_path" not in ContainerBuildTool.model_json_schema().get(
            "properties", {}
        )
        assert "tag" in ContainerBuildTool.model_fields  # sanity: tag remains

    def test_vault_dockerfile_missing_returns_clean_error_no_fallback(self):
        """Missing vault Dockerfile -> clean error JSON; NO fallback build attempt."""
        with patch("tools.container_control.ContainerManager") as mock_cls:
            mock_manager = MagicMock()
            mock_manager.build_image.side_effect = RuntimeError(
                "Vault Dockerfile not found at /x/Dockerfile. "
                "The vault-managed <workspace>/Dockerfile must exist before building."
            )
            mock_cls.return_value = mock_manager

            tool = ContainerBuildTool(
                workspace_path="/tmp/test_ws",
                session_permissions={"container": True},
            )
            result = _parse_result(tool.execute())

        mock_cls.assert_called_once()  # exactly one manager built
        mock_manager.build_image.assert_called_once_with(tag=None)
        assert mock_manager.build_image.call_count == 1  # no fallback retry/build
        assert result["success"] is False
        assert "Vault Dockerfile not found" in result["error"]
        assert result["duration"] >= 0


# ══════════════════════════════════════════════════════════════════════════════
#  ContainerManager.get_logs
# ══════════════════════════════════════════════════════════════════════════════

class TestContainerManagerGetLogs:
    """Tests for ContainerManager.get_logs() with a mocked docker client."""

    @staticmethod
    def _manager(logs_return=b""):
        manager = ContainerManager.__new__(ContainerManager)  # skip docker.from_env()
        manager.client = MagicMock()
        logs_mock = MagicMock()
        logs_mock.logs.return_value = logs_return
        manager.client.containers.get.return_value = logs_mock
        return manager, logs_mock

    @patch("infra.container_manager.DOCKER_AVAILABLE", True)
    def test_success_decodes_stdout_and_stderr(self):
        manager, logs_mock = self._manager(
            _framed_logs([b"hello\n", b"world\n"], [b"err1\n", b"err2\n"])
        )
        result = manager.get_logs(container_id="abc")

        assert set(result.keys()) == {"stdout", "stderr"}
        assert result["stdout"] == "hello\nworld\n"
        assert result["stderr"] == "err1\nerr2\n"
        logs_mock.logs.assert_called_once_with(
            stdout=True, stderr=True, tail=100, since=None
        )

    @patch("infra.container_manager.DOCKER_AVAILABLE", True)
    def test_tail_and_since_passthrough(self):
        manager, logs_mock = self._manager()
        manager.get_logs(container_id="c1", tail=50, since="10m")
        logs_mock.logs.assert_called_once_with(
            stdout=True, stderr=True, tail=50, since="10m"
        )

    @patch("infra.container_manager.DOCKER_AVAILABLE", True)
    def test_truncated_at_100kb(self):
        big = b"x" * 103000  # > EXEC_OUTPUT_LIMIT_BYTES (102400)
        manager, _ = self._manager(_framed_logs([b"ok\n"], [big]))
        result = manager.get_logs(container_id="abc")

        assert "truncated" in result["stderr"]
        assert len(result["stderr"]) > EXEC_OUTPUT_LIMIT_BYTES
        assert len(result["stderr"]) <= EXEC_OUTPUT_LIMIT_BYTES + 40
        assert result["stdout"] == "ok\n"  # small stream untouched

    @patch("infra.container_manager.DOCKER_AVAILABLE", True)
    def test_raw_tty_output_falls_back_to_stdout(self):
        # No frame headers (tty containers) -> whole payload treated as stdout.
        manager, _ = self._manager(b"plain log line\n")
        result = manager.get_logs(container_id="abc")
        assert result["stdout"] == "plain log line\n"
        assert result["stderr"] == ""

    @patch("infra.container_manager.DOCKER_AVAILABLE", True)
    def test_container_not_found_raises(self):
        manager = ContainerManager.__new__(ContainerManager)
        manager.client = MagicMock()
        manager.client.containers.get.side_effect = NotFound(
            "404 Client Error: No such container: abc"
        )
        with pytest.raises(RuntimeError, match="Container abc not found"):
            manager.get_logs(container_id="abc")

    @patch("infra.container_manager.DOCKER_AVAILABLE", True)
    def test_access_error_raises(self):
        manager = ContainerManager.__new__(ContainerManager)
        manager.client = MagicMock()
        manager.client.containers.get.side_effect = Exception("daemon unreachable")
        with pytest.raises(RuntimeError, match="Failed to access container abc"):
            manager.get_logs(container_id="abc")

    @patch("infra.container_manager.DOCKER_AVAILABLE", True)
    def test_daemon_error_raises(self):
        manager, logs_mock = self._manager()
        logs_mock.logs.side_effect = Exception("daemon down")
        with pytest.raises(RuntimeError, match="Failed to fetch logs"):
            manager.get_logs(container_id="abc")


# ══════════════════════════════════════════════════════════════════════════════
#  ContainerLogsTool
# ══════════════════════════════════════════════════════════════════════════════

class TestContainerLogsTool:
    """Tests for ContainerLogsTool: JSON envelope, never raises, param plumbing."""

    def test_execute_returns_success_json(self):
        with patch("tools.container_control.ContainerManager") as mock_cls:
            mock_manager = MagicMock()
            mock_manager.get_logs.return_value = {
                "stdout": "line1\nline2\n",
                "stderr": "err1\n",
            }
            mock_cls.return_value = mock_manager

            tool = ContainerLogsTool(
                workspace_path="/tmp/test_ws",
                session_permissions={"container": True},
                container_id="abc",
            )
            result = _parse_result(tool.execute())

        mock_cls.assert_called_once()  # _make_manager() built exactly one manager
        mock_manager.get_logs.assert_called_once_with(
            container_id="abc", tail=100, since=None
        )
        assert set(result.keys()) == {"success", "stdout", "stderr", "duration"}
        assert result["success"] is True
        assert result["stdout"] == "line1\nline2\n"
        assert result["stderr"] == "err1\n"
        assert "error" not in result
        assert result["duration"] >= 0

    def test_execute_returns_error_json_on_runtime_error(self):
        with patch("tools.container_control.ContainerManager") as mock_cls:
            mock_manager = MagicMock()
            mock_manager.get_logs.side_effect = RuntimeError("Container abc not found")
            mock_cls.return_value = mock_manager

            tool = ContainerLogsTool(
                workspace_path="/tmp/test_ws",
                session_permissions={"container": True},
                container_id="abc",
            )
            result = _parse_result(tool.execute())

        assert result["success"] is False
        assert "Container abc not found" in result["error"]
        assert result["duration"] >= 0

    def test_execute_returns_error_json_on_unexpected_error(self):
        with patch("tools.container_control.ContainerManager") as mock_cls:
            mock_manager = MagicMock()
            mock_manager.get_logs.side_effect = ValueError("boom")
            mock_cls.return_value = mock_manager

            tool = ContainerLogsTool(
                workspace_path="/tmp/test_ws",
                session_permissions={"container": True},
                container_id="abc",
            )
            result = _parse_result(tool.execute())

        assert result["success"] is False
        assert "Unexpected error: boom" in result["error"]
        assert result["duration"] >= 0

    def test_required_categories(self):
        assert ContainerLogsTool.required_categories == ["container:true"]

    def test_container_id_required_no_default(self):
        assert ContainerLogsTool.model_fields["container_id"].is_required()
        assert ContainerLogsTool.model_fields["tail"].default == 100
        assert ContainerLogsTool.model_fields["since"].default is None


# ═════════════════════════════════════════════════════════════════════════════
#  ContainerManager.status introspection (Phase 6)
# ═════════════════════════════════════════════════════════════════════════════

def _introspection_exec_run(raise_on_usage_log=False, pip_output=None):
    """Build a fake.exec_run dispatcher for the Phase 6 introspection tests.

    Matches the commands issued by ``_exec_checked()`` (cmd lists with
    ``/bin/sh -c <command>``) and returns demux-style
    ``(exit_code, (stdout_bytes, stderr_bytes))`` tuples. Any unrecognised
    command (e.g. the exec() quota probe ``du -s ...``) yields an empty,
    successful result.
    """

    def dispatcher(cmd, **kwargs):
        command = cmd[2] if isinstance(cmd, list) and len(cmd) >= 3 else str(cmd)
        if "usage.log" in command:
            if raise_on_usage_log:
                raise RuntimeError("append failed")
            return (0, (b"2025-01-01T00:00:00+00:00 | s1 | echo hi\n", b""))
        if "pip list --format=json" in command:
            return (0, (pip_output or b'[{"name":"requests","version":"2.31.0"}]', b""))
        if "/proc/" in command:
            return (0, (b"1\t/bin/sh\n42\tpython3 main.py\n", b""))
        if "du -sh /workspace" in command:
            return (0, (b"12M\t/workspace\n3M\t/home/agent/.local\n", b""))
        if "echo hello" in command:
            return (0, (b"hello\n", b""))
        return (0, (b"", b""))

    return dispatcher


class TestContainerManagerStatusIntrospection:
    """Phase 6: status() live introspection + persistent usage log."""

    @staticmethod
    def _manager(dispatcher=None):
        manager = ContainerManager.__new__(ContainerManager)  # skip docker.from_env()
        client = MagicMock()
        fake = MagicMock()
        fake.id = "c1"
        fake.name = "tm-1"
        fake.status = "running"
        fake.attrs = {"State": {"StartedAt": _iso_dt(120)}}
        if dispatcher is not None:
            fake.exec_run.side_effect = dispatcher
        client.containers.get.return_value = fake
        client.stats.return_value = {"memory_stats": {"usage": 1234}}
        manager.client = client
        manager.workspace_id = "ws-1"
        manager.session_id = "sess-1"
        manager.container_notes = {}
        manager.workspace_config = {}
        return manager, fake

    def test_status_running_includes_introspection(self):
        """Running container: packages, processes, disk, history all present."""
        manager, _ = self._manager(_introspection_exec_run())
        result = manager.status("c1")

        assert result["container_id"] == "c1"
        assert result["name"] == "tm-1"
        assert result["status"] == "running"
        assert result["memory_usage_bytes"] == 1234
        assert result["note"] == ""
        assert isinstance(result["uptime_seconds"], int)
        assert 110 <= result["uptime_seconds"] <= 130
        assert result["installed_packages"] == [
            {"name": "requests", "version": "2.31.0"}
        ]
        assert result["running_processes"] == [
            {"pid": 1, "command": "/bin/sh"},
            {"pid": 42, "command": "python3 main.py"},
        ]
        assert result["disk_usage"] == {"workspace": "12M", "packages": "3M"}
        assert result["recent_commands"] == [
            "2025-01-01T00:00:00+00:00 | s1 | echo hi"
        ]
        assert "introspection_errors" not in result

    def test_status_introspection_failures_degrade(self):
        """Daemon exec failures -> error entries, no raise, no partial keys."""
        manager, fake = self._manager()
        fake.exec_run.side_effect = Exception("daemon down")

        result = manager.status("c1")

        assert result["status"] == "running"
        assert "installed_packages" not in result
        assert "running_processes" not in result
        assert "disk_usage" not in result
        assert result["recent_commands"] == []
        assert result["introspection_errors"] == [
            "pip list failed",
            "process scan failed",
            "du failed",
        ]

    def test_status_bad_pip_json_reported(self):
        """Unparseable pip JSON -> introspection_errors, packages absent."""
        manager, _ = self._manager(_introspection_exec_run(pip_output=b"not json"))
        result = manager.status("c1")

        assert "installed_packages" not in result
        assert result["introspection_errors"] == ["pip list JSON unparseable"]
        assert result["running_processes"]  # other probes still succeed

    def test_status_stopped_has_no_introspection(self):
        """Stopped container: no Phase 6 keys, no exec probes issued."""
        manager, fake = self._manager(_introspection_exec_run())
        fake.status = "exited"

        result = manager.status("c1")

        assert result["status"] == "exited"
        for key in ("installed_packages", "running_processes", "disk_usage",
                    "recent_commands", "introspection_errors"):
            assert key not in result
        fake.exec_run.assert_not_called()

    def test_exec_appends_usage_log(self):
        """exec() success appends a session-scoped line to usage.log."""
        manager, fake = self._manager(_introspection_exec_run())

        result = manager.exec("c1", "echo hello")

        assert result["stdout"] == "hello\n"
        assert result["stderr"] == ""
        assert result["exit_code"] == 0
        append_calls = [
            c for c in fake.exec_run.call_args_list
            if c.kwargs.get("cmd") and "usage.log" in " ".join(c.kwargs["cmd"])
        ]
        assert len(append_calls) == 1
        cmd = append_calls[0].kwargs["cmd"]
        assert cmd[:2] == ["/bin/sh", "-c"]
        assert cmd[2].startswith("printf '%s\\n' '")
        assert "echo hello" in cmd[2]
        assert "sess-1" in cmd[2]

    def test_exec_usage_log_failure_ignored(self):
        """usage.log append failure never affects the exec result."""
        manager, _ = self._manager(_introspection_exec_run(raise_on_usage_log=True))

        result = manager.exec("c1", "echo hello")

        assert result["stdout"] == "hello\n"
        assert result["exit_code"] == 0

    def test_container_history_returns_lines(self):
        """container_history() returns tail lines for a running container."""
        manager, _ = self._manager(_introspection_exec_run())

        lines = manager.container_history("c1", tail=20)

        assert lines == ["2025-01-01T00:00:00+00:00 | s1 | echo hi"]

    def test_container_history_stopped_empty(self):
        """container_history() is [] for non-running containers, no exec."""
        manager, fake = self._manager(_introspection_exec_run())
        fake.status = "exited"

        assert manager.container_history("c1", tail=20) == []
        fake.exec_run.assert_not_called()

    def test_container_summary_running(self):
        """container_summary() -> packages_count + disk_usage for running."""
        manager, _ = self._manager(_introspection_exec_run())

        assert manager.container_summary("c1") == {
            "packages_count": 1,
            "disk_usage": {"workspace": "12M", "packages": "3M"},
        }

    def test_container_summary_stopped(self):
        """container_summary() -> {} for non-running containers."""
        manager, fake = self._manager(_introspection_exec_run())
        fake.status = "exited"

        assert manager.container_summary("c1") == {}
        fake.exec_run.assert_not_called()

