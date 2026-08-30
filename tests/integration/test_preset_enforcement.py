"""Integration tests: Tool preset enforcement (mode-locking).

Verifies that:
1. Agent mode rejects tool additions outside its preset.
2. Engineer mode excludes direct-editing tools (FileEditor).
3. Custom mode allows full tool mutation that persists through round-trip.
4. Mode validator strictly overwrites enabled_tools for non-custom modes.
"""

import json
from pathlib import Path

import pytest

from agent.config.session_config import SessionConfig
from session.tool_presets import ENGINEER_TOOLS, AGENT_TOOLS, CUSTOM_TOOLS, PRESETS


class TestPresetEnforcement:
    """Mode-locking behaviour of SessionConfig."""

    @pytest.fixture
    def output_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "presets"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ── Agent mode ───────────────────────────────────────────────────────

    def test_agent_update_tools_is_noop(self) -> None:
        """update_tools() is a no-op for agent mode."""
        config = SessionConfig(mode="agent")
        assert set(config.enabled_tools) == set(AGENT_TOOLS)

        # Attempt to add "Worker" — should be ignored
        config.update_tools(["Worker"])
        assert "Worker" not in config.enabled_tools
        # The original agent preset must remain unchanged
        assert set(config.enabled_tools) == set(AGENT_TOOLS)

    def test_agent_update_prompt_is_noop(self) -> None:
        """update_prompt() is a no-op for agent mode."""
        config = SessionConfig(mode="agent")
        original_prompt = config.system_prompt
        assert original_prompt is not None

        config.update_prompt("This should be ignored")
        assert config.system_prompt == original_prompt

    # ── Engineer mode ────────────────────────────────────────────────────

    def test_engineer_excludes_file_editor(self) -> None:
        """Engineer mode does NOT include direct file-editing tools."""
        config = SessionConfig(mode="engineer")
        assert "FileEditor" not in config.enabled_tools
        assert set(config.enabled_tools) == set(ENGINEER_TOOLS)

    def test_engineer_includes_worker(self) -> None:
        """Engineer mode includes the Worker tool for orchestration."""
        config = SessionConfig(mode="engineer")
        assert "Worker" in config.enabled_tools

    def test_engineer_update_tools_is_noop(self) -> None:
        """update_tools() is a no-op for engineer mode."""
        config = SessionConfig(mode="engineer")
        original = list(config.enabled_tools)

        config.update_tools(["Respond"])
        assert config.enabled_tools == original

    # ── Custom mode ──────────────────────────────────────────────────────

    def test_custom_add_tool_persists(self, output_dir: Path) -> None:
        """Custom mode: adding a tool persists through round-trip."""
        config = SessionConfig(
            mode="custom",
            enabled_tools=["Respond", "CheckSystem"],
        )

        # Add a tool manually
        config.enabled_tools.append("Worker")

        # Serialize and reload
        data = config.model_dump(exclude={"api_key"})
        (output_dir / "custom_add.json").write_text(json.dumps(data))
        loaded = json.loads((output_dir / "custom_add.json").read_text())
        restored = SessionConfig(**loaded)

        assert "Worker" in restored.enabled_tools

    def test_custom_remove_tool_persists(self, output_dir: Path) -> None:
        """Custom mode: removing a tool persists through round-trip."""
        config = SessionConfig(
            mode="custom",
            enabled_tools=["Respond", "CheckSystem", "git_read"],
        )

        # Remove a tool manually
        config.enabled_tools.remove("CheckSystem")

        data = config.model_dump(exclude={"api_key"})
        (output_dir / "custom_remove.json").write_text(json.dumps(data))
        loaded = json.loads((output_dir / "custom_remove.json").read_text())
        restored = SessionConfig(**loaded)

        assert "CheckSystem" not in restored.enabled_tools
        assert "Respond" in restored.enabled_tools
        assert "git_read" in restored.enabled_tools

    def test_custom_update_tools_works(self) -> None:
        """update_tools() replaces the tool list in custom mode."""
        config = SessionConfig(
            mode="custom",
            enabled_tools=["Respond"],
        )

        new_tools = ["Worker", "CheckSystem", "git_read"]
        config.update_tools(new_tools)
        assert config.enabled_tools == new_tools

    def test_custom_update_prompt_works(self) -> None:
        """update_prompt() replaces the prompt in custom mode."""
        config = SessionConfig(
            mode="custom",
            system_prompt="Original prompt",
        )

        config.update_prompt("New custom prompt")
        assert config.system_prompt == "New custom prompt"

    # ── Mode validator strictness ────────────────────────────────────────

    def test_validator_overwrites_tools_for_non_custom(self) -> None:
        """Passing enabled_tools to constructor is ignored for non-custom modes."""
        config = SessionConfig(
            mode="engineer",
            enabled_tools=["Respond"],  # should be overwritten
        )
        # Should have ENGINEER_TOOLS, not just ["Respond"]
        assert set(config.enabled_tools) == set(ENGINEER_TOOLS)
        assert len(config.enabled_tools) == len(ENGINEER_TOOLS)

    def test_validator_overwrites_prompt_for_non_custom(self) -> None:
        """Passing system_prompt to constructor is ignored for non-custom modes."""
        config = SessionConfig(
            mode="engineer",
            system_prompt="This will be replaced",
        )
        assert config.system_prompt is not None
        # Should contain the engineer prompt text, not our override
        assert "AI software engineer" in config.system_prompt
        assert "This will be replaced" not in config.system_prompt
