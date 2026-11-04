"""S7b-4a: ADOPTION AT SPAWN -- the successor takes over a dead turn.

What is pinned here
-------------------
A HARD-KILLED / crashed incarnation leaves its journal record behind, and the
successor spawned afterwards reuses the SAME worker directory: ``spawn``
iterates ``_worker_registry`` filtering ``is_alive()``
(``tools/workspace/worker_thread.py:5294-5300``), so after a crash
``live_same == []`` and the chain falls through to ``spawn_iid = 1`` -- the bare
worker name, hence ``workers/<name>``.  The leftover record is therefore
addressed to a LOWER epoch (``_generation`` is allocated per directory,
monotonically), and ``write_inflight`` FAILS CLOSED on it: without adoption the
successor's own WRITE POINT 1 would be refused and its turn would run
unjournalled.  The contract:

* The successor ADOPTS an ``inflight`` record from a strictly older epoch, once,
  at turn start, before WRITE POINT 1 (``_adopt_predecessor_journal``, called
  from ``WorkerThread._run_tool_loop``).  The record becomes the successor's
  (attempts bumped by one, status ``adopted``) and one log line says so.
* The adopted query is not merely recorded: it is queued on ``_input_queue``,
  the seam ``run()`` dequeues from (``worker_thread.py:3473`` for the get, the
  bare-string branch at ``:3608-3613``), so the crashed caller's work RUNS --
  exactly once.
* ``cancelled`` is terminal and ``recoverable`` is the DETACH park's record
  (the RESUME path's business, NOT spawn's): neither is adopted, and the file is
  left byte-identical -- which is also the assertion message that names the
  reason.
* Nothing on disk, a corrupt journal, or a record at this epoch or newer: no
  adoption, no raise, no behaviour change.
* VACUITY: with adoption neutered, the very same fixture leaves the successor's
  WRITE POINT 1 REFUSED and the predecessor's bytes untouched.  That is the
  wedge adoption exists to remove -- without this test, a broken adoption path
  would silently still look green.

Determinism
-----------
No thread is ever ``start()``ed, no sleeps, no Docker, no network.  Records are
planted with the REAL ``write_inflight`` under a predecessor ``CommandFence``
(so the payload and the axes are the production ones), and the turn is driven
by calling ``_run_tool_loop`` directly with a scripted agent -- the same
``_unstarted_thread`` / ``_wire`` / ``_ScriptedAgent`` pattern as
``tests/test_worker_journal_kill.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.workspace import worker_thread as worker_thread_module
from tools.workspace.command_channel import CommandFence
from tools.workspace.inflight_journal import (
    MAX_ATTEMPTS,
    STATUS_ADOPTED,
    STATUS_CANCELLED,
    STATUS_INFLIGHT,
    STATUS_RECOVERABLE,
    InflightRecord,
    journal_path,
    read_inflight,
    read_inflight_unfenced,
    utcnow_iso,
    write_inflight,
)
from tools.workspace.worker_thread import WorkerThread

#: The answer a turn produces.  Distinctive so a failure names what was kept.
WORK = "THE TURN'S OWN ANSWER, delivered."

#: The query the DEAD predecessor was running when it died.
DEAD_QUERY = "finish the crashed turn's work"

#: The query the successor was spawned for.
NEW_QUERY = "the successor's own new work"


# ── harness ──────────────────────────────────────────────────────────────────

def _unstarted_thread(tmp_path) -> WorkerThread:
    """A REAL ``WorkerThread`` that is never ``start()``ed."""
    return WorkerThread(
        name="test-worker-adoption",
        definition={"name": "test-worker-adoption",
                    "system_prompt": "You are a test worker."},
        agent_config={},
        workspace_dir=Path(tmp_path),
        session_id="sess-adoption",
        container_manager=None,
        max_container_count=4,
        max_token_usage=None,
        max_runtime_s=None,
    )


class _ScriptedAgent:
    """Agent stand-in that records every query the loop actually runs."""

    def __init__(self, mid_turn=None) -> None:
        self.queries = []
        self._mid_turn = mid_turn

    # ── the reading-mode seam pause() drives ─────────────────────────────────
    def enter_reading_mode(self, reason: str = "pause") -> None:
        pass

    def request_pause(self) -> None:
        self.enter_reading_mode("pause")

    # ── the turn ─────────────────────────────────────────────────────────────
    def process_query(self, query: str):
        self.queries.append(query)
        yield {"type": "assistant_message", "content": "working on it..."}
        if self._mid_turn is not None:
            self._mid_turn()
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
    """Install the null presenter and a recording agent; leave the rest alone."""
    thread._state_bridge = _NullStateBridge()
    thread._publish_event = lambda etype, data: None
    thread._agent = _ScriptedAgent()
    return thread


class _LogCapture:
    """Records ``log(level, channel, message)`` calls made by the loop."""

    def __init__(self) -> None:
        self.entries = []

    def __call__(self, level, channel, message) -> None:
        self.entries.append((level, channel, message))

    def messages(self):
        return [message for _level, _channel, message in self.entries]


@pytest.fixture
def log_capture(monkeypatch) -> _LogCapture:
    capture = _LogCapture()
    monkeypatch.setattr(worker_thread_module, "log", capture)
    return capture


def _predecessor_fence(
    thread: WorkerThread, *, epoch_delta: int = -1, instance_delta: int = 0
) -> CommandFence:
    """The fence of the incarnation that preceded this one.

    A DEAD predecessor is a LOWER epoch in the SAME directory -- that is the
    whole reason the unfenced reader exists.  ``epoch_delta=0`` builds the
    other interesting case: a different incarnation at THIS epoch, which is
    not a dead predecessor's and must not be adopted.
    """
    return CommandFence(
        session_id=thread.session_id or "",
        worker_name=thread.worker_name,
        instance_id=(thread.instance_id or 1) + instance_delta,
        epoch=thread._generation + epoch_delta,
    )


def _plant_predecessor_record(
    thread: WorkerThread,
    *,
    status: str = STATUS_INFLIGHT,
    query: str = DEAD_QUERY,
    query_id: str = "q-dead",
    attempts: int = 0,
    epoch_delta: int = -1,
    instance_delta: int = 0,
):
    """Write the record the dead predecessor left, with its OWN fence."""
    stamp = utcnow_iso()
    record = InflightRecord(
        query_id=query_id,
        query=query,
        status=status,
        attempts=attempts,
        requested_by="send_query",
        created_at=stamp,
        updated_at=stamp,
    )
    fence = _predecessor_fence(
        thread, epoch_delta=epoch_delta, instance_delta=instance_delta
    )
    assert write_inflight(thread._worker_dir, record, fence) is None, (
        "the fixture could not plant the predecessor's record"
    )
    return record, fence


def _unfenced_snapshot(thread: WorkerThread):
    """The record as the ADOPTION reader sees it: ``(record, axes, reason)``."""
    return read_inflight_unfenced(thread._worker_dir)


# ── 1. the crash: an ``inflight`` record from a lower epoch is ADOPTED ───────

def test_a_dead_predecessors_inflight_record_is_adopted_at_turn_start(
    tmp_path, log_capture, monkeypatch
):
    """The leftover ``inflight`` record becomes the successor's, once."""
    thread = _wire(_unstarted_thread(tmp_path))
    dead, dead_fence = _plant_predecessor_record(thread)

    calls = []
    real_adopt = worker_thread_module._adopt_predecessor_journal

    def _counting_adopt(worker_dir, fence, generation):
        calls.append((fence, generation))
        return real_adopt(worker_dir, fence, generation)

    monkeypatch.setattr(
        worker_thread_module, "_adopt_predecessor_journal", _counting_adopt
    )

    thread._current_query_id = "q-new"
    # Snapshot the journal MID-turn: WRITE POINT 3 clears it at the end of the
    # turn, and what matters here is that WP1 could land at all.
    mid_turn = {}
    thread._agent = _ScriptedAgent(
        mid_turn=lambda: mid_turn.update(snapshot=_unfenced_snapshot(thread))
    )

    thread._run_tool_loop(NEW_QUERY)

    recorded, axes, why = mid_turn["snapshot"]
    assert recorded is not None and why is None, (
        "after adoption the successor's own WRITE POINT 1 was still refused: "
        f"{why!r}"
    )
    assert axes["epoch"] == thread._generation, axes
    assert recorded.query == NEW_QUERY

    adopted = thread._journal_adopted
    assert adopted is not None, (
        "the successor did not adopt the dead predecessor's inflight record: "
        "its own WRITE POINT 1 would have been refused"
    )
    assert adopted.status == STATUS_ADOPTED, adopted.status
    assert adopted.attempts == dead.attempts + 1, (
        f"attempts must follow on from the stored count: {adopted.attempts!r}"
    )
    # An UPDATE of the turn that exists, re-stamped to THIS incarnation.
    assert adopted.query_id == dead.query_id
    assert adopted.query == DEAD_QUERY
    assert adopted.created_at == dead.created_at
    assert journal_path(thread._worker_dir).exists() is False, (
        "the completed turn left a record behind: WRITE POINT 3 did not clear"
    )
    assert len(calls) == 1, calls
    assert any("adopted query 'q-dead'" in message for message in log_capture.messages()), (
        f"no adoption log line: {log_capture.messages()!r}"
    )


def test_the_adopted_query_actually_runs_once(tmp_path, monkeypatch):
    """The crashed caller's turn RUNS -- via run()'s own dequeue seam, once."""
    thread = _wire(_unstarted_thread(tmp_path))
    _plant_predecessor_record(thread)

    calls = []
    real_adopt = worker_thread_module._adopt_predecessor_journal
    monkeypatch.setattr(
        worker_thread_module,
        "_adopt_predecessor_journal",
        lambda worker_dir, fence, generation: (
            calls.append(generation) or real_adopt(worker_dir, fence, generation)
        ),
    )

    thread._run_tool_loop(NEW_QUERY)

    # The successor's own turn ran first, as it was the one dequeued.
    assert thread._agent.queries == [NEW_QUERY], thread._agent.queries

    # The adopted query is on the queue ``run()`` drains
    # (``raw = self._input_queue.get(timeout=2.0)``, worker_thread.py:3473), as
    # a ``(query_id, query, reply_q)`` TRIPLE (S7b-4b): the dead job's id must
    # travel with it, or run() binds ``query_id = None``
    # (worker_thread.py:3607-3618) and the crashed caller's registry entry is
    # never completed (``:3996-4002``).
    queued = thread._input_queue.get_nowait()
    assert isinstance(queued, tuple) and len(queued) == 3, (
        f"the adopted query was not queued as a triple for run(): {queued!r}"
    )
    _queued_id, _queued_query, _queued_reply_q = queued
    assert _queued_query == DEAD_QUERY, (
        f"the adopted query was not queued for run(): {queued!r}"
    )

    # ...and running it once delivers it (run() binds ``_current_query_id`` from
    # the triple before the turn starts, worker_thread.py:3617-3618).
    thread._current_query_id = _queued_id
    thread._run_tool_loop(_queued_query)
    assert thread._agent.queries == [NEW_QUERY, DEAD_QUERY], thread._agent.queries
    assert len(calls) == 1, (
        f"adoption ran more than once per incarnation: {calls!r}"
    )
    empty = pytest.raises(Exception)
    with empty:
        thread._input_queue.get_nowait()


# ── 2. ``cancelled`` is terminal ─────────────────────────────────────────────

def test_a_cancelled_record_is_not_adopted(tmp_path):
    """A killed job is never resurrected: the successor runs its own query."""
    thread = _wire(_unstarted_thread(tmp_path))
    _plant_predecessor_record(thread, status=STATUS_CANCELLED)

    thread._run_tool_loop(NEW_QUERY)

    assert thread._journal_adopted is None, (
        "a cancelled record was adopted: a killed job must never be resurrected"
    )
    assert thread._agent.queries == [NEW_QUERY], thread._agent.queries
    with pytest.raises(Exception):
        thread._input_queue.get_nowait()


# ── 3. ``recoverable`` is the RESUME path's business ─────────────────────────

def test_a_recoverable_record_is_not_adopted_by_spawn(tmp_path):
    """The detach park's record belongs to RESUME, not to spawn."""
    thread = _wire(_unstarted_thread(tmp_path))
    _plant_predecessor_record(thread, status=STATUS_RECOVERABLE)
    before_bytes = journal_path(thread._worker_dir).read_bytes()

    thread._run_tool_loop(NEW_QUERY)

    assert thread._journal_adopted is None, (
        "a recoverable record was adopted at spawn: that record is the DETACH "
        "park's, and replaying it is the RESUME path's business, not spawn's"
    )
    assert journal_path(thread._worker_dir).read_bytes() == before_bytes, (
        "the spawn path touched the detach park's record"
    )
    record, _axes, _why = _unfenced_snapshot(thread)
    assert record is not None and record.status == STATUS_RECOVERABLE


# ── 4. nothing on disk ───────────────────────────────────────────────────────

def test_no_journal_means_no_adoption_and_no_error(tmp_path):
    """The ordinary case: nothing to adopt, nothing changes."""
    thread = _wire(_unstarted_thread(tmp_path))
    assert journal_path(thread._worker_dir).exists() is False

    thread._run_tool_loop(NEW_QUERY)

    assert thread._journal_adopted is None
    assert thread._agent.queries == [NEW_QUERY], thread._agent.queries
    with pytest.raises(Exception):
        thread._input_queue.get_nowait()


# ── 5. a corrupt journal ─────────────────────────────────────────────────────

def test_a_corrupt_journal_is_not_adopted_and_does_not_raise(tmp_path):
    """Garbage on disk is a no-op, not a failed turn."""
    thread = _wire(_unstarted_thread(tmp_path))
    journal_path(thread._worker_dir).write_bytes(b"{ this is not json")

    thread._run_tool_loop(NEW_QUERY)  # must not raise

    assert thread._journal_adopted is None, (
        "a corrupt journal was reported as an adoption"
    )
    assert thread._agent.queries == [NEW_QUERY], thread._agent.queries
    with pytest.raises(Exception):
        thread._input_queue.get_nowait()


# ── 6. an epoch that is not older ────────────────────────────────────────────

def test_a_record_at_this_epoch_is_not_adopted(tmp_path):
    """Only a STRICTLY older epoch is a dead predecessor's."""
    thread = _wire(_unstarted_thread(tmp_path))
    _plant_predecessor_record(
        thread, epoch_delta=0, instance_delta=1, query_id="q-sibling"
    )
    before_bytes = journal_path(thread._worker_dir).read_bytes()

    adopted = worker_thread_module._adopt_predecessor_journal(
        thread._worker_dir, thread._fence(), thread._generation
    )

    assert adopted is None, (
        "a record at THIS epoch was adopted: only a strictly older epoch is a "
        "dead predecessor's"
    )
    assert journal_path(thread._worker_dir).read_bytes() == before_bytes, (
        "the declined adoption still wrote to the journal"
    )


def test_a_record_from_a_newer_epoch_is_not_adopted(tmp_path):
    """A newer incarnation's record is never taken over by an older one."""
    thread = _wire(_unstarted_thread(tmp_path))
    _plant_predecessor_record(
        thread, epoch_delta=+1, instance_delta=1, query_id="q-newer"
    )

    adopted = worker_thread_module._adopt_predecessor_journal(
        thread._worker_dir, thread._fence(), thread._generation
    )

    assert adopted is None, "a NEWER incarnation's record was adopted"


# ── 7. VACUITY: without adoption, WRITE POINT 1 is REFUSED ───────────────────

def test_without_adoption_the_successors_write_point_1_is_refused(
    tmp_path, log_capture, monkeypatch
):
    """Pin the wedge adoption removes: WP1 fail-closed on a foreign record.

    The seam is the honest one the loop really uses: neutering the module-level
    helper is exactly the "adoption disabled" configuration, and the same
    fixture that produces a successful adoption above must now leave the
    successor's turn UNJOURNALLED -- ``write_inflight`` refuses to displace
    foreign work in progress, and ``clear_inflight`` refuses to erase it, so the
    predecessor's bytes survive the whole turn unchanged.
    """
    monkeypatch.setattr(
        worker_thread_module, "_adopt_predecessor_journal",
        lambda worker_dir, fence, generation: None,
    )
    thread = _wire(_unstarted_thread(tmp_path))
    _plant_predecessor_record(thread)
    before_bytes = journal_path(thread._worker_dir).read_bytes()

    thread._run_tool_loop(NEW_QUERY)

    assert thread._journal_adopted is None
    assert thread._agent.queries == [NEW_QUERY], thread._agent.queries
    assert journal_path(thread._worker_dir).read_bytes() == before_bytes, (
        "without adoption the successor's WRITE POINT 1 was supposed to be "
        "REFUSED (fail closed on a foreign in-flight record) and the "
        "predecessor's bytes left untouched -- it was not"
    )
    record, axes, _why = _unfenced_snapshot(thread)
    assert record is not None and record.status == STATUS_INFLIGHT
    assert axes["epoch"] == thread._generation - 1, axes
    # The successor's own record never landed: this turn ran unjournalled,
    # which is precisely the wedge the adoption path removes.
    assert read_inflight(thread._worker_dir, thread._fence()) == (
        None,
        "epoch %d != %d" % (thread._generation - 1, thread._generation),
    ), "the successor's turn WAS journalled with adoption disabled"
    assert any("[journal] write refused" in message for message in log_capture.messages()), (
        f"the refusal was not logged: {log_capture.messages()!r}"
    )


# ── 8. S7b-4b DEFECT 1: the replay is attributed to the DEAD job ─────────────

def test_the_replay_carries_the_dead_job_id_so_its_job_is_not_left_dangling(
    tmp_path,
):
    """A bare-string replay binds ``query_id=None``: the dead job never ends.

    The bare string only ever carried the TEXT.  ``run()`` unpacks what it
    dequeues at ``worker_thread.py:3607-3618``, so a bare string takes the
    ``query_id, query, reply_q = None, raw, None`` branch and
    ``self._current_query_id`` becomes ``None`` -- and run() only finishes the
    job-registry entry when that id is set
    (``if self._current_query_id is not None: _registry.complete(
    self._current_query_id, envelope)``, ``:3996-4002``), which is also the only
    case in which ``envelope['query_id']`` is stamped (``:3957-3958``).  The
    crashed caller blocked in ``wait_for_job`` would therefore wait forever on a
    job nobody could terminalize.  The triple is what makes the replay the DEAD
    job's, so its entry reaches a terminal state instead of dangling.
    """
    thread = _wire(_unstarted_thread(tmp_path))
    dead, _dead_fence = _plant_predecessor_record(thread)

    thread._run_tool_loop(NEW_QUERY)

    item = thread._input_queue.get_nowait()
    assert isinstance(item, tuple) and len(item) == 3, (
        "the adopted query was enqueued as a bare string: run() then binds "
        f"query_id=None and the dead job's registry entry dangles ({item!r})"
    )
    query_id, query, reply_q = item
    assert query_id == dead.query_id, (
        "the replay was not attributed to the dead job's id: "
        f"{query_id!r} != {dead.query_id!r}"
    )
    assert query == DEAD_QUERY, query
    assert reply_q is None, (
        "the dead caller's private reply queue died with it; the envelope must "
        f"fall back to the shared output queue instead ({reply_q!r})"
    )
    # The journal record and the queue agree on whose turn this is.
    assert thread._journal_adopted is not None
    assert thread._journal_adopted.query_id == query_id


# ── 9. S7b-4b DEFECT 2: attempts ACCUMULATE, and the cap is honoured ─────────

def test_attempts_accumulate_across_replays_and_stop_at_the_cap(tmp_path):
    """Adoption 0 -> 1 must survive WRITE POINT 1, and a second crash makes 2.

    ``_journal_write`` used to re-stamp ``attempts=0`` on every write, so the
    count adoption owned was wiped by the replay's own WRITE POINT 1:
    ``MAX_ATTEMPTS`` could never fire and a poison query would replay forever.
    """
    thread = _wire(_unstarted_thread(tmp_path))
    _plant_predecessor_record(thread, attempts=0)

    thread._run_tool_loop(NEW_QUERY)
    first = thread._journal_adopted
    assert first is not None and first.attempts == 1, first

    # The replay turn, driven exactly as run() would: the triple it was queued
    # as supplies ``_current_query_id``, and MID-turn (before WRITE POINT 3
    # clears it) the record on disk is what the next successor would find.
    query_id, query, _reply_q = thread._input_queue.get_nowait()
    thread._current_query_id = query_id
    mid_turn = {}
    thread._agent = _ScriptedAgent(
        mid_turn=lambda: mid_turn.update(snapshot=_unfenced_snapshot(thread))
    )
    thread._run_tool_loop(query)

    recorded, _axes, why = mid_turn["snapshot"]
    assert recorded is not None and why is None, why
    assert recorded.query == DEAD_QUERY, recorded.query
    assert recorded.attempts == 1, (
        "WRITE POINT 1 re-stamped attempts=0 and wiped the adoption's count, so "
        f"the cap can never be reached: {recorded.attempts!r}"
    )

    # A SECOND crash: the next incarnation of the SAME worker directory (spawn
    # reuses ``workers/<name>``, so its generation is strictly higher) finds the
    # record at attempt 1 and adopts it as attempt 2.
    successor = _wire(_unstarted_thread(tmp_path))
    assert successor._generation > thread._generation, (
        successor._generation, thread._generation,
    )
    _plant_predecessor_record(successor, attempts=1)
    successor._run_tool_loop(NEW_QUERY)
    second = successor._journal_adopted
    assert second is not None and second.attempts == 2, second

    # ...and a record ALREADY at the cap is refused, never adopted (building
    # attempt MAX_ATTEMPTS + 1 would raise): the successor runs its own turn and
    # leaves the predecessor's bytes untouched.
    capped = _wire(_unstarted_thread(tmp_path))
    _plant_predecessor_record(capped, attempts=MAX_ATTEMPTS)
    before_bytes = journal_path(capped._worker_dir).read_bytes()

    capped._run_tool_loop(NEW_QUERY)

    assert capped._journal_adopted is None, (
        "a record already at MAX_ATTEMPTS was adopted: the cap must stop the "
        "replay, not be silently exceeded"
    )
    assert journal_path(capped._worker_dir).read_bytes() == before_bytes, (
        "the refused adoption still wrote to the journal"
    )
    assert capped._agent.queries == [NEW_QUERY], capped._agent.queries


# ── 10. S7b-4b: no bleed -- an ordinary turn still writes attempts=0 ─────────

def test_an_ordinary_turn_after_an_adoption_writes_attempts_zero(tmp_path):
    """The replay count applies to exactly ONE turn: the replay itself."""
    thread = _wire(_unstarted_thread(tmp_path))
    _plant_predecessor_record(thread)

    thread._run_tool_loop(NEW_QUERY)          # adopts: attempts 1
    replay_id, replay_query, _reply_q = thread._input_queue.get_nowait()
    thread._current_query_id = replay_id
    thread._run_tool_loop(replay_query)       # the replay consumes the count
    assert thread._journal_replay_attempts == {}, (
        "the replay count was not consumed by the replay turn: "
        f"{thread._journal_replay_attempts!r}"
    )

    # A LATER, ordinary turn of the same incarnation: nothing to replay.
    follow_up = "ordinary follow-up work"
    thread._current_query_id = "q-ordinary"
    mid_turn = {}
    thread._agent = _ScriptedAgent(
        mid_turn=lambda: mid_turn.update(snapshot=_unfenced_snapshot(thread))
    )
    thread._run_tool_loop(follow_up)

    recorded, _axes, why = mid_turn["snapshot"]
    assert recorded is not None and why is None, why
    assert recorded.query == follow_up, recorded.query
    assert recorded.attempts == 0, (
        "the adopted attempt count BLED into an ordinary turn: "
        f"{recorded.attempts!r}"
    )

