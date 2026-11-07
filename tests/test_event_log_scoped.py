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


def _query_with_workspace(session_id, workspace_id):
    """Same query, with plausible workspace context supplied by the caller."""
    tool = CheckSystem(
        query="event_log",
        session_id=session_id,
        workspace_id=workspace_id,
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


def test_event_log_survives_more_than_500_neighbour_records(event_log_file):
    """R4: neighbour traffic must not starve the caller out of the view.

    The window used to be the last 500 *lines* of the shared file, taken before
    the ownership filter.  600 neighbour records between two of this session's
    therefore pushed both of them out of the window and the query truthfully
    reported an empty view of the caller's own log.  Filtering while scanning
    backwards spends the window on the caller's records first.
    """
    records = [_event("session_start", "src-a-old", {"marker": "mine-old"}, "sess-A")]
    for i in range(600):
        records.append(_event("session_start", f"src-b-{i:03d}", {}, "sess-B"))
    records.append(
        _event("session_start", "src-a-new", {"marker": "mine-new"}, "sess-A")
    )
    _write(event_log_file, records)

    output = _query("sess-A")

    assert "Event log (2 recent entries, scoped to this session):" in output
    assert "src-a-old" in output
    assert "src-a-new" in output
    assert "src-b-" not in output


def test_event_log_reads_past_a_neighbour_block_for_a_full_page(event_log_file):
    """A whole page (50) survives 500+ neighbour records written after it."""
    records = [
        _event("session_start", f"src-a-{i:02d}", {"marker": f"mine-{i:02d}"}, "sess-A")
        for i in range(50)
    ]
    records.extend(
        _event("session_start", f"src-b-{i:03d}", {}, "sess-B") for i in range(700)
    )
    _write(event_log_file, records)

    output = _query("sess-A")

    assert "Event log (50 recent entries, scoped to this session):" in output
    assert "src-a-00" in output
    assert "src-a-49" in output
    assert "src-b-" not in output


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



# -- Session/workspace conflation (W3-T7) ------------------------------------
#
# Scoping is by SESSION, never by workspace: many sessions share one workspace,
# so a workspace-wide match would hand every caller its neighbours' records.
# These two tests fail if the session scope is ever widened to the workspace.
# (The third case the task names -- an unscoped caller getting nothing -- is
# already pinned by test_event_log_fails_closed_without_session_scope above.)


def test_event_log_never_shows_a_same_workspace_neighbour(event_log_file):
    """Two sessions in the SAME workspace must not see each other's records."""
    _write(
        event_log_file,
        [
            _event(
                "tool_result",
                "src-mine",
                {"workspace_id": "ws-shared", "marker": "mine-1"},
                "sess-A",
            ),
            _event(
                "tool_result",
                "src-neighbour",
                {"workspace_id": "ws-shared", "marker": "neighbour-1"},
                "sess-B",
            ),
        ],
    )

    # The caller's workspace context IS the neighbour's workspace.
    output = _query_with_workspace("sess-A", "ws-shared")

    assert "src-mine" in output
    assert "src-neighbour" not in output
    assert "neighbour-1" not in output


def test_event_log_never_shows_a_foreign_workspace_record(event_log_file):
    """A record owned by a session in another workspace never appears.

    Both when the caller supplies its own workspace and when it supplies the
    foreign workspace id: workspace context is not a scope.
    """
    _write(
        event_log_file,
        [
            _event(
                "tool_result",
                "src-mine",
                {"workspace_id": "ws-mine", "marker": "mine-1"},
                "sess-A",
            ),
            _event(
                "tool_result",
                "src-foreign",
                {"workspace_id": "ws-secret", "marker": "foreign-1"},
                "sess-Z",
            ),
        ],
    )

    for claimed_workspace in ("ws-mine", "ws-secret"):
        output = _query_with_workspace("sess-A", claimed_workspace)
        assert "src-mine" in output, claimed_workspace
        assert "src-foreign" not in output, claimed_workspace
        assert "foreign-1" not in output, claimed_workspace
