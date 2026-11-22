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


class FakeMonotonic:
    """Controllable stand-in for ``time.monotonic``.

    Returns ONE explicit value for every call and never advances on its own.
    Unlike ``mock.patch(..., side_effect=[...])``, which hands out one value per
    call and raises StopIteration past the end, this is immune to extra clock
    reads -- from the call under test, from a logging statement, or from a
    worker thread leaked by another test module in the same session (the patch
    target ``tools.workspace.worker.time`` IS the global ``time`` module, so the
    patch is process-wide).
    """

    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


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
            clock = FakeMonotonic(0.0)
            with mock.patch("tools.workspace.worker.time.monotonic", clock):
                thread._heartbeat_tick()      # now=0.0 -> throttled
                clock.now = 31.0
                thread._heartbeat_tick()      # now=31.0 -> fires
            # Select THIS worker's own heartbeat by name: the patched bus is
            # process-global, so a leaked worker's heartbeat lands here too
            # (see FakeMonotonic and the two payload tests below).
            hb_events = [
                e for e in self.heartbeat_events()
                if e.data.get("worker_name") == "w-hb"
            ]
            self.assertEqual(len(hb_events), 1)
            # In-memory heartbeat updated by the firing tick.
            self.assertIsNotNone(thread.last_heartbeat)

    def test_second_heartbeat_within_interval_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            thread = make_thread(tmp, name="w-hb2")
            thread._last_heartbeat_monotonic = 0.0
            # Call 1: now=31.0 -> fires (sets monotonic to 31.0).
            # Call 2: now=32.0 -> 32.0-31.0 < 30, suppressed.
            clock = FakeMonotonic(31.0)
            with mock.patch("tools.workspace.worker.time.monotonic", clock):
                thread._heartbeat_tick()      # now=31.0 -> fires, stamps 31.0
                clock.now = 32.0
                thread._heartbeat_tick()      # now=32.0 -> suppressed
            hb_events = [
                e for e in self.heartbeat_events()
                if e.data.get("worker_name") == "w-hb2"
            ]
            self.assertEqual(len(hb_events), 1)

    def test_leaked_worker_heartbeat_does_not_break_the_idle_count(self):
        """A worker leaked by another module must not inflate this count.

        While these tests hold their patches both ``global_event_bus`` and
        ``time.monotonic`` are process-global (see FakeMonotonic), so a
        WorkerThread left alive by another test module in the same session
        publishes its first WORKER_HEARTBEAT into THIS test's fake bus.  Its
        ``_last_heartbeat_monotonic`` is still 0.0 -- it has not ticked yet --
        so the fake clock at 31.0 clears the interval gate.  The leak is
        INJECTED here rather than waited for: the real one is a sub-millisecond
        race between a foreign thread's 2s tick and this test's patch window.
        """
        with tempfile.TemporaryDirectory() as tmp:
            thread = make_thread(tmp, name="w-hb-leak")
            thread._last_heartbeat_monotonic = 0.0
            leaked = make_thread(tmp, name="w-leaked", session_id="s2")
            leaked._last_heartbeat_monotonic = 0.0
            clock = FakeMonotonic(0.0)
            with mock.patch("tools.workspace.worker.time.monotonic", clock):
                thread._heartbeat_tick()      # now=0.0 -> throttled
                clock.now = 31.0
                leaked._heartbeat_tick()      # the leak: a foreign worker fires
                thread._heartbeat_tick()      # now=31.0 -> fires
            # Narrowed to this worker: the foreign heartbeat injected above is
            # in the bus, and it is not this worker's.
            hb_events = [
                e for e in self.heartbeat_events()
                if e.data.get("worker_name") == "w-hb-leak"
            ]
            self.assertEqual(len(hb_events), 1)

    def test_duplicate_delivery_for_the_same_worker_is_still_counted_twice(self):
        """The narrowed observable must still count a genuine second tick.

        Without this pair, a filter that counted nothing at all would pass the
        two idle tests.  Two ticks that each clear HEARTBEAT_INTERVAL_S for the
        SAME worker are two deliveries and must read 2.
        """
        with tempfile.TemporaryDirectory() as tmp:
            thread = make_thread(tmp, name="w-hb-dup")
            thread._last_heartbeat_monotonic = 0.0
            clock = FakeMonotonic(31.0)
            with mock.patch("tools.workspace.worker.time.monotonic", clock):
                thread._heartbeat_tick()      # 31.0 - 0.0  >= 30 -> fires
                clock.now = 62.0
                thread._heartbeat_tick()      # 62.0 - 31.0 >= 30 -> fires again
            hb_events = [
                e for e in self.heartbeat_events()
                if e.data.get("worker_name") == "w-hb-dup"
            ]
            self.assertEqual(len(hb_events), 2)

    def test_heartbeat_payload_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            thread = make_thread(tmp, name="w-hb3")
            thread._last_heartbeat_monotonic = 0.0
            clock = FakeMonotonic(31.0)
            with mock.patch("tools.workspace.worker.time.monotonic", clock):
                thread._heartbeat_tick()      # now=31.0 -> fires
            # Select THIS test's own worker by name.  A heartbeat published
            # into the (patched) global bus by a worker thread leaked from
            # another test module in the same session can arrive first, and it
            # is not the payload under test -- its shape says nothing about
            # _heartbeat_tick()'s output for w-hb3.
            evt = next(
                e for e in self.heartbeat_events()
                if e.data.get("worker_name") == "w-hb3"
            )
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
            thread._park_event.set()
            thread.status = "paused"
            clock = FakeMonotonic(31.0)
            with mock.patch("tools.workspace.worker.time.monotonic", clock):
                thread._heartbeat_tick()      # now=31.0 -> fires
            # Select THIS test's own worker by name, as in
            # test_heartbeat_payload_shape (lines 119-127): a heartbeat published
            # into the (patched) global bus by a worker thread leaked from
            # another test module in the same session can arrive first, and it
            # is not the payload under test.
            hb_events = [
                e for e in self.heartbeat_events()
                if e.data.get("worker_name") == "w-hb-p"
            ]
            self.assertEqual(len(hb_events), 1)
            self.assertEqual(hb_events[0].data["status"], "paused")
