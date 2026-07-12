"""Tests for ConfigSnapshot — capturing and persisting AgentConfig."""

from __future__ import annotations

import json
import os
import time
import pytest

from agent.config.models import AgentConfig
from agent.logging.config_snapshot import ConfigSnapshot


class TestConfigSnapshot:
    """Tests for ConfigSnapshot capture/load cycle."""

    @pytest.fixture
    def workspace(self, tmp_path):
        """Return a temporary workspace path as string."""
        return str(tmp_path / "test_workspace")

    @pytest.fixture
    def sample_config(self):
        """Return a realistic AgentConfig for testing."""
        return AgentConfig(
            model="gpt-4",
            provider_type="openai",
            api_key="sk-test-secret",  # should be excluded from snapshot
            max_turns=25,
            token_monitor_warning_threshold=10000,
            token_monitor_critical_threshold=20000,
            timeout_seconds=300,
            enabled_tools=[
                "Bash",
                "PythonExecutor",
                "FileEditor",
                "GlobTool",
            ],
            base_url="https://api.openai.com",
            temperature=0.5,
        )

    # ------------------------------------------------------------------
    # Happy path
    # ------------------------------------------------------------------

    def test_capture_creates_file(self, workspace, sample_config):
        """capture() should write config_snapshot.json to the workspace."""
        snapshotter = ConfigSnapshot(workspace)
        snapshotter.capture(sample_config, label="session_start")

        assert os.path.exists(snapshotter.file_path), (
            f"Expected {snapshotter.file_path} to exist"
        )

        with open(snapshotter.file_path) as f:
            data = json.load(f)

        assert data["label"] == "session_start"
        assert isinstance(data["timestamp"], (int, float))
        assert data["model"] == "gpt-4"
        assert data["max_turns"] == 25
        assert data["token_warning_threshold"] == 10000
        assert data["token_critical_threshold"] == 20000
        assert data["timeout_seconds"] == 300
        assert data["enabled_tools"] == [
            "Bash",
            "FileEditor",
            "GlobTool",
            "PythonExecutor",
        ]  # sorted

    def test_capture_excludes_api_key(self, workspace, sample_config):
        """The snapshot should NOT contain the raw api_key."""
        snapshotter = ConfigSnapshot(workspace)
        snapshotter.capture(sample_config, label="test")

        with open(snapshotter.file_path) as f:
            data = json.load(f)

        # api_key should be excluded from the config dump
        assert "api_key" not in data["config"], (
            "api_key should be excluded from snapshot config"
        )

    def test_capture_excludes_stop_check(self, workspace, sample_config):
        """The snapshot should NOT contain the stop_check field."""
        snapshotter = ConfigSnapshot(workspace)
        snapshotter.capture(sample_config, label="test")

        with open(snapshotter.file_path) as f:
            data = json.load(f)

        assert "stop_check" not in data["config"], (
            "stop_check should be excluded from snapshot config"
        )

    def test_capture_includes_config_dict(self, workspace, sample_config):
        """The snapshot's 'config' key should contain the full model dump."""
        snapshotter = ConfigSnapshot(workspace)
        snapshotter.capture(sample_config, label="test")

        with open(snapshotter.file_path) as f:
            data = json.load(f)

        cfg = data["config"]
        assert cfg["model"] == "gpt-4"
        assert cfg["max_turns"] == 25
        assert cfg["timeout_seconds"] == 300
        assert "temperature" in cfg

    def test_load_returns_snapshot(self, workspace, sample_config):
        """load() should return the previously captured data."""
        snapshotter = ConfigSnapshot(workspace)
        snapshotter.capture(sample_config, label="my_label")

        loaded = snapshotter.load()
        assert loaded is not None
        assert loaded["label"] == "my_label"
        assert loaded["model"] == "gpt-4"
        assert loaded["max_turns"] == 25

    def test_load_capture_roundtrip(self, workspace, sample_config):
        """capture then load should produce identical top-level keys."""
        snapshotter = ConfigSnapshot(workspace)
        snapshotter.capture(sample_config, label="roundtrip")

        loaded = snapshotter.load()
        assert loaded is not None
        for key in ("label", "model", "max_turns", "timeout_seconds", "enabled_tools"):
            assert key in loaded, f"Missing key: {key}"

    # ------------------------------------------------------------------
    # Edge cases
    # ------------------------------------------------------------------

    def test_load_no_file(self, workspace):
        """load() should return None when no snapshot exists."""
        snapshotter = ConfigSnapshot(workspace)
        assert snapshotter.load() is None

    def test_capture_default_label(self, workspace, sample_config):
        """capture() should default to label='session_start'."""
        snapshotter = ConfigSnapshot(workspace)
        snapshotter.capture(sample_config)

        data = snapshotter.load()
        assert data is not None
        assert data["label"] == "session_start"

    def test_corrupted_file_returns_corrupted_data(self, workspace, sample_config):
        """load() should return whatever is in the file (no recovery)."""
        # Write invalid JSON
        os.makedirs(os.path.dirname(str(workspace)), exist_ok=True)
        bad_path = os.path.join(workspace, "config_snapshot.json")
        os.makedirs(os.path.dirname(bad_path), exist_ok=True)
        with open(bad_path, "w") as f:
            f.write("{invalid json")

        snapshotter = ConfigSnapshot(workspace)
        with pytest.raises(json.JSONDecodeError):
            snapshotter.load()

    def test_multiple_captures_overwrite(self, workspace, sample_config):
        """A second capture should overwrite the first."""
        snapshotter = ConfigSnapshot(workspace)

        # First capture
        cfg1 = AgentConfig(model="gpt-3.5-turbo", provider_type="openai")
        snapshotter.capture(cfg1, label="first")

        # Second capture (different model)
        cfg2 = AgentConfig(model="gpt-4", provider_type="openai")
        snapshotter.capture(cfg2, label="second")

        data = snapshotter.load()
        assert data is not None
        assert data["label"] == "second"
        assert data["model"] == "gpt-4"

    def test_timestamp_is_recent(self, workspace, sample_config):
        """The captured timestamp should be close to current time."""
        before = time.time()
        snapshotter = ConfigSnapshot(workspace)
        snapshotter.capture(sample_config, label="ts_test")
        after = time.time()

        data = snapshotter.load()
        assert data is not None
        ts = data["timestamp"]
        assert before <= ts <= after, (
            f"Timestamp {ts} not in range [{before}, {after}]"
        )

    def test_non_existent_directory_creates_it(self, workspace, sample_config):
        """capture() should create directories that don't exist."""
        nested = os.path.join(workspace, "sub", "dir")
        snapshotter = ConfigSnapshot(nested)
        snapshotter.capture(sample_config, label="nested")
        assert os.path.exists(snapshotter.file_path)
        data = snapshotter.load()
        assert data is not None
        assert data["label"] == "nested"

    def test_config_with_no_enabled_tools(self, workspace):
        """Minimal config with no tools should still produce a valid snapshot."""
        cfg = AgentConfig(model="deepseek-chat", provider_type="openai", enabled_tools=[])
        snapshotter = ConfigSnapshot(workspace)
        snapshotter.capture(cfg, label="no_tools")

        data = snapshotter.load()
        assert data is not None
        assert data["model"] == "deepseek-chat"
        assert data["enabled_tools"] == []
