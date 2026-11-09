"""C10 hermetic pins: does a blocked tool call RETURN when the turn is cancelled?

The claim under test (C10): a tool call already blocked inside
``DockerCodeRunner`` must RETURN promptly when the turn-cancel token fires.

CORRECTED CLAIM -- what the code actually does, and what this file now pins:
``DockerCodeRunner`` does not poll the token itself; the promptness comes from
``ContainerManager.exec``, which runs ``container.exec_run`` on a daemon thread
and polls ``is_interrupted()`` every ``_WAIT_SLICE_SECONDS = 0.5``
(``infra/container_manager.py:2582``).  A blocked tool call therefore returns
within one 0.5 s poll slice of the cancel -- not within the 0.1 s slice the
original C10 wording assumed.

Nothing here touches Docker, a provider, or the network.  The "container" is a
``threading.Event``-driven fake injected at the same seam
``tests/test_container_exec_interrupt.py`` uses (a real ``ContainerManager``
constructed with ``__new__``, a fake ``client.containers.get``, and the guards
that are not under test neutralised).

The call chain actually exercised, quoted from the source:

  ``DockerCodeRunner.execute()``             tools/docker_code_runner.py:295
    -> ``manager = ContainerManager(...)``    tools/docker_code_runner.py:394
    -> ``info = manager.start(...)``          tools/docker_code_runner.py:411
    -> ``result = manager.exec(info["id"],`` tools/docker_code_runner.py:450
    -> ``container.exec_run(**exec_kwargs)``  infra/container_manager.py:2558

``exec_run`` runs on a daemon thread; the caller that used to block in a single
full-timeout ``join`` now waits in ``_WAIT_SLICE_SECONDS = 0.5`` slices and
polls ``is_interrupted()`` between them (``infra/container_manager.py:2582``
onward).  The fake container deliberately does NOT poll the token: it models
the inner blocking call only, so any promptness observed comes from the layer
above it.

Measured on this harness after that fix: 0.501 s from ``token.set()`` to the
call returning, 4 of 4 trials.  Test 1 explains why its margin is 2.0 s
rather than 0.5 s.
"""

from __future__ import annotations

import json
import threading

import pytest

import tools.docker_code_runner as dcr
from infra.container_manager import ContainerManager
from tools import cancellation
from tools.sleep_tool import Sleep


# ---------------------------------------------------------------------------
# Fakes -- shape mirrored from tests/test_container_exec_interrupt.py
# ---------------------------------------------------------------------------


class _BlockingContainer:
    """A container whose ``exec_run`` blocks until *release* is set.

    It does NOT poll the cancel token: it models today's inner blocking call,
    so the only thing that can make the caller return early is the layer above.
    ``entered`` reports that the blocking call was actually reached, so the
    test observes "blocked" instead of sleeping and hoping.
    """

    def __init__(self, container_id="c1", block_seconds=30.0):
        self.id = container_id
        self.name = container_id
        self.status = "running"
        self.labels = {}
        self.attrs = {"State": {"Status": "running"}}
        self.release = threading.Event()
        self.entered = threading.Event()
        self.block_seconds = block_seconds
        self.killed = False
        self.exec_calls = []

    def reload(self):
        return None

    def kill(self):
        self.killed = True
        self.release.set()

    def remove(self, force=False):
        return None

    def exec_run(self, **kwargs):
        self.exec_calls.append(kwargs)
        self.entered.set()
        self.release.wait(self.block_seconds)
        return (0, (b"out", b"err"))


class _Containers:
    def __init__(self, container):
        self._container = container

    def get(self, container_id):
        return self._container

    def list(self, all=False, filters=None):
        return []


class _Client:
    def __init__(self, container):
        self.containers = _Containers(container)


def _make_manager(container, tmp_path):
    """A REAL ``ContainerManager`` (so ``exec`` is production code) with a fake client.

    ``start``/``stop`` are replaced because they are not under test here; the
    wait-and-poll loop inside ``exec`` is, and that stays untouched.
    """
    cm = ContainerManager.__new__(ContainerManager)
    cm.workspace_path = str(tmp_path)
    cm.image = "agent-executor"
    cm.network = "none"
    cm.mem_limit = "1g"
    cm.cpu_quota = 100000
    cm.session_id = "s1"
    cm.workspace_id = "ws-1"
    cm.session_permissions = {}
    cm._session_config = {}
    cm._containers = {}
    cm.client = _Client(container)
    cm.vault_root = str(tmp_path)
    cm.container_notes = {}
    cm.max_containers = 10
    cm.workspace_config = {}
    cm.dockerfile_path = None
    # Neutralise the guards that are not under test.
    cm._agent_access_denial = lambda lifecycle_class: None
    cm.class_of = lambda ctr: "ephemeral"
    cm._check_exec_drift = lambda ctr, cid: ("allow", None)
    cm._exceeds_disk_quota = lambda ctr, mb: False
    cm._append_usage_log = lambda *a, **k: None
    cm.dropped = []
    cm._drop_container = lambda cid: cm.dropped.append(cid)
    # Lifecycle bookkeeping around exec() is out of scope; exec() itself is not.
    cm.start = lambda **kwargs: {"id": container.id, "status": "created"}
    cm.stop = lambda container_id: None
    return cm


def _install_runner(monkeypatch, manager, tmp_path):
    """Point ``DockerCodeRunner`` at the fake client behind the REAL manager."""
    monkeypatch.setattr(dcr, "DOCKER_AVAILABLE", True)
    monkeypatch.setattr(dcr, "ContainerManager", lambda **kwargs: manager)
    monkeypatch.setattr(dcr, "audit_event", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(
        dcr.DockerCodeRunner, "_resolve_registry_workspace", lambda self: str(tmp_path)
    )


@pytest.fixture(autouse=True)
def _clean_token():
    """The main thread never carries a binding or a stale reason into a test."""
    cancellation.unbind()
    yield
    cancellation.unbind()


# ---------------------------------------------------------------------------
# Test 1 -- the C10 pin.  GREEN, and deliberately not marked as an expected
# failure: the mechanism exists today (ContainerManager.exec polls the token),
# so this is a regression pin against its removal, not a statement about a
# missing feature.
# ---------------------------------------------------------------------------


def test_blocked_tool_call_returns_within_a_poll_slice_of_cancel(monkeypatch, tmp_path):
    """A blocked tool call RETURNS within one poll slice of the turn cancel.

    WHY 2.0 s and not 0.5 s: ``ContainerManager.exec`` polls ``is_interrupted()``
    every ``_WAIT_SLICE_SECONDS = 0.5`` (``infra/container_manager.py:2582``),
    so the worst-case latency after the token fires is one full slice --
    measured here at 0.501 s, 4/4 trials.  A 0.5 s margin was tried first and
    rejected as flaky-on-the-boundary: it sat exactly on that 0.501 s edge and
    the body passed only 3 of 5 consecutive runs.  2.0 s is 4x the poll period,
    deliberately off any boundary, so a red here means the poll is gone rather
    than that the box was busy.
    """
    ctr = _BlockingContainer()
    _install_runner(monkeypatch, _make_manager(ctr, tmp_path), tmp_path)
    token = threading.Event()
    captured = {}

    def _run_tool_call():
        # Bound on the thread that RUNS the tool call: the token is thread-local
        # (tools/cancellation.py:17), so binding it anywhere else would test a
        # different thing entirely.
        cancellation.bind_turn_cancel(token)
        try:
            captured["payload"] = dcr.DockerCodeRunner(
                command="sleep 30", timeout=30
            ).execute()
        finally:
            cancellation.unbind()

    thread = threading.Thread(
        target=_run_tool_call, name="c10-blocked-tool-call", daemon=True
    )
    try:
        thread.start()
        # Do not sleep-and-hope: wait until the fake reports it is really blocked.
        assert ctr.entered.wait(5.0), "the fake container was never entered"
        token.set()
        thread.join(2.0)
        # catches: C10 mechanism — this goes red the day ContainerManager.exec
        # stops polling is_interrupted() (the call would hang until the fake
        # released it, i.e. is_alive() true at 2.0 s), which is the exact
        # regression that silently turns a cancellable worker back into an
        # uncancellable one.
        assert not thread.is_alive(), (
            "the tool call was still blocked 2.0 s (4x the 0.5 s poll slice) "
            f"after the turn was cancelled; payload={captured.get('payload')!r}"
        )
    finally:
        # Not optional: a thread left blocked here would hang the next seat.
        ctr.release.set()
        thread.join(5)
        assert not thread.is_alive(), "the tool-call thread leaked out of the test"

    # ``execute()`` returns a JSON *string* (``json.dumps`` at
    # tools/docker_code_runner.py:484), so the promptness above is only
    # meaningful if the string carries the interrupted payload.
    payload = json.loads(captured["payload"])
    # catches: a return that is NOT the interrupted payload -- e.g. exec
    # reporting a clean exit for a process that never finished, or the timeout
    # path firing first -- would make the promptness assertion above vacuous.
    assert payload["status"] == "interrupted"
    assert payload["exit_code"] is None
    assert payload["reason"] == "turn_cancelled"


# ---------------------------------------------------------------------------
# Test 2 -- thread scoping (green today).
# ---------------------------------------------------------------------------


def test_turn_cancel_is_thread_scoped():
    """A token bound on one thread is invisible to the other, and ``unbind()``
    clears it for good.

    Related but NOT a duplicate: ``tests/test_sleep_tool.py::
    test_cancelling_one_worker_does_not_cancel_another_worker_sleep`` (lines
    173-191) already covers cross-thread isolation *during* work -- worker A is
    cancelled (``interrupted`` True, ``elapsed_seconds`` < 0.8) while worker B
    keeps sleeping (``interrupted`` False, ``elapsed_seconds`` >= 1.0).  What
    that test never observes is the CLEAR: each thread there unbinds on its way
    out and nothing asks again.  Our addition is the clear-and-stay-clear case
    -- after ``unbind()``, ``is_interrupted()`` reads False even though the
    event that WAS bound is still set, which is what a leaked binding
    (``_tls.event`` never reset, or a process-global event) would fail.
    """
    token = threading.Event()
    token.set()
    bound = threading.Event()
    step2 = threading.Event()
    done = threading.Event()
    seen = {}

    def _helper():
        cancellation.bind_turn_cancel(token)
        seen["helper_after_bind"] = cancellation.is_interrupted()
        bound.set()
        step2.wait(5.0)
        cancellation.unbind()
        seen["helper_after_unbind"] = cancellation.is_interrupted()
        done.set()

    thread = threading.Thread(target=_helper, name="c10-scope-helper", daemon=True)
    try:
        thread.start()
        assert bound.wait(5.0), "the helper thread never bound its token"
        # The main thread never bound that event, so it must not see it.
        seen["main"] = cancellation.is_interrupted()
        step2.set()
        assert done.wait(5.0), "the helper thread never reached unbind()"
    finally:
        step2.set()
        thread.join(5)
        assert not thread.is_alive(), "the scope-helper thread leaked out of the test"

    # catches: thread-scope regression -- a cancel leaking across threads would
    # let one worker's cancel stop another worker in the same runtime process,
    # which is the isolation the threading.local exists to provide.
    assert seen["helper_after_bind"] is True
    assert seen["main"] is False
    assert seen["helper_after_unbind"] is False


# ---------------------------------------------------------------------------
# Positive control -- same harness, blocked work that DOES poll (Sleep).
# ---------------------------------------------------------------------------


def test_sleep_tool_returns_within_a_slice_of_cancel():
    token = threading.Event()
    started = threading.Event()
    captured = {}

    def _run_sleep():
        cancellation.bind_turn_cancel(token)
        try:
            started.set()
            captured["payload"] = json.loads(Sleep(seconds=30).execute())
        finally:
            cancellation.unbind()

    thread = threading.Thread(target=_run_sleep, name="c10-sleep", daemon=True)
    try:
        thread.start()
        assert started.wait(5.0), "the sleep tool was never called"
        token.set()
        thread.join(0.5)
        # catches: vacuous -- if this fails, the harness or the cancel mechanism
        # is broken, and Test 1's green would be meaningless.  This control is
        # what makes Test 1 trustworthy: blocked work that DOES poll has to come
        # back fast, so a slow Test 1 would implicate the upper layer's poll.
        assert not thread.is_alive(), (
            "the sleep tool did not return within 0.5 s of the cancel; "
            f"payload={captured.get('payload')!r}"
        )
    finally:
        token.set()
        thread.join(5)
        assert not thread.is_alive(), "the sleep thread leaked out of the test"

    payload = captured["payload"]
    assert payload["interrupted"] is True
    assert payload["ok"] is False
    # A 30 s request that returned in under half a second can only be the
    # cancel path -- not the sleep running to completion.
    assert payload["requested_seconds"] == 30.0
    assert payload["elapsed_seconds"] < 0.5
