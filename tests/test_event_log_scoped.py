"""Session scoping + body redaction for ``CheckSystem(query="event_log")`` (F17).

The EventLogger JSONL file is ONE vault-global file shared by every session, so
``_query_event_log`` may never serve it unfiltered: only records attributable to
the calling session are shown, and message bodies are redacted at render time.

Every test here drives the real ``execute()`` path against a throw-away JSONL
file (``EventLogger.instance()`` is monkeypatched), so the real vault log is
never read or written.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from agent.logging.event_logger import EventLogger
from tools.workspace.check_system import CheckSystem

ALLOWED_QUERIES = ["event_log"]


class _StubLogger:
    """Stand-in for EventLogger: ``_query_event_log`` only reads ``file_path``."""

    def __init__(self, path) -> None:
        self.file_path = str(path)


@pytest.fixture(autouse=True)
def _pin_vault_allowlist():
    """Pin the vault allowlist so the handler is always reached (mirrors tests/tools/test_workspace_tools.py)."""
    with patch.object(
        CheckSystem, "_load_allowlist_from_vault", return_value=ALLOWED_QUERIES
    ):
        yield


@pytest.fixture()
def event_log_file(tmp_path, monkeypatch):
    """A tmp_path-backed JSONL log with EventLogger.instance() redirected to it."""
    path = tmp_path / "event_log.jsonl"
    path.write_text("", encoding="utf-8")
    stub = _StubLogger(path)
    monkeypatch.setattr(EventLogger, "instance", classmethod(lambda cls: stub))
    return path


def _event(event_type, source, data, session_id=None):
    """One event_log.jsonl record in the shape EventLogger writes."""
    record = {
        "timestamp": "2024-01-01T00:00:00",
        "event_type": event_type,
        "event_id": "evt",
        "source": source,
        "data": data,
    }
    if session_id is not None:
        record["session_id"] = session_id
    return record


def _write(path, records):
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    return path


def _query(session_id):
    """Run the real event_log query; execute() returns the handler string verbatim."""
    tool = CheckSystem(
        query="event_log",
        session_id=session_id,
        session_permissions={"filesystem": "read"},
    )
    return tool.execute()


def test_event_log_scoped_to_caller_session(event_log_file):
    _write(
        event_log_file,
        [
            _event("session_start", "src-alpha-1", {}, "sess-A"),
            _event("session_start", "src-beta-1", {}, "sess-B"),
            _event("session_start", "src-alpha-2", {}, "sess-A"),
            _event("session_start", "src-beta-2", {}, "sess-B"),
        ],
    )

    output = _query("sess-A")

    assert "src-alpha-1" in output
    assert "src-alpha-2" in output
    assert "src-beta-1" not in output
    assert "src-beta-2" not in output


def test_event_log_fails_closed_without_session_scope(event_log_file):
    _write(
        event_log_file,
        [
            _event("session_start", "src-alpha-1", {}, "sess-A"),
            _event("session_start", "src-beta-1", {}, "sess-B"),
        ],
    )

    output = _query(None)

    assert "No session scope" in output
    assert "src-alpha-1" not in output
    assert "src-beta-1" not in output


def test_event_log_matches_conversation_id_attribution(event_log_file):
    _write(
        event_log_file,
        [
            # Real shape of user_query / turn / tool_result: no top-level
            # session_id, attribution lives in data["conversation_id"].
            _event(
                "user_query",
                "src-alpha-conv",
                {"conversation_id": "sess-A", "content": "hello there"},
            ),
            # Real shape of container_lifecycle: attribution in data["session_id"].
            _event(
                "container_lifecycle",
                "src-alpha-lifecycle",
                {"session_id": "sess-A", "action": "destroy"},
            ),
            _event(
                "user_query",
                "src-beta-conv",
                {"conversation_id": "sess-B", "content": "not mine"},
            ),
        ],
    )

    output = _query("sess-A")

    assert "src-alpha-conv" in output
    assert "src-alpha-lifecycle" in output
    assert "src-beta-conv" not in output


def test_event_log_excludes_unattributed_events(event_log_file):
    _write(
        event_log_file,
        [
            _event("session_start", "src-orphan", {"note": "no owner anywhere"}),
            _event("session_start", "src-alpha-1", {}, "sess-A"),
        ],
    )

    for session_id in ("sess-A", "sess-B"):
        output = _query(session_id)
        assert "src-orphan" not in output

    # And an unattributed record must not even be counted for a stranger.
    assert "Event log (0 recent entries, scoped to this session):" in _query("sess-B")


def test_event_log_redacts_message_bodies(event_log_file):
    secret = "SECRET-c0ffee-DO-NOT-LEAK"
    marker = f"[content: {len(secret)} chars, type: str]"
    _write(
        event_log_file,
        [
            _event(
                "user_query",
                "src-uq",
                {"conversation_id": "sess-A", "content": secret},
            ),
            _event(
                "turn",
                "src-turn",
                {"conversation_id": "sess-A", "assistant_content": secret},
            ),
            _event(
                "tool_result",
                "src-tool",
                {"conversation_id": "sess-A", "result": secret},
            ),
        ],
    )

    output = _query("sess-A")

    assert secret not in output
    assert output.count(marker) == 3


def test_event_log_redacts_tool_calls(event_log_file):
    _write(
        event_log_file,
        [
            _event(
                "turn",
                "src-calls",
                {
                    "conversation_id": "sess-A",
                    "tool_calls": [
                        {"name": "payload-tool", "arguments": "payload-args"},
                        {"name": "second-tool", "arguments": "more-payload"},
                    ],
                },
            ),
        ],
    )

    output = _query("sess-A")

    assert "[tool_calls: 2 items]" in output
    assert "payload-tool" not in output
    assert "payload-args" not in output
    assert "more-payload" not in output


def test_event_log_preserves_container_lifecycle_payload(event_log_file):
    _write(
        event_log_file,
        [
            _event(
                "container_lifecycle",
                "src-lifecycle",
                {
                    "action": "destroy",
                    "reason": "operator requested teardown",
                    "workspace_id": "ws-42",
                    "session_id": "sess-A",
                    "contributing_source": "landing_page",
                },
            ),
        ],
    )

    output = _query("sess-A")

    # The destroy/restart audit record is policy metadata, never redacted.
    assert "destroy" in output
    assert "operator requested teardown" in output
    assert "ws-42" in output
    assert "sess-A" in output
    assert "landing_page" in output


def test_event_log_shows_last_50_after_filtering(event_log_file):
    records = []
    for i in range(60):
        records.append(
            _event("session_start", "src-a", {"marker": f"aid-{i:02d}"}, "sess-A")
        )
        if i % 12 == 0:
            records.append(
                _event("session_start", "src-b", {"marker": f"bid-{i:02d}"}, "sess-B")
            )
    _write(event_log_file, records)

    output = _query("sess-A")

    # Filtering happens BEFORE the 50-entry cap: 60 owned events -> the last 50.
    assert "Event log (50 recent entries, scoped to this session):" in output
    assert "bid-" not in output
    assert "aid-10" in output
    assert "aid-09" not in output
    assert "aid-59" in output


def test_event_log_tolerates_non_string_event_type(event_log_file):
    """A malformed (non-string) ``event_type`` must not abort the whole query."""
    _write(
        event_log_file,
        [
            _event(["user_query"], "src-bad-type-1", {"content": "x"}, "sess-A"),
            _event("session_start", "src-good-type-1", {"marker": "ok-1"}, "sess-A"),
        ],
    )

    output = _query("sess-A")

    assert "src-good-type-1" in output
    assert "scoped to this session" in output
    assert "[event_log] Error:" not in output

