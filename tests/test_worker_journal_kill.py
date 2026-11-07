"""S7b-3 (Option K): what a DELIBERATE kill does to the inflight journal.

What is pinned here
-------------------
``WorkerThread.kill`` is the ONLY termination path (``stop`` detaches), and it
is called from a crash-shaped and a deliberate context alike.  The contract:

* ``kill()`` -- the plain, keyword-free call every pre-existing call site makes
  -- journals NOTHING.  A hard kill / best-effort stop must not be able to fail
  on, or wait for, bookkeeping, so it keeps its exact previous behaviour.
* ``kill(journal_reason=...)`` -- a DELIBERATE kill (a manager stop, a session
  reset) -- is terminal bookkeeping, and says so TWICE, so one grep finds both
  halves:
    - the inflight record is flipped to ``cancelled`` with that ``reason``;
    - the job it belonged to is terminalized ``stopped`` with that ``reason``
      in its envelope.
* The cancel is an UPDATE of the turn that exists: ``query_id``, ``query``,
  ``created_at`` and ``last_tool`` are carried over, ``attempts`` stays 0.  No
  record on disk means nothing to cancel -- a kill must never conjure a phantom
  turn for a successor to find, and it must never erase the record either.
* WRITE POINT 3 is held back for a cancelled record.  ``clear_inflight``
  deletes any record fenced to this incarnation regardless of status, so
  without that guard the loop's own exit would erase the evidence a moment
  after the kill wrote it.  The flag is per turn, so the NEXT turn clears
  exactly as it always did.
* The bookkeeping never gates the kill: a journal that raises, or a write that
  is refused, still lets the worker die -- and a refused write must not hold
  WRITE POINT 3 back (there is nothing on disk to keep).
* A record fenced to ANOTHER incarnation is not this worker's to cancel: the
  kill leaves that file byte-identical and reads it as absent.
* A RESET drops the journal as well as the state files -- a parseable record
  and a corrupt one alike, because ``_drop_persisted_files`` unlinks and never
  parses.  A reset leaves nothing behind at all.

Vocabulary (deliberately two vocabularies, one string)

* JOURNAL status: ``cancelled`` ("the operator killed it; never resurrect it").
* JOB terminal status: ``stopped`` ("a user/operator ended this").  The DETACH
  park keeps its own pair -- ``interrupted`` + ``detached-midturn-park`` -- which
  ``tests/test_worker_inflight_journal.py`` pins; nothing here changes it.

Determinism
-----------
Record-level tests plant the record with the loop's own ``_journal_write``
helper and then call ``kill``/``_journal_cancel`` directly -- no thread is ever
``start()``ed, no sleeps, no races.  The WRITE POINT 3 test raises the cancel
from inside the scripted agent's event generator (the same ``_unstarted_thread``
pattern as ``tests/test_worker_pause_midturn_cooperative.py``), so it lands at
the same point of the turn on every run.  No Docker, no network.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.workspace import worker_thread as worker_thread_module
from tools.workspace.command_channel import CommandFence
from tools.workspace.inflight_journal import (
    STATUS_CANCELLED,
    STATUS_INFLIGHT,
    InflightRecord,
    journal_path,
    read_inflight,
    utcnow_iso,
    write_inflight,
)
from tools.workspace.worker_manager import (
    _MANAGER_STOP_JOURNAL_REASON,
    WorkerManager,
)
from tools.workspace.worker_thread import (
    _JOURNAL_RESET_REASON,
    Worker,
    WorkerThread,
)

#: The answer the turn under way produces.  Distinctive so a failure names
#: exactly what was kept (or lost).
WORK = "THE TURN'S OWN ANSWER, delivered."


def _unstarted_thread(tmp_path) -> WorkerThread:
    """A REAL ``WorkerThread`` that is never ``start()``ed."""
    return WorkerThread(
        name="test-worker-journal-kill",
        definition={"name": "test-worker-journal-kill",
                    "system_prompt": "You are a test worker."},
        agent_config={},
        workspace_dir=Path(tmp_path),
        session_id="sess-journal-kill",
        container_manager=None,
        max_container_count=4,
        max_token_usage=None,
        max_runtime_s=None,
    )


def _snapshot(thread: WorkerThread):
    """The journal as THIS incarnation sees it: ``(record, reason)``."""
    return read_inflight(thread._worker_dir, thread._fence())


def _plant_record(thread: WorkerThread, *, query="do the thing", last_tool=None):
    """Write the record WRITE POINT 1 would have written, and stop there.

    Planted through the loop's OWN helper (``_journal_write``), so the payload,
    the fence and ``attempts=0`` are the production ones.  What these tests pin
    is what a kill does to a record -- the write points themselves live in
    ``tests/test_worker_inflight_journal.py``.
    """
    thread._journal_write(
        query_id=thread._current_query_id or "q-planted",
        query=query,
        status=STATUS_INFLIGHT,
        created_at=utcnow_iso(),
        last_tool=last_tool,
    )
    record, reason = _snapshot(thread)
    assert reason is None, reason
    assert record is not None, "the planted record never reached disk"
    return record


class _ScriptedAgent:
    """Agent stand-in that raises an event between two known events.

    ``process_query`` is the production event-generator seam; the loop drains it
    event by event.  ``mid_turn`` runs from INSIDE the generator -- i.e. while
    the turn is under way and the concluding event has not been yielded -- which
    is the only way to reach the loop's exit with a mid-turn cancel in place.
    """

    def __init__(self, thread: WorkerThread, mid_turn=None):
        self._thread = thread
        self._mid_turn = mid_turn
        self.reached_mid_turn = False

    # ── the reading-mode seam pause() drives ────────────────────────────────
    def enter_reading_mode(self, reason: str = "pause") -> None:
        pass

    def request_pause(self) -> None:
        self.enter_reading_mode("pause")

    # ── the turn ───────────────────────────────────────────────────────────
    def process_query(self, query: str):
        yield {"type": "assistant_message", "content": "working on it..."}
        if self._mid_turn is not None:
            self._mid_turn()
            self.reached_mid_turn = True
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


# ── the DELIBERATE kill: the record ─────────────────────────────────────────


def test_kill_with_a_reason_cancels_the_record(tmp_path, registry):
    """A deliberate kill flips the record to ``cancelled`` + its reason."""
    thread = _wire(_unstarted_thread(tmp_path))
    thread._current_query_id = "q-killed"
    before = _plant_record(thread, last_tool={"name": "read_file",
                                             "args_hash": "a" * 64})
    assert before.status == STATUS_INFLIGHT

    thread.kill(journal_reason=_MANAGER_STOP_JOURNAL_REASON)

    record, reason = _snapshot(thread)
    assert reason is None, reason
    assert record is not None, (
        "the kill cleared the record instead of cancelling it: the turn it "
        "ended is indistinguishable from a crash"
    )
    assert record.status == STATUS_CANCELLED
    assert record.reason == _MANAGER_STOP_JOURNAL_REASON
    # An UPDATE of the turn that exists, never a fresh record.
    assert record.query_id == before.query_id
    assert record.query == before.query
    assert record.created_at == before.created_at
    assert record.last_tool == before.last_tool
    assert record.attempts == 0


# ── the DELIBERATE kill: the job ────────────────────────────────────────────


def test_kill_terminalizes_the_job_as_stopped(tmp_path, registry):
    """The same string lands on the job: ``stopped``, not the park's status."""
    thread = _wire(_unstarted_thread(tmp_path))
    thread._current_query_id = "q-killed"

    thread.kill(journal_reason=_MANAGER_STOP_JOURNAL_REASON)

    assert len(registry.calls) == 1, registry.calls
    job_id, status, envelope = registry.calls[0]
    assert job_id == "q-killed"
    assert status == "stopped", (
        f"a kill is the JOB vocabulary 'stopped', not 'interrupted': {status!r}"
    )
    assert envelope == {
        "content": "",
        "status": "stopped",
        "reason": _MANAGER_STOP_JOURNAL_REASON,
    }, envelope


# ── the plain kill opts OUT ─────────────────────────────────────────────────


def test_a_plain_kill_journals_nothing(tmp_path, registry):
    """``kill()`` keeps its exact previous behaviour: no journal, no job."""
    thread = _wire(_unstarted_thread(tmp_path))
    thread._current_query_id = "q-plain"
    _plant_record(thread)
    before_bytes = journal_path(thread._worker_dir).read_bytes()

    thread.kill()

    record, reason = _snapshot(thread)
    assert reason is None, reason
    assert record is not None, "a plain kill erased a record it never owned"
    assert record.status == STATUS_INFLIGHT, (
        "a plain kill wrote to the journal: it must stay opt-in"
    )
    assert record.reason is None
    assert journal_path(thread._worker_dir).read_bytes() == before_bytes
    assert registry.calls == [], (
        f"a plain kill terminalized the job: {registry.calls}"
    )


def test_kill_on_an_idle_worker_writes_no_journal(tmp_path, registry):
    """Nothing to cancel: a kill must not conjure a phantom turn."""
    thread = _wire(_unstarted_thread(tmp_path))
    assert _snapshot(thread) == (None, None)

    thread.kill(journal_reason=_MANAGER_STOP_JOURNAL_REASON)

    assert _snapshot(thread) == (None, None)
    assert journal_path(thread._worker_dir).exists() is False, (
        "the kill created a journal record for a turn that never existed"
    )
    assert registry.calls == [], registry.calls


def test_a_repeated_cancel_keeps_the_first_record(tmp_path, registry):
    """Repeating the same cancel writes nothing: the first bytes survive."""
    thread = _wire(_unstarted_thread(tmp_path))
    thread._current_query_id = "q-twice"
    _plant_record(thread)
    thread.kill(journal_reason=_JOURNAL_RESET_REASON)
    first_bytes = journal_path(thread._worker_dir).read_bytes()
    first_record, _ = _snapshot(thread)
    assert first_record.status == STATUS_CANCELLED

    thread.kill(journal_reason=_JOURNAL_RESET_REASON)

    assert journal_path(thread._worker_dir).read_bytes() == first_bytes, (
        "a repeated cancel re-stamped the record (its updated_at is evidence)"
    )
    assert _snapshot(thread)[0].updated_at == first_record.updated_at


# ── WRITE POINT 3 is held back for a cancelled record ───────────────────────


def test_the_loop_exit_does_not_erase_the_cancelled_record(tmp_path, registry):
    """The turn's own exit must not clear the cancel the kill just wrote."""
    thread = _wire(_unstarted_thread(tmp_path))
    thread._current_query_id = "q-midturn"
    # The reset's exact seam: kill(journal_reason=...) calls this very helper,
    # so raising it mid-turn reproduces the kill without also stopping the loop
    # (whose stop/turn-cancel duties would only obscure what is being pinned).
    agent = _ScriptedAgent(
        thread,
        mid_turn=lambda: thread._journal_cancel(reason=_JOURNAL_RESET_REASON),
    )
    thread._agent = agent

    thread._run_tool_loop("do the thing")

    assert agent.reached_mid_turn is True
    record, reason = _snapshot(thread)
    assert reason is None, reason
    assert record is not None, (
        "the loop's exit erased the record the cancel had just written"
    )
    assert record.status == STATUS_CANCELLED
    assert record.reason == _JOURNAL_RESET_REASON
    assert record.query_id == "q-midturn"
    assert record.query == "do the thing"
    assert thread._journal_cancel_reason == _JOURNAL_RESET_REASON


def test_the_next_turn_clears_again(tmp_path, registry):
    """The held-back clear is PER TURN: it cannot leak into a later exit."""
    thread = _wire(_unstarted_thread(tmp_path))
    thread._current_query_id = "q-midturn"
    thread._agent = _ScriptedAgent(
        thread,
        mid_turn=lambda: thread._journal_cancel(reason=_JOURNAL_RESET_REASON),
    )
    thread._run_tool_loop("do the thing")
    assert _snapshot(thread)[0].status == STATUS_CANCELLED

    # A new turn: its write displaces the cancelled husk (the one status an
    # ordinary write may displace), and its exit clears as it always did.
    thread._current_query_id = "q-next"
    thread._agent = _ScriptedAgent(thread)
    reply = thread._run_tool_loop("do the next thing")

    assert reply == WORK
    assert thread._journal_cancel_reason is None, (
        "the cancel flag leaked into a later turn and would hold its clear back"
    )
    assert journal_path(thread._worker_dir).exists() is False
    assert _snapshot(thread) == (None, None)


def test_a_refused_cancel_does_not_hold_the_exit_back(tmp_path, registry,
                                                     monkeypatch):
    """Only a cancel that really reached disk may suppress the clear."""
    thread = _wire(_unstarted_thread(tmp_path))
    thread._current_query_id = "q-refused"
    _plant_record(thread)
    monkeypatch.setattr(
        worker_thread_module, "write_inflight",
        lambda *a, **k: "refused: record for another incarnation is work in progress",
    )

    thread._journal_cancel(reason=_MANAGER_STOP_JOURNAL_REASON)

    assert thread._journal_cancel_reason is None, (
        "a refused cancel held WRITE POINT 3 back for a record it never wrote"
    )
    assert _snapshot(thread)[0].status == STATUS_INFLIGHT


# ── the bookkeeping never gates the kill ────────────────────────────────────


def test_a_raising_journal_never_stops_the_kill(tmp_path, registry,
                                                monkeypatch):
    """A journal that raises must not stop (or delay) the worker dying."""
    thread = _wire(_unstarted_thread(tmp_path))
    thread._current_query_id = "q-boom"
    _plant_record(thread)

    def _boom(*args, **kwargs):
        raise RuntimeError("journal is on fire")

    monkeypatch.setattr(worker_thread_module, "read_inflight", _boom)

    thread.kill(journal_reason=_MANAGER_STOP_JOURNAL_REASON)

    assert thread.status == "killed"
    assert thread._stop_event.is_set(), "the kill did not reach its own duties"
    assert thread._journal_cancel_reason is None


# ── the manager's stop hands the reason over ────────────────────────────────


class _JournalAwareThread:
    """A thread double that declares the S7b-3 keyword."""

    def __init__(self) -> None:
        self.kills: list = []

    def kill(self, *, journal_reason=None) -> None:
        self.kills.append(journal_reason)

    def join(self, timeout=None) -> None:
        pass

    def is_alive(self) -> bool:
        return False


class _LegacyThread:
    """A pre-S7b-3 double: ``kill`` takes no keyword at all."""

    def __init__(self) -> None:
        self.kills = 0

    def kill(self) -> None:
        self.kills += 1

    def join(self, timeout=None) -> None:
        pass

    def is_alive(self) -> bool:
        return False


class _DetachOnlyThread:
    """A double that predates the stop/kill split: only ``stop`` exists."""

    def __init__(self) -> None:
        self.stops = 0

    def stop(self) -> None:
        self.stops += 1

    def join(self, timeout=None) -> None:
        pass

    def is_alive(self) -> bool:
        return False


def test_the_manager_hands_the_reason_to_kill():
    """A manager stop is deliberate: the reason rides on the kill."""
    manager = WorkerManager(registry=object())
    thread = _JournalAwareThread()

    assert manager._stop_thread(thread, "w-stop") is True

    assert thread.kills == [_MANAGER_STOP_JOURNAL_REASON], thread.kills


def test_the_manager_still_kills_a_thread_without_the_keyword():
    """Old doubles keep the plain zero-arg call (the journal is not a gate)."""
    manager = WorkerManager(registry=object())
    thread = _LegacyThread()

    assert manager._stop_thread(thread, "w-legacy") is True

    assert thread.kills == 1


def test_the_manager_falls_back_to_stop_when_kill_is_absent():
    """The pre-2.1a ``stop()`` fallback is untouched."""
    manager = WorkerManager(registry=object())
    thread = _DetachOnlyThread()

    assert manager._stop_thread(thread, "w-detach-only") is True

    assert thread.stops == 1


# ── a record fenced to ANOTHER incarnation is not ours to cancel ─────────────


def test_a_foreign_fenced_record_is_not_overwritten(tmp_path, registry):
    """The kill may only cancel ITS OWN turn: another incarnation's survives.

    Planted with the REAL public writer (``write_inflight``) under a fence that
    differs from this worker's on one axis -- a predecessor's record, or a live
    successor's -- so the file on disk is one this incarnation has no claim to.
    The kill must read it as absent and leave it byte-identical: a cancel is an
    UPDATE of a turn this worker owns, never a takeover of somebody else's.
    """
    thread = _wire(_unstarted_thread(tmp_path))
    mine = thread._fence()
    foreign = CommandFence(
        session_id=mine.session_id,
        worker_name=mine.worker_name,
        instance_id=mine.instance_id,
        epoch=mine.epoch + 1,
    )
    assert foreign.matches(mine) is False, foreign
    record = InflightRecord(
        query_id="q-foreign",
        query="someone else's turn",
        status=STATUS_INFLIGHT,
        attempts=0,
        requested_by="op-foreign",
        created_at=utcnow_iso(),
        updated_at=utcnow_iso(),
    )
    assert write_inflight(thread._worker_dir, record, foreign) is None

    before = journal_path(thread._worker_dir).read_bytes()
    thread.kill(journal_reason=_MANAGER_STOP_JOURNAL_REASON)

    assert journal_path(thread._worker_dir).read_bytes() == before, (
        "the kill rewrote a record fenced to another incarnation"
    )
    got, reason = _snapshot(thread)
    assert got is None, "a foreign incarnation's turn was read as this worker's"
    assert reason, "a foreign-fenced record read as absent for no stated reason"
    assert thread._journal_cancel_reason is None, (
        "a cancel that never reached disk held WRITE POINT 3 back"
    )


# ── the reset DROPS the journal: nothing behind at all ──────────────────────


def _drive_reset(thread: WorkerThread, monkeypatch):
    """Run the REAL ``Worker._action_reset`` over an unstarted thread.

    ``_drop_persisted_files`` is a NESTED function inside the action (in
    ``_reset_one``), so it has no callable surface of its own -- the action is
    the only seam that reaches it.  Three things are supplied and nothing else:
    resolution (this thread is not in the real registry), ``join`` (an unstarted
    thread cannot be joined) and ``is_alive`` (True at the action's entry check,
    False by the join loop's first look -- i.e. "the kill took effect").  The
    kill itself is the REAL one, wrapped only to record the reason it was
    handed, because for a reset the reason is the point.
    """
    looks = {"n": 0}

    def _is_alive() -> bool:
        looks["n"] += 1
        return looks["n"] == 1

    thread.is_alive = _is_alive
    thread.join = lambda timeout=None: None
    monkeypatch.setattr(
        worker_thread_module,
        "_resolve_worker_thread",
        lambda *args, **kwargs: (thread, "sess-journal-kill", 1),
    )
    kills: list = []
    real_kill = thread.kill

    def _kill(*, journal_reason=None) -> None:
        kills.append(journal_reason)
        real_kill(journal_reason=journal_reason)

    thread.kill = _kill
    worker = Worker(
        action="reset",
        worker_name="test-worker-journal-kill",
        session_id="sess-journal-kill",
    )
    return worker._action_reset(workers=[]), kills


def test_reset_drops_the_journal(tmp_path, registry, monkeypatch):
    """A reset leaves no record behind -- parseable OR corrupt, without parsing.

    A reset's promise is that the next spawn starts COMPLETELY fresh, so the
    record the reset's own kill just cancelled is dropped with the state files.
    A leftover of ANY status would either owe a successor a replay or wedge a
    new turn's write as a foreign husk.
    """
    planted = _wire(_unstarted_thread(tmp_path / "planted"))
    planted._current_query_id = "q-reset"
    _plant_record(planted)
    target = journal_path(planted._worker_dir)
    assert target.exists() is True

    result, kills = _drive_reset(planted, monkeypatch)

    assert result["status"] == "reset", result
    assert kills == [_JOURNAL_RESET_REASON], (
        f"the reset's kill must be the DELIBERATE one: {kills}"
    )
    assert target.exists() is False, (
        "the reset left the killed turn's record behind for a successor to adopt"
    )
    assert _snapshot(planted) == (None, None)

    # A corrupt journal carries no fence and no status: the drop must not read
    # it, so an unparseable leftover goes by the same entry rather than wedging
    # the reset on a decode step that could raise.
    corrupt = _wire(_unstarted_thread(tmp_path / "corrupt"))
    bad = journal_path(corrupt._worker_dir)
    bad.write_bytes(b'{"not": json')
    assert bad.exists() is True

    result, _kills = _drive_reset(corrupt, monkeypatch)

    assert result["status"] == "reset", result
    assert bad.exists() is False, "a corrupt journal survived the reset"

