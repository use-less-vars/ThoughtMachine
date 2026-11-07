"""Integration tests: Session lifecycle round-trip for all modes.

Creates a Session, persists to JSON, rebuilds from JSON, and verifies
that mode, tools, and prompt survive the round-trip.
"""

import json
from pathlib import Path

import pytest

from session.models import Session
from agent.config.session_config import SessionConfig
from session.tool_presets import PRESETS


class TestSessionLifecycle:
    """Round-trip a session through JSON serialisation for each mode."""

    MODES = ["engineer", "agent", "custom"]

    @pytest.fixture
    def session_dir(self, tmp_path: Path) -> Path:
        """A per-test temp directory for session JSON files."""
        d = tmp_path / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ── helpers ──────────────────────────────────────────────────────────

    def _assert_prompt_text(self, mode: str, prompt: str) -> None:
        """Check that ``prompt`` contains mode-appropriate text."""
        hints = {
            "agent":    "AI agent",
            "engineer": "AI software engineer",
        }
        hint = hints.get(mode)
        if hint:
            assert hint in prompt, (
                f"Expected prompt for mode={mode!r} to contain {hint!r}, "
                f"got {prompt[:100]!r}..."
            )

    def _assert_tools_match_preset(self, mode: str, tools: list) -> None:
        """Assert the tool list matches the canonical preset for *mode*.

        For ``'custom'`` mode the validator does NOT enforce any preset,
        so the tool list may be empty (from factory) or user-defined.
        We only perform a strict assertion for non-custom modes.
        """
        if mode == 'custom':
            return  # no preset enforced
        expected = set(PRESETS[mode])
        assert set(tools) == expected, (
            f"Mode {mode!r}: expected {len(expected)} preset tools, "
            f"got {len(tools)} tools"
        )

    # ── round-trip per mode ──────────────────────────────────────────────

    @pytest.mark.parametrize("mode", MODES)
    def test_session_roundtrip(self, mode: str, session_dir: Path) -> None:
        """Create, persist, reload, and verify a session for *mode*."""
        # 1. Create SessionConfig (mode validator auto-loads tools + prompt)
        config = SessionConfig.from_factory(mode=mode)

        # 2. Create Session with config stored in metadata
        session = Session(
            mode=mode,
            metadata={
                "session_config": config.model_dump(exclude={"api_key"}),
            },
        )

        # 3. Serialise to JSON on disk
        data = session.to_persistable_dict()
        json_path = session_dir / f"session_{mode}.json"
        json_path.write_text(json.dumps(data, indent=2))

        # 4. Read back and rebuild Session
        loaded_data = json.loads(json_path.read_text())
        loaded = Session.from_persistable_dict(loaded_data)

        # 5. Rebuild SessionConfig from the persisted metadata
        loaded_config = SessionConfig(**loaded.metadata["session_config"])

        # 6a. Mode survived
        assert loaded.mode == mode, f"Expected mode={mode!r}, got {loaded.mode!r}"

        # 6b. Tools match the canonical preset for this mode
        self._assert_tools_match_preset(mode, loaded_config.enabled_tools)

        # 6c. Prompt contains mode-appropriate text
        prompt = loaded_config.system_prompt or ""
        self._assert_prompt_text(mode, prompt)

        # 7. No api_key leaked into the persisted JSON
        raw = json_path.read_text()
        assert "api_key" not in raw, (
            "api_key found in persisted session JSON"
        )
