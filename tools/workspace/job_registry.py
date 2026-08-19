# tools/workspace/job_registry.py
"""
Worker job registry (Phase 2B) -- in-memory, thread-safe tracking of
non-blocking worker jobs (action='submit_query').

The registry mirrors WORKER_* global events into a small bounded store so
callers that used ``submit_query`` (which returns immediately with a job_id)
can poll progress and results via ``job_status`` without blocking on the
worker. The full result envelope is stored so callers can inspect the final
answer after the worker completes.

Stdlib + agent.events only. Every public method is safe to call when the
event bus is unavailable; nothing here ever raises for the caller.
"""

from __future__ import annotations

import copy
import threading
from datetime import datetime, timezone
from typing import Optional

try:
    from agent.events import EventType, global_event_bus
except ImportError:  # pragma: no cover - event stack unavailable
    EventType = None
    global_event_bus = None

# Maximum number of job records kept (bounded; oldest evicted when full).
JOB_REGISTRY_MAX_JOBS = 200
# Preview cap for completed-job results (full envelope stored separately).
PREVIEW_CAP = 8000
# Preview cap for partial/error/timeout snippets.
PARTIAL_PREVIEW_CAP = 2000

# Worker event type names the registry mirrors into job status transitions.
_JOB_EVENT_TYPES = (
    "worker_running",
    "worker_partial_result",
    "worker_completed",
    "worker_timeout",
    "worker_error",
)


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


class WorkerJobRegistry:
    """Thread-safe, bounded, event-fed store of non-blocking worker jobs.

    Job records are keyed by ``query_id`` (the job_id returned by
    action='submit_query'). All read accessors return deep copies, so
    callers can never mutate the registry's internal state.
    """

    def __init__(self, event_bus=None):
        self._event_bus = event_bus if event_bus is not None else global_event_bus
        self._lock = threading.RLock()
        self._jobs: dict = {}
        self._subscribed = False

    # -- subscription --------------------------------------------------

    def ensure_subscribed(self) -> bool:
        """Subscribe to WORKER_* events (idempotent). Returns True on success."""
        if self._subscribed:
            return True
        if self._event_bus is None or EventType is None or not hasattr(self._event_bus, "subscribe"):
            return False
        try:
            for type_name in _JOB_EVENT_TYPES:
                self._event_bus.subscribe(EventType(type_name), self._on_event)
            self._subscribed = True
        except Exception:
            self._subscribed = False
        return self._subscribed

    # -- event feed ----------------------------------------------------

    def _on_event(self, event) -> None:
        """Mirror a WORKER_* event into the job store. Never raises."""
        try:
            _raw_type = getattr(event, "type", None)
            type_str = getattr(_raw_type, "value", None)
            if type_str is None and isinstance(_raw_type, str):
                type_str = _raw_type
            data = getattr(event, "data", None) or {}
            job_id = data.get("query_id")
            if not job_id:
                return
            worker_name = data.get("worker_name")
            session_id = data.get("session_id")
            if type_str == "worker_running":
                self.update(job_id, status="running",
                            worker_name=worker_name, session_id=session_id)
            elif type_str == "worker_partial_result":
                self.update(job_id, status="partial",
                            preview=str(data.get("content") or "")[:PARTIAL_PREVIEW_CAP],
                            worker_name=worker_name, session_id=session_id)
            elif type_str == "worker_timeout":
                self.update(job_id, status="timeout", completed_at=_now_iso(),
                            worker_name=worker_name, session_id=session_id)
            elif type_str == "worker_error":
                self.update(job_id, status="error",
                            preview=str(data.get("error") or "")[:PARTIAL_PREVIEW_CAP],
                            completed_at=_now_iso(),
                            worker_name=worker_name, session_id=session_id)
            elif type_str == "worker_completed":
                self.update(job_id, status="completed",
                            preview=str(data.get("content") or data.get("status") or "")[:PARTIAL_PREVIEW_CAP],
                            completed_at=_now_iso(),
                            worker_name=worker_name, session_id=session_id)
        except Exception:
            pass

    # -- mutations -----------------------------------------------------

    def register(self, job_id: str, worker_name: str, session_id=None,
                 instance_id=None) -> Optional[dict]:
        """Register a submitted job. Returns the stored record (deep copy)."""
        if not job_id:
            return None
        with self._lock:
            self._evict_if_needed_locked()
            now = _now_iso()
            record = {
                "job_id": job_id,
                "worker_name": worker_name,
                "session_id": session_id or "",
                "instance_id": instance_id,
                "status": "submitted",
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
                "preview": "",
                "result": None,
            }
            self._jobs[job_id] = record
            return copy.deepcopy(record)

    def update(self, job_id: str, **fields) -> Optional[dict]:
        """Update an existing job record (no-op for unknown jobs)."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            for key, value in fields.items():
                if key in ("worker_name", "session_id", "instance_id", "status", "completed_at", "preview"):
                    job[key] = value
            job["updated_at"] = _now_iso()
            return copy.deepcopy(job)

    def complete(self, job_id: str, envelope) -> Optional[dict]:
        """Store the final result envelope for a job (status -> completed).

        Creates the record on demand so jobs that were never explicitly
        registered (e.g. synchronous query paths that still carry a
        query_id) still end up with a completed record.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                self._evict_if_needed_locked()
                now = _now_iso()
                job = {
                    "job_id": job_id,
                    "worker_name": None,
                    "session_id": "",
                    "instance_id": None,
                    "status": "submitted",
                    "created_at": now,
                    "updated_at": now,
                    "completed_at": None,
                    "preview": "",
                    "result": None,
                }
                self._jobs[job_id] = job
            job["status"] = "completed"
            job["result"] = copy.deepcopy(envelope) if isinstance(envelope, dict) else envelope
            job["preview"] = (
                str((envelope or {}).get("content", ""))[:PREVIEW_CAP]
                if isinstance(envelope, dict)
                else ""
            )
            job["completed_at"] = _now_iso()
            job["updated_at"] = job["completed_at"]
            return copy.deepcopy(job)

    # -- reads ---------------------------------------------------------

    def job(self, job_id: str) -> Optional[dict]:
        """Deep copy of one job record, or None."""
        with self._lock:
            job = self._jobs.get(job_id)
            return copy.deepcopy(job) if job is not None else None

    def jobs(self, worker_name=None, status=None, instance_id=None) -> list:
        """Deep copies of all job records, optionally filtered."""
        with self._lock:
            out = []
            for job in self._jobs.values():
                if worker_name is not None and job.get("worker_name") != worker_name:
                    continue
                if status is not None and job.get("status") != status:
                    continue
                if instance_id is not None and job.get("instance_id") != instance_id:
                    continue
                out.append(copy.deepcopy(job))
            return out

    # -- internals -----------------------------------------------------

    def _evict_if_needed_locked(self) -> None:
        """Evict oldest jobs (by created_at) when the store is at capacity."""
        while len(self._jobs) >= JOB_REGISTRY_MAX_JOBS:
            if not self._jobs:
                return
            oldest = min(self._jobs, key=lambda k: self._jobs[k]["created_at"])
            del self._jobs[oldest]


# Module-level singleton (mirrors the worker_lifecycle observer pattern).
_WORKER_JOB_REGISTRY = None


def _get_worker_job_registry():
    """Return the module-level WorkerJobRegistry singleton (best-effort).

    Creates and subscribes it on first use. Never raises -- callers may
    ignore the result when the registry is unavailable.
    """
    global _WORKER_JOB_REGISTRY
    if _WORKER_JOB_REGISTRY is None:
        _WORKER_JOB_REGISTRY = WorkerJobRegistry()
        try:
            _WORKER_JOB_REGISTRY.ensure_subscribed()
        except Exception:
            pass
    return _WORKER_JOB_REGISTRY
