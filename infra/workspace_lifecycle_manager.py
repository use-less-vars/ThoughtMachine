"""Workspace Lifecycle Manager — Phase 1 (worker state machine + execution tracking).

The supervisor wraps a worker's query loop with a strict state machine
(``WorkerState``), per-query correlation IDs, soft/hard timeout handling and
an execution tracker that can terminate in-flight docker execs, scoped
containers and subprocesses when a worker times out or is stopped.

The integration layer (``tools.workspace.worker.WorkerThread``) builds a
``WorkerSupervisor`` lazily and delegates ``process_query`` to it ONLY when
``use_workspace_lifecycle_manager`` is enabled in the session config
(``is_wlm_enabled``). When the flag is off the old query path runs untouched.

ContainerManager real API used here (verified against infra/container_manager.py):
- ``start(image=None, name=None, note=None)`` — request_container delegation
- ``stop(container_id)`` — idempotent, never raises
- ``remove(container_id)`` — idempotent, never raises (stops first)
- ``_session_config`` — private per-session config dict (no public property)
NOTE: ContainerManager has NO ``exec_stop`` method. ``ExecutionTracker`` calls
``exec_stop`` when the manager exposes it (forward-compat / fakes) and falls
back to ``stop(container_id)`` otherwise — stopping the container terminates
any exec running inside it.

ResourceContainerManager naming convention (infra/resource_container_manager.py):
containers are named ``tm-res-<sha256(workspace_path)[:12]>-git`` and the image
tag is ``RESOURCE_IMAGE_TAG = "tm-resource-git"``. Requests naming either are
rejected by ``request_container`` (resource containers are reserved for the
main agent).
"""

from __future__ import annotations

import collections
import enum
import logging
import os
import queue
import signal
import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional

try:
    from agent.logging import log as _agent_log
except ImportError:
    _agent_log = None

try:
    from infra.resource_container_manager import RESOURCE_IMAGE_TAG
except ImportError:  # pragma: no cover - defensive
    RESOURCE_IMAGE_TAG = "tm-resource-git"

try:
    from infra.registry_wiring import get_active_registry, is_registry_active
except ImportError:  # pragma: no cover - defensive
    def get_active_registry(session_config=None):  # type: ignore[misc]
        return None

    def is_registry_active(session_config=None) -> bool:  # type: ignore[misc]
        return False

# Resource container name convention (see ResourceContainerManager.container_name).
_RESOURCE_NAME_PREFIX = "tm-res-"
_RESOURCE_NAME_SUFFIX = "-git"

logger = logging.getLogger(__name__)

# Timeouts (seconds)
SOFT_TIMEOUT = 300    # default per-query wait bound
HARD_TIMEOUT = 600    # upper bound for the underlying worker loop
EXEC_KILL_GRACE = 10  # grace period for terminating a docker exec
QUERY_ID_PREFIX = "q_"


def _log(level: str, component: str, message: str) -> None:
    """Log via the agent logging facade when available, else stdlib logging."""
    if _agent_log is not None:
        _agent_log(level, component, message)
    else:
        getattr(logger, level.lower(), logger.info)(message)


class WorkerState(enum.Enum):
    IDLE = "idle"
    BUSY = "busy"
    PAUSED = "paused"
    TIMED_OUT = "timed_out"
    STOPPING = "stopping"
    STOPPED = "stopped"


class StateMachineError(Exception):
    """Raised when a state transition is invalid for the current state."""


def _get_session_config(container_manager: Any) -> dict:
    """Best-effort read of a manager's session config (public or private)."""
    if container_manager is None:
        return {}
    for attr in ("session_config", "_session_config"):
        cfg = getattr(container_manager, attr, None)
        if isinstance(cfg, dict):
            return cfg
    return {}


def is_wlm_enabled(container_manager: Any) -> bool:
    """True when ``use_workspace_lifecycle_manager`` is set in session config."""
    return bool(
        _get_session_config(container_manager).get("use_workspace_lifecycle_manager", False)
    )


class ExecutionTracker:
    """Tracks in-flight executions so they can be terminated on timeout/stop.

    Execution details dict: ``{query_id, tool_call_id, container_id, exec_id,
    start_time, type}`` where ``type`` is one of ``docker_exec``,
    ``subprocess`` (may also carry ``pid``) or ``scoped_container``.
    """

    def __init__(self) -> None:
        self._executions: Dict[str, dict] = {}
        self.kill_grace_seconds: float = EXEC_KILL_GRACE

    def register(
        self,
        worker_id: str,
        query_id: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        container_id: Optional[str] = None,
        exec_id: Optional[str] = None,
        pid: Optional[int] = None,
        tool_name: Optional[str] = None,
        type: str = "subprocess",
    ) -> str:
        """Register a new execution and return its id.

        The id is deterministic when a ``tool_call_id`` is available
        (``worker_id:tool_call_id``); otherwise it is generated from the
        worker and query ids. Stores a details dict with the same shape the
        termination paths read.
        """
        if tool_call_id:
            execution_id = f"{worker_id}:{tool_call_id}"
        else:
            execution_id = f"{worker_id}:{query_id}:{uuid.uuid4().hex[:8]}"
        self._executions[execution_id] = {
            "query_id": query_id,
            "tool_call_id": tool_call_id,
            "container_id": container_id,
            "exec_id": exec_id,
            "start_time": time.monotonic(),
            "type": type,
            "tool_name": tool_name,
            "pid": pid,
        }
        return execution_id

    def add(self, execution_id: str, details: dict) -> None:
        """Record an execution under ``execution_id``."""
        self._executions[execution_id] = details

    def remove(self, execution_id: str) -> None:
        """Forget an execution (idempotent)."""
        self._executions.pop(execution_id, None)

    def active_count(self) -> int:
        """Number of currently tracked executions."""
        return len(self._executions)

    def clear(self) -> None:
        """Forget all executions (no side effects)."""
        self._executions.clear()

    def terminate_all(
        self,
        worker_id: str,
        container_manager: Any,
        resource_container_manager: Any,
    ) -> None:
        """Terminate every tracked execution; then clear the tracker.

        - ``docker_exec``: ``exec_stop(container_id, exec_id, timeout=EXEC_KILL_GRACE)``
          when the manager exposes it, else ``stop(container_id)`` (real
          ContainerManager API — stopping the container kills its execs). On
          failure the container is force-removed via ``remove(container_id)``.
        - ``scoped_container``: ``stop(container_id)``.
        - ``subprocess``: ``os.killpg`` when the pid is a process group, else
          ``os.kill(pid, SIGTERM)``.

        Every step is guarded: a failure in one execution never prevents the
        others from being terminated. Idempotent — an empty tracker is a safe
        no-op.
        """
        if not self._executions:
            _log("DEBUG", "workspace.lifecycle",
                 f"terminate_all(worker={worker_id}): no active executions")
            return
        _log("INFO", "workspace.lifecycle",
             f"terminate_all(worker={worker_id}): terminating "
             f"{len(self._executions)} execution(s)")
        for execution_id, details in list(self._executions.items()):
            ex_type = details.get("type")
            try:
                if ex_type == "docker_exec":
                    self._terminate_docker_exec(worker_id, execution_id, details, container_manager)
                elif ex_type == "scoped_container":
                    self._terminate_scoped_container(worker_id, execution_id, details, container_manager)
                elif ex_type == "subprocess":
                    self._terminate_subprocess(worker_id, execution_id, details)
                else:
                    _log("WARNING", "workspace.lifecycle",
                         f"terminate_all: execution {execution_id} has unknown "
                         f"type {ex_type!r} — skipping")
            except Exception as exc:  # one failure must not block the rest
                _log("WARNING", "workspace.lifecycle",
                     f"terminate_all: failed to terminate {execution_id}: {exc}")
        self._executions.clear()
        _log("INFO", "workspace.lifecycle",
             f"terminate_all(worker={worker_id}): tracker cleared")

    def _terminate_docker_exec(self, worker_id, execution_id, details, container_manager) -> None:
        container_id = details.get("container_id")
        exec_id = details.get("exec_id")
        if container_manager is None:
            _log("DEBUG", "workspace.lifecycle",
                 f"terminate_all: no container_manager — cannot terminate "
                 f"docker_exec {execution_id}")
            return
        exec_stop = getattr(container_manager, "exec_stop", None)
        try:
            if exec_stop is not None:
                _log("INFO", "workspace.lifecycle",
                     f"terminate_all: docker_exec {execution_id} — "
                     f"exec_stop(container={container_id}, exec={exec_id}, "
                     f"timeout={EXEC_KILL_GRACE})")
                exec_stop(container_id, exec_id, timeout=EXEC_KILL_GRACE)
            else:
                # Real ContainerManager has no exec_stop; stopping the
                # container terminates any exec running inside it.
                _log("INFO", "workspace.lifecycle",
                     f"terminate_all: docker_exec {execution_id} — no exec_stop "
                     f"API, stopping container {container_id}")
                container_manager.stop(container_id)
        except Exception as exc:
            _log("WARNING", "workspace.lifecycle",
                 f"terminate_all: docker_exec {execution_id} termination failed "
                 f"({exc}) — force-removing container {container_id}")
            try:
                container_manager.remove(container_id)
            except Exception as remove_exc:
                _log("WARNING", "workspace.lifecycle",
                     f"terminate_all: force-remove of {container_id} failed: {remove_exc}")

    def _terminate_scoped_container(self, worker_id, execution_id, details, container_manager) -> None:
        container_id = details.get("container_id")
        if container_manager is None:
            _log("DEBUG", "workspace.lifecycle",
                 f"terminate_all: no container_manager — cannot stop scoped "
                 f"container for {execution_id}")
            return
        _log("INFO", "workspace.lifecycle",
             f"terminate_all: scoped_container {execution_id} — stop({container_id})")
        container_manager.stop(container_id)

    def _terminate_subprocess(self, worker_id, execution_id, details) -> None:
        pid = details.get("pid", details.get("process_id"))
        if pid is None:
            _log("WARNING", "workspace.lifecycle",
                 f"terminate_all: subprocess {execution_id} has no pid — skipping")
            return
        try:
            try:
                os.killpg(int(pid), signal.SIGTERM)
                _log("INFO", "workspace.lifecycle",
                     f"terminate_all: subprocess {execution_id} — "
                     f"killpg({pid}, SIGTERM)")
            except (ProcessLookupError, PermissionError, OSError):
                os.kill(int(pid), signal.SIGTERM)
                _log("INFO", "workspace.lifecycle",
                     f"terminate_all: subprocess {execution_id} — "
                     f"kill({pid}, SIGTERM)")
            self._escalate_subprocess_kill(int(pid))
        except Exception as exc:
            _log("WARNING", "workspace.lifecycle",
                 f"terminate_all: subprocess {execution_id} kill failed: {exc}")

    def _escalate_subprocess_kill(self, pid: int) -> None:
        """SIGKILL the process group if it is still alive after the grace period.

        Polls for process-group death with ``os.killpg(pid, 0)`` every 50ms up
        to ``kill_grace_seconds``; only escalates to SIGKILL when the group is
        still alive. A dead group (``ProcessLookupError``) returns immediately
        so a successful SIGTERM never triggers an extra SIGKILL.
        """
        grace = getattr(self, "kill_grace_seconds", EXEC_KILL_GRACE)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                return  # already dead
            except OSError:
                pass
            time.sleep(0.05)
        try:
            os.killpg(pid, signal.SIGKILL)
            _log("INFO", "workspace.lifecycle",
                 f"subprocess {pid} still alive after {grace}s grace — "
                 f"killpg({pid}, SIGKILL)")
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, signal.SIGKILL)
                _log("INFO", "workspace.lifecycle",
                     f"subprocess {pid} still alive after {grace}s grace — "
                     f"kill({pid}, SIGKILL)")
            except Exception as exc:
                _log("WARNING", "workspace.lifecycle",
                     f"subprocess {pid} SIGKILL escalation failed: {exc}")


class WorkerSupervisor:
    """State machine + correlation-ID queue around a worker's query handling.

    ``query_handler`` is intentionally NOT a constructor parameter: the
    integration layer assigns it after construction
    (``supervisor.query_handler = callable(query, query_id)``) so the
    supervisor can be built lazily and the handler wired to the existing
    worker loop.
    """

    def __init__(
        self,
        worker_id: str,
        container_manager: Any,
        resource_container_manager: Any,
        *,
        feature_flag_check: Optional[Callable[[], bool]] = None,
        session_id: Optional[str] = None,
        permissions_provider: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.worker_id = worker_id
        self._container_manager = container_manager
        self._resource_container_manager = resource_container_manager
        self.session_id = session_id
        self.permissions_provider = permissions_provider

        if feature_flag_check is None:
            cfg = _get_session_config(container_manager)
            feature_flag_check = lambda: bool(
                cfg.get("use_workspace_lifecycle_manager", False)
            )
        self._feature_flag_check = feature_flag_check

        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._state = WorkerState.IDLE
        self._paused_intentional = False
        self._timeout_triggered = False
        self.execution_tracker = ExecutionTracker()
        self._output_queue: "queue.Queue[tuple[Optional[str], Any]]" = queue.Queue()
        self._query_id: Optional[str] = None
        self._pending_query: Optional[str] = None
        # Assigned by the integration layer (not a constructor param).
        self.query_handler: Optional[Callable[[Any, str], None]] = None

        # Soft-timeout warning (80% of the wait budget), emitted once per query.
        self.soft_timeout_warning_emitted = False
        self.soft_timeout_warning_callback: Optional[Callable[[str], None]] = None
        self._query_started_at: Optional[float] = None
        # Bounded per-query outcome log (abandoned/completed) used by the
        # worker integration to prune stale attempts before merging context.
        self._query_log: "collections.deque" = collections.deque(maxlen=2)

        if not self._feature_flag_check():
            _log("WARNING", "workspace.lifecycle",
                 f"WorkerSupervisor({worker_id}) constructed while "
                 f"use_workspace_lifecycle_manager is OFF — the integration "
                 f"layer must NOT use this supervisor (old path runs).")

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------
    # Contract: validate + mutate under ``self._lock``; any terminate_all
    # side-effect runs AFTER the lock is released (never while holding it).

    def transition_busy(self) -> str:
        """IDLE|PAUSED|TIMED_OUT -> BUSY; returns the new query id."""
        with self._lock:
            if self._state not in (
                WorkerState.IDLE,
                WorkerState.PAUSED,
                WorkerState.TIMED_OUT,
            ):
                raise StateMachineError(
                    f"transition_busy: invalid from state {self._state.value}"
                )
            self._state = WorkerState.BUSY
            self._timeout_triggered = False
            self.soft_timeout_warning_emitted = False
            self._query_started_at = time.monotonic()
            self._drain_output_queue()
            self._query_id = QUERY_ID_PREFIX + uuid.uuid4().hex
            return self._query_id

    def transition_idle(self) -> bool:
        """BUSY -> IDLE; clears the execution tracker."""
        with self._lock:
            if self._state != WorkerState.BUSY:
                raise StateMachineError(
                    f"transition_idle: invalid from state {self._state.value}"
                )
            self._state = WorkerState.IDLE
            self._query_id = None
        self.execution_tracker.clear()
        return True

    def transition_pause(self, intentional: bool = True) -> bool:
        """IDLE|BUSY -> PAUSED. A BUSY query becomes the pending query."""
        with self._lock:
            if self._state not in (WorkerState.IDLE, WorkerState.BUSY):
                raise StateMachineError(
                    f"transition_pause: invalid from state {self._state.value}"
                )
            if self._state == WorkerState.BUSY:
                self._pending_query = self._query_id
            self._paused_intentional = intentional
            self._state = WorkerState.PAUSED
        return True

    def transition_timeout(self) -> bool:
        """BUSY -> TIMED_OUT; terminates tracked executions (after unlock)."""
        with self._lock:
            if self._state != WorkerState.BUSY:
                raise StateMachineError(
                    f"transition_timeout: invalid from state {self._state.value}"
                )
            self._state = WorkerState.TIMED_OUT
            self._timeout_triggered = True
            self._record_query_outcome(self._query_id, abandoned=True)
        self._terminate_all()
        return True

    def transition_stopping(self) -> bool:
        """BUSY|PAUSED|TIMED_OUT -> STOPPING; terminates executions (after unlock)."""
        with self._lock:
            if self._state not in (
                WorkerState.BUSY,
                WorkerState.PAUSED,
                WorkerState.TIMED_OUT,
            ):
                raise StateMachineError(
                    f"transition_stopping: invalid from state {self._state.value}"
                )
            self._state = WorkerState.STOPPING
            self._record_query_outcome(self._query_id, abandoned=True)
        self._terminate_all()
        return True

    def transition_stopped(self) -> bool:
        """STOPPING -> STOPPED; final cleanup (clear tracker + pending state)."""
        with self._lock:
            if self._state != WorkerState.STOPPING:
                raise StateMachineError(
                    f"transition_stopped: invalid from state {self._state.value}"
                )
            self._state = WorkerState.STOPPED
            self._pending_query = None
            self._query_id = None
        self.execution_tracker.clear()
        return True

    def transition_resume(self) -> bool:
        """PAUSED -> IDLE (no pending query) or PAUSED -> BUSY (pending restored)."""
        with self._lock:
            if self._state != WorkerState.PAUSED:
                raise StateMachineError(
                    f"transition_resume: invalid from state {self._state.value}"
                )
            pending = self._pending_query
            self._pending_query = None
            self._paused_intentional = False
            if pending is not None:
                self._state = WorkerState.BUSY
                self._query_id = pending
                self._timeout_triggered = False
            else:
                self._state = WorkerState.IDLE
        return True

    # ------------------------------------------------------------------
    # Query processing
    # ------------------------------------------------------------------

    def process_query(self, query: Any, timeout: Optional[float] = None) -> Any:
        """Run one query through the handler and return the matching reply.

        Raises ``StateMachineError`` while BUSY (one query at a time) or
        STOPPING/STOPPED. A PAUSED+intentional supervisor is auto-resumed and
        re-paused when the query completes. Waits up to ``timeout`` (or
        ``SOFT_TIMEOUT``) for a reply whose query id matches; on timeout the
        supervisor transitions to TIMED_OUT and ``TimeoutError`` is raised.
        Replies with a different query id are discarded as stale.
        """
        with self._lock:
            state = self._state
            if state == WorkerState.BUSY:
                raise StateMachineError(
                    f"WorkerSupervisor({self.worker_id}) is BUSY — one query at a time"
                )
            if state in (WorkerState.STOPPING, WorkerState.STOPPED):
                raise StateMachineError(
                    f"WorkerSupervisor({self.worker_id}) is {state.value} — "
                    f"not accepting queries"
                )
            auto_resume = state == WorkerState.PAUSED and self._paused_intentional
            if auto_resume and self._pending_query is not None:
                # A previously interrupted query is superseded by this new one;
                # its eventual reply will be discarded by the correlation check.
                _log("WARNING", "workspace.lifecycle",
                     f"process_query({self.worker_id}): dropping pending query "
                     f"{self._pending_query} on auto-resume")
                self._pending_query = None

        if auto_resume:
            self.transition_resume()

        query_id = self.transition_busy()
        handler = self.query_handler
        if handler is not None:
            threading.Thread(
                target=self._run_handler, args=(query, query_id), daemon=True
            ).start()
        else:
            _log("WARNING", "workspace.lifecycle",
                 f"process_query({self.worker_id}): no query_handler set — "
                 f"query {query_id} will time out")

        wait_bound = timeout if timeout is not None else SOFT_TIMEOUT
        deadline = time.monotonic() + wait_bound
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.transition_timeout()
                raise TimeoutError(
                    f"query {query_id} timed out after {wait_bound}s"
                )
            if not self.soft_timeout_warning_emitted and remaining <= 0.2 * wait_bound:
                self._emit_soft_timeout_warning(query_id)
            try:
                qid, reply = self._output_queue.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue
            if qid != query_id:
                _log("DEBUG", "workspace.lifecycle",
                     f"discarding stale reply for {qid} (expecting {query_id})")
                continue
            self._finalize_query(query_id, reply, auto_resume)
            return reply

    def _run_handler(self, query: Any, query_id: str) -> None:
        """Run ``query_handler`` in the daemon thread; publish errors."""
        handler = self.query_handler
        if handler is None:
            return
        try:
            handler(query, query_id)
        except Exception as exc:
            _log("WARNING", "workspace.lifecycle",
                 f"query_handler failed for {query_id}: {exc}")
            self._publish_reply(query_id, {"error": str(exc)})

    def _finalize_query(self, query_id: str, reply: Any, auto_resume: bool) -> None:
        """BUSY -> IDLE (or re-pause when the query auto-resumed)."""
        with self._lock:
            state = self._state
            self._record_query_outcome(query_id, abandoned=False)
        try:
            if state == WorkerState.BUSY:
                self.transition_idle()
        except StateMachineError as exc:
            _log("DEBUG", "workspace.lifecycle",
                 f"finalize {query_id}: idle transition skipped ({exc})")
        if auto_resume:
            try:
                self.transition_pause(intentional=True)
            except StateMachineError as exc:
                _log("DEBUG", "workspace.lifecycle",
                     f"finalize {query_id}: re-pause skipped ({exc})")

    def _publish_reply(self, query_id: str, reply: Any) -> None:
        """Publish a (query_id, reply) tuple — hook for integration/tests."""
        self._output_queue.put((query_id, reply))

    # ------------------------------------------------------------------
    # Query outcome log (for abandoned-attempt pruning)
    # ------------------------------------------------------------------

    @property
    def current_query_id(self) -> Optional[str]:
        """Query id of the in-flight query (None when idle)."""
        with self._lock:
            return self._query_id

    def _record_query_outcome(self, query_id: Optional[str], abandoned: bool) -> None:
        """Record an outcome for ``query_id`` in the bounded log (under lock).

        Dedupes: re-recording an id already in the log only updates the
        existing entry, so a query that is recorded twice (e.g. stopped after
        timing out) stays a single row.
        """
        if not query_id:
            return
        for entry in self._query_log:
            if entry["query_id"] == query_id:
                if abandoned:
                    entry["abandoned"] = True
                    entry.pop("completed_at", None)
                else:
                    entry["abandoned"] = False
                    entry["completed_at"] = time.monotonic()
                return
        self._query_log.append(
            {
                "query_id": query_id,
                "started_at": time.monotonic(),
                "completed_at": None if abandoned else time.monotonic(),
                "abandoned": abandoned,
            }
        )

    def abandoned_query_ids(self) -> list:
        """Query ids recorded as abandoned (most recent first)."""
        with self._lock:
            return [e["query_id"] for e in reversed(self._query_log) if e["abandoned"]]

    def completed_query_ids(self) -> list:
        """Query ids recorded as completed (most recent first)."""
        with self._lock:
            return [e["query_id"] for e in reversed(self._query_log) if not e["abandoned"]]

    def _emit_soft_timeout_warning(self, query_id: str) -> None:
        """Emit the 80%-budget soft-timeout warning exactly once per query."""
        with self._lock:
            if self.soft_timeout_warning_emitted:
                return
            self.soft_timeout_warning_emitted = True
        _log("WARNING", "workspace.lifecycle",
             f"[WLM] soft timeout warning (80% of budget) query_id={query_id}")
        callback = self.soft_timeout_warning_callback
        if callback is not None:
            try:
                callback(query_id)
            except Exception as exc:
                _log("WARNING", "workspace.lifecycle",
                     f"soft_timeout_warning_callback failed for {query_id}: {exc}")

    # ------------------------------------------------------------------
    # Public control API
    # ------------------------------------------------------------------

    def pause(self, intentional: bool = True) -> bool:
        return self.transition_pause(intentional=intentional)

    def resume(self) -> bool:
        return self.transition_resume()

    def stop(self) -> None:
        """STOPPING then STOPPED (terminates executions on the way)."""
        self.transition_stopping()
        self.transition_stopped()

    def status_report(self) -> dict:
        with self._lock:
            state = self._state
            query_id = self._query_id
            paused_intentional = self._paused_intentional
            timeout_triggered = self._timeout_triggered
        return {
            "state": state,
            "is_alive": state not in (WorkerState.STOPPING, WorkerState.STOPPED),
            "query_id": query_id,
            "active_executions": self.execution_tracker.active_count(),
            "paused_intentional": paused_intentional,
            "timeout_triggered": timeout_triggered,
        }

    # ------------------------------------------------------------------
    # Container requests (resource-container guard)
    # ------------------------------------------------------------------

    def request_container(
        self,
        permissions: Any,
        *,
        worker_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> dict:
        """Request a container, refusing resource-container requests.

        Delegates to ``container_manager.start(image=..., name=..., note=...)``
        — the real ContainerManager ``start()`` API (permissions are already
        applied to the manager at construction; only image/name/note are
        accepted here).
        """
        if self._is_resource_container_request(permissions):
            raise PermissionError(
                "Resource containers (git, tm-res-*) are reserved for the main "
                "agent and cannot be requested by a worker."
            )
        cm = self._container_manager
        if cm is None:
            raise RuntimeError(
                f"WorkerSupervisor({self.worker_id}) has no container_manager — "
                f"request_container unavailable"
            )
        if isinstance(permissions, dict):
            image = permissions.get("image")
            name = permissions.get("name")
            note = permissions.get("note")
        else:
            image, name, note = permissions, None, None
        # Phase 3: with the registry active the registry owns creation and the
        # container limit; the legacy ContainerManager.start() path runs only
        # when the registry is inactive.
        if is_registry_active(_get_session_config(cm)):
            registry = get_active_registry(_get_session_config(cm))
            return registry.request_container(
                worker_id or self.worker_id,
                session_id or self.worker_id,
                permissions if isinstance(permissions, dict) else {},
                image=image,
            )
        return cm.start(image=image, name=name, note=note)

    def release_container(self, container_id: str) -> dict:
        """Release a container via the real ``container_manager.stop()`` API."""
        cm = self._container_manager
        if cm is None:
            raise RuntimeError(
                f"WorkerSupervisor({self.worker_id}) has no container_manager — "
                f"release_container unavailable"
            )
        # Phase 3: with the registry active, release goes through the registry
        # (name-keyed destroy); containers not tracked by the registry fall
        # back to the legacy stop path.
        if is_registry_active(_get_session_config(cm)):
            registry = get_active_registry(_get_session_config(cm))
            name = None
            for handle in registry.list_all() or []:
                if handle.get("id") == container_id or handle.get("name") == container_id:
                    name = handle.get("name")
                    break
            if name is not None:
                registry.destroy_container(name)
                return {"status": "stopped", "container_id": container_id,
                        "name": name}
        return cm.stop(container_id)

    def _is_resource_container_request(self, permissions_or_image: Any) -> bool:
        """True when the request names the resource image or a resource container.

        Resource containers follow the ``tm-res-<sha256[:12]>-git`` convention
        (ResourceContainerManager.container_name).
        """
        values: list = []
        if isinstance(permissions_or_image, dict):
            for key in ("image", "name"):
                value = permissions_or_image.get(key)
                if value is not None:
                    values.append(str(value))
        else:
            values.append(str(permissions_or_image))
        for value in values:
            if value == RESOURCE_IMAGE_TAG:
                return True
            if value.startswith(_RESOURCE_NAME_PREFIX) and value.endswith(_RESOURCE_NAME_SUFFIX):
                return True
        return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _drain_output_queue(self) -> None:
        """Drop queued stale replies (must be called under ``self._lock``)."""
        while True:
            try:
                stale = self._output_queue.get_nowait()
            except queue.Empty:
                return
            stale_qid = stale[0] if isinstance(stale, tuple) and stale else stale
            _log("INFO", "workspace.lifecycle",
                 f"[WLM-DRAIN] query_id={stale_qid} state={self._state.value} "
                 f"dropping stale reply")

    def _terminate_all(self) -> None:
        """Run the execution tracker's terminate_all (no lock held)."""
        self.execution_tracker.terminate_all(
            self.worker_id,
            self._container_manager,
            self._resource_container_manager,
        )
