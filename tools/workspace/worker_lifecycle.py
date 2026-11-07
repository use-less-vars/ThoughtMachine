"""Worker lifecycle observer (Phase 2A).

Tracks worker lifecycle events published to the global event bus and derives
liveness/staleness signals from worker heartbeats.

Pure standard library + ``agent.events`` — no dependency on
``tools.workspace.worker`` (worker.py imports this module lazily, so there is
no import cycle).
"""

import threading
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from agent.events import EventType, global_event_bus

try:
    from agent.events import create_event
except ImportError:  # pragma: no cover - defensive
    create_event = None  # type: ignore

# Centralized literal defaults (Phase A consolidation). Re-exported here so
# existing importers/tests keep seeing the same names on this module.
from agent.config.defaults import (
    WORKER_LIFECYCLE_EVENT_TYPES,
    PER_WORKER_RING_SIZE,
    GLOBAL_RING_SIZE,
    HEARTBEAT_STALE_AFTER_S,
    HEARTBEAT_INTERVAL_S,
    WORKER_HUNG_GRACE_S,
)

# Alias used by consumers of the observer (tests, supervisor).
STALE_AFTER_S = HEARTBEAT_STALE_AFTER_S


class WorkerLifecycleObserver:
    """Subscribes to worker lifecycle events and keeps bounded rings of them.

    Thread-safe: all mutable state is guarded by a single lock. All public
    read APIs acquire the lock and return copies.
    """

    def __init__(
        self,
        event_bus=None,
        stale_after_s: int = HEARTBEAT_STALE_AFTER_S,
        *,
        stale_callback=None,
        hung_grace_s: int = WORKER_HUNG_GRACE_S,
    ):
        self._event_bus = event_bus if event_bus is not None else global_event_bus
        self._lock = threading.Lock()
        self._global_ring = deque(maxlen=GLOBAL_RING_SIZE)
        self._per_worker = {}  # worker_name -> deque of records
        self._last_heartbeat = {}  # worker_name -> heartbeat ISO string
        self._stale_flagged = {}  # worker_name -> True once stale reported
        self._terminal = {}  # worker_name -> True after completed/error
        self._stale_after_s = stale_after_s
        self._stale_callback = stale_callback
        self._hung_grace_s = max(0, hung_grace_s)
        self._worker_session = {}  # worker_name -> session_id
        self._hung_notified = {}  # worker_name -> True once hung emitted
        self._subscribed = False

    def ensure_subscribed(self) -> bool:
        """Subscribe to all worker lifecycle event types (idempotent)."""
        if self._subscribed:
            return True
        if self._event_bus is None or EventType is None or not hasattr(self._event_bus, "subscribe"):
            return False
        count = 0
        for type_name in WORKER_LIFECYCLE_EVENT_TYPES:
            try:
                self._event_bus.subscribe(EventType(type_name), self._on_event)
                count += 1
            except Exception:
                continue
        self._subscribed = count > 0
        return self._subscribed

    def _on_event(self, event) -> None:
        """EventBus subscriber callback — records the event and updates liveness."""
        data = dict(getattr(event, "data", {}) or {})
        worker = data.get("worker_name") or ""
        metadata = getattr(event, "metadata", None)
        ts = getattr(metadata, "timestamp", None) if metadata else None
        if isinstance(ts, datetime):
            ts_iso = ts.isoformat()
        elif isinstance(ts, str):
            ts_iso = ts
        else:
            ts_iso = data.get("timestamp") or datetime.now(timezone.utc).isoformat()

        raw_type = getattr(event, "type", "")
        type_str = getattr(raw_type, "value", None) or (raw_type if isinstance(raw_type, str) else "") or ""

        session_id = getattr(metadata, "session_id", None) if metadata else None

        record = {
            "type": type_str,
            "worker_name": worker,
            "timestamp": ts_iso,
            "data": data,
            "source": getattr(metadata, "source", None) if metadata else None,
            "session_id": session_id,
        }

        hung_action = None
        with self._lock:
            if type_str == "worker_heartbeat":
                hb = data.get("last_heartbeat") or ts_iso
                if hb:
                    self._last_heartbeat[worker] = hb
                    self._stale_flagged.pop(worker, None)
                    self._hung_notified.pop(worker, None)
                    self._terminal.pop(worker, None)
            if type_str in ("worker_completed", "worker_error"):
                self._terminal[worker] = True
            if worker:
                self._worker_session[worker] = session_id
            ring = self._per_worker.setdefault(worker, deque(maxlen=PER_WORKER_RING_SIZE))
            ring.append(record)
            self._global_ring.append(record)
            hung_action = self._check_stale_for(worker, datetime.now(timezone.utc))
        if hung_action:
            self._emit_hung_actions([hung_action])

    def _heartbeat_age(self, worker: str, now: datetime) -> Optional[float]:
        """Age in seconds of the worker's last heartbeat (None when unknown)."""
        hb = self._last_heartbeat.get(worker)
        if not hb:
            return None
        try:
            hb_dt = datetime.fromisoformat(hb)
            if hb_dt.tzinfo is None:
                hb_dt = hb_dt.replace(tzinfo=timezone.utc)
            return (now - hb_dt).total_seconds()
        except (ValueError, TypeError):
            return None

    def _is_stale(self, worker: str, now: datetime) -> bool:
        """True when the worker's last heartbeat is older than the threshold."""
        age = self._heartbeat_age(worker, now)
        return age is not None and age > self._stale_after_s

    def _is_hung(self, worker: str, now: datetime) -> bool:
        """True when the worker is stale AND past the hung grace period."""
        age = self._heartbeat_age(worker, now)
        return age is not None and age > self._stale_after_s + self._hung_grace_s

    def _hung_action_for(self, worker: str, now: datetime) -> Optional[tuple]:
        """Build a one-shot hung notification for an already-stale worker.

        Must be called with ``self._lock`` held — private helper. Emits at
        most once per worker (tracked in ``_hung_notified``). Returns
        ``(worker, info)`` when the worker is hung and not yet notified, else
        None.
        """
        if not self._is_hung(worker, now):
            return None
        if self._hung_notified.get(worker):
            return None
        self._hung_notified[worker] = True
        info = {
            "reason": "stale_heartbeat",
            "last_heartbeat": self._last_heartbeat.get(worker),
            "heartbeat_age_seconds": self._heartbeat_age(worker, now),
            "session_id": self._worker_session.get(worker, ""),
        }
        return (worker, info)

    def _check_stale_for(self, worker: str, now: datetime) -> Optional[tuple]:
        """Flag a worker as stale (once) if its heartbeat is too old.

        Must be called with ``self._lock`` held — private helper. Returns a
        hung action ``(worker, info)`` when the worker is past the hung grace
        period (so the caller can emit outside the lock), else None.
        """
        if not worker or self._terminal.get(worker) or self._stale_flagged.get(worker):
            return None
        if self._is_stale(worker, now):
            self._stale_flagged[worker] = True
            rec = {
                "type": "worker_stale",
                "worker_name": worker,
                "timestamp": now.isoformat(),
                "data": {"last_heartbeat": self._last_heartbeat.get(worker)},
                "source": "worker_lifecycle_observer",
                "session_id": None,
            }
            ring = self._per_worker.setdefault(worker, deque(maxlen=PER_WORKER_RING_SIZE))
            ring.append(rec)
            self._global_ring.append(rec)
            return self._hung_action_for(worker, now)
        return None

    def check_stale_transitions(self, now: Optional[datetime] = None) -> int:
        """Scan known workers, flag newly-stale ones and emit hung actions.

        Returns the number of newly-flagged stale workers. Hung notifications
        (WORKER_TIMEOUT event + ``stale_callback``) are re-evaluated for
        already-flagged workers on every scan, so a grace period that elapses
        between scans is still honored; each worker is notified at most once.
        """
        now = now or datetime.now(timezone.utc)
        count = 0
        pending = []
        with self._lock:
            for worker in list(self._last_heartbeat.keys()):
                if self._terminal.get(worker):
                    continue
                if self._is_stale(worker, now):
                    if not self._stale_flagged.get(worker):
                        self._stale_flagged[worker] = True
                        rec = {
                            "type": "worker_stale",
                            "worker_name": worker,
                            "timestamp": now.isoformat(),
                            "data": {"last_heartbeat": self._last_heartbeat.get(worker)},
                            "source": "worker_lifecycle_observer",
                            "session_id": None,
                        }
                        ring = self._per_worker.setdefault(worker, deque(maxlen=PER_WORKER_RING_SIZE))
                        ring.append(rec)
                        self._global_ring.append(rec)
                        count += 1
                    action = self._hung_action_for(worker, now)
                    if action:
                        pending.append(action)
        self._emit_hung_actions(pending)
        return count

    def _emit_hung_actions(self, pending: list) -> None:
        """Publish WORKER_TIMEOUT and invoke the stale callback (never raises)."""
        for worker, info in pending or []:
            try:
                if self._event_bus is not None and create_event is not None:
                    evt = create_event(
                        EventType.WORKER_TIMEOUT,
                        data={
                            "worker_name": worker,
                            "worker_id": worker,
                            "reason": "stale_heartbeat",
                            "last_heartbeat": info.get("last_heartbeat"),
                            "heartbeat_age_seconds": round(info.get("heartbeat_age_seconds") or 0.0, 1),
                            "session_id": info.get("session_id") or "",
                            "source": "worker_lifecycle_observer",
                        },
                        source="worker_lifecycle_observer",
                        session_id=info.get("session_id") or "",
                    )
                    if evt is not None:
                        self._event_bus.publish(evt)
            except Exception:
                pass
            if self._stale_callback is not None:
                try:
                    self._stale_callback(worker, info)
                except Exception:
                    pass

    def recent_events(self, worker_name: Optional[str] = None) -> list:
        """Return recent lifecycle records (per-worker or global ring)."""
        with self._lock:
            if worker_name:
                return list(self._per_worker.get(worker_name, []))
            return list(self._global_ring)

    def staleness(self, worker_name: str) -> bool:
        """True when the worker is currently considered stale."""
        with self._lock:
            if self._terminal.get(worker_name):
                return False
            if self._stale_flagged.get(worker_name):
                return True
            return self._is_stale(worker_name, datetime.now(timezone.utc))

    def last_heartbeat(self, worker_name: str) -> Optional[str]:
        """Return the worker's most recent heartbeat ISO string (or None)."""
        with self._lock:
            return self._last_heartbeat.get(worker_name)
