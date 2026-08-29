"""Execution tracking for worker lifecycle management (extracted).

Tracks in-flight executions (docker execs, scoped containers, subprocesses,
container execs) so they can be terminated when a worker times out or is
stopped. Extracted VERBATIM from ``infra.workspace_lifecycle_manager``
(``ExecutionTracker`` plus the module helpers it relies on) so the tracker
can be reused without importing the full workspace lifecycle manager.

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
tag is ``RESOURCE_IMAGE_TAG = "tm-resource-git"``.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional

try:
    from agent.logging import log as _agent_log
except ImportError:
    _agent_log = None

from agent.config.defaults import EXEC_KILL_GRACE

try:
    from infra.resource_container_manager import RESOURCE_IMAGE_TAG
except ImportError:  # pragma: no cover - defensive
    RESOURCE_IMAGE_TAG = "tm-resource-git"

# Resource container name convention (see ResourceContainerManager.container_name).
_RESOURCE_NAME_PREFIX = "tm-res-"
_RESOURCE_NAME_SUFFIX = "-git"

# Container ownership labels (same conventions as tools.workspace.worker):
# the ``thoughtmachine.worker`` label carries the owning worker's identity
# ("<session_id or 'unknown'>:<worker_name>") and the
# ``thoughtmachine.resource`` label marks shared workspace infrastructure
# (git checkouts / tooling images) that must NEVER be touched by worker
# teardown.
_WORKER_CONTAINER_LABEL = "thoughtmachine.worker"
_RESOURCE_CONTAINER_LABEL = "thoughtmachine.resource"

logger = logging.getLogger(__name__)


def _log(level: str, component: str, message: str) -> None:
    """Log via the agent logging facade when available, else stdlib logging."""
    if _agent_log is not None:
        _agent_log(level, component, message)
    else:
        getattr(logger, level.lower(), logger.info)(message)


def _container_info(container_manager: Any, container_id: Optional[str]):
    """Best-effort container lookup (labels/name/image dict or object).

    Tries ``inspect(container_id)`` first (duck-typed; the real
    ContainerManager has no public inspect, fakes may), then matches
    ``list_containers()`` entries by id or name. Returns None when the
    container cannot be found.
    """
    if container_manager is None or not container_id:
        return None
    try:
        inspect = getattr(container_manager, "inspect", None)
        if inspect is not None:
            result = inspect(container_id)
            if result is not None:
                return result
    except Exception:
        pass
    try:
        for entry in container_manager.list_containers() or []:
            if not isinstance(entry, dict):
                continue
            cid = entry.get("id") or entry.get("container_id") or entry.get("name")
            if cid and str(cid) == str(container_id):
                return entry
    except Exception:
        pass
    return None


def _is_worker_owned_container(container: Any, worker_id: str, session_id: Optional[str]) -> bool:
    """True when ``container`` belongs to ``worker_id`` (label ownership).

    Ownership is established by the EXACT value of the
    ``thoughtmachine.worker`` label: it must equal the bare worker id or the
    worker's owner identity ``<session_id>:<worker_id>``. Resource containers
    (``thoughtmachine.resource`` label, ``tm-res-*`` names, the
    ``tm-resource-git`` image) are shared workspace infrastructure and are
    NEVER worker-owned.
    """
    if container is None:
        return False
    labels = getattr(container, "labels", None)
    if labels is None and isinstance(container, dict):
        labels = container.get("labels")
    labels = labels or {}
    # Shared resource containers are never owned by any worker.
    if labels.get(_RESOURCE_CONTAINER_LABEL):
        return False
    owner = labels.get(_WORKER_CONTAINER_LABEL)
    if not owner:
        return False
    if str(owner) == str(worker_id):
        return True
    if session_id and str(owner) == f"{session_id}:{worker_id}":
        return True
    # Belt-and-braces resource-convention checks (dict/object shape).
    name = getattr(container, "name", None)
    if name is None and isinstance(container, dict):
        name = container.get("name")
    if name and str(name).startswith(_RESOURCE_NAME_PREFIX):
        return False
    image = getattr(container, "image", None)
    if image is None and isinstance(container, dict):
        image = container.get("image")
    if image == RESOURCE_IMAGE_TAG:
        return False
    return False


class ExecutionTracker:
    """Tracks in-flight executions so they can be terminated on timeout/stop.

    Execution details dict: ``{query_id, tool_call_id, container_id, exec_id,
    start_time, type}`` where ``type`` is one of ``docker_exec``,
    ``subprocess`` (may also carry ``pid``), ``scoped_container`` or
    ``container_exec`` (a process running inside a container — carries
    ``container_id`` and ``pid``).
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
        session_id: Optional[str] = None,
    ) -> None:
        """Terminate every tracked execution; then clear the tracker.

        - ``docker_exec``: ``exec_stop(container_id, exec_id, timeout=EXEC_KILL_GRACE)``
          when the manager exposes it, else ``stop(container_id)`` (real
          ContainerManager API — stopping the container kills its execs). On
          failure the container is force-removed via ``remove(container_id)``.
        - ``scoped_container``: ``stop(container_id)``.
        - ``subprocess``: ``os.killpg`` when the pid is a process group, else
          ``os.kill(pid, SIGTERM)``.
        - ``container_exec``: minimal ``docker exec <container> kill <pid>``
          (``exec_run`` when the manager exposes it, else the docker CLI).
          Without a pid, the container is stopped ONLY when it is
          worker-owned (label check) — resource/shared containers are never
          touched.

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
                elif ex_type == "container_exec":
                    self._terminate_container_exec(
                        worker_id, execution_id, details, container_manager, session_id
                    )
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

    def _terminate_container_exec(
        self,
        worker_id: str,
        execution_id: str,
        details: dict,
        container_manager: Any,
        session_id: Optional[str] = None,
    ) -> None:
        """Terminate a process running inside a container (minimal touch).

        With ``container_id`` + ``pid`` the minimal action is a
        ``docker exec <container> kill <pid>`` — the container itself keeps
        running and is never stopped. Without a pid the container is stopped
        ONLY when it is worker-owned (``thoughtmachine.worker`` label matches
        the worker); resource/shared containers are never touched.
        """
        container_id = details.get("container_id")
        pid = details.get("pid")
        if container_id and pid:
            self._docker_exec_kill(execution_id, container_id, int(pid), container_manager)
            return
        if not container_id:
            _log("WARNING", "workspace.lifecycle",
                 f"terminate_all: container_exec {execution_id} has neither "
                 f"container_id nor pid — skipping")
            return
        if container_manager is None:
            _log("WARNING", "workspace.lifecycle",
                 f"terminate_all: container_exec {execution_id} has no pid and "
                 f"no container_manager — skipping (cannot verify ownership)")
            return
        info = _container_info(container_manager, container_id)
        if _is_worker_owned_container(info, worker_id, session_id):
            _log("INFO", "workspace.lifecycle",
                 f"terminate_all: container_exec {execution_id} — no pid, "
                 f"worker-owned container {container_id} — stop({container_id})")
            container_manager.stop(container_id)
        else:
            _log("WARNING", "workspace.lifecycle",
                 f"terminate_all: container_exec {execution_id} has no pid and "
                 f"container {container_id} is not worker-owned — skipping "
                 f"(never touch resource/shared containers)")

    def _docker_exec_kill(self, execution_id, container_id, pid, container_manager) -> None:
        """Minimal in-container kill: ``docker exec <container> kill <pid>``.

        Prefers ``container_manager.exec_run`` when the manager exposes it,
        else shells out to the docker CLI. On failure only a warning is
        logged — the container is deliberately NOT stopped (minimal
        termination semantics).
        """
        exec_run = getattr(container_manager, "exec_run", None)
        try:
            if callable(exec_run):
                _log("INFO", "workspace.lifecycle",
                     f"terminate_all: container_exec {execution_id} — "
                     f"exec_run({container_id}, kill {pid})")
                exec_run(container_id, ["kill", str(pid)])
            else:
                _log("INFO", "workspace.lifecycle",
                     f"terminate_all: container_exec {execution_id} — "
                     f"docker exec {container_id} kill {pid}")
                subprocess.run(
                    ["docker", "exec", str(container_id), "kill", str(pid)],
                    capture_output=True,
                    timeout=EXEC_KILL_GRACE,
                )
        except Exception as exc:
            _log("WARNING", "workspace.lifecycle",
                 f"terminate_all: container_exec {execution_id} docker exec "
                 f"kill failed ({exc}) — container left running (minimal touch)")

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
