"""A pause that lands MID-TURN must not throw the turn's work away.

The pause contract
------------------
``WorkerThread.pause()`` is COOPERATIVE.  It raises the park signal *and* puts
the worker's agent into reading mode (``Agent.enter_reading_mode("pause")`` --
tool allow-list narrowed to ``['Respond']``), whose whole purpose is to make the
agent CONCLUDE the current turn with a Respond instead of being cut off.  That
wiring is pinned by ``tests/test_worker_pause_reading_mode.py``.

This module pins the OTHER half of the same contract, which lives in the
worker's tool loop (``WorkerThread._run_tool_loop``): when the park signal is
observed between two events of a live turn, the loop must let that turn finish
and keep the answer it produced, then park.  The alternative -- replacing the
turn with a synthetic ``{"status": "paused", ...}`` envelope and dropping the
agent's content -- discards work the user already paid for, and it is exactly
the behaviour the bridge's pause fan-out was changed to stop worrying about
(``web_ui/backend/bridge.py`` may now pause a worker that is mid async job,
because a paused turn is supposed to end with a real Respond).

Determinism
-----------
The pause is raised from INSIDE the event generator, i.e. between two known
events, so there is no wall-clock window, no sleep and no thread race: the
pause lands at the same point in the turn on every run.

Construction: a REAL ``WorkerThread`` that is never ``start()``ed (the same
``_unstarted_thread`` pattern as ``tests/test_worker_pause_reading_mode.py`` and
``tests/test_worker_operation_semantics.py``) plus a scripted agent stand-in.
No Docker, no network, no live agent loop.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.workspace.worker_thread import WorkerThread

# The answer the turn under way produces.  Deliberately distinctive so a test
# failure names exactly what was lost.
MID_TURN_WORK = "THE TURN'S OWN ANSWER: half-finished analysis, now complete."


def _unstarted_thread(tmp_path) -> WorkerThread:
    """A REAL ``WorkerThread`` that is never ``start()``ed.

    ``_run_tool_loop`` is driven directly, so a live ``run()`` loop would only
    race the assertions by rewriting status.json on its own cadence.
    """
    return WorkerThread(
        name="test-worker-pause-midturn",
        definition={"name": "test-worker-pause-midturn",
                    "system_prompt": "You are a test worker."},
        agent_config={},
        workspace_dir=Path(tmp_path),
        session_id="sess-pause-midturn",
        container_manager=None,
        max_container_count=4,
        max_token_usage=None,
        max_runtime_s=None,
    )


class _ScriptedAgent:
    """Agent stand-in whose turn pauses ITSELF at an exact point.

    ``process_query`` is the production event-generator seam: the loop drains
    it event by event.  The generator calls the REAL ``thread.pause()`` between
    two yielded events, so the pause is guaranteed to land mid-turn (the turn is
    already under way) and before the concluding event (so a loop that honours
    the pause cooperatively still has everything it needs to finish).

    The scripted tail models what reading mode makes the real agent do: stop
    calling tools and conclude with a Respond.
    """

    def __init__(self, thread: WorkerThread, park=None) -> None:
        self._thread = thread
        # The production park signal to raise mid-turn: pause() by default,
        # detach() for the non-cooperative pin below.
        self._park = park or thread.pause
        self.reading_mode_reasons: list[str] = []
        self.request_pause_calls = 0
        self.parked_mid_turn = False

    # ── the reading-mode seam pause() drives ────────────────────────────────
    def enter_reading_mode(self, reason: str = "pause") -> None:
        self.reading_mode_reasons.append(reason)

    def request_pause(self) -> None:
        self.request_pause_calls += 1
        self.enter_reading_mode("pause")

    # ── the turn ────────────────────────────────────────────────────────────
    def process_query(self, query: str):
        # 1. the turn is already under way
        yield {"type": "assistant_message", "content": "working on it..."}

        # 2. the Pause button lands HERE -- between two events, racelessly
        self._park()
        self.parked_mid_turn = True

        # 3. in reading mode the agent concludes the turn with a Respond
        yield {
            "type": "agent_responded",
            "content": MID_TURN_WORK,
            "status": "success",
            "confidence": "high",
        }


def _capture_events(thread: WorkerThread) -> list[tuple[str, dict]]:
    """Record published events without touching an event bus."""
    events: list[tuple[str, dict]] = []
    thread._publish_event = lambda etype, data: events.append((etype, dict(data)))
    return events


class _NullStateBridge:
    """Minimal stand-in for the presenter bridge, to keep the loop's
    presenter lazy-init OUT of this test: the pause/park decision is taken
    before any presenter work, so wiring one in would only add noise."""

    context_length = 0


def _wire(thread: WorkerThread) -> list[tuple[str, dict]]:
    """Isolate the loop under test from the presenter and the event bus."""
    thread._state_bridge = _NullStateBridge()
    return _capture_events(thread)


def test_pause_mid_turn_keeps_the_turns_answer(tmp_path):
    """A pause observed mid-turn must yield the agent's answer, not a stub."""
    thread = _unstarted_thread(tmp_path)
    events = _wire(thread)
    agent = _ScriptedAgent(thread)
    thread._agent = agent

    reply = thread._run_tool_loop("do the thing")

    # The pause really did land mid-turn, and it really did reach the agent
    # (pause() itself, the loop's own request_pause(), and the cross-process
    # twin riding in the same command.json the loop polls).
    assert agent.parked_mid_turn is True
    assert thread._park_event.is_set() is True
    assert agent.reading_mode_reasons, agent.reading_mode_reasons
    assert set(agent.reading_mode_reasons) == {"pause"}, agent.reading_mode_reasons

    # THE POINT: the turn's own answer survives the pause.
    assert reply == MID_TURN_WORK, (
        "pause() discarded the mid-turn result: the tool loop returned "
        f"{reply!r} instead of the agent's answer {MID_TURN_WORK!r}"
    )
    assert "Worker paused by user" not in reply
    # ...because the concluding event was actually PROCESSED, not skipped.
    assert thread._respond_metadata.get("status") == "success"

    # ...and the worker still parks, reporting the paused status.
    assert thread.status == "paused"
    paused = [data for etype, data in events if etype == "worker_paused"]
    assert paused and paused[-1]["status"] == "paused", events
    assert paused[-1]["worker_name"] == "test-worker-pause-midturn"


def test_detach_mid_turn_still_truncates_the_turn(tmp_path):
    """DETACH is NOT a pause: it parks NOW and keeps the terminal envelope.

    ``detach()`` raises the same park flag but is the explicit "park now"
    signal -- it also cancels the in-flight turn (``_turn_cancel``).  The loop
    must keep truncating it: waiting for a turn to wind down before honouring a
    detach would delay the detach ack for as long as the turn runs.  This pin
    exists so the cooperative-pause fix cannot silently make detach patient.
    """
    thread = _unstarted_thread(tmp_path)
    _wire(thread)
    agent = _ScriptedAgent(thread, park=lambda: (
        setattr(thread, "_detach_requested", True), thread._park_event.set()))
    thread._agent = agent

    reply = thread._run_tool_loop("do the thing")

    assert agent.parked_mid_turn is True
    assert json.loads(reply) == {
        "status": "paused",
        "message": "Worker paused by user",
    }, reply
    # The turn was abandoned, not drained to its conclusion: the concluding
    # agent_responded event was never processed.
    assert thread._respond_metadata == {}
