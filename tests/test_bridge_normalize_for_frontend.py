"""Tests for ``WebAgentBridge._normalize_for_frontend`` tool-only turns.

``web_ui/backend/bridge.py`` drops an assistant message whose ``content`` is
empty, via the gate at the top of the ``tool_calls`` branch::

    if role == "assistant" and msg.get("tool_calls"):
        assistant_msg = {k: v for k, v in msg.items() if k != "tool_calls"}
        if assistant_msg.get("content"):     # <-- drops tool-only turns
            normalized.append(assistant_msg)
        ...

A *tool-only* turn (assistant message whose ``content`` is empty and whose
work lives entirely in the top-level ``tool_calls`` list) therefore had its
assistant placeholder silently dropped.  The fix narrows the gate to

    if assistant_msg.get("content") or msg.get("tool_calls"):

so a tool-only turn survives, while a *genuinely* empty assistant turn
(no content and no tool calls) is still dropped by the sibling branch that
guards the plain-string path (``if isinstance(content, str) and
content.strip() == "": continue``) -- that sibling guard is untouched.

``_normalize_for_frontend`` is a ``@staticmethod`` on ``WebAgentBridge``; the
call form used below is ``WebAgentBridge._normalize_for_frontend(messages)``.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web_ui.backend.bridge import WebAgentBridge


def _roles(messages):
    return [m.get("role") for m in messages]


def test_tool_only_assistant_message_survives():
    """An assistant message carrying only tool_calls must not be dropped."""
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "Respond",
                        "arguments": "{\"message\": \"hi\"}",
                    },
                }
            ],
        }
    ]

    out = WebAgentBridge._normalize_for_frontend(messages)

    assistants = [m for m in out if m.get("role") == "assistant"]
    assert len(assistants) == 1, (
        "tool-only assistant message was dropped; got roles="
        f"{_roles(out)}"
    )
    # The tool call itself must still be surfaced as a tool_call entry.
    assert any(m.get("role") == "tool_call" for m in out), (
        f"tool_call entry missing; got roles={_roles(out)}"
    )


def test_genuinely_empty_assistant_message_is_dropped():
    """Empty content *and* no tool_calls must still be dropped (no over-narrowing)."""
    for msg in (
        {"role": "assistant", "content": ""},
        {"role": "assistant", "content": "", "tool_calls": []},
    ):
        out = WebAgentBridge._normalize_for_frontend([dict(msg)])
        assert all(m.get("role") != "assistant" for m in out), (
            f"genuinely-empty assistant message survived: {out!r}"
        )
