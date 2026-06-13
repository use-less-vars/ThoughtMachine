"""Tests for StateBridge save_config system prompt handling."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from agent.config import loader as config_loader
from agent.presenter.state_bridge import StateBridge


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def temp_custom_prompt_path():
    """Provide a temporary custom_system_prompt.txt path (isolated from user's real one)."""
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
        temp_path = f.name
    yield temp_path
    # Cleanup
    try:
        os.unlink(temp_path)
    except FileNotFoundError:
        pass


@pytest.fixture
def state_bridge(temp_custom_prompt_path):
    """Create a StateBridge with a temporary config path and mocked CUSTOM_SYSTEM_PROMPT_PATH."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        config_path = f.name

    bridge = StateBridge(config_path=config_path)

    # Patch the custom system prompt path so tests don't touch ~/.thoughtmachine/
    with patch.object(config_loader, "CUSTOM_SYSTEM_PROMPT_PATH", temp_custom_prompt_path):
        yield bridge

    # Cleanup
    try:
        os.unlink(config_path)
    except FileNotFoundError:
        pass


@pytest.fixture
def factory_default_text():
    """Return the current factory-default system prompt text."""
    return config_loader.load_default_system_prompt_text()


# ── Tests ────────────────────────────────────────────────────────────────────


class TestSaveConfigSystemPrompt:
    """Tests for system prompt save/delete logic in StateBridge.save_config()."""

    def test_none_prompt_removes_custom_file(self, state_bridge):
        """A None system_prompt should delete the custom file."""
        custom_path = Path(config_loader.CUSTOM_SYSTEM_PROMPT_PATH)
        # Pre-create the custom file
        custom_path.parent.mkdir(parents=True, exist_ok=True)
        custom_path.write_text("some old prompt\n", encoding="utf-8")
        assert custom_path.exists()

        state_bridge.save_config({"system_prompt": None})
        assert not custom_path.exists(), "Custom file should be removed for None prompt"

    def test_empty_string_prompt_removes_custom_file(self, state_bridge):
        """An empty string system_prompt should delete the custom file."""
        custom_path = Path(config_loader.CUSTOM_SYSTEM_PROMPT_PATH)
        custom_path.parent.mkdir(parents=True, exist_ok=True)
        custom_path.write_text("some old prompt\n", encoding="utf-8")
        assert custom_path.exists()

        state_bridge.save_config({"system_prompt": ""})
        assert not custom_path.exists(), "Custom file should be removed for empty prompt"

    def test_whitespace_prompt_removes_custom_file(self, state_bridge):
        """A whitespace-only system_prompt should delete the custom file."""
        custom_path = Path(config_loader.CUSTOM_SYSTEM_PROMPT_PATH)
        custom_path.parent.mkdir(parents=True, exist_ok=True)
        custom_path.write_text("some old prompt\n", encoding="utf-8")
        assert custom_path.exists()

        state_bridge.save_config({"system_prompt": "   \n  "})
        assert not custom_path.exists(), "Custom file should be removed for whitespace-only prompt"

    def test_custom_prompt_writes_file(self, state_bridge):
        """A non-default, non-empty system_prompt should be written to the custom file."""
        custom_path = Path(config_loader.CUSTOM_SYSTEM_PROMPT_PATH)
        custom_prompt = "This is a custom prompt for testing."

        state_bridge.save_config({"system_prompt": custom_prompt})
        assert custom_path.exists(), "Custom file should exist after saving a custom prompt"
        written = custom_path.read_text(encoding="utf-8")
        assert written == custom_prompt + "\n", "Custom prompt should be written with trailing newline"

    def test_custom_prompt_overwrites_previous(self, state_bridge):
        """Saving a new custom prompt should overwrite any previous custom file."""
        custom_path = Path(config_loader.CUSTOM_SYSTEM_PROMPT_PATH)
        custom_path.parent.mkdir(parents=True, exist_ok=True)
        custom_path.write_text("old prompt\n", encoding="utf-8")

        new_prompt = "new custom prompt"
        state_bridge.save_config({"system_prompt": new_prompt})
        written = custom_path.read_text(encoding="utf-8")
        assert written == new_prompt + "\n", "Old prompt should be overwritten"

    def test_factory_default_prompt_removes_custom_file(self, state_bridge, factory_default_text):
        """The factory-default system prompt text should trigger a delete (no custom file)."""
        custom_path = Path(config_loader.CUSTOM_SYSTEM_PROMPT_PATH)
        custom_path.parent.mkdir(parents=True, exist_ok=True)
        custom_path.write_text("some old prompt\n", encoding="utf-8")
        assert custom_path.exists()

        state_bridge.save_config({"system_prompt": factory_default_text})
        assert not custom_path.exists(), (
            "Custom file should be removed when prompt matches factory default"
        )

    def test_factory_default_stripped_variant_removes_custom_file(self, state_bridge, factory_default_text):
        """Factory default with extra whitespace around it should still trigger delete."""
        custom_path = Path(config_loader.CUSTOM_SYSTEM_PROMPT_PATH)
        custom_path.parent.mkdir(parents=True, exist_ok=True)
        custom_path.write_text("some old prompt\n", encoding="utf-8")
        assert custom_path.exists()

        # Add leading/trailing whitespace — should be stripped before comparison
        state_bridge.save_config({"system_prompt": "  " + factory_default_text + "  "})
        assert not custom_path.exists(), (
            "Custom file should be removed when prompt (after strip) matches factory default"
        )

    def test_near_miss_prompt_writes_file(self, state_bridge, factory_default_text):
        """A prompt similar but not identical to factory default should still be written."""
        custom_path = Path(config_loader.CUSTOM_SYSTEM_PROMPT_PATH)
        near_miss = factory_default_text + "\n# custom addition"

        state_bridge.save_config({"system_prompt": near_miss})
        assert custom_path.exists(), "Custom file should exist for non-matching prompt"
        written = custom_path.read_text(encoding="utf-8")
        assert near_miss + "\n" in written or written == near_miss + "\n"

    def test_prompt_trailing_newline_matches(self, state_bridge, factory_default_text):
        """A prompt matching factory default but with trailing newline should trigger delete."""
        custom_path = Path(config_loader.CUSTOM_SYSTEM_PROMPT_PATH)
        custom_path.parent.mkdir(parents=True, exist_ok=True)
        custom_path.write_text("some old prompt\n", encoding="utf-8")
        assert custom_path.exists()

        state_bridge.save_config({"system_prompt": factory_default_text + "\n"})
        assert not custom_path.exists(), (
            "Custom file should be removed when prompt (after strip) matches factory default"
        )

    def test_reset_config_to_factory_removes_custom_file(self, state_bridge):
        """reset_config_to_factory should remove the custom system prompt file."""
        custom_path = Path(config_loader.CUSTOM_SYSTEM_PROMPT_PATH)
        custom_path.parent.mkdir(parents=True, exist_ok=True)
        custom_path.write_text("some old prompt\n", encoding="utf-8")
        assert custom_path.exists()

        state_bridge.reset_config_to_factory()
        assert not custom_path.exists(), (
            "Custom file should be removed after reset_config_to_factory"
        )
