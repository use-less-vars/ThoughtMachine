"""Tests for ``SessionManager._normalize_for_frontend`` via the pagination API.

``web_ui/backend/session_manager.py`` carries a *copy* of the bridge's
``_normalize_for_frontend`` helper (kept local so ``SessionManager`` need not
import the bridge module).  It had the SAME defect: the gate at the top of the
``tool_calls`` branch dropped a *tool-only* assistant turn (empty ``content``
and work living entirely in the top-level ``tool_calls`` list)::

    if role == "assistant" and msg.get("tool_calls"):
        assistant_msg = {k: v for k, v in msg.items() if k != "tool_calls"}
        if assistant_msg.get("content"):     # <-- dropped tool-only turns
            normalized.append(assistant_msg)
        ...

The one-line mirror applied to ``bridge.py`` is reproduced here verbatim::

    if assistant_msg.get("content") or msg.get("tool_calls"):

These tests deliberately drive the two PUBLIC frontend-facing entry points
that route through the helper -- ``get_conversation`` and
``load_more_messages`` -- rather than calling the private staticmethod
directly, so the fix is proven at the surface the WebSocket/REST handlers
actually use.

The sibling guard (``if isinstance(content, str) and content.strip() == "":
continue``) is untouched by the mirror; a genuinely-empty assistant turn
(no content *and* no tool_calls) must still be dropped.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from session.models import Session
from session.store import FileSystemSessionStore

from web_ui.backend.config_manager import ConfigManager
from web_ui.backend.session_manager import SessionManager


def _make_manager(tmp_path):
    store = FileSystemSessionStore(
        sessions_dir=str(tmp_path / "sessions"),
        state_dir=str(tmp_path / "state"),
    )
    return SessionManager(session_store=store, config_manager=ConfigManager())


def _tool_only_history():
    """A user turn followed by a tool-only assistant turn."""
    return [
        {"role": "user", "content": "please run the tool"},
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
        },
    ]


def _roles(messages):
    return [m.get("role") for m in messages]


def test_get_conversation_keeps_tool_only_assistant(tmp_path):
    """``get_conversation`` must not drop a tool-only assistant turn."""
    manager = _make_manager(tmp_path)
    session = Session(user_history=_tool_only_history())

    out = manager.get_conversation(session)

    assert out is not None
    assistants = [m for m in out if m.get("role") == "assistant"]
    assert len(assistants) == 1, (
        "tool-only assistant message was dropped by get_conversation; got "
        f"roles={_roles(out)}"
    )
    assert any(m.get("role") == "tool_call" for m in out), (
        f"tool_call entry missing; got roles={_roles(out)}"
    )


def test_load_more_messages_keeps_tool_only_assistant(tmp_path):
    """``load_more_messages`` must not drop a tool-only assistant turn."""
    manager = _make_manager(tmp_path)
    session = Session(user_history=_tool_only_history())

    page = manager.load_more_messages(session, offset=0, limit=50)

    assert page is not None
    assert page["type"] == "more_messages"
    assert page["total_count"] == 2
    assistants = [m for m in page["messages"] if m.get("role") == "assistant"]
    assert len(assistants) == 1, (
        "tool-only assistant message was dropped by load_more_messages; got "
        f"roles={_roles(page['messages'])}"
    )
    assert any(m.get("role") == "tool_call" for m in page["messages"]), (
        f"tool_call entry missing; got roles={_roles(page['messages'])}"
    )


def test_get_conversation_drops_genuinely_empty_assistant(tmp_path):
    """Empty content *and* no tool_calls is still dropped (no over-narrowing)."""
    manager = _make_manager(tmp_path)
    session = Session(
        user_history=[
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": ""},
            {"role": "assistant", "content": "", "tool_calls": []},
        ]
    )

    out = manager.get_conversation(session)

    assert out is not None
    assert all(m.get("role") != "assistant" for m in out), (
        f"genuinely-empty assistant message survived: {out!r}"
    )
