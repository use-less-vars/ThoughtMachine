"""S7b-4c: MULTI-PROCESS ADOPTION -- a real OS process takes over a dead one.

What is pinned here
-------------------
Every other adoption test drives ``_run_tool_loop`` in-process.  This one puts
THREE real OS processes (``multiprocessing`` ``spawn``) over ONE worker
directory, hard-kills two of them mid-flight with ``SIGKILL``, and checks that
the successor really takes the crashed turn over and drives it to terminal:

* P1 journals its turn (WRITE POINT 1), is ``SIGKILL``ed mid-turn.  On disk the
  record stays ``inflight`` with ``attempts == 0`` -- a real crash, no cleanup.
* P2 is spawned over the SAME directory, so its ``_generation`` is strictly
  higher and its own WRITE POINT 1 would be refused by the leftover record.
  It ADOPTS it instead (``attempts`` 0 -> 1) and starts the replay turn; it is
  ``SIGKILL``ed during that replay.  On disk: the ORIGINAL ``query_id``,
  ``attempts == 1``, ``inflight``, ``generation`` of P2 (the ``epoch`` axis
  stays the conversation's, so it is unchanged -- the two numbers are not the
  same thing).
* P3 is spawned over the same directory, ADOPTS again (``attempts`` 1 -> 2),
  and COMPLETES the job: the journal file is cleared (WRITE POINT 3) and the
  turn that completed carries the ORIGINAL ``query_id`` -- the work the first
  process died holding, not a fresh query.  P3 then still answers a *further*
  query, which is the observable proof that adoption left no deadlock and no
  corrupted worker state behind.

Two side contracts ride along:

* (a) two LIVE processes over DIFFERENT directories (the real isolation unit)
  are both killed, and the successor of one directory must NOT touch the
  other's record: that file stays byte-identical.  Adoption is per-directory,
  never a filesystem-wide sweep.
* (b) a poisoned leftover at ``MAX_ATTEMPTS`` is NOT adopted (no more retries)
  and its bytes are left untouched -- an unadoptable record may wedge that one
  job, but it must never be silently rewritten, adopted, or hang the
  incarnation.

DOCUMENTED LIMITATION (out of scope for S7b-4)
----------------------------------------------
CROSS-PROCESS LIVENESS DETECTION IS NOT IMPLEMENTED.  A freshly spawned
process's in-memory job registry is empty, so it cannot tell "my predecessor is
dead" from "my predecessor is alive and still working this directory": the only
discriminator on disk is the journal record, which both look identical behind.
This module therefore only pins the case the current design supports -- the
successor is spawned AFTER its predecessor is dead (``SIGKILL`` + ``join``
before the next ``start()``).  Two *concurrently live* processes are exercised
only over DIFFERENT directories (scenario (a)), where nothing must be shared.
Making concurrent adoption safe needs a real liveness signal (future work: an
unfenced ``status.json`` heartbeat a successor can compare against its own
clock); until then, adopting while a live twin shares the directory is a
known, accepted gap, not something these tests claim to cover.

Determinism / anti-hang
-----------------------
No sleeps are used as synchronisation: the parent polls the journal file and a
per-turn witness file (``fsync``-ed by the child) with short intervals and hard
deadlines, so a stuck child fails the test instead of hanging it.  Every child
is tracked by a fixture that unconditionally ``SIGKILL``s and reaps leftovers,
nothing is left in ``multiprocessing.active_children()``, and all work happens
under ``tmp_path``.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.workspace import worker_thread as worker_thread_module
from tools.workspace.command_channel import CommandFence
from tools.workspace.inflight_journal import (
    MAX_ATTEMPTS,
    STATUS_INFLIGHT,
    InflightRecord,
    journal_path,
    read_inflight_unfenced,
    record_incarnation,
    utcnow_iso,
    write_inflight,
)
from tools.workspace.worker_registry import WorkerRegistry
from tools.workspace.worker_thread import WorkerThread

try:  # ``spawn`` is the only start method this test is about.
    _SPAWN = multiprocessing.get_context("spawn")
except ValueError:  # pragma: no cover - platform without spawn
    _SPAWN = None

#: Worker name shared by every incarnation (the directory key).
NAME = "mp-adoption-worker"

#: Session the puppet workers are spawned in (namespaces their worker dir).
SESSION = "sess-mp-adoption"

#: The answer a completed turn produces.
WORK = "THE TURN'S OWN ANSWER, delivered."

#: The query P1 died running -- the one that must survive two crashes.
DEAD_QUERY = "finish the crashed turn's work"

#: P2's own query (it must finish this first, then replay DEAD_QUERY).
NEW_QUERY = "the second process's own new work"

#: P3's own query (then it replays DEAD_QUERY a second time).
FINAL_QUERY = "the third process's own new work"

#: A further query, only to prove the third process is still healthy.
AFTER_QUERY = "still answering after the adoption?"

#: Queries for the isolation scenarios.
OTHER_QUERY = "the other directory's own work"
THIRD_QUERY = "the returning process's own work"
FRESH_QUERY = "a fresh turn next to a poisoned record"

#: Hard deadline for one wait; the whole module stays far below 60s.
WAIT_S = 20.0


def _require_spawn() -> None:
    if _SPAWN is None:  # pragma: no cover - platform without spawn
        pytest.skip("'spawn' start method is unavailable on this platform")


# ── child side (runs in the spawned process) ────────────────────────────────

class _NullStateBridge:
    """Keeps the loop's presenter lazy-init out of these tests."""

    context_length = 0


def _witness(witness_path: Path, **payload) -> None:
    """Append one durable line: the parent polls this file, never a socket."""
    payload.setdefault("pid", os.getpid())
    payload.setdefault("at", time.time())
    with open(witness_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


class _PuppetAgent:
    """Agent stand-in whose turn is canned, deterministic, and observable.

    ``block_on`` names the query whose turn must be held open forever (so the
    parent can ``SIGKILL`` the process *mid-turn*); ``None`` completes every
    turn.  Each turn writes a ``start`` and a ``done`` witness line carrying the
    ``query_id`` the loop bound and the incarnation's generation -- the only
    place the parent can read what a live child believed.
    """

    def __init__(self, witness_path: Path, block_on=None) -> None:
        self._witness_path = Path(witness_path)
        self._block_on = block_on
        self._thread = None
        self.queries: list[str] = []

    # ── seams the loop pokes ────────────────────────────────────────────────
    def enter_reading_mode(self, reason: str = "pause") -> None:
        pass

    def request_pause(self) -> None:
        self.enter_reading_mode("pause")

    # ── the turn ────────────────────────────────────────────────────────────
    def process_query(self, query: str):
        self.queries.append(query)
        thread = self._thread
        _witness(
            self._witness_path,
            phase="start",
            query=query,
            query_id=getattr(thread, "_current_query_id", None),
            generation=getattr(thread, "_generation", None),
        )
        yield {"type": "assistant_message", "content": "working on it..."}
        if self._block_on is not None and query == self._block_on:
            # Hold the turn open until the parent's SIGKILL lands.
            while True:
                time.sleep(0.05)
        _witness(
            self._witness_path,
            phase="done",
            query=query,
            query_id=getattr(thread, "_current_query_id", None),
            generation=getattr(thread, "_generation", None),
        )
        yield {
            "type": "agent_responded",
            "content": WORK,
            "status": "success",
            "confidence": "high",
        }


def _build_puppet_worker(**kwargs) -> WorkerThread:
    """Factory named in the spec; runs INSIDE the spawned process.

    It returns a REAL ``WorkerThread`` that ``worker_runtime_main`` then
    ``start()``s, so the real ``run()`` loop (real dequeue, real
    ``_run_tool_loop``, real journal write points, real adoption) drives the
    turn.  Only the agent is a puppet, which is what keeps the turn's work
    canned and observable.
    """
    name = kwargs["name"]
    thread = WorkerThread(
        name=name,
        definition={"name": name, "system_prompt": "You are a test worker."},
        agent_config={},
        workspace_dir=Path(kwargs["workspace_dir"]),
        session_id=kwargs["session_id"],
        container_manager=None,
        max_container_count=4,
        max_token_usage=None,
        max_runtime_s=None,
        instance_id=int(kwargs["instance_id"]),
    )
    thread._state_bridge = _NullStateBridge()
    thread._publish_event = lambda etype, data: None
    agent = _PuppetAgent(Path(kwargs["witness_path"]), kwargs.get("block_on"))
    agent._thread = thread
    thread._agent = agent
    return thread


# ── parent side helpers ─────────────────────────────────────────────────────

def _worker_dir(tmp_path, name: str, instance_id: int) -> Path:
    """The directory the SPAWNED process really uses for this worker.

    Delegates to ``WorkerRegistry.worker_dir`` (``workers/<session>/<label>``)
    instead of hand-assembling ``workers/<label>``: the puppet specs below spawn
    each incarnation with ``session_id=SESSION``, so the legacy, un-namespaced
    path is one no process journals into — every poll against it timed out.
    """
    return WorkerRegistry.worker_dir(tmp_path, SESSION, name, instance_id)


def _witness_path(tmp_path) -> Path:
    return Path(tmp_path) / "witness.jsonl"


def _witness_lines(tmp_path) -> list[dict]:
    path = _witness_path(tmp_path)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:  # a torn tail line mid-append: ignore it
            continue
    return out


def _poll(predicate, *, what: str, timeout: float = WAIT_S):
    """Poll ``predicate`` with short intervals; fail (never hang) on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"timed out after {timeout}s waiting for: {what}")


def _read(worker_dir: Path):
    """``(record, fence_axes)`` from the ADOPTION-ONLY reader.

    ``read_inflight_unfenced`` returns ``(record, axes, reason)``.  The axes carry
    the CONVERSATION epoch; the writing INCARNATION is the record's ``generation``
    field, which is what a successor compares against its own generation (the
    axes epoch is only the legacy fallback for records written before the split).
    """
    record, axes, _reason = read_inflight_unfenced(worker_dir)
    return record, axes


def _read_nonempty(worker_dir: Path):
    """``(record, axes)`` or ``None`` -- a poll predicate."""
    record, axes = _read(worker_dir)
    return (record, axes) if record is not None else None


def _record(worker_dir: Path):
    return _read(worker_dir)[0]


def _spec(tmp_path, *, name: str, instance_id: int, block_on, epoch: int = 1) -> dict:
    """Hand-built spec: no parent-side WorkerThread, hence no generation burn."""
    return {
        "worker": name,
        "instance": instance_id,
        "epoch": epoch,
        "worker_factory": f"{__name__}:_build_puppet_worker",
        "worker_kwargs": {
            "name": name,
            "instance_id": instance_id,
            "workspace_dir": str(Path(tmp_path)),
            "session_id": SESSION,
            "witness_path": str(_witness_path(tmp_path)),
            "block_on": block_on,
        },
    }


def _spawn(spec: dict, live: list):
    from tools.workspace.worker_runtime_process import ProcessWorkerRuntime

    runtime = ProcessWorkerRuntime(spec)
    live.append(runtime)
    runtime.start()
    return runtime


def _dispatch_async(runtime, query: str) -> threading.Thread:
    """Dispatch without blocking: a blocked turn never answers, by design."""

    def _run() -> None:
        try:
            runtime.dispatch_query(query, timeout=WAIT_S)
        except Exception:  # the runtime is killed out from under this call
            pass

    thread = threading.Thread(target=_run, name="dispatch", daemon=True)
    thread.start()
    return thread


def _hard_kill(runtime) -> None:
    """SIGKILL the real OS process and reap it: no zombie, no graceful path."""
    process = runtime._process
    assert process is not None, "runtime never spawned a process"
    pid = process.pid
    assert pid, "runtime process has no pid"
    os.kill(pid, signal.SIGKILL)
    process.join(20)
    assert not process.is_alive(), f"pid {pid} survived SIGKILL"
    assert process.exitcode == -signal.SIGKILL, (
        f"pid {pid} exitcode {process.exitcode} is not SIGKILL"
    )


def _assert_no_orphans() -> None:
    leftover = [
        child.name
        for child in multiprocessing.active_children()
        if child.name.startswith("worker-runtime-")
    ]
    assert leftover == [], f"leftover runtime processes: {leftover}"


@pytest.fixture
def live_runtimes():
    """Every runtime started here, unconditionally killed and reaped."""
    started: list = []
    yield started
    for runtime in started:
        process = getattr(runtime, "_process", None)
        try:
            if process is not None and process.is_alive():
                process.kill()
            if process is not None:
                process.join(5)
        except Exception:  # pragma: no cover - teardown must never mask a fail
            pass


# ── the three-process chain ────────────────────────────────────────────────

def test_three_process_chain_adopts_a_sigkilled_turn_to_terminal(
    tmp_path, live_runtimes
):
    """P1 killed mid-turn -> P2 adopts and is killed mid-replay -> P3 finishes."""
    _require_spawn()
    worker_dir = _worker_dir(tmp_path, NAME, 1)
    witness = _witness_path(tmp_path)

    # ── P1: journals its turn, then dies holding it ─────────────────────────
    p1 = _spawn(
        _spec(tmp_path, name=NAME, instance_id=1, block_on=DEAD_QUERY), live_runtimes
    )
    _dispatch_async(p1, DEAD_QUERY)
    rec1, axes1 = _poll(
        lambda: _read_nonempty(worker_dir),
        what="P1's WRITE POINT 1 to reach disk",
    )
    assert rec1.status == STATUS_INFLIGHT, rec1.status
    assert rec1.attempts == 0, rec1.attempts
    assert rec1.query == DEAD_QUERY, rec1.query
    original_query_id = rec1.query_id
    _poll(
        lambda: any(
            w.get("pid") == p1.pid
            and w.get("phase") == "start"
            and w.get("query") == DEAD_QUERY
            for w in _witness_lines(tmp_path)
        ),
        what="P1's turn to actually start",
    )
    gen1 = record_incarnation(rec1, axes1)
    assert gen1 is not None, (rec1.generation, axes1)
    _hard_kill(p1)

    # The crash leaves the record exactly where it was: inflight, unattempted.
    survived, _ = _read(worker_dir)
    assert survived is not None, "the journal record vanished with its process"
    assert survived.query_id == original_query_id
    assert survived.status == STATUS_INFLIGHT and survived.attempts == 0

    # ── P2: same directory, higher generation -> must ADOPT ─────────────────
    p2 = _spawn(
        _spec(tmp_path, name=NAME, instance_id=1, block_on=DEAD_QUERY), live_runtimes
    )
    _dispatch_async(p2, NEW_QUERY)

    def _replay_started_by_p2():
        for line in _witness_lines(tmp_path):
            if (
                line.get("pid") == p2.pid
                and line.get("phase") == "start"
                and line.get("query") == DEAD_QUERY
                and line.get("query_id") == original_query_id
            ):
                return line
        return None

    witness2 = _poll(
        _replay_started_by_p2,
        what="P2 to adopt the dead turn and replay it under the ORIGINAL query id",
    )
    on_disk2, axes2 = _read(worker_dir)
    assert on_disk2 is not None, "the replay turn left no journal record"
    assert on_disk2.query_id == original_query_id, on_disk2.query_id
    assert on_disk2.attempts == 1, f"attempts {on_disk2.attempts} != 1"
    assert on_disk2.status == STATUS_INFLIGHT, on_disk2.status
    # The two numbers are separate: ``epoch`` identifies the CONVERSATION and is
    # constant across incarnations of one worker directory, while ``generation``
    # identifies the INCARNATION and must strictly advance here -- that advance
    # is exactly what entitles P2 to adopt P1's record.
    gen2 = record_incarnation(on_disk2, axes2)
    assert gen2 > gen1, (gen2, gen1)
    assert axes2.get("epoch") == axes1.get("epoch"), (axes2, axes1)
    _hard_kill(p2)

    # Killed mid-replay: still inflight, still attempt 1, still the same job.
    survived2, _ = _read(worker_dir)
    assert survived2 is not None, "the record vanished when P2 was killed"
    assert survived2.query_id == original_query_id
    assert survived2.attempts == 1 and survived2.status == STATUS_INFLIGHT

    # ── P3: adopts again (1 -> 2) and drives the job to TERMINAL ────────────
    p3 = _spawn(_spec(tmp_path, name=NAME, instance_id=1, block_on=None), live_runtimes)
    _dispatch_async(p3, FINAL_QUERY)

    def _replay_done_by_p3():
        for line in _witness_lines(tmp_path):
            if (
                line.get("pid") == p3.pid
                and line.get("phase") == "done"
                and line.get("query") == DEAD_QUERY
                and line.get("query_id") == original_query_id
            ):
                return line
        return None

    witness3 = _poll(
        _replay_done_by_p3,
        what="P3 to complete the adopted turn under the ORIGINAL query id",
    )
    assert witness3["generation"] > witness2["generation"], (
        "the successor did not run in a newer incarnation"
    )
    # WRITE POINT 3: terminal means the journal file is gone.
    _poll(
        lambda: not journal_path(worker_dir).exists(),
        what="the journal to be cleared once the adopted job is terminal",
    )
    assert _record(worker_dir) is None

    # ── and the third process is still a healthy worker ────────────────────
    reply = p3.dispatch_query(AFTER_QUERY, timeout=WAIT_S)
    assert isinstance(reply, str) and reply.strip(), (
        f"the surviving process stopped answering: {reply!r}"
    )
    p3.close(grace_ms=1000)
    assert not p3.alive
    _assert_no_orphans()


# ── (a) directory isolation under real concurrency ─────────────────────────

def test_concurrent_processes_over_different_dirs_never_cross_adopt(
    tmp_path, live_runtimes
):
    """Two LIVE processes, two directories; a successor adopts only its own."""
    _require_spawn()
    dir_a = _worker_dir(tmp_path, NAME, 1)
    dir_b = _worker_dir(tmp_path, NAME, 2)

    first = _spawn(
        _spec(tmp_path, name=NAME, instance_id=1, block_on=OTHER_QUERY), live_runtimes
    )
    _dispatch_async(first, OTHER_QUERY)
    rec_a, _ = _poll(
        lambda: _read_nonempty(dir_a), what="the first directory's journal"
    )

    # Concurrently LIVE: the second process starts while the first still runs.
    second = _spawn(
        _spec(tmp_path, name=NAME, instance_id=2, block_on=THIRD_QUERY), live_runtimes
    )
    assert first.alive and second.alive, "the two processes must overlap in time"
    _dispatch_async(second, THIRD_QUERY)
    rec_b, _ = _poll(
        lambda: _read_nonempty(dir_b), what="the second directory's journal"
    )
    assert rec_b.query_id != rec_a.query_id
    bytes_b = journal_path(dir_b).read_bytes()

    _hard_kill(first)
    _hard_kill(second)

    # The returning incarnation of directory A must adopt A's record only.
    third = _spawn(_spec(tmp_path, name=NAME, instance_id=1, block_on=None), live_runtimes)
    _dispatch_async(third, THIRD_QUERY)

    _poll(
        lambda: any(
            w.get("pid") == third.pid
            and w.get("phase") == "done"
            and w.get("query") == OTHER_QUERY
            and w.get("query_id") == rec_a.query_id
            for w in _witness_lines(tmp_path)
        ),
        what="the successor to finish its OWN directory's dead turn",
    )
    _poll(
        lambda: not journal_path(dir_a).exists(),
        what="the adopted turn to reach terminal in its own directory",
    )
    # ... and to have left the other directory completely alone.
    assert journal_path(dir_b).read_bytes() == bytes_b, (
        "a successor rewrote a foreign directory's record"
    )
    untouched, _ = _read(dir_b)
    assert untouched is not None and untouched.query_id == rec_b.query_id
    assert untouched.attempts == 0 and untouched.status == STATUS_INFLIGHT
    assert not any(
        w.get("pid") == third.pid and w.get("query") == THIRD_QUERY
        and w.get("query_id") == rec_b.query_id
        for w in _witness_lines(tmp_path)
    ), "the successor replayed a query from a directory it does not own"

    third.close(grace_ms=1000)
    _assert_no_orphans()


# ── (b) a record at MAX_ATTEMPTS is not adopted ────────────────────────────

def test_poison_record_at_max_attempts_is_not_adopted(tmp_path, live_runtimes):
    """No retries left: the successor runs its own turn and touches nothing."""
    _require_spawn()
    worker_dir = _worker_dir(tmp_path, NAME, 1)

    # A leftover that has already burned every retry, addressed to an epoch
    # STRICTLY OLDER than the first incarnation of a fresh directory
    # (generation 1): epoch 0, which ``write_inflight`` accepts as a fence.
    # So the successor really reaches the adoption path and really has to
    # DECLINE it on MAX_ATTEMPTS -- no warm-up spawn needed, and the
    # generation assertion below proves the declination was "too many
    # attempts" rather than "not an older epoch".
    fence = CommandFence(
        session_id=SESSION,
        worker_name=NAME,
        instance_id=1,
        epoch=0,
    )
    poison = InflightRecord(
        query_id="poison-qid",
        query="poisoned work that must not be retried",
        status=STATUS_INFLIGHT,
        attempts=MAX_ATTEMPTS,
        requested_by=NAME,
        created_at=utcnow_iso(),
        updated_at=utcnow_iso(),
    )
    assert write_inflight(worker_dir, poison, fence) is None, "could not plant the record"
    before = journal_path(worker_dir).read_bytes()

    successor = _spawn(
        _spec(tmp_path, name=NAME, instance_id=1, block_on=None), live_runtimes
    )
    reply = successor.dispatch_query(FRESH_QUERY, timeout=WAIT_S)
    assert isinstance(reply, str) and reply.strip(), (
        "a poisoned record must not stop the incarnation from working"
    )
    generations = [
        w.get("generation")
        for w in _witness_lines(tmp_path)
        if w.get("pid") == successor.pid and w.get("query") == FRESH_QUERY
    ]
    assert generations and all(isinstance(g, int) and g > 0 for g in generations), (
        f"the successor is not a newer incarnation than the poison record: {generations}"
    )
    # Not adopted, not rewritten, not retried.
    assert journal_path(worker_dir).read_bytes() == before, (
        "an unadoptable record was rewritten"
    )
    after, _ = _read(worker_dir)
    assert after is not None
    assert after.query_id == "poison-qid"
    assert after.attempts == MAX_ATTEMPTS
    assert after.status == STATUS_INFLIGHT
    assert not any(
        w.get("query") == poison.query for w in _witness_lines(tmp_path)
    ), "the poisoned query was replayed despite MAX_ATTEMPTS"

    successor.close(grace_ms=1000)
    _assert_no_orphans()
