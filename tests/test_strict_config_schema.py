"""Phase B: strict config schemas (extra='forbid').

AgentConfig, SessionConfig and ProviderProfile must reject unknown keys at
construction, and PresetLoader must skip preset YAML files that contain keys
outside the documented preset schema.
"""
import textwrap

import pytest
from pydantic import ValidationError

from agent.config.models import AgentConfig
from agent.config.session_config import SessionConfig
from agent.config.provider_profile import ProviderProfile
from agent.config.preset import PresetLoader


# ── Pydantic models: unknown keys raise ──────────────────────────────────────


def test_agent_config_rejects_unknown_keys():
    with pytest.raises(ValidationError):
        AgentConfig(**{"mode": "agent", "bogus_field": 1})


def test_session_config_rejects_unknown_keys():
    with pytest.raises(ValidationError):
        SessionConfig(**{"mode": "agent", "bogus_field": 1})


def test_session_config_rejects_frontend_aux_keys():
    # Keys the frontend sends but which are not SessionConfig fields must not
    # be silently swallowed — the server filters them via _session_config_dict.
    with pytest.raises(ValidationError):
        SessionConfig(**{"mode": "agent", "api_key_configured": False})


def test_provider_profile_rejects_unknown_keys():
    with pytest.raises(ValidationError):
        ProviderProfile(**{"id": "p1", "label": "P1", "bogus_field": 1})


# ── PresetLoader: unknown YAML keys skip the preset ──────────────────────────


def _write_preset(tmp_path, name, content):
    p = tmp_path / f"{name}.yaml"
    p.write_text(textwrap.dedent(content))
    return p


def test_preset_loader_rejects_unknown_keys(tmp_path):
    _write_preset(
        tmp_path,
        "good",
        """
        name: Good
        system_prompt: hi
        model: deepseek-chat
        """,
    )
    _write_preset(
        tmp_path,
        "bad",
        """
        name: Bad
        system_prompt: hi
        model: deepseek-chat
        unknown_key: nope
        """,
    )
    loader = PresetLoader(str(tmp_path))
    assert set(loader.list_presets()) == {"Good"}
    assert loader.get_preset("Bad") is None


def test_preset_loader_accepts_all_documented_keys(tmp_path):
    _write_preset(
        tmp_path,
        "full",
        """
        name: Full
        system_prompt: hi
        model: deepseek-chat
        temperature: 0.5
        tools:
          - FilePreviewTool
        safety_level: standard
        """,
    )
    loader = PresetLoader(str(tmp_path))
    preset = loader.get_preset("Full")
    assert preset is not None
    assert preset.temperature == 0.5
    assert preset.tools == ["FilePreviewTool"]
    assert preset.safety_level == "standard"
