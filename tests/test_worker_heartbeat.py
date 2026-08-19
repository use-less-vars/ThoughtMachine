"""
Phase 2A — worker heartbeat publication tests (no real LLM / no Docker).

Exercises WorkerThread._heartbeat_tick(): throttling via
_last_heartbeat_monotonic, in-memory last_heartbeat update, and
WORKER_HEARTBEAT publication to the global event bus with the expected
payload shape (worker_name, session_id, status, last_heartbeat,
current_context_tokens, max_context_tokens).

Run:  python3 -m pytest tests/test_worker_heartbeat.py -q
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.workspace.worker import WorkerThread  # noqa: E402
from agent.events import EventType  # noqa: E402

SYS_PROMPT = "You are a helpful worker assistant."


def make_thread(workspace_dir, name="w-hb", timeout=60, session_id="s1"):
    return WorkerThread(
        name=name,
        definition={"system_prompt": SYS_PROMPT},
        agent_config={"model": "gpt-4o"},
        workspace_dir=Path(workspace_dir),
        session_id=session_id,
        timeout_seconds=timeout,
    )


class _FakeEventBus:
    """Callable stand-in for agent.events.EventBus (no real subscription)."""

    def __init__(self, *a, **k):
        pass

    def publish(self, *a, **k):
        return None


class RecordingFake:
    """Records events published to the (patched) global event bus."""

    def __init__(self):
        self.events = []

    def publish(self, evt):
        self.events.append(evt)


class _RunSafetyPatches(unittest.TestCase):
    """Patches event plumbing; global bus is a RecordingFake."""

    def setUp(self):
        self.fake_bus = RecordingFake()
        patchers = [
            mock.patch("tools.workspace.worker.EventBus", new=_FakeEventBus),
            mock.patch("tools.workspace.worker.register_worker_event_bus",
                       new=lambda *a, **k: None),
            mock.patch("tools.workspace.worker.unregister_worker_event_bus",
                       new=lambda *a, **k: None),
            mock.patch("tools.workspace.worker.global_event_bus", new=self.fake_bus),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        super().setUp()

    def heartbeat_events(self):
        return [e for e in self.fake_bus.events if e.type == EventType.WORKER_HEARTBEAT]


class TestIdleHeartbeat(_RunSafetyPatches):
    """_heartbeat_tick fires at most once per HEARTBEAT_INTERVAL_S."""

    def test_heartbeat_fires_once_then_throttled(self):
        with tempfile.TemporaryDirectory() as tmp:
            thread = make_thread(tmp, name="w-hb")
            thread._last_heartbeat_monotonic = 0.0
            # Call 1: now=0.0 -> 0.0-0.0 < 30, no fire (throttle).
            # Call 2: now=31.0 -> 31.0-0.0 >= 30, fires.
            with mock.patch("tools.workspace.worker.time.monotonic",
                            side_effect=[0.0, 31.0]):
                thread._heartbeat_tick()
                thread._heartbeat_tick()
            hb_events = self.heartbeat_events()
            self.assertEqual(len(hb_events), 1)
            # In-memory heartbeat updated by the firing tick.
            self.assertIsNotNone(thread.last_heartbeat)

    def test_second_heartbeat_within_interval_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            thread = make_thread(tmp, name="w-hb2")
            thread._last_heartbeat_monotonic = 0.0
            # Call 1: now=31.0 -> fires (sets monotonic to 31.0).
            # Call 2: now=32.0 -> 32.0-31.0 < 30, suppressed.
            with mock.patch("tools.workspace.worker.time.monotonic",
                            side_effect=[31.0, 32.0]):
                thread._heartbeat_tick()
                thread._heartbeat_tick()
            self.assertEqual(len(self.heartbeat_events()), 1)

    def test_heartbeat_payload_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            thread = make_thread(tmp, name="w-hb3")
            thread._last_heartbeat_monotonic = 0.0
            with mock.patch("tools.workspace.worker.time.monotonic",
                            side_effect=[31.0]):
                thread._heartbeat_tick()
            evt = self.heartbeat_events()[0]
            data = evt.data
            self.assertEqual(data["worker_name"], "w-hb3")
            self.assertEqual(data["session_id"], "s1")
            self.assertEqual(data["status"], "ready")
            self.assertEqual(data["last_heartbeat"], thread.last_heartbeat)
            self.assertIn("current_context_tokens", data)
            self.assertIn("max_context_tokens", data)
            # Event metadata carries source/session.
            self.assertEqual(evt.metadata.source, "worker:w-hb3")
            self.assertEqual(evt.metadata.session_id, "s1")


class TestPausedHeartbeat(_RunSafetyPatches):
    """Heartbeat continues while paused."""

    def test_paused_heartbeat_fires(self):
        with tempfile.TemporaryDirectory() as tmp:
            thread = make_thread(tmp, name="w-hb-p")
            thread._last_heartbeat_monotonic = 0.0
            thread._pause_event.set()
            thread.status = "paused"
            with mock.patch("tools.workspace.worker.time.monotonic",
                            side_effect=[31.0]):
                thread._heartbeat_tick()
            hb_events = self.heartbeat_events()
            self.assertEqual(len(hb_events), 1)
            self.assertEqual(hb_events[0].data["status"], "paused")
