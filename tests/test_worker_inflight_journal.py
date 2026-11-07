"""S7b-2b: the inflight journal's WRITE POINTS in the worker's tool loop.

What is pinned here
-------------------
``tools/workspace/inflight_journal.py`` is the primitive (atomic, fenced,
never-raising).  THIS module pins where ``WorkerThread._run_tool_loop`` calls
it, and what state the journal is in at each of those moments:

* WRITE POINT 1 -- turn start: an ``inflight`` record exists before the turn
  runs, carrying the query text and ``attempts=0``.
* WRITE POINT 2 -- first tool call: the record's ``last_tool`` becomes
  ``{"name": <tool>, "args_hash": sha256(json.dumps(args, sort_keys=True))}``
  (R2's tool identity), keeping the SAME ``query_id`` and ``created_at``.
* WRITE POINT 3 -- terminal success: the record is GONE.  Nothing is left to
  recover once the turn's work has been delivered.
* WRITE POINT 4 -- DETACH park: the record is flipped to ``recoverable`` with a
  ``reason`` (the turn's work was thrown away), and the in-flight JOB is
  terminalized ``interrupted`` so a join can never complete or restart it.
* WRITE POINT 5 -- PAUSE park: NOTHING.  A pause is cooperative: the turn is
  drained, its answer delivered, and the record cleared like any successful
  turn.  A pause that wrote ``recoverable`` would make a successor replay work
  the user already received.

Failure policy, also pinned: the journal is best-effort.  A write that raises
or is refused must not fail, delay or alter the turn.

Determinism
-----------
A journal is written AND cleared inside one turn, so it can only be observed
from inside the turn: the scripted agent snapshots it between two events
(``probes``).  The park is raised from inside the event generator as well, so
it lands at the same point in the turn on every run -- no sleeps, no thread
races.  Construction is a REAL ``WorkerThread`` that is never ``start()``ed,
the same ``_unstarted_thread`` pattern as
``tests/test_worker_pause_midturn_cooperative.py``.  No Docker, no network.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.workspace import worker_thread as worker_thread_module
from tools.workspace.inflight_journal import (
    STATUS_INFLIGHT,
    STATUS_RECOVERABLE,
    journal_path,
    read_inflight,
)
from tools.workspace.worker_thread import WorkerThread

#: The answer the turn under way produces.  Distinctive so a failure names
#: exactly what was lost.
WORK = "THE TURN'S OWN ANSWER, delivered."


def _unstarted_thread(tmp_path) -> WorkerThread:
    """A REAL ``WorkerThread`` that is never ``start()``ed."""
    return WorkerThread(
        name="test-worker-journal",
        definition={"name": "test-worker-journal",
                    "system_prompt": "You are a test worker."},
        agent_config={},
        workspace_dir=Path(tmp_path),
        session_id="sess-inflight-journal",
        container_manager=None,
        max_container_count=4,
        max_token_usage=None,
        max_runtime_s=None,
    )


def _snapshot(thread: WorkerThread):
    """The journal as THIS incarnation sees it: ``(record, reason)``."""
    return read_inflight(thread._worker_dir, thread._fence())


def _args_hash(args) -> str:
    """The ``args_hash`` the journal is expected to carry."""
    return hashlib.sha256(
        json.dumps(args, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


class _ScriptedAgent:
    """Agent stand-in that snapshots the journal between two known events.

    ``process_query`` is the production event-generator seam; the loop drains
    it event by event.  Between the events the generator calls ``probe()`` --
    the only way to see a journal that this same turn writes and clears -- and
    optionally raises the production park signal, exactly like the pause/detach
    pins in ``tests/test_worker_pause_midturn_cooperative.py``.
    """

    def __init__(self, thread: WorkerThread, probe, *, park=None, tool_call=None):
        self._thread = thread
        self._probe = lambda: probe(thread)
        self._park = park
        self._tool_call = tool_call
        self.probes: list = []
        self.parked_mid_turn = False

    # ── the reading-mode seam pause() drives ────────────────────────────────
    def enter_reading_mode(self, reason: str = "pause") -> None:
        pass

    def request_pause(self) -> None:
        self.enter_reading_mode("pause")

    # ── the turn ───────────────────────────────────────────────────────────
    def process_query(self, query: str):
        yield {"type": "assistant_message", "content": "working on it..."}
        # Journal as of turn start (before any tool call).
        self.probes.append(self._probe())

        if self._tool_call is not None:
            yield {"type": "tool_call", **self._tool_call}
            # Journal after the tool call was journalled (write point 2).
            self.probes.append(self._probe())

        if self._park is not None:
            # Park HERE: the turn is under way and the concluding event has not
            # been yielded yet, so a non-cooperative park must abandon it.
            self._park()
            self.parked_mid_turn = True

        yield {
            "type": "agent_responded",
            "content": WORK,
            "status": "success",
            "confidence": "high",
        }


class _NullStateBridge:
    """Keeps the loop's presenter lazy-init out of these tests."""

    context_length = 0


def _wire(thread: WorkerThread) -> WorkerThread:
    """Install the null presenter; leave every other seam alone."""
    thread._state_bridge = _NullStateBridge()
    thread._publish_event = lambda etype, data: None
    return thread


class _FakeRegistry:
    """Records ``terminalize`` calls instead of touching the real singleton."""

    def __init__(self) -> None:
        self.calls: list = []

    def terminalize(self, job_id, status="paused", envelope=None):
        self.calls.append((job_id, status, dict(envelope or {})))
        return {"job_id": job_id, "status": status}


@pytest.fixture
def registry(monkeypatch) -> _FakeRegistry:
    """A job registry that never leaves the test process."""
    fake = _FakeRegistry()
    monkeypatch.setattr(
        worker_thread_module, "_get_worker_job_registry", lambda: fake
    )
    return fake


# ── WRITE POINT 1: turn start ───────────────────────────────────────────────


def test_turn_start_journals_an_inflight_record(tmp_path, registry):
    """An ``inflight`` record exists while the turn runs."""
    thread = _wire(_unstarted_thread(tmp_path))
    agent = _ScriptedAgent(thread, _snapshot)
    thread._agent = agent

    reply = thread._run_tool_loop("do the thing")

    # The turn itself is unaffected.
    assert reply == WORK

    record, reason = agent.probes[0]
    assert reason is None, reason
    assert record is not None, "no inflight record was journalled at turn start"
    assert record.status == STATUS_INFLIGHT
    assert record.query == "do the thing"
    assert record.attempts == 0
    assert record.last_tool is None
    assert isinstance(record.query_id, str) and record.query_id
    assert isinstance(record.requested_by, str) and record.requested_by


def test_the_journal_follows_the_current_query_id(tmp_path, registry):
    """When the turn carries a query id, that id is the journalled one."""
    thread = _wire(_unstarted_thread(tmp_path))
    thread._current_query_id = "q-correlated"
    agent = _ScriptedAgent(thread, _snapshot)
    thread._agent = agent

    thread._run_tool_loop("do the thing")

    record, reason = agent.probes[0]
    assert reason is None, reason
    assert record is not None
    assert record.query_id == "q-correlated"


# ── WRITE POINT 2: tool calls ───────────────────────────────────────────────


def test_a_tool_call_records_last_tool(tmp_path, registry):
    """The record's ``last_tool`` becomes R2's ``{name, args_hash}``."""
    args = {"path": "notes.txt", "limit": 5}
    thread = _wire(_unstarted_thread(tmp_path))
    agent = _ScriptedAgent(
        thread, _snapshot,
        tool_call={"name": "read_file", "arguments": args},
    )
    thread._agent = agent

    reply = thread._run_tool_loop("do the thing")

    assert reply == WORK
    before, after = agent.probes[0], agent.probes[1]
    assert before[0] is not None and before[0].last_tool is None, before
    record = after[0]
    assert record is not None, after[1]
    assert record.last_tool == {"name": "read_file", "args_hash": _args_hash(args)}
    # An update, not a new turn: identity and status are carried over.
    assert record.status == STATUS_INFLIGHT
    assert record.query_id == before[0].query_id
    assert record.created_at == before[0].created_at
    assert record.attempts == 0


# ── WRITE POINT 3: terminal success ─────────────────────────────────────────


def test_terminal_success_clears_the_record(tmp_path, registry):
    """A completed turn leaves NOTHING to recover."""
    thread = _wire(_unstarted_thread(tmp_path))
    agent = _ScriptedAgent(thread, _snapshot)
    thread._agent = agent

    reply = thread._run_tool_loop("do the thing")

    assert reply == WORK
    assert agent.probes[0][0] is not None, "the record was never written"
    assert journal_path(thread._worker_dir).exists() is False, (
        "the inflight record survived a completed turn: a successor would "
        "replay work the caller already received"
    )
    assert _snapshot(thread) == (None, None)


# ── WRITE POINT 4: the DETACH park ──────────────────────────────────────────


def test_detach_park_flips_the_record_to_recoverable(tmp_path, registry):
    """DETACH abandons the turn: keep it recoverable and interrupt the job."""
    thread = _wire(_unstarted_thread(tmp_path))
    thread._current_query_id = "q-detach"
    agent = _ScriptedAgent(
        thread, _snapshot,
        park=lambda: (setattr(thread, "_detach_requested", True),
                      thread._park_event.set()),
    )
    thread._agent = agent

    reply = thread._run_tool_loop("do the thing")

    # The park itself is unchanged: detach still truncates the turn.
    assert agent.parked_mid_turn is True
    assert json.loads(reply) == {
        "status": "paused",
        "message": "Worker paused by user",
    }, reply
    assert agent.probes[0][0].status == STATUS_INFLIGHT, (
        "the flip must be the PARK's doing, not the write point's"
    )

    record, reason = _snapshot(thread)
    assert reason is None, reason
    assert record is not None, (
        "the detach park cleared the record: the abandoned turn is unrecoverable"
    )
    assert record.status == STATUS_RECOVERABLE
    assert record.query_id == "q-detach"
    assert isinstance(record.reason, str) and record.reason

    # ...and the job it belonged to is terminal, with the same reason.
    assert len(registry.calls) == 1, registry.calls
    job_id, status, envelope = registry.calls[0]
    assert job_id == "q-detach"
    assert status == "interrupted"
    assert envelope.get("reason") == record.reason, envelope


# ── WRITE POINT 5: the PAUSE park writes NOTHING ────────────────────────────


def test_pause_park_does_not_flip_the_record(tmp_path, registry):
    """A cooperative pause is NOT a recoverable turn."""
    thread = _wire(_unstarted_thread(tmp_path))
    thread._current_query_id = "q-pause"
    agent = _ScriptedAgent(thread, _snapshot, park=thread.pause)
    thread._agent = agent

    reply = thread._run_tool_loop("do the thing")

    assert agent.parked_mid_turn is True
    assert reply == WORK, "the pause must still deliver the turn's answer"
    assert [record.status for record, _ in agent.probes if record] == [
        STATUS_INFLIGHT
    ], "a pause must never journal a recoverable record"
    assert journal_path(thread._worker_dir).exists() is False
    assert registry.calls == [], (
        f"a pause must not terminalize the job as interrupted: {registry.calls}"
    )


# ── the failure policy: the journal is best-effort ──────────────────────────


def test_a_raising_journal_never_breaks_the_turn(tmp_path, registry, monkeypatch):
    """A journal that raises must not fail (or alter) the turn."""
    def _boom(*args, **kwargs):
        raise RuntimeError("journal is on fire")

    monkeypatch.setattr(worker_thread_module, "write_inflight", _boom)
    monkeypatch.setattr(worker_thread_module, "clear_inflight", _boom)

    thread = _wire(_unstarted_thread(tmp_path))
    thread._agent = _ScriptedAgent(thread, _snapshot)

    assert thread._run_tool_loop("do the thing") == WORK


def test_a_refused_journal_write_never_breaks_the_turn(tmp_path, registry,
                                                       monkeypatch):
    """A REFUSED write (foreign record on disk) is carried on without journaling."""
    monkeypatch.setattr(
        worker_thread_module, "write_inflight",
        lambda *a, **k: "refused: record for another incarnation is work in progress",
    )

    thread = _wire(_unstarted_thread(tmp_path))
    thread._agent = _ScriptedAgent(thread, _snapshot)

    assert thread._run_tool_loop("do the thing") == WORK
