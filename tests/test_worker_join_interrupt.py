"""A ``join``/``wait_for_job`` WAIT is cancellable; the JOB never is.

The join poll loop (``Worker._action_join``) runs on the worker's tool
thread, which owns the thread-local turn-cancel token bound by
``tools.sleep_tool.bind_turn_cancel``.  When that token is set (detach(),
kill(), cross-process 'stop') the wait returns a structured
``status='interrupted'`` envelope instead of blocking out its timeout -- and
the job stays in the registry, untouched.

Hermetic: no real worker thread, no LLM, no Docker, no sleeps-as-sync.

Run:  python3 -m pytest tests/test_worker_join_interrupt.py -q
"""

import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.sleep_tool import bind_turn_cancel, unbind  # noqa: E402
from tools.workspace import worker_thread as wt  # noqa: E402
from tools.workspace.job_registry import WorkerJobRegistry  # noqa: E402
from tools.workspace.worker_thread import Worker  # noqa: E402

WORKER = "w-join"
SESSION = "s-join"
IID = 1


class _NullBus:
    """Stand-in event bus: the registry is fed directly in these tests."""

    def subscribe(self, *a, **k):
        return None

    def publish(self, *a, **k):
        return None


def _live_fake_thread():
    """A live thread object shaped like a WorkerThread (running, not stopped)."""
    latch = threading.Event()
    t = threading.Thread(target=latch.wait, daemon=True)
    t.status = "running"
    t._stop_event = threading.Event()  # present but NOT set
    t.start()
    return t, latch


def _make_tool(job_id, timeout=5, agent_config=None):
    return Worker(
        action="join",
        worker_name=WORKER,
        worker_query=job_id,
        timeout_seconds=timeout,
        session_id=SESSION,
        instance_id=IID,
        agent_config=agent_config or {},
    )


def _patch(monkeypatch, registry, thread):
    monkeypatch.setattr(wt, "_get_worker_job_registry", lambda: registry)
    monkeypatch.setattr(
        wt, "_resolve_worker_thread", lambda *a, **k: (thread, SESSION, IID)
    )


def _run_on_tool_thread(fn, bind_event=None):
    """Run *fn* on a fresh thread (optionally with a bound token); return its result."""
    out = []

    def _target():
        if bind_event is not None:
            bind_turn_cancel(bind_event)
        try:
            out.append(fn())
        finally:
            if bind_event is not None:
                unbind()

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    return t, out


def test_interrupted_join_returns_early_without_cancelling_the_job(monkeypatch):
    registry = WorkerJobRegistry(event_bus=_NullBus())
    registry.register("job-wait", WORKER, session_id=SESSION, instance_id=IID)
    thread, latch = _live_fake_thread()
    _patch(monkeypatch, registry, thread)

    token = threading.Event()
    tool = _make_tool("job-wait", timeout=5)
    started = time.monotonic()
    runner, out = _run_on_tool_thread(lambda: tool._action_join([]), bind_event=token)
    time.sleep(0.35)  # let the poll loop enter its wait
    token.set()  # what detach()/kill() does to the owning worker
    runner.join(timeout=5)
    elapsed = time.monotonic() - started
    latch.set()

    assert out, "join never returned"
    res = out[0]
    assert res["status"] == "interrupted"
    assert res["interrupted"] is True
    assert res["reason"] == "turn_cancelled"
    assert res["job_id"] == "job-wait"
    assert res["last_status"] == "submitted"
    assert res["completed_jobs"] == []
    assert elapsed < 3.0, f"interrupted join took {elapsed:.2f}s (timeout was 5s)"

    # The job is NOT cancelled and NOT removed by the interrupted wait.
    still = registry.job("job-wait")
    assert still is not None
    assert still["status"] == "submitted"


def test_completed_jobs_are_reported_on_interrupt(monkeypatch):
    registry = WorkerJobRegistry(event_bus=_NullBus())
    registry.register("job-done", WORKER, session_id=SESSION, instance_id=IID)
    registry.update("job-done", status="completed")
    registry.register("job-wait", WORKER, session_id=SESSION, instance_id=IID)
    thread, latch = _live_fake_thread()
    _patch(monkeypatch, registry, thread)

    token = threading.Event()
    tool = _make_tool("job-wait", timeout=5)
    runner, out = _run_on_tool_thread(lambda: tool._action_join([]), bind_event=token)
    time.sleep(0.35)
    token.set()
    runner.join(timeout=5)
    latch.set()

    res = out[0]
    assert res["status"] == "interrupted"
    assert [j["job_id"] for j in res["completed_jobs"]] == ["job-done"]
    assert res["completed_jobs"][0]["status"] == "completed"


def test_unbound_join_waits_out_its_full_timeout(monkeypatch):
    """No token bound (tests / direct use) => the full-timeout wait is unchanged."""
    registry = WorkerJobRegistry(event_bus=_NullBus())
    registry.register("job-wait", WORKER, session_id=SESSION, instance_id=IID)
    thread, latch = _live_fake_thread()
    _patch(monkeypatch, registry, thread)

    tool = _make_tool("job-wait", timeout=1)
    started = time.monotonic()
    runner, out = _run_on_tool_thread(lambda: tool._action_join([]))
    runner.join(timeout=10)
    elapsed = time.monotonic() - started
    latch.set()

    res = out[0]
    assert res["status"] == "timeout"
    assert "interrupted" not in res
    assert elapsed >= 0.9
    assert registry.job("job-wait")["status"] == "submitted"


def test_stop_check_probe_interrupts_the_wait(monkeypatch):
    """An injected ``agent_config['stop_check']`` probe mirrors Sleep._external_stop."""
    registry = WorkerJobRegistry(event_bus=_NullBus())
    registry.register("job-wait", WORKER, session_id=SESSION, instance_id=IID)
    thread, latch = _live_fake_thread()
    _patch(monkeypatch, registry, thread)

    tool = _make_tool("job-wait", timeout=5, agent_config={"stop_check": lambda: True})
    started = time.monotonic()
    runner, out = _run_on_tool_thread(lambda: tool._action_join([]))
    runner.join(timeout=5)
    elapsed = time.monotonic() - started
    latch.set()

    res = out[0]
    assert res["status"] == "interrupted"
    assert res["reason"] == "stop_check"
    assert elapsed < 3.0
    assert registry.job("job-wait")["status"] == "submitted"
