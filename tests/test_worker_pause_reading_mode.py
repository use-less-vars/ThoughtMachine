"""Pause reaches the WORKER's agent, not just the main agent's.

Reading mode is the pause mechanism (``Agent.enter_reading_mode('pause')``
narrows the tool allow-list to ``['Respond']``; ``leave_reading_mode('pause')``
releases it owner-scoped). The Pause button therefore has to reach every
worker's agent too, on all four entry points:

* ``WorkerThread.pause()``      -- REST/direct pause of one worker
* ``WorkerThread.resume()``     -- REST/direct resume of one worker
* ``_poll_command()``           -- the cross-process ``command.json`` twins of
  the two above (a worker in another process never sees a method call)
* ``WebAgentBridge.pause()`` / ``resume()`` -- the Pause button fanning out to
  the session's workers, and the resume path releasing the main agent

The bridge used to SKIP workers running an async (``submit_query``) job, on the
premise that a pause would truncate the turn. Reading mode makes that premise
obsolete: the worker's agent concludes its turn with a Respond instead of being
cut off. ``TestBridgePauseReachesAsyncJobWorker`` pins that the skip is gone.

Construction: a REAL ``WorkerThread`` that is never ``start()``ed (the same
``_unstarted_thread`` pattern as ``tests/test_worker_operation_semantics.py``)
plus a REAL ``Agent`` for the reading-mode assertions, so the test measures the
production restriction, not a mock of it.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import web_ui.backend.bridge as bridge_mod
from agent.config.models import AgentConfig
from tools.workspace.command_channel import write_command
from agent.core.agent import Agent
from session.store import FileSystemSessionStore
from tools.workspace.worker_thread import WorkerThread
from web_ui.backend.bridge import WebAgentBridge


def _make_agent(session_id: str = "test-worker-pause") -> Agent:
    return Agent(
        config=AgentConfig(api_key="test-key", enable_logging=False),
        session_id=session_id,
    )


def _unstarted_thread(tmp_path, name: str = "test-worker",
                      session_id: str = "sess-worker-pause") -> WorkerThread:
    """A REAL ``WorkerThread`` that is never ``start()``ed.

    One lifecycle transition is driven synchronously per test, so a live
    ``run()`` loop would only add noise (it rewrites status.json and polls
    command.json on its own cadence).
    """
    return WorkerThread(
        name=name,
        definition={"name": name, "system_prompt": "You are a test worker."},
        agent_config={},
        workspace_dir=Path(tmp_path),
        session_id=session_id,
        container_manager=None,
        max_container_count=4,
        max_token_usage=None,
        max_runtime_s=None,
    )


def _write_command(thread: WorkerThread, action: str) -> None:
    """Write a FENCED command where the worker actually polls for it."""
    thread._worker_dir.mkdir(parents=True, exist_ok=True)
    assert write_command(thread._worker_dir, action, thread._fence()) is True


# ══════════════════════════════════════════════════════════════════════════════
# WorkerThread.pause() / resume()
# ══════════════════════════════════════════════════════════════════════════════

class TestWorkerPauseTouchesAgent:
    def test_pause_puts_the_workers_agent_in_reading_mode(self, tmp_path):
        thread = _unstarted_thread(tmp_path)
        agent = _make_agent()
        thread._agent = agent
        assert agent.state.is_tool_allowed("ContainerExecTool") is True

        thread.pause()

        assert agent.state.restriction_reason == "pause"
        assert agent.state.restrictions_active is True
        assert agent.state.get_allowed_tools() == ["Respond"]
        assert agent.state.is_tool_allowed("ContainerExecTool") is False
        assert agent._pause_requested is True
        # The park signal is still raised: pausing the agent must not stop the
        # worker thread from parking.
        assert thread._park_event.is_set() is True

    def test_pause_does_not_set_the_status_it_is_the_loop_that_does(self, tmp_path):
        """KNOWN CAVEAT: ``pause()`` leaves ``status`` alone."""
        thread = _unstarted_thread(tmp_path)
        thread._agent = _make_agent()
        before = thread.status

        thread.pause()

        # The 'paused' status is written by the worker's run loop after it has
        # actually parked (_park_event wait), not by pause(). Pinned so nobody
        # "fixes" the reading-mode wiring by racing the loop's status write.
        assert thread.status == before

    def test_resume_releases_the_pause_owned_restriction(self, tmp_path):
        thread = _unstarted_thread(tmp_path)
        agent = _make_agent()
        thread._agent = agent
        thread.pause()
        thread.status = "paused"          # what the run loop would have written

        assert thread.resume() is True

        assert agent.state.restriction_reason is None
        assert agent.state.restrictions_active is False
        assert agent.state.is_tool_allowed("ContainerExecTool") is True
        assert agent._pause_requested is False
        assert thread._park_event.is_set() is False
        assert thread.status == "ready"

    def test_resume_does_not_release_a_token_restriction(self, tmp_path):
        """A token monitor that re-attributed the restriction outlives a resume."""
        thread = _unstarted_thread(tmp_path)
        agent = _make_agent()
        thread._agent = agent
        thread.pause()
        agent.state.restrict("token")     # token monitor takes ownership
        thread.status = "paused"

        assert thread.resume() is True

        assert agent.state.restriction_reason == "token"
        assert agent.state.is_tool_allowed("SummarizeTool") is True
        # The worker itself still resumes -- only the foreign restriction stays.
        assert thread.status == "ready"
        assert thread._park_event.is_set() is False

    def test_refused_resume_leaves_reading_mode_in_force(self, tmp_path):
        """Resume of a killed worker returns False before touching any state."""
        thread = _unstarted_thread(tmp_path)
        agent = _make_agent()
        thread._agent = agent
        thread.pause()
        thread._stop_event.set()

        assert thread.resume() is False

        assert agent.state.restriction_reason == "pause"
        assert agent._pause_requested is True

    def test_pause_and_resume_without_an_agent_do_not_raise(self, tmp_path):
        thread = _unstarted_thread(tmp_path)
        assert thread._agent is None

        thread.pause()
        assert thread._park_event.is_set() is True

        assert thread.resume() is True
        assert thread._park_event.is_set() is False
        assert thread.status == "ready"


# ══════════════════════════════════════════════════════════════════════════════
# _poll_command(): the cross-process twins
# ══════════════════════════════════════════════════════════════════════════════

class TestPollCommandReachesAgent:
    def test_pause_command_puts_the_workers_agent_in_reading_mode(self, tmp_path):
        thread = _unstarted_thread(tmp_path)
        agent = _make_agent()
        thread._agent = agent
        _write_command(thread, "pause")

        thread._poll_command()

        assert agent.state.restriction_reason == "pause"
        assert agent.state.is_tool_allowed("ContainerExecTool") is False
        assert thread._park_event.is_set() is True
        # The command file is consumed (not re-polled forever).
        assert (thread._worker_dir / "command.json").exists() is False

    def test_resume_command_releases_the_pause_owned_restriction(self, tmp_path):
        thread = _unstarted_thread(tmp_path)
        agent = _make_agent()
        thread._agent = agent
        _write_command(thread, "pause")
        thread._poll_command()
        thread.status = "paused"
        _write_command(thread, "resume")

        thread._poll_command()

        assert agent.state.restriction_reason is None
        assert agent.state.is_tool_allowed("ContainerExecTool") is True
        assert thread._park_event.is_set() is False
        assert thread.status == "ready"

    def test_resume_command_keeps_a_token_restriction(self, tmp_path):
        thread = _unstarted_thread(tmp_path)
        agent = _make_agent()
        thread._agent = agent
        thread.pause()
        agent.state.restrict("token")
        _write_command(thread, "resume")

        thread._poll_command()

        assert agent.state.restriction_reason == "token"

    def test_poll_command_without_an_agent_does_not_raise(self, tmp_path):
        thread = _unstarted_thread(tmp_path)
        _write_command(thread, "pause")

        thread._poll_command()
        assert thread._park_event.is_set() is True

        _write_command(thread, "resume")
        thread._poll_command()
        assert thread._park_event.is_set() is False


# ══════════════════════════════════════════════════════════════════════════════
# WebAgentBridge.pause() / resume()
# ══════════════════════════════════════════════════════════════════════════════

class _FakeWorkerThread:
    """Stand-in for a registry thread: records pause/resume calls."""

    def __init__(self, status: str, async_job: bool = False):
        self.status = status
        self._current_query_id = ("job-1",) if async_job else None
        self._current_reply_queue = None
        self.pause_calls = 0
        self.resume_calls = 0

    def pause(self) -> None:
        self.pause_calls += 1

    def resume(self) -> bool:
        self.resume_calls += 1
        return True


class _FakeAgent:
    """Main-agent double: records the reading-mode release."""

    def __init__(self):
        self._pause_requested = True
        self.leave_calls = []

    def leave_reading_mode(self, reason="pause") -> bool:
        self.leave_calls.append(reason)
        return True


def _bridge_with(tmp_path):
    bridge = WebAgentBridge(
        event_callback=lambda e: None,
        session_store=FileSystemSessionStore(),
    )
    bridge._session_id = "sess-bridge"
    bridge._controller = None
    bridge._running = True
    bridge._thread = threading.current_thread()   # alive -> is_running True
    return bridge


def _patch_registry(monkeypatch, entries):
    monkeypatch.setattr(bridge_mod, "WORKER_BUS_AVAILABLE", True)
    monkeypatch.setattr(bridge_mod, "_worker_registry", entries)
    monkeypatch.setattr(bridge_mod, "_registry_lock", threading.Lock())
    logged = []

    def recorder(level, category, message, *args, **kwargs):
        logged.append((level, category, message))

    monkeypatch.setattr(bridge_mod, "log", recorder)
    return logged


class TestBridgePauseReachesAsyncJobWorker:
    def test_pause_is_not_skipped_for_a_busy_async_job_worker(self, tmp_path, monkeypatch):
        bridge = _bridge_with(tmp_path)
        busy = _FakeWorkerThread("busy", async_job=True)
        idle = _FakeWorkerThread("ready")
        other_session = _FakeWorkerThread("busy", async_job=True)
        logged = _patch_registry(monkeypatch, {
            ("sess-bridge", "busy-worker", 1): busy,
            ("sess-bridge", "idle-worker", 1): idle,
            ("sess-other", "foreign-worker", 1): other_session,
        })

        bridge.pause()

        # The skip used to drop exactly this thread ('async job in progress').
        assert busy.pause_calls == 1
        assert idle.pause_calls == 1
        assert other_session.pause_calls == 0
        assert not any("Skip pause" in message for _lvl, _cat, message in logged)
        # The main agent still gets the pause request (request_pause ->
        # enter_reading_mode('pause')); asserted via the park event here since
        # no agent double is installed.
        assert bridge._park_event.is_set() is False


class TestBridgeResumeReleasesReadingMode:
    def test_resume_releases_the_main_agents_reading_mode(self, tmp_path, monkeypatch):
        bridge = _bridge_with(tmp_path)
        agent = _FakeAgent()
        bridge._agent = agent
        _patch_registry(monkeypatch, {})

        bridge.resume()

        assert agent.leave_calls == ["pause"]
        assert agent._pause_requested is False
        assert bridge._park_event.is_set() is True

    def test_resume_tolerates_an_agent_double_without_the_verb(self, tmp_path, monkeypatch):
        """Older doubles/embeddings without leave_reading_mode must not break resume."""

        class _Bare:
            def __init__(self):
                self._pause_requested = True

        bridge = _bridge_with(tmp_path)
        bare = _Bare()
        bridge._agent = bare
        _patch_registry(monkeypatch, {})

        bridge.resume()

        assert bare._pause_requested is False
        assert bridge._park_event.is_set() is True

    def test_resume_resumes_paused_workers_only(self, tmp_path, monkeypatch):
        bridge = _bridge_with(tmp_path)
        bridge._agent = _FakeAgent()
        paused = _FakeWorkerThread("paused")
        ready = _FakeWorkerThread("ready")
        _patch_registry(monkeypatch, {
            ("sess-bridge", "paused-worker", 1): paused,
            ("sess-bridge", "ready-worker", 1): ready,
        })

        bridge.resume()

        assert paused.resume_calls == 1
        assert ready.resume_calls == 0


if __name__ == "__main__":   # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
