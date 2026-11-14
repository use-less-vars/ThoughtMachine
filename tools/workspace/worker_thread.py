# tools/workspace/worker_thread.py
"""Worker thread runtime extracted from tools.workspace.worker (W3a split).

Holds WorkerThread and the worker-side lifecycle helpers (WorkerBusAdapter,
WorkerSessionLifecycle, the global event-bus publishers, the stale-observer /
job-registry accessors, permission-ceiling merging, and the module-level
worker registry aliases).  tools.workspace.worker re-exports every public
name from this module, so existing importers and tests are unaffected.
"""

from __future__ import annotations

import collections
import hashlib
import json
import logging
import os
import queue
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, ClassVar, Dict, List, Literal, Optional, Union

from pydantic import Field, PrivateAttr, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

from tools.base import ToolBase
from tools.workspace.command_channel import (
    CommandFence,
    claim_command,
    discard,
    fence_rejection_reason,
    quarantine,
    write_command,
)
from tools.workspace.inflight_journal import (
    INFLIGHT_FILENAME,
    MAX_ATTEMPTS,
    STATUS_CANCELLED,
    STATUS_INFLIGHT,
    STATUS_RECOVERABLE,
    InflightRecord,
    adopt_inflight,
    clear_inflight,
    read_inflight,
    read_inflight_unfenced,
    utcnow_iso,
    write_inflight,
)
from tools.workspace.worker_runtime_protocol import Heartbeat, WorkerState

try:
    from agent.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)

# File lock for atomic writes (same pattern as FileSystemSessionStore)
try:
    from session.lock import FileLock
except ImportError:
    FileLock = None  # type: ignore

# Centralized literal defaults (Phase A consolidation) — the subset used by the
# moved worker-thread code. Re-exported from tools.workspace.worker for
# backward compatibility.
from agent.config.defaults import (
    SPAWN_QUEUE_TIMEOUT,
    QUERY_WAIT_GRACE_SECONDS,
    MAX_WORKERS_PER_SESSION,
    HEARTBEAT_INTERVAL_S,
    HEARTBEAT_STALE_AFTER_S,
    WORKER_DEFAULT_MAX_CONTAINERS,
    DEFAULT_WORKER_SYSTEM_PROMPT,
    WORKER_DEFAULT_TEMPERATURE,
    WORKER_TIMEOUT_SECONDS,
    WORKER_MAX_RETRIES,
)

# Module constants that live in worker.py's import section; duplicated here so
# the moved code binds its own module globals (values identical).
WORKER_DEFAULT_MAX_RUNTIME_S: Optional[float] = None
_NO_SESSION_KEY = "<no-session>"

# Input-queue marker meaning ONLY "wake up and re-read the park flags".
# A bare ``None`` in ``_input_queue`` keeps its unambiguous meaning: STOP.
# The two were historically the same value, which made a leftover sentinel
# consumed after ``resume()`` (that clears ``_park_event``) read as STOP and
# silently drop every query queued behind it.
_PARK_SENTINEL: Any = object()

# Reply-wait granularity for ``WorkerThread.send_query``.  The wait is taken one
# slice at a time so a pause / turn-cancel raised while the CALLER is blocked in
# it lands within one slice instead of at the deadline (300 s + 60 s grace) --
# unsliced, the pause stayed invisible for minutes and the turn was finally
# reported as "timeout" (audit D16).
QUERY_WAIT_SLICE_SECONDS: float = 0.5

#: Reason stamped on a turn the DETACH park ABANDONED. It rides on the journal
#: record the park flips to ``recoverable`` and on the envelope of the job the
#: same park terminalizes as ``interrupted``, so one grep finds both halves.
_JOURNAL_DETACH_REASON = "detached-midturn-park"

#: Reason stamped on the journal record a RESET kills and then drops. The
#: cancelled record is unlinked a moment later (``_drop_persisted_files``),
#: so no successor ever sees this string on disk -- it exists so the kill that
#: a reset performs is still a DELIBERATE one (the same bookkeeping seam every
#: other kill uses) rather than a silent ``kill()`` that journals nothing.
_JOURNAL_RESET_REASON = "reset-drop"

# Bounded attempts for ``WorkerThread._allocate_generation``. A retry is only
# taken when a concurrent allocator overwrote the sidecar after our rename
# (see the post-rename verification there).
_GENERATION_ALLOCATE_ATTEMPTS = 5
# One-shot latch for the degraded (no ``session.lock``) generation warning.
_UNLOCKED_GENERATION_WARNED = False
# Per-worker-dir in-process locks serialising generation allocation.  See
# ``_generation_thread_lock`` for why ``FileLock`` alone is not enough.
_generation_thread_locks: Dict[str, "threading.Lock"] = {}
_generation_thread_locks_guard = threading.Lock()


def _generation_thread_lock(key: str) -> "threading.Lock":
    """Return the process-wide lock serialising allocations for ``key``.

    ``session.lock.FileLock`` USED to unlink its lock file on release, which let
    a waiter that had opened the *old* inode lock an orphaned inode while a
    third caller locked a freshly created one: two critical sections then ran
    concurrently (measured on this repo: overlapping windows in 14 of 40
    acquisitions under 8-thread contention; 0 once the lock file was never
    unlinked).  That defect is FIXED -- ``session/lock.py`` ``release()``
    leaves the lock file in place, so the lock is inode-stable again and every
    acquirer meets the same rendezvous point.  This in-process lock is kept
    anyway: spawns are threads of one backend process, so serialising them here
    costs one uncontended ``threading.Lock`` instead of a ``flock`` poll loop,
    and the allocation stays correct even if path-level churn ever returns.
    ``FileLock`` is still taken for the cross-process case, and the
    post-rename verification below still covers its leftover exposure.
    """
    with _generation_thread_locks_guard:
        lock = _generation_thread_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _generation_thread_locks[key] = lock
        return lock


def _sha256_args(args: Any) -> str:
    """Stable hex digest of a tool call's arguments (the journal's ``args_hash``).

    ``sort_keys`` so two dicts that differ only in insertion order hash the
    same, ``default=str`` so a non-JSON-serialisable argument degrades to its
    repr instead of raising -- the journal is bookkeeping, never a gate.
    """
    try:
        payload = json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        payload = repr(args)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _stop_already_pending(thread: Any) -> bool:
    """True when *thread* has a stop request in flight.

    Defensive: only a real ``Event.is_set()`` returning ``True`` counts, so
    test doubles with attribute mocks are not mistaken for stopped workers.
    """
    event = getattr(thread, "_stop_event", None)
    try:
        return event is not None and event.is_set() is True
    except Exception:
        return False

# Optional: WorkerContext (imported eagerly — no circular dep)
try:
    from agent.core.worker_context import (
        WorkerContext,
        WORKER_NAME_CONTEXTVAR,
    )
except ImportError:
    WorkerContext = None  # type: ignore
    WORKER_NAME_CONTEXTVAR = None  # type: ignore

# Optional: ContainerManager — used at spawn time to build a per-worker
# manager for worker-scoped container teardown. Imported lazily so this module
# stays importable when the docker stack is unavailable (cleanup then no-ops).
try:
    from infra.container_manager import ContainerManager as _CM
except Exception:
    _CM = None  # type: ignore

try:
    from agent.core.state import ExecutionState, TurnState, TimeState
except ImportError:
    ExecutionState = None
    TurnState = None
    TimeState = None

from tools.workspace.worker_container import (
    _WORKER_CONTAINER_LABEL,
    _RESOURCE_CONTAINER_LABEL,
    cleanup_worker_containers,
    is_resource_container,
    is_worker_owned_container,
    is_worker_teardown_excluded_container,
    worker_owner_label,
)
from tools.workspace.worker_timeout import (
    JOIN_DEFAULT_WAIT_SECONDS,
    _worker_timeout_detected,
    _worker_query_wait_timeout,
    budget_for_thread,
    clamped_wait_for_job_timeout,
    store_budget_on_thread,
    wait_for_worker_exit,
)
from thoughtmachine.timebudget import (
    CEILING_OWNER,
    DECLARED_CEILING,
    DEFAULT_REQUEST_SECONDS,
    TimeBudget,
    TimeBudgetExceeded,
)
from tools.workspace.worker_execution import ExecutionTracker

# Fail-closed default levels for permission categories a session profile does
# not explicitly expose.  Used by _restrictive_merge() so a worker can never
# fill in a missing category with its own (potentially permissive) footprint
# value.  Imported LAZILY (not at module import time): a module-level
# ``from thoughtmachine.security import SAFE_DEFAULTS`` triggered
# thoughtmachine/security.py's try/except ImportError during pytest
# collection — while agent.events was mid-load — permanently pinning its
# EventType to None and breaking security-prompt tests later in the session.
_SAFE_DEFAULTS_CACHE: Optional[Dict[str, Any]] = None


def _load_safe_defaults() -> Dict[str, Any]:
    """Return the fail-closed SAFE_DEFAULTS map, importing it lazily.

    Cached after the first successful import so the cost is paid once.
    Falls back to an empty dict (with a warning) if thoughtmachine.security
    is unavailable; callers then resolve missing categories to ``None``.
    """
    global _SAFE_DEFAULTS_CACHE
    if _SAFE_DEFAULTS_CACHE is None:
        try:
            from thoughtmachine.security import SAFE_DEFAULTS

            _SAFE_DEFAULTS_CACHE = SAFE_DEFAULTS
        except ImportError:
            log(
                "WARN",
                "worker.permissions",
                "thoughtmachine.security unavailable; "
                "worker safe-defaults fall back to an empty dict",
            )
            _SAFE_DEFAULTS_CACHE = {}
    return _SAFE_DEFAULTS_CACHE

try:
    from agent.events import EventBus, create_event, EventType, global_event_bus
except ImportError:
    EventBus = None
    create_event = None
    EventType = None
    global_event_bus = None

# Optional: WorkerLifecycleObserver (Phase 2A) — imported lazily so worker.py
# stays importable without tools.workspace.worker_lifecycle (which only
# depends on agent.events, so there is no import cycle at runtime).
try:
    from tools.workspace.worker_lifecycle import WorkerLifecycleObserver
except ImportError:
    WorkerLifecycleObserver = None  # type: ignore


def _on_worker_stale(worker_name: str, info: dict) -> None:
    """Best-effort callback invoked when the observer flags a hung worker.

    Looks up the worker thread in the module-level registry and terminates its
    in-flight executions so the supervisor can reclaim the worker. Deliberately
    never raises: this is invoked from the lifecycle observer's event path and
    must not break it. Registry globals (``_registry_lock`` / ``_worker_registry``)
    are assigned later in this module, so they are resolved at call time.
    """
    try:
        session_id = info.get("session_id") or ""
        instance_id = info.get("instance_id")
        thread = None
        try:
            thread, _sid, _iid = _resolve_worker_thread(
                worker_name,
                instance_id=instance_id,
                session_id=session_id or None,
            )
        except _WorkerAmbiguityError:
            thread = None
        if thread is None:
            with _registry_lock:
                thread = _worker_registry.get((session_id, worker_name, instance_id or 1))
                if thread is None:
                    thread = _worker_registry.get((session_id, worker_name))
                if thread is None:
                    thread = _worker_registry.get((_NO_SESSION_KEY, worker_name))
        if thread is None:
            # Name-only fallback: first matching entry across sessions.
            with _registry_lock:
                for key, cand in _worker_registry.items():
                    if key[1] == worker_name:
                        thread = cand
                        break
        if thread is None:
            return
        wlm = getattr(thread, "_wlm", None)
        if wlm is not None:
            wlm.terminate_executions()
            return
        # Flag-off path: terminate through the worker's default-on tracker so
        # a blocking tool call on a hung worker can unblock (the same
        # executions the WLM path would have killed).
        tracker = getattr(thread, "_execution_tracker", None)
        if tracker is not None:
            try:
                tracker.terminate_all(
                    thread.worker_name,
                    getattr(thread, "_container_manager", None),
                    None,
                    session_id=thread.session_id,
                )
            except Exception:
                pass
    except Exception:
        pass


_WORKER_LIFECYCLE_OBSERVER = None


def _get_worker_lifecycle_observer():
    """Return the module-level WorkerLifecycleObserver singleton (best-effort).

    Creates and subscribes it on first use. Returns None when the observer
    module or the event bus is unavailable, so callers can safely ignore the
    result.
    """
    global _WORKER_LIFECYCLE_OBSERVER
    if WorkerLifecycleObserver is None:
        return None
    if _WORKER_LIFECYCLE_OBSERVER is None:
        _WORKER_LIFECYCLE_OBSERVER = WorkerLifecycleObserver(
            stale_callback=_on_worker_stale
        )
        try:
            _WORKER_LIFECYCLE_OBSERVER.ensure_subscribed()
        except Exception:
            pass
    return _WORKER_LIFECYCLE_OBSERVER


# Optional: WorkerJobRegistry (Phase 2B) — imported lazily so worker.py stays
# importable without tools.workspace.job_registry (stdlib + agent.events only).
try:
    from tools.workspace.job_registry import WorkerJobRegistry
except ImportError:
    WorkerJobRegistry = None  # type: ignore

_WORKER_JOB_REGISTRY = None


def _get_worker_job_registry():
    """Return the module-level WorkerJobRegistry singleton (best-effort).

    Creates and subscribes it on first use. Returns None when the registry
    module or the event bus is unavailable, so callers can safely ignore the
    result.

    The singleton is resolved through the tools.workspace.worker facade when
    it is loaded, so ``mock.patch("tools.workspace.worker._WORKER_JOB_REGISTRY",
    new=...)`` (used by tests) is observed by this module's callers too: the
    facade re-export binding is the one tests replace, and it stays in sync
    with this module's global either way. A replaced facade FUNCTION binding
    (``mock.patch("tools.workspace.worker._get_worker_job_registry", ...)``)
    is honored the same way: when the facade's binding is no longer this
    function, the call is delegated to it.
    """
    global _WORKER_JOB_REGISTRY
    if WorkerJobRegistry is None:
        return None
    try:
        import tools.workspace.worker as _worker_facade  # noqa: F401
    except Exception:
        _worker_facade = None
    if _worker_facade is not None:
        _facade_fn = getattr(_worker_facade, "_get_worker_job_registry", None)
        if _facade_fn is not None and _facade_fn is not _get_worker_job_registry:
            # Facade binding was replaced (test patch): delegate so the
            # injected registry is the one callers observe.
            return _facade_fn()
    if (_worker_facade is not None
            and getattr(_worker_facade, "_WORKER_JOB_REGISTRY", None) is not None):
        _WORKER_JOB_REGISTRY = _worker_facade._WORKER_JOB_REGISTRY
        return _WORKER_JOB_REGISTRY
    if _WORKER_JOB_REGISTRY is None:
        _WORKER_JOB_REGISTRY = WorkerJobRegistry()
        try:
            _WORKER_JOB_REGISTRY.ensure_subscribed()
        except Exception:
            pass
    if _worker_facade is not None:
        _worker_facade._WORKER_JOB_REGISTRY = _WORKER_JOB_REGISTRY
    return _WORKER_JOB_REGISTRY


def _publish_global_worker_event(event_type, data: dict) -> None:
    """Publish a worker lifecycle event to the global event bus (best-effort).

    Never raises: worker lifecycle events are observability aids, not control
    plane. ``data`` must include ``worker_name`` and may include ``session_id``.
    """
    # Resolve the bus through the tools.workspace.worker facade so that
    # mock.patch("tools.workspace.worker.global_event_bus", ...) (used by
    # tests) is observed here too; fall back to this module's own binding
    # while the facade is not fully imported yet.
    try:
        from tools.workspace.worker import global_event_bus
    except ImportError:
        pass
    if global_event_bus is None or EventType is None or create_event is None:
        return
    try:
        evt = create_event(
            event_type,
            data=data,
            source=f"worker:{data.get('worker_name', 'unknown')}",
            session_id=data.get("session_id") or "",
        )
        global_event_bus.publish(evt)
    except Exception:
        pass


class WorkerBusAdapter:
    """Drop-in replacement for GUIIntegration that publishes to per-worker EventBus.

    Provides the same interface as GUIIntegration (state property, emit_* methods)
    but publishes events to the worker's EventBus instead of emitting Qt signals.
    """

    def __init__(self, event_bus, worker_name: str, state_bridge=None):
        self._event_bus = event_bus
        self.worker_name = worker_name
        self._state_bridge = state_bridge
        self._state = ExecutionState.READY if ExecutionState else None

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, new_state):
        if self._state != new_state:
            old_state = self._state
            self._state = new_state
            if self._event_bus is not None and create_event is not None and EventType is not None:
                try:
                    evt = create_event(
                        EventType.WORKER_STATUS,
                        data={
                            "worker_name": self.worker_name,
                            "execution_state": new_state.value if hasattr(new_state, 'value') else str(new_state),
                            "old_execution_state": old_state.value if hasattr(old_state, 'value') else str(old_state),
                        },
                        source="worker",
                        session_id="",
                    )
                    self._event_bus.publish(evt)
                except Exception:
                    pass

    def emit_tokens_updated(self, total_input: int, total_output: int) -> None:
        log('DEBUG', 'pipeline.worker_bus',
            f"[TOKEN_PIPELINE] WorkerBusAdapter.emit_tokens_updated: "
            f"worker={self.worker_name} total_input={total_input} total_output={total_output}")
        self._publish("tokens_updated", {"total_input": total_input, "total_output": total_output})

    def emit_context_updated(self, context_length: int) -> None:
        log('DEBUG', 'pipeline.worker_bus',
            f"[TOKEN_PIPELINE] WorkerBusAdapter.emit_context_updated: "
            f"worker={self.worker_name} context_length={context_length}")
        self._publish("context_updated", {"context_length": context_length})

    def emit_status_message(self, message: str) -> None:
        log('DEBUG', 'pipeline.worker_bus',
            f"[TOKEN_PIPELINE] WorkerBusAdapter.emit_status_message: "
            f"worker={self.worker_name} message={str(message)[:100]!r}")
        self._publish("status_message", {"message": str(message)[:500]})

    def emit_error_occurred(self, error: str, traceback_str: str) -> None:
        log('DEBUG', 'pipeline.worker_bus',
            f"[TOKEN_PIPELINE] WorkerBusAdapter.emit_error_occurred: "
            f"worker={self.worker_name} error={str(error)[:100]!r}")
        self._publish("error_occurred", {"error": str(error)[:500], "traceback": str(traceback_str)[:2000]})

    def emit_config_changed(self, config: dict) -> None:
        log('DEBUG', 'pipeline.worker_bus',
            f"[TOKEN_PIPELINE] WorkerBusAdapter.emit_config_changed: "
            f"worker={self.worker_name} config_keys={list(config.keys())}")
        # Redact sensitive fields (api keys, tokens, secrets) before the config
        # is broadcast to the event bus / any consumer.
        try:
            from agent.config.audit import redact_config
            if isinstance(config, dict):
                config = redact_config(config)
        except Exception:
            pass
        self._publish("config_changed", {"config": config})

    def emit_conversation_changed(self) -> None:
        log('DEBUG', 'pipeline.worker_bus',
            f"[TOKEN_PIPELINE] WorkerBusAdapter.emit_conversation_changed: "
            f"worker={self.worker_name}")
        self._publish("conversation_changed", {})

    def forward_agent_event(self, event: dict) -> None:
        """Publish an agent event to the per-worker EventBus for frontend consumption."""
        event_type = event.get("type", "")
        log('DEBUG', 'pipeline.worker_bus',
            f"[TOKEN_PIPELINE] WorkerBusAdapter.forward_agent_event: "
            f"worker={self.worker_name} event_type={event_type!r} "
            f"session_id={event.get('session_id', 'N/A')}")

        if event_type == "agent_responded":
            content = event.get("content", "") or event.get("data", {}).get("content", "")
            reasoning = event.get("reasoning_content", "") or event.get("data", {}).get("reasoning_content", "")
            response_type = event.get("response_type", event.get("data", {}).get("response_type", "answer"))
            # FIX C: skip publishing empty placeholder messages. They produce invisible
            # bubbles in the worker panel AND consume the frontend dedup key (canonical
            # type|timestamp), causing the real full-content message (same logical event,
            # same timestamp) to be deduplicated away. Verified: no streaming /
            # update-in-place flow depends on empty-content worker_message publishes.
            if not content and not reasoning:
                log('DEBUG', 'pipeline.worker_bus',
                    f"forward_agent_event: SKIPPED agent_responded (empty content/reasoning) "
                    f"[worker={self.worker_name}]")
                return
            self._publish("worker_message", {
                "content": str(content)[:1000],
                "reasoning_content": str(reasoning)[:2000] if reasoning else None,
                "response_type": response_type,
            })

        elif event_type == "tool_call_start":
            # Pending tool-call notification: emitted by the agent BEFORE the tool
            # executes so the worker panel can render an *in-progress* row. The
            # later "tool_call"/"tool_result" events provide the outcome.
            log('DEBUG', 'pipeline.worker_bus',
                f"[TOKEN_PIPELINE] WorkerBusAdapter.forward_agent_event: tool_call_start "
                f"[worker={self.worker_name}] tool={event.get('tool_name', '?')}")
            self._publish("tool_call_start", {
                "tool_name": event.get("tool_name", ""),
                "arguments": event.get("arguments", ""),
                "tool_call_id": event.get("tool_call_id", ""),
            })

        elif event_type == "tool_call":
            log('DEBUG', 'pipeline.worker_bus',
                f"[TOKEN_PIPELINE] WorkerBusAdapter.forward_agent_event: tool_call "
                f"[worker={self.worker_name}] tool={event.get('tool_name', '?')}")
            self._publish("tool_call", {
                "tool_name": event.get("tool_name", ""),
                "arguments": event.get("arguments", {}),
                "tool_call_id": event.get("tool_call_id", ""),
            })

        elif event_type == "tool_result":
            result = event.get("result", "") or event.get("data", {}).get("result", "")
            log('DEBUG', 'pipeline.worker_bus',
                f"[TOKEN_PIPELINE] WorkerBusAdapter.forward_agent_event: tool_result "
                f"[worker={self.worker_name}] tool={event.get('tool_name', '?')} "
                f"success={event.get('success', True)}")
            self._publish("tool_result", {
                "tool_name": event.get("tool_name", ""),
                "success": event.get("success", True),
                "error": event.get("error", "") or "",
                "result": str(result)[:1000] if result else "",
                "tool_call_id": event.get("tool_call_id", ""),
            })

        elif event_type == "token_warning":
            log('DEBUG', 'pipeline.worker_bus',
                f"[TOKEN_PIPELINE] WorkerBusAdapter.forward_agent_event: token_warning "
                f"[worker={self.worker_name}] token_count={event.get('token_count', 0)}")
            self._publish("token_warning", {
                "message": (event.get("message", "") or event.get("data", {}).get("message", ""))[:500],
                "warning_message": (event.get("warning_message", "") or event.get("data", {}).get("warning_message", ""))[:500],
                "token_count": event.get("token_count", 0),
                "old_state": str(event.get("old_state", "")),
                "new_state": str(event.get("new_state", "")),
            })

        elif event_type == "turn_warning":
            message = event.get("message", "") or event.get("data", {}).get("message", "")
            log('DEBUG', 'pipeline.worker_bus',
                f"[TOKEN_PIPELINE] WorkerBusAdapter.forward_agent_event: turn_warning "
                f"[worker={self.worker_name}] turn_count={event.get('turn_count', 0)}")
            self._publish("turn_warning", {
                "old_state": str(event.get("old_state", "")),
                "new_state": str(event.get("new_state", "")),
                "turn_count": event.get("turn_count", 0),
                "warning_message": str(message)[:500],
            })

        elif event_type == "time_warning":
            log('DEBUG', 'pipeline.worker_bus',
                f"[TOKEN_PIPELINE] WorkerBusAdapter.forward_agent_event: time_warning "
                f"[worker={self.worker_name}] elapsed_seconds={event.get('elapsed_seconds', 0)}")
            self._publish("time_warning", {
                "message": (event.get("message", "") or event.get("data", {}).get("message", ""))[:500],
                "elapsed_seconds": event.get("elapsed_seconds", 0),
            })

        elif event_type == "turn":
            content = event.get("content", "") or event.get("data", {}).get("content", "")
            reasoning = event.get("reasoning_content", "") or event.get("data", {}).get("reasoning_content", "")
            log('DEBUG', 'pipeline.worker_bus',
                f"[TOKEN_PIPELINE] WorkerBusAdapter.forward_agent_event: assistant_message "
                f"[worker={self.worker_name}] content_len={len(str(content))} "
                f"reasoning={bool(reasoning)}")
            # FIX C: skip empty placeholder messages (see agent_responded guard above).
            if not content and not reasoning:
                log('DEBUG', 'pipeline.worker_bus',
                    f"forward_agent_event: SKIPPED turn (empty content/reasoning) "
                    f"[worker={self.worker_name}]")
                return
            self._publish("assistant_message", {
                "content": str(content)[:1000],
                "reasoning_content": str(reasoning)[:2000] if reasoning else None,
            })

        elif event_type == "system_notification":
            log('DEBUG', 'pipeline.worker_bus',
                f"[TOKEN_PIPELINE] WorkerBusAdapter.forward_agent_event: system_notification "
                f"[worker={self.worker_name}] "
                f"type_detail={event.get('type_detail', event.get('notification_type', 'N/A'))}")
            self._publish("system_notification", {
                "type": event.get("type_detail", event.get("notification_type", "general")),
                "message": (event.get("message", "") or event.get("data", {}).get("message", ""))[:500],
                "context_length": event.get("context_length", 0),
            })

        elif event_type == "context_summarized":
            log('DEBUG', 'pipeline.worker_bus',
                f"forward_agent_event: context_summarized [worker={self.worker_name}] "
                f"message={event.get('message', '')[:100]}")
            self._publish("context_summarized", {
                "token_count": event.get("token_count", 0),
                "message": str(event.get("message", "") or "")[:500],
                "old_state": str(event.get("old_state", "")),
                "new_state": str(event.get("new_state", "")),
            })

        elif event_type == "token_recovery":
            log('DEBUG', 'pipeline.worker_bus',
                f"forward_agent_event: token_recovery [worker={self.worker_name}] "
                f"token_count={event.get('token_count', 0)}")
            self._publish("token_recovery", {
                "token_count": event.get("token_count", 0),
                "old_state": str(event.get("old_state", "")),
                "new_state": str(event.get("new_state", "")),
                "recovery_message": str(event.get("recovery_message", "") or "")[:500],
            })

        elif event_type == "user_message" or event_type == "user_interaction_requested":
            # DIAG: user_message/user_interaction_requested deliberately NOT forwarded
            # (these come from the old _publish_event path instead)
            log('DEBUG', 'pipeline.worker_bus',
                f"forward_agent_event -> SKIPPED {event_type} [worker={self.worker_name}]")
            pass

    def _publish(self, event_type: str, data: dict) -> None:
        log('DEBUG', 'pipeline.worker_bus',
            f"_publish [worker={self.worker_name}]: event_type={event_type!r}, "
            f"data_keys={list(data.keys())}, event_bus={'SET' if self._event_bus else 'NONE'}, "
            f"EventType={'AVAILABLE' if EventType else 'UNAVAILABLE'}")
        if self._event_bus is None or EventType is None:
            log('DEBUG', 'pipeline.worker_bus',
                f"_publish SKIPPED [worker={self.worker_name}]: "
                f"event_bus={'None' if self._event_bus is None else 'set'}, "
                f"EventType={'None' if EventType is None else 'set'}")
            return
        try:
            from agent.events import BaseEvent, EventMetadata
            evt = EventType(event_type) if isinstance(event_type, str) else event_type
            event = BaseEvent(
                type=evt,
                metadata=EventMetadata(source="worker", session_id=""),
                data={**data, "worker_name": self.worker_name},
            )
            self._event_bus.publish(event)
            log('DEBUG', 'pipeline.worker_bus',
                f"_publish SUCCESS [worker={self.worker_name}]: event_type={event_type!r}, "
                f"subscribers_count={len(self._event_bus._subscribers.get(evt, [])) if hasattr(self._event_bus, '_subscribers') else 'N/A'}")
        except Exception as exc:
            log('WARNING', 'tools.worker', f"WorkerBusAdapter._publish failed for {event_type}: {exc}")


class WorkerSessionLifecycle:
    """Minimal SessionLifecycle stub for worker event processing.

    Provides the state property and setter that EventProcessor expects,
    without session persistence or GUI callback registration.
    """

    def __init__(self):
        self._state = ExecutionState.READY if ExecutionState else None

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, new_state):
        if self._state != new_state:
            self._state = new_state

    def mark_clean(self) -> None:
        pass

    def has_unsaved_changes(self) -> bool:
        return False


logger = logging.getLogger("tools.workspace.worker")

# Hard blocklist: tools that workers must NEVER use, regardless of permissions.
# NOTE: container tools (DockerCodeRunner, ContainerStartTool, ContainerExecTool,
# etc.) ARE allowed for workers — they are permission-gated like any other tool,
# NOT hard-blocked: DockerCodeRunner requires filesystem:write + container:true
# (docker_code_runner.py:56) and the ToolExecutor enforces those categories via
# check_required_categories, so a worker can only reach them when its effective
# (session × footprint) profile allows it. Only the tools below are hard-blocked
# (recursion, MCP server management, system/infrastructure discovery, persistent
# knowledge store). This is defense-in-depth: the blocklist is enforced at two
# separate points (enabled_tools filtering and tool class resolution).
_WORKER_BLOCKLIST: frozenset[str] = frozenset({
    "Worker",           # recursion: worker spawning workers
    "CheckSystem",       # system/network/worker/infrastructure discovery
    "KnowledgeBaseTool", # persistent cross-session knowledge store
})

# ── Worker tool universe (single source of truth) ─────────────────────────────
# Two universes exist in this build: ``SIMPLIFIED_TOOL_CLASSES`` (what
# ``enabled_tools`` is filtered against, agent/config/models.py:379-401) and
# ``iter_tool_classes()`` (what spawn-time class resolution used to walk).  A
# tool present in the latter but not the former resolved at spawn and was then
# silently filtered out at run time — silent narrowing, forbidden by design doc
# §4.2.  ``_worker_tool_universe()`` is therefore the ONE universe: the classes
# the runtime can actually instantiate, addressable by class name AND by the
# stable ``tool_name()`` alias (``GitReadTool`` <-> ``git_read``).


def _worker_tool_universe() -> Dict[str, type]:
    """Return the authoritative worker tool universe: name -> class.

    Keys are the class ``__name__`` (canonical) plus, for every class whose
    stable ``tool_name()`` differs, that alias.  Canonical names win: aliases
    are added with ``setdefault``, so a class can never shadow another class'
    name.  Aliases of blocklisted classes are not added at all, so the
    blocklist cannot be bypassed through a stable name.
    """
    from tools import simplified_tool_classes

    universe: Dict[str, type] = {}
    aliases: Dict[str, type] = {}
    for cls in simplified_tool_classes():
        name = getattr(cls, "__name__", "")
        if not name:
            continue
        universe[name] = cls
        if name in _WORKER_BLOCKLIST:
            continue
        try:
            alias = cls.tool_name()
        except Exception:
            # ``tool_name()`` is optional for third-party tool classes.
            continue
        if alias and alias != name:
            aliases.setdefault(alias, cls)
    for alias, cls in aliases.items():
        universe.setdefault(alias, cls)
    return universe


def _canonical_worker_tool_name(name: str) -> str:
    """Map *name* to its canonical class ``__name__`` (identity when unknown)."""
    cls = _worker_tool_universe().get(name)
    if cls is None:
        return name
    return getattr(cls, "__name__", name)


def _worker_granted_tool_names(registry: Any) -> List[str]:
    """Granted tool names for *registry*, in ``simplified_tool_classes()`` order.

    Deliberately NOT ``registry.names`` (which is sorted): the historical
    ``tools: []`` derivation order is registration order and
    tests/test_ci_read_tool.py mirrors it verbatim.  Values are de-duplicated by
    class, so a tool's alias spelling can never add a second entry.
    """
    from tools import simplified_tool_classes

    granted = set(registry.resolved.values())
    names: List[str] = []
    for cls in simplified_tool_classes():
        name = getattr(cls, "__name__", "")
        if name and name not in _WORKER_BLOCKLIST and cls in granted:
            names.append(name)
    return names


# Default system prompt for worker sub-agents.
# This is a fixed instruction rather than inheriting the main agent's prompt,
# preventing delegation loops in Engineer mode.

# Worker LLM sampling defaults (used when the worker definition does not
# specify a value).  These are worker-scoped: the main agent's sampling
# settings are never inherited.
WORKER_DEFAULT_MAX_TOKENS: Optional[int] = None
WORKER_DEFAULT_TRUNCATION: Optional[int] = None
from tools.workspace.worker_registry import WorkerRegistry as _WorkerRegistry

_registry_instance = _WorkerRegistry.get_instance()
_worker_registry = _registry_instance._worker_registry
_registry_lock = _registry_instance._registry_lock
_worker_event_bus_registry = _registry_instance._worker_event_bus_registry
_bus_registry_lock = _registry_instance._bus_registry_lock
def register_worker_event_bus(session_id: str, worker_name: str, event_bus: Any, instance_id: int = 1, *, epoch: int | None = None) -> Any:
    """Delegate to WorkerRegistry singleton (backward compat).

    *epoch* is the owning incarnation's ``WorkerThread._generation``, passed
    straight through so the registry can refuse a stale (older) registration.
    """
    return _WorkerRegistry.get_instance().register_event_bus(session_id, worker_name, event_bus, instance_id=instance_id, epoch=epoch)

def unregister_worker_event_bus(session_id: str, worker_name: str, instance_id: int = 1, *, epoch: int | None = None) -> Any:
    """Delegate to WorkerRegistry singleton (backward compat).

    *epoch* is the owning incarnation's ``WorkerThread._generation``, passed
    straight through so the registry can refuse a stale (older) teardown.
    """
    return _WorkerRegistry.get_instance().unregister_event_bus(session_id, worker_name, instance_id=instance_id, epoch=epoch)

def _unregister_runtime_handles(entries: Any, *, epoch: int | None = None) -> None:
    """Best-effort, epoch-fenced removal of the runtime handles for *entries*.

    *entries* is an iterable of registry keys -- ``(session_id, worker_name)``
    or ``(session_id, worker_name, instance_id)``, the two shapes
    ``_worker_registry`` itself uses -- so a caller can hand over exactly the
    entries it just removed from the registry.  S7a-3 mirrors handle removal
    onto the paths that already remove the registry entry and invents no
    lifecycle semantics of its own: a parked or cooperatively stopped worker
    keeps its registry entry, and therefore keeps its handle.

    *epoch* is the dying incarnation's ``_generation``, passed straight through
    for the reason ``WorkerRegistry`` fences its own removal: a predecessor's
    teardown must not unplug its successor's handle (the S7a-1 EventBus bug,
    moved to the store that feeds the supervisor's command path).

    Never raises, and never called with ``_registry_lock`` held: the handle
    store takes its own lock and the ordering is registry -> handle, never back.
    Both properties matter because this runs in teardown paths, where
    bookkeeping must never be the thing that breaks the exit.
    """
    try:
        from tools.workspace.worker_runtime import unregister_runtime_handle
    except Exception:
        logger.debug(
            "runtime handle store unavailable; no handle to unregister",
            exc_info=True,
        )
        return
    for entry in entries or ():
        try:
            session_id = entry[0]
            worker_name = entry[1]
            instance_id = entry[2] if len(entry) >= 3 else None
        except Exception:
            logger.debug("unusable runtime handle key %r; skipped", entry)
            continue
        try:
            unregister_runtime_handle(
                session_id, worker_name, instance_id=instance_id, epoch=epoch
            )
        except Exception:
            logger.debug(
                "could not unregister the runtime handle for worker %r "
                "(session %r, instance %r)",
                worker_name,
                session_id,
                instance_id,
                exc_info=True,
            )

class _WorkerAmbiguityError(Exception):
    """Raised when a worker name maps to multiple live instances and no
    ``instance_id`` was given to disambiguate."""


def _resolve_worker_thread(
    worker_name: str,
    instance_id: Optional[int] = None,
    session_id: Optional[str] = None,
):
    """Resolve a worker name (+ optional instance/session) to a live thread.

    Returns ``(thread, session_key, iid)``. With no explicit ``instance_id``,
    exactly one live candidate must exist; multiple live candidates raise
    ``_WorkerAmbiguityError`` (callers must pass ``instance_id``). No live
    candidate raises ``_WorkerAmbiguityError`` with a 'not running' message.
    """
    with _registry_lock:
        if instance_id is not None:
            # Exact 3-tuple lookup first, then a name+instance scan (session
            # filter applies only when given).
            if session_id is not None:
                thread = _worker_registry.get((session_id, worker_name, instance_id))
                if thread is not None:
                    return (thread, session_id, instance_id)
            for key, thread in list(_worker_registry.items()):
                wname = key[1]
                iid = key[2] if len(key) >= 3 else 1
                if wname == worker_name and iid == instance_id:
                    if session_id is None or key[0] == session_id:
                        return (thread, key[0], iid)
            raise _WorkerAmbiguityError(
                f"Worker '{worker_name}' instance {instance_id} is not running"
            )
        candidates = []
        for key, thread in list(_worker_registry.items()):
            wname = key[1]
            iid = key[2] if len(key) >= 3 else 1
            if wname != worker_name:
                continue
            if session_id is not None and key[0] != session_id:
                continue
            alive = bool(getattr(thread, "is_alive", lambda: True)())
            if not alive:
                continue
            candidates.append((key[0], iid, thread))
    if not candidates:
        raise _WorkerAmbiguityError(f"Worker '{worker_name}' is not running")
    if len(candidates) == 1:
        sid, iid, thread = candidates[0]
        return (thread, sid, iid)
    labels = sorted(
        _WorkerRegistry.instance_label(worker_name, iid) for _sid, iid, _t in candidates
    )
    raise _WorkerAmbiguityError(
        f"Worker '{worker_name}' is ambiguous: {len(candidates)} live "
        f"instances ({', '.join(labels)}). Specify instance_id."
    )


def _pause_join_target(thread, worker_name: str) -> bool:
    """Proxy the session main-agent pause onto the ONE worker a join waits on.

    Operator ruling (b): "If Main is paused while waiting on a worker, the
    worker must be paused."  The scope is deliberately the single worker the
    caller is ACTIVELY waiting on (the ``join`` / ``wait_for_job`` target, and
    only that thread) -- async workers the caller is not waiting on keep
    running, which is what ``want_all`` already controls and what this helper
    must never touch.

    Idempotent by construction, because ``_action_join`` re-enters this branch
    on every join call while the pause lasts:

    * ``status == 'paused'``: the worker already parked -- do nothing.
    * ``_park_event`` already set: ``WorkerThread.pause()`` was already issued
      for this pause (the event is set synchronously by ``pause()``, the thread
      status only flips later, at the run loop's next boundary).  Bailing here
      is what stops a re-park, a fresh cross-process ``pause`` command write
      and a repeated log line per call.
    * the worker's own agent already carries a ``'pause'`` restriction:
      leave it alone (never steal or re-own an existing pause restriction).

    Never sets ``_manual_only_pause``: the proxied pause must stay
    auto-resumable by the existing paths (``_action_query``'s paused branch,
    ``_action_spawn``'s sole-paused path, the operator resume route).

    Returns True only when this call actually issued ``thread.pause()``.
    """
    if getattr(thread, "status", None) == "paused":
        return False
    park = getattr(thread, "_park_event", None)
    if park is not None and bool(getattr(park, "is_set", lambda: False)()):
        return False
    agent = getattr(thread, "_agent", None)
    state = getattr(agent, "state", None)
    if state is not None and getattr(state, "restriction_reason", None) == "pause":
        return False
    try:
        thread.pause()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Main-pause proxy: pause failed for worker '%s': %s", worker_name, exc)
        return False
    logger.info(
        "Main-pause proxy: paused worker '%s' (the join target of the paused caller)",
        worker_name,
    )
    return True


def _record_proxied_join_target(
    session_id: Optional[str],
    worker_name: str,
    instance_id: int,
) -> None:
    """Remember a worker that ``_pause_join_target`` actually parked for *session_id*.

    This record is the whole safety story for the symmetric unpause (PHASE 2g):
    ``_resume_proxied_join_targets`` can only ever resume a worker this proxy
    itself paused -- never one the operator paused -- because only calls that got
    ``True`` back from :func:`_pause_join_target` are recorded here.

    Idempotent (a set of ``(worker_name, instance_id)`` triples) and defensive:
    bookkeeping must never break the join that triggered it, so failures are
    logged and swallowed.
    """
    if not session_id:
        return
    try:
        with _PROXIED_JOIN_TARGETS_LOCK:
            _PROXIED_JOIN_TARGETS.setdefault(session_id, set()).add(
                (worker_name, int(instance_id))
            )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Main-pause proxy: could not record paused target '%s': %s",
            worker_name,
            exc,
        )


def _resume_proxied_join_targets(session_id: Optional[str]) -> list[str]:
    """Lift the worker pauses that the session main-pause proxy applied.

    Exact symmetry for :func:`_pause_join_target`: when the session-level "main
    agent paused" marker is discarded at the start of a new operator turn
    (``Agent._discard_session_main_pause``), the join targets this process
    parked on its behalf are resumed as well.  This is strictly the RECORDED
    targets -- never a sweep of the session's workers -- so a worker the
    operator paused by hand is not touched here.

    The record is popped before any resume happens (the lock guards the dict
    mutation only and is never held across ``resume()``), so a failing or slow
    resume cannot wedge the bookkeeping and a repeat call is a no-op.

    Every target is independent and defensive: a missing/dead/ambiguous worker
    (``_WorkerAmbiguityError``) is skipped, a killed worker refuses the resume
    itself (``resume() -> False``), a detached worker is an operator stop and is
    left alone, and an already-running worker is a silent no-op.  Never raises;
    returns the worker names actually resumed.
    """
    if not session_id:
        return []
    with _PROXIED_JOIN_TARGETS_LOCK:
        targets = _PROXIED_JOIN_TARGETS.pop(session_id, set())
    resumed: list[str] = []
    for worker_name, instance_id in sorted(targets):
        try:
            thread, _sid, _iid = _resolve_worker_thread(
                worker_name,
                instance_id=instance_id,
                session_id=session_id,
            )
        except _WorkerAmbiguityError as exc:
            logger.info(
                "Main-pause proxy: leaving worker '%s' instance %s alone: %s",
                worker_name,
                instance_id,
                exc,
            )
            continue
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "Main-pause proxy: could not resolve worker '%s' to resume it: %s",
                worker_name,
                exc,
            )
            continue
        status = getattr(thread, "status", None)
        park = getattr(thread, "_park_event", None)
        parked = park is not None and bool(getattr(park, "is_set", lambda: False)())
        if getattr(thread, "_detach_requested", False) or status in (
            "detached",
            "detaching",
            "killed",
            "killing",
        ):
            logger.info(
                "Main-pause proxy: not resuming worker '%s' (status=%s)",
                worker_name,
                status,
            )
            continue
        if status != "paused" and not parked:
            logger.info(
                "Main-pause proxy: worker '%s' is already running; nothing to resume",
                worker_name,
            )
            continue
        try:
            if thread.resume():
                resumed.append(worker_name)
                logger.info(
                    "Main-pause proxy: resumed worker '%s' (symmetric unpause of "
                    "the session main-pause, session '%s')",
                    worker_name,
                    session_id,
                )
            else:
                logger.info(
                    "Main-pause proxy: worker '%s' refused resume (killed); left as is",
                    worker_name,
                )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "Main-pause proxy: resume failed for worker '%s': %s",
                worker_name,
                exc,
            )
    return resumed


# ---------------------------------------------------------------------------
# Restrictive merge for permission ceiling enforcement
# ---------------------------------------------------------------------------

# Session permissions act as the ceiling — worker can only reduce strictness
_STRICTNESS_ORDER: dict[str, int] = {
    "container": 0,
    "filesystem": 1,
    "network": 2,
}

# Value ordering within each category: stricter = lower index
# "deny" is stricter than "allow", "none" is stricter than "read".  The maps
# cover the legacy string vocabularies only; canonical category values outside
# this vocab fall out of the map and merge session-wins (the session is the
# ceiling, so a worker may never raise a level above it).
_PERMISSION_ORDER: dict[str, dict[str, int]] = {
    "container": {"deny": 0, "allow": 1},
    "filesystem": {"none": 0, "read": 1, "write": 2},
    "network": {"deny": 0, "allow": 1},
}

def _restrictive_merge(
    session_perms: dict[str, Any],
    worker_perms: dict[str, Any],
) -> dict[str, Any]:
    """
    Merge worker permissions into session permissions, picking the *more
    restrictive* (stricter) value for each key.

    The session acts as the ceiling — a worker cannot exceed the session's
    permission level for any category.  Values may be booleans (``False`` =
    deny, ``True`` = allow) or strings whose strictness order is defined
    by ``_PERMISSION_ORDER``.

    Examples
    --------
    # The session is the ceiling: the merged result may never exceed the
    # session level, and unmapped (canonical) values merge session-wins.
    >>> _restrictive_merge({"filesystem": "read"}, {"filesystem": "write"})
    {'filesystem': 'read'}
    >>> _restrictive_merge({"git": "read"}, {"git": "write"})
    {'git': 'read'}
    >>> _restrictive_merge({"network": "banned"}, {"network": "ask"})
    {'network': 'banned'}
    >>> _restrictive_merge({"container": False}, {"container": True})
    {'container': False}

    Runtime note
    ------------
    This helper only builds the worker's *spawn-time* ``session_permissions``
    mirror -- the ``SessionPermissions`` snapshot its ``AgentConfig`` is
    constructed with.  It is NOT the runtime cap: the declared worker footprint
    is passed separately to ``AgentConfig.permission_footprint`` and applied by
    the security gate on top of the grants re-read from disk on every tool
    call, so a grant written after spawn takes effect without respawning the
    worker.  Never treat this function's result as the authority for what a
    running worker may do.
    """
    result = {}
    all_keys = set(session_perms) | set(worker_perms)
    for key in all_keys:
        s_val = session_perms.get(key)
        w_val = worker_perms.get(key)

        if s_val is None:
            # Fail-closed: the session does not expose this category, so the
            # worker may NOT fill it in with its own (potentially permissive)
            # footprint value.  Resolve to the safe-default level instead.
            result[key] = _load_safe_defaults().get(key)
        elif w_val is None:
            result[key] = s_val  # session provides
        elif isinstance(s_val, bool) or isinstance(w_val, bool):
            # Boolean case: False (deny) is stricter than True (allow)
            result[key] = False if (s_val is False or w_val is False) else True
        else:
            # Both are strings — pick the stricter one by order map
            order = _PERMISSION_ORDER.get(key, {})
            s_rank = order.get(s_val, 99)
            w_rank = order.get(w_val, 99)
            # Lower rank = stricter
            result[key] = s_val if s_rank <= w_rank else w_val
    return result


def _canonicalize_session_permissions(
    permissions: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Strip legacy permission grains at the worker AgentConfig boundary.

    The canonical session-permission schema is exactly six categories
    (``container | network | filesystem | git | mcp | host_bash``), with
    ``write_on_feature_branch`` carried on the ``git`` channel.  The legacy
    ``system``/``execution`` categories and the split ``git_read`` /
    ``git_write`` grains were removed in the permission-schema unification
    (the gate no longer mirrors ``git`` into derived grains).  Any legacy keys
    surviving in the merged dict (stale worker definitions, hand-built test
    configs) are dropped here — ``SessionPermissions`` ignores them anyway,
    but the AgentConfig-bound dict stays canonical and unambiguous.
    """
    if not isinstance(permissions, dict):
        return permissions
    result = dict(permissions)
    for legacy in ("git_read", "git_write", "system", "execution"):
        result.pop(legacy, None)
    return result


#: Legacy permission grains dropped from a worker definition's declared
#: footprint (same four keys ``_canonicalize_session_permissions`` strips).
_LEGACY_FOOTPRINT_KEYS: tuple = ("git_read", "git_write", "system", "execution")


def _canonicalize_worker_footprint(
    footprint: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Canonicalize a worker definition's DECLARED permission footprint.

    Runs at the definition LOAD boundary (``WorkerThread.__init__``), before
    the footprint is used as the spawn-time ``_restrictive_merge`` input or
    handed to ``AgentConfig.permission_footprint``.  The security gate itself
    stays strict -- it never learns the legacy vocabulary.

    * the legacy ``git_read`` / ``git_write`` / ``system`` / ``execution``
      grains are stripped, exactly as ``_canonicalize_session_permissions``
      strips them from the merged session dict;
    * the one documented legacy *value* form -- ``network`` persisted as a
      boolean -- is folded onto the canonical level, mirroring
      ``SessionPermissions.coerce_legacy_network_permission`` (``True`` ->
      ``write``, ``False`` -> ``banned``);
    * canonical footprints are returned unchanged (idempotent), so re-loading
      a definition is a no-op;
    * any other unknown category raises ``ValueError`` naming the key AND its
      value: an unresolvable definition is a loud load error, not a silent
      deny.

    Values that are neither canonical nor the legacy bool form are passed
    through untouched -- the gate's strict membership test still hard-denies
    them, and callers rely on the merge-only string vocabulary (``"none"`` /
    ``"deny"`` / ``"allow"``) surviving verbatim.
    """
    from security.resource_catalog import RESOURCE_CATALOG

    if not footprint:
        return {}
    if not isinstance(footprint, dict):
        raise ValueError(
            "worker permission footprint must be a mapping, got "
            f"{type(footprint).__name__}: {footprint!r}"
        )
    canonical: Dict[str, Any] = {}
    for key, value in footprint.items():
        if key in _LEGACY_FOOTPRINT_KEYS:
            continue
        if key not in RESOURCE_CATALOG:
            raise ValueError(
                f"worker permission footprint declares unknown category {key!r} "
                f"= {value!r}; canonical categories are {sorted(RESOURCE_CATALOG)}"
            )
        if key == "network" and isinstance(value, bool):
            value = "write" if value else "banned"
        canonical[key] = value
    return canonical


# ---------------------------------------------------------------------------
# Worker-scoped container cleanup helpers
# ---------------------------------------------------------------------------

# Worker-scoped container ownership/cleanup helpers live in
# tools.workspace.worker_container (extraction); the constants and functions
# are re-exported at the top of this module.


# ---------------------------------------------------------------------------
# S7b-4a: adoption at spawn
# ---------------------------------------------------------------------------


def _adopt_predecessor_journal(
    worker_dir: Path,
    fence: CommandFence,
    generation: int,
) -> Optional[InflightRecord]:
    """Take over the turn a DEAD predecessor left in *worker_dir*, or ``None``.

    This is the S7b-4 one-and-only adoption path: it runs ONCE per incarnation,
    at turn start, BEFORE WRITE POINT 1, because ``write_inflight`` FAILS CLOSED
    on a foreign record that is still work in progress -- so without this the
    successor's own WP1 write would be refused and its turn would run
    unjournalled (the wedge pinned by
    ``tests/test_worker_adoption_at_spawn.py``).

    MODULE-LEVEL on purpose: the call site references it by bare module global
    name, so a test can neuter adoption with a single
    ``monkeypatch.setattr(worker_thread_module, "_adopt_predecessor_journal",
    ...)`` -- exactly how ``write_inflight`` itself is faked.

    WHOSE DIRECTORY THIS IS (why spawn can adopt at all): the spawn-side
    ``_worker_registry`` iteration (``worker_thread.py:5294-5300``) filters on
    ``is_alive()``, so after a crash ``live_same == []``; with
    ``explicit_iid is None`` and an empty ``live_same`` the chain at
    ``worker_thread.py:5301-5392`` therefore falls through to ``spawn_iid = 1``,
    the instance label stays the BARE worker name
    (``worker_registry.py:129-139``) and ``self._worker_dir`` is
    ``workers/<name>`` -- the very directory the dead incarnation used.  (An
    earlier design note wrongly assumed the successor got a fresh directory and
    would need a label change; it does not, and none was made.)  So the
    predecessor's record is HERE, and it is addressed to a LOWER epoch --
    ``_generation`` is allocated per worker directory, monotonically
    (``_allocate_generation``).

    The LOOK is ``read_inflight_unfenced`` because this incarnation's fence can
    never match a predecessor's epoch by definition; the TAKE is
    ``adopt_inflight``, the one deliberate displacing writer. Neither is
    reimplemented here, and neither refusal list is changed.

    Adopts exactly ONE status: ``inflight`` -- the state a HARD-KILLED / crashed
    incarnation leaves behind (it never reached the park flip). The others are
    deliberately NOT this path's:

    * ``recoverable`` -- the DETACH park's record; replaying it is the RESUME
      path's business, not spawn's.
    * ``cancelled`` -- terminal: a killed job is never resurrected (R3/R6).
      ``adopt_inflight`` refuses these too; the check here means we never even
      ask.
    * ``adopted`` -- somebody else already took it; do not double-adopt.

    ...and only when the record's on-disk epoch is STRICTLY LOWER than
    *generation*: a record at this epoch or a later one is not a dead
    predecessor's, and this same check is what makes adoption ONCE -- the
    adoption write restamps the record to THIS fence, so every later turn's
    look sees its own epoch and declines.

    ATTEMPTS: the caller owns R3, and ``adopt_inflight`` only VERIFIES that the
    handed-in count follows on, so the bumped record is built here. A record
    already at :data:`MAX_ATTEMPTS` is NOT adopted (building attempt 4 would
    raise) -- terminalizing that job ``interrupted`` is the resume path's R3
    decision, not spawn's.

    Returns the adopted record (status ``adopted``, stamped with *fence*,
    ``attempts`` bumped by one) or ``None`` for every "nothing to do" case --
    no record, an unusable/corrupt journal, a status this path does not own, an
    epoch that is not older, a refusal from ``adopt_inflight``. NOTHING is
    logged here for those: a predecessor's leftovers are normal, not an
    incident, and a journal must never be able to fail or noisy-fail a turn.
    """
    try:
        existing, axes, _read_reason = read_inflight_unfenced(worker_dir)
        if existing is None:
            # Nothing journalled, or unusable: nothing to adopt, nothing to say.
            return None
        if existing.status != STATUS_INFLIGHT:
            # ``recoverable`` belongs to the RESUME path (the detach park left
            # it for replay), ``cancelled`` is terminal, ``adopted`` is taken.
            return None
        epoch = (axes or {}).get("epoch")
        if type(epoch) is not int or epoch >= generation:
            # Not a DEAD predecessor's record -- either our own (already
            # adopted, or written by this turn) or a newer incarnation's.
            return None
        if existing.attempts + 1 > MAX_ATTEMPTS:
            # R3 territory: the caller must terminalize as ``interrupted``.
            return None
        bumped = InflightRecord(
            query_id=existing.query_id,
            query=existing.query,
            status=STATUS_INFLIGHT,
            attempts=existing.attempts + 1,
            requested_by=existing.requested_by,
            created_at=existing.created_at,
            updated_at=utcnow_iso(),
            last_tool=existing.last_tool,
            reason=existing.reason,
        )
        adopted, _refusal = adopt_inflight(worker_dir, fence, bumped)
        return adopted
    except Exception as exc:  # bookkeeping must never fail a turn
        log("WARNING", "tools.worker",
            f"[journal] adoption failed for worker '{fence.worker_name}': {exc}")
        return None


# ---------------------------------------------------------------------------
# Initial-context ("briefing") delivery
# ---------------------------------------------------------------------------

#: Prefix of every initial-context payload. It is the marker the F1/WLM prune
#: boundaries look for, so it must stay stable across the role change below.
INITIAL_CONTEXT_PREFIX = "Initial context: "

#: Prefix of every FOLLOW-UP context payload (``action='query'`` /
#: ``'submit_query'``). Deliberately NOT the initial-context prefix:
#: ``is_initial_context_message()`` and the spawn-side prune/dedupe boundaries
#: key on that exact marker, so reusing it for a follow-up query would make the
#: query look like an initial-context message and corrupt that handling.
FOLLOWUP_CONTEXT_PREFIX = "Follow-up context for this query:"


def initial_context_message(initial_context: Dict[str, Any]) -> Dict[str, Any]:
    """Build the history message that carries a worker's initial context.

    The payload is exactly what workers have always been given; only the ROLE
    changed, from ``system`` to ``user``. It rides on the conversation's first
    ``role='user'`` message so it is ordinary conversational history, because
    ``LLMClient.ensure_system_prompt`` deletes every system message that is not
    a conversation summary (agent/core/llm_client.py:229-234). That is what
    used to happen to a briefing delivered as ``role='system'``: it was
    stripped before the first LLM call, once by ``Agent.__init__``
    (agent/core/agent.py:554) and again at the start of every turn
    (agent/core/agent.py:1562), so the worker never saw its brief.
    """
    return {
        "role": "user",
        "content": (
            f"{INITIAL_CONTEXT_PREFIX}"
            f"{json.dumps(initial_context, default=str, ensure_ascii=True)}"
        ),
    }


def is_initial_context_message(message: Any) -> bool:
    """True when *message* carries an initial-context payload.

    Role-agnostic on purpose: a ``context.json`` written before the delivery
    role changed holds ``role='system'``, a current one holds ``role='user'``,
    and the F1/WLM prune boundaries have to keep finding both.
    """
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    return isinstance(content, str) and content.startswith(INITIAL_CONTEXT_PREFIX)


# ---------------------------------------------------------------------------
# Worker thread
# ---------------------------------------------------------------------------

class WorkerThread(threading.Thread):
    """
    A worker runs as a daemon thread on the host, reusing the agent's
    LLM provider configuration.  It maintains a conversation context
    persisted to ``<workspace_dir>/workers/<name>/context.json``.

    Lifecycle
    ---------
    ready  ──spawn──▶  ready  ──query──▶  busy  ──done──▶  ready
      ▲                    │            │                   │
      │                    ├──stop──▶  stopping ──▶ stopped │
      │                    ├──pause──▶ paused ──resume──▶  ready
      │                    └──error──▶  error               │
      └───────────────────────────  spawn again  ◄──────────┘
    """

    def __init__(
        self,
        name: str,
        definition: dict,
        agent_config: dict,
        workspace_dir: Path,
        tool_classes: Optional[Dict[str, type[ToolBase]]] = None,
        session_permissions: Optional[Dict[str, Any]] = None,
        project_root: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        session_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        instance_id: int = 1,
        container_manager: Optional[Any] = None,
        *,
        max_container_count: Optional[int] = None,
        max_token_usage: Optional[int] = None,
        max_runtime_s: Optional[float] = None,
        frame_sink: Optional[Callable[[Any], None]] = None,
        initial_context: Optional[Dict[str, Any]] = None,
    ) -> None:
        # S4a: assigned before anything else -- every other attribute below can
        # reach a helper that calls _write_status_file, so the sink is set
        # first rather than left to a getattr() default.
        self._frame_sink = frame_sink
        self.instance_id = instance_id
        instance_label = _WorkerRegistry.instance_label(name, instance_id)
        super().__init__(daemon=True, name=f"worker-{instance_label}")
        self.worker_name = name
        self.definition = definition
        self._agent_config_dict = agent_config
        self.session_id = session_id
        self.workspace_id: Optional[str] = workspace_id
        # Container manager used to stop/remove containers owned by this
        # worker at teardown (see _cleanup_worker_containers).
        self._container_manager: Optional[Any] = container_manager
        # Process mode rebuilds this worker in a child interpreter from a
        # spec whose ``workspace_dir`` crossed the boundary as a ``str``
        # (``worker_runtime_process.py:205``); coerce once, here, so every
        # caller -- thread, child and test -- gets the same Path maths.
        if not isinstance(workspace_dir, Path):
            workspace_dir = Path(workspace_dir)
        self._worker_dir = workspace_dir / "workers" / instance_label
        self._worker_dir.mkdir(parents=True, exist_ok=True)
        # The workspace root under its own name (as passed, not re-derived by
        # walking back out of ``workers/<label>/``): process mode rebuilds this
        # worker in a child interpreter from ``spec_from_worker``, which reads
        # ``workspace_dir`` off the object (§3.3).
        self.workspace_dir: Path = workspace_dir

        # F2: generation token — a monotonic per-worker counter that guards
        # context.json against stale-thread overwrites after a force-spawn
        # (a still-alive old thread must not clobber the replacement's file).
        self._generation: int = self._allocate_generation()

        # Tool classes available to this worker (name -> class)
        self._tool_classes: Dict[str, type[ToolBase]] = tool_classes or {}

        # Frozen capability set: resolved lazily from ``definition["tools"]``
        # on first use and cached for the life of this worker (§4.2 / §7.2).
        # Annotated as a string because the registry import is deliberately
        # lazy (it pulls in ``tools``, which imports this module's package).
        self._capability_registry: Optional["CapabilityRegistry"] = None

        # Session permissions for gate-checking tool calls
        self._session_permissions: Dict[str, Any] = session_permissions or {}
        # Worker-level permission footprint from definition.  Canonicalized
        # once here (definition LOAD boundary): legacy bool ``network`` and
        # legacy grains never reach the security gate.
        self._permission_footprint: Dict[str, Any] = _canonicalize_worker_footprint(
            definition.get("permission_footprint") or definition.get("worker_permissions", {})
        )

        # Project root from the session (resolved from workspace config)
        self._project_root: Optional[str] = project_root

        # Session-scoped configuration (the nested
        # ``agent_config["session_config"]`` dict) is read once here so the
        # timeout, retry and resource-budget chains below all share it.
        _session_cfg = (agent_config or {}).get("session_config") or {}
        if not isinstance(_session_cfg, dict):
            _session_cfg = {}

        # Override timeout (SESSION-owned chain; see docs/param_ownership_map.md):
        # explicit constructor param > worker definition ("timeout_seconds") >
        # agent_config ("worker_timeout_seconds") > session_config
        # ("worker_timeout_seconds") > WORKER_TIMEOUT_SECONDS module default.
        # NOTE: definition.get("timeout_seconds", WORKER_TIMEOUT_SECONDS) would
        # return None if the key exists with a null value, so each hop checks
        # for None explicitly.
        _def_timeout = definition.get("timeout_seconds")
        if _def_timeout is None:
            _def_timeout = (agent_config or {}).get("worker_timeout_seconds")
        if _def_timeout is None:
            _def_timeout = _session_cfg.get("worker_timeout_seconds")
        if _def_timeout is None:
            _def_timeout = WORKER_TIMEOUT_SECONDS
        self._timeout_seconds: int = (
            timeout_seconds
            if timeout_seconds is not None
            else _def_timeout
        )

        # Worker query retry budget (SESSION-owned chain): worker definition
        # ("max_retries") > agent_config ("worker_max_retries") >
        # session_config ("worker_max_retries") > WORKER_MAX_RETRIES default.
        _retries = definition.get("max_retries")
        if _retries is None:
            _retries = (agent_config or {}).get("worker_max_retries")
        if _retries is None:
            _retries = _session_cfg.get("worker_max_retries")
        if _retries is None:
            _retries = WORKER_MAX_RETRIES
        self._worker_max_retries: int = _retries

        # Per-worker resource budgets (Phase 3, item 6).  Resolution order:
        # explicit constructor param → session_config (the nested
        # ``agent_config["session_config"]`` dict) → module defaults
        # (containers=4, tokens/runtime=unlimited).

        self._max_container_count: int = WORKER_DEFAULT_MAX_CONTAINERS
        if max_container_count is not None:
            self._max_container_count = max_container_count
        else:
            _limits = _session_cfg.get("container_limits")
            if isinstance(_limits, dict) and _limits.get("max_containers") is not None:
                try:
                    self._max_container_count = max(1, int(_limits["max_containers"]))
                except (TypeError, ValueError):
                    self._max_container_count = WORKER_DEFAULT_MAX_CONTAINERS

        self._max_token_usage: Optional[int] = WORKER_DEFAULT_MAX_TOKENS
        if max_token_usage is not None:
            self._max_token_usage = max_token_usage
        elif _session_cfg.get("max_token_usage") is not None:
            try:
                self._max_token_usage = int(_session_cfg["max_token_usage"])
            except (TypeError, ValueError):
                self._max_token_usage = WORKER_DEFAULT_MAX_TOKENS

        self._max_runtime_s: Optional[float] = WORKER_DEFAULT_MAX_RUNTIME_S
        if max_runtime_s is not None:
            self._max_runtime_s = max_runtime_s
        elif _session_cfg.get("max_runtime_s") is not None:
            try:
                self._max_runtime_s = float(_session_cfg["max_runtime_s"])
            except (TypeError, ValueError):
                self._max_runtime_s = WORKER_DEFAULT_MAX_RUNTIME_S

        # Monotonic start of this worker's lifetime; the runtime budget is
        # measured against it (per-worker lifetime, not per-query).
        self._budget_started_at: float = time.monotonic()

        # Runtime state
        self.status: str = "ready"      # ready | busy | paused | completed | error | stopping | stopped
        self.current_task: Optional[str] = None
        self.error: Optional[str] = None
        self.last_heartbeat: Optional[str] = None
        self._last_heartbeat_monotonic: float = 0.0
        # Worker tray truth fields — persisted in status.json, exposed via
        # GET /workspace/{ws_id}/workers and published in worker_status WS
        # events. ``paused_manually`` is derived from ``_manual_only_pause``
        # (defined below), never a separate attribute.
        self.started_at: Optional[str] = None
        self.last_query_at: Optional[str] = None
        self._last_reasoning: Optional[str] = None

        # Telemetry tracking
        self._tool_call_count: int = 0
        self._timeout_triggered: bool = False
        # Idempotency guard for worker-scoped container cleanup: cleanup
        # runs at most once per thread, so it is safe to call from both
        # run() teardown and _action_stop finally paths.
        self._containers_cleaned: bool = False
        self._wlm_soft_warning_received: bool = False
        self._final_token_usage: Optional[int] = None
        self._respond_metadata: Dict[str, Any] = {}  # captures status/confidence/meta from last Respond call
        # Legacy query id of the in-flight attempt (set by run() when it
        # dequeues a (query_id, query) tuple; None for plain-string producers
        # such as the WLM handler). Stamped into the envelope so send_query
        # can correlate replies even when multiple callers overlap.
        self._current_query_id: Optional[str] = None
        # S7b-3 (Option K): the reason a DELIBERATE kill cancelled this turn
        # with, or None. Set by ``_journal_cancel`` (i.e. only on a kill that
        # opted in) and consulted at WRITE POINT 3: the loop's exit must not
        # erase the ``cancelled`` record that very kill just left, because that
        # record is the terminal evidence the kill exists to produce. Cleared
        # per turn, so it can never leak into a later turn's exit.
        self._journal_cancel_reason: Optional[str] = None
        # S7b-4a: the record a DEAD predecessor left in this worker's
        # directory, once THIS incarnation took it over at turn start. ``None``
        # means nothing has been adopted yet, which is also what makes adoption
        # happen exactly ONCE per incarnation. The stored record is the adopted
        # one (status ``adopted``, attempts bumped), kept for observability.
        self._journal_adopted: Optional[InflightRecord] = None
        # S7b-4b: the attempt count a REPLAYED turn must carry to disk, keyed by
        # the adopted query id. Written at adoption, POPPED by WRITE POINT 1 of
        # the turn that is that replay -- so an ordinary turn writes 0 and the
        # count can never bleed into a later turn.
        self._journal_replay_attempts: Dict[str, int] = {}
        # Per-caller reply channel (set by run() when it dequeues a
        # (query_id, query, reply_q) triple from send_query). Envelopes are
        # routed to this private queue when set, so concurrent send_query
        # callers can never steal each other's replies; the shared
        # _output_queue remains the fallback for plain-string producers
        # (WLM handler, spawn auto-query). None between queries.
        self._current_reply_queue: Optional[queue.Queue] = None

        # Cached authoritative token count from agent's token_update events
        self._cached_context_tokens: Optional[int] = None

        # F1 markers: last query dequeued, and last query that completed.
        # Persisted in context.json so a force-respawn can tell whether the
        # previous attempt finished (last_query == last_completed_query) or
        # was cut short by a soft timeout / force-stop. If it never completed
        # and the new spawn carries a DIFFERENT query, the stale partial
        # attempt is pruned before the new context is merged (no doubling).
        self._last_query: Optional[str] = None
        self._last_completed_query: Optional[str] = None
        # WLM query id of the last attempt — persisted alongside the F1 markers
        # so a WLM-enabled force-respawn can prune abandoned attempts exactly.
        self._last_query_id: Optional[str] = None
        # Cumulative count of stale/abandoned attempt prunes performed by this
        # worker (F1 + WLM prune paths). Persisted in context.json so it
        # survives restarts; exposed via GET /api/workspace/{ws_id}/workers
        # as ``pruned_since_last_query``.
        self._pruned_since_last_query: int = 0

        # Agent instance + WorkerContext (created lazily in run())
        self._agent: Optional[Any] = None
        self._worker_ctx: Optional[Any] = None
        # The initial context ("briefing") can arrive either here, at
        # construction, or be assigned by the spawn action after construction
        # (tools/workspace/worker_thread.py:_action_spawn). Both end up
        # identical; the constructor argument exists because process mode
        # rebuilds this thread in a child interpreter from a picklable spec
        # (worker_runtime_process.spec_from_worker) and cannot assign attributes
        # to an object it has not built yet.
        self._initial_context: Optional[Dict[str, Any]] = (
            dict(initial_context) if initial_context else None
        )

        # Per-worker EventBus (created lazily in run() before Agent)
        self._event_bus: Optional[Any] = None

        # Presenter components (created lazily in _run_tool_loop)
        self._state_bridge: Optional[Any] = None
        self._event_processor: Optional[Any] = None
        self._worker_bus_adapter: Optional[Any] = None

        # Inter-thread communication
        self._input_queue: queue.Queue = queue.Queue()
        self._output_queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        # PARK primitive (driven by detach()/resume()) -- deliberately NOT the user-facing pause
        self._park_event = threading.Event()
        self._resume_event = threading.Event()
        # Per-WORKER, per-TURN cancellation token -- a THIRD, distinct signal:
        #   _stop_event   = terminal kill latch (never cleared; guards resume)
        #   _park_event  = park flag (driven by detach()/resume())
        #   _turn_cancel  = in-flight cancellation token consulted by tools
        # Cleared at the start of EVERY query, set on graceful stop (detach() /
        # cross-process 'stop') and on kill().  Bound to the executing tool
        # thread (tools.sleep_tool.bind_turn_cancel) so a tool sees only its own
        # worker's token: one worker's kill can never cancel another's tool.
        self._turn_cancel = threading.Event()
        # PHASE 2.1b: ordered bookkeeping for the per-instance FIFO query
        # queue. ``_input_queue`` is the queue itself (already a FIFO with a
        # single query in flight, drained head-first); this deque mirrors the
        # job_ids of the queries queued through ``enqueue_query`` in the SAME
        # order, so the pending set is introspectable (panel queue count +
        # job IDs) and can be rejected as a unit on TTL expiry or kill.
        self._queued_job_ids: "collections.deque[str]" = collections.deque()
        self._queued_lock = threading.Lock()
        # Execution tracking (default-on): every in-flight blocking tool call
        # is registered here unconditionally (WLM feature flag on or off) so
        # the interrupt paths (stop(), stale-recovery, WLM timeout/stop) can
        # terminate it. In flag-on mode the WorkerSupervisor SHARES this
        # tracker (see _get_wlm), so its terminate/clear transitions observe
        # the same executions.
        self._execution_tracker = ExecutionTracker()
        # UNIT C: True when this worker was paused with the manual-only flag.
        # A later query must NOT auto-resume such a worker (see _action_spawn's
        # sole-paused path); it has to be resumed explicitly via the 'resume'
        # action (which clears the marker).
        self._manual_only_pause: bool = False
        # PHASE 2.1a: 'stop' DETACHES (thread parks; containers and the
        # registry entry survive) and 'kill' terminates.  _detach_requested
        # marks a park entered because of a detach request; _detached_at
        # stamps its start for the detached-worker TTL sweep
        # (PeriodicStaleCheck).
        self._detach_requested: bool = False
        self._detached_at: Optional[float] = None
        # Digest of the last ``command.json`` payload that was deliberately NOT
        # acted on (an unknown action).  Lets _poll_command ignore a stray
        # command file without unlinking it -- an unlink there would race with
        # an operator writing the next real command.

    # ── public API called from the tool thread ─────────────────────

    @property
    def event_bus(self):
        """Return the per-worker EventBus instance."""
        return self._event_bus

    @property
    def owner_identity(self) -> str:
        """Owner identity stamped on containers this worker creates.

        Format: ``<session_id or 'unknown'>:<worker_name>`` — the single
        source of truth used BOTH when stamping the ``thoughtmachine.worker``
        docker label (via the worker-name context var in ``_run_tool_loop``)
        and when comparing labels at teardown
        (``_is_worker_owned_container``).
        """
        return f"{self.session_id or 'unknown'}:{self.worker_name}"

    @property
    def instance_label(self) -> str:
        """Return this instance's display label (``name`` or ``name#iid``)."""
        return _WorkerRegistry.instance_label(self.worker_name, self.instance_id)

    @property
    def max_context_tokens(self) -> int:
        """
        Return the context window limit for this worker's model.

        Derives the limit from the model name in the injected
        ``_agent_config_dict``, using the same lookup as
        ``TokenCounter.get_model_context_window()``.

        Returns:
            Maximum context tokens (int). Defaults to 128000.
        """
        model = (self._agent_config_dict or {}).get("model", "").lower()
        windows = {
            "gpt-4-32k": 32768,
            "gpt-4-turbo": 128000,
            "gpt-4o": 128000,
            "gpt-4": 8192,
            "gpt-3.5-turbo": 16385,
            "gpt-3.5": 16385,
            "deepseek": 128000,
            "claude-3-opus": 200000,
            "claude-3-sonnet": 200000,
            "claude-3-haiku": 200000,
            "claude": 200000,
        }
        for key, window in windows.items():
            if key in model:
                return window
        return 128000

    def get_current_context_tokens(self) -> int:
        """
        Return the current conversation's token count.

        Prefers the authoritative value from ``StateBridge.context_length``
        (updated by ``EventProcessor`` from ``token_update`` events). Falls
        back to the legacy ``_cached_context_tokens`` (still written by the
        summarization-detection code), then estimation via
        ``WorkerContext.estimated_context_tokens()``.

        Returns:
            Token count (int).
        """
        if self._state_bridge is not None and self._state_bridge.context_length > 0:
            return self._state_bridge.context_length
        if self._cached_context_tokens is not None:
            return self._cached_context_tokens
        if self._worker_ctx is not None:
            return self._worker_ctx.estimated_context_tokens()
        return 0

    def _budget_check(self) -> Optional[Dict[str, str]]:
        """Return a fail-closed error payload when a per-worker budget is exceeded.

        Checks the runtime budget first (monotonic elapsed time vs
        ``_max_runtime_s``), then the current context-token count vs
        ``_max_token_usage``. Returns ``None`` while both budgets are within
        limits (or unset). Never raises.
        """
        if self._max_runtime_s is not None:
            if time.monotonic() - self._budget_started_at > self._max_runtime_s:
                return {
                    "error": f"Worker runtime budget exceeded ({self._max_runtime_s}s)",
                    "reason": "runtime_budget",
                }
        if self._max_token_usage is not None:
            current = self.get_current_context_tokens()
            if current > self._max_token_usage:
                return {
                    "error": f"Worker token budget exceeded ({current} > {self._max_token_usage})",
                    "reason": "token_budget",
                }
        return None

    @staticmethod
    def _drain_queue(q: queue.Queue) -> None:
        """Discard everything currently queued on ``q`` (get_nowait to empty)."""
        while True:
            try:
                q.get_nowait()
            except queue.Empty:
                break

    def _drain_output_queue(self) -> None:
        """Discard any stale replies currently sitting in the output queue.

        A reply produced for a previous (timed-out or superseded) query must
        never satisfy a later send_query call — drain before enqueueing and
        again on timeout so late replies cannot leak into the next call.
        """
        self._drain_queue(self._output_queue)

    def _emit_reply(self, payload: str, nowait: bool = False) -> None:
        """Route a reply to the current caller's private reply queue when one
        is set (legacy send_query correlation), else to the shared output
        queue (plain-string producers: WLM handler, spawn auto-query).
        """
        target = (
            self._current_reply_queue
            if self._current_reply_queue is not None
            else self._output_queue
        )
        if nowait:
            target.put_nowait(payload)
        else:
            target.put(payload)

    def _reply_matches_query(self, response: str, query_id: str) -> bool:
        """Return True when a queue item is the envelope for ``query_id``.

        Envelopes produced by the run() loop carry a ``query_id`` key; plain
        strings (error payloads, legacy replies) and envelopes without a
        matching id cannot satisfy a correlated send_query call.
        """
        try:
            payload = json.loads(response)
        except (json.JSONDecodeError, TypeError):
            return False
        if not isinstance(payload, dict) or "query_id" not in payload:
            return False
        return payload["query_id"] == query_id

    def _wait_interrupt_reason(self) -> Optional[str]:
        """``reason`` when the CALLER's turn was paused/cancelled, else ``None``.

        Read from the bindings of the thread that is blocked in ``send_query``
        (the turn-running thread the controller/agent binds), exactly as
        ``Sleep.execute`` and ``Worker._join_cancel_check`` do:

          * **pause** (``tools.cancellation.pause_requested``): the SHARED event
            the controller owns.  It is the only pause signal that can cross
            into a thread blocked inside a tool, which is why the controller
            binds it to the turn-running thread for the turn.
          * **turn cancel**: the turn token (``detach()``/``kill()``/a
            cross-process 'stop').  Only a BOUND token counts, so an unbound
            thread keeps the legacy full-deadline wait -- a leftover fallback
            interrupt must never abort an unrelated wait.

        Pause is checked first: it is a request to wind down, not a cancel, and
        the two are distinguished by ``reason`` (see ``Sleep``).
        """
        from tools.sleep_tool import _tls, cancel_reason, is_interrupted, pause_requested
        try:
            if pause_requested():
                return "paused"
            if getattr(_tls, "event", None) is not None and is_interrupted():
                return cancel_reason() or "turn_cancelled"
        except Exception:  # pragma: no cover - defensive
            return None
        return None

    def _interrupted_reply(self, query_id: str, reason: str, started: float) -> str:
        """The envelope ``send_query`` returns when its WAIT is interrupted.

        A JSON string, like every reply that method returns, using the same
        interruption vocabulary as ``join``/``sleep`` (``status='interrupted'``,
        ``interrupted=True``, ``reason``).  Only the WAIT is over: the worker may
        still be running the query, so the envelope says so rather than
        implying the turn completed.
        """
        return json.dumps({
            "status": "interrupted",
            "interrupted": True,
            "reason": reason,
            "query_id": query_id,
            "worker_name": self.worker_name,
            "elapsed_seconds": round(time.monotonic() - started, 1),
            "note": (
                "Wait interrupted; the query was NOT cancelled and the worker "
                "may still be running it."
            ),
        }, default=str)

    def send_query(self, query: str, timeout: float = 120.0) -> str:
        """Send a query to this worker and block for a response.

        The wait is interruptible: when the CALLING thread's pause event or
        turn-cancel token is set, it returns an ``interrupted`` envelope within
        ``QUERY_WAIT_SLICE_SECONDS`` instead of blocking out ``timeout``.  Only
        the wait ends -- the query itself is not cancelled.
        """
        # Implicit UNPAUSE: a new query IS the resume.
        #
        # Pause is a wind-down, not a latch -- so a query arriving on a
        # pause-restricted worker must find it RUNNING again *before* the turn
        # is dispatched, otherwise the model is nudged to conclude a turn it
        # has only just been asked to start.  A worker has no controller in its
        # object graph (no pause_event to poke): the signals are this thread's
        # own agent state plus the two park flags.
        #
        # The release is OWNER-SCOPED -- Agent.leave_reading_mode('pause') ->
        # AgentState.clear_restriction('pause') -- so a 'token'/'timeout'/'turn'
        # restriction owned by the agent's monitors survives untouched:
        # clearing it here would let an operator escape a context-window
        # restriction just by asking another question.  The park flags move to
        # the running position ONLY on this path; an ordinary query leaves them
        # exactly as it found them.
        _qp_agent = getattr(self, "_agent", None)
        _qp_state = getattr(_qp_agent, "state", None)
        if _qp_state is not None and getattr(_qp_state, "restriction_reason", None) == "pause":
            try:
                _qp_agent.leave_reading_mode("pause")
            except Exception as exc:
                log("WARNING", "tools.worker",
                    f"Implicit unpause failed for worker '{self.worker_name}': {exc}")
            self._park_event.clear()
            self._resume_event.set()
            log("DEBUG", "tools.worker",
                f"Implicit unpause for worker '{self.worker_name}': new query resumes a "
                f"pause-restricted worker (one query = resume; owner-scoped 'pause' "
                f"release, park flags -> running)")
        # Workspace Lifecycle Manager fast path (feature-flagged, default off)
        try:
            used, reply = self._process_query_via_wlm(query, timeout=timeout)
        except TimeoutError:
            self._publish_global_event(EventType.WORKER_TIMEOUT, {
                "status": self.status,
                "reason": "query_timeout",
                "timeout_seconds": timeout,
                "last_heartbeat": self.last_heartbeat,
                **({"query_id": self._current_query_id}
                   if self._current_query_id is not None else {}),
            })
            raise
        if used:
            return reply
        # Correlate this call with a fresh query id and drain any stale
        # replies left over from a previous timed-out query, so a late reply
        # can never satisfy this (or the next) call. The worker run() loop
        # dequeues the (query_id, query) tuple and echoes the id back in the
        # envelope it produces.
        self._drain_output_queue()
        query_id = uuid.uuid4().hex
        # Each call gets its own private reply queue, carried to the run()
        # loop inside the input item. The worker routes its envelope to this
        # queue, so two concurrent send_query callers can never steal each
        # other's replies (the shared _output_queue is only a fallback for
        # plain-string producers). The correlation check below is kept as
        # defense in depth.
        reply_q: queue.Queue = queue.Queue()
        self._input_queue.put((query_id, query, reply_q))
        started = time.monotonic()
        deadline = started + timeout
        while True:
            remaining = deadline - time.monotonic()
            # One SLICE at a time.  A reply still arrives on the private
            # queue, but the wait never blocks longer than
            # QUERY_WAIT_SLICE_SECONDS without re-reading the caller's
            # pause/cancel signals, so an interruption lands within one slice
            # instead of at the deadline.
            slice_timeout = min(QUERY_WAIT_SLICE_SECONDS, remaining)
            try:
                response = reply_q.get(
                    timeout=slice_timeout if slice_timeout > 0 else 0
                )
            except queue.Empty:
                # Interruption BEFORE the deadline: a pause (or a turn cancel)
                # raised while this call is blocked is a request to wind the
                # wait down, not a timeout.  Reporting it as "timeout" is what
                # kept the pause invisible for minutes (audit D16) -- so this
                # check deliberately precedes the deadline check.
                interrupt_reason = self._wait_interrupt_reason()
                if interrupt_reason is not None:
                    return self._interrupted_reply(query_id, interrupt_reason, started)
                if time.monotonic() < deadline:
                    # Slice elapsed, deadline not reached: keep waiting for our
                    # own envelope.
                    continue
                # Drain our private queue AND the shared fallback queue so a
                # late reply (on either channel) cannot leak into the next
                # call. Our queue is dropped anyway (local to this call), but
                # the drain keeps the invariant explicit.
                self._drain_queue(reply_q)
                self._drain_output_queue()
                # Fallback: read heartbeat from status.json if in-memory is None
                hb = self.last_heartbeat
                if hb is None:
                    try:
                        status_path = self._worker_dir / "status.json"
                        if status_path.exists():
                            data = json.loads(status_path.read_text(encoding="utf-8"))
                            hb = data.get("last_heartbeat")
                    except (OSError, json.JSONDecodeError):
                        pass
                if hb:
                    try:
                        hb_dt = datetime.fromisoformat(hb)
                        age = (datetime.now(timezone.utc) - hb_dt).total_seconds()
                        detail = f" (last heartbeat {age:.0f}s ago)"
                    except (ValueError, TypeError):
                        detail = f" (last heartbeat: {hb})"
                else:
                    detail = ""
                self._publish_global_event(EventType.WORKER_TIMEOUT, {
                    "status": self.status,
                    "reason": "query_timeout",
                    "timeout_seconds": timeout,
                    "last_heartbeat": self.last_heartbeat,
                    **({"query_id": self._current_query_id}
                       if self._current_query_id is not None else {}),
                })
                raise TimeoutError(
                    f"Worker '{self.worker_name}' did not respond within {timeout}s{detail}"
                )
            if self._reply_matches_query(response, query_id):
                return response
            # Non-matching item: a stale envelope or another caller's reply.
            # Discard it and keep waiting for our own envelope.
            continue

    # ------------------------------------------------------------------
    # Workspace Lifecycle Manager wiring (feature-flagged, default off).
    # When enabled, send_query delegates to the supervisor; the legacy
    # queue-based path above remains byte-for-byte untouched and is used
    # whenever the flag is off or the supervisor is unavailable.
    # ------------------------------------------------------------------

    def _get_wlm(self):
        """Return the lazily-created WorkerSupervisor, or None when disabled."""
        existing = getattr(self, "_wlm", None)
        if existing is not None:
            return existing
        if not self._wlm_flag_enabled():
            return None
        from infra.workspace_lifecycle_manager import WorkerSupervisor, HARD_TIMEOUT

        supervisor = WorkerSupervisor(
            worker_id=self.worker_name,
            container_manager=getattr(self, "_container_manager", None),
            resource_container_manager=None,
            feature_flag_check=lambda: True,
            session_id=self.session_id,
            permissions_provider=lambda: self._session_permissions,
            max_container_count=getattr(
                self, "_max_container_count", WORKER_DEFAULT_MAX_CONTAINERS
            ),
        )

        def _wlm_handler(query, query_id):
            """Bridge the supervisor to the worker's existing run() loop."""
            self._input_queue.put(query)
            try:
                reply = self._output_queue.get(timeout=HARD_TIMEOUT)
            except queue.Empty:
                reply = json.dumps({"error": "Worker did not respond in time."})
            supervisor._publish_reply(query_id, reply)

        supervisor.query_handler = _wlm_handler
        # Share the worker's default-on execution tracker so the supervisor's
        # terminate/clear transitions see the same executions _run_tool_loop
        # registered (one tracker per worker, flag on or off). Guarded with
        # getattr for duck-typed fakes that lack the attribute (wiring tests).
        _tracker = getattr(self, "_execution_tracker", None)
        if _tracker is not None:
            supervisor.execution_tracker = _tracker
        self._wlm = supervisor
        log("INFO", "tools.worker",
            f"Workspace Lifecycle Manager enabled for worker '{self.worker_name}'")
        return supervisor

    def _wlm_flag_enabled(self) -> bool:
        """True when the workspace lifecycle manager feature flag is set.

        The flag is read from (in priority order):
        1. top-level ``use_workspace_lifecycle_manager`` in the injected
           agent-config dict (production plumbing: SessionConfig →
           AgentConfig → ToolExecutor → Worker tool),
        2. the legacy nested ``session_config`` dict (tests / live smoke),
        3. the worker definition.
        """
        acfg = self._agent_config_dict or {}
        if acfg.get("use_workspace_lifecycle_manager"):
            return True
        cfg = acfg.get("session_config")
        if isinstance(cfg, dict) and cfg.get("use_workspace_lifecycle_manager"):
            return True
        return bool((self.definition or {}).get("use_workspace_lifecycle_manager", False))

    def _process_query_via_wlm(self, query: str, timeout=None):
        """Route a query through the supervisor when enabled.

        Returns (used, reply): used=False means the caller should fall back
        to the legacy queue-based path.
        """
        supervisor = self._get_wlm()
        if supervisor is None:
            return (False, None)
        return (True, supervisor.process_query(query, timeout=timeout))

    def stop(self) -> None:
        """Detach the worker: park it without terminating it (PHASE 2.1a).

        ``stop`` is an alias for :meth:`detach`.  Termination is
        :meth:`kill`.
        """
        self.detach()

    def detach(self) -> None:
        """Park the worker without terminating it.

        The run loop is unblocked and parks in the detached state, so the
        worker stops pulling queries — but the thread, its containers and its
        registry entry all survive. Only an operator ``resume`` revives it: a
        query submitted while the worker is detached is QUEUED (see
        ``enqueue_query``), never a resume. Context is persisted and the
        worker is registered as detached so the stale-container sweep skips it
        until the detached TTL expires.
        """
        # Synchronous lifecycle transition: 'detaching' must be observable
        # before the run loop acknowledges the park.
        self.status = "detaching"
        self._write_status_file()
        self._detach_requested = True
        self._detached_at = time.time()
        self._park_event.set()
        self._resume_event.clear()
        # Graceful stop CANCELS the in-flight turn (a long-blocking tool must
        # return instead of holding the park back) -- this is the "Stop now"
        # signal, NOT termination: _stop_event stays clear so the worker can be
        # resumed later.
        self._turn_cancel.set()
        # Persist the context: a detached worker is expected to be revived
        # later, possibly by another process.
        try:
            self._save_context()
        except Exception as exc:
            log("WARNING", "tools.worker",
                f"Context save failed while detaching worker '{self.worker_name}': {exc}")
        # PHASE 2.1a ORDERING INVARIANT: the container manager must already
        # know this worker is detached BEFORE ``status`` reports "detached", so
        # no reader (the REST status poll, the PeriodicStaleCheck sweep) can
        # ever observe a detached worker whose containers are still reapable.
        # ``_set_detached_registration`` never raises and resolves
        # ``owner_identity`` itself (:1551-1572), so hoisting it here cannot
        # introduce a new early return or an exception path.
        self._set_detached_registration(True)
        self.status = "detached"
        self._write_status_file()
        self._publish_global_event(EventType.WORKER_STOPPING, {"status": "detached", "reason": "detach_requested"})
        # Write a STOP command for cross-process signalling.  Fenced: an
        # unfenced payload is UNADDRESSED, and the consumer quarantines
        # anything it cannot prove was meant for this incarnation, so the
        # fence is what makes this signal deliverable at all.  Best effort --
        # ``write_command`` reports failure through its return value and never
        # raises, so a failed write cannot change the control flow of a detach.
        write_command(self._worker_dir, "stop", self._fence())
        # Unblock the input queue wait so the park flags are re-read.
        self._input_queue.put(_PARK_SENTINEL)

    def kill(self, *, journal_reason: Optional[str] = None) -> None:
        """Terminate the worker: signal the run loop to exit and reclaim it.

        PHASE 2.1a: this is the ONLY termination path — ``stop`` detaches.

        S7b-3 (Option K): *journal_reason* opts this kill IN to the inflight
        journal.  It is keyword-only and defaulted to ``None`` on purpose — a
        plain ``kill()`` keeps its exact previous behaviour (no journal read,
        no journal write), which is what a crash-shaped / best-effort kill and
        every existing call site want.  A caller that KNOWS the kill is
        deliberate (a manager stop, a reset) passes a reason, and the turn on
        disk becomes: record ``cancelled`` + job ``stopped``, both carrying
        that reason.

        The bookkeeping is guarded and runs FIRST, before the duties below,
        but never GATES them: a journal failure may not stop a worker from
        dying.
        """
        # Synchronous lifecycle transition: 'killed' must be observable before
        # the run loop acknowledges the stop event.
        self.status = "killed"
        self._write_status_file()
        if journal_reason is not None:
            # S7b-3 (Option K): a DELIBERATE kill records itself as terminal
            # bookkeeping before the worker goes away — the record is flipped
            # to ``cancelled`` (never to be resurrected, and the one live-
            # looking status a successor's ordinary write may displace) and the
            # job this turn belonged to is terminalized ``stopped`` with the
            # same reason in its envelope, so one grep finds both halves.  The
            # content is deliberately empty: the loop's own reply envelope
            # (written by run()) carries the human-facing message.
            self._journal_cancel(reason=journal_reason)
            self._journal_interrupt_job("", status="stopped", reason=journal_reason)
        wlm = getattr(self, "_wlm", None)
        if self._wlm_flag_enabled() and wlm is not None:
            try:
                wlm.stop()
            except Exception as exc:
                log("WARNING", "tools.worker",
                    f"WLM stop delegation failed for worker '{self.worker_name}': {exc}")
        # Default-on execution tracking: terminate any in-flight blocking tool
        # call so the run loop can observe the stop event and exit. In flag-on
        # mode wlm.stop() already terminated through the SHARED tracker, so
        # this is an idempotent no-op there; in flag-off mode it is the only
        # path that unblocks a stuck tool call.
        self._terminate_tracked_executions()
        self._publish_global_event(EventType.WORKER_STOPPING, {"status": "killed", "reason": "kill_requested"})
        # PHASE 2.1b: a killed worker never drains its queue, so every query
        # queued for it is rejected (runtime channel + audit log) instead of
        # being silently dropped or started on a dead instance.
        self.reject_queued_queries("killed")
        self._stop_event.set()
        # Cancel THIS worker's in-flight turn so a long-blocking tool (e.g.
        # ``sleep``) returns instead of waiting out the full block.  This is a
        # per-worker token, not a process-global interrupt: killing this worker
        # must never cancel another worker's tool call.
        self._turn_cancel.set()
        # A killed worker is never revived: clear the detach markers so the
        # detached TTL sweep cannot revisit it, and lift the detached
        # registration so stale-container reaping resumes.
        self._detach_requested = False
        self._detached_at = None
        self._set_detached_registration(False)
        # Unblock the input queue wait
        self._input_queue.put(None)

    def _set_detached_registration(self, detached: bool) -> None:
        """Register/unregister this worker as detached in the container manager.

        The stale-container sweep consults this so a detached worker's
        containers are not reaped while it waits to be resumed.  Never raises.
        """
        try:
            identity = self.owner_identity
        except Exception:
            return
        try:
            from infra.container_manager import (
                register_detached_worker,
                unregister_detached_worker,
            )
            if detached:
                register_detached_worker(identity)
            else:
                unregister_detached_worker(identity)
        except Exception as exc:
            log("WARNING", "tools.worker",
                f"Detached registration update failed for worker '{self.worker_name}': {exc}")

    def _terminate_tracked_executions(self) -> None:
        """Terminate in-flight executions in the default-on tracker.

        Never raises. In flag-on mode the supervisor terminates through the
        SAME tracker (see _get_wlm), so this is a safe no-op there; in
        flag-off mode it terminates a blocking tool call so the thread can
        observe the stop event and exit.
        """
        try:
            self._execution_tracker.terminate_all(
                self.worker_name,
                getattr(self, "_container_manager", None),
                None,
                session_id=self.session_id,
            )
        except Exception as exc:
            log("WARNING", "tools.worker",
                f"Execution termination failed for worker '{self.worker_name}': {exc}")

    def pause(self) -> None:
        """Signal the worker to pause after completing its current task."""
        wlm = getattr(self, "_wlm", None)
        if self._wlm_flag_enabled() and wlm is not None:
            try:
                wlm.pause()
            except Exception as exc:
                log("WARNING", "tools.worker",
                    f"WLM pause delegation failed for worker '{self.worker_name}': {exc}")
        self._park_event.set()
        self._resume_event.clear()
        # Reading mode: a paused worker's agent must stop calling tools and
        # conclude with a Respond, exactly like the main agent's Pause button.
        # Guarded -- _agent is None before spawn and after teardown.
        agent = getattr(self, "_agent", None)
        if agent is not None:
            try:
                agent.enter_reading_mode("pause")
            except Exception as exc:
                log("WARNING", "tools.worker",
                    f"enter_reading_mode failed for worker '{self.worker_name}': {exc}")
        # Write a PAUSE command for cross-process signalling (fenced, for the
        # same reason as stop(): an unaddressed payload is quarantined, and a
        # best-effort return value never changes this method's control flow).
        write_command(self._worker_dir, "pause", self._fence())
        # Unblock the input queue wait so it can check the pause event
        self._input_queue.put(_PARK_SENTINEL)

    def resume(self) -> bool:
        """Resume a paused worker.

        Returns ``False`` — without touching any state — when the worker was
        KILLED (``_stop_event`` set, or status ``"killed"``).  Reviving such a
        thread would resurrect a worker that has already been shut down: it
        would re-enter the main loop with the stop flag set and immediately
        report ``{"status": "killed"}`` to the caller instead of running the
        query.  Callers that need a query to run after a kill must spawn a
        fresh instance instead.

        A DETACHED worker (status ``"detached"``) IS revivable: PHASE 2.1a
        makes ``stop`` a detach, so resume() is the supported way back.
        """
        if self._stop_event.is_set() or self.status == "killed":
            log("WARNING", "tools.worker",
                f"Refusing to resume worker '{self.worker_name}': worker has been "
                f"killed (status={self.status})")
            return False
        wlm = getattr(self, "_wlm", None)
        if self._wlm_flag_enabled() and wlm is not None:
            try:
                wlm.resume()
            except Exception as exc:
                log("WARNING", "tools.worker",
                    f"WLM resume delegation failed for worker '{self.worker_name}': {exc}")
        # Cancel any stale pause command left by pause() while the worker was
        # busy (it would re-pause the worker on the next poll).  This MUST
        # happen before _park_event.clear(): the pause park loop exits as soon
        # as the event is cleared, so the channel must already be settled
        # before the worker can wake and poll it again.  Symmetric with the
        # stop()/pause() command writes.
        #
        # The old code UNLINKED the live command.json here.  That is now
        # forbidden (the producer must never delete the live path -- it is
        # exactly the read/unlink race), and claim-then-discard would be
        # CONTENT-BLIND deletion: it would just as happily eat a pending
        # 'kill' an operator wrote while this worker was busy.  So the stale
        # pause is cancelled by the channel's own means instead: a fenced
        # 'resume' command, which atomically REPLACES the pause (no window in
        # which the channel is empty) and is idempotent -- the poller's
        # 'resume' branch mirrors this method.  It is also fenced to THIS
        # incarnation, so a leftover one is quarantined, never obeyed, by a
        # future incarnation with a different epoch.
        write_command(self._worker_dir, "resume", self._fence())
        self._park_event.clear()
        self._resume_event.set()
        # UNIT C: the worker is now actually running again, so the manual-only
        # marker is stale.  Clear it so later queries/spawns do not treat this
        # worker as manually paused (the REST fast-path resume reaches this
        # method directly; the agent-facing ``resume`` action that used to be
        # the only other place clearing the marker no longer exists).
        self._manual_only_pause = False
        # PHASE 2.1a: a resumed worker is no longer detached — clear the
        # detach markers and lift the detached registration.
        self._detach_requested = False
        self._detached_at = None
        self._set_detached_registration(False)
        # Reading mode: release the 'pause'-owned restriction (owner-scoped, so a
        # 'token'/'timeout'/'turn' restriction survives).  Only reached after the
        # kill guard above, so a refused resume never lifts the restriction.
        agent = getattr(self, "_agent", None)
        if agent is not None:
            try:
                agent.leave_reading_mode("pause")
            except Exception as exc:
                log("WARNING", "tools.worker",
                    f"leave_reading_mode failed for worker '{self.worker_name}': {exc}")
        self.status = "ready"
        self._write_status_file()
        return True

    # ── PHASE 2.1b: detached-worker FIFO query queue ──────────────────

    def enqueue_query(self, job_id: str, query: str) -> None:
        """Append a query to this instance's FIFO queue (PHASE 2.1b).

        Appending NEVER sets ``_resume_event``: a detached worker keeps
        parking in its wait loop, so a queued query burns no tokens until
        an operator resumes the worker. The ``job_id`` is mirrored in
        ``_queued_job_ids`` (same order) so the pending set can be
        introspected and rejected as a unit on TTL expiry or kill.

        Callers own job registration; this only appends.
        """
        with self._queued_lock:
            self._queued_job_ids.append(job_id)
        self._input_queue.put((job_id, query, None))

    def pending_query_jobs(self) -> List[str]:
        """Ordered snapshot of the job_ids queued but not yet in flight."""
        with self._queued_lock:
            return list(self._queued_job_ids)

    def _discard_pending_job(self, job_id: Any) -> None:
        """Drop *job_id* from the pending-queue bookkeeping. Never raises."""
        if job_id is None:
            return
        with self._queued_lock:
            try:
                self._queued_job_ids.remove(job_id)
            except ValueError:
                pass

    def withdraw_queued_query(self, job_id: Any) -> None:
        """Remove a still-queued job from BOTH the queue and its bookkeeping.

        Used when a submission cannot be completed after it was enqueued (the
        job registry refused the record), so the query is neither left behind
        as an orphan nor reported as accepted. Surviving entries \u2014 including
        ``None``/park markers \u2014 keep their original order. Never raises.
        """
        if job_id is None:
            return
        try:
            self._discard_pending_job(job_id)
            leftover = []
            try:
                while True:
                    item = self._input_queue.get_nowait()
                    if (
                        isinstance(item, tuple)
                        and len(item) >= 2
                        and item[0] == job_id
                    ):
                        continue
                    leftover.append(item)
            except queue.Empty:
                pass
            for item in leftover:
                self._input_queue.put(item)
        except Exception:  # pragma: no cover - defensive: rollback never raises
            pass

    def reject_queued_queries(
        self,
        reason: str,
        audit_event_type: str = "WORKER_QUEUED_QUERY_REJECTED",
    ) -> List[str]:
        """Reject every queued-but-not-started query (PHASE 2.1b).

        Called when the queue can never be drained: the detached TTL expired
        or the worker is being killed. Each rejected job is terminalised in
        the job registry as ``error``, announced on the global runtime
        channel as a ``worker_error`` event carrying its ``query_id`` (the
        bridge forwards it to the WebSocket, so the rejection is loud rather
        than a silently polled job status), and recorded in the audit log.

        ``reason`` is interpolated into the operator-facing message
        ``worker '<name>' <reason> — queued query discarded``.

        Returns the rejected job_ids in queue order. Never raises.
        """
        with self._queued_lock:
            job_ids = list(self._queued_job_ids)
            self._queued_job_ids.clear()
        if not job_ids:
            return []
        message = f"worker '{self.worker_name}' {reason} — queued query discarded"
        try:
            registry = _get_worker_job_registry()
        except Exception:
            registry = None
        for job_id in job_ids:
            if registry is not None:
                try:
                    registry.terminalize(
                        job_id,
                        status="error",
                        envelope={
                            "error": message,
                            "status": "error",
                            "query_id": job_id,
                        },
                    )
                except Exception:
                    pass
            if EventType is not None:
                try:
                    _publish_global_worker_event(EventType.WORKER_ERROR, {
                        "worker_name": self.worker_name,
                        "session_id": self.session_id or "",
                        "query_id": job_id,
                        "error": message,
                    })
                except Exception:
                    pass
        try:
            from thoughtmachine.audit_logger import audit_event

            audit_event(
                audit_event_type,
                f"worker={self.worker_name} session={self.session_id or ''} "
                f"reason={reason} count={len(job_ids)} "
                f"job_ids={','.join(job_ids)}",
            )
        except Exception:
            pass
        return job_ids

    def reset(self) -> Dict[str, Any]:
        """Reset per-query and persisted context state; the thread stays alive.

        W3 minimal reset semantics: bump the generation token via
        ``_allocate_generation`` (so any context.json written by an older
        instance is superseded), clear the persisted WorkerContext
        (context.json is removed; the next query reloads fresh through
        ``_load_context``), and zero every ephemeral per-query field.
        Cooperative stop/pause/resume semantics are untouched - this only
        clears state; the thread remains alive and ready for a fresh query.
        Returns a status dict (``reset: False`` with ``reason: 'busy'`` when
        the worker has an in-flight query, since resetting mid-query would
        corrupt the reply path).
        """
        if self.status == "busy":
            return {
                "reset": False,
                "worker_name": self.worker_name,
                "instance_id": self.instance_id,
                "status": self.status,
                "reason": "busy",
            }
        self._generation = self._allocate_generation()
        try:
            self._context_path().unlink(missing_ok=True)
        except OSError:
            pass
        self._worker_ctx = None
        self._agent = None
        self._initial_context = None
        # Ephemeral per-query state (F1 markers, telemetry, reply routing).
        self._last_query = None
        self._last_completed_query = None
        self._last_query_id = None
        self._last_reasoning = None
        self._respond_metadata = {}
        self._final_token_usage = None
        self._cached_context_tokens = None
        self._tool_call_count = 0
        self._timeout_triggered = False
        self._pruned_since_last_query = 0
        self._current_query_id = None
        self._journal_cancel_reason = None
        # A reset is a NEW incarnation at a NEW epoch: whatever the previous
        # incarnation adopted is no longer this one's business, and the record
        # it might adopt afresh is a genuinely dead predecessor's.
        self._journal_adopted = None
        self._journal_replay_attempts.clear()
        self._current_reply_queue = None
        self.current_task = None
        self.error = None
        self.last_query_at = None
        try:
            self._execution_tracker.clear()
        except Exception:
            pass
        self._write_status_file()
        return {
            "reset": True,
            "worker_name": self.worker_name,
            "instance_id": self.instance_id,
            "status": self.status,
            "generation": self._generation,
        }

    def _fence(self) -> CommandFence:
        """The identity fence every command addressed to THIS incarnation carries.

        Built FRESH on every call and never cached in ``__init__``:
        ``_generation`` is re-allocated on each spawn
        (``_reset_for_new_generation``), so a cached fence would reject this
        worker's own commands after the first reset.

        ``epoch`` is ``_generation`` -- deliberately NOT a second counter. The
        fence epoch has to BE the worker's real incarnation number, or commands
        written by this very worker would be rejected by its own consumer.
        """
        return CommandFence(
            session_id=self.session_id or "",
            worker_name=self.worker_name,
            instance_id=self.instance_id,
            epoch=self._generation,
        )

    # ── inflight journal write points (S7b-2b) ──────────────────────────────
    # The journal is BOOKKEEPING for a successor incarnation, never a gate:
    # every call below is fenced through ``_fence()`` (the same fresh, live
    # ``_generation``-derived identity the command channel uses, so a record
    # can never be stamped with a fence that disagrees with this incarnation's)
    # and a write that raises or is REFUSED is only logged -- no journal
    # failure may fail, delay or alter a turn.

    def _journal_write(
        self,
        *,
        query_id: str,
        query: str,
        status: str,
        created_at: str,
        last_tool: Optional[Dict[str, str]] = None,
        reason: Optional[str] = None,
        attempts: int = 0,
    ) -> bool:
        """Journal this turn as *status*; True when the record reached disk.

        WRITE POINT 1 (turn start) and WRITE POINT 2 (each tool call) call
        this with ``STATUS_INFLIGHT``; WRITE POINT 4 (the DETACH park) with
        ``STATUS_RECOVERABLE`` and a ``reason``.  ``created_at`` is threaded
        in by the caller so an update is an UPDATE -- identity (``query_id``,
        ``created_at``) is set once per turn, never re-stamped.

        ``attempts`` defaults to 0: a fresh turn is not a replay.  The ONE
        caller that passes otherwise is WRITE POINT 1 of a turn adopted from a
        dead predecessor (S7b-4b), which must carry the adoption's bumped count
        to disk -- re-stamping 0 there would wipe the pre-crash count and
        ``MAX_ATTEMPTS`` could never fire, so a poison query would replay
        forever.  ``requested_by`` is a heuristic, not
        an identity claim: a turn that arrived through
        ``send_query``/``submit_query`` carries a query id (the registry job
        id), while one ``run()`` drained off the input queue in WLM mode does
        not -- in that case the journal's ``query_id`` is synthetic and no job
        exists to terminalize.
        """
        record = InflightRecord(
            query_id=query_id,
            query=query,
            status=status,
            attempts=attempts,
            requested_by=(
                "send_query" if self._current_query_id is not None
                else "worker_queue"
            ),
            created_at=created_at,
            updated_at=utcnow_iso(),
            last_tool=last_tool,
            reason=reason,
        )
        try:
            refusal = write_inflight(self._worker_dir, record, self._fence())
        except Exception as exc:
            # Construction validates too (an invalid status/last_tool is a
            # caller bug), so the whole call is inside the guard: the turn is
            # what matters, not the bookkeeping.
            log("WARNING", "tools.worker",
                f"[journal] write failed for worker '{self.worker_name}' "
                f"(query_id={query_id}, status={status}): {exc}")
            return False
        if refusal is not None:
            # A refusal is NOT an error to act on here: the only recorded
            # reason to refuse is another incarnation's non-cancelled record,
            # and displacing that is a deliberate adoption (S7b-4), not
            # something a turn may do implicitly.  So: log, and run on.
            log("WARNING", "tools.worker",
                f"[journal] write refused for worker '{self.worker_name}' "
                f"(query_id={query_id}, status={status}): {refusal}")
            return False
        return True

    def _journal_clear(self) -> None:
        """Drop THIS incarnation's journal record (WRITE POINT 3).

        Called on every terminal exit of the loop that did NOT park, so a
        completed turn leaves nothing for a successor to replay.  Idempotent
        (nothing to remove is success) and fenced: a record belonging to
        another incarnation is left alone -- that is the successor's evidence,
        not ours to erase.  Never raises.
        """
        try:
            reason = clear_inflight(self._worker_dir, self._fence())
        except Exception as exc:
            log("WARNING", "tools.worker",
                f"[journal] clear failed for worker '{self.worker_name}': {exc}")
            return
        if reason is not None:
            log("WARNING", "tools.worker",
                f"[journal] clear refused for worker '{self.worker_name}': "
                f"{reason}")

    def _journal_cancel(self, *, reason: str) -> None:
        """Flip THIS incarnation's journal record to ``cancelled`` (S7b-3).

        A DELIBERATE kill (``kill(journal_reason=...)``) is not a crash: the
        turn will never be resumed by anyone, so its record must say that
        instead of being left ``inflight`` for a successor to mistake for
        recoverable work.  ``cancelled`` is exactly that statement -- and it is
        also the one live-looking status ``write_inflight`` lets an ordinary
        write displace, so the husk a kill leaves behind cannot wedge the next
        legitimate turn.

        Read-modify-write over the record on disk, never a fresh one: identity
        (``query_id``, ``created_at``, ``query``) and ``last_tool`` are carried
        over, so a cancel is an UPDATE of the turn that exists.  NO record on
        disk means nothing to cancel -- a kill on an idle worker must not
        conjure a phantom turn for a successor to find.  ``attempts`` is
        re-stamped 0 by ``_journal_write`` (which owns that field: the journal
        records the turn, not a replay count).

        Idempotent: a repetition for the same reason finds the record already
        ``cancelled`` and writes nothing, so the first cancel's bytes -- and its
        ``updated_at`` -- survive a repeated request unchanged.

        Fenced like every other write point: a record belonging to another
        incarnation reads as absent here and is left byte-identical.

        Never raises: it is bookkeeping for a worker that is dying.
        """
        try:
            existing, _why = read_inflight(self._worker_dir, self._fence())
            if existing is None:
                return
            if existing.status == STATUS_CANCELLED and existing.reason == reason:
                return
            cancelled = self._journal_write(
                query_id=existing.query_id,
                query=existing.query,
                status=STATUS_CANCELLED,
                created_at=existing.created_at,
                last_tool=existing.last_tool,
                reason=reason,
            )
        except Exception as exc:
            log("WARNING", "tools.worker",
                f"[journal] cancel failed for worker '{self.worker_name}': {exc}")
            return
        if cancelled:
            # Only a record that really reached disk may hold WRITE POINT 3
            # back: if the cancel was refused or failed there is nothing on
            # disk worth keeping, and the exit must clear as usual.
            self._journal_cancel_reason = reason

    def _journal_interrupt_job(
        self,
        content: str,
        *,
        status: str = "interrupted",
        reason: str = _JOURNAL_DETACH_REASON,
    ) -> None:
        """Terminalize the job whose turn was ABANDONED mid-flight.

        Two abandoners use this, and only the status/reason differ:

        * the DETACH park (defaults: ``interrupted`` +
          ``_JOURNAL_DETACH_REASON``) -- the turn's work is thrown away, so
          the job it belonged to must never be completed or restarted by a
          later join;
        * a DELIBERATE kill (``stopped`` + the kill's own ``journal_reason``)
          -- S7b-3.  ``stopped`` is the JOB's vocabulary for "a user/operator
          ended this", which is exactly what the kill's reason says.

        ``WorkerJobRegistry.terminalize`` takes no ``reason`` parameter, so the
        reason rides in the ENVELOPE instead (stored verbatim as the job's
        result) -- one string, one grep, both halves.

        No ``_current_query_id`` (a turn drained off the input queue) means no
        job to terminalize; that is the normal WLM-mode case, not a failure.
        """
        job_id = self._current_query_id
        if job_id is None:
            return
        try:
            registry = _get_worker_job_registry()
        except Exception as exc:
            log("WARNING", "tools.worker",
                f"[journal] job registry unavailable for worker "
                f"'{self.worker_name}' (query_id={job_id}): {exc}")
            return
        if registry is None:
            return
        try:
            registry.terminalize(
                job_id,
                status=status,
                envelope={
                    "content": content,
                    "status": status,
                    "reason": reason,
                },
            )
        except Exception as exc:
            log("WARNING", "tools.worker",
                f"[journal] terminalize failed for worker '{self.worker_name}' "
                f"(query_id={job_id}): {exc}")

    def _poll_command(self) -> None:
        """Consume one command from this worker's fenced cross-process channel.

        The channel is ``<workspace>/workers/<label>/command.json``. Every
        payload written by ``command_channel.write_command`` carries a FENCE
        (``session_id`` / ``worker_name`` / ``instance_id`` / ``epoch``) and the
        worker obeys a command only when all four match :meth:`_fence` -- which
        is re-derived from the live ``_generation`` on every poll, so a command
        addressed to an older incarnation is refused rather than obeyed.

        Two rules, and both of them are the point of ``command_channel``:

        * NEVER read or unlink the LIVE path. The file is claimed first
          (``claim_command`` ``os.replace``-s it to a private name), so a
          command written the instant after a claim survives, untouched, for
          the next poll: the read/unlink race that used to destroy commands is
          gone by construction, and two consumers cannot both claim one command.
        * NEVER DELETE a command this worker refuses. Every refusal path -- a
          disagreeing fence, no fence, a malformed fence, corrupt JSON, an
          unknown action -- quarantines the claimed file to
          ``command.json.rejected-*`` and logs a WARNING. A refused command is
          the only evidence that something addressed this worker and was not
          obeyed, so it stays on disk for post-mortem.

        A handled action is discarded and signalled. PHASE 2.1a: ``"stop"``
        DETACHES, ``"kill"`` is the only terminating action.

        The old ``_ignored_command_digest`` memo is GONE. It existed only
        because this loop used to unlink the live path, which made any deletion
        capable of destroying a command written in the read/unlink window --
        hence "leave the stray file where it is and remember its digest so
        re-polls are cheap". Claiming removes that threat entirely: the claimed
        file is exclusively ours, so quarantining it can never destroy someone
        else's write. A memo justified by a race that no longer exists is
        misleading residue, and the channel now DRAINS instead of re-reading an
        unactionable payload on every poll.
        """
        claim = claim_command(self._worker_dir)
        if claim is None:
            return
        claimed, payload = claim

        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            # Corrupt payload: keep the evidence, obey nothing.
            quarantine(claimed, "corrupt-json")
            log("WARNING", "tools.worker",
                f"Quarantined a corrupt (invalid JSON) command for worker "
                f"'{self.worker_name}'")
            return

        reason = fence_rejection_reason(self._fence(), data)
        if reason is not None:
            # Unaddressed or wrongly-addressed payload. Obeying it is exactly
            # the cross-talk bug this fence exists to prevent.
            quarantine(claimed, reason)
            log("WARNING", "tools.worker",
                f"Rejected command for worker '{self.worker_name}' "
                f"(instance_id={self.instance_id}): {reason}")
            return

        action = data.get("action")
        if action == "stop":
            # PHASE 2.1a: a cross-process 'stop' DETACHES.  It must not
            # set _stop_event — the thread parks and stays revivable.
            discard(claimed)
            self._detach_requested = True
            self._detached_at = time.time()
            self._park_event.set()
            self._resume_event.clear()
            # Cross-process graceful stop: same turn-cancel signal as
            # detach(), since this branch parks the worker inline.
            self._turn_cancel.set()
            try:
                self._save_context()
            except Exception:
                pass
            # Same ORDERING INVARIANT as detach(): register first, then
            # make "detached" observable.
            self._set_detached_registration(True)
            self.status = "detached"
            self._write_status_file()
            self._input_queue.put(_PARK_SENTINEL)
        elif action == "kill":
            # PHASE 2.1a: a cross-process 'kill' TERMINATES.  'stop' only
            # detaches, so this is the sole terminating signal.  Delegate
            # to WorkerThread.kill() so the stop event, the global
            # WORKER_STOPPING publish, the tracked-execution teardown and
            # the detach-registration cleanup all stay in one place.
            discard(claimed)
            self.kill()
        elif action == "pause":
            discard(claimed)
            self._park_event.set()
            # Mirror WorkerThread.pause(): the parked worker's agent enters
            # reading mode.  Deliberately duplicated instead of calling
            # self.pause() -- that would rewrite command.json (re-pausing on
            # the next poll) and re-delegate to the WLM.
            p_agent = getattr(self, "_agent", None)
            if p_agent is not None:
                try:
                    p_agent.enter_reading_mode("pause")
                except Exception as exc:
                    log("WARNING", "tools.worker",
                        f"enter_reading_mode failed for worker '{self.worker_name}': {exc}")
            self._input_queue.put(_PARK_SENTINEL)
        elif action == "resume":
            # Resume while a pause is pending (or a leftover from an
            # earlier REST resume): cancel the pause so the worker keeps
            # running.  Mirrors WorkerThread.resume() semantics; without
            # this the command.json would linger forever (no-op on every
            # poll).  Events are thread-safe, so this is safe even if the
            # blocked pause loop is waiting on _resume_event.
            if self._stop_event.is_set() or self.status == "killed":
                # Mirror WorkerThread.resume(): a killed worker is terminal and
                # must not be revived by a channel 'resume'.
                quarantine(claimed, "resume-on-killed")
                log("WARNING", "tools.worker",
                    f"Refusing channel resume for worker '{self.worker_name}': "
                    f"worker has been killed (status={self.status})")
                return
            discard(claimed)
            self._park_event.clear()
            self._resume_event.set()
            # Mirrors WorkerThread.resume(): the worker keeps running, so
            # the manual-only marker is stale and must be cleared too.
            self._manual_only_pause = False
            # PHASE 2.1a: mirror WorkerThread.resume() (:1657-1663) -- a
            # resumed worker is no longer detached, so the markers go and
            # the registration is lifted BEFORE the park loop above
            # reports "ready".  Clearing only _park_event (as this branch
            # used to) left a "ready" worker that the stale-container sweep
            # still skipped as detached, forever.
            self._detach_requested = False
            self._detached_at = None
            self._set_detached_registration(False)
            # Mirror WorkerThread.resume(): release the 'pause'-owned
            # reading-mode restriction (owner-scoped).  Duplicated rather
            # than calling self.resume() -- that returns a bool, delegates
            # to the WLM and would re-log a refusal path.
            r_agent = getattr(self, "_agent", None)
            if r_agent is not None:
                try:
                    r_agent.leave_reading_mode("pause")
                except Exception as exc:
                    log("WARNING", "tools.worker",
                        f"leave_reading_mode failed for worker '{self.worker_name}': {exc}")
            self.status = "ready"
            self._write_status_file()
        else:
            # Not a command this worker understands.  Unchanged POLICY (do not
            # obey it, do not delete it) but the mechanism changed with the
            # claim: the payload has already been moved out of the live name,
            # so "leave it there and memoise its digest" no longer describes
            # anything real.  Quarantined like every other refusal, which both
            # preserves the evidence and lets the channel drain.
            quarantine(claimed, "unknown-action-%s" % (action,))
            log("WARNING", "tools.worker",
                f"Ignoring unknown command action {action!r} for worker "
                f"'{self.worker_name}'")

    def _last_elapsed(self) -> Optional[float]:
        """Return elapsed seconds from the most recent query execution."""
        return getattr(self, '_last_elapsed_val', None)

    def _resolve_capability_registry(self) -> "CapabilityRegistry":
        """Resolve this worker's declared tool set once; cache the frozen result.

        One resolution per worker: the registry is built from the definition,
        so a later edit to the template affects only the next spawn.  Declared
        names are canonicalised first, so a template that uses a tool's stable
        name (``git_read``) resolves to the class instead of being reported as
        "not shippable by this build".

        ``permission_veto=None`` is deliberate: the registry says which tools
        EXIST, the footprint gate in ``Worker._action_spawn`` says which of them
        may RUN (design doc §7.2).
        """
        if self._capability_registry is not None:
            return self._capability_registry
        from tools.capability_registry import CapabilityRegistry

        universe = _worker_tool_universe()
        declared = [
            _canonical_worker_tool_name(name)
            for name in (self.definition.get("tools") or [])
        ]
        self._capability_registry = CapabilityRegistry.resolve(
            declared,
            available_names=tuple(universe),
            lookup=universe,
            blocklist=_WORKER_BLOCKLIST,
            parent_enabled_tools=(self._agent_config_dict or {}).get(
                "enabled_tools"
            ),
            permission_veto=None,
        )
        return self._capability_registry

    def _build_agent_config(self) -> Any:
        """
        Build an AgentConfig from the worker definition and parent agent config.

        Imports ``AgentConfig`` lazily to avoid circular imports with
        ``agent.core.agent`` which pulls in ``tools.TOOL_CLASSES``.

        The worker inherits all fields from the parent (ToolExecutor-injected)
        config dict, then overrides only worker-specific settings
        (system_prompt, temperature, enabled_tools, max_turns,
        timeout_seconds, stop_check).

        Also injects ``session_permissions`` so that tools running inside
        the worker use the same security policy as the parent agent.

        Returns None if AgentConfig is not importable, or if provider/model
        are missing.
        """
        try:
            from agent.config.models import AgentConfig
        except ImportError:
            logger.warning("AgentConfig not available — cannot create worker agent")
            return None

        cfg = self._agent_config_dict or {}
        provider_type = cfg.get("provider", "")
        model = cfg.get("model", "")
        if not provider_type or not model:
            logger.warning(
                "agent_config missing provider (%s) or model (%s)",
                provider_type, model,
            )
            return None

        # ── Forward the full parent config dict ────────────────────────
        worker_cfg = dict(cfg)
        # The ToolExecutor injects the key as ``provider``, but AgentConfig
        # expects the Pydantic field name ``provider_type``.
        worker_cfg["provider_type"] = worker_cfg.pop("provider")

        # ── Worker-specific overrides ──────────────────────────────────
        # The worker NEVER inherits the main agent's system_prompt or sampling
        # settings: every sampling-relevant field below is resolved from the
        # worker definition (or its own worker defaults), so spawning cannot
        # leak main-agent settings into a worker agent.
        worker_cfg["system_prompt"] = self.definition.get(
            "system_prompt",
            DEFAULT_WORKER_SYSTEM_PROMPT,
        )
        # Temperature is worker-scoped: the worker definition wins, else the
        # worker default (0.7) — the main agent's temperature is never
        # inherited.  max_tokens / truncation_limit live on the worker
        # definition schema only (None = worker default); AgentConfig has no
        # such fields, so they are intentionally NOT injected into the cfg
        # (Pydantic's extra='ignore' would silently drop them anyway).
        worker_cfg["temperature"] = (
            self.definition.get("temperature")
            if self.definition.get("temperature") is not None
            else WORKER_DEFAULT_TEMPERATURE
        )
        # Tool set: resolved once through the capability registry.  The
        # registry owns the blocklist, the parent clamp (narrowing recorded in
        # ``.parent_denied``) and the implicit "all simplified tools" default,
        # so ``enabled_tools`` can never be a silently narrowed subset.
        resolved_tool_names: Optional[frozenset] = None
        try:
            _registry = self._resolve_capability_registry()
            enabled_tools = _worker_granted_tool_names(_registry)
            # The SAME frozen registry object that fed enabled_tools -- never a
            # re-resolution and never a set rebuilt from enabled_tools, so the
            # two cannot drift.  AgentConfig intersects this with enabled_tools
            # at run time (defence in depth: widening enabled_tools cannot
            # widen real tool availability).
            resolved_tool_names = frozenset(_registry.names)
        except Exception as exc:
            # Never raise from here: the spawn boundary (Worker._action_spawn)
            # is what fails loudly.  Fall back to the legacy derivation
            # verbatim so config building stays total.
            logger.debug(
                "Worker '%s' could not resolve a capability registry (%s); "
                "using the legacy enabled_tools derivation",
                self.worker_name, exc,
            )
            worker_tools = self.definition.get("tools", [])
            parent_enabled_tools = cfg.get("enabled_tools")
            if worker_tools:
                enabled_tools = [t for t in worker_tools if t not in _WORKER_BLOCKLIST]
            else:
                from tools import simplified_tool_classes
                enabled_tools = [cls.__name__ for cls in simplified_tool_classes()
                                 if cls.__name__ not in _WORKER_BLOCKLIST]
            # Intersect with parent's enabled_tools — worker cannot exceed parent
            if parent_enabled_tools is not None:
                enabled_tools = [t for t in enabled_tools if t in parent_enabled_tools]
        worker_cfg["enabled_tools"] = enabled_tools
        # None when the registry could not be resolved (legacy behaviour).
        worker_cfg["resolved_tool_names"] = resolved_tool_names
        # Resolve max_turns: prefer definition, then parent config (treating
        # None as absent), then default 100.
        _inherit_max_turns = cfg.get("max_turns")
        if _inherit_max_turns is None:
            _inherit_max_turns = 100
        _def_max_turns = self.definition.get("max_turns")
        if _def_max_turns is None:
            _def_max_turns = _inherit_max_turns
        worker_cfg["max_turns"] = _def_max_turns
        # Override token monitor warning threshold from worker definition
        warning_threshold = self.definition.get("warning_threshold_tokens", None)
        if warning_threshold is not None:
            worker_cfg["token_monitor_warning_threshold"] = warning_threshold

        # Override token monitor critical threshold from worker definition
        critical_threshold = self.definition.get("critical_threshold_tokens", None)
        if critical_threshold is not None:
            worker_cfg["token_monitor_critical_threshold"] = critical_threshold
        worker_cfg["timeout_seconds"] = self._timeout_seconds
        # Warn at 80% of timeout (minimum 5s) so CRITICAL triggers before
        # the hard cutoff when using the worker tool's timeout window.
        _timeout_for_warning = (
            self._timeout_seconds
            if self._timeout_seconds is not None
            else WORKER_TIMEOUT_SECONDS
        )
        worker_cfg["time_warning_threshold"] = max(
            5, int(_timeout_for_warning * 0.8)
        )
        worker_cfg["time_monitor_enabled"] = True
        worker_cfg["stop_check"] = self._stop_event.is_set

        # ── Inject session permissions (restrictive merge — session is ceiling) ──
        merged = _restrictive_merge(self._session_permissions, self._permission_footprint)
        # Canonicalize: drop any legacy permission grains that survived the
        # merge (stale worker definitions).  The AgentConfig-bound dict uses
        # the canonical six-category schema, with write_on_feature_branch
        # carried on the ``git`` channel.
        worker_cfg["session_permissions"] = _canonicalize_session_permissions(merged)

        # ── Inject the DECLARED worker cap separately (do NOT collapse it) ──
        # ``permission_footprint`` is the cap written in the worker definition,
        # unmerged and un-canonicalized.  It is passed to the worker config as
        # its own field so the tool executor can apply it as a cap on top of the
        # session permissions re-read from disk at every tool call.  Folding it
        # into ``session_permissions`` here would re-freeze the session snapshot
        # for the worker's whole lifetime: a grant written to disk after spawn
        # could then never take effect until the worker was respawned.
        # Copy it: the definition dict must not be shared into a live config,
        # where a later mutation of the definition would silently change the cap.
        worker_cfg["permission_footprint"] = dict(self._permission_footprint)

        # ── Safety net: inject workspace_path if missing ────────────────
        # Worker._build_agent_config (the Tool-class method) now injects
        # workspace_path from the parent, but in case that fails (e.g., the
        # path was not available at the tool level), fall back to the
        # project_root that was resolved from workspace_dir/config.json.
        if "workspace_path" not in worker_cfg and self._project_root:
            worker_cfg["workspace_path"] = self._project_root

        # Safety net: ensure max_turns is a valid int (fall back to 100)
        if "max_turns" not in worker_cfg or worker_cfg.get("max_turns") is None:
            worker_cfg["max_turns"] = 100

        # Mark config as running inside a worker for security context
        worker_cfg["worker_mode"] = True

        log('DEBUG', 'core.token', f"Worker agent config: token_monitor_warning_threshold={worker_cfg.get('token_monitor_warning_threshold', 'NOT SET')} token_monitor_critical_threshold={worker_cfg.get('token_monitor_critical_threshold', 'NOT SET')} max_turns={worker_cfg.get('max_turns', 'NOT SET')} timeout_seconds={worker_cfg.get('timeout_seconds', 'NOT SET')}")

        _config = AgentConfig(**worker_cfg)
        # ``_tool_classes`` used to be spawn-time data nothing ever read.  Feed it
        # from the runtime authority instead: exactly the list the agent will
        # hand to its ToolExecutor (enabled_tools intersected with
        # resolved_tool_names).  The dict-keyed-by-class-name shape is kept for
        # the process-mode kwarg map (worker_runtime_process._OPTIONAL_KWARGS).
        try:
            self._tool_classes = {
                cls.__name__: cls
                for cls in _config.get_filtered_tool_classes(
                    allowed_names=resolved_tool_names
                )
            }
        except Exception as exc:
            logger.debug(
                "Worker '%s' could not mirror the runtime tool classes (%s)",
                self.worker_name, exc,
            )
        return _config

    def _run_tool_loop(
        self,
        query: str,
    ) -> str:
        """
        Run the agent conversation loop for a single query using Agent.process_query().

        Iterates over all events yielded by the agent, logging heartbeats
        and checking the stop event. Returns the final response text.
        """
        # One CLEAR per query: a cancel from a previous turn must never abort
        # this one.  ``_stop_event`` is the terminal kill latch and is NOT
        # touched here (a killed worker stays killed).
        self._turn_cancel.clear()

        if self._agent is None:
            return json.dumps({"error": "Agent not initialized"})

        self.current_task = query[:200]
        final_content = ""
        _start = time.monotonic()

        # Lazy-init presenter components for event processing
        if self._state_bridge is None:
            try:
                from agent.presenter.state_bridge import StateBridge
            except ImportError:
                log('WARNING', 'tools.worker', 'Failed to lazy-import StateBridge')
                StateBridge = None
            try:
                from agent.presenter.event_processor import EventProcessor
            except ImportError:
                log('WARNING', 'tools.worker', 'Failed to lazy-import EventProcessor')
                EventProcessor = None
            if StateBridge is not None and ExecutionState is not None:
                log('DEBUG', 'pipeline.worker',
                    f"Lazy-init presenter [worker={self.worker_name}]: creating WorkerBusAdapter "
                    f"with event_bus={'SET' if self._event_bus else 'NONE'}")
                self._state_bridge = StateBridge(config_path=None)
                bus_adapter = WorkerBusAdapter(self._event_bus, self.worker_name, state_bridge=self._state_bridge)
                self._worker_bus_adapter = bus_adapter
                session_lifecycle = WorkerSessionLifecycle()
                self._event_processor = EventProcessor(
                    state_bridge=self._state_bridge,
                    session_lifecycle=session_lifecycle,
                    gui_integration=bus_adapter,
                )
                log('DEBUG', 'pipeline.worker',
                    f"Lazy-init complete [worker={self.worker_name}]: "
                    f"worker_bus_adapter={'SET' if self._worker_bus_adapter else 'NONE'}, "
                    f"event_processor={'SET' if self._event_processor else 'NONE'}")

        # Log the user query as a user_message event
        self._publish_event("user_message", {"query": query})

        # Check stop before starting tool loop
        if self._stop_event.is_set():
            return json.dumps({
                "status": "stopped",
                "message": "Worker stopped before processing query",
            })

        # Stamp the worker-name context var for the duration of this turn:
        # tool calls executed by the agent read it via
        # agent.core.worker_context.current_worker_name() and stamp the
        # ``thoughtmachine.worker`` docker label on containers they create,
        # so teardown can reclaim them (see module docstring). The stamp
        # carries the worker's OWNER IDENTITY (``<session_id or
        # 'unknown'>:<worker_name>``), not the bare name, so teardown can
        # match labels exactly. Reset in the finally block below so the var
        # never leaks across queries.
        _worker_ctx_token = None
        if WORKER_NAME_CONTEXTVAR is not None:
            _worker_ctx_token = WORKER_NAME_CONTEXTVAR.set(self.owner_identity)

        # Bind THIS worker's turn token to the executing thread.  Tools run
        # inline on this thread (agent/core/tool_executor.py:689), so binding at
        # the turn boundary is the narrowest point that covers every tool call
        # without touching the executor.
        from tools.sleep_tool import bind_pause_event as _bind_pause_event
        from tools.sleep_tool import bind_turn_cancel as _bind_turn_cancel
        from tools.sleep_tool import unbind as _unbind_turn_cancel
        _bind_turn_cancel(self._turn_cancel)
        # PAUSE PLUMBING: pause has no thread-local token of its own.  It is
        # raised by the bridge/controller on ANOTHER thread, so the only way a
        # tool blocked on THIS (the turn-running) thread can see it is a shared
        # reference.  Bind this worker's own ``_park_event`` -- cleared =
        # running, set = pause requested, exactly the polarity
        # ``bind_pause_event`` documents -- so a tool that waits
        # (``ContainerManager.exec``, ``Sleep``) wakes on a pause instead of
        # blocking until its own timeout.  The event is never set or cleared
        # here: it belongs to the worker, and ``pause()``/``resume()`` own it.
        _bind_pause_event(self._park_event)

        # ── S7b-4a: ADOPT the dead predecessor's turn, ONCE, before WP1 ─────
        # A crashed incarnation leaves an ``inflight`` record in THIS directory
        # (instance 1 keeps the bare worker name, so the successor reuses it --
        # see ``_adopt_predecessor_journal``), and WRITE POINT 1 below would be
        # REFUSED by it: ``write_inflight`` fails closed on foreign work in
        # progress. Adoption is the one deliberate takeover, so it happens here,
        # at turn start, before the write, and at most once per incarnation
        # (``_journal_adopted``); every later turn finds its own epoch on disk
        # and adopts nothing. A journal that is absent, corrupt or somebody
        # else's business is a silent no-op -- a predecessor's leftovers must
        # never fail or noise up this turn.
        if self._journal_adopted is None:
            _adopted_record = _adopt_predecessor_journal(
                self._worker_dir, self._fence(), self._generation
            )
            if _adopted_record is not None:
                self._journal_adopted = _adopted_record
                log("INFO", "tools.worker",
                    f"[journal] adopted query '{_adopted_record.query_id}' from "
                    f"a dead predecessor "
                    f"(attempt {_adopted_record.attempts})")
                # The adopted turn must RUN, not merely be recorded: queue it
                # the way ``send_query`` does -- a ``(query_id, query,
                # reply_q)`` TRIPLE, NOT the bare string the spawn auto-query
                # and the WLM handler use.
                #
                # The ID is the whole reason for the triple. ``run()`` unpacks
                # the item at worker_thread.py:3607-3618, and a bare string
                # takes the ``query_id, query, reply_q = None, raw, None``
                # branch: ``self._current_query_id`` would be ``None``, so the
                # crashed caller's job-registry entry could never be finished
                # -- run() only completes it when the id is set
                # (``if self._current_query_id is not None: _registry.complete(
                # self._current_query_id, envelope)``, :3996-4002) and only
                # stamps ``envelope['query_id']`` in that same case
                # (:3957-3958). The caller sitting in ``wait_for_job`` on that
                # job would hang on a job nobody can terminalize. With the
                # triple the replay IS attributed to the dead job and its
                # envelope closes that entry.
                #
                # ``reply_q=None`` is accepted and safe: the envelope simply
                # falls back to the shared output queue (``_emit_reply``,
                # :1569-1575) -- the dead caller's private reply queue died
                # with it.
                #
                # It runs on the NEXT loop iteration, exactly once: the queue
                # item is consumed, and this block cannot fire again. Nothing is
                # lost (the record's query text is carried, not dropped) and
                # nothing runs twice (the adopted record is not replayed from
                # disk, it is queued).
                self._journal_replay_attempts[_adopted_record.query_id] = (
                    _adopted_record.attempts
                )
                self._input_queue.put(
                    (_adopted_record.query_id, _adopted_record.query, None)
                )

        # ── WRITE POINT 1: journal the turn BEFORE it runs ──────────────────
        # Placed here, not earlier, so the two early returns above (no agent,
        # stop already signalled) leave NO stale record behind: nothing is
        # journalled that this loop does not then either finish or park.  The
        # query id is the registry job id when this turn has one; otherwise
        # (a turn drained off the input queue in WLM mode) a synthetic one, so
        # a successor can still name the work it is recovering.
        _journal_query_id = self._current_query_id or uuid.uuid4().hex
        _journal_created_at = utcnow_iso()
        _journal_last_tool: Optional[Dict[str, str]] = None
        # Set by the DETACH park, which is the ONE exit that must leave the
        # record behind; every other terminal exit clears it (WRITE POINT 3).
        _journal_parked = False
        # S7b-3: cleared HERE, at the start of the turn and before the write,
        # because a kill sets it MID-turn (after this line) and the value must
        # then survive to WRITE POINT 3.  A later turn must not inherit it --
        # that turn has its own record to clear.
        self._journal_cancel_reason = None
        # S7b-4b: a REPLAYED turn carries the adoption's attempt count to disk
        # instead of re-stamping 0 -- re-stamping would wipe the pre-crash count
        # on this very write, MAX_ATTEMPTS could never fire and a poison query
        # would replay forever. ``pop`` makes it apply to exactly ONE turn: an
        # ordinary turn finds no entry and writes 0.
        self._journal_write(
            query_id=_journal_query_id,
            query=query,
            status=STATUS_INFLIGHT,
            created_at=_journal_created_at,
            attempts=self._journal_replay_attempts.pop(
                self._current_query_id, 0
            ),
        )

        # A pause is reported to the bus/status file ONCE per turn, even though
        # every remaining event of the draining turn observes the park flag.
        _pause_reported = False

        try:
            for event in self._agent.process_query(query):
                                # Fix 1.1B: Poll for stop command on every event
                self._poll_command()

                # Per-worker resource budgets (Phase 3, item 6): fail closed.
                budget_payload = self._budget_check()
                if budget_payload is not None:
                    self.error = budget_payload["error"]
                    final_content = json.dumps(budget_payload)
                    self._publish_event("worker_error", {
                        "error": budget_payload["error"],
                        "reason": budget_payload["reason"],
                    })
                    self._publish_global_event(
                        EventType.WORKER_ERROR if EventType is not None else "worker_error",
                        {
                            "error": budget_payload["error"],
                            "reason": budget_payload["reason"],
                        },
                    )
                    break

                # Log heartbeat for liveliness checks and flush to disk
                self.last_heartbeat = datetime.now(timezone.utc).isoformat()
                self._write_status_file()

                # Check stop signal
                if self._stop_event.is_set():
                    self._agent.request_pause()
                    final_content = json.dumps({
                        "status": "stopped",
                        "message": "Worker stopped by user",
                    })
                    break

                # Check pause signal.
                #
                # A PAUSE IS COOPERATIVE.  pause() has already put the agent
                # into reading mode, whose whole point is to make it CONCLUDE
                # this turn with a Respond instead of being cut off.  Replacing
                # the turn with a synthetic "paused" envelope here threw that
                # conclusion away -- work the user had already paid for -- and
                # contradicted the reading-mode contract (pinned by
                # tests/test_worker_pause_mid_turn_cooperative.py).  So: tell
                # the agent to wind down, report the pause ONCE, and keep
                # draining events until the agent's own conclusion arrives;
                # run() parks the worker (and returns this loop's reply) once
                # the turn is over.
                #
                # DETACH is deliberately NOT cooperative: it is the explicit
                # "park now" signal (it also cancels the in-flight turn via
                # _turn_cancel), so it keeps the terminal envelope and parks
                # without waiting for the turn to wind down.
                if self._park_event.is_set():
                    if not _pause_reported:
                        _pause_reported = True
                        self._agent.request_pause()
                        self.status = "paused"
                        self._write_status_file()
                        self._publish_event('worker_paused', {
                            'status': 'paused',
                            'worker_name': self.worker_name,
                            'session_id': self.session_id or "",
                            'current_context_tokens': self.get_current_context_tokens(),
                            'max_context_tokens': self.max_context_tokens,
                        })
                    if self._detach_requested:
                        final_content = json.dumps({
                            "status": "paused",
                            "message": "Worker paused by user",
                        })
                        # ── WRITE POINT 4: DETACH abandons the turn ────────
                        # The work under way is DISCARDED (unlike a pause,
                        # which concludes the turn), so the record flips to
                        # ``recoverable`` -- it is the successor's to replay --
                        # and the job this turn belonged to is terminalized
                        # ``interrupted`` so a join can never complete or
                        # restart it.  The envelope stays the production stub
                        # (run() stamps ``query_id`` into the reply); only the
                        # JOB's envelope carries the reason.
                        _journal_parked = True
                        self._journal_write(
                            query_id=_journal_query_id,
                            query=query,
                            status=STATUS_RECOVERABLE,
                            created_at=_journal_created_at,
                            last_tool=_journal_last_tool,
                            reason=_JOURNAL_DETACH_REASON,
                        )
                        self._journal_interrupt_job(final_content)
                        break
                else:
                    # A resume that lands inside this same turn clears the park
                    # flag; a later pause must report itself again.
                    _pause_reported = False

                # WLM: surface the supervisor's soft-timeout warning to the
                # worker log exactly once per query (the worker loop keeps
                # running; the supervisor enforces the hard deadline).
                wlm = getattr(self, "_wlm", None)
                if (
                    self._wlm_flag_enabled()
                    and wlm is not None
                    and getattr(wlm, "soft_timeout_warning_emitted", False)
                    and not self._wlm_soft_warning_received
                ):
                    self._wlm_soft_warning_received = True
                    log("WARNING", "tools.worker",
                        f"[WLM] soft timeout warning received "
                        f"query_id={getattr(wlm, 'current_query_id', None)} "
                        f"worker={self.worker_name}")

                event_type = event.get("type", "")

                # Feed each event through the EventProcessor BEFORE the existing chain
                if self._event_processor is not None:
                    try:
                        self._event_processor.process_event(event)
                    except Exception as exc:
                        log('WARNING', 'tools.worker', f"EventProcessor failed for {event_type}: {exc}")

                # Forward event to the per-worker EventBus via the bus adapter
                # (replaces the old manual self._publish_event() calls for
                #  tool_call, tool_result, token_warning, turn_warning,
                if self._worker_bus_adapter is not None:
                    log('TRACE', '[WORKER_EVENT_OBSERVE]', f"event: type={event.get('type')}, yielding to presenter")
                    try:
                        self._worker_bus_adapter.forward_agent_event(event)
                    except Exception as exc:
                        log('WARNING', 'tools.worker', f"forward_agent_event failed for {event_type}: {exc}")

                # Capture final response content and reasoning
                if event_type == "agent_responded":
                    final_content = event.get("content", "")
                    self._last_reasoning = event.get("reasoning")
                    # Capture Respond metadata
                    self._respond_metadata = {
                        "status": event.get("status"),
                        "confidence": event.get("confidence"),
                        "meta": event.get("meta"),
                    }

                # Phase 2B: emit WORKER_PARTIAL_RESULT while work is running so
                # non-blocking callers (submit_query) can stream progress. The
                # terminal PARTIAL_RESULT publish below covers failed/stopped
                # attempts; this covers the success path mid-flight. Turn-level
                # only (agent_responded / partial events with content) — no
                # per-token spam.
                if (
                    self._current_query_id is not None
                    and event_type in ("agent_responded", "partial", "agent_partial",
                                       "agent_message", "assistant_message")
                ):
                    _partial_content = event.get("content", "")
                    if _partial_content:
                        _publish_global_worker_event(EventType.WORKER_PARTIAL_RESULT, {
                            "worker_name": self.worker_name,
                            "session_id": self.session_id or "",
                            "instance_id": self.instance_id,
                            "instance_label": self.instance_label,
                            "query_id": self._current_query_id,
                            "status": "running",
                            "reason": "partial",
                            "content": str(_partial_content)[:2000],
                        })

                # Track tool call count
                if event_type == "tool_call":
                    self._tool_call_count += 1
                    # Execution tracking (default-on): register the in-flight
                    # execution so the interrupt paths (stop(), stale-recovery,
                    # WLM timeout/stop transitions) can terminate it — feature
                    # flag on or off. In flag-on mode the supervisor SHARES
                    # this tracker (see _get_wlm); in flag-off mode stop() and
                    # the stale observer terminate through it directly. The
                    # tracker is cleared when the tool loop returns (run()).
                    try:
                        _container_id = event.get("container_id")
                        _exec_id = event.get("exec_id")
                        _pid = event.get("pid")
                        # Nested tool payloads (arguments/data/result) may
                        # carry the execution identifiers instead.
                        for _key in ("arguments", "data", "result"):
                            _nested = event.get(_key)
                            if isinstance(_nested, dict):
                                if _container_id is None:
                                    _container_id = _nested.get("container_id")
                                if _exec_id is None:
                                    _exec_id = _nested.get("exec_id")
                                if _pid is None:
                                    _pid = _nested.get("pid")
                        _exec_type = (
                            "container_exec" if (_container_id and _pid) else "subprocess"
                        )
                        _wlm_now = getattr(self, "_wlm", None)
                        self._execution_tracker.register(
                            worker_id=self.worker_name,
                            query_id=(
                                getattr(_wlm_now, "current_query_id", None)
                                or self._current_query_id
                            ),
                            tool_call_id=event.get("tool_call_id"),
                            tool_name=event.get("name") or event.get("tool_name"),
                            container_id=_container_id,
                            exec_id=_exec_id,
                            pid=_pid,
                            type=_exec_type,
                        )
                    except Exception as exc:
                        log("WARNING", "tools.worker",
                            f"Execution registration failed for worker "
                            f"'{self.worker_name}': {exc}")

                    # ── WRITE POINT 2: the tool now in flight ─────────────
                    # R2's replacement for a per-tool idempotency ledger: the
                    # successor gets ``{name, args_hash}`` of the LAST tool this
                    # abandoned turn called, so a post-crash replay can tell a
                    # side-effecting call from a pure one.  An UPDATE: same
                    # query id, same ``created_at``, status still ``inflight``.
                    _journal_tool = event.get("name") or event.get("tool_name")
                    if isinstance(_journal_tool, str) and _journal_tool:
                        _journal_last_tool = {
                            "name": _journal_tool,
                            "args_hash": _sha256_args(event.get("arguments")),
                        }
                        self._journal_write(
                            query_id=_journal_query_id,
                            query=query,
                            status=STATUS_INFLIGHT,
                            created_at=_journal_created_at,
                            last_tool=_journal_last_tool,
                        )

                elif event_type == "stopped":
                    stop_reason = event.get("stop_reason", "unknown")
                    final_content = json.dumps({
                        "error": f"Worker stopped: {stop_reason}",
                        "content": f"Worker stopped before producing a final response. Reason: {stop_reason}."
                    })
                    break

                elif event_type == "stop_reason":
                    reason = event.get("stop_reason", "unknown")
                    if reason == "max_turns_reached":
                        final_content = json.dumps({
                            "error": "Worker reached maximum turns",
                        })
                    break

                elif event_type == "error":
                    error_msg = event.get("error", "Unknown error")
                    final_content = json.dumps({"error": error_msg})
                    break

                # [PIPELINE:HOPS] Cache the agent's authoritative post-prune token count
                if event_type == "token_update":
                    log('DEBUG', 'pipeline.hops',
                        f"[PIPELINE:HOPS] Received token_update event: "
                        f"worker={self.worker_name} "
                        f"context_length={event.get('context_length', '?')} "
                        f"total_input={event.get('total_input', '?')} "
                        f"total_output={event.get('total_output', '?')}")
                    context_length = event.get("context_length")
                    if context_length is not None:
                        self._cached_context_tokens = int(context_length)

                        # Emit to per-worker bus so the bridge forwards to frontend
                        if self._worker_bus_adapter is not None:
                            self._worker_bus_adapter.emit_context_updated(int(context_length))

                # Keep legacy tool_execution handler for backward compatibility
                if event_type == "tool_execution":
                    tool_name = event.get("tool_name", "")
                    tool_args = event.get("tool_args", {})
                    result = event.get("result", "")

        except Exception as exc:
            logger.exception("Worker _run_tool_loop failed")
            final_content = json.dumps({"error": f"Worker execution failed: {exc}"})
        finally:
            # Drop THIS thread's pause reference (``_unbind_turn_cancel`` below
            # would clear it too, but the pairing is stated here so the turn
            # boundary stays readable).  The event itself is untouched: it is
            # the worker's, not this module's.
            _bind_pause_event(None)
            _unbind_turn_cancel()
            if _worker_ctx_token is not None and WORKER_NAME_CONTEXTVAR is not None:
                WORKER_NAME_CONTEXTVAR.reset(_worker_ctx_token)

        self.current_task = None
        # Store elapsed time for inclusion in query result
        self._last_elapsed_val = time.monotonic() - _start

        # Detect timeout (D3). The production TimeState enum values are
        # LOWERCASE ('critical' — see agent/core/state.py), so the legacy
        # uppercase comparison never matched and soft timeouts were missed.
        # Detect robustly via any of:
        #   * restriction_reason == 'timeout' (state.py sets this when the
        #     agent's time monitor crosses the timeout threshold), or
        #   * time_state.value case-insensitively equal to 'CRITICAL', or
        #   * the loop actually ran >= timeout_seconds (belt-and-braces
        #     fallback for when the time monitor is disabled but the budget
        #     was exceeded).
        if hasattr(self, '_agent') and self._agent is not None:
            try:
                if _worker_timeout_detected(
                    self._agent.state,
                    getattr(self, '_last_elapsed_val', None),
                    getattr(self, '_timeout_seconds', None),
                ):
                    self._timeout_triggered = True
            except Exception:
                pass

        # Capture final token usage
        self._final_token_usage = self.get_current_context_tokens()

        # ── WRITE POINT 3: nothing is left to recover ───────────────────────
        # Every exit that reaches here DELIVERED (or explicitly reported) this
        # turn's work: success, a budget refusal, stop, an agent error, a stop
        # reason, an exception.  So the record goes, unconditionally -- with
        # TWO exceptions.  The DETACH park threw the turn away and has already
        # flipped the record to ``recoverable`` for its successor: clearing
        # that here would erase exactly the evidence the park exists to leave.
        # A DELIBERATE kill is the second: it cancelled the record (``cancelled``
        # + its reason), which is the TERMINAL EVIDENCE the kill exists to
        # produce -- the same principle, one status over.  ``clear_inflight``
        # deletes any record fenced to this incarnation regardless of status,
        # so without this guard the loop's own exit would erase the cancel a
        # moment after writing it.  A PAUSE needs no special case: it falls
        # through to this clear like any other completed turn (the turn was
        # drained, not lost).
        if not _journal_parked and self._journal_cancel_reason is None:
            self._journal_clear()

        return final_content

    # ── worker-scoped container cleanup ───────────────────────────────────────────────

    def _is_worker_owned_container(self, container: Any) -> bool:
        """Return True when ``container`` belongs to this worker.

        Ownership is established by an EXACT match: the
        ``thoughtmachine.worker`` label value must equal this worker's owner
        identity (``<session_id or 'unknown'>:<worker_name>`` — see module
        docstring). Stale/mismatched values (sibling workers, bare names,
        previous sessions) are ignored.

        The lifecycle-class exclusion is folded into this predicate (rather
        than living in the teardown loop) so the ownership gate and the
        teardown sweep share ONE seam: ownership is True only when the
        container is worker-labelled AND resolves to an ephemeral class.
        FAIL-OPEN on an unresolvable class (recordless container).
        """
        labels = getattr(container, "labels", None)
        if labels is None and isinstance(container, dict):
            labels = container.get("labels")
        if not labels:
            return False
        if labels.get(_WORKER_CONTAINER_LABEL) != self.owner_identity:
            return False
        return not is_worker_teardown_excluded_container(container)

    def _cleanup_worker_containers(self) -> None:
        """Stop and remove containers owned by this worker.

        Idempotent (runs at most once per thread, guarded by
        ``_containers_cleaned``) and never raises, so it is safe to call
        from every teardown path — ``run()``'s finally block and
        ``_action_stop``'s finally paths — without masking the worker's
        own terminal status. Only containers carrying the
        worker-ownership label are touched; resource containers are always
        excluded (see ``is_resource_container``).
        """
        if getattr(self, "_containers_cleaned", False):
            return
        self._containers_cleaned = True
        cm = getattr(self, "_container_manager", None)
        if cm is None:
            return
        try:
            listed = cm.list_containers()
        except Exception:
            return
        if listed is None:
            return
        if not isinstance(listed, (list, tuple)):
            try:
                listed = list(listed)
            except TypeError:
                return
        for container in listed:
            try:
                if is_resource_container(container):
                    continue
                if not self._is_worker_owned_container(container):
                    continue
                if isinstance(container, dict):
                    target = container.get("container_id") or container.get("name")
                else:
                    target = container
                if target is None:
                    continue
                try:
                    cm.stop(target)
                except Exception:
                    pass
                try:
                    cm.remove(target)
                except Exception:
                    pass
            except Exception:
                continue

    # ── thread run loop ────────────────────────────────────────────

    def run(self) -> None:
        """Main worker loop — processes queries until stop is signalled.

        Creates the Agent and WorkerContext lazily on first query,
        reusing them for subsequent queries to maintain conversation state.
        """
        # Resolve shared event-bus state through the tools.workspace.worker
        # facade so that mock.patch("tools.workspace.worker.EventBus" /
        # "register_worker_event_bus" / "unregister_worker_event_bus" /
        # "global_event_bus", ...) (used by tests) is observed here too; the
        # facade re-export bindings are the ones tests replace. Falls back to
        # this module's own bindings while the facade is not fully imported.
        try:
            from tools.workspace.worker import (
                EventBus,
                register_worker_event_bus,
                unregister_worker_event_bus,
                global_event_bus,
            )
        except ImportError:
            pass
        self.last_heartbeat = datetime.now(timezone.utc).isoformat()
        # Worker tray truth: run() executes exactly once per thread, so this
        # is the worker's birth timestamp.
        self.started_at = datetime.now(timezone.utc).isoformat()

        try:
            # ── Load persisted context or create fresh ────────────────
            self._worker_ctx = self._load_context()

            # The live thread ids (real parent session/workspace resolved at
            # spawn) are authoritative over any ids persisted in context.json
            # (an older run may have stored a phantom worker-<uuid> session id
            # or no workspace id at all).
            if self._worker_ctx is not None:
                if self.session_id:
                    self._worker_ctx.session_id = self.session_id
                if self.workspace_id:
                    self._worker_ctx.workspace_id = self.workspace_id

            # Override persisted status/error with live thread state
            self.status = "ready"
            self.error = None
            self._write_status_file()
            # ── Observe-only: a stop request set before run() ─────────
            # _stop_event is created in __init__ (see the inter-thread
            # communication block) and is never assigned outside this file, so
            # it cannot be inherited from a previous life of this instance id —
            # the only way it is already set here is that something really did
            # request this instance to stop (stop() itself, a command-file
            # poll, or the WLM) after it was constructed. Clearing it would
            # discard a genuine stop, so this path deliberately only reports.
            # The main loop below honours the flag and exits; the caller sees a
            # bounded failure, not a hang (see the spawn-side read).
            if self._stop_event.is_set():
                log("WARNING", "tools.worker",
                    f"Worker '{self.worker_name}' instance {self.instance_id} "
                    "was stopped before it started running; observed an "
                    "already-set stop request (not cleared: it is a real "
                    "stop for this instance)")
            # Create per-worker EventBus early so _publish_event below works
            self._event_bus = EventBus()
            register_worker_event_bus(self.session_id or "", self.worker_name, self._event_bus, instance_id=self.instance_id, epoch=self._generation)
            # Attach EventLogger to this worker's per-worker bus
            try:
                from agent.logging.event_logger import EventLogger
                EventLogger.instance().attach_worker_bus(self.worker_name, self._event_bus)
            except Exception:
                pass
            # Publish WORKER_SPAWNED to the global bus so the bridge can
            # discover and subscribe to this worker's per-worker EventBus
            # immediately, rather than waiting for the first query.
            if global_event_bus is not None and EventType is not None and create_event is not None:
                try:
                    evt = create_event(
                        EventType.WORKER_SPAWNED,
                        source=f"worker:{self.worker_name}",
                        session_id=self.session_id or "",
                        data={
                            "session_id": self.session_id or "",
                            "worker_name": self.worker_name,
                            "instance_id": self.instance_id,
                            "instance_label": self.instance_label,
                            "current_context_tokens": self.get_current_context_tokens(),
                            "max_context_tokens": self.max_context_tokens,
                            "status": "ready",
                        },
                    )
                    global_event_bus.publish(evt)
                except Exception:
                    pass
            # Also publish to the per-worker bus for local subscribers
            self._publish_event('worker_spawned', {'status': 'ready'})
            if self._worker_ctx is None:
                # Load system prompt from definition
                system_prompt = self.definition.get(
                    "system_prompt",
                    DEFAULT_WORKER_SYSTEM_PROMPT,
                )
                user_history = [
                    {"role": "system", "content": system_prompt}
                ]
                if self._initial_context:
                    ic_message = initial_context_message(self._initial_context)
                    ic_payload = ic_message["content"]
                    # Auditability without leaking the brief: record only the
                    # payload length and a short digest of the exact string
                    # appended. Briefings can carry secrets, so no preview.
                    logger.debug(
                        "Initial context payload for worker '%s': %d chars, sha256=%s",
                        self.worker_name,
                        len(ic_payload),
                        hashlib.sha256(ic_payload.encode("utf-8")).hexdigest()[:12],
                    )
                    user_history.append(ic_message)
                # Reset cached token count for a fresh run
                self._cached_context_tokens = None
                self._worker_ctx = WorkerContext(
                    session_id=self.session_id,
                    workspace_id=self.workspace_id,
                    worker_name=self.worker_name,
                    user_history=user_history,
                )

                # ── Fix 2: Auto-process initial context query ──
                # When spawn provides context with a "query" key, the worker
                # should start working immediately without requiring a
                # separate query action. We push the query into the input
                # queue so the existing loop (agent creation + _run_tool_loop)
                # handles it naturally.
                if self._initial_context:
                    initial_query = self._initial_context.get("query")
                    if initial_query:
                        logger.debug(
                            "Auto-queueing initial query for worker '%s'",
                            self.worker_name,
                        )
                        self._input_queue.put(initial_query)

            else:
                # ── Context loaded from disk: merge initial_context if provided ──
                # When context.json exists, we preserve the loaded conversation and
                # merge _initial_context into it rather than replacing it.
                if self._initial_context:
                    # F1: prune an incomplete attempt for a DIFFERENT query
                    # before merging the new context. Prevents context doubling
                    # on force-respawn after a soft timeout: the loaded
                    # history's last attempt belongs to a previous task
                    # (last_query != new query) and never completed
                    # (last_query != last_completed_query). Completed attempts
                    # are preserved.
                    initial_query = self._initial_context.get("query")
                    if initial_query:
                        if self._wlm_flag_enabled() and getattr(self, "_wlm", None) is not None:
                            self._prune_abandoned_attempt_via_wlm(initial_query)
                        else:
                            self._prune_stale_attempt_before_merge(initial_query)
                    # Add the initial context as the conversation's first user
                    # message for continuity -- NOT as a system message, which
                    # ensure_system_prompt would delete (see
                    # initial_context_message).
                    ctx_msg = initial_context_message(self._initial_context)
                    # Check if this initial_context was already injected (avoid duplicates
                    # on repeated spawn calls)
                    already_present = any(
                        msg.get("content", "") == ctx_msg["content"]
                        for msg in self._worker_ctx.user_history
                    )
                    if not already_present:
                        self._worker_ctx.user_history.append(ctx_msg)
                        logger.debug(
                            "Merged initial_context into loaded context for worker '%s'",
                            self.worker_name,
                        )
                    # Auto-queue the query if present
                    if initial_query:
                        logger.debug(
                            "Auto-queueing initial query for worker '%s' (loaded context)",
                            self.worker_name,
                        )
                        self._input_queue.put(initial_query)

            self._save_context()

            while not self._stop_event.is_set():
                # Check for command.json before blocking on input queue
                self._poll_command()
                if self._stop_event.is_set():
                    break
                # Wait for the next query with a 2-second timeout
                # so we can also poll for command.json periodically
                try:
                    raw = self._input_queue.get(timeout=2.0)
                except queue.Empty:
                    self._heartbeat_tick()
                    continue
                if self._stop_event.is_set():
                    break
                if raw is _PARK_SENTINEL or raw is None:
                    # ``_PARK_SENTINEL`` means "wake and re-read the flags" and
                    # nothing else; a bare ``None`` keeps its unambiguous
                    # meaning: STOP.  Both used to be a bare ``None``, so a
                    # leftover wake-up marker consumed after ``resume()`` had
                    # already cleared ``_park_event`` read as STOP and broke
                    # the loop with work still queued (BUG 1).
                    if self._park_event.is_set():
                        # PHASE 2.1a: a park entered because of a detach
                        # reports 'detached', not 'paused'.
                        self.status = "detached" if self._detach_requested else "paused"
                        self._write_status_file()
                        self._publish_event('worker_paused', {
                            'status': 'paused',
                            'worker_name': self.worker_name,
                            'session_id': self.session_id or "",
                            'current_context_tokens': self.get_current_context_tokens(),
                            'max_context_tokens': self.max_context_tokens,
                        })
                        if global_event_bus is not None and EventType is not None and create_event is not None:
                            try:
                                evt = create_event(
                                    EventType.WORKER_STATUS,
                                    data={
                                        "session_id": self.session_id or "",
                                        "worker_name": self.worker_name,
                                        "instance_id": self.instance_id,
                                        "instance_label": self.instance_label,
                                        "status": "paused",
                                        "current_context_tokens": self.get_current_context_tokens(),
                                        "max_context_tokens": self.max_context_tokens,
                                        "started_at": self.started_at,
                                        "last_query_at": self.last_query_at,
                                        "paused_manually": bool(self._manual_only_pause),
                                    },
                                    source=f"worker:{self.worker_name}",
                                    session_id=self.session_id or "",
                                )
                                global_event_bus.publish(evt)
                            except Exception:
                                pass
                        self.current_task = None
                        self.last_heartbeat = datetime.now(timezone.utc).isoformat()
                        # Block until resumed (or stopped)
                        while self._park_event.is_set() and not self._stop_event.is_set():
                            # DEAF-PARK FIX: a parked worker must still notice
                            # cross-process control.  Without this poll a
                            # command.json 'kill' (the ONLY terminating action,
                            # PHASE 2.1a) was invisible while parked, so the
                            # worker could not be terminated at all until the
                            # detached TTL swept it -- and a 'stop' could not
                            # convert a pause-park into a detach.  Costs one
                            # stat/parse per wake-up (1 s cadence, no
                            # busy-spin).
                            self._poll_command()
                            # The poll can KILL this worker (a command.json
                            # 'kill' sets _stop_event): leave the wake-up at
                            # once instead of sleeping out the remaining wait.
                            if self._stop_event.is_set():
                                break
                            self._heartbeat_tick()
                            self._resume_event.wait(1.0)
                        if self._stop_event.is_set():
                            break
                        # Resume — transition back to ready
                        self.status = "ready"
                        self._resume_event.clear()
                        self._write_status_file()
                        self._publish_event('worker_resumed', {
                            'status': 'ready',
                            'worker_name': self.worker_name,
                            'session_id': self.session_id or "",
                            'current_context_tokens': self.get_current_context_tokens(),
                            'max_context_tokens': self.max_context_tokens,
                        })
                        if global_event_bus is not None and EventType is not None and create_event is not None:
                            try:
                                evt = create_event(
                                    EventType.WORKER_STATUS,
                                    data={
                                        "session_id": self.session_id or "",
                                        "worker_name": self.worker_name,
                                        "instance_id": self.instance_id,
                                        "instance_label": self.instance_label,
                                        "status": "ready",
                                        "current_context_tokens": self.get_current_context_tokens(),
                                        "max_context_tokens": self.max_context_tokens,
                                        "started_at": self.started_at,
                                        "last_query_at": self.last_query_at,
                                        "paused_manually": bool(self._manual_only_pause),
                                    },
                                    source=f"worker:{self.worker_name}",
                                    session_id=self.session_id or "",
                                )
                                global_event_bus.publish(evt)
                            except Exception:
                                pass
                        # Drain stale park markers / None unblock signals left
                        # by pause() so they are not mistaken for a stop.
                        # PHASE 2.1b: the surviving (real) items are re-appended
                        # in their ORIGINAL order.  Re-putting only the first
                        # one would send it to the TAIL and break the strict
                        # FIFO drain order of the queue.
                        leftover = []
                        try:
                            while True:
                                item = self._input_queue.get_nowait()
                                if item is not None and item is not _PARK_SENTINEL:
                                    leftover.append(item)
                        except queue.Empty:
                            pass
                        for item in leftover:
                            self._input_queue.put(item)
                        continue
                    if raw is _PARK_SENTINEL:
                        # A stale wake-up marker: ``resume()`` already cleared
                        # ``_park_event``, so this is a no-op -- NOT a stop
                        # and NOT an immediate re-park.  Discard and continue.
                        continue
                    # Bare None with no pause and no stop event is the legacy
                    # stop sentinel. Preserve that contract.
                    break
                # send_query enqueues (query_id, query[, reply_q]) tuples so
                # replies can be correlated and routed to the caller's private
                # queue; plain-string producers (WLM handler, initial
                # auto-query, stop/pause markers) carry no id and keep the
                # shared _output_queue fallback.
                if isinstance(raw, tuple) and len(raw) == 3:
                    query_id, query, reply_q = raw
                elif isinstance(raw, tuple) and len(raw) == 2:
                    query_id, query = raw
                    reply_q = None
                else:
                    query_id, query, reply_q = None, raw, None
                # PHASE 2.1b: the job has left the queue and is now the single
                # in-flight query, so it is no longer rejectable as queued.
                self._discard_pending_job(query_id)
                self._current_query_id = query_id
                self._current_reply_queue = reply_q

                # F1: record the most recent query dequeued (even if it times
                # out mid-attempt) so a later force-respawn can tell whether
                # the last attempt completed.
                self._last_query = query
                # Worker tray truth: record when this worker accepted a query.
                self.last_query_at = datetime.now(timezone.utc).isoformat()

                # ── Create Agent lazily (first query only) ────────────
                if self._agent is None:
                    agent_cfg = self._build_agent_config()
                    if agent_cfg is None:
                        reply = json.dumps({
                            "error": "Cannot create Agent: invalid agent_config",
                            **({"query_id": self._current_query_id}
                               if self._current_query_id is not None else {}),
                        })
                        self._emit_reply(reply)
                        break
                    # Lazy import to avoid circular dep: agent.core.agent ↔ tools
                    try:
                        from agent.core.agent import Agent
                    except ImportError:
                        reply = json.dumps({
                            "error": "Cannot create Agent: module not importable",
                            **({"query_id": self._current_query_id}
                               if self._current_query_id is not None else {}),
                        })
                        self._emit_reply(reply)
                        break

                    # Publish WORKER_SPAWNED is now handled earlier in run() —
                    # no need for a duplicate lazy publish here.
                    self._agent = Agent(
                        config=agent_cfg,
                        session=self._worker_ctx,
                        event_bus=self._event_bus,
                    )

                # ── Fix 1.2: Set busy before processing query ───────────
                self.status = "busy"
                self._write_status_file()
                self._publish_event('worker_status', {'status': 'busy', 'current_task': self.current_task})
                # Also publish to global_event_bus so the bridge receives it
                if global_event_bus is not None and EventType is not None and create_event is not None:
                    try:
                        evt = create_event(
                            EventType.WORKER_STATUS,
                            data={
                                "session_id": self.session_id or "",
                                "worker_name": self.worker_name,
                                "instance_id": self.instance_id,
                                "instance_label": self.instance_label,
                                "status": "busy",
                                "current_task": self.current_task,
                                "current_context_tokens": self.get_current_context_tokens(),
                                "max_context_tokens": self.max_context_tokens,
                                "started_at": self.started_at,
                                "last_query_at": self.last_query_at,
                                "paused_manually": bool(self._manual_only_pause),
                            },
                            source=f"worker:{self.worker_name}",
                            session_id=self.session_id or "",
                        )
                        global_event_bus.publish(evt)
                    except Exception:
                        pass
                self._publish_global_event(EventType.WORKER_RUNNING, {
                    "status": "busy",
                    "current_task": self.current_task,
                    "current_context_tokens": self.get_current_context_tokens(),
                    "max_context_tokens": self.max_context_tokens,
                    **({"query_id": self._current_query_id}
                       if self._current_query_id is not None else {}),
                })
                # ── Reset turn/time state per query ────────────────────────
                # Each query is an isolated unit — reset turn counter and
                # time tracking so the restriction pipeline starts fresh.
                if self._agent is not None and hasattr(self._agent, 'state'):
                    self._agent.state.current_turn = 0
                    if TurnState is not None:
                        self._agent.state.turn_state = TurnState.LOW
                        self._agent.state.last_turn_warning_state = TurnState.LOW
                    _cp_before_restrictions_active = getattr(self._agent.state, 'restrictions_active', None)
                    _cp_before_restriction_reason = getattr(self._agent.state, 'restriction_reason', None)
                    self._agent.state.restrictions_active = False
                    self._agent.state.restrictions_pending = False
                    self._agent.state.restriction_reason = None
                    log('INFO', '[CONTROL_PLANE_WORKER_CLEAR]', f'state_id={id(self._agent.state)} before_restrictions_active={_cp_before_restrictions_active} before_restriction_reason={_cp_before_restriction_reason} after_restrictions_active={getattr(self._agent.state, "restrictions_active", None)} after_restriction_reason={getattr(self._agent.state, "restriction_reason", None)} session_id={self.session_id or "unknown"}')
                    # wall-clock time; agent.py uses time.time() for consistency
                    self._agent.state.time_start = time.time()
                    if TimeState is not None:
                        self._agent.state.last_time_warning_state = TimeState.LOW
                # A soft timeout is per-query: clear the sticky flag so the
                # next query starts fresh (same per-query reset semantics as
                # the turn/time state above, but independent of the agent).
                self._timeout_triggered = False

                try:
                    reply = self._run_tool_loop(query)
                finally:
                    # Execution tracking (default-on): the in-flight tool
                    # calls have completed (or the loop was interrupted) —
                    # forget them so a later stop() never kills stale pids.
                    # Flag-on mode clears the same tracker via the
                    # supervisor's transition_idle; clearing here is an
                    # idempotent no-op in that window.
                    try:
                        self._execution_tracker.clear()
                    except Exception:
                        pass

                # Check if worker was paused during the tool loop
                if self._park_event.is_set():
                    # Preserve paused status — don't overwrite with "ready"
                    # PHASE 2.1a: a detach-requested park reports 'detached'.
                    self.status = "detached" if self._detach_requested else "paused"
                    self._write_status_file()
                    self._publish_event('worker_paused', {
                        'status': 'paused',
                        'worker_name': self.worker_name,
                        'session_id': self.session_id or "",
                        'current_context_tokens': self.get_current_context_tokens(),
                        'max_context_tokens': self.max_context_tokens,
                    })
                    # Also publish to global_event_bus so the bridge receives it
                    if global_event_bus is not None and EventType is not None and create_event is not None:
                        try:
                            evt = create_event(
                                EventType.WORKER_STATUS,
                                data={
                                    "session_id": self.session_id or "",
                                    "worker_name": self.worker_name,
                                    "instance_id": self.instance_id,
                                    "instance_label": self.instance_label,
                                    "status": "paused",
                                    "current_context_tokens": self.get_current_context_tokens(),
                                    "max_context_tokens": self.max_context_tokens,
                                    "started_at": self.started_at,
                                    "last_query_at": self.last_query_at,
                                    "paused_manually": bool(self._manual_only_pause),
                                },
                                source=f"worker:{self.worker_name}",
                                session_id=self.session_id or "",
                            )
                            global_event_bus.publish(evt)
                        except Exception:
                            pass

                    self.current_task = None
                    self.last_heartbeat = datetime.now(timezone.utc).isoformat()

                    # Persist context before blocking
                    if self._worker_ctx is not None:
                        self._worker_ctx.compact_after_summary()
                    self._save_context()

                    # Terminalize any in-flight job: a Pause All during an
                    # async job must not strand it as 'running' forever, and a
                    # later resume must not complete or restart it (BUG 2).
                    if self._current_query_id is not None:
                        try:
                            _registry = _get_worker_job_registry()
                            if _registry is not None:
                                _registry.terminalize(
                                    self._current_query_id,
                                    status="paused",
                                    envelope={"content": reply, "status": "paused"},
                                )
                        except Exception:
                            pass

                    # Send the pause response back to the waiting tool call.
                    # A correlated (legacy send_query) caller needs the query id
                    # echoed so it can accept the reply; plain-string producers
                    # (WLM handler, spawn auto-query) keep the raw reply.
                    if self._current_query_id is not None:
                        self._emit_reply(json.dumps({
                            "content": reply,
                            "status": "paused",
                            "query_id": self._current_query_id,
                        }))
                    else:
                        self._emit_reply(reply)

                    # Block until resumed (or stopped)
                    while self._park_event.is_set() and not self._stop_event.is_set():
                        # DEAF-PARK FIX (same as the park loop above): poll for
                        # a cross-process command while parked, so 'kill'
                        # terminates and 'stop' detaches instead of both being
                        # swallowed until the detached TTL expires.
                        self._poll_command()
                        # A 'kill' poll sets _stop_event -- do not sleep out the
                        # rest of this wake-up before leaving the park loop.
                        if self._stop_event.is_set():
                            break
                        self._heartbeat_tick()
                        self._resume_event.wait(1.0)

                    if self._stop_event.is_set():
                        break

                    # Resume — transition back to ready
                    self.status = "ready"
                    self._resume_event.clear()
                    self._write_status_file()
                    self._publish_event('worker_resumed', {
                        'status': 'ready',
                        'worker_name': self.worker_name,
                        'session_id': self.session_id or "",
                        'current_context_tokens': self.get_current_context_tokens(),
                        'max_context_tokens': self.max_context_tokens,
                    })
                    if global_event_bus is not None and EventType is not None and create_event is not None:
                        try:
                            evt = create_event(
                                EventType.WORKER_STATUS,
                                data={
                                    "session_id": self.session_id or "",
                                    "worker_name": self.worker_name,
                                    "instance_id": self.instance_id,
                                    "instance_label": self.instance_label,
                                    "status": "ready",
                                    "current_context_tokens": self.get_current_context_tokens(),
                                    "max_context_tokens": self.max_context_tokens,
                                    "started_at": self.started_at,
                                    "last_query_at": self.last_query_at,
                                    "paused_manually": bool(self._manual_only_pause),
                                },
                                source=f"worker:{self.worker_name}",
                                session_id=self.session_id or "",
                            )
                            global_event_bus.publish(evt)
                        except Exception:
                            pass

                    # Drain stale park markers / None unblock signals from the
                    # input queue.  These are left behind by pause() or _poll_command()
                    # when the worker was busy in _run_tool_loop.  Without
                    # this drain, the main loop interprets them as stop
                    # signals (line 1172) and exits immediately after resume.
                    # PHASE 2.1b: the surviving (real) items are re-appended in
                    # their ORIGINAL order — re-putting only the first one
                    # would send it to the TAIL and break strict FIFO order.
                    leftover = []
                    try:
                        while True:
                            item = self._input_queue.get_nowait()
                            if item is not None and item is not _PARK_SENTINEL:
                                leftover.append(item)
                    except queue.Empty:
                        pass
                    for item in leftover:
                        self._input_queue.put(item)

                    # Continue the outer loop to wait for next query
                    continue

                # Back to ready after query completes (not paused)
                self.status = "ready"
                self._write_status_file()
                self._publish_event('worker_status', {'status': 'ready'})
                # Also publish to global_event_bus so the bridge receives it
                if global_event_bus is not None and EventType is not None and create_event is not None:
                    try:
                        evt = create_event(
                            EventType.WORKER_STATUS,
                            data={
                                "session_id": self.session_id or "",
                                "worker_name": self.worker_name,
                                "instance_id": self.instance_id,
                                "instance_label": self.instance_label,
                                "status": "ready",
                                "current_context_tokens": self.get_current_context_tokens(),
                                "max_context_tokens": self.max_context_tokens,
                                "started_at": self.started_at,
                                "last_query_at": self.last_query_at,
                                "paused_manually": bool(self._manual_only_pause),
                            },
                            source=f"worker:{self.worker_name}",
                            session_id=self.session_id or "",
                        )
                        global_event_bus.publish(evt)
                    except Exception:
                        pass

                self.current_task = None
                self.last_heartbeat = datetime.now(timezone.utc).isoformat()

                # F1: mark this query as completed — unless a soft timeout cut
                # it short, in which case the attempt is left incomplete so a
                # later force-respawn can prune it.
                if not self._timeout_triggered:
                    self._last_completed_query = query

                # Compact conversation history after summarization
                # (Agent inserts summary messages but doesn't remove old ones)
                if self._worker_ctx is not None:
                    self._worker_ctx.compact_after_summary()

                # Persist and log
                if self._wlm_flag_enabled() and getattr(self, "_wlm", None) is not None:
                    try:
                        self._last_query_id = self._wlm.current_query_id
                    except Exception:
                        pass
                self._save_context()

                # Send the response back to the waiting tool call

                # Build telemetry
                telemetry = {
                    "elapsed_seconds": round(self._last_elapsed_val, 1) if self._last_elapsed_val else None,
                    "tool_call_count": self._tool_call_count,
                    "timeout_triggered": self._timeout_triggered,
                    "token_usage": self._final_token_usage,
                }

                # Determine status for force-stop cases. A soft timeout is
                # terminal for the attempt even when the agent emitted a
                # 'progress' status: the envelope must report 'timeout' so the
                # caller surfaces the worker's own timeout instead of a
                # still-running impression (D3).
                status = self._respond_metadata.get("status")
                if self._timeout_triggered:
                    status = "timeout"

                # Build envelope
                envelope = {
                    "content": reply if reply else "Worker finished with no output.",
                    "status": status,
                    "confidence": self._respond_metadata.get("confidence"),
                    "meta": self._respond_metadata.get("meta"),
                    "telemetry": telemetry,
                }

                # Legacy path: expose this attempt's query id in the envelope
                # so send_query can correlate replies even when multiple
                # callers overlap. The WLM block below keeps precedence when
                # the supervisor is active (plain-string producer, so this
                # legacy stamp stays None in that case anyway).
                if self._current_query_id is not None:
                    envelope["query_id"] = self._current_query_id

                # WLM: expose the supervisor's query id in the envelope so the
                # caller can correlate the reply with the lifecycle manager.
                if self._wlm_flag_enabled() and getattr(self, "_wlm", None) is not None:
                    try:
                        envelope["query_id"] = self._wlm.current_query_id
                    except Exception:
                        pass

                # If reply is already JSON (error case), parse and merge
                if reply:
                    try:
                        parsed_reply = json.loads(reply)
                        if isinstance(parsed_reply, dict):
                            if "error" in parsed_reply:
                                envelope["content"] = parsed_reply.get("error", reply)
                                envelope["status"] = envelope["status"] or ("timeout" if self._timeout_triggered else "error")
                            else:
                                envelope["content"] = reply
                    except (json.JSONDecodeError, TypeError):
                        envelope["content"] = reply

                if envelope.get("status") in ("stopped", "paused", "error", "timeout", "max_turns", "max_turns_reached"):
                    self._publish_global_event(EventType.WORKER_PARTIAL_RESULT, {
                        "status": envelope.get("status"),
                        "reason": envelope.get("status"),
                        "content": str(envelope.get("content", ""))[:2000],
                        "current_context_tokens": self.get_current_context_tokens(),
                        "max_context_tokens": self.max_context_tokens,
                        **({"query_id": self._current_query_id}
                           if self._current_query_id is not None else {}),
                    })

                # Phase 2B: record the completed job (best-effort — never
                # breaks the worker). The registry stores the full envelope so
                # non-blocking callers (submit_query) can retrieve results via
                # job_status.
                if self._current_query_id is not None:
                    try:
                        _registry = _get_worker_job_registry()
                        if _registry is not None:
                            _registry.complete(self._current_query_id, envelope)
                    except Exception:
                        pass

                self._emit_reply(json.dumps(envelope, default=str))

        except Exception as exc:
            logger.exception("Worker thread %s failed", self.worker_name)
            self.status = "error"
            self.error = str(exc)
            self._write_status_file()
            # Put the error into the output queue so any waiting query call gets it
            error_json = json.dumps({
                "error": str(exc),
                **({"query_id": self._current_query_id}
                   if self._current_query_id is not None else {}),
            })
            try:
                self._emit_reply(error_json, nowait=True)
            except queue.Full:
                pass
            self._publish_event('worker_error', {'error': str(exc)})
            # Also publish to global_event_bus so the bridge receives it
            if global_event_bus is not None and EventType is not None and create_event is not None:
                try:
                    evt = create_event(
                        EventType.WORKER_ERROR,
                        data={
                            "session_id": self.session_id or "",
                            "worker_name": self.worker_name,
                            "instance_id": self.instance_id,
                            "instance_label": self.instance_label,
                            "error": str(exc),
                            "current_context_tokens": self.get_current_context_tokens(),
                            "max_context_tokens": self.max_context_tokens,
                            **({"query_id": self._current_query_id}
                               if self._current_query_id is not None else {}),
                        },
                        source=f"worker:{self.worker_name}",
                        session_id=self.session_id or "",
                    )
                    global_event_bus.publish(evt)
                except Exception:
                    pass
        else:
            # Terminal status on the KILL path is 'killed' (not 'completed');
            # a clean drain with no kill request ends as 'completed'.
            final_status = "killed" if self._stop_event.is_set() else "completed"
            self.status = final_status
            self._write_status_file()
            self._publish_event('worker_completed', {'status': final_status, 'worker_name': self.worker_name})
            # Also publish to global_event_bus so the bridge receives it
            if global_event_bus is not None and EventType is not None and create_event is not None:
                try:
                    evt = create_event(
                        EventType.WORKER_COMPLETED,
                        data={
                            "session_id": self.session_id or "",
                            "worker_name": self.worker_name,
                            "instance_id": self.instance_id,
                            "instance_label": self.instance_label,
                            "status": final_status,
                            "current_context_tokens": self.get_current_context_tokens(),
                            "max_context_tokens": self.max_context_tokens,
                            **({"query_id": self._current_query_id}
                               if self._current_query_id is not None else {}),
                        },
                        source=f"worker:{self.worker_name}",
                        session_id=self.session_id or "",
                    )
                    global_event_bus.publish(evt)
                except Exception:
                    pass
        finally:
            # D2: a worker that TERMINATED VIA ITS OWN TIMEOUT MECHANISM is
            # removed from the live registry so it never leaves a stale
            # (ghost) entry behind. Cooperatively-stopped or completed workers
            # KEEP their entry (callers observe the final status via
            # action='check'; spawn only reuses live threads). Best-effort:
            # the force-stop path may already have popped it.
            if self._timeout_triggered:
                try:
                    _WorkerRegistry.get_instance().unregister_worker(
                        self.session_id or "", self.worker_name,
                        instance_id=self.instance_id,
                        epoch=getattr(self, "_generation", None),
                    )
                except Exception:
                    pass
                # S7a-3: mirror that removal onto the handle store, with the
                # same epoch -- a dying predecessor's finally must not unplug
                # its successor's handle.  Keyed by the RAW session id, the
                # value spawn registered under and the routes look up with.
                _unregister_runtime_handles(
                    [(self.session_id, self.worker_name, self.instance_id)],
                    epoch=getattr(self, "_generation", None),
                )
            try:
                unregister_worker_event_bus(self.session_id or "", self.worker_name, instance_id=self.instance_id, epoch=getattr(self, "_generation", None))
            except Exception:
                pass
            if self._worker_ctx is not None:
                self._save_context()
            # Stop/remove containers this worker created (idempotent).
            self._cleanup_worker_containers()

    # ── persistence ────────────────────────────────────────────────

    def _context_path(self) -> Path:
        return self._worker_dir / "context.json"

    def _allocate_generation(self) -> int:
        """F2: allocate a monotonic per-worker generation token.

        Every new WorkerThread instance holds a strictly higher generation than
        any thread it replaces, so a still-alive stale thread's
        ``_save_context()`` is rejected by the guard once the replacement has
        persisted.  The counter is the ``worker_dir/generation`` sidecar folded
        together with any ``generation`` already persisted in context.json (so
        a lost sidecar does not restart the counter at 1).

        SINGLE-LOCK INVARIANT: the entire read-modify-write -- sidecar read,
        context.json read, ``base + 1``, temp-file write, ``os.replace`` -- runs
        inside ONE ``FileLock`` on the sidecar, itself nested in a per-worker
        ``threading.Lock`` (see ``_generation_thread_lock``: the in-process lock
        is the cheap serialiser for spawns, which are threads of one backend
        process).  A read taken before the lock
        is worthless: two concurrent spawners would see the same base and both
        persist the same epoch, which is precisely the race this guard closes.

        FAIL-CLOSED: nothing here swallows a persistence error.  A lock timeout
        (``FileLockTimeoutError``), a write failure or a rename failure
        propagates.  Both call sites propagate: ``__init__`` (spawn) and
        ``reset()``.  A worker that cannot be given a strictly greater epoch
        must not be spawned, and a reset that could not raise the epoch must
        not be reported as done.

        BELT-AND-BRACES: ``FileLock.release()`` USED to unlink the lock file, so
        a third acquirer could lock a fresh inode the previous holder's waiter
        still had open.  That defect is fixed in ``session/lock.py``: release no
        longer unlinks, so the inode -- and therefore the mutual exclusion -- is
        stable for every acquirer, in this process or another.  The guard below
        is kept regardless, because it costs one stat: the rename is verified
        afterwards (value AND inode) and the whole allocation retried under a
        fresh lock, bounded by ``_GENERATION_ALLOCATE_ATTEMPTS``.  An epoch is
        only ever returned after it has been observed persisted, so whatever
        exposure is left over (a cross-process holder, a filesystem where
        locking is advisory) surfaces as a retry rather than as a duplicate
        epoch.

        DEGRADED: when ``session.lock`` is unavailable the counter is still
        allocated unlocked, and a one-shot warning records that monotonicity is
        not guaranteed.
        """
        global _UNLOCKED_GENERATION_WARNED

        gen_file = self._worker_dir / "generation"
        ctx_path = self._context_path()

        if FileLock is None:
            if not _UNLOCKED_GENERATION_WARNED:
                _UNLOCKED_GENERATION_WARNED = True
                log(
                    "WARNING", "tools.worker",
                    f"session.lock unavailable: generation allocation for "
                    f"{self._worker_dir} is unlocked, so generation "
                    f"monotonicity is NOT guaranteed across concurrent spawns",
                )
            with _generation_thread_lock(str(gen_file)):
                new_gen, _ino = self._write_generation(gen_file, ctx_path)
            return new_gen

        with _generation_thread_lock(str(gen_file)):
            for _attempt in range(_GENERATION_ALLOCATE_ATTEMPTS):
                with FileLock(str(gen_file)):
                    new_gen, ino = self._write_generation(gen_file, ctx_path)
                    if self._generation_persisted(gen_file, new_gen, ino):
                        return new_gen
        raise RuntimeError(
            f"could not allocate a generation for {self._worker_dir}: "
            f"{_GENERATION_ALLOCATE_ATTEMPTS} consecutive attempts were each "
            f"overwritten by a concurrent allocator (two holders raced the same "
            f"read-modify-write); refusing to return an epoch that was never "
            f"observed persisted"
        )

    def _write_generation(self, gen_file: Path, ctx_path: Path) -> tuple:
        """Read-modify-write the sidecar; return ``(epoch, renamed_inode)``.

        The caller MUST already hold the ``FileLock`` on ``gen_file`` -- that is
        the single-lock invariant.  On any failure the temp file is removed and
        the exception is re-raised: a partially applied increment must never be
        reported as an allocated epoch.
        """
        base = self._read_generation_sidecar(gen_file)
        # context.json is only a fallback for a LOST sidecar, so no shape of it
        # may brick a spawn: anything unreadable just means "no counter here".
        if ctx_path.exists():
            try:
                base = max(base, int(json.loads(ctx_path.read_text(encoding="utf-8")).get("generation") or 0))
            except (json.JSONDecodeError, OSError, TypeError, ValueError):
                pass
        new_gen = base + 1
        fd, tmp_path_str = tempfile.mkstemp(
            dir=str(self._worker_dir), prefix=".generation_", suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(str(new_gen))
            # os.replace preserves the source inode, so this identifies OUR
            # write later (see _generation_persisted).
            ino = os.stat(tmp_path_str).st_ino
            os.replace(tmp_path_str, str(gen_file))
        except BaseException:
            try:
                os.unlink(tmp_path_str)
            except OSError:
                pass
            raise
        return new_gen, ino

    @staticmethod
    def _read_generation_sidecar(gen_file: Path) -> int:
        """Current sidecar value, or 0 when the sidecar is absent/unparsable.

        The sidecar is authoritative, so a real OSError (permissions, I/O)
        propagates instead of silently resetting the counter -- that reset is
        exactly what would let a stale thread keep saving.
        """
        try:
            return int(gen_file.read_text(encoding="utf-8").strip() or 0)
        except (FileNotFoundError, ValueError):
            return 0

    @staticmethod
    def _generation_persisted(gen_file: Path, new_gen: int, ino: int) -> bool:
        """True iff the sidecar now holds exactly the epoch WE renamed in.

        The inode half is what makes this meaningful: a sidecar carrying our
        value on a different inode means a concurrent allocator won the rename
        (possibly writing the same value), so the allocation must be retried.
        """
        try:
            value = int(gen_file.read_text(encoding="utf-8").strip() or 0)
            return value == new_gen and os.stat(gen_file).st_ino == ino
        except (OSError, ValueError):
            return False

    def _load_context(self) -> Optional[WorkerContext]:
        """Load WorkerContext from disk, if present. Returns None if not found."""
        path = self._context_path()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                ctx = WorkerContext.from_persistable_dict(data)
                self.status = data.get("status", "ready")
                self.error = data.get("error")
                self.last_heartbeat = data.get("last_heartbeat")
                self._last_query = data.get("last_query")
                self._last_completed_query = data.get("last_completed_query")
                self._last_query_id = data.get("last_query_id")
                try:
                    self._pruned_since_last_query = int(
                        data.get("pruned_since_last_query") or 0
                    )
                except (TypeError, ValueError):
                    self._pruned_since_last_query = 0
                # F2: restore generation; keep the max so this thread's saves
                # are never rejected against a file it just loaded.
                try:
                    file_gen = int(data.get("generation") or 0)
                except (TypeError, ValueError):
                    file_gen = 0
                self._generation = max(self._generation, file_gen)
                return ctx
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(
                    "Failed to load worker context %s: %s", path, exc
                )
        return None

    def _prune_stale_attempt_before_merge(self, new_query: str) -> None:
        """F1: drop an incomplete attempt for a DIFFERENT query before merging new context.

        Prevents context doubling on force-respawn after a soft timeout: the loaded
        history's last attempt belongs to a previous task (last_query != new_query)
        and never completed (last_query != last_completed_query). Truncate from the
        last "Initial context" boundary so the new query does not execute against
        (and double) the stale partial attempt. Completed attempts are preserved.
        """
        last_query = self._last_query
        last_completed = self._last_completed_query
        if not last_query or last_query == new_query:
            return  # same-task retry (or legacy context w/o marker) — keep history
        if last_query == last_completed:
            return  # last attempt completed — completed work is kept
        history = self._worker_ctx.user_history
        ic_indices = [
            i for i, msg in enumerate(history) if is_initial_context_message(msg)
        ]
        if not ic_indices:
            return
        boundary = ic_indices[-1]
        del history[boundary:]
        self._pruned_since_last_query += 1
        logger.debug(
            "Pruned incomplete attempt for query %r before merging new context (worker '%s')",
            last_query,
            self.worker_name,
        )

    def _prune_abandoned_attempt_via_wlm(self, new_query: str) -> None:
        """WLM variant of F1: prune an attempt the supervisor marked abandoned.

        Uses the supervisor's query outcome log: when the last attempt's query
        id is recorded as abandoned (timed out / stopped mid-query), truncate
        the loaded history from the last "Initial context" boundary before the
        new context is merged. Falls back to the legacy F1 heuristic whenever
        the WLM signal is unavailable (no supervisor, no query id, or the id
        is not in the log).
        """
        wlm = getattr(self, "_wlm", None)
        if wlm is None:
            self._prune_stale_attempt_before_merge(new_query)
            return
        try:
            abandoned = set(wlm.abandoned_query_ids())
            completed = set(wlm.completed_query_ids())
        except Exception as exc:
            log("WARNING", "tools.worker",
                f"WLM query-log read failed for worker '{self.worker_name}' "
                f"(falling back to F1): {exc}")
            self._prune_stale_attempt_before_merge(new_query)
            return
        last_qid = getattr(self, "_last_query_id", None)
        if not last_qid:
            self._prune_stale_attempt_before_merge(new_query)
            return
        if last_qid in completed:
            return  # last attempt completed — completed work is kept
        if last_qid not in abandoned:
            # WLM has no verdict for this id (e.g. pre-WLM context) — fall back.
            self._prune_stale_attempt_before_merge(new_query)
            return
        history = self._worker_ctx.user_history
        ic_indices = [
            i for i, msg in enumerate(history) if is_initial_context_message(msg)
        ]
        if not ic_indices:
            return
        boundary = ic_indices[-1]
        del history[boundary:]
        self._pruned_since_last_query += 1
        logger.debug(
            "Pruned WLM-abandoned attempt (query_id=%s) before merging new context (worker '%s')",
            last_qid,
            self.worker_name,
        )

    def _emit_frame(self, frame: Any) -> None:
        """Hand *frame* to the supervisor sink, when one is installed.

        ``frame_sink`` is the runtime -> supervisor seam (S4a): the in-process
        worker thread publishes ``worker_state`` frames through the same
        callable a process runtime will write to its socket, so the supervisor
        consumes one code path in both modes. ``None`` (the production default
        until the supervisor wires a sink) makes this a no-op.

        It never raises: a broken sink must not take the worker down, so the
        failure is logged and swallowed. That guarantee covers *this* call
        only. The sink runs on the worker's own thread, so a sink that blocks
        delays the status write that triggered it, and a sink that calls back
        into ``_write_status_file`` re-enters here -- there is deliberately no
        re-entrancy guard, because a guard would silently drop the inner frame
        instead of making the recursion visible.
        """
        sink = self._frame_sink
        if sink is None:
            return
        try:
            sink(frame)
        except Exception as exc:
            log("WARNING", "tools.worker", f"frame_sink failed: {exc}")

    def _write_status_file(self) -> None:
        """
        Write runtime status to ``status.json`` so the web API backend
        (which runs in a separate process) can read it.

        This file is read by ``GET /api/workspace/{ws_id}/workers``
        to populate ``runtime_status``, ``current_task``,
        ``last_heartbeat``, ``error``, ``started_at``,
        ``last_query_at`` and ``paused_manually`` for each worker.
        """
        data = {
            "runtime_status": self.status,
            "current_task": self.current_task,
            "last_heartbeat": self.last_heartbeat,
            "error": self.error,
            "session_id": self.session_id,
            "current_context_tokens": self.get_current_context_tokens(),
            "max_context_tokens": self.max_context_tokens,
            "started_at": self.started_at,
            "last_query_at": self.last_query_at,
            "paused_manually": bool(self._manual_only_pause),
        }
        target = self._worker_dir / "status.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path_str = tempfile.mkstemp(
            dir=str(target.parent),
            prefix=".status_",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
            if FileLock is not None:
                with FileLock(str(target)):
                    os.replace(tmp_path_str, str(target))
            else:
                os.replace(tmp_path_str, str(target))
            # S4a: publish the state frame only after status.json is durably
            # in place and the FileLock is released (see _emit_frame for why
            # the lock must not be held across the sink). The except branch
            # deliberately emits nothing: a status write that failed has no
            # state worth announcing, and its logger.error is the evidence.
            if self._frame_sink is not None:
                self._emit_frame(
                    WorkerState(
                        status=data["runtime_status"],
                        current_task=data["current_task"],
                        error=data["error"],
                        paused_manually=data["paused_manually"],
                    )
                )
        except (OSError, Exception) as exc:
            logger.error("Failed to write worker status file: %s", exc)
            try:
                if os.path.exists(tmp_path_str):
                    os.unlink(tmp_path_str)
            except OSError:
                pass

    def _save_context(self) -> None:
        """
        Persist WorkerContext + runtime status to disk atomically.

        Uses WorkerContext.to_persistable_dict() for the conversation
        data, augmented with runtime fields (status, error, heartbeat).
        Also writes a lightweight ``status.json`` consumed by the
        web API backend.
        """
        if self._worker_ctx is None:
            self._write_status_file()
            return
        target = self._context_path()
        # F2: generation guard — a stale writer (an older WorkerThread instance
        # still alive after a force-spawn) must not overwrite the replacement
        # thread's context.json.  Skip the save if the file already carries a
        # NEWER generation than this thread's.
        if target.exists():
            try:
                existing = json.loads(target.read_text(encoding="utf-8"))
                existing_gen = int(existing.get("generation") or 0)
                if existing_gen > self._generation:
                    logger.debug(
                        "Stale worker '%s' (generation %d) skipped context save: "
                        "file already has generation %d",
                        self.worker_name, self._generation, existing_gen,
                    )
                    return
            except (json.JSONDecodeError, OSError, TypeError, ValueError):
                pass
        ctx_data = self._worker_ctx.to_persistable_dict()
        data = {
            **ctx_data,
            "status": self.status,
            "error": self.error,
            "current_task": self.current_task,
            "last_heartbeat": self.last_heartbeat,
            "last_query": self._last_query,
            "last_completed_query": self._last_completed_query,
            "last_query_id": getattr(self, "_last_query_id", None),
            "pruned_since_last_query": getattr(self, "_pruned_since_last_query", 0),
            "generation": self._generation,
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file in the same directory (atomic on same filesystem)
        fd, tmp_path_str = tempfile.mkstemp(
            dir=str(target.parent),
            prefix=".context_",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
            if FileLock is not None:
                with FileLock(str(target)):
                    os.replace(tmp_path_str, str(target))
            else:
                os.replace(tmp_path_str, str(target))
        except (OSError, Exception) as exc:
            logger.error("Failed to save worker context: %s", exc)
            # Clean up the temp file if the write failed
            try:
                if os.path.exists(tmp_path_str):
                    os.unlink(tmp_path_str)
            except OSError:
                pass
        self._write_status_file()

    def _publish_event(self, event_type: str, data: dict) -> None:
        """
        Publish a typed event to the worker's own EventBus for real-time forwarding
        to WebSocket clients via the bridge subscriber.

        Auto-injects ``worker_name`` from ``self.worker_name`` so that call sites
        don't need to include it — many events (WorkerSpawnedEvent,
        WorkerMessageEvent, AssistantMessageEvent, etc.) validate that
        ``worker_name`` is present in the data dict.
        """
        if "worker_name" not in data:
            data = {**data, "worker_name": self.worker_name}
        if self._event_bus is None or create_event is None or EventType is None:
            return

        try:
            if isinstance(event_type, str):
                try:
                    resolved_type = EventType(event_type)
                except ValueError:
                    log('WARNING', 'tools.worker', f"Unknown event type: {event_type}")
                    return

            event = create_event(
                event_type=resolved_type,
                data=data,
                source="worker",
                session_id=self.session_id or self.worker_name,
            )
            self._event_bus.publish(event)
        except Exception as e:
            log('ERROR', 'tools.worker', f"Failed to publish event {event_type}: {e}")

    def _publish_global_event(self, event_type, data: dict) -> None:
        """Publish a lifecycle event to the *global* event bus (best-effort).

        Auto-injects ``session_id`` and ``worker_name`` so call sites can stay
        minimal. Never raises — failures are swallowed by the module helper.
        """
        _publish_global_worker_event(event_type, {
            "session_id": self.session_id or "",
            "worker_name": self.worker_name,
            "instance_id": self.instance_id,
            "instance_label": self.instance_label,
            **data,
        })

    def _heartbeat_tick(self) -> None:
        """Publish a WORKER_HEARTBEAT if the interval has elapsed (no-op otherwise).

        Updates ``last_heartbeat`` in memory (status.json writes remain the
        responsibility of the existing status-file machinery). Called from the
        idle loop, the pause loop, and the busy query path.
        """
        now = time.monotonic()
        if now - self._last_heartbeat_monotonic < HEARTBEAT_INTERVAL_S:
            return
        self._last_heartbeat_monotonic = now
        self.last_heartbeat = datetime.now(timezone.utc).isoformat()
        current_context_tokens = self.get_current_context_tokens()
        self._publish_global_event(EventType.WORKER_HEARTBEAT, {
            "status": self.status,
            "current_task": self.current_task,
            "last_heartbeat": self.last_heartbeat,
            "current_context_tokens": current_context_tokens,
            "max_context_tokens": self.max_context_tokens,
        })
        # S5: the same tick also crosses the frame seam, so a supervisor that
        # cannot see this thread (process mode) learns the worker is alive.
        # ``last_heartbeat`` is the ISO-8601 string the protocol field carries
        # (``Heartbeat.at``); the token counts are the same two sources the
        # global-bus event above already reports.
        self._emit_frame(
            Heartbeat(
                at=self.last_heartbeat,
                status=self.status,
                current_context_tokens=current_context_tokens,
                max_context_tokens=self.max_context_tokens,
            )
        )



# ============================================================================
# _SESSION_MAIN_PAUSED (session-level main-agent pause set)  (moved verbatim from tools.workspace.worker; W4 facade extraction)
# ============================================================================
# Session-level main-agent pause set (UNIT C, pause/resume v2). A session id
# present here means the session's MAIN agent is paused ("Pause Main"); its
# async workers keep running unless they were paused too ("Pause All"). While
# the main agent is paused, join/wait_for_job return {"status": "paused"}
# promptly instead of blocking. Pausing is cooperative only: workers pause at
# their next safe loop boundary via WorkerThread.pause(); no thread is killed.
_SESSION_MAIN_PAUSED: set[str] = set()

# PHASE 2g: the join targets THIS process's proxy actually parked, per session
# id: ``{session_id: {(worker_name, instance_id), ...}}``.  Written only where
# ``_pause_join_target`` returned True and popped (never swept) by
# ``_resume_proxied_join_targets`` when the session main-pause is discarded, so
# the symmetric unpause can only ever touch a worker this proxy paused.  The
# lock guards the dict mutation only -- never held across ``resume()``.
_PROXIED_JOIN_TARGETS: dict[str, set[tuple[str, int]]] = {}
_PROXIED_JOIN_TARGETS_LOCK = threading.Lock()


# ============================================================================
# workspace capabilities (CAPABILITIES_AVAILABLE, resolve_workspace_id, _load_template_workers, _workspace_dir)  (moved verbatim from tools.workspace.worker; W4 facade extraction)
# ============================================================================
# Optional dependencies
# ---------------------------------------------------------------------------
try:
    from thoughtmachine.workspace_capabilities import (
        resolve_workspace_id,
        _load_template_workers,
        _workspace_dir,
    )
    CAPABILITIES_AVAILABLE = True
except ImportError:
    CAPABILITIES_AVAILABLE = False
    resolve_workspace_id = None
    _workspace_dir = None


def _same_workspace_root(left: Any, right: Any) -> bool:
    """True when two workspace-root values name the same directory.

    Used to decide whether a registry-resolved workspace root and this tool's
    injected ``workspace_path`` agree.  Values that are not path-like (test
    doubles, MagicMocks) are reported as "same", so callers keep their
    previous behaviour instead of being dragged into a comparison they never
    opted into.
    """
    if not isinstance(left, (str, os.PathLike)) or not isinstance(
        right, (str, os.PathLike)
    ):
        return True
    try:
        def _norm(value: Any) -> str:
            return (
                os.path.normcase(os.path.abspath(os.path.expanduser(os.fspath(value))))
                .replace("\\", "/")
                .rstrip("/")
            )

        return _norm(left) == _norm(right)
    except Exception:
        return True


# ============================================================================
# StateBridge / EventProcessor placeholders  (moved verbatim from tools.workspace.worker; W4 facade extraction)
# ============================================================================
# Presenter components for per-worker event processing
StateBridge = None  # lazy-imported in _run_tool_loop

EventProcessor = None  # lazy-imported in _run_tool_loop


# ============================================================================
# security gate (GATE_AVAILABLE, check_required_categories)  (moved verbatim from tools.workspace.worker; W4 facade extraction)
# ============================================================================
# Optional: security gate for worker permission checks
try:
    from security.security_gate import check_required_categories
    GATE_AVAILABLE = True
except ImportError:
    GATE_AVAILABLE = False
    check_required_categories = None  # type: ignore


# ============================================================================
# NullEventBus / _NULL_EVENT_BUS  (moved verbatim from tools.workspace.worker; W4 facade extraction)
# ============================================================================
# NullEventBus — used for worker security prompts where no interactive
# user is available to respond (returns "deny" instantly).
try:
    from agent.events import NullEventBus
    _NULL_EVENT_BUS = NullEventBus()
except ImportError:
    NullEventBus = None  # type: ignore
    _NULL_EVENT_BUS = None


# ============================================================================
# tool registry + shutdown_workers / get_worker_event_bus / get_worker_event_buses_for_session  (moved verbatim from tools.workspace.worker; W4 facade extraction)
# ============================================================================
# Global tool name → class registry (built lazily to avoid circular import
# with tools.__init__ — that module imports this file (Worker) before
# TOOL_CLASSES is fully populated, so we resolve on first use).
def _build_tool_registry() -> dict[str, type["ToolBase"]]:
    from tools import iter_tool_classes
    return {cls.__name__: cls for cls in iter_tool_classes()}

_TOOL_REGISTRY: dict[str, type["ToolBase"]] = {}
def _get_tool_registry() -> dict[str, type["ToolBase"]]:
    if not _TOOL_REGISTRY:
        _TOOL_REGISTRY.update(_build_tool_registry())
    return _TOOL_REGISTRY

# ---------------------------------------------------------------------------
# Module-level worker registry (delegates to WorkerRegistry singleton)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Shutdown helper  (exposed for bridge integration)
# ---------------------------------------------------------------------------

def shutdown_workers(timeout: float = 5.0, *, session_id) -> None:
    """Delegate to WorkerRegistry singleton (backward compat).
    *session_id* is keyword-only and required: the caller must state whose
    workers it is stopping. ``ALL_SESSIONS`` is the process-exit path only.
    """
    _WorkerRegistry.get_instance().shutdown_workers(timeout=timeout, session_id=session_id)

def get_worker_event_bus(session_id: str, worker_name: str, instance_id: int = 1) -> Any:
    """Delegate to WorkerRegistry singleton (backward compat)."""
    return _WorkerRegistry.get_instance().get_event_bus(session_id, worker_name, instance_id=instance_id)


def get_worker_event_buses_for_session(session_id: str) -> Dict[str, Any]:
    """Delegate to WorkerRegistry singleton (backward compat)."""
    return _WorkerRegistry.get_instance().get_event_buses_for_session(session_id)


# ============================================================================
# class Worker (the Worker tool)  (moved verbatim from tools.workspace.worker; W4 facade extraction)
# ============================================================================
# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

#: The worker an action lands on when the caller omits ``worker_name``.
#: ``resources/worker_templates/default.json`` ships a template of this name, so
#: "omitted" always names a worker that exists rather than demanding a name the
#: agent was never told to supply (the schema said ``worker_name`` was optional
#: while ``execute()`` rejected None -- that contradiction cost a turn).
DEFAULT_WORKER_NAME = "default"


class Worker(ToolBase):
    """Manage background or child worker processes.

    When spawning a worker, set ``force=True`` to automatically stop the
    existing instance(s) of the same worker belonging to the **current
    session** before creating a fresh one.  This is useful for recovering
    from a stale or hanging worker of your own.  Force is deliberately
    session-scoped: a worker of the same name owned by another session (and
    therefore by another workspace) is left running untouched.

    Lifecycle authority: ``action='stop'`` DETACHES a worker (recoverable park
    — the thread, its containers and its registration all survive, and queued
    queries are kept); it never terminates anything.  Terminating (``kill``)
    and reviving (``resume``) a worker are operator-only operations exposed in
    the workspace UI, not actions this tool offers the agent.

    ``worker_name`` is genuinely optional: omit it (or leave it blank) and every
    action that needs a worker uses the ``default`` worker.  A spawn task may be
    given either as the top-level ``query`` argument or as ``context['query']``
    — the same task, run on the same blocking dispatch path; passing both is
    only accepted when they are identical, otherwise the call is rejected as
    ambiguous rather than silently dropping one of them.
    """

    tool: str = "Worker"
    # Workers are not gated by session permissions: spawning/running a worker
    # is decoupled from the 'execution' session permission (2026-08-03).
    required_categories: ClassVar[List[str]] = []

    # SECURITY (C68): every Worker action reaches a SESSION-SCOPED resource (a
    # spawn parks a thread, a query drives one, the journal is filed under the
    # owning session).  ToolExecutor therefore REFUSES the call outright when
    # it has no authenticated session id, rather than injecting ``''`` -- which
    # this module degrades to the shared '<no-session>' scope (_NO_SESSION_KEY,
    # :85), i.e. a worker that belongs to nobody.
    requires_authenticated_session: ClassVar[bool] = True

    action: str = Field(
        description="Action: list, spawn, check, query, stop, submit_query, job_status, "
        "join, wait_for_job, pause, reset, inspect. "
        "action='inspect' is a READ-ONLY substrate self-check: it reports, per "
        "worker instance, the runtime mode, whether a child process exists, its "
        "pid, whether it is alive, this process's pid and whether the child is "
        "this very process. It never starts, drives or forks anything. "
        "action='stop' DETACHES the worker: it parks (status 'detached', context "
        "saved, containers and registration kept, queued work preserved) and an "
        "operator can revive it — a stop is therefore never a termination. "
        "Killing and resuming a worker are operator-only (workspace UI), not "
        "agent actions. "
        "When action='spawn' and a task is given -- as the top-level 'query' "
        "argument or as context['query'] -- the spawn call BLOCKS until the "
        "worker finishes the task and returns the full result. The two "
        "spellings are the same task: give both and they must agree, or the "
        "call is rejected as ambiguous. With no task, spawn returns "
        "immediately; use action='query' later. "
        "action='submit_query' enqueues a query and returns immediately with a job_id "
        "(non-blocking); poll progress with action='job_status' (worker_query = job id, "
        "or empty to list this worker's jobs)."
    )

    worker_name: Optional[str] = Field(
        default=None,
        description=(
            "Name of the worker, as listed in workers.json. Optional: when "
            f"omitted (or blank) every action that needs a worker uses the "
            f"'{DEFAULT_WORKER_NAME}' worker. "
            "Use action='list' to see the available names."
        ),
    )

    worker_query: Optional[str] = Field(
        default=None,
        description="Query string to send to an already-spawned worker. "
        "Only valid with action='query' (the call BLOCKS until the worker responds), "
        "action='submit_query' (non-blocking; returns a job handle immediately), "
        "or action='job_status' (job id to look up; empty lists jobs for the worker). "
        "On 'query'/'submit_query' any 'context' you also pass is prepended to "
        "this string; worker_query stays authoritative.",
    )

    query: Optional[str] = Field(
        default=None,
        description="The task for action='spawn' (e.g. query='Review src/'). "
        "The spawn call BLOCKS until the worker finishes and returns the "
        "worker's result. This is the same task as context['query'] -- "
        "passing both is only accepted when they are identical, otherwise the "
        "call is rejected as ambiguous. Only valid with action='spawn'; for "
        "an already-spawned worker use worker_query.",
    )

    context: Optional[Union[Dict, str]] = Field(
        default=None,
        description="Optional context dict or briefing string. "
        "Valid on action='spawn', 'query' and 'submit_query'; on any other "
        "action it is ignored (and the response says so via 'context_ignored'). "
        "On 'spawn': if this dict includes a 'query' key (e.g., {'query': "
        "'Review src/'}), the worker executes that task immediately and the "
        "spawn call BLOCKS until the worker finishes. The 'query' value becomes "
        "the worker's first instruction. Other keys (e.g., config) are passed "
        "as metadata. You may also pass a plain string, which will be treated "
        "as the query. "
        "On 'query'/'submit_query' the context is PREPENDED to worker_query as a "
        "labelled 'Follow-up context for this query:' block, so a follow-up "
        "keeps the briefing channel. worker_query is authoritative there: a "
        "'query' key inside the context is dropped (the response notes it via "
        "'context_note'), and a plain string is used verbatim as briefing text.",
    )

    timeout_seconds: Optional[int] = Field(
        default=None,
        description="Override the worker's default timeout in seconds. "
                      "If not set, the worker definition's timeout is used.",
    )

    # SECURITY (C68): an AUTHORITY FACT, not an argument. ``SkipJsonSchema``
    # keeps it out of every generated JSON schema (the model must not be told
    # it may supply a session id); ToolExecutor pops any caller-supplied value
    # before validation and injects its own authenticated id.
    session_id: SkipJsonSchema[Optional[str]] = Field(
        default=None,
        exclude=True,
        description="Session ID that spawned this worker (injected by ToolExecutor)",
    )

    worktree_root: Optional[str] = Field(
        default=None,
        description="Optional narrower workspace root for path-anchored tool "
        "operations (injected by ToolExecutor when the session is bound to one).",
    )

    instance_id: Optional[int] = Field(
        default=None,
        description="Worker instance ID to address. When omitted, actions that "
        "target a worker by name operate on the single live instance and error "
        "with an ambiguity message if the name maps to multiple live instances. "
        "Spawn with an explicit instance_id to create/replace that instance; "
        "without it a fresh instance id is allocated (1, 2, ...).",
    )

    force: bool = Field(
        default=False,
        description=(
            "If True, stop this session's existing instance(s) of the worker "
            "before spawning a fresh one. Workers of the same name in other "
            "sessions/workspaces are not touched."
        ),
    )

    purpose: Optional[str] = Field(
        default=None,
        description="Purpose hint for the worker: 'coding', 'reviewing', 'researching', 'opinion', etc."
    )
    style: Optional[Literal["precise", "exploratory"]] = Field(
        default="precise",
        description="Worker behaviour: 'precise' = focused, minimal output; 'exploratory' = thorough, may digress"
    )
    meta: Optional[dict] = Field(
        default=None,
        description="General-purpose metadata dict forwarded to worker context"
    )

    skip_output_truncation: ClassVar[bool] = True

    # Termination ("kill") and revival ("resume") are deliberately NOT here:
    # both are operator-only operations (web UI / workspace HTTP routes), never
    # agent actions.  The agent's "stop" DETACHES (recoverable park).
    VALID_ACTIONS: ClassVar[list[str]] = ["list", "spawn", "check", "query", "stop", "submit_query", "job_status", "join", "wait_for_job", "pause", "reset", "inspect"]

    #: Actions that cannot do anything without naming a worker.  Every other
    #: action keeps ``worker_name=None``: for pause/stop that None is
    #: MEANINGFUL (session/main scope), not a missing argument -- defaulting
    #: them to a worker name would silently redirect a main-agent pause onto
    #: one worker.
    ACTIONS_REQUIRING_WORKER: ClassVar[tuple[str, ...]] = (
        "spawn", "check", "query", "submit_query", "join", "wait_for_job", "reset",
    )

    #: Actions that DO carry a ``context`` payload.  Every other action ignores
    #: one (reported as ``context_ignored``), because the field used to accept a
    #: context on every action and silently drop it on all but spawn.
    CONTEXT_ACTIONS: ClassVar[tuple[str, ...]] = ("spawn", "query", "submit_query")

    #: ``context`` exactly as the caller supplied it.  ``coerce_context`` below
    #: rewrites a plain string to ``{"query": <str>}``, which erases the
    #: difference between "raw briefing text" and a genuine dict carrying a
    #: 'query' key; the follow-up renderer needs that difference.
    _raw_context: Any = PrivateAttr(default=None)

    @model_validator(mode='wrap')
    @classmethod
    def _capture_raw_context(cls, data: Any, handler: Any) -> Any:
        """Keep the pre-coercion ``context`` so a string stays distinguishable."""
        raw = data.get('context') if isinstance(data, dict) else None
        obj = handler(data)
        if isinstance(raw, str):
            obj._raw_context = raw
        return obj

    # ── Pydantic v2 validator: coerce plain string context to {"query": ...} ──
    @field_validator('context', mode='before')
    @classmethod
    def coerce_context(cls, v: Any) -> Any:
        """If the LLM passes a plain string as context, wrap it as a query dict."""
        if isinstance(v, str):
            return {"query": v}
        return v

    @field_validator('worker_name', mode='before')
    @classmethod
    def blank_worker_name_means_omitted(cls, v: Any) -> Any:
        """A blank/whitespace name is OMITTED, never a worker called ``''``."""
        if isinstance(v, str) and not v.strip():
            return None
        return v

    # ------------------------------------------------------------------
    def execute(self) -> str:
        try:
            if self.action not in self.VALID_ACTIONS:
                if self.action in ("kill", "resume"):
                    # These exist, but only for an operator.  Teach the agent
                    # instead of reporting a bare "unknown action".
                    return json.dumps({
                        "error": (
                            f"Action '{self.action}' is operator-only: agents "
                            f"cannot {self.action} workers, and nothing was "
                            f"{('killed' if self.action == 'kill' else 'resumed')}. "
                            "Operators do this from the workspace UI. "
                            "To park a worker, use action='stop': it DETACHES "
                            "(status 'detached', context saved, containers and "
                            "registration kept, queued work preserved) and an "
                            "operator can revive it later, so nothing is lost."
                        ),
                        "available_actions": self.VALID_ACTIONS,
                    })
                return json.dumps({
                    "error": f"Unknown action: {self.action}",
                    "available_actions": self.VALID_ACTIONS,
                })

            # An omitted worker_name is not an error: every action that NEEDS a
            # worker lands on DEFAULT_WORKER_NAME.  pause/resume/stop (and
            # 'list', 'job_status') keep None, because there None means
            # session/main scope rather than a missing argument.
            if self.action in self.ACTIONS_REQUIRING_WORKER and self.worker_name is None:
                self.worker_name = DEFAULT_WORKER_NAME

            # A top-level 'query' is the spawn task, folded into the context so
            # both spellings run the ONE blocking dispatch path below.
            try:
                self._fold_query_into_context()
            except ValueError as exc:
                return json.dumps({"error": str(exc), "action": self.action})

            # === Resolve the workspace id the worker must inherit ===
            # The parent's own workspace_path wins over SessionRegistry; see
            # _resolve_worker_workspace_id() for why (a stale registry entry
            # cross-wires the worker onto another, possibly dead, workspace).
            ws_id = self._resolve_worker_workspace_id()

            workers = self._load_workers(ws_id)

            # ── ONE CLOCK (defect #5): resolve the caller's budget here ──
            # This is the earliest dispatch point.  ``strict=True`` means a
            # request above DECLARED_CEILING FAILS AT DISPATCH: no spawn, no
            # queue, no sleep -- a refusal envelope naming the request, the
            # ceiling and the layer that owns it.  Nothing on this path is
            # silently clamped any more.
            self._dispatch_budget = None
            if self.action in ("spawn", "query", "join", "wait_for_job"):
                try:
                    self._dispatch_budget = TimeBudget.resolve(
                        self.timeout_seconds,
                        default=DEFAULT_REQUEST_SECONDS,
                        cap=DECLARED_CEILING,
                        strict=True,
                        source=f"worker_thread.{self.action}",
                    )
                except TimeBudgetExceeded as exc:
                    return json.dumps(
                        {
                            "error": str(exc),
                            "message": str(exc),
                            "status": "rejected",
                            "action": self.action,
                            "worker_name": self.worker_name,
                            "requested_timeout_seconds": exc.requested,
                            "declared_ceiling_seconds": exc.ceiling,
                            "timeout_ceiling_owner": exc.owner,
                            "timeout_clamped": False,
                            "timeout_source": exc.source,
                        },
                        indent=2,
                        default=str,
                    )

            handler = {
                "list": lambda: self._action_list(workers),
                "spawn": lambda: self._action_spawn(workers, ws_id),
                "check": lambda: self._action_check(workers),
                "query": lambda: self._action_query(workers),
                "stop": lambda: self._action_stop(workers),
                "submit_query": lambda: self._action_submit_query(workers),
                "job_status": lambda: self._action_job_status(workers),
                "join": lambda: self._action_join(workers),
                "wait_for_job": lambda: self._action_join(workers),
                "pause": lambda: self._action_pause(workers),
                "reset": lambda: self._action_reset(workers),
                "inspect": lambda: self._action_inspect(workers),
            }[self.action]

            result = handler()
            # Every envelope a timed action returns carries the ONE clock it
            # was given (requested / effective / clamped / clamped_by /
            # source) as flat keys -- never buried in a string.
            if isinstance(result, dict) and self._dispatch_budget is not None:
                result = {**result, **self._dispatch_budget.as_envelope_fields()}
            if self.action not in self.CONTEXT_ACTIONS and self.context is not None:
                # A context this action cannot carry must be dropped VISIBLY.
                result["context_ignored"] = (
                    "context is only used by spawn/query/submit_query"
                )
            return json.dumps(result, indent=2, default=str)
        except Exception as exc:
            logger.exception("Worker failed")
            error_envelope = {"error": str(exc), "action": self.action}
            if self.action not in self.CONTEXT_ACTIONS and self.context is not None:
                # Even a failed action must not drop a context invisibly.
                error_envelope["context_ignored"] = (
                    "context is only used by spawn/query/submit_query"
                )
            return json.dumps(error_envelope, indent=2)

    # -- helpers -----------------------------------------------------

    def _fold_query_into_context(self) -> None:
        """Fold the top-level ``query`` argument into ``context['query']``.

        ``context={'query': ...}`` is the long-standing spelling and the only
        one with a dispatch behind it (``_action_spawn`` copies ``context`` into
        ``thread._initial_context`` and then BLOCKS on the worker's first reply
        because that dict has a 'query' key).  ``query=`` exists because it is
        the shape an agent reaches for, so it is merged INTO the context instead
        of growing a second dispatch path that could drift from the first.

        Both precedence rules are explicit:

        * top-level ``query`` + a context WITHOUT a 'query' key -> the
          top-level value becomes the task;
        * both present and DIFFERENT -> ``ValueError`` naming both, because
          silently picking one drops work the caller asked for;
        * both present and equal -> accepted once (the context already carries
          exactly that task, so nothing is duplicated or dispatched twice).
        """
        if self.query is None or not self.query.strip():
            return
        task = self.query
        if self.action != "spawn":
            raise ValueError(
                f"'query' is only valid with action='spawn'; for "
                f"action='{self.action}' the task belongs in 'worker_query'. "
                "Nothing was dispatched."
            )
        if not isinstance(self.context, dict):
            self.context = {"query": task}
            return
        existing = self.context.get("query")
        if existing is None or (isinstance(existing, str) and not existing.strip()):
            merged = dict(self.context)
            merged["query"] = task
            self.context = merged
            return
        if str(existing) != task:
            raise ValueError(
                f"Conflicting tasks: query={task!r} but "
                f"context['query']={existing!r}. They differ, so this call was "
                "rejected rather than dispatching one of them silently -- pass "
                "the task once, either as query= or as context={'query': ...}."
            )

    def _followup_context_block(self) -> tuple[Optional[str], Optional[str]]:
        """Render ``context`` for a FOLLOW-UP query (``query``/``submit_query``).

        Returns ``(block, note)``.  ``block`` is the labelled text to PREPEND to
        ``worker_query`` -- ``None`` when the caller supplied no usable context.
        ``note`` is what the envelope should say about anything that was
        dropped.

        ``worker_query`` is authoritative on a follow-up, so a ``query`` key
        inside a context dict is DROPPED here and reported in ``note``; it is
        never merged and can never override the query being sent.

        A plain string is raw BRIEFING text, not a task.  ``coerce_context``
        turned it into ``{"query": <str>}``, which looks exactly like a genuine
        dict, so the pre-coercion value (``_raw_context``) is used instead.
        """
        raw = self._raw_context
        if isinstance(raw, str):
            text = raw.strip()
            return (
                f"{FOLLOWUP_CONTEXT_PREFIX}\n{text}" if text else None
            ), None

        context = self.context
        if context is None:
            return None, None
        if isinstance(context, str):
            text = context.strip()
            return (
                f"{FOLLOWUP_CONTEXT_PREFIX}\n{text}" if text else None
            ), None
        if not isinstance(context, dict):
            text = str(context).strip()
            return (
                f"{FOLLOWUP_CONTEXT_PREFIX}\n{text}" if text else None
            ), None

        payload = dict(context)
        note = None
        if "query" in payload:
            dropped = payload.pop("query")
            note = (
                "context['query'] ignored; worker_query wins "
                f"(context['query']={dropped!r})"
            )
        if not payload:
            return None, note
        rendered = json.dumps(
            payload, default=str, ensure_ascii=True, sort_keys=True
        )
        return f"{FOLLOWUP_CONTEXT_PREFIX}\n{rendered}", note

    def _compose_followup_query(self) -> tuple[str, Optional[str]]:
        """``worker_query`` with any follow-up context PREPENDED to it.

        One string, on purpose: every delivery path (direct ``send_query``, the
        detached FIFO ``enqueue_query``, the paused-resume dispatch) carries a
        single string, so one composition serves all three.  With no context
        supplied the result is ``worker_query`` byte-for-byte.
        """
        block, note = self._followup_context_block()
        if not block:
            return self.worker_query, note
        return f"{block}\n\n{self.worker_query}", note

    def _load_workers(self, ws_id: Optional[str]) -> list:
        """
        Load workers list from workers.json in workspace dir.

        Falls back to scanning workspace directories or ``_load_template_workers()``
        when the file is missing, empty, fails to parse, or when the workspace ID
        cannot be resolved.

        Template workers are **merged** into the result — any worker template found
        on disk (in ``resources/worker_templates/``) that is not already present in
        the workspace's ``workers.json`` is appended automatically.  This ensures
        newly added templates (e.g. ``default.json``) are always
        available without requiring workspace re-initialisation.
        """
        from tools.workspace.worker import _workspace_dir
        if not CAPABILITIES_AVAILABLE or not _workspace_dir:
            return []

        if not ws_id:
            scanned = self._scan_workspace_dirs_for_workers()
            if scanned:
                return self._merge_template_workers(scanned)
            logger.info("No workspace ID resolved and no workers.json found in any workspace, falling back to template workers")
            return _load_template_workers()

        workers_path = _workspace_dir(ws_id) / "workers.json"
        if not workers_path.exists():
            logger.info(f"workers.json not found at {workers_path}, falling back to templates")
            return _load_template_workers()

        try:
            data = json.loads(workers_path.read_text(encoding="utf-8"))
            if not data:  # empty array
                logger.info(f"workers.json is empty, falling back to templates")
                return _load_template_workers()
            return self._merge_template_workers(data)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load workers.json: {e}, falling back to templates")
            return _load_template_workers()

    @staticmethod
    def _merge_template_workers(workers: list, *, include_bundled: bool = True) -> list:
        """
        Merge template workers from disk into *workers* (in-place of the list
        object, but returned for chaining).

        Any worker definition found in ``_load_template_workers()`` whose
        ``name`` is not already present in *workers* is appended.  This lets
        newly added template files (e.g. ``default.json``)
        become available without requiring workspace re-initialisation.

        Workers from the workspace file take precedence — existing names are
        never overwritten.
        """
        existing_names: set[str] = {
            w["name"] for w in workers
            if isinstance(w, dict) and w.get("name")
        }
        for template in _load_template_workers(include_bundled=include_bundled):
            name = template.get("name")
            if name and name not in existing_names:
                workers.append(template)
                existing_names.add(name)
                logger.info("Merged template worker '%s' into workspace workers", name)
        return workers

    def _scan_workspace_dirs_for_workers(self) -> list:
        """
        Scan ``~/.thoughtmachine/workspaces/<id>/workers.json`` for all workspace
        directories and return the first valid workers list found.

        This is a fallback when ``ws_id`` cannot be resolved (e.g. when the
        session's ``workspace_path`` config is not set).
        """
        import os

        base = Path(os.path.expanduser("~/.thoughtmachine/workspaces"))
        if not base.is_dir():
            return []

        for entry in sorted(base.iterdir()):
            if not entry.is_dir():
                continue
            workers_path = entry / "workers.json"
            if not workers_path.is_file():
                continue
            try:
                data = json.loads(workers_path.read_text(encoding="utf-8"))
                if data and isinstance(data, list):
                    logger.info(
                        "Loaded %d workers from %s (workspace fallback scan)",
                        len(data), workers_path,
                    )
                    return data
            except (json.JSONDecodeError, OSError):
                continue

        return []

    def _find_worker(self, workers: list, name: str) -> Optional[dict]:
        """Find a worker by name in the workers list."""
        for w in workers:
            if isinstance(w, dict) and w.get("name") == name:
                return w
        return None

    # ── workspace identity resolution ──────────────────────────────────
    #
    # A spawned worker is gated by ITS OWN ToolExecutor, against ITS OWN
    # workspace id.  If that id names a different (e.g. deleted) workspace than
    # the one this tool is running in, every permission category fails closed
    # and the worker can do nothing at all — the field symptom is
    # ``filesystem:banned`` inside a worker whose parent session has full
    # filesystem permission.
    #
    # Two independent sources can disagree:
    #
    #   1. ``self.workspace_path`` — the path ToolExecutor injected into this
    #      call, which is exactly the path the PARENT's permission gate
    #      resolved its own workspace id from.  Authoritative by construction.
    #   2. ``SessionRegistry`` — the ``workspace_id`` recorded against
    #      ``self.session_id``.  This is the source that goes stale: the
    #      registry is rebuilt from disk by scanning the legacy ``sessions/``
    #      dir *and* every ``workspaces/*/sessions/`` dir, and a session file
    #      found under more than one workspace dir is read "first found wins",
    #      stamped with whichever ``workspaces/<id>`` directory the scan
    #      reached first.  A moved/duplicated workspace, or a rebind that left
    #      the old record behind, therefore resolves a session to the wrong
    #      (possibly deleted) workspace.
    #
    # ``resolve_workspace_id()`` carries a second, milder aliasing hazard:
    # several workspace ``config.json`` files can normalise to the same root
    # and it then returns the alphabetically first match.  Both hazards are why
    # the path-derived id wins below and why a divergence is logged loudly
    # instead of being papered over.

    def _workspace_id_from_parent_path(self) -> Optional[str]:
        """Resolve the workspace id from this tool's injected ``workspace_path``.

        Uses the *facade* binding (``tools.workspace.worker``) rather than this
        module's own, so that tests patching
        ``tools.workspace.worker.resolve_workspace_id`` stay effective.
        """
        workspace_path = getattr(self, "workspace_path", None)
        if not isinstance(workspace_path, str) or not workspace_path:
            return None
        if not CAPABILITIES_AVAILABLE:
            return None
        try:
            from tools.workspace.worker import resolve_workspace_id

            return resolve_workspace_id(workspace_path) or None
        except Exception:
            logger.debug(
                "Worker tool could not resolve a workspace id from its injected "
                "workspace_path",
                exc_info=True,
            )
            return None

    def _workspace_id_from_registry(self) -> Optional[str]:
        """Return the ``workspace_id`` SessionRegistry records for this session.

        Best-effort: any registry failure (missing vault, unreadable state,
        unknown session) yields ``None`` so the path-derived id can decide.
        """
        if not self.session_id:
            return None
        try:
            from session.session_registry import SessionRegistry

            session_info = SessionRegistry.get_default().get(self.session_id)
        except Exception:
            return None
        if not session_info:
            return None
        return session_info.get("workspace_id") or None

    def _resolve_worker_workspace_id(self) -> Optional[str]:
        """Return the workspace id a spawned worker must inherit.

        Precedence: the id derived from this tool's own injected
        ``workspace_path`` (the workspace the parent session was actually gated
        against) beats the id recorded in ``SessionRegistry``.  The registry
        remains the source when no path is injected, and the last resort when
        the path cannot be mapped to a workspace at all.

        A divergence is logged as a WARNING naming both ids: it means one of
        the two sources is stale (see the block comment above
        ``_workspace_id_from_parent_path``).  The worker deliberately inherits
        the PARENT's id — inheriting the registry's id instead is what produced
        workers that resolve against a dead workspace and fail every permission
        category closed (``filesystem:banned``).
        """
        path_id = self._workspace_id_from_parent_path()
        registry_id = self._workspace_id_from_registry()

        # Prefer the workspace whose on-disk sidecar matches this session.
        # Reason: _workspace_id_from_parent_path() is a reverse-map from a
        # persisted workspace_path; after a project rename (EE ->
        # ThoughtMachine-dev) that cached path can be stale. Registry can be
        # stale too (older sessions pre-date it). Sidecar presence is the only
        # ground truth that says "this session is actually provisioned here".
        # The path is built from ``_workspace_dir`` -- the one authority for
        # ``<vault>/workspaces/<id>`` -- so the probe honours
        # ``THOUGHTMACHINE_VAULT_ROOT`` like every other vault path.
        chosen: Optional[str] = None
        try:
            _sid = self.session_id or ""

            def _has_sidecar(wid: str) -> bool:
                if not wid or not _sid or _workspace_dir is None:
                    return False
                return (
                    _workspace_dir(wid) / "sessions" / _sid / "permissions.json"
                ).exists()

            _path_ok = _has_sidecar(path_id)
            _reg_ok = _has_sidecar(registry_id)
            if _path_ok and not _reg_ok:
                chosen = path_id
            elif _reg_ok and not _path_ok:
                chosen = registry_id
        except Exception:
            # Never let a sidecar probe break resolution — fall through.
            logger.debug(
                "worker workspace sidecar probe failed; falling back to the "
                "path-derived id",
                exc_info=True,
            )
            chosen = None

        if chosen is None:
            # Inconclusive (both candidates provisioned, or neither, or the
            # probe failed): the path-derived id is the directory this session
            # is ACTUALLY running in, while the registry record is a persisted
            # mapping that can point at a workspace that has since been
            # moved/deleted -- that is the exact cross-wiring this method
            # exists to prevent. `path_id` falls back to the registry only
            # when the path-derived id is unavailable.
            chosen = path_id or registry_id

        if path_id and registry_id and path_id != registry_id:
            logger.warning(
                "Worker tool workspace cross-wiring: session %s is recorded in "
                "SessionRegistry under workspace %r, but the workspace_path it "
                "is actually running in resolves to %r; choosing %r. The worker "
                "inherits THIS session's workspace, so a stale registry entry "
                "(duplicate session records across workspace dirs resolve "
                "'first found wins') cannot point it at a dead workspace.",
                self.session_id,
                registry_id,
                path_id,
                chosen,
            )

        return chosen


    def _resolve_ws_dir(self, ws_id: str) -> Optional[Path]:
        """Resolve workspace directory from workspace ID."""
        from tools.workspace.worker import _workspace_dir
        if not CAPABILITIES_AVAILABLE or not _workspace_dir:
            return None
        return _workspace_dir(ws_id)

    def _build_agent_config(self) -> dict:
        """
        Build an agent config dict from the tool's injected agent_config.

        Returns a dict with provider, model, api_key, base_url, temperature
        and any other fields present in ``self.agent_config`` (injected by
        ToolExecutor).  This dict is passed to WorkerThread so it can create
        an ``AgentConfig`` at runtime.

        Also injects ``workspace_path`` from the parent tool's workspace_path
        so that the worker's AgentConfig has it set, which in turn lets the
        worker's ToolExecutor propagate it to tool calls.
        """
        cfg = self.agent_config or {}
        result = dict(cfg)

        # The worker's own ToolExecutor gates EVERY tool call against whatever
        # workspace this path resolves to, so it must be THIS session's
        # workspace and never a stale registry entry (see the workspace-identity
        # block above).  ``_resolve_registry_workspace()`` prefers
        # SessionRegistry -> WorkspaceRegistry and only falls back to the
        # injected ``workspace_path``; here the injected path wins instead,
        # because that is the value the parent's own gate already resolved
        # against.  Passing the registry's answer unconditionally is its own
        # cross-wiring path: the worker would re-gate against a foreign (or
        # deleted) workspace and fail every category closed.
        parent_ws = getattr(self, 'workspace_path', None)
        registry_ws = self._resolve_registry_workspace()
        ws_path = registry_ws or parent_ws
        if (
            isinstance(parent_ws, (str, os.PathLike))
            and isinstance(registry_ws, (str, os.PathLike))
            and not _same_workspace_root(parent_ws, registry_ws)
        ):
            logger.warning(
                "Worker tool building a worker config: the registry workspace "
                "root %r differs from this session's workspace_path %r. "
                "Passing the parent's path so the worker is not gated against "
                "a foreign workspace.",
                registry_ws,
                parent_ws,
            )
            ws_path = parent_ws
        if ws_path:
            result["workspace_path"] = ws_path

        # Inheritance is deliberately ABSENT: worktree_root is forwarded only
        # when THIS tool instance was itself given one, never copied from the
        # parent agent's config.  A sub-worker must therefore be opted in
        # explicitly, so it can never be silently dragged into a worktree
        # (writing to a checkout its caller is not looking at) merely because
        # the worker that spawned it happens to be bound there.
        _wt = getattr(self, 'worktree_root', None)
        if _wt:
            result['worktree_root'] = _wt

        # Propagate the workspace-lifecycle-manager feature flag so the
        # WorkerThread's _wlm_flag_enabled() sees it at the top level.
        # ToolExecutor normally injects it already; the nested fallback
        # covers direct callers that pass the legacy ``session_config``
        # shape.
        if "use_workspace_lifecycle_manager" not in result:
            nested_cfg = cfg.get("session_config") or {}
            result["use_workspace_lifecycle_manager"] = bool(
                nested_cfg.get("use_workspace_lifecycle_manager")
            )

        return result

    def _resolve_tool_class(self, tool_name: str) -> Optional[type[ToolBase]]:
        """Resolve a tool name string to its class via _TOOL_REGISTRY.

        Uses ``_get_tool_registry()`` which builds the registry lazily
        on first access to avoid the circular import with ``tools.__init__``.
        """
        cls = _get_tool_registry().get(tool_name)
        if cls is None:
            logger.warning("No known tool class for '%s'", tool_name)
            return None
        return cls

    def _find_all_worker_threads(self, worker_name: str,
                                 session_id: str | None = None) -> list[tuple[str, Any]]:
        """
        Search the ``_worker_registry`` for all entries matching *worker_name*.

        By default the search spans the entire registry, regardless of
        session_id.  When *session_id* is given the lookup is session-scoped.

        Returns a list of ``(session_key, thread)`` tuples.
        """
        registry = _WorkerRegistry.get_instance()
        if session_id is None:
            # Historical call shape (name only) — kept so wrappers and test
            # doubles that replace find_workers_by_name with a one-argument
            # callable keep working on the unscoped path.
            return registry.find_workers_by_name(worker_name)
        return registry.find_workers_by_name(worker_name, session_id=session_id)

    def _effective_max_workers(self) -> int:
        """
        Return this session's worker spawn cap.

        Reads the ``max_workers`` key from the injected ``agent_config``
        (top level first, then nested under ``session_config`` for the legacy
        shape). Invalid values (non-int, unparseable, or <= 0) fall back to
        the safe default ``MAX_WORKERS_PER_SESSION``.
        """
        cfg = self.agent_config or {}
        # Preferred key: ``max_workers_per_session`` (top-level, then nested
        # under ``session_config``), then the legacy ``max_workers`` key.
        raw = cfg.get("max_workers_per_session")
        if raw is None:
            nested_cfg = cfg.get("session_config")
            if isinstance(nested_cfg, dict):
                raw = nested_cfg.get("max_workers_per_session")
        if raw is None:
            raw = cfg.get("max_workers")
        if raw is None:
            nested_cfg = cfg.get("session_config")
            if isinstance(nested_cfg, dict):
                raw = nested_cfg.get("max_workers")
        if raw is None:
            return MAX_WORKERS_PER_SESSION
        try:
            cap = int(raw)
        except (TypeError, ValueError):
            return MAX_WORKERS_PER_SESSION
        if cap <= 0:
            return MAX_WORKERS_PER_SESSION
        return cap

    def _live_workers_in_session(self, session_key: str) -> int:
        """
        Count LIVE worker threads currently registered for ``session_key``.

        Only threads whose ``is_alive()`` is True count toward the spawn cap;
        dead (completed/stopped/errored) registry entries do not.
        """
        from tools.workspace.worker import _registry_lock, _worker_registry
        count = 0
        with _registry_lock:
            for key, thread in _worker_registry.items():
                sid = key[0]
                if sid == session_key and thread.is_alive():
                    count += 1
        return count

    # -- action implementations --------------------------------------

    def _action_list(self, workers: list) -> dict:
        """Return all known worker definitions plus runtime status."""
        from tools.workspace.worker import _registry_lock, _worker_registry
        session_key = self.session_id or _NO_SESSION_KEY
        augmented = []
        for w in workers:
            name = w.get("name", "")
            base = dict(w)
            # Merge runtime status from registry — one entry per live instance.
            with _registry_lock:
                instances = [
                    (key[2] if len(key) >= 3 else 1, thread)
                    for key, thread in _worker_registry.items()
                    if key[0] == session_key and key[1] == name
                ]
            if instances:
                for iid, thread in sorted(instances, key=lambda item: item[0]):
                    entry = dict(base)
                    entry["instance_id"] = iid
                    entry["instance_label"] = (
                        thread.instance_label
                        if hasattr(thread, "instance_label")
                        else _WorkerRegistry.instance_label(name, iid)
                    )
                    entry["runtime_status"] = thread.status
                    entry["current_task"] = thread.current_task
                    entry["last_heartbeat"] = thread.last_heartbeat
                    entry["error"] = thread.error
                    augmented.append(entry)
            else:
                entry = dict(base)
                entry["instance_id"] = None
                entry["instance_label"] = name
                entry["runtime_status"] = "stopped"
                augmented.append(entry)

        return {"workers": augmented, "count": len(augmented)}

    def _action_inspect(self, workers: list) -> dict:
        """Report each worker instance's RUNTIME SUBSTRATE -- read-only.

        A self-check, not a control verb: for every live instance of every
        worker defined here it answers "what is actually running this worker?"
        -- the mode its runtime handle was built in, whether a child process
        exists, that child's pid and liveness, the pid of THIS process, and
        whether the child turns out to be this very process.

        Nothing here starts, drives or forks anything: the per-instance
        answers come from ``WorkerRuntimeHandle.substrate()``, which never
        calls ``_ensure_process_runtime``. A worker with no live handle is
        reported (``has_runtime_handle=False``, no substrate) rather than
        hidden, because "there is no handle here" is itself the finding.
        """
        from tools.workspace.worker import _registry_lock, _worker_registry
        from tools.workspace.worker_runtime import runtime_handle_for

        session_key = self.session_id or _NO_SESSION_KEY
        parent_pid = os.getpid()
        entries = []
        for w in workers:
            name = w.get("name", "")
            with _registry_lock:
                instances = sorted(
                    (key[2] if len(key) >= 3 else 1, thread)
                    for key, thread in _worker_registry.items()
                    if key[0] == session_key and key[1] == name
                )
            if not instances:
                entries.append({
                    "worker_name": name,
                    "instance_id": None,
                    "has_runtime_handle": False,
                    "runtime_mode": None,
                    "child_exists": None,
                    "child_pid": None,
                    "child_alive": None,
                    "parent_pid": parent_pid,
                    "same_process": None,
                })
                continue
            for iid, thread in instances:
                handle = runtime_handle_for(session_key, name, iid)
                entry = {
                    "worker_name": name,
                    "instance_id": iid,
                    "has_runtime_handle": handle is not None,
                }
                if handle is None:
                    entry.update({
                        "runtime_mode": None,
                        "child_exists": None,
                        "child_pid": None,
                        "child_alive": None,
                        "parent_pid": parent_pid,
                        "same_process": None,
                    })
                else:
                    entry.update(handle.substrate())
                entries.append(entry)

        return {
            "workers": entries,
            "count": len(entries),
            "parent_pid": parent_pid,
        }

    def _action_spawn(self, workers: list, ws_id: Optional[str]) -> dict:
        """Spawn a worker thread from its definition in workers.json."""
        # ── Commit 8: dispatcher-side duration instrumentation ──
        # Definition of the "dispatch wall" window: it measures the PARENT
        # session's own wall-clock time spent handling THIS spawn action —
        # from the moment _action_spawn begins (spawn_started_at, taken before
        # any worker lookup, creation, or registry lock) to the moment the
        # response envelope is returned to the caller (dispatch_finished_at).
        # dispatch_started_at marks the actual point at which the worker is
        # started/queried.  These numbers are DISPATCHER-side (this parent
        # process) and are deliberately independent of the worker's own clock
        # (see worker_self_reported) — anti-pattern member 4.
        spawn_started_at = time.time()
        from tools.workspace.worker import GATE_AVAILABLE, WorkerThread, _registry_lock, _worker_registry, check_required_categories, resolve_workspace_id
        definition = self._find_worker(workers, self.worker_name)
        if definition is None:
            return {
                "error": f"Worker '{self.worker_name}' not found in workers.json",
            }

        # ── Force-respawn: restart THIS session's instances only ──
        # Force is a restart of *my* worker, never a global name sweep: a
        # worker of the same name owned by another session (and therefore by
        # another workspace) is not this caller's property and must survive
        # untouched.  Session scoping is what makes force workspace-safe.
        session_key = self.session_id or _NO_SESSION_KEY
        if self.force:
            stale_instances = self._find_all_worker_threads(
                self.worker_name, session_id=session_key
            )
            stopped_info = []
            for sid, thread in stale_instances:
                worker_label = f"{self.worker_name} (session={sid})"
                logger.info("Force-spawn: killing stale worker '%s'", worker_label)
                # Only a thread that is genuinely gone may be unregistered below:
                # popping the entry of a live thread would orphan it (alive, but
                # invisible to every registry lookup, so it can never be stopped,
                # checked or reaped).
                survived_force_stop = False
                try:
                    # PHASE 2.1a: force-respawn REPLACES this instance — it must
                    # terminate. stop() only detaches now (and would leave the
                    # stale thread parked and alive forever).
                    thread.kill()
                    # Robust join with retry (same pattern as the termination
                    # paths in _action_reset / the session cascade)
                    _budget = max(30, thread._timeout_seconds)
                    _elapsed = 0.0
                    while _elapsed < _budget:
                        thread.join(timeout=2.0)
                        _elapsed += 2.0
                        if not thread.is_alive():
                            break
                    if thread.is_alive():
                        survived_force_stop = True
                except Exception as exc:
                    logger.exception("Error killing stale worker '%s': %s", worker_label, exc)
                    # stop() raised: the thread is only recycled if it actually died.
                    survived_force_stop = bool(
                        getattr(thread, "is_alive", lambda: False)()
                    )
                finally:
                    # F3: compact summarized history before persisting so a
                    # force-stop right after SummarizeTool does not persist ~2x.
                    if thread._worker_ctx is not None:
                        thread._worker_ctx.compact_after_summary()
                    thread._save_context()
                    if survived_force_stop:
                        # Leave the entry in place: the instance is NOT recycled
                        # and callers must still be able to see and stop it.
                        logger.error(
                            "Force-spawn: worker '%s' instance %s survived "
                            "force-stop (still alive); its registry entry is "
                            "kept so the instance is not orphaned",
                            self.worker_name,
                            getattr(thread, "instance_id", None),
                        )
                    else:
                        _replaced_handle_keys = []
                        with _registry_lock:
                            for key, t in list(_worker_registry.items()):
                                if (
                                    key[0] == sid
                                    and key[1] == self.worker_name
                                    and t is thread
                                ):
                                    _worker_registry.pop(key, None)
                                    _replaced_handle_keys.append(key)
                        # S7a-3: drop the replaced incarnation's handle too,
                        # outside the registry lock and fenced with ITS epoch, so
                        # this teardown can never unplug the handle of the
                        # instance this spawn is about to create.
                        _unregister_runtime_handles(
                            _replaced_handle_keys,
                            epoch=getattr(thread, "_generation", None),
                        )
                stopped_info.append({"session_id": sid, "status": thread.status})
            if stopped_info:
                logger.info(
                    "Force-spawn: stopped %d stale instance(s) of worker '%s'",
                    len(stopped_info), self.worker_name,
                )

        # Prevent duplicate spawns (session-scoped key, instance-aware).
        # Name-only spawns allocate a fresh instance id (1, 2, ...); the sole
        # exception is a single PAUSED instance, which is resumed so the main
        # agent can seamlessly re-query it without manual stop/resume steps.
        resume_paused_worker = None
        spawn_iid = None
        explicit_iid = self.instance_id
        reuse_target = None  # (thread, instance_id, legacy_message); set when a
                             # live non-paused instance is reused (D2).
        with _registry_lock:
            live_same = []
            for key, t in list(_worker_registry.items()):
                if key[0] == session_key and key[1] == self.worker_name:
                    iid = key[2] if len(key) >= 3 else 1
                    if bool(getattr(t, "is_alive", lambda: True)()):
                        live_same.append((iid, t))
            if explicit_iid is not None:
                match = [t for iid, t in live_same if iid == explicit_iid]
                if match:
                    existing = match[0]
                    # A paused instance is auto-resumed ONLY when it is a
                    # legitimate continue-existing target: force means
                    # "restart", and a thread with a stop already in flight
                    # is dead — resuming either one resurrects a stopped
                    # worker that then reports {"status": "stopped"}.
                    if (
                        existing.status == "paused"
                        and not self.force
                        and not _stop_already_pending(existing)
                    ):
                        if getattr(existing, "_manual_only_pause", False):
                            return {
                                "error": (
                                    f"Worker '{self.worker_name}' instance "
                                    f"{explicit_iid} is paused (manual-only) "
                                    "and will not be auto-resumed by a query; "
                                    "resume it explicitly with the 'resume' action."
                                ),
                                "status": "paused",
                            }
                        # Paused worker — auto-resume and re-route the new query.
                        resume_paused_worker = existing
                    elif self.force or _stop_already_pending(existing):
                        # Force (or a stop already in flight): never reuse or
                        # resume. Route to the double-checked spawn at the SAME
                        # explicit id, which re-checks the registry under the
                        # lock and either spawns there (the old instance is
                        # gone by then) or refuses with an explicit error (it
                        # is still registered and alive).
                        spawn_iid = explicit_iid
                    else:
                        # Continue-existing routing: an explicit instance that
                        # is live and not paused is reused, not rejected. Any
                        # spawn query is delivered to it AFTER the lock (D2).
                        reuse_target = (
                            existing,
                            explicit_iid,
                            (
                                f"Worker '{self.worker_name}' instance "
                                f"{explicit_iid} is already running; reusing "
                                "the existing instance."
                            ),
                        )
                else:
                    spawn_iid = explicit_iid
            else:
                only_live = live_same[0][1] if len(live_same) == 1 else None
                if (
                    only_live is not None
                    and only_live.status == "paused"
                    and not self.force
                    and not _stop_already_pending(only_live)
                ):
                    if getattr(live_same[0][1], "_manual_only_pause", False):
                        return {
                            "error": (
                                f"Worker '{self.worker_name}' is paused "
                                "(manual-only) and will not be auto-resumed by "
                                "a query; resume it explicitly with the "
                                "'resume' action."
                            ),
                            "status": "paused",
                        }
                    resume_paused_worker = live_same[0][1]
                elif only_live is not None and _stop_already_pending(only_live):
                    # A live thread whose stop is already in flight is dead: it
                    # must not be reused (the query would hang or come back as
                    # the stale {"status": "stopped"} envelope). Allocate a
                    # NEW id for the replacement instead of colliding with the
                    # dying entry, which is still registered until it exits.
                    spawn_iid = max([iid for iid, _ in live_same], default=0) + 1
                elif len(live_same) == 1 and not self.force:
                    # Continue-existing routing: a single live (non-paused)
                    # instance is reused instead of spawning a duplicate. Any
                    # spawn query is delivered to it AFTER the lock (D2).
                    reuse_target = (
                        live_same[0][1],
                        live_same[0][0],
                        (
                            f"Worker '{self.worker_name}' is already running; "
                            "reusing the existing instance."
                        ),
                    )
                else:
                    spawn_iid = max([iid for iid, _ in live_same], default=0) + 1

        # ── Reuse deliver-and-block (D2) ──
        # A spawn request colliding with a live, non-paused instance used to
        # silently DROP its query. Now the query is delivered to the existing
        # instance via send_query (private reply queue) and the caller blocks
        # for the reply — same semantics as a query action, without spawning a
        # duplicate. The existing worker is NEVER stopped on timeout: it is a
        # pre-existing live instance, not one we spawned, and it can be
        # queried again later.
        if reuse_target is not None:
            reuse_thread, reuse_iid, reuse_message = reuse_target
            query = (
                self.context.get("query", "")
                if isinstance(self.context, dict)
                else str(self.context or "")
            )
            if not query:
                # No query to deliver — nothing was dropped; keep the legacy
                # reuse message.
                return {
                    "worker_name": self.worker_name,
                    "instance_id": reuse_iid,
                    "instance_label": getattr(reuse_thread, "instance_label", None),
                    "spawned": False,
                    "status": reuse_thread.status,
                    "message": reuse_message,
                }
            wait = _worker_query_wait_timeout(
                getattr(reuse_thread, "_timeout_seconds", None),
                fallback=300.0,
            )
            # ── Commit 8: dispatch window opens — query about to be delivered ──
            dispatch_started_at = time.time()
            try:
                response = reuse_thread.send_query(query, timeout=wait)
            except TimeoutError as exc:
                return {
                    "error": str(exc),
                    "note": (
                        f"Worker '{self.worker_name}' instance {reuse_iid} did "
                        "not respond within the query window; the existing "
                        "instance is still alive and can be queried again via "
                        "action='query'."
                    ),
                    "worker_name": self.worker_name,
                    "instance_id": reuse_iid,
                    "instance_label": getattr(reuse_thread, "instance_label", None),
                    "spawned": False,
                    "status": reuse_thread.status,
                }
            elapsed = reuse_thread._last_elapsed()
            # ── Commit 8: dispatch window closes — envelope about to return ──
            dispatch_finished_at = time.time()
            return {
                "worker_name": self.worker_name,
                "instance_id": reuse_iid,
                "instance_label": getattr(reuse_thread, "instance_label", None),
                "spawned": False,
                "status": reuse_thread.status,
                "response": response,
                "elapsed_seconds": round(elapsed, 1) if elapsed is not None else None,
                # Commit 8: worker's self-measured value, never trusted as the
                # dispatch duration (anti-pattern member 4).  Always present;
                # null when the worker self-reported nothing.
                "worker_self_reported": (
                    {"elapsed_seconds": round(elapsed, 1)}
                    if elapsed is not None else None
                ),
                # Commit 8: dispatcher-side timing (this parent process).
                "spawn_started_at": spawn_started_at,
                "dispatch_started_at": dispatch_started_at,
                "dispatch_finished_at": dispatch_finished_at,
                "dispatch_duration_wall": dispatch_finished_at - dispatch_started_at,
            }

        # Resume first, outside the registry lock (holding it while waiting on
        # the output queue can take minutes).  A thread that refuses to resume
        # has a stop already in flight: it is DEAD, so its output queue must
        # not be replayed — that is exactly how the stale
        # {"status": "stopped"} envelope got handed back to the caller.  Fall
        # through to a fresh spawn instead.
        if (
            resume_paused_worker is not None
            and resume_paused_worker.resume() is False
        ):
            logger.warning(
                "Spawn: paused worker '%s' (session=%s) refused to resume "
                "(stop already in flight); spawning a fresh instance instead",
                self.worker_name, session_key,
            )
            resume_paused_worker = None
            if explicit_iid is not None:
                spawn_iid = explicit_iid
            else:
                spawn_iid = max([iid for iid, _ in live_same], default=0) + 1

        if resume_paused_worker is not None:
            # Send the new query to the worker we just resumed.
            query = (
                self.context.get("query", "")
                if isinstance(self.context, dict)
                else str(self.context or "")
            )
            # Drain stale park markers / None signals left by pause() in the
            # input queue.  If a real query was queued by someone else, keep it — but the
            # NEW spawn query runs FIRST (F4 FIFO): it is enqueued before the
            # stale item, which is re-queued at the tail and processed after.
            stale_query = None
            try:
                while True:
                    item = resume_paused_worker._input_queue.get_nowait()
                    if item is not None and item is not _PARK_SENTINEL:
                        stale_query = item
                        break
            except queue.Empty:
                pass

            # ── Commit 8: dispatch window opens — query about to be delivered ──
            dispatch_started_at = time.time()
            resume_paused_worker._input_queue.put(query)
            if stale_query is not None:
                resume_paused_worker._input_queue.put(stale_query)
            resume_wait = _worker_query_wait_timeout(
                getattr(resume_paused_worker, "_timeout_seconds", None),
                fallback=SPAWN_QUEUE_TIMEOUT,
            )
            try:
                final_result = resume_paused_worker._output_queue.get(timeout=resume_wait)
            except queue.Empty:
                final_result = json.dumps({
                    "error": f"Worker '{self.worker_name}' did not respond within {resume_wait}s",
                    "note": "Worker timed out. The paused worker was resumed but did not produce a response.",
                })
            try:
                parsed = json.loads(final_result) if final_result else {}
            except (json.JSONDecodeError, TypeError):
                parsed = {"response": final_result}
            if not isinstance(parsed, dict):
                parsed = {"response": str(parsed)}
            parsed.setdefault("worker_name", self.worker_name)
            parsed.setdefault("spawned", True)
            parsed.setdefault("instance_id", resume_paused_worker.instance_id)
            parsed.setdefault("instance_label", resume_paused_worker.instance_label)
            elapsed = resume_paused_worker._last_elapsed()
            if elapsed is not None:
                parsed["elapsed_seconds"] = round(elapsed, 1)
            # ── Commit 8: dispatch window closes — envelope about to return ──
            dispatch_finished_at = time.time()
            # Commit 8: self-reported value (never trusted as dispatch duration).
            parsed["worker_self_reported"] = (
                {"elapsed_seconds": round(elapsed, 1)}
                if elapsed is not None else None
            )
            parsed["spawn_started_at"] = spawn_started_at
            parsed["dispatch_started_at"] = dispatch_started_at
            parsed["dispatch_finished_at"] = dispatch_finished_at
            parsed["dispatch_duration_wall"] = dispatch_finished_at - dispatch_started_at
            return parsed

        # ── Session-level spawn cap ──
        # Count LIVE workers already running for this session and refuse the
        # spawn at/above the cap. Force-replacements never reach this point:
        # the force block above already stopped and popped the stale entry.
        max_workers = self._effective_max_workers()
        live_workers = self._live_workers_in_session(session_key)
        if live_workers >= max_workers:
            return {
                "error": (
                    f"Worker spawn limit reached: {live_workers}/{max_workers} "
                    f"workers already running for this session. Stop an existing "
                    f"worker before spawning '{self.worker_name}'."
                ),
                "max_workers": max_workers,
                "live_workers": live_workers,
            }

        # Build agent config for this worker
        agent_config = self._build_agent_config()
        if agent_config is None:
            return {
                "error": "Cannot create worker: AgentConfig unavailable. "
                          "Check that agent_config has provider and model.",
                "worker_name": self.worker_name,
            }

        # Resolve workspace directory for persistence
        ws_dir: Optional[Path] = None
        if ws_id:
            ws_dir = self._resolve_ws_dir(ws_id)
        if ws_dir is None:
            return {
                "error": "Cannot create worker: no workspace directory resolved.",
                "worker_name": self.worker_name,
            }

        # Declared tool names.  An empty list is the implicit "all simplified
        # tools" default; the capability registry below owns that meaning.
        tool_names = list(definition.get("tools") or [])
        worker_perms = definition.get("permission_footprint") or definition.get("worker_permissions", {})
        # ── Fail-closed spawn guard ────────────────────────────────────
        # The worker footprint may only RESTRICT categories the session
        # explicitly exposes.  A footprint that requests a category absent
        # from the session profile is rejected outright: the worker can never
        # grant itself access to a category the session does not expose.
        session_perms = self.effective_permissions or self.session_permissions or {}
        denied_categories = sorted(set(worker_perms) - set(session_perms))
        if denied_categories:
            return {
                "error": (
                    "Cannot create worker: permission footprint requests "
                    "category/categories not exposed by the session: "
                    f"{', '.join(denied_categories)} (fail-closed \u2014 workers "
                    "may only use categories the session explicitly exposes)."
                ),
                "worker_name": self.worker_name,
            }
        # Effective worker profile: session (ceiling) intersected with the
        # worker footprint.  Used for spawn-time tool validation below.
        spawn_effective = _restrictive_merge(session_perms, worker_perms)
        # ── Capability resolution (fail loud, before the thread is built) ──
        # The registry says which tools EXIST for this build; the footprint gate
        # below says which of them may RUN (§7.2).  A name this build cannot
        # ship aborts the spawn here, before any worker object is constructed,
        # so no half-capable worker can ever start.
        from tools.capability_registry import (
            CapabilityRegistry,
            CapabilityResolveError,
        )

        universe = _worker_tool_universe()
        try:
            capability_registry = CapabilityRegistry.resolve(
                [_canonical_worker_tool_name(name) for name in tool_names],
                available_names=tuple(universe),
                lookup=universe,
                blocklist=_WORKER_BLOCKLIST,
                parent_enabled_tools=(self.agent_config or {}).get("enabled_tools"),
                permission_veto=None,
            )
        except CapabilityResolveError as err:
            return {
                "error": (
                    f"Worker '{self.worker_name}' requires tool(s) not "
                    "shippable by this build: "
                    f"{', '.join(err.missing_names)}"
                ),
                "worker_name": self.worker_name,
                "missing_tools": list(err.missing_names),
            }

        # Canonical class map: keyed by class name, so a tool's alias spelling
        # cannot create a second entry for the same class.
        tool_classes: Dict[str, type[ToolBase]] = {
            cls.__name__: cls for cls in capability_registry.resolved.values()
        }
        missing_tools: list[str] = []

        if not tool_names:
            # Implicit "all simplified tools" default: report the privilege,
            # never grant it silently (§4.2).  The spawn envelope is unchanged.
            implicit_granted = _worker_granted_tool_names(capability_registry)
            if implicit_granted:
                logger.warning(
                    "Worker '%s' has an empty tool list (implicit / "
                    "unrestricted): resolved to %d tool(s) from the default "
                    "universe",
                    self.worker_name, len(implicit_granted),
                )

        if tool_names:
            # Blocklist exclusions stay SOFT (the worker spawns without them)
            # and are reported to the operator, never silently dropped.
            for excluded_name in capability_registry.excluded:
                logger.warning(
                    "Worker '%s': tool %s (excluded for worker safety)",
                    self.worker_name, excluded_name,
                )
            for tool_name, cls in capability_registry.resolved.items():
                # Spawn-time footprint validation: check tool categories against
                # the worker's declared permission footprint.
                tool_cats = cls.get_required_categories({})
                if tool_cats and GATE_AVAILABLE and check_required_categories is not None:
                    ok, _err = check_required_categories(
                        required=tool_cats,
                        effective=spawn_effective,
                        tool_name=tool_name,
                        tool_args={},
                        description=(
                            f"Worker '{self.worker_name}' footprint validation"
                            f" for {tool_name}"
                        ),
                        # Footprint already intersected into spawn_effective
                        # via _restrictive_merge — pass None so the gate does
                        # not re-apply the raw footprint on top.
                        permission_footprint=None,
                        is_worker_context=True,
                    )
                    if not ok:
                        missing_tools.append(
                            f"{tool_name} (permission denied by worker footprint)"
                        )
                        tool_classes.pop(tool_name, None)

        # Resolve project root from workspace config so tools get the right
        # workspace_path (the project root, not the worker's internal dir).
        project_root: Optional[str] = None
        if ws_dir:
            config_path = ws_dir / "config.json"
            try:
                config_data = json.loads(config_path.read_text(encoding="utf-8"))
                if isinstance(config_data, dict):
                    project_root = config_data.get("root")
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                project_root = None

        # Compute effective timeout: explicit tool override, otherwise None so
        # WorkerThread.__init__ applies its SESSION-owned resolution chain
        # (definition > agent_config > session_config > WORKER_TIMEOUT_SECONDS).
        # ── ONE CLOCK (defect #5) ──────────────────────────────────────
        # The SESSION-owned chain the thread would apply (definition >
        # agent_config > session_config > WORKER_TIMEOUT_SECONDS) is evaluated
        # here, exactly once, through the declared ceiling -- and the resolved
        # ``effective`` is what the thread is given, so the worker's own agent
        # loop (worker_thread.py:2985 -> AgentConfig.timeout_seconds) times out
        # on the same number the caller waits for.
        _chain_timeout = definition.get("timeout_seconds")
        if _chain_timeout is None:
            _chain_timeout = (agent_config or {}).get("worker_timeout_seconds")
        if _chain_timeout is None:
            _chain_timeout = (
                (agent_config or {}).get("session_config") or {}
            ).get("worker_timeout_seconds")
        _requested_timeout = (
            self.timeout_seconds
            if self.timeout_seconds is not None
            else _chain_timeout
        )
        budget = TimeBudget.resolve(
            _requested_timeout,
            default=WORKER_TIMEOUT_SECONDS,
            cap=DECLARED_CEILING,
            strict=True,
            source="worker_thread.spawn",
        )
        self._dispatch_budget = budget
        effective_timeout = budget.effective

        # Build a ContainerManager bound to this session/workspace so the
        # worker can stop/remove the containers it owns at teardown. Falls
        # back to None when Docker/ContainerManager is unavailable — worker
        # teardown then skips container cleanup instead of crashing (mirrors
        # tools/container_control._make_manager).
        container_manager = None
        if _CM is not None:
            try:
                workspace_id = None
                try:
                    from thoughtmachine.workspace_capabilities import resolve_workspace_id
                    workspace_id = resolve_workspace_id(ws_dir)
                except Exception:
                    pass
                container_manager = _CM(
                    workspace_path=ws_dir,
                    session_id=self.session_id,
                    workspace_id=workspace_id or ws_id or "default",
                    session_permissions=self.effective_permissions or self.session_permissions,
                )
            except Exception:
                container_manager = None

        # Spawn-time snapshot semantics must be enforced by COPY: passing the
        # supervisor's object by reference would leave every worker of a
        # session sharing ONE mutable permissions dict.
        _spawn_permissions = self.effective_permissions or self.session_permissions
        if _spawn_permissions is not None:
            _spawn_permissions = dict(_spawn_permissions)

        # ── Forced spawn starts from a clean slate ──
        # A forced spawn REPLACES the previous incarnation, so it must not
        # rehydrate that incarnation's conversation.  The persisted transcript
        # is removed here, BEFORE the constructor allocates the new generation,
        # so ``_load_context()`` in run() finds nothing and the worker builds a
        # fresh WorkerContext.  (Before this, a forced spawn restored the old
        # transcript, so a freshly spawned worker came up already consuming
        # ~92k of its own token budget.)  The path deliberately mirrors
        # ``WorkerThread.__init__``'s ``self._worker_dir``.  A NON-forced spawn
        # never reaches this block: conversation continuity across spawn/query
        # boundaries stays the default for it.
        # No resolved workspace dir ⇒ nothing to unlink, and building the path
        # must not raise TypeError (only OSError is caught below).
        if self.force and ws_dir is not None:
            _stale_context = (
                ws_dir
                / "workers"
                / _WorkerRegistry.instance_label(self.worker_name, spawn_iid)
                / "context.json"
            )
            try:
                _stale_context.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(
                    "Force-spawn: could not remove the previous conversation "
                    "at %s (%s); the new instance may restore it",
                    _stale_context, exc,
                )

        # Create and start the worker thread
        thread = WorkerThread(
            name=self.worker_name,
            definition=definition,
            agent_config=agent_config,
            workspace_dir=ws_dir,
            tool_classes=tool_classes if tool_classes else None,
            session_permissions=_spawn_permissions,
            project_root=project_root,
            timeout_seconds=effective_timeout,
            session_id=self.session_id,
            workspace_id=ws_id,
            instance_id=spawn_iid,
            container_manager=container_manager,
        )
        # Pin the dispatch-time budget on the thread so later hops (query /
        # join) reuse THIS number instead of re-resolving a different one.
        store_budget_on_thread(thread, budget)

        # S5: close the runtime -> supervisor frame seam for this worker. The
        # handle is built here and never started, so nothing about the thread's
        # own run() changes; it only receives the frames the thread emits
        # (WorkerState / Heartbeat) from this point on -- before thread.start(),
        # because a status write or heartbeat in the first turn is exactly the
        # window where a late sink loses frames. The import is function-local
        # to match this method's existing local-import block (worker_runtime is
        # a supervisor-side module; importing it at module level would put it in
        # the worker body's import graph, which the child process also loads),
        # not because of an import cycle -- worker_runtime imports only the
        # protocol module.
        from tools.workspace.worker_runtime import (
            attach_thread_frame_sink,
            register_runtime_handle,
        )

        _runtime_handle = attach_thread_frame_sink(thread)

        # Store initial context for the thread to pick up in run().  This is a
        # COPY, not an alias: the caller may reuse one context dict across
        # several spawns, so aliasing it and then mutating it in place would
        # cross-contaminate workers and mutate caller-owned state.
        if self.context is not None and isinstance(self.context, dict):
            thread._initial_context = dict(self.context)
        elif isinstance(self.context, str) and self.context:
            # A plain-string context means "the task" per the tool schema; it
            # must become the query, not be silently dropped.
            thread._initial_context = {"query": self.context}
        else:
            thread._initial_context = {}

        # Merge purpose, style, meta into the COPY (never into self.context)
        if self.purpose is not None:
            thread._initial_context["purpose"] = self.purpose
        if self.style is not None:
            thread._initial_context["style"] = self.style
        if self.meta is not None:
            thread._initial_context["meta"] = self.meta

        # Context persistence on spawn is now conditional:
        #   * NON-forced spawn — context.json is preserved. If it exists from a
        #     previous run it is loaded in run() and _initial_context is merged
        #     into the loaded context rather than replacing it, so worker
        #     conversation state persists across spawn/query boundaries.
        #   * FORCED spawn — the previous conversation was already unlinked
        #     above (before the constructor), so run() loads nothing and the
        #     worker starts from an empty conversation plus _initial_context.
        #     force means a genuine clean slate, not a restore.

        # -- Command channel: a stale command.json is left IN PLACE --
        # There is deliberately no spawn-time cleanup here any more.  The
        # TODO that used to sit at this spot ("command.json carries no
        # instance/id fence") described the race the CommandFence closes:
        # ``_generation`` is re-allocated for every spawn, so a command a
        # PREVIOUS incarnation (or another session sharing the directory)
        # wrote can never match the new epoch and is QUARANTINED by
        # ``_poll_command`` on the first poll.  Unlinking it here would
        # destroy the only evidence that a foreign writer addressed this
        # worker -- and could delete a command that is mid-claim.

        # ── Fix 1 (P0.1): Double-checked locking for spawn concurrency ──
        # Re-check under lock before registering to prevent TOCTOU race:
        # another thread may have spawned the same worker between the early
        # check (above) and this registration point.
        with _registry_lock:
            existing = _worker_registry.get((session_key, self.worker_name, spawn_iid))
            if existing is not None and existing.is_alive():
                # Another thread won the race — don't overwrite.
                # With force this is not a lost race but a failed stop: the
                # instance we asked to replace is still registered and alive
                # (it may also have a stop already in flight, which is what
                # routed us here from the early availability check). Say so
                # instead of the generic "already running".
                if self.force:
                    _why = (
                        "a stop already in flight"
                        if _stop_already_pending(existing)
                        else "could not be stopped"
                    )
                    return {
                        "error": (
                            f"Worker '{self.worker_name}' instance {spawn_iid} "
                            f"could not be replaced: the existing instance is "
                            f"still running ({_why}) and survived force-stop; "
                            "it was left registered so it is not orphaned. "
                            "Wait for it to exit before spawning this id again."
                        ),
                        "status": existing.status,
                        "worker_name": self.worker_name,
                        "instance_id": spawn_iid,
                        "stop_pending": _stop_already_pending(existing),
                    }
                return {
                    "error": (
                        f"Worker '{self.worker_name}' instance {spawn_iid} "
                        f"is already running"
                    ),
                    "status": existing.status,
                }
            # Re-check the spawn cap under the lock (TOCTOU guard): another
            # spawn may have registered between the early cap check and here.
            live_now = sum(
                1
                for key, t in _worker_registry.items()
                if key[0] == session_key and t.is_alive()
            )
            if live_now >= max_workers:
                return {
                    "error": (
                        f"Worker spawn limit reached: {live_now}/{max_workers} "
                        f"workers already running for this session. Stop an existing "
                        f"worker before spawning '{self.worker_name}'."
                    ),
                    "max_workers": max_workers,
                    "live_workers": live_now,
                }
            _worker_registry[(session_key, self.worker_name, spawn_iid)] = thread
            # S7a: seed the epoch record in THIS SAME critical section. Without
            # it the raw write above registers a thread with no epoch, so the
            # fenced run() teardown (CAS on the thread's own _generation) can
            # never match and the entry would leak in _worker_registry forever.
            # register_worker() cannot be used here: it takes this very
            # (non-reentrant) _registry_lock, and moving the registration
            # outside the block would re-open the capped-spawn TOCTOU race this
            # block exists to close.
            _spawn_epoch = getattr(thread, "_generation", None)
            # S7a: mirror WorkerRegistry._coerce_epoch — only a real int may become
            # an epoch; a duck-typed/mocked _generation must degrade to unversioned
            # rather than poisoning the map with a value that breaks later int
            # comparisons on this key.
            if not isinstance(_spawn_epoch, int) or isinstance(_spawn_epoch, bool):
                _spawn_epoch = None
            if _spawn_epoch is not None:
                _spawn_key = (session_key, self.worker_name, spawn_iid)
                _stored_epoch = _registry_instance._worker_epochs.get(_spawn_key)
                if _stored_epoch is None or _stored_epoch <= _spawn_epoch:
                    _registry_instance._worker_epochs[_spawn_key] = _spawn_epoch

        # S7a-3: publish the handle the frame-sink attachment just built, so the
        # supervisor's command path can reach this worker in process instead of
        # only through ``command.json``.
        #
        # Placement is AFTER the critical section above, never inside it: the
        # handle store takes its own lock, and the ordering every caller
        # respects is registry -> handle, never the other way round.  The
        # section itself is not restructured for this -- moving the registry
        # write out of it would re-open the capped-spawn TOCTOU race the block
        # exists to close.
        #
        # The key's session id is the RAW ``self.session_id``, not
        # ``session_key``: the routes look a handle up with exactly the value
        # they address the worker with, and ``_NO_SESSION_KEY`` is a
        # registry-only sentinel -- keying handles with it would make every
        # legacy worker's handle unreachable, a silent miss instead of a
        # visible failure.
        #
        # Fail open, like the other spawn-side bookkeeping here: an instance id
        # that is not an int cannot key a handle, and a spawn must never break
        # over it (nor invent a default).  ``epoch`` is the same coerced value
        # seeded into ``_worker_epochs`` above, so the store's compare-and-set
        # speaks the registry's language; ``None`` means "unversioned" and
        # registers exactly as it did before this change.
        try:
            if isinstance(spawn_iid, int) and not isinstance(spawn_iid, bool):
                register_runtime_handle(
                    _runtime_handle,
                    session_id=self.session_id,
                    worker_name=self.worker_name,
                    instance_id=spawn_iid,
                    epoch=_spawn_epoch,
                )
            else:
                logger.debug(
                    "Spawn: not registering a runtime handle for worker '%s' "
                    "instance %r: the instance id is not an int",
                    self.worker_name,
                    spawn_iid,
                )
        except Exception:
            logger.debug(
                "Spawn: could not register a runtime handle for worker '%s' "
                "instance %r",
                self.worker_name,
                spawn_iid,
                exc_info=True,
            )

        # ── Commit 8: dispatch window opens — the worker thread starts here ──
        dispatch_started_at = time.time()
        thread.start()

        if missing_tools:
            logger.warning(
                "Worker '%s' spawned but tool(s) not found: %s",
                self.worker_name, missing_tools,
            )

        # ── Wait for first response if auto-query was queued ──
        # If initial context has a "query" key, the worker auto-processes it
        # and puts the result in _output_queue.  We wait for that first
        # response, then return it while the thread stays alive for future
        # queries.  If no auto-query was queued, return immediately.
        has_auto_query = (
            thread._initial_context is not None
            and isinstance(thread._initial_context, dict)
            and "query" in thread._initial_context
        )
        if has_auto_query:
            auto_wait = _worker_query_wait_timeout(
                getattr(thread, "_timeout_seconds", None),
                fallback=SPAWN_QUEUE_TIMEOUT,
            )
            try:
                final_result = thread._output_queue.get(timeout=auto_wait)
            except queue.Empty:
                final_result = json.dumps({
                    "error": f"Worker '{self.worker_name}' did not respond "
                             f"within {auto_wait}s",
                    "note": "Worker timed out. The result above is partial work "
                             "completed before timeout. The worker thread is still "
                             "alive — you can query it again via action='query' to "
                             "request continuation.",
                })
            try:
                parsed = json.loads(final_result) if final_result else {}
            except (json.JSONDecodeError, TypeError):
                parsed = {"response": final_result}
            if not isinstance(parsed, dict):
                parsed = {"response": str(parsed)}
            parsed.setdefault("worker_name", self.worker_name)
            parsed.setdefault("spawned", True)
            parsed.setdefault("instance_id", thread.instance_id)
            parsed.setdefault("instance_label", thread.instance_label)
            elapsed = thread._last_elapsed()
            if elapsed is not None:
                parsed["elapsed_seconds"] = round(elapsed, 1)
            # ── Commit 8: dispatch window closes — envelope about to return ──
            dispatch_finished_at = time.time()
            # Commit 8: self-reported value (never trusted as dispatch duration).
            parsed["worker_self_reported"] = (
                {"elapsed_seconds": round(elapsed, 1)}
                if elapsed is not None else None
            )
            parsed["spawn_started_at"] = spawn_started_at
            parsed["dispatch_started_at"] = dispatch_started_at
            parsed["dispatch_finished_at"] = dispatch_finished_at
            parsed["dispatch_duration_wall"] = dispatch_finished_at - dispatch_started_at
            return parsed
        else:
            # No auto-query — return immediately; worker stays alive
            # ── Commit 8: dispatch window closes — envelope about to return ──
            dispatch_finished_at = time.time()
            return {
                "worker_name": self.worker_name,
                "instance_id": thread.instance_id,
                "instance_label": thread.instance_label,
                "spawned": True,
                "status": "ready",
                # Commit 8: worker reported nothing yet (no query ran).
                "worker_self_reported": None,
                "spawn_started_at": spawn_started_at,
                "dispatch_started_at": dispatch_started_at,
                "dispatch_finished_at": dispatch_finished_at,
                "dispatch_duration_wall": dispatch_finished_at - dispatch_started_at,
            }

    def _heartbeat_summary(self, hb_iso: Optional[str]):
        """Summarize a worker's heartbeat age for the check payload.

        Returns ``(age_seconds, stale)`` where ``age_seconds`` is rounded to
        one decimal (None when there is no/invalid heartbeat) and ``stale`` is
        True when the heartbeat is older than HEARTBEAT_STALE_AFTER_S.
        """
        if not hb_iso:
            return (None, False)
        try:
            hb_dt = datetime.fromisoformat(hb_iso)
            if hb_dt.tzinfo is None:
                hb_dt = hb_dt.replace(tzinfo=timezone.utc)
            age = round((datetime.now(timezone.utc) - hb_dt).total_seconds(), 1)
            return (age, age > HEARTBEAT_STALE_AFTER_S)
        except (ValueError, TypeError):
            return (None, False)

    def _action_check(self, workers: list) -> dict:
        """Check on a specific worker by name (+ optional instance_id).

        Resolves live instances for the current session first; when no
        instance_id is given and none are live here, falls back to searching
        across all sessions and reports any foreign-session instances found.
        Name-only lookups against multiple live instances require an explicit
        instance_id.
        """
        session_key = self.session_id or _NO_SESSION_KEY
        _get_worker_lifecycle_observer()  # best-effort: ensure observer subscription
        try:
            thread, sid, iid = _resolve_worker_thread(
                self.worker_name,
                instance_id=self.instance_id,
                session_id=session_key,
            )
        except _WorkerAmbiguityError as exc:
            msg = str(exc)
            all_instances = []
            if self.instance_id is None and "is not running" in msg:
                # Not found in current session → search across all sessions
                all_instances = self._find_all_worker_threads(self.worker_name)
            if all_instances:
                # Found in another session — report with note
                if len(all_instances) == 1:
                    sid, t = all_instances[0]
                    return {
                        "worker_name": self.worker_name,
                        "status": t.status,
                        "current_task": t.current_task,
                        "last_heartbeat": t.last_heartbeat,
                        "error": t.error,
                        "session_id": sid,
                        "note": "Worker is from a different session",
                        "alive": t.is_alive(),
                    }
                # Multiple instances across sessions
                instances = []
                for sid, t in all_instances:
                    instances.append({
                        "session_id": sid,
                        "status": t.status,
                        "current_task": t.current_task,
                        "last_heartbeat": t.last_heartbeat,
                        "error": t.error,
                        "alive": t.is_alive(),
                    })
                return {
                    "worker_name": self.worker_name,
                    "note": "Worker instances found across multiple sessions",
                    "instances": instances,
                    "count": len(instances),
                }

            if "ambiguous" in msg:
                # Name maps to multiple live instances in this session — the
                # caller must pass an explicit instance_id.
                return {
                    "error": msg,
                    "worker_name": self.worker_name,
                }

            # No instances found anywhere — report as stopped
            entry = self._find_worker(workers, self.worker_name)
            if entry:
                return {
                    "worker_name": self.worker_name,
                    "status": "stopped",
                    "current_task": None,
                    "last_heartbeat": None,
                    "error": None,
                }
            return {
                "error": f"Worker '{self.worker_name}' not found",
            }

        age, stale = self._heartbeat_summary(thread.last_heartbeat)
        return {
            "worker_name": self.worker_name,
            "instance_id": iid,
            "instance_label": _WorkerRegistry.instance_label(self.worker_name, iid),
            "status": thread.status,
            "current_task": thread.current_task,
            "last_heartbeat": thread.last_heartbeat,
            "error": thread.error,
            "session_id": sid,
            "current_context_tokens": thread.get_current_context_tokens(),
            "max_context_tokens": thread.max_context_tokens,
            "alive": thread.is_alive(),
            "conversation_length": len(thread._worker_ctx.user_history) if thread._worker_ctx else 0,
            "last_heartbeat_age_s": age,
            "stale": stale,
        }

    def _wait_for_worker_exit(self, thread) -> bool:
        """Wait (bounded) for a cooperatively-stopped worker thread to exit.

        Used by the sync-query timeout path so ``_action_query`` never returns
        while the worker thread is still alive (e.g. stuck inside a
        DockerCodeRunner call): the caller must observe a terminal state, not
        an in-flight thread. Same join-retry pattern as ``_action_stop``
        (budget = max(30, thread timeout)); never Thread.kill. Returns True
        when the thread exited within the budget, False when the budget
        elapsed with the daemon thread still alive (the envelope then reports
        the degraded outcome and the thread is left to terminate on its own).
        """
        # The thread may be a test double (or otherwise lack a numeric
        # ``_timeout_seconds``); fall back to the module default so the join
        # budget stays a bounded number.
        _timeout_seconds = getattr(thread, "_timeout_seconds", WORKER_TIMEOUT_SECONDS)
        if not isinstance(_timeout_seconds, (int, float)):
            _timeout_seconds = WORKER_TIMEOUT_SECONDS
        _join_budget = max(30, int(_timeout_seconds or WORKER_TIMEOUT_SECONDS))
        _join_elapsed = 0.0
        _join_step = 2.0
        while _join_elapsed < _join_budget:
            thread.join(timeout=_join_step)
            _join_elapsed += _join_step
            if not thread.is_alive():
                return True
            logger.debug(
                "Still waiting for worker '%s' to stop "
                "(%.0f/%ds elapsed)",
                self.worker_name, _join_elapsed, _join_budget,
            )
        if thread.is_alive():
            logger.warning(
                "Worker '%s' did not stop within %ds budget after query "
                "timeout; returning envelope with thread still running "
                "(daemon thread)",
                self.worker_name, _join_budget,
            )
            return False
        return True

    def _action_query(self, workers: list) -> dict:
        """Query a worker and wait for a response (synchronous, blocking)."""
        try:
            thread, _sid, iid = _resolve_worker_thread(
                self.worker_name,
                instance_id=self.instance_id,
                session_id=self.session_id or _NO_SESSION_KEY,
            )
        except _WorkerAmbiguityError as exc:
            return {
                "error": str(exc),
                "worker_name": self.worker_name,
            }

        if not thread.is_alive():
            return {
                "error": f"Worker '{self.worker_name}' is no longer alive "
                          f"(status: {thread.status}).",
                "status": thread.status,
                "error_detail": thread.error,
            }

        if not self.worker_query:
            return {
                "error": "worker_query is required for action 'query'",
            }

        # A follow-up carries its context INSIDE the query string: one code
        # path then serves the direct, detached-queue and paused-resume
        # deliveries alike.  No context -> delivered_query IS worker_query.
        delivered_query, context_note = self._compose_followup_query()

        # PHASE 2.1b: a submission against a DETACHED worker is QUEUED and
        # returns immediately -- it does NOT resume the worker. Resuming is
        # operator-initiated only (docs/design-worker-lifecycle.md, "Queries
        # submitted while the worker is detached"), so the caller keeps the
        # submit_query contract and gets its job_id back at enqueue time. Only
        # the paused branch below stays a resume.
        if thread.status == "detached":
            job_id = uuid.uuid4().hex
            try:
                enqueue = getattr(thread, "enqueue_query", None)
                if callable(enqueue):
                    enqueue(job_id, delivered_query)
                else:  # pragma: no cover - duck-typed thread doubles
                    thread._input_queue.put((job_id, delivered_query, None))
            except Exception as exc:
                return {
                    "error": f"Failed to enqueue query: {exc}",
                    "worker_name": self.worker_name,
                }
            registry = _get_worker_job_registry()
            if registry is not None:
                try:
                    registry.register(
                        job_id,
                        self.worker_name,
                        self.session_id or "",
                        instance_id=iid,
                    )
                except Exception:
                    # All-or-nothing: a query is accepted only when it is BOTH
                    # queued and registered. Swallowing a registration failure
                    # would hand back a job_id no later job_status / audit /
                    # resume path can resolve, so roll the queue entry back and
                    # let the failure surface (Worker.execute turns it into a
                    # JSON error envelope; nothing success-shaped is returned).
                    try:
                        withdraw = getattr(thread, "withdraw_queued_query", None)
                        if callable(withdraw):
                            withdraw(job_id)
                        else:  # pragma: no cover - duck-typed thread doubles
                            discard = getattr(thread, "_discard_pending_job", None)
                            if callable(discard):
                                discard(job_id)
                    except Exception:
                        pass
                    raise
            detached_envelope = {
                "worker_name": self.worker_name,
                "instance_id": iid,
                "instance_label": _WorkerRegistry.instance_label(self.worker_name, iid),
                "job_id": job_id,
                "status": "submitted",
                "note": "Worker is detached: the query was queued (strict FIFO) and "
                        "runs only once an operator resumes the worker. Poll with "
                        "action='job_status' / action='join'.",
            }
            if context_note is not None:
                detached_envelope["context_note"] = context_note
            return detached_envelope
        if thread.status == "paused":
            # A paused worker must never be queried destructively (BUG 3):
            # the old behavior blocked up to 300s, then stopped the worker
            # and reclaimed its containers. Manual-only pauses reject the
            # query immediately (non-destructively); normal pauses are
            # auto-resumed and the query proceeds normally below.
            if getattr(thread, "_manual_only_pause", False):
                paused_envelope = {
                    "status": "paused",
                    "message": "Worker is manually paused",
                    "worker_name": self.worker_name,
                    "session_id": self.session_id or "",
                }
                if self.context is not None:
                    # The query was NOT sent, so the context was not delivered
                    # either -- say so instead of dropping it silently.
                    paused_envelope["context_note"] = (
                        "context was NOT delivered: the worker is manually paused "
                        "and the query was not sent. Re-send it with the query "
                        "once the worker is resumed."
                    )
                return paused_envelope
            try:
                _resume_outcome = thread.resume()
            except Exception as exc:
                _resume_outcome = False
                logger.warning(
                    "Query: worker '%s' raised on resume: %s", self.worker_name, exc
                )
            if _resume_outcome is False or _stop_already_pending(thread):
                # A stop is already in flight: this thread is dead. Blocking in
                # send_query() below would wait out the full query timeout and
                # still return nothing useful, so refuse up front.
                return {
                    "error": (
                        f"Worker '{self.worker_name}' has a stop in flight "
                        f"(status: {getattr(thread, 'status', None)}) and "
                        "refused to resume; the query was not sent."
                    ),
                    "status": getattr(thread, "status", None),
                    "worker_name": self.worker_name,
                    "instance_id": iid,
                }

        # ONE CLOCK: record the budget this hop actually uses (the thread's
        # dispatch-time budget, or a per-call request) for the envelope.
        self._dispatch_budget = budget_for_thread(
            thread,
            requested=self.timeout_seconds,
            default=DEFAULT_REQUEST_SECONDS,
            source="worker_thread.query",
        )
        wait = _worker_query_wait_timeout(
            getattr(thread, "_timeout_seconds", None),
            fallback=DEFAULT_REQUEST_SECONDS,
        )
        if wait != DEFAULT_REQUEST_SECONDS:
            logger.info(
                "query wait %.0fs (effective timeout %.0fs + grace); "
                "no cap applied -- bounded only by the declared ceiling",
                wait,
                wait - QUERY_WAIT_GRACE_SECONDS,
            )
        try:
            # Block until the worker responds
            response = thread.send_query(delivered_query, timeout=wait)
            elapsed = thread._last_elapsed()
            result = {
                "worker_name": self.worker_name,
                "instance_id": iid,
                "instance_label": _WorkerRegistry.instance_label(self.worker_name, iid),
                "response": response,
                "elapsed_seconds": round(elapsed, 1) if elapsed is not None else None,
            }
            if thread._last_reasoning is not None:
                result["reasoning"] = thread._last_reasoning
            if context_note is not None:
                result["context_note"] = context_note
            return result
        except TimeoutError as exc:
            # Cooperative stop: the query deadline elapsed with no reply. Stop
            # the worker thread so its run() loop drains, its terminal path
            # runs, and its containers are reclaimed. Never Thread.kill — the
            # thread always terminates via its own run() teardown (which calls
            # _cleanup_worker_containers).
            # PHASE 2.1a: this path must TERMINATE — stop() only detaches now
            # (it would leave the thread parked, alive and un-drained), so the
            # query timeout calls kill(). Still not Thread.kill (the OS verb):
            # the thread exits via its own run() teardown. Best-effort: if it
            # raises, run() teardown remains authoritative.
            try:
                thread.kill()
            except Exception:
                pass
            # Synchronous reclaim: the worker may be stuck inside a DockerCodeRunner
            # call, so run()'s finally (the safety net) may not run until that call
            # returns. Reclaim worker-owned containers NOW from the timeout path.
            # If the worker is mid-tool-call using the container, the stop/remove
            # will make that call fail and return — acceptable. No join() here; no
            # Thread.kill; never touches resource containers.
            try:
                thread._cleanup_worker_containers()
            except Exception:
                pass
            # ── Stop guarantee: bounded wait for actual thread exit ──
            # The sync call must not return while the worker thread is still
            # alive (it may be stuck inside a DockerCodeRunner call): the
            # caller would observe a terminal envelope while the thread is
            # still in flight. join() in retry steps (same pattern as
            # _action_stop), bounded by max(30, timeout_seconds). Never
            # Thread.kill — if the budget elapses the daemon thread is left to
            # terminate on its own and the envelope reports the degraded
            # outcome.
            clean_exit = self._wait_for_worker_exit(thread)
            stopped_note = (
                "Worker did not respond in time and was stopped cooperatively "
                "(its containers were reclaimed). Re-spawn the worker before "
                "querying it again."
            )
            degraded_note = (
                "Worker did not respond in time and was stopped cooperatively, "
                "but it did not exit within the stop budget and may still be "
                "terminating in the background. Re-spawn the worker before "
                "querying it again."
            )
            if not clean_exit:
                logger.warning(
                    "Worker '%s' still alive after query-timeout stop budget; "
                    "returning envelope with thread running",
                    self.worker_name,
                )
            # Heartbeat classification now only shapes the message content —
            # both branches terminate the worker, because a timed-out query is
            # final (the worker was stopped and its containers reclaimed).
            if thread.last_heartbeat:
                try:
                    hb_dt = datetime.fromisoformat(thread.last_heartbeat)
                    age_seconds = (datetime.now(timezone.utc) - hb_dt).total_seconds()
                    if age_seconds > 600.0:  # 2× the 300s query timeout
                        _publish_global_worker_event(EventType.WORKER_TIMEOUT, {
                            "session_id": self.session_id or "",
                            "worker_name": self.worker_name,
                            "status": thread.status,
                            "reason": "hung",
                            "last_heartbeat": thread.last_heartbeat,
                            "heartbeat_age_s": round(age_seconds, 1),
                            "current_context_tokens": thread.get_current_context_tokens(),
                            "max_context_tokens": thread.max_context_tokens,
                            **({"query_id": getattr(thread, "_current_query_id", None)}
                               if getattr(thread, "_current_query_id", None) is not None else {}),
                        })
                        return {
                            "error": f"Worker appears hung (last heartbeat: {thread.last_heartbeat}, {age_seconds:.0f}s ago)",
                            "worker_name": self.worker_name,
                            "status": thread.status,
                            "current_task": thread.current_task,
                            "note": degraded_note if not clean_exit else stopped_note,
                        }
                except (ValueError, TypeError):
                    pass  # Malformed heartbeat — fall through to generic timeout
            return {
                "error": str(exc),
                "note": degraded_note if not clean_exit else stopped_note,
                "worker_name": self.worker_name,
                "status": thread.status,
            }

    def _action_submit_query(self, workers: list) -> dict:
        """Submit a query without blocking (Phase 2B).

        Enqueues a (job_id, query, None) tuple: replies are NOT routed to a
        private queue, so they land on the shared _output_queue and are drained
        as stale by any later synchronous send_query call. Returns immediately
        with a job handle; progress/completion arrive via WORKER_* events and
        the job registry.
        """
        session_key = self.session_id or _NO_SESSION_KEY
        try:
            thread, _sid, iid = _resolve_worker_thread(
                self.worker_name,
                instance_id=self.instance_id,
                session_id=session_key,
            )
        except _WorkerAmbiguityError as exc:
            return {"error": str(exc), "worker_name": self.worker_name}
        if not thread.is_alive():
            return {
                "error": f"Worker '{self.worker_name}' is no longer alive (status: {thread.status}).",
                "status": thread.status,
                "error_detail": thread.error,
            }
        if not self.worker_query:
            return {"error": "worker_query is required for action 'submit_query'"}
        # Same single-string composition as action='query': the queued item is
        # what the worker later reads, so the context must ride inside it.
        delivered_query, context_note = self._compose_followup_query()
        job_id = uuid.uuid4().hex
        try:
            # PHASE 2.1b: append through the bookkept FIFO appender. This only
            # ever appends (a busy worker's later submissions queue behind the
            # running turn; a detached worker stays parked and burns nothing).
            enqueue = getattr(thread, "enqueue_query", None)
            if callable(enqueue):
                enqueue(job_id, delivered_query)
            else:  # pragma: no cover - duck-typed thread doubles
                thread._input_queue.put((job_id, delivered_query, None))
        except Exception as exc:
            return {"error": f"Failed to enqueue query: {exc}", "worker_name": self.worker_name}
        registry = _get_worker_job_registry()
        if registry is not None:
            try:
                registry.register(
                    job_id,
                    self.worker_name,
                    self.session_id or "",
                    instance_id=iid,
                )
            except Exception:
                # All-or-nothing, same contract as the detached ``query`` path:
                # a submission is accepted only when it is BOTH queued and
                # registered. Withdraw the queue entry and re-raise so
                # Worker.execute surfaces the failure instead of returning a
                # job_id the registry never stored (which the queue would then
                # run with no id the job_status / audit / resume path resolves).
                try:
                    withdraw = getattr(thread, "withdraw_queued_query", None)
                    if callable(withdraw):
                        withdraw(job_id)
                    else:  # pragma: no cover - duck-typed thread doubles
                        discard = getattr(thread, "_discard_pending_job", None)
                        if callable(discard):
                            discard(job_id)
                except Exception:
                    pass
                raise
        submitted_envelope = {
            "worker_name": self.worker_name,
            "instance_id": iid,
            "instance_label": _WorkerRegistry.instance_label(self.worker_name, iid),
            "job_id": job_id,
            "status": "submitted",
            "note": "Query submitted (non-blocking). Progress/completion arrive via "
                    "WORKER_PARTIAL_RESULT / WORKER_COMPLETED events; poll with "
                    "action='job_status'.",
        }
        if context_note is not None:
            submitted_envelope["context_note"] = context_note
        return submitted_envelope

    def _action_job_status(self, workers: list) -> dict:
        """Read job records from the registry (non-blocking, Phase 2B)."""
        registry = _get_worker_job_registry()
        if registry is None:
            return {"error": "Worker job registry unavailable", "worker_name": self.worker_name}
        job_id = (self.worker_query or "").strip() or None
        if job_id:
            rec = registry.job(job_id)
            if rec is None:
                return {"worker_name": self.worker_name, "job_id": job_id, "error": "Job not found"}
            return {
                "worker_name": self.worker_name,
                "job_id": rec.get("job_id"),
                "status": rec.get("status"),
                "created_at": rec.get("created_at"),
                "updated_at": rec.get("updated_at"),
                "completed_at": rec.get("completed_at"),
                "has_result": rec.get("result") is not None,
                "preview": rec.get("preview", ""),
            }
        jobs = [
            {
                "job_id": r.get("job_id"),
                "status": r.get("status"),
                "created_at": r.get("created_at"),
                "updated_at": r.get("updated_at"),
                "completed_at": r.get("completed_at"),
                "has_result": r.get("result") is not None,
                "preview": r.get("preview", ""),
            }
            for r in registry.jobs(
                worker_name=self.worker_name or None,
                instance_id=self.instance_id,
            )
        ]
        return {"worker_name": self.worker_name, "jobs": jobs, "count": len(jobs)}

    def _action_join(self, workers: list) -> dict:
        """Block until a submitted job reaches a terminal state (Phase 2B).

        Shared handler for both the ``join`` and ``wait_for_job`` actions.
        ``worker_query`` carries the job_id (same convention as ``job_status``).
        Polls the job registry in 0.25s slices for a total of the RESOLVED
        budget -- the per-call request if given, else the budget pinned on the
        thread at spawn (default 60, bounded only by DECLARED_CEILING). There
        is no 300 s cap. Returns early on: job terminal state
        (completed / timeout / error), a partial result (status ``partial_result``
        + preview), the worker thread pausing (``status == 'paused'``) or being
        detached (``status == 'detached'``), a kill signal (stop_event set /
        status ``killed``), or thread death. Also returns early -- WITHOUT
        cancelling the job -- when the caller's turn is cancelled: the token
        bound to this tool thread is set, so the WAIT stops but the job stays
        in the registry (``status == 'interrupted'``, ``interrupted == True``).
        """
        _JOB_TERMINAL = ("completed", "timeout", "error")
        job_id = (self.worker_query or "").strip()
        if not job_id:
            return {
                "error": "worker_query is required for action 'join' (pass the job_id)",
                "worker_name": self.worker_name,
            }
        try:
            thread, _sid, iid = _resolve_worker_thread(
                self.worker_name,
                instance_id=self.instance_id,
                session_id=self.session_id or _NO_SESSION_KEY,
            )
        except _WorkerAmbiguityError as exc:
            return {"error": str(exc), "worker_name": self.worker_name}
        registry = _get_worker_job_registry()
        if registry is None:
            return {"error": "Worker job registry unavailable", "worker_name": self.worker_name}
        rec = registry.job(job_id)
        if rec is None:
            return {
                "error": "Job not found",
                "job_id": job_id,
                "worker_name": self.worker_name,
            }
        # ── ONE CLOCK (defect #5): no more hardcoded 300 ───────────────
        # The wait is the RESOLVED budget for this worker (the per-call request
        # if given, else the budget resolved at dispatch), and the return value
        # below is the effective number -- a 420 s request waits 420 s.
        budget = budget_for_thread(
            thread,
            requested=self.timeout_seconds,
            default=JOIN_DEFAULT_WAIT_SECONDS,
            source="worker_thread.join",
        )
        self._dispatch_budget = budget
        if budget.clamped:
            logger.warning("wait_for_job: %s", budget.clamp_reason())
        total = int(budget.effective)
        deadline = time.monotonic() + total
        last_status = rec.get("status")
        while True:
            rec = registry.job(job_id)
            status = rec.get("status") if rec is not None else None
            if status is not None:
                last_status = status
            if rec is not None and status in _JOB_TERMINAL:
                out = {
                    "worker_name": self.worker_name,
                    "instance_id": iid,
                    "instance_label": _WorkerRegistry.instance_label(self.worker_name, iid),
                    "job_id": job_id,
                    "status": status,
                    "preview": rec.get("preview", ""),
                    "has_result": rec.get("result") is not None,
                    "completed_at": rec.get("completed_at"),
                }
                if rec.get("result") is not None:
                    out["result"] = rec.get("result")
                return out
            if rec is not None and status == "partial" and rec.get("preview"):
                return {
                    "worker_name": self.worker_name,
                    "instance_id": iid,
                    "instance_label": _WorkerRegistry.instance_label(self.worker_name, iid),
                    "job_id": job_id,
                    "status": "partial_result",
                    "preview": rec.get("preview"),
                }
            # Session main-pause (UNIT C): the main agent is paused, so a
            # blocking wait is pointless — return paused promptly. Pause
            # propagates INTO join waits this way.
            if self.session_id and self.session_id in _SESSION_MAIN_PAUSED:
                # PHASE 2f (operator ruling (b)): Main is paused while waiting
                # on THIS worker, so this worker is paused too.  Scoped to the
                # join target only -- async workers the caller is not waiting
                # on are untouched (that is the ``want_all`` axis, not this
                # one).  Idempotent; see _pause_join_target.
                if _pause_join_target(thread, self.worker_name):
                    _record_proxied_join_target(self.session_id, self.worker_name, iid)
                return {
                    "worker_name": self.worker_name,
                    "instance_id": iid,
                    "instance_label": _WorkerRegistry.instance_label(self.worker_name, iid),
                    "job_id": job_id,
                    "status": "paused",
                    "note": "Session main agent is paused; job remains pending.",
                }
            # Worker-side wake conditions.
            if getattr(thread, "status", None) in ("paused", "detached"):
                return {
                    "worker_name": self.worker_name,
                    "instance_id": iid,
                    "instance_label": _WorkerRegistry.instance_label(self.worker_name, iid),
                    "job_id": job_id,
                    "status": "paused",
                    "note": "Worker is paused/detached; job remains pending.",
                }
            stop_event = getattr(thread, "_stop_event", None)
            if (stop_event is not None and stop_event.is_set()) or \
                    getattr(thread, "status", None) in ("killed",):
                return {
                    "worker_name": self.worker_name,
                    "instance_id": iid,
                    "instance_label": _WorkerRegistry.instance_label(self.worker_name, iid),
                    "job_id": job_id,
                    "status": "stopped",
                    "note": "Worker was killed; job did not reach a terminal state.",
                }
            if not thread.is_alive():
                return {
                    "worker_name": self.worker_name,
                    "instance_id": iid,
                    "instance_label": _WorkerRegistry.instance_label(self.worker_name, iid),
                    "job_id": job_id,
                    "status": "stopped",
                    "note": "Worker thread is no longer alive; job did not reach a terminal state.",
                }
            if time.monotonic() >= deadline:
                return {
                    "worker_name": self.worker_name,
                    "instance_id": iid,
                    "instance_label": _WorkerRegistry.instance_label(self.worker_name, iid),
                    "job_id": job_id,
                    "status": "timeout",
                    "note": f"Timed out after {total}s waiting for job; last known status: {last_status}.",
                }
            # Interruptible WAIT.  A cancel (detach()/kill()/cross-process
            # 'stop') stops the WAIT only: the job keeps running and STAYS in
            # the registry -- nothing here cancels or removes it.  With no
            # token bound (tests, direct use) this never fires, so the
            # full-timeout wait is unchanged.
            cancelled, reason = self._join_cancel_check()
            if cancelled:
                return {
                    "worker_name": self.worker_name,
                    "instance_id": iid,
                    "instance_label": _WorkerRegistry.instance_label(self.worker_name, iid),
                    "job_id": job_id,
                    "status": "interrupted",
                    "interrupted": True,
                    "reason": reason,
                    "last_status": last_status,
                    "completed_jobs": self._join_completed_jobs(registry),
                    "note": (
                        "Wait interrupted; the job was NOT cancelled and remains in "
                        f"the registry (last known status: {last_status})."
                    ),
                }
            time.sleep(0.25)

    def _join_cancel_check(self) -> tuple:
        """``(cancelled, reason)`` for the join poll loop.

        Primary signal: the thread-local turn-cancel token bound by the
        executing worker (``tools.sleep_tool.bind_turn_cancel``) -- the very
        token that interrupts ``sleep``.  Secondary: an injected
        ``agent_config['stop_check']`` probe, mirroring
        ``Sleep._external_stop`` (normally absent: ``AgentConfig`` strips it).
        """
        from tools.sleep_tool import cancel_reason, is_interrupted
        try:
            if is_interrupted():
                return True, cancel_reason() or "turn_cancelled"
        except Exception:  # pragma: no cover - defensive
            pass
        cfg = self.agent_config or {}
        probe = cfg.get("stop_check") if isinstance(cfg, dict) else None
        if callable(probe):
            try:
                if probe():
                    return True, "stop_check"
            except Exception:  # pragma: no cover - defensive
                pass
        return False, None

    def _join_completed_jobs(self, registry) -> list:
        """Jobs of this worker/instance already terminal when a join is interrupted."""
        try:
            jobs = registry.jobs(worker_name=self.worker_name or None, instance_id=self.instance_id)
        except Exception:  # pragma: no cover - defensive
            return []
        return [
            {
                "job_id": r.get("job_id"),
                "status": r.get("status"),
                "completed_at": r.get("completed_at"),
            }
            for r in jobs
            if r.get("status") in ("completed", "timeout", "error")
        ]

    def _action_pause(self, workers: list) -> dict:
        """Pause the session's main agent and/or its workers (UNIT C, v2).

        Cooperative only: workers pause at their next safe loop boundary
        (``WorkerThread.pause()`` sets the pause event; the thread flips its own
        ``status`` to ``'paused'``). No thread is killed; a paused idle worker
        stays alive until explicitly stopped or resumed.

        ``worker_query`` is a comma/space-separated mode string:
          - (empty) or ``main`` \u2192 pause the main agent only. Async workers
            keep running; ``join``/``wait_for_job`` return
            ``{"status": "paused"}`` while the main agent is paused.
          - ``all`` \u2192 also cooperatively pause every live worker of this
            session (Pause All).
          - ``manual_only`` \u2192 the workers paused by this call are marked
            manual-only: a later query will NOT auto-resume them; an OPERATOR
            must resume them (workspace UI / the HTTP resume route).  The
            agent has no resume action.
        When ``worker_name`` is set, only that worker is paused (the session
        main agent is not paused unless ``main`` is also present).
        """
        from tools.workspace.worker import _registry_lock, _worker_registry
        session_key = self.session_id or _NO_SESSION_KEY
        spec = (self.worker_query or "").strip().lower()
        tokens = {t for t in spec.replace(",", " ").split() if t}
        want_all = "all" in tokens
        want_main = (not tokens) or ("main" in tokens)
        manual_only = "manual_only" in tokens
        if self.worker_name:
            want_all = True
            want_main = False
        if want_main:
            _SESSION_MAIN_PAUSED.add(session_key)
        paused_workers = []
        with _registry_lock:
            for key, thread in list(_worker_registry.items()):
                if key[0] != session_key:
                    continue
                if self.worker_name is not None and key[1] != self.worker_name:
                    continue
                alive = bool(getattr(thread, "is_alive", lambda: True)())
                if not alive:
                    continue
                if not want_all:
                    continue
                if manual_only:
                    try:
                        thread._manual_only_pause = True
                    except Exception:
                        pass
                try:
                    thread.pause()
                except Exception:
                    pass
                paused_workers.append({
                    "worker_name": key[1],
                    "instance_id": key[2] if len(key) >= 3 else 1,
                })
        if want_main and not want_all:
            note = "Main agent paused; async workers continue."
        elif want_all:
            note = "Main agent and session workers paused (cooperative)."
        else:
            note = "Requested worker(s) paused."
        return {
            "status": "paused",
            "session_id": self.session_id,
            "scope": "all" if want_all else "main",
            "main_agent_paused": want_main,
            "manual_only": manual_only,
            "workers_paused": paused_workers,
            "note": note,
        }

    def _action_stop(self, workers: list) -> dict:
        """DETACH a running worker and persist its context (PHASE 2.1a).

        ``stop`` never terminates anything: the worker parks with status
        ``"detached"``, its thread, containers and registry entry all survive.
        Only an OPERATOR revives it (UI resume / HTTP resume route); a query
        submitted while it is detached is QUEUED in strict FIFO order and runs
        after that resume — it is never itself a resume.  Termination is
        operator-only as well (the UI kill route); this tool offers the agent
        neither ``kill`` nor ``resume``.

        First tries the current session; if not found, falls back to a
        same-session sweep for any remaining instances of the name.  A live
        worker of the same name owned by ANOTHER session is never detached:
        session scoping is what makes stop workspace-safe.
        """
        session_key = self.session_id or _NO_SESSION_KEY
        try:
            thread, sid, iid = _resolve_worker_thread(
                self.worker_name,
                instance_id=self.instance_id,
                session_id=session_key,
            )
        except _WorkerAmbiguityError as exc:
            msg = str(exc)
            all_instances = []
            if self.instance_id is None and "is not running" in msg:
                # ── Not found in current session → sweep THIS session only ──
                # Never sweep the whole registry: a worker of the same name
                # owned by another session (and therefore another workspace)
                # is not this caller's property and must survive untouched.
                all_instances = self._find_all_worker_threads(
                    self.worker_name, session_id=self.session_id or None
                )
            if "ambiguous" in msg:
                return {
                    "error": msg,
                    "worker_name": self.worker_name,
                }
            if not all_instances:
                entry = self._find_worker(workers, self.worker_name)
                if entry:
                    return {
                        "worker_name": self.worker_name,
                        "status": "detached",
                        "message": f"Worker '{self.worker_name}' was not running.",
                    }
                return {
                    "error": f"Worker '{self.worker_name}' not found",
                }

            # Found in this session (non-live leftovers) — detach them all.
            detached_info = []
            for sid, t in all_instances:
                worker_label = f"{self.worker_name} (session={sid})"
                logger.info("Same-session stop: detaching worker '%s'", worker_label)
                try:
                    t.stop()
                except Exception as exc:
                    logger.exception("Error detaching worker '%s'", worker_label)
                    detached_info.append({"session_id": sid, "error": str(exc)})
                    continue
                # A DETACH reclaims nothing: no join, no container cleanup and
                # no registry pop — the thread is parked, not terminated.
                if t._worker_ctx is not None:
                    t._worker_ctx.compact_after_summary()
                t._save_context()
                t._write_status_file()
                detached_info.append({"session_id": sid, "status": t.status})

            return {
                "worker_name": self.worker_name,
                "status": "detached",
                "message": (
                    f"Detached {len(detached_info)} instance(s) of worker "
                    f"'{self.worker_name}' in this session."
                ),
                "detached_instances": detached_info,
            }

        # ── Found in current session ──
        if not thread.is_alive():
            return {
                "worker_name": self.worker_name,
                "status": thread.status,
                "message": f"Worker '{self.worker_name}' was not running.",
            }

        thread.stop()
        if thread._worker_ctx is not None:
            thread._worker_ctx.compact_after_summary()
        thread._save_context()
        thread._write_status_file()
        return {
            "worker_name": self.worker_name,
            "status": "detached",
            "message": f"Worker '{self.worker_name}' detached successfully.",
        }

    def _action_reset(self, workers: list) -> dict:
        """Stop a worker, DROP its persisted context, and free its spawn slot.

        Mirrors ``_action_stop`` resolution (current session first, then a
        same-session fallback — another session's worker is never touched),
        but instead of persisting the final context
        the worker's state files (context.json / status.json / command.json)
        are DELETED so the next spawn starts completely fresh.  Other
        workers and resource containers are never touched.
        """

        from tools.workspace.worker import _registry_lock, _worker_registry
        def _drop_persisted_files(t: Any) -> None:
            wdir = getattr(t, "_worker_dir", None)
            if wdir is None:
                return
            # S7b-3: the inflight journal goes too.  A reset's promise is that
            # the next spawn starts COMPLETELY fresh, and a surviving record --
            # whatever its status -- would either owe a successor a replay or
            # hold a new turn's write up as a foreign leftover.  ``unlink``
            # never parses the file, so a corrupt journal is dropped by this
            # same entry without a decode step that could raise.
            for fname in (
                "context.json",
                "status.json",
                "command.json",
                INFLIGHT_FILENAME,
            ):
                try:
                    (wdir / fname).unlink(missing_ok=True)
                except OSError:
                    pass

        def _reset_one(t: Any, sid: str) -> dict:
            try:
                # TRAP T1: reset must TERMINATE (kill), never detach — a
                # detached thread's run() finally-block would re-save the
                # context we are about to drop from disk.
                # S7b-3: the kill is DELIBERATE, so it opts into the journal --
                # the record is cancelled (and the job stopped) before the
                # files below are dropped, so a successor can never adopt a
                # turn this reset ended by hand.  ``_drop_persisted_files``
                # then unlinks the record: a reset leaves nothing behind at
                # all.
                t.kill(journal_reason=_JOURNAL_RESET_REASON)
                # Robust join with retry (same pattern as the force-respawn
                # termination path): the worker may be blocked inside a slow
                # LLM call.
                _budget = max(30, t._timeout_seconds)
                _elapsed = 0.0
                while _elapsed < _budget:
                    t.join(timeout=2.0)
                    _elapsed += 2.0
                    if not t.is_alive():
                        break
                if t.is_alive():
                    logger.warning(
                        "Reset: worker '%s' did not stop within %ds budget; "
                        "dropping its context anyway.",
                        self.worker_name, _budget,
                    )
            except Exception as exc:
                logger.exception("Error stopping worker '%s' during reset", self.worker_name)
                return {"session_id": sid, "error": str(exc)}
            finally:
                t._cleanup_worker_containers()
                _reset_handle_keys = []
                with _registry_lock:
                    for key, existing_t in list(_worker_registry.items()):
                        if (
                            key[0] == sid
                            and key[1] == self.worker_name
                            and existing_t is t
                        ):
                            _worker_registry.pop(key, None)
                            _reset_handle_keys.append(key)
                # S7a-3: the reset just killed this incarnation, so its handle
                # goes with the registry entry -- fenced with its own epoch, out
                # side the registry lock.
                _unregister_runtime_handles(
                    _reset_handle_keys, epoch=getattr(t, "_generation", None)
                )
                # Delete AFTER the join completes so the thread's run()
                # finally-block (which re-saves the context) cannot resurrect it.
                _drop_persisted_files(t)
            return {"session_id": sid, "status": "reset"}

        session_key = self.session_id or _NO_SESSION_KEY
        try:
            thread, sid, iid = _resolve_worker_thread(
                self.worker_name,
                instance_id=self.instance_id,
                session_id=session_key,
            )
        except _WorkerAmbiguityError as exc:
            msg = str(exc)
            all_instances = []
            if self.instance_id is None and "is not running" in msg:
                # Not found in current session → sweep THIS session only.
                # Never sweep the whole registry: a worker of the same name
                # owned by another session (and therefore another workspace)
                # is not this caller's property and must survive untouched.
                # ``self.session_id or None`` keeps the historical unscoped
                # lookup for a falsy session_id.
                all_instances = self._find_all_worker_threads(
                    self.worker_name, session_id=self.session_id or None
                )
            if "ambiguous" in msg:
                return {
                    "error": msg,
                    "worker_name": self.worker_name,
                }
            if not all_instances:
                entry = self._find_worker(workers, self.worker_name)
                if entry:
                    return {
                        "worker_name": self.worker_name,
                        "status": "reset",
                        "message": (
                            f"Worker '{self.worker_name}' was not running; "
                            "nothing to reset."
                        ),
                        "freed_slot": False,
                    }
                return {
                    "error": f"Worker '{self.worker_name}' not found",
                }

            # Found in this session (non-live leftovers) — reset all of them.
            reset_info = []
            for sid, t in all_instances:
                logger.info("Same-session reset: resetting worker '%s'", self.worker_name)
                info = _reset_one(t, sid)
                if info not in reset_info:
                    reset_info.append(info)
            return {
                "worker_name": self.worker_name,
                "status": "reset",
                "message": (
                    f"Reset {len(reset_info)} instance(s) of worker "
                    f"'{self.worker_name}' in this session."
                ),
                "reset_instances": reset_info,
                "freed_slot": True,
            }

        # ── Found in current session ──
        if not thread.is_alive():
            _reset_dropped_keys = []
            with _registry_lock:
                _reset_key = (sid, self.worker_name, iid)
                if _worker_registry.get(_reset_key) is thread:
                    _worker_registry.pop(_reset_key, None)
                    _reset_dropped_keys.append(_reset_key)
            # S7a-3: same identity guard for the handle store -- only the
            # incarnation that was actually still registered loses its handle,
            # and the removal is fenced with that incarnation's epoch.
            _unregister_runtime_handles(
                _reset_dropped_keys, epoch=getattr(thread, "_generation", None)
            )
            thread._cleanup_worker_containers()
            _drop_persisted_files(thread)
            return {
                "worker_name": self.worker_name,
                "instance_id": iid,
                "status": "reset",
                "message": (
                    f"Worker '{self.worker_name}' was already stopped; "
                    "state files removed."
                ),
                "freed_slot": True,
            }

        info = _reset_one(thread, sid)
        max_workers = self._effective_max_workers()
        live_workers = self._live_workers_in_session(session_key)
        return {
            "worker_name": self.worker_name,
            "instance_id": iid,
            "status": "reset",
            "message": (
                f"Worker '{self.worker_name}' stopped and its context was "
                "reset (state files dropped)."
            ),
            "freed_slot": True,
            "max_workers": max_workers,
            "live_workers": live_workers,
            "reset_instance": info,
        }


