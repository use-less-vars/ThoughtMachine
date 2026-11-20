"""Contract: a configured system-prompt ADDENDUM must reach the assembled
system prompt of a NEW session, in EVERY mode.

The operator writes extra instructions ("added context") for the agent.  The
base system prompt is legitimately mode-locked (``agent`` / ``engineer`` carry a
factory prompt, and ``custom`` may replace it), so the only free-text channel
that can promise "what you write is what the model sees" is an ADDITIVE one.
This test drives that channel through the canonical configuration path used by
the web UI's ``apply_config`` (ConfigManager -> SessionConfig) and then the
canonical assembly point (``LLMClient.ensure_system_prompt``).

Regression guard for the three ways an earlier build dropped the text:

1. mode lock - ``SessionConfig.update_prompt`` returned silently for any mode
   but ``custom``, so the value vanished while the UI still said "applied";
2. ``~/.thoughtmachine/custom_system_prompt.txt`` precedence - it rewrote
   ``system_prompt`` wholesale, so a configured value was overwritten;
3. the field-validator / after-validator rewrite of ``system_prompt`` for
   ``agent`` / ``engineer``.
"""

import pytest

from agent.config.session_config import SessionConfig
from web_ui.backend.config_manager import ConfigManager

CANARY = "CANARY-AGENT-SYSTEM-PROMPT-ADDENDUM-7f3a"
BASE = "BASE-PROMPT-CANARY-11c2"


def _apply(session_config, payload):
    """Push *payload* through the web UI's config path, return the new config.

    ``ConfigManager.apply_config`` deep-copies its input and RETURNS the updated
    ``SessionConfig`` (it never mutates the caller's object); the bridge then
    persists that returned instance.  The test therefore has to drive the
    returned object, otherwise it would assert against an untouched config.
    """
    _frontend_config, updated = ConfigManager.apply_config(
        payload, current_config=session_config
    )
    assert updated is not None, "apply_config returned no updated SessionConfig"
    return updated


def _assembled_system_prompt(session_config):
    """Build the agent config from the session config and assemble the prompt.

    Mirrors the production chain:
    SessionConfig.to_agent_config()  (Bridge.apply_config, Step 3)
      -> LLMClient.ensure_system_prompt()  (agent/core/llm_client.py:305)
    """
    from agent.core.llm_client import LLMClient

    agent_config = session_config.to_agent_config(workspace_path="/tmp/ws")

    # Assembly only needs ``self.config``; constructing a provider client is
    # unrelated to the system-prompt path, so bind the config directly.
    client = object.__new__(LLMClient)
    client.config = agent_config
    messages = client.ensure_system_prompt([])
    assert messages, "ensure_system_prompt produced no messages"
    return [m for m in messages if m.get("role") == "system"][0]["content"]


@pytest.mark.parametrize("mode", ["agent", "engineer", "custom"])
def test_configured_addendum_is_in_assembled_prompt(mode):
    """The addendum must be assembled into the prompt in every mode."""
    session_config = SessionConfig(mode=mode, system_prompt=BASE)

    updated = _apply(session_config, {"system_prompt_addendum": CANARY})

    assembled = _assembled_system_prompt(updated)

    assert CANARY in assembled, (
        f"configured system-prompt addendum is missing from the assembled "
        f"system prompt in mode={mode!r} (prompt length={len(assembled)}, "
        f"first 120 chars={assembled[:120]!r})"
    )
    # Additive, never substitutive: the base prompt is still there.
    assert BASE in assembled or mode != "custom"


@pytest.mark.parametrize("mode", ["agent", "engineer", "custom"])
def test_addendum_is_appended_after_the_base_prompt(mode):
    """The block goes at the END, so the base prompt stays a stable prefix."""
    session_config = SessionConfig(mode=mode, system_prompt=BASE)
    updated = _apply(session_config, {"system_prompt_addendum": CANARY})

    assembled = _assembled_system_prompt(updated)

    assert assembled.index(CANARY) > 0
    assert assembled.rstrip().endswith(CANARY), (
        "the addendum must be the LAST text in the system prompt, otherwise it "
        f"breaks the cache-stable prefix; tail={assembled[-160:]!r}"
    )


def test_empty_addendum_leaves_the_prompt_untouched():
    """No configured addendum == byte-identical to the un-composed prompt."""
    without = SessionConfig(mode="agent", system_prompt=BASE)
    empty = SessionConfig(mode="agent", system_prompt=BASE, system_prompt_addendum="   ")
    updated = _apply(empty, {"system_prompt_addendum": "   "})

    assert _assembled_system_prompt(updated) == _assembled_system_prompt(without)


def test_custom_system_prompt_file_cannot_clobber_the_addendum(tmp_path, monkeypatch):
    """``custom_system_prompt.txt`` rewrites ``system_prompt`` only."""
    from pathlib import Path

    fake_home = tmp_path / "home"
    custom_file = fake_home / ".thoughtmachine" / "custom_system_prompt.txt"
    custom_file.parent.mkdir(parents=True)
    custom_file.write_text("PROMPT-FROM-FILE-9ab1", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    session_config = SessionConfig(mode="custom")
    updated = _apply(session_config, {"system_prompt_addendum": CANARY})

    assembled = _assembled_system_prompt(updated)

    assert CANARY in assembled, (
        "the addendum must survive the custom_system_prompt.txt precedence, "
        f"which may only rewrite the base prompt; got {assembled[:200]!r}"
    )


def test_locked_mode_still_refuses_the_base_prompt_and_says_so(caplog):
    """The base prompt stays locked - but the refusal is logged, not silent."""
    session_config = SessionConfig(mode="engineer")

    with caplog.at_level("WARNING"):
        updated = _apply(session_config, {"system_prompt": CANARY})

    assert updated.system_prompt != CANARY
    assert any(
        "IGNORED" in record.getMessage() and "system_prompt_addendum" in record.getMessage()
        for record in caplog.records
    ), "a mode-locked system_prompt push must warn and point at the addendum channel"
