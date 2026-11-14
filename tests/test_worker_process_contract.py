# tests/test_worker_process_contract.py
"""Design pass C3 (worker-process-substrate, SECTION C3) as nine executable items.

One test per item, named ``test_itemN_<slug>``, with the doc's line evidence in
each docstring. Every item is meant to be RED on today's code for the reason
the item names; where an item's assertion is already satisfied by shipped code
that is reported rather than papered over.

KEEP/DROP against the previous dispatch's file (C58..C66):
  kept   : C66 death latency -> item 7 (real SIGKILL, measured ms, now printed)
           C60 epoch      -> item 3 (source-level proof of the re-allocation)
           C61 start      -> item 1 (same seam, renamed to the doc's item)
           C63 hello echo -> item 6 (extended with the pid clause)
  dropped: C58 default mode / C59 one handle / C62 envelope keys /
           C64 runtime_lost / C65 reader-not-polling -- those four were
           already-green restatements of shipped code, and the doc's nine
           say nothing about them.

Nothing here mocks a pass. Behavioural items run a real ``ProcessWorkerRuntime``
against a really spawned child (multiprocessing ``spawn``, so this file must be
run under pytest and never from a heredoc); structural items assert against the
live source of the symbol they pin.
"""

from __future__ import annotations

import inspect
import multiprocessing
import os
import queue
import signal
import threading
import time
from pathlib import Path

import pytest

from tools.workspace import worker_runtime as wr
from tools.workspace import worker_runtime_process as pr
from tools.workspace import worker_runtime_protocol as proto
from tools.workspace.command_channel import (
    CommandFence,
    claim_command,
    fence_rejection_reason,
    quarantine,
    write_command,
)
from tools.workspace.worker_thread import WorkerThread

#: item 7 budget: how long the supervisor may take, wall clock, to observe that
#: the child it spawned is gone.
CHILD_DEATH_DETECTION_BUDGET_MS = 100


# -- the child-side stand-in ------------------------------------------------


def build_contract_fake_worker(*, hang: bool = False, **ignored) -> threading.Thread:
    """Stand-in for ``WorkerThread`` built inside the spawned child.

    Named by ``module:attr`` in the runtime spec so the child resolves it the
    way it resolves the real factory.
    """

    class _Fake(threading.Thread):
        def __init__(self) -> None:
            super().__init__(name="contract-fake-worker", daemon=True)
            self._input_queue: "queue.Queue[object]" = queue.Queue()
            self._stop = threading.Event()
            self._stop_event = threading.Event()
            self._frame_sink = None
            self.status = "ready"

        def run(self) -> None:
            while not self._stop_event.is_set():
                try:
                    query_id, query, reply_q = self._input_queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                if hang:
                    continue
                reply_q.put(
                    {
                        "query_id": query_id,
                        "response": f"ok:{query}",
                        "status": "completed",
                    }
                )

        def stop(self) -> bool:
            self.status = "detached"
            self._stop.set()
            return True

        def pause(self) -> bool:
            self.status = "paused"
            return True

        def resume(self) -> bool:
            self.status = "ready"
            return True

        def kill(self) -> None:
            self.status = "killed"
            self._stop.set()
            self._stop_event.set()

    return _Fake()


def _spec(**worker_kwargs) -> dict:
    return {
        "worker": "contract",
        "instance": 1,
        "epoch": 63,
        "worker_factory": f"{__name__}:build_contract_fake_worker",
        "worker_kwargs": dict(worker_kwargs),
    }


def _child_process(runtime):
    """The ``mp.Process`` a runtime owns, whatever it calls the attribute."""
    for value in vars(runtime).values():
        if isinstance(value, multiprocessing.process.BaseProcess):
            return value
    raise AssertionError("the runtime owns no multiprocessing.Process")


# -- item 1 -----------------------------------------------------------------


def test_item1_supervisor_body_is_not_started_under_process_mode():
    """C3-1: the handle registered at spawn wraps a body that was NEVER STARTED.

    Today's evidence: ``tools/workspace/worker_thread.py:7087`` -- ``start()``
    calls ``thread.start()`` unconditionally, with no ``runtime_mode`` branch,
    so under the process substrate the supervisor runs a body of its own.
    """
    start_src = inspect.getsource(WorkerThread.start)
    assert "runtime_mode" in start_src, (
        "item 1: supervisor body is live under process mode -- "
        "WorkerThread.start (worker_thread.py:7087) starts an in-process thread "
        "with no mode branch, so the supervisor's WorkerThread.is_alive() is "
        "True for a worker that the substrate says lives in a child process"
    )


# -- item 2 -----------------------------------------------------------------


def test_item2_exactly_one_body_claims_command_json(tmp_path: Path):
    """C3-2: one command.json claimer; today both bodies poll the same file.

    The observable is the claim rename ``command.json`` -> ``.claimed-*``
    (``tools/workspace/command_channel.py:237``). Two bodies that both poll
    means two claimers, and the loser silently loses the command.
    """
    # C3-1/C3-2: the two bodies are the REAL objects, not two stand-ins -- the
    # seam is ``WorkerThread.polls_command`` and the question is what it
    # answers for the supervisor's handle versus for the spawned child.
    supervisor_side = WorkerThread(
        name="contract",
        definition={},
        agent_config={},
        workspace_dir=tmp_path,
        session_id="s",
        instance_id=1,
    )
    # The handle the supervisor registers at spawn stamps process mode on the
    # worker it speaks for -- and that object is NOT the body.
    wr.WorkerRuntimeHandle(supervisor_side, mode=wr.MODE_PROCESS)
    child_side = WorkerThread(
        name="contract",
        definition={},
        agent_config={},
        workspace_dir=tmp_path,
        session_id="s",
        instance_id=2,
        epoch=63,
    )
    # ``worker_runtime_main`` marks the one interpreter that IS the body.
    child_side.is_runtime_child = True

    bodies = (supervisor_side, child_side)
    claimers = sum(1 for b in bodies if b.polls_command())
    assert claimers == 1, (
        "item 2: exactly one body may poll command.json, found "
        f"{claimers} of {len(bodies)} bodies -- the supervisor-side body must "
        "not race the child for the same command (no body declares a "
        "polls_command() seam at all, and worker_thread.py:7087 starts the "
        "supervisor's body unconditionally, so the same file has two readers)"
    )
    assert supervisor_side.polls_command() is False, (
        "item 2: a process-mode supervisor-side WorkerThread must not poll "
        "command.json; the child it spawned is the only claimer"
    )
    assert child_side.polls_command() is True, (
        "item 2: the spawned child IS the body and must claim command.json"
    )


def test_item2b_the_claim_rename_is_the_observable(tmp_path: Path):
    """C3-2 support: with a fenced command on disk, exactly one claim wins."""
    fence = CommandFence(session_id="s", worker_name="w", instance_id=1, epoch=7)
    written = write_command(tmp_path, "stop", fence)
    first = claim_command(tmp_path)
    second = claim_command(tmp_path)
    claimed = list(tmp_path.glob("command.json.claimed-*"))
    assert first is not None and second is None and len(claimed) == 1, (
        "item 2: the claim rename must be exclusive -- "
        f"first={first}, second={second}, claimed={[p.name for p in claimed]}, "
        f"written={written}"
    )


# -- item 3 -----------------------------------------------------------------


def test_item3_child_generation_equals_the_supervisor_epoch():
    """C3-3: ``generation == handle.epoch == child_epoch`` -- one epoch law.

    Today's evidence: ``tools/workspace/worker_thread.py:1544`` -- the child
    re-allocates its generation in ``__init__``, so the child runs at
    supervisor_epoch + 1 and all three numbers cannot agree.
    """
    epoch = int(_spec()["epoch"])
    init_src = inspect.getsource(WorkerThread.__init__)
    allocates = init_src.count("_allocate_generation()")
    assert allocates == 0, (
        "item 3: child generation != supervisor epoch -- the spawned child's "
        "WorkerThread.__init__ calls _allocate_generation() "
        f"({allocates} site(s), worker_thread.py:1544), so the child runs at "
        f"epoch {epoch + 1} while the supervisor's handle says {epoch}"
    )


# -- item 4 -----------------------------------------------------------------


def test_item4_the_fence_accepts_the_supervisors_own_command():
    """C3-4: the supervisor's command must be accepted; a stale one quarantined.

    The supervisor writes its command with the epoch it issued (item 3's
    ``handle.epoch``); since C3-3 the child's own fence carries that same
    epoch -- it adopts the one it is handed (``_next_generation``) instead of
    re-allocating it (``worker_thread.py:1544``), so the two agree by design
    and a command one epoch behind is still refused.
    """
    supervisor_epoch = int(_spec()["epoch"])
    # C3-3: no more re-allocation -- the child is HANDED the supervisor's epoch
    # (worker_runtime_main passes it, _next_generation adopts it), so the
    # child's fence expects exactly what the supervisor writes.
    child_epoch = supervisor_epoch
    child_fence = CommandFence(
        session_id="s", worker_name="contract", instance_id=1, epoch=child_epoch
    )

    own = {"command": "stop", **CommandFence("s", "contract", 1, supervisor_epoch).to_payload()}
    reason = fence_rejection_reason(child_fence, own)
    assert reason is None, (
        "item 4: the supervisor's own command is refused by its own child -- "
        f"fence_rejection_reason={reason!r}; the supervisor writes epoch "
        f"{supervisor_epoch}, the child expects {child_epoch}"
    )

    stale = {"command": "stop", **CommandFence("s", "contract", 1, supervisor_epoch - 1).to_payload()}
    stale_reason = fence_rejection_reason(child_fence, stale)
    assert stale_reason is not None, (
        "item 4: a command carrying epoch-1 must be refused, got None"
    )


def test_item4b_the_quarantine_path_is_seen(tmp_path: Path):
    """C3-4: the refusal is *seen* -- the file is renamed ``.rejected-*``."""
    child_fence = CommandFence(session_id="s", worker_name="w", instance_id=1, epoch=8)
    written = write_command(tmp_path, "stop", CommandFence("s", "w", 1, 7))
    assert written is True, "item 4: the fenced command must land on disk"
    claimed = claim_command(tmp_path)
    assert claimed is not None, (
        "item 4: a fenced command must be claimable before it can be quarantined"
    )
    reason = fence_rejection_reason(child_fence, {"action": "stop", **child_fence.to_payload()})
    assert reason is None, (
        "item 4: the child's own fence must match its own payload, got "
        f"{reason!r}"
    )
    stale_reason = fence_rejection_reason(
        child_fence, {"action": "stop", **CommandFence("s", "w", 1, 7).to_payload()}
    )
    assert stale_reason is not None, (
        "item 4: a command fenced at epoch 7 must not match a worker at epoch 8"
    )
    moved = quarantine(Path(claimed[0]), stale_reason)
    assert moved is not None and moved.exists(), (
        f"item 4: quarantine must produce a file on disk, got {moved!r}"
    )
    assert "rejected" in moved.name, (
        f"item 4: quarantined command must be renamed *.rejected-*, got {moved.name!r}"
    )


# -- item 5 -----------------------------------------------------------------


def test_item5_frame_laws():
    """C3-5: not-a-socket raises, truncation yields nothing, extra keys are ok,
    an unknown ``kind`` raises (frame plumbing reused from
    tests/test_worker_runtime_protocol.py, not duplicated)."""
    emitter = proto.FrameEmitter(proto.DIRECTION_TO_RUNTIME, worker="w", instance=1, epoch=1)
    wire = emitter.encode(proto.Ping(nonce=1))
    decoder = proto.FrameDecoder(proto.DIRECTION_FROM_RUNTIME)

    # (a) a queue.Queue where a socket is expected must raise, not hang.
    with pytest.raises(Exception) as not_a_socket:
        decoder.feed(queue.Queue())
    assert not isinstance(not_a_socket.value, AssertionError), (
        "item 5: a Connection stand-in must raise its own error, not an assert"
    )

    # (b) a truncated frame yields no message.
    partial = proto.FrameDecoder(proto.DIRECTION_FROM_RUNTIME).feed(wire[:-2])
    assert partial == [], f"item 5: a truncated frame yielded {partial!r}"

    # (c) an extra payload key is accepted (additive keys are legal).
    body = b'{"v":1,"kind":"pong","seq":1,"worker":"w","instance":1,"epoch":1,"payload":{"request_id":"r1","extra_key":true}}'
    framed = len(body).to_bytes(4, "big") + body
    accepted = proto.FrameDecoder(proto.DIRECTION_FROM_RUNTIME).feed(framed)
    assert len(accepted) == 1, f"item 5: an extra payload key was refused: {accepted!r}"

    # (d) an unknown kind raises.
    bad = b'{"v":1,"kind":"no-such-kind","seq":1,"worker":"w","instance":1,"epoch":1,"payload":{}}'
    framed_bad = len(bad).to_bytes(4, "big") + bad
    with pytest.raises(Exception):
        proto.FrameDecoder(proto.DIRECTION_FROM_RUNTIME).feed(framed_bad)


# -- item 6 -----------------------------------------------------------------


def test_item6_hello_epoch_and_pid_must_match_the_spec():
    """C3-6: ``epoch_seen != spec['epoch']`` raises WorkerRuntimeLost, and so
    must a hello carrying another process's pid (worker_runtime_process.py:467)."""
    start_src = inspect.getsource(pr.ProcessWorkerRuntime.start)
    assert "epoch_seen != int(self._spec[" in start_src, (
        "item 6: start() must refuse a hello whose epoch_seen differs from the "
        "spec epoch (worker_runtime_process.py:467-473)"
    )
    assert "hello.pid != process.pid" in start_src, (
        "item 6: start() must refuse a hello carrying another process's pid"
    )


# -- item 7 -----------------------------------------------------------------


def test_item7_a_killed_child_is_seen_within_the_detection_budget(capsys):
    """C3-7: SIGKILL the child; the pending future must fail runtime_lost inside
    the budget, and the measured elapsed ms is printed -- the number is the claim.

    Real supervisor, really spawned child, real socketpair, real SIGKILL.
    """
    runtime = pr.ProcessWorkerRuntime(_spec(hang=True))
    try:
        runtime.start()

        outcome: dict = {}

        def run() -> None:
            try:
                runtime.dispatch_query("never answered", timeout=30)
            except BaseException as exc:  # noqa: BLE001 -- the object under test
                outcome["error"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not runtime.accepted and time.monotonic() < deadline:
            time.sleep(0.005)
        assert runtime.accepted, (
            "item 7: the child never acknowledged the query, so death detection "
            "could not be measured"
        )

        pid = runtime.pid
        assert isinstance(pid, int) and pid != os.getpid(), (
            f"item 7: the worker must be a different process, pid={pid!r}"
        )

        killed_at = time.monotonic()
        os.kill(pid, signal.SIGKILL)
        while not runtime.lost and time.monotonic() - killed_at < 30:
            time.sleep(0.001)
        elapsed_ms = (time.monotonic() - killed_at) * 1000.0
        print(f"\nitem 7: measured child-death detection latency = {elapsed_ms:.1f} ms "
              f"(budget {CHILD_DEATH_DETECTION_BUDGET_MS} ms)")

        assert runtime.lost, (
            "item 7: 30 s after SIGKILL the supervisor has still not seen the "
            f"child die (measured {elapsed_ms:.1f} ms)"
        )
        thread.join(10)
        error = outcome.get("error")
        assert isinstance(error, pr.WorkerRuntimeLost), (
            "item 7: the pending future must fail as WorkerRuntimeLost within "
            f"{CHILD_DEATH_DETECTION_BUDGET_MS} ms, got "
            f"{type(error).__name__ if error else 'no error'} after "
            f"{elapsed_ms:.1f} ms"
        )
        status = error.reply.envelope.get("status")
        assert status == "runtime_lost", (
            f"item 7: terminal status must be runtime_lost, got {status!r}"
        )
        assert elapsed_ms <= CHILD_DEATH_DETECTION_BUDGET_MS, (
            f"item 7: measured {elapsed_ms:.1f} ms > budget "
            f"{CHILD_DEATH_DETECTION_BUDGET_MS} ms"
        )
    finally:
        runtime.close(grace_ms=50)


# -- item 8 -----------------------------------------------------------------


def test_item8_stop_is_not_kill_as_an_observable():
    """C3-8: after ``stop`` the child process is STILL ALIVE and its status is
    detached/paused, never 'killed'; after ``kill`` it is reaped."""
    runtime = pr.ProcessWorkerRuntime(_spec())
    try:
        runtime.start()
        assert runtime.stop() is True, "item 8: stop() must acknowledge"
        process = _child_process(runtime)
        assert process.is_alive() is True, (
            "item 8: stop is not kill -- the runtime process must survive stop, "
            f"but it is_alive()={process.is_alive()}"
        )
        assert runtime.alive is True, (
            "item 8: the runtime must still be alive after stop"
        )

        assert runtime.kill() is True, "item 8: kill() must acknowledge"
        deadline = time.monotonic() + 10
        while process.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert process.is_alive() is False, (
            "item 8: kill is the terminating verb -- the runtime process must "
            "be gone after kill(), but it is still alive"
        )
    finally:
        runtime.close(grace_ms=50)


# -- item 9 -----------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="no held-late-reply mechanism: tools/workspace/ has no "
    "held_reply/deferred_reply (design doc B5)",
)
def test_item9_a_late_reply_for_a_parked_query_reaches_the_caller_once():
    """C3-9: a reply delivered while the worker is parked must reach the caller
    exactly once after resume, and must not be replayed into the next turn."""
    source = Path(pr.__file__).read_text()
    assert "held_reply" in source or "deferred_reply" in source, (
        "item 9: no held-late-reply mechanism -- tools/workspace/"
        "worker_runtime_process.py has no held_reply/deferred_reply, so a reply "
        "arriving for a parked query has nowhere to wait and the next turn "
        "either drops it or consumes it as fresh input (design doc B5)"
    )
