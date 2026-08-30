"""Integration tests: SessionConfig serialisation round-trip.

Construct a fully-populated SessionConfig, serialise with
``model_dump(exclude={'api_key'})``, persist to JSON, reload,
and verify all non-sensitive fields survive.
"""

import json
from pathlib import Path

import pytest

from agent.config.session_config import SessionConfig
from session.tool_presets import PRESETS


# Full set of non-sensitive fields to round-trip
FULL_CONFIG_KWARGS = dict(
    mode="engineer",
    workspace_path="/tmp/test_ws",
    system_prompt="Custom prompt text",
    provider_id="test_provider",
    model="test-model",
    temperature=0.5,
    max_turns=100,
    base_url="https://test.api.com",
    session_permissions={"container": False, "network": False},
    enabled_tools=["Respond", "CheckSystem", "git_read"],
)


class TestConfigRoundtrip:
    """Serialise / deserialise SessionConfig with and without api_key."""

    @pytest.fixture
    def output_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "configs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ── helpers ──────────────────────────────────────────────────────────

    def _roundtrip(
        self,
        config: SessionConfig,
        output_dir: Path,
        name: str = "config.json",
    ) -> tuple[SessionConfig, dict]:
        """Serialize *config* to JSON, reload, return (rebuilt, raw_dict)."""
        data = config.model_dump(exclude={"api_key"})
        json_path = output_dir / name
        json_path.write_text(json.dumps(data, indent=2))
        loaded_data = json.loads(json_path.read_text())
        restored = SessionConfig(**loaded_data)
        return restored, loaded_data

    # ── tests ────────────────────────────────────────────────────────────

    def test_all_explicit_fields_roundtrip(self, output_dir: Path) -> None:
        """All non-sensitive fields survive round-trip for 'engineer' mode."""
        config = SessionConfig(api_key="sk-test-secret-12345", **FULL_CONFIG_KWARGS)

        restored, raw = self._roundtrip(config, output_dir)

        # Mode
        assert restored.mode == config.mode

        # Tools — for non-custom modes the validator overrides enabled_tools
        # with the preset.  We check that the preset was applied.
        expected_tools = set(PRESETS[config.mode])
        assert set(restored.enabled_tools) == expected_tools

        # Prompt — validator loads resource prompt; our explicit value is
        # replaced by the mode preset for non-custom modes.
        assert restored.system_prompt is not None
        assert "AI software engineer" in restored.system_prompt

        # Other non-sensitive fields that are *not* overwritten by validator
        assert restored.workspace_path == config.workspace_path
        assert restored.provider_id == config.provider_id
        assert restored.model == config.model
        assert restored.temperature == config.temperature
        assert restored.max_turns == config.max_turns
        assert restored.base_url == config.base_url
        assert restored.session_permissions == config.session_permissions

        # api_key must NOT be present in serialized data
        assert "api_key" not in raw, "api_key leaked into serialized JSON"

    def test_custom_mode_respects_explicit_values(self, output_dir: Path) -> None:
        """In custom mode the validator does *not* override tools/prompt."""
        explicit_tools = ["Respond", "CheckSystem", "git_read"]
        config = SessionConfig(
            mode="custom",
            enabled_tools=list(explicit_tools),
            system_prompt="My custom prompt",
            api_key="sk-test-secret-12345",
        )

        restored, raw = self._roundtrip(config, output_dir)

        # Custom mode — tools and prompt should survive
        assert restored.mode == "custom"
        assert set(restored.enabled_tools) == set(explicit_tools)
        assert restored.system_prompt == "My custom prompt"

        # api_key absent in serialized data
        assert "api_key" not in raw

    def test_agent_mode_applies_preset(self, output_dir: Path) -> None:
        """Agent mode auto-loads agent preset tools + prompt."""
        config = SessionConfig(
            mode="agent",
            api_key="sk-test-secret-12345",
        )
        restored, raw = self._roundtrip(config, output_dir)

        assert set(restored.enabled_tools) == set(PRESETS["agent"])
        assert restored.system_prompt is not None
        assert "AI agent" in restored.system_prompt
        assert "api_key" not in raw

    def test_serialized_json_structure(self, output_dir: Path) -> None:
        """Check the shape of the serialized JSON."""
        config = SessionConfig(
            mode="custom",
            enabled_tools=["Respond"],
            api_key="sk-test-secret-12345",
        )
        _, raw = self._roundtrip(config, output_dir)

        # Must NOT contain api_key
        assert "api_key" not in raw
        # Must contain expected top-level keys
        for key in ("mode", "enabled_tools", "temperature", "max_turns"):
            assert key in raw, f"Missing expected key {key!r} in serialized JSON"
