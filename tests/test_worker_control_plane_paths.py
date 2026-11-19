# tests/test_worker_control_plane_paths.py
"""C23: the four control-plane paths that no test drove end-to-end.

Each path is asserted by ONE test named for the claim it makes:

* path (i)  -- ``WorkerRuntimeHandle`` builds its process runtime LAZILY
  (nothing at spawn, exactly one build at the first dispatch, reused after).
* path (ii) -- ``AgentController.resume()`` is the release half: it clears
  the pause signals and takes the RUN axis out of PAUSED, while leaving a
  restriction it does not own alone.
* path (iii)-- ``Agent.restart()`` RE-POINTS the rebuilt ``ToolExecutor`` at
  the agent's LIVE state object rather than at a fresh one.
* path (iv) -- ``AgentController.request_pause()`` on an IDLE controller
  settles PAUSING -> PAUSED itself and emits ``session_stop``, because no
  in-flight turn exists that could land on PAUSED later.

Grep tags carried in the docstrings and on the assertions themselves:
``[CONTROL_PLANE_STATE]`` ``[CONTROL_PLANE_PAUSE]`` ``[CONTROL_PLANE_GATE]``
``[CONTROL_PLANE_RESTART]`` ``[CONTROL_PLANE_CONTROLLER]``
``[CONTROL_PLANE_WORKER_CLEAR]``.

Harness style: real objects wherever they are cheap (``WorkerThread``,
``AgentController``, ``Agent``, ``AgentState``); the only patched things are
LEAVES whose real behaviour is a fork/vault-write covered elsewhere, and each
patch is justified where it is applied.
"""

from __future__ import annotations

import queue
from pathlib import Path

import pytest

from agent.config.models import AgentConfig
from agent.controller import AgentController
from agent.core.agent import Agent
from agent.core.state import ExecutionState
import tools.workspace.worker_runtime_process as worker_runtime_process
from tools.workspace.worker_runtime import (
    MODE_PROCESS,
    attach_thread_frame_sink,
)
from tools.workspace.worker_thread import WorkerThread

WORKER = "cp-paths-worker"
SESSION_ID = "sess-cp-paths"


def _real_worker(tmp_path: Path) -> WorkerThread:
    """A real ``WorkerThread`` that is never started (no run(), no Agent)."""
    return WorkerThread(
        name=WORKER,
        definition={},
        agent_config={"provider": "openai", "model": "gpt-4"},
        workspace_dir=tmp_path,
        session_permissions={},
        instance_id=1,
    )


def _real_agent() -> Agent:
    """A real ``Agent`` built the cheapest way (no network, no logging)."""
    agent = Agent(
        config=AgentConfig(api_key="test-key", enable_logging=False),
        session_id=SESSION_ID,
    )
    if agent.conversation is None:
        agent.conversation = []
    return agent


def _bound_controller(agent: Agent) -> AgentController:
    """A real ``AgentController`` bound to *agent* and never ``start()``ed.

    ``_running`` is set and ``thread`` left ``None``: ``is_running`` then takes
    its "alive supervisor, no thread object" path and answers ``True`` without
    starting a thread -- the same shape the bridge calls these methods from.
    """
    controller = AgentController()
    controller.agent = agent
    controller.current_session_id = SESSION_ID
    controller._running = True
    controller.thread = None
    return controller


def _drain(q: "queue.Queue") -> list:
    events = []
    while True:
        try:
            events.append(q.get_nowait())
        except queue.Empty:
            return events


# ---------------------------------------------------------------------------
# path (i): lazy build of the process runtime
# ---------------------------------------------------------------------------

def test_control_plane_state_handle_spawn_builds_no_process_runtime(tmp_path):
    """[CONTROL_PLANE_STATE] Spawning a process-mode handle builds NOTHING.

    Claim: ``WorkerRuntimeHandle.__init__`` leaves ``_spec`` and
    ``_process_runtime`` ``None`` (its own docstring: "constructing a handle
    must not spawn an OS process (§7)"), and the liveness probes
    (``alive`` / ``ping``) read ``_process_runtime`` directly instead of going
    through ``_ensure_process_runtime`` -- "a probe that spawns an OS process
    would make asking about a process the reason it exists".

    Uses the PRODUCTION construction site ``attach_thread_frame_sink`` around a
    real ``WorkerThread``; nothing is patched.
    """
    worker = _real_worker(tmp_path)

    handle = attach_thread_frame_sink(worker, mode=MODE_PROCESS)

    assert handle.mode == MODE_PROCESS  # [CONTROL_PLANE_STATE] precondition
    assert handle._process_runtime is None  # [CONTROL_PLANE_STATE] no child yet
    assert handle._spec is None  # [CONTROL_PLANE_STATE] no spec either

    # Probes are read-only: neither may be the thing that spawns.
    assert handle.alive is None  # [CONTROL_PLANE_STATE] "no child", not "dead"
    assert handle.ping() is None  # [CONTROL_PLANE_STATE] nothing to ping
    assert handle._process_runtime is None  # [CONTROL_PLANE_STATE] still none
    assert handle._spec is None  # [CONTROL_PLANE_STATE] still none

    handle.close()


def test_control_plane_gate_first_dispatch_builds_process_runtime_once(
    tmp_path, monkeypatch
):
    """[CONTROL_PLANE_GATE] The first dispatch builds exactly one runtime, reused.

    Claim: ``_ensure_process_runtime`` spawns on FIRST USE and reuses it: the
    second ``dispatch_query`` must reach the same runtime object, and
    ``ProcessWorkerRuntime`` must be constructed exactly once.

    Patch justification (leaf): the real constructor forks an interpreter, and
    the fork/handshake itself is covered by ``tests/test_worker_process_e2e.py``.
    Here the leaf ``ProcessWorkerRuntime`` is replaced by a recorder so the
    BUILD COUNT and the reuse are observable without a child process; the spec
    is still produced by the real ``spec_from_worker`` against the real worker.
    """
    worker = _real_worker(tmp_path)
    handle = attach_thread_frame_sink(worker, mode=MODE_PROCESS)
    builds = []

    class _RecorderRuntime:
        def __init__(self, spec, *, on_frame=None):
            self.spec = spec
            self.on_frame = on_frame
            self.started = 0
            self.queries = []
            builds.append(self)

        def start(self):
            self.started += 1

        @property
        def alive(self):
            return True

        def dispatch_query(self, query, timeout=120.0):
            self.queries.append((query, timeout))
            return f"reply:{query}"

        def close(self, *, grace_ms=5000):
            pass

    monkeypatch.setattr(
        worker_runtime_process, "ProcessWorkerRuntime", _RecorderRuntime
    )

    try:
        assert handle._process_runtime is None  # [CONTROL_PLANE_GATE] before
        assert handle.dispatch_query("one") == "reply:one"
        assert handle.dispatch_query("two") == "reply:two"

        assert len(builds) == 1  # [CONTROL_PLANE_GATE] built exactly once
        runtime = builds[0]
        assert runtime.started == 1  # [CONTROL_PLANE_GATE] started once
        assert [q for q, _ in runtime.queries] == ["one", "two"]
        assert handle._process_runtime is runtime  # [CONTROL_PLANE_GATE] reused
        assert handle._spec is not None  # [CONTROL_PLANE_GATE] spec built here
        assert handle._spec == runtime.spec
        assert handle.alive is True  # [CONTROL_PLANE_GATE] now a real child
    finally:
        handle.close()
        assert handle._process_runtime is None  # [CONTROL_PLANE_GATE] cleared

# ---------------------------------------------------------------------------
# path (iv): controller idle pause branch
# ---------------------------------------------------------------------------

def test_control_plane_pause_idle_branch_settles_paused_and_emits_session_stop():
    """[CONTROL_PLANE_PAUSE] An IDLE pause settles PAUSING->PAUSED and emits stop.

    Claim (``AgentController.request_pause`` idle branch): with NO turn in
    flight (``_processing_query`` False), the controller sets ``pause_event`` /
    ``_pause_requested``, walks PAUSING then PAUSED through the setter itself
    -- "nothing is in flight in this branch, so the wind-down is already
    complete: settle on PAUSED" -- puts the agent in 'pause' reading mode, and
    emits ``session_stop`` with ``stop_reason='paused'``.  That last event is
    what distinguishes this branch from ``pause()``, which never emits it.

    Real objects throughout (real ``Agent``, real ``AgentState``, real
    ``AgentController``); the emitted events are read off the controller's own
    ``event_queue``, so nothing is patched at all.
    """
    agent = _real_agent()
    controller = _bound_controller(agent)

    # Preconditions: the branch under test is REACHED (not the early return)
    # and nothing is in flight, which is what makes it the idle branch.
    assert controller.is_running is True  # [CONTROL_PLANE_PAUSE] reachable
    assert controller._processing_query is False  # [CONTROL_PLANE_PAUSE] idle
    assert agent.state.execution_state == ExecutionState.READY

    controller.request_pause()

    # Interrupt signal and its latch.
    assert controller.pause_event.is_set() is True  # [CONTROL_PLANE_PAUSE]
    assert controller._pause_requested is True  # [CONTROL_PLANE_PAUSE]

    # RUN axis: PAUSING then PAUSED, both from this branch (no turn to land).
    assert agent.state.execution_state == ExecutionState.PAUSED  # [CONTROL_PLANE_PAUSE]

    # Reading mode owned by 'pause': the allow-list is narrowed to Respond.
    assert agent.state.restriction_reason == "pause"  # [CONTROL_PLANE_PAUSE]
    assert agent.state.get_allowed_tools() == ["Respond"]  # [CONTROL_PLANE_PAUSE]

    events = _drain(controller.event_queue)
    # ``AgentState.set_execution_state`` returns legacy-shaped flat dicts
    # (``convert_to_legacy_format`` flattens the typed payload), so the two
    # state names sit at the top level of the event.
    transitions = [
        (e["old_state"], e["new_state"])
        for e in events
        if e.get("type") == "execution_state_change"
    ]
    assert transitions == [  # [CONTROL_PLANE_CONTROLLER] both writes emitted
        (ExecutionState.READY.value, ExecutionState.PAUSING.value),
        (ExecutionState.PAUSING.value, ExecutionState.PAUSED.value),
    ]
    stops = [e for e in events if e.get("type") == "session_stop"]
    assert len(stops) == 1  # [CONTROL_PLANE_PAUSE] exactly one stop event
    assert stops[0]["stop_reason"] == "paused"  # [CONTROL_PLANE_PAUSE]


# ---------------------------------------------------------------------------
# path (ii): resume-side clear
# ---------------------------------------------------------------------------

def test_control_plane_worker_clear_resume_releases_paused_to_ready():
    """[CONTROL_PLANE_WORKER_CLEAR] ``resume()`` is the release half of the pause.

    Claim: ``AgentController.resume()`` clears both pause signals
    (``pause_event`` and the ``_pause_requested`` latch on controller AND
    agent), releases the 'pause'-owned restriction, and takes the RUN axis out
    of PAUSED *itself* -- "pause is the ONLY owner of those two states and this
    is the release of that pause, so the state has to leave them HERE".  Idle
    (``_processing_query`` False) means the settle target is READY, and the
    write goes through ``_set_execution_state`` so the bridge receives an
    ``execution_state_change`` (→ ``state_changed``).

    The state is reached the real way: an idle ``request_pause()`` first, then
    ``resume()``.  Real objects only, nothing patched.
    """
    agent = _real_agent()
    controller = _bound_controller(agent)

    controller.request_pause()
    assert agent.state.execution_state == ExecutionState.PAUSED  # precondition
    assert agent.state.restriction_reason == "pause"  # precondition
    _drain(controller.event_queue)

    controller.resume()

    # Interrupt signal + latch cleared on BOTH sides.
    assert controller.pause_event.is_set() is False  # [CONTROL_PLANE_WORKER_CLEAR]
    assert controller._pause_requested is False  # [CONTROL_PLANE_WORKER_CLEAR]
    assert agent._pause_requested is False  # [CONTROL_PLANE_WORKER_CLEAR]
    # Owner-scoped release: the 'pause' restriction is gone.
    assert agent.state.restriction_reason is None  # [CONTROL_PLANE_WORKER_CLEAR]
    assert agent.state.get_allowed_tools() == []  # [CONTROL_PLANE_WORKER_CLEAR]
    # RUN axis released here (nothing else does it), idle -> READY.
    assert agent.state.execution_state == ExecutionState.READY  # [CONTROL_PLANE_CONTROLLER]

    events = _drain(controller.event_queue)
    transitions = [
        (e["old_state"], e["new_state"])
        for e in events
        if e.get("type") == "execution_state_change"
    ]
    assert transitions == [  # [CONTROL_PLANE_CONTROLLER] the release is announced
        (ExecutionState.PAUSED.value, ExecutionState.READY.value),
    ]


# ---------------------------------------------------------------------------
# path (iii): restart re-point
# ---------------------------------------------------------------------------

def test_control_plane_restart_repoints_rebuilt_executor_at_live_state():
    """[CONTROL_PLANE_RESTART] ``Agent.restart()`` re-points the NEW executor.

    Claim: ``Agent.restart`` keeps ONE ``AgentState`` object across the restart
    and hands THAT object to the rebuilt ``ToolExecutor`` -- ``ToolExecutor(...,
    self.state, ...)`` -- so the executor's allowance/permission checks read the
    live state (``same_object=True`` in the ``[CONTROL_PLANE_RESTART]`` log
    lines).  A rebuild that passed a fresh or stale state would silently
    disconnect tool gating from the agent's state.

    Real ``Agent`` + real ``ToolExecutor``; nothing patched.
    """
    agent = _real_agent()
    old_state = agent.state
    old_executor = agent.tool_executor
    new_config = AgentConfig(
        api_key="test-key", enable_logging=False, model="gpt-4o"
    )

    assert agent.restart(new_config) is True  # [CONTROL_PLANE_RESTART]

    # One state object survives the restart ...
    assert agent.state is old_state  # [CONTROL_PLANE_RESTART] state preserved
    # ... and the executor was rebuilt ...
    assert agent.tool_executor is not old_executor  # [CONTROL_PLANE_RESTART]
    # ... re-pointed at that SAME live state, not a copy.
    assert agent.tool_executor.state is agent.state  # [CONTROL_PLANE_RESTART] re-point
    assert agent.tool_executor.state is not None  # [CONTROL_PLANE_RESTART]
    assert agent.tool_executor.config is new_config  # [CONTROL_PLANE_RESTART]
    assert agent.config is new_config  # [CONTROL_PLANE_RESTART]

