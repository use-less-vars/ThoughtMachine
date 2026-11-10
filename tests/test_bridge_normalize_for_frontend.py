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


# ── C28: tool_call_id propagation out of both normalizer copies ──────────────

import copy as _copy

import pytest

from web_ui.backend.session_manager import SessionManager

_MISSING = object()

NORMALIZERS = [
    pytest.param(WebAgentBridge._normalize_for_frontend, id="bridge"),
    pytest.param(SessionManager._normalize_for_frontend, id="session_manager"),
]


def _tc(call_id=_MISSING, name="Read", args='{"path": "a.txt"}'):
    call = {"type": "function", "function": {"name": name, "arguments": args}}
    if call_id is not _MISSING:
        call["id"] = call_id
    return call


def _by_role(messages, role):
    return [m for m in messages if m.get("role") == role]


@pytest.mark.parametrize("normalize", NORMALIZERS)
def test_structured_tool_calls_each_keep_their_real_id(normalize):
    out = normalize([
        {"role": "assistant", "content": "", "tool_calls": [_tc("call_a"), _tc("call_b")]},
    ])
    calls = _by_role(out, "tool_call")
    assert len(calls) == 2
    assert [m["tool_call_id"] for m in calls] == ["call_a", "call_b"]


@pytest.mark.parametrize("normalize", NORMALIZERS)
def test_tool_result_carries_the_same_id_as_its_call(normalize):
    out = normalize([
        {"role": "assistant", "content": "", "tool_calls": [_tc("call_x")]},
        {"role": "tool", "tool_call_id": "call_x", "content": "done"},
    ])
    call = _by_role(out, "tool_call")[0]
    result = _by_role(out, "tool_result")[0]
    assert call["tool_call_id"] == "call_x"
    assert result["tool_call_id"] == "call_x"


@pytest.mark.parametrize("normalize", NORMALIZERS)
def test_identical_duplicate_calls_get_distinct_ids(normalize):
    out = normalize([
        {"role": "assistant", "content": "", "tool_calls": [_tc(), _tc(), _tc()]},
    ])
    ids = [m["tool_call_id"] for m in _by_role(out, "tool_call")]
    assert len(ids) == 3
    assert len(set(ids)) == 3
    assert all(i.startswith("legacy:") for i in ids)


@pytest.mark.parametrize("normalize", NORMALIZERS)
@pytest.mark.parametrize("blank", [_MISSING, None, ""])
def test_missing_id_falls_back_to_unique_synthetic_id(normalize, blank):
    out = normalize([
        {"role": "assistant", "content": "", "tool_calls": [_tc(blank), _tc(blank)]},
    ])
    ids = [m["tool_call_id"] for m in _by_role(out, "tool_call")]
    assert len(ids) == 2
    assert ids[0] != ids[1]
    assert all(i.startswith("legacy:") for i in ids)


@pytest.mark.parametrize("normalize", NORMALIZERS)
def test_tool_result_without_id_gets_synthetic_id(normalize):
    out = normalize([{"role": "tool", "content": "orphan"}])
    result = _by_role(out, "tool_result")[0]
    assert result["tool_call_id"].startswith("legacy:")


@pytest.mark.parametrize("normalize", NORMALIZERS)
def test_legacy_text_tool_call_gets_stable_synthetic_id(normalize):
    history = [{"role": "assistant", "content": "[Tool call: Read({'path': 'a.txt'})]"}]
    first = [m["tool_call_id"] for m in _by_role(normalize(_copy.deepcopy(history)), "tool_call")]
    second = [m["tool_call_id"] for m in _by_role(normalize(_copy.deepcopy(history)), "tool_call")]
    assert len(first) == 1
    assert first[0].startswith("legacy:")
    assert first == second


def test_both_normalizers_agree_on_tool_call_ids():
    history = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [_tc("call_a"), _tc()]},
        {"role": "tool", "tool_call_id": "call_a", "content": "ok"},
        {"role": "tool", "content": "orphan"},
        {"role": "assistant", "content": "[Tool call: Read({'path': 'a.txt'})]"},
    ]
    bridge_out = WebAgentBridge._normalize_for_frontend(_copy.deepcopy(history))
    sm_out = SessionManager._normalize_for_frontend(_copy.deepcopy(history))
    assert bridge_out == sm_out

