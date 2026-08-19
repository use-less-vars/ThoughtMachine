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

# Lifecycle event types this observer subscribes to (Phase 2A)
WORKER_LIFECYCLE_EVENT_TYPES = (
    "worker_spawned",
    "worker_status",
    "worker_running",
    "worker_heartbeat",
    "worker_stopping",
    "worker_completed",
    "worker_error",
    "worker_timeout",
    "worker_partial_result",
)

# Per-worker ring buffer size and global ring buffer size
PER_WORKER_RING_SIZE = 50
GLOBAL_RING_SIZE = 500

# A worker whose last heartbeat is older than this is considered stale
HEARTBEAT_STALE_AFTER_S = 600


class WorkerLifecycleObserver:
    """Subscribes to worker lifecycle events and keeps bounded rings of them.

    Thread-safe: all mutable state is guarded by a single lock. All public
    read APIs acquire the lock and return copies.
    """

    def __init__(self, event_bus=None, stale_after_s: int = HEARTBEAT_STALE_AFTER_S):
        self._event_bus = event_bus if event_bus is not None else global_event_bus
        self._lock = threading.Lock()
        self._global_ring = deque(maxlen=GLOBAL_RING_SIZE)
        self._per_worker = {}  # worker_name -> deque of records
        self._last_heartbeat = {}  # worker_name -> heartbeat ISO string
        self._stale_flagged = {}  # worker_name -> True once stale reported
        self._terminal = {}  # worker_name -> True after completed/error
        self._stale_after_s = stale_after_s
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

        record = {
            "type": type_str,
            "worker_name": worker,
            "timestamp": ts_iso,
            "data": data,
            "source": getattr(metadata, "source", None) if metadata else None,
            "session_id": getattr(metadata, "session_id", None) if metadata else None,
        }

        with self._lock:
            if type_str == "worker_heartbeat":
                hb = data.get("last_heartbeat") or ts_iso
                if hb:
                    self._last_heartbeat[worker] = hb
                    self._stale_flagged.pop(worker, None)
                    self._terminal.pop(worker, None)
            if type_str in ("worker_completed", "worker_error"):
                self._terminal[worker] = True
            ring = self._per_worker.setdefault(worker, deque(maxlen=PER_WORKER_RING_SIZE))
            ring.append(record)
            self._global_ring.append(record)
            self._check_stale_for(worker, datetime.now(timezone.utc))

    def _is_stale(self, worker: str, now: datetime) -> bool:
        """True when the worker's last heartbeat is older than the threshold."""
        hb = self._last_heartbeat.get(worker)
        if not hb:
            return False
        try:
            hb_dt = datetime.fromisoformat(hb)
            if hb_dt.tzinfo is None:
                hb_dt = hb_dt.replace(tzinfo=timezone.utc)
            return (now - hb_dt).total_seconds() > self._stale_after_s
        except (ValueError, TypeError):
            return False

    def _check_stale_for(self, worker: str, now: datetime) -> None:
        """Flag a worker as stale (once) if its heartbeat is too old.

        Must be called with ``self._lock`` held — private helper.
        """
        if not worker or self._terminal.get(worker) or self._stale_flagged.get(worker):
            return
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

    def check_stale_transitions(self, now: Optional[datetime] = None) -> int:
        """Scan known workers and flag any newly-stale ones; returns count."""
        now = now or datetime.now(timezone.utc)
        count = 0
        with self._lock:
            for worker in list(self._last_heartbeat.keys()):
                if self._stale_flagged.get(worker) or self._terminal.get(worker):
                    continue
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
                    count += 1
        return count

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
