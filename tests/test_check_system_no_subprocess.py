# tests/test_check_system_no_subprocess.py
"""RED-first pin for a migration that has NOT been done yet (subprocess removal).

``CheckSystem._query_event_log`` (``tools/workspace/check_system.py:1112-1118``)
currently shells out with a function-local ``import subprocess`` and
``subprocess.run(["tail", "-n", "500", file_path], ...)``.  That call sits
inside the handler's bare ``except Exception``, so on any host without a
``tail`` binary the query silently degrades to ``[event_log] Error: ...``
instead of an event log.

Migration (NOT DONE yet): delete the subprocess call from that chokepoint and
read the JSONL file directly in ``tools/workspace/check_system.py``.  The
module must end up importing no ``subprocess`` at all, while the rendered
output stays byte-identical (session scoping, body redaction, last-50 slice).

These tests describe the post-migration world, so they FAIL today:

* ``test_check_system_module_imports_no_subprocess`` -- AST scan of the module
  (RED: the handler imports ``subprocess`` inline).
* ``test_event_log_query_serves_the_log_without_any_subprocess`` and
  ``test_event_log_query_keeps_the_last_50_owned_entries`` -- drive the real
  ``execute()`` with every ``subprocess`` entry point poisoned, so "needs a
  host process" is a hard failure rather than a silent degradation.
* ``test_event_log_still_scoped_to_caller_session`` -- one line of parity with
  the exhaustive suite in ``tests/test_event_log_scoped.py``.

All fixtures are tmp_path-backed (``EventLogger.instance()`` is monkeypatched),
so the real vault log is never read or written; no network, no real subprocess.
"""

from __future__ import annotations

import ast
import inspect
import json
import subprocess

import pytest

from agent.logging.event_logger import EventLogger
from tools.workspace import check_system as check_system_module
from tools.workspace.check_system import CheckSystem

ALLOWED_QUERIES = ["event_log"]


# ---------------------------------------------------------------------------
# helpers / fixtures (mirroring tests/test_event_log_scoped.py)
# ---------------------------------------------------------------------------


class _StubLogger:
    """Stand-in for EventLogger: ``_query_event_log`` only reads ``file_path``."""

    def __init__(self, path) -> None:
        self.file_path = str(path)


@pytest.fixture(autouse=True)
def _pin_vault_allowlist():
    """Pin the vault allowlist so the event_log handler is always reached."""
    from unittest.mock import patch

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


@pytest.fixture()
def no_subprocess(monkeypatch):
    """Poison every subprocess entry point for the duration of one test.

    The event_log query reads a local file: it needs no host process, so any
    attempt to spawn one is a failure of the migration, not a fallback.
    """

    def _forbidden(*args, **kwargs):
        raise AssertionError("the event_log query must not use subprocess")

    for name in (
        "run",
        "Popen",
        "call",
        "check_call",
        "check_output",
        "getoutput",
        "getstatusoutput",
    ):
        monkeypatch.setattr(subprocess, name, _forbidden)
    return _forbidden


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


def _subprocess_imports(module) -> list:
    """Every ``subprocess`` import found in *module*'s source (comments ignored)."""
    tree = ast.parse(inspect.getsource(module))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(
                alias.name for alias in node.names if alias.name.split(".")[0] == "subprocess"
            )
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "subprocess":
                found.append(node.module or "subprocess")
    return found


# ---------------------------------------------------------------------------
# the migration
# ---------------------------------------------------------------------------


def test_check_system_module_imports_no_subprocess():
    """The check_system module must not reach for a host process at all."""
    assert _subprocess_imports(check_system_module) == []


def test_event_log_query_serves_the_log_without_any_subprocess(
    event_log_file, no_subprocess
):
    """With subprocess unavailable the handler still renders the scoped log."""
    _write(
        event_log_file,
        [
            _event("session_start", "src-alpha-1", {}, "sess-A"),
            _event("session_start", "src-beta-1", {}, "sess-B"),
        ],
    )

    output = _query("sess-A")

    # Today the poisoned subprocess.run is swallowed by the handler's
    # ``except Exception`` and this renders "[event_log] Error: ...".
    assert "Error" not in output
    assert output.startswith("Event log (1 recent entries, scoped to this session):")
    assert f"Path: {event_log_file}" in output
    assert "src-alpha-1" in output
    assert "src-beta-1" not in output


def test_event_log_query_keeps_the_last_50_owned_entries(event_log_file, no_subprocess):
    """Parity with the ``tail`` version: 60 owned events render as the last 50."""
    records = []
    for i in range(60):
        records.append(_event("session_start", f"src-alpha-{i:03d}", {}, "sess-A"))
        records.append(_event("session_start", f"src-beta-{i:03d}", {}, "sess-B"))
    _write(event_log_file, records)

    output = _query("sess-A")

    assert "Error" not in output
    assert "Event log (50 recent entries, scoped to this session):" in output
    assert output.count("  #") == 50
    assert "  #50: [session_start] src-alpha-059" in output
    # The ten oldest owned records fall outside the 50-entry window.
    assert "src-alpha-010" in output
    assert "src-alpha-009" not in output
    assert "src-alpha-000" not in output
    # A neighbour session never fills the window.
    assert "src-beta-059" not in output


def test_event_log_still_scoped_to_caller_session(event_log_file):
    _write(
        event_log_file,
        [
            _event("session_start", "src-alpha-1", {}, "sess-A"),
            _event("session_start", "src-beta-1", {}, "sess-B"),
        ],
    )
    # Deliberate one-line duplication: tests/test_event_log_scoped.py owns the
    # exhaustive scoping/redaction matrix; this only proves the subprocess-free
    # rewrite in check_system.py does not lose it.
    assert "src-alpha-1" not in _query("sess-B")
