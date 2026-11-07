"""
Phase 2A — WorkerLifecycleObserver tests (pure unit, no LLM / no Docker).

Verifies the observer:
  1. Receives all 9 worker lifecycle event types via a fake bus.
  2. Flags staleness exactly once per worker (stale-once semantics) and
     recovers when a fresh heartbeat arrives.
  3. Bounded rings: per-worker cap (50) and global cap (500).
  4. All public read APIs are lock-only copies (non-blocking under load).

Run:  python3 -m pytest tests/test_worker_lifecycle_events.py -q
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.events import EventType, create_event  # noqa: E402
from tools.workspace.worker_lifecycle import (  # noqa: E402
    WORKER_LIFECYCLE_EVENT_TYPES,
    PER_WORKER_RING_SIZE,
    GLOBAL_RING_SIZE,
    WorkerLifecycleObserver,
)


class FakeBus:
    """Minimal pub/sub bus that dispatches to type-matched callbacks."""

    def __init__(self):
        self.cbs = []

    def subscribe(self, event_type, callback):
        self.cbs.append((event_type, callback))

    def publish(self, evt):
        for et, cb in self.cbs:
            if et == evt.type:
                cb(evt)


def make_event(type_name, worker_name="w1", extra=None, source="test", session_id="s1"):
    # Generic payload satisfying every worker event validator (each checks
    # presence of its required keys; extras are allowed).
    data = {
        "worker_name": worker_name,
        "status": "ready",
        "reason": "test",
        "error": None,
    }
    if extra:
        data.update(extra)
    return create_event(EventType(type_name), data=data, source=source, session_id=session_id)


class TestObserverReceivesAllTypes(unittest.TestCase):
    def test_all_nine_types_recorded(self):
        bus = FakeBus()
        obs = WorkerLifecycleObserver(event_bus=bus)
        self.assertTrue(obs.ensure_subscribed())
        for t in WORKER_LIFECYCLE_EVENT_TYPES:
            bus.publish(make_event(t))
        events = obs.recent_events("w1")
        types = {e["type"] for e in events}
        self.assertEqual(types, set(WORKER_LIFECYCLE_EVENT_TYPES))
        self.assertEqual(len(events), len(WORKER_LIFECYCLE_EVENT_TYPES))
        # Global ring mirrors the per-worker ring.
        self.assertEqual(len(obs.recent_events()), len(WORKER_LIFECYCLE_EVENT_TYPES))


class TestStaleOnceSemantics(unittest.TestCase):
    def test_stale_flagged_once_then_cleared_by_fresh_heartbeat(self):
        bus = FakeBus()
        obs = WorkerLifecycleObserver(event_bus=bus)
        obs.ensure_subscribed()

        # Staleness is detected at ingestion: the heartbeat is 1200s old, so
        # the observer flags it immediately (exactly one worker_stale record).
        old = (datetime.now(timezone.utc) - timedelta(seconds=1200)).isoformat()
        bus.publish(make_event("worker_heartbeat", extra={"last_heartbeat": old}))
        self.assertTrue(obs.staleness("w1"))
        stale_records = [e for e in obs.recent_events("w1") if e["type"] == "worker_stale"]
        self.assertEqual(len(stale_records), 1)
        # Already flagged -> no new transitions on a re-scan.
        self.assertEqual(obs.check_stale_transitions(), 0)
        self.assertEqual(obs.last_heartbeat("w1"), old)

        # Fresh heartbeat clears staleness; no new stale on re-scan.
        fresh = datetime.now(timezone.utc).isoformat()
        bus.publish(make_event("worker_heartbeat", extra={"last_heartbeat": fresh}))
        self.assertFalse(obs.staleness("w1"))
        self.assertEqual(obs.check_stale_transitions(), 0)
        stale_records = [e for e in obs.recent_events("w1") if e["type"] == "worker_stale"]
        self.assertEqual(len(stale_records), 1)

    def test_check_stale_transitions_flags_late_staleness(self):
        # A worker whose heartbeat went stale WITHOUT further events is picked
        # up by check_stale_transitions (not at ingestion).
        bus = FakeBus()
        obs = WorkerLifecycleObserver(event_bus=bus)
        obs.ensure_subscribed()
        fresh = datetime.now(timezone.utc).isoformat()
        bus.publish(make_event("worker_heartbeat", extra={"last_heartbeat": fresh}))
        self.assertFalse(obs.staleness("w1"))
        later = datetime.now(timezone.utc) + timedelta(seconds=1300)
        self.assertEqual(obs.check_stale_transitions(now=later), 1)
        self.assertTrue(obs.staleness("w1"))
        # Stale-once: a second scan flags nothing new.
        self.assertEqual(obs.check_stale_transitions(now=later), 0)
        stale_records = [e for e in obs.recent_events("w1") if e["type"] == "worker_stale"]
        self.assertEqual(len(stale_records), 1)

    def test_terminal_event_suppresses_stale(self):
        bus = FakeBus()
        obs = WorkerLifecycleObserver(event_bus=bus)
        obs.ensure_subscribed()
        old = (datetime.now(timezone.utc) - timedelta(seconds=1200)).isoformat()
        bus.publish(make_event("worker_heartbeat", extra={"last_heartbeat": old}))
        bus.publish(make_event("worker_completed"))
        self.assertEqual(obs.check_stale_transitions(), 0)
        self.assertFalse(obs.staleness("w1"))


class TestRingCaps(unittest.TestCase):
    def test_per_worker_ring_capped_at_50(self):
        bus = FakeBus()
        obs = WorkerLifecycleObserver(event_bus=bus)
        obs.ensure_subscribed()
        for i in range(60):
            bus.publish(make_event("worker_status", worker_name="w1",
                                   extra={"seq": i}))
        self.assertEqual(len(obs.recent_events("w1")), PER_WORKER_RING_SIZE)

    def test_global_ring_capped_at_500(self):
        bus = FakeBus()
        obs = WorkerLifecycleObserver(event_bus=bus)
        obs.ensure_subscribed()
        for i in range(550):
            bus.publish(make_event("worker_status", worker_name=f"w{i % 10}",
                                   extra={"seq": i}))
        self.assertEqual(len(obs.recent_events()), GLOBAL_RING_SIZE)


class TestNonBlockingReads(unittest.TestCase):
    def test_read_apis_return_copies(self):
        bus = FakeBus()
        obs = WorkerLifecycleObserver(event_bus=bus)
        obs.ensure_subscribed()
        bus.publish(make_event("worker_heartbeat"))
        # Mutating the returned list must not affect internal state.
        events = obs.recent_events("w1")
        events.clear()
        self.assertEqual(len(obs.recent_events("w1")), 1)
        self.assertIsInstance(obs.last_heartbeat("w1"), str)
        self.assertIsNone(obs.last_heartbeat("missing"))
        self.assertFalse(obs.staleness("missing"))
