# tools/container_control.py
"""Per-session container control tools wrapping ContainerManager.

These tools expose a persistent, composable container lifecycle to the agent:

- ``ContainerStartTool``   - start (or reuse) a container for the session
- ``ContainerExecTool``    - run a shell command inside an existing container
- ``ContainerStopTool``    - stop a container (idempotent)
- ``ContainerStatusTool``  - report a container's status
- ``ContainerListTool``    - list this session's containers
- ``ContainerBuildTool``   - build a Docker image from the host workspace
- ``ContainerLogsTool``    - fetch a container's stdout/stderr logs

Unlike ``DockerCodeRunner`` (start -> exec -> stop in a single call), these
tools *compose across calls*: ``ContainerStartTool`` leaves the container
running so the agent can ``ContainerExecTool`` into it repeatedly, then
``ContainerStopTool`` shuts it down. Each tool call constructs a fresh
``ContainerManager``, so cross-call reuse is achieved through the
``thoughtmachine.container_name`` / ``thoughtmachine.workspace_id`` docker
labels (same workspace/name) rather than the manager's in-memory registry.

``cleanup_workspace()`` in ``container_manager`` remains available as a
belt-and-braces sweep that stops and removes every container carrying a
workspace label (used when a workspace is decommissioned).

Security posture is identical to ``DockerCodeRunner``: Docker isolation with a
read-only root filesystem, dropped capabilities, no-new-privileges, non-root
user (1000:1000), memory + CPU quotas, and network disabled unless session
permissions allow a bridge network.
"""

import json
import os
import time
from typing import Any, ClassVar, Dict, List, Literal, Optional

from pydantic import Field

from .base import ToolBase

# Worker-name context var (stdlib-only leaf module — no circular import).
# Falls back to None so the container tools keep working outside a worker turn.
try:
    from agent.core.worker_context import current_worker_name
except ImportError:

    def current_worker_name():  # type: ignore
        return None

# Guarded Docker SDK import (mirrors container_manager): keeps this module
# importable when the docker package is missing so the rest of the agent loads.
try:
    import docker
    from docker.errors import APIError, DockerException, NotFound
    DOCKER_AVAILABLE = True
except ImportError:
    DOCKER_AVAILABLE = False
    docker = None
    APIError = Exception
    DockerException = Exception
    NotFound = Exception

try:
    from infra.container_manager import ContainerManager
except ImportError:
    ContainerManager = None

# Guarded security import for the DockerSetupError fallback (mirrors
# docker_code_runner). Container control tools report failures as JSON via
# RuntimeError, so this is kept for import parity with the runner.
try:
    from thoughtmachine.security import DockerSetupError  # noqa: F401
    SECURITY_AVAILABLE = True
except ImportError:
    SECURITY_AVAILABLE = False

    class DockerSetupError(RuntimeError):  # noqa: F401
        """Raised when Docker sandbox setup fails (fallback if thoughtmachine.security is unavailable)."""
        pass


def _json_response(success: bool, **fields) -> str:
    """Build a compact JSON response for the container control tools."""
    response = {"success": success, **fields}
    # Drop the optional error key when it is empty/falsy.
    if not response.get("error"):
        response.pop("error", None)
    return json.dumps(response, indent=2)


class _ContainerControlBase(ToolBase):
    """Shared plumbing for the per-session container control tools."""

    required_categories: ClassVar[List[str]] = ["container:true"]

    def _make_manager(self):
        """Build a ContainerManager bound to the current session/workspace.

        Raises RuntimeError when Docker is unavailable, ContainerManager cannot
        be imported, or the workspace path cannot be resolved (never falls back
        to cwd, which may be outside the workspace).
        """
        if not DOCKER_AVAILABLE:
            raise RuntimeError(
                "Docker Python SDK not installed. Install with 'pip install docker'."
            )
        if ContainerManager is None:
            raise RuntimeError(
                "Could not import ContainerManager. Make sure docker package is "
                "installed and infra.container_manager.py exists."
            )
        ws = self._resolve_registry_workspace()
        if ws is None:
            # Never fall back to cwd: the current directory may be outside the
            # workspace, and mounting it would leak host files into the container.
            raise RuntimeError(
                "workspace path could not be resolved; refusing to fall back to cwd"
            )
        workspace_id = None
        try:
            from thoughtmachine.workspace_capabilities import resolve_workspace_id
            workspace_id = resolve_workspace_id(ws)
        except Exception:
            pass
        return ContainerManager(
            workspace_path=ws,
            session_id=getattr(self, "session_id", None),
            workspace_id=workspace_id,
            session_permissions=getattr(self, "session_permissions", None),
            image=getattr(self, "image", None) or "agent-executor",
            mem_limit=getattr(self, "mem_limit", "512m"),
            cpu_quota=getattr(self, "cpu_quota", 50000),
        )

    def _respond(self, success: bool, **fields) -> str:
        """Serialize a JSON response through the standard truncation path."""
        return self._truncate_output(_json_response(success, **fields))


class ContainerStartTool(_ContainerControlBase):
    """Start (or reuse) a persistent container for the current session.

    Unlike DockerCodeRunner, this tool does NOT stop the container afterwards:
    it leaves the container running so subsequent ContainerExecTool calls can
    run commands against it. Stop it explicitly with ContainerStopTool (or rely
    on the session cleanup sweep).

    Container reuse: if a container with the same name already exists for this
    session (auto-generated from the workspace + session, or the provided
    ``name``), it is reused instead of creating a fresh one. Reuse works across
    tool calls and manager instances via the ``thoughtmachine.*`` docker labels.

    Security features:
    - Docker isolation with read-only root filesystem
    - Dropped Linux capabilities + no-new-privileges
    - No network access by default (permission-gated)
    - Non-root user execution (uid 1000:1000)
    - Memory and CPU quotas (mem_limit / cpu_quota)

    Sticky notes: the optional ``note`` field attaches a note to the container.
    Notes live on a per-workspace vault bulletin board
    (``<vault_root>/workspaces/<workspace_id>/container_notes.json``), never in
    Docker labels (docker has no label-update API, so labels are immutable
    after create on a stock engine). On a fresh create the note is written to
    the bulletin board; on reuse a new note overwrites the stored entry, and
    the RESPONSE carries the new note value regardless.

    Returns JSON with structure:
    {
      "success": bool,
      "container_id": str,
      "name": str,
      "status": "created" | "reused",
      "note": str,
      "duration": float,
      "error": str (optional)
    }
    """
    tool: Literal["ContainerStartTool"] = "ContainerStartTool"

    image: str = Field(
        default="agent-executor",
        description="Docker image name (default: agent-executor)"
    )
    name: Optional[str] = Field(
        default=None,
        description="Optional container name. If omitted, an auto-generated per-session name is used (agent-exec-<workspace-hash>-<session-tag>)."
    )
    note: Optional[str] = Field(
        default=None,
        description="Optional sticky note attached to the container. Stored in the per-workspace vault bulletin board (container_notes.json), not in Docker labels. On reuse, overwrites the stored note and returns the new value."
    )
    mem_limit: str = Field(
        default="512m",
        description="Memory limit (e.g., '512m', '1g')"
    )
    cpu_quota: int = Field(
        default=50000,
        description="CPU quota in microseconds (default 50000 = 50ms per 100ms period)"
    )
    worker_name: Optional[str] = Field(
        default=None,
        description=(
            "[internal] Name of the worker sub-agent that created this container "
            "(injected by ToolExecutor from the worker context var; stamped as the "
            "``thoughtmachine.worker`` docker label on fresh creates so worker "
            "teardown can reclaim it)."
        ),
    )

    def execute(self) -> str:
        start_time = time.time()
        try:
            manager = self._make_manager()
            info = manager.start(
                image=self.image,
                name=self.name,
                note=self.note,
                worker_name=getattr(self, "worker_name", None) or current_worker_name(),
            )
            if "error" in info:
                return self._respond(
                    False,
                    error=info["error"],
                    duration=time.time() - start_time,
                )
            return self._respond(
                True,
                container_id=info["id"],
                name=info["name"],
                status=info["status"],
                note=info.get("note"),
                duration=time.time() - start_time,
            )
        except RuntimeError as e:
            return self._respond(False, error=str(e), duration=time.time() - start_time)
        except Exception as e:
            return self._respond(
                False,
                error=f"Unexpected error: {e}",
                duration=time.time() - start_time,
            )


class ContainerExecTool(_ContainerControlBase):
    """Run a shell command inside an existing container.

    Executes ``command`` inside the container identified by ``container_id``
    (previously returned by ContainerStartTool). The command is passed to
    /bin/sh -c with a timeout; on timeout the container is killed and the tool
    returns exit_code -2 with timed_out=True.

    This tool does NOT stop the container: the caller owns the container
    lifecycle (start -> exec... -> stop).

    Workspace access:
    - Container workspace mounted at /workspace (session volume)
    - Working directory defaults to /workspace (other directories are created if missing)
    - Optional environment variables

    Returns JSON with structure:
    {
      "success": bool,
      "exit_code": int,
      "stdout": str,
      "stderr": str,
      "command": str,
      "duration": float,
      "timed_out": bool,
      "error": str (optional)
    }
    """
    required_categories: ClassVar[List[str]] = ["container:true", "filesystem:write"]
    tool: Literal["ContainerExecTool"] = "ContainerExecTool"

    container_id: str = Field(
        ...,
        min_length=1,
        description="ID or name of the container to execute the command in (returned by ContainerStartTool)."
    )
    command: str = Field(
        ...,
        min_length=1,
        description="Shell command to execute inside the container (passed to /bin/sh -c)"
    )
    timeout: int = Field(
        default=30,
        description="Maximum execution time in seconds (default 30)"
    )
    workdir: str = Field(
        default="/workspace",
        description="Working directory inside the container (default: /workspace)"
    )
    environment: Optional[Dict[str, str]] = Field(
        default=None,
        description="Environment variables to set inside container (key=value)"
    )

    def execute(self) -> str:
        start_time = time.time()
        try:
            manager = self._make_manager()
            result = manager.exec(
                self.container_id,
                command=self.command,
                timeout=self.timeout,
                workdir=self.workdir,
                environment=self.environment,
            )
            timed_out = result["exit_code"] == -2
            return self._respond(
                result["exit_code"] == 0,
                exit_code=result["exit_code"],
                stdout=result["stdout"],
                stderr=result["stderr"],
                command=self.command,
                duration=time.time() - start_time,
                timed_out=timed_out,
            )
        except TimeoutError:
            return self._respond(
                False,
                exit_code=-2,
                timed_out=True,
                error=f"Command timed out after {self.timeout} seconds",
                duration=time.time() - start_time,
            )
        except RuntimeError as e:
            return self._respond(
                False,
                exit_code=-1,
                error=str(e),
                duration=time.time() - start_time,
            )
        except Exception as e:
            return self._respond(
                False,
                exit_code=-1,
                error=f"Unexpected error: {e}",
                duration=time.time() - start_time,
            )


class ContainerStopTool(_ContainerControlBase):
    """Stop a container started by ContainerStartTool.

    Idempotent: stopping an already-stopped container reports status "stopped";
    a container that no longer exists reports "missing". This tool never
    raises — failures are returned in the JSON response.

    Returns JSON with structure:
    {
      "success": bool,
      "container_id": str,
      "status": "stopped" | "missing" | "error",
      "name": str (optional),
      "error": str (optional),
      "duration": float
    }
    """
    tool: Literal["ContainerStopTool"] = "ContainerStopTool"

    container_id: str = Field(
        ...,
        min_length=1,
        description="ID or name of the container to stop (returned by ContainerStartTool)."
    )

    def execute(self) -> str:
        start_time = time.time()
        try:
            manager = self._make_manager()
            result = manager.stop(self.container_id)
            return self._respond(
                result.get("status") == "stopped",
                container_id=self.container_id,
                status=result.get("status"),
                name=result.get("name"),
                error=result.get("error"),
                duration=time.time() - start_time,
            )
        except RuntimeError as e:
            return self._respond(False, error=str(e), duration=time.time() - start_time)
        except Exception as e:
            return self._respond(
                False,
                error=f"Unexpected error: {e}",
                duration=time.time() - start_time,
            )


class ContainerStatusTool(_ContainerControlBase):
    """Report the status of a container.

    Returns the current state of the container identified by ``container_id``
    (running/exited/created/missing), including uptime and memory usage when
    available. This tool never raises — failures are returned in the JSON
    response.

    Returns JSON with structure:
    {
      "success": bool,
      "container_id": str,
      "name": str,
      "status": str,
      "uptime_seconds": int | null,
      "memory_usage_bytes": int | null,
      "error": str (optional),
      "duration": float
    }
    """
    tool: Literal["ContainerStatusTool"] = "ContainerStatusTool"

    container_id: str = Field(
        ...,
        min_length=1,
        description="ID or name of the container to inspect."
    )

    def execute(self) -> str:
        start_time = time.time()
        try:
            manager = self._make_manager()
            result = manager.status(self.container_id)
            return self._respond(True, **result, duration=time.time() - start_time)
        except RuntimeError as e:
            return self._respond(False, error=str(e))
        except Exception as e:
            return self._respond(False, error=f"Unexpected error: {e}")


class ContainerListTool(_ContainerControlBase):
    """List the containers currently tracked for this workspace.

    Queries the Docker daemon for every container (running or not) carrying
    this workspace's ``thoughtmachine.workspace_id`` label — the exact label
    source ``ContainerStartTool`` applies — so containers started by other
    workspaces, or unlabeled ones, never appear. This tool never raises —
    failures are returned in the JSON response.

    Returns JSON with structure:
    {
      "success": bool,
      "containers": [
        {
          "container_id": str,
          "name": str,
          "image": str | null,
          "status": str,
          "uptime_seconds": int | null,
          "workspace_id": str,
          "note": str
        }
      ],
      "count": int,
      "duration": float,
      "error": str (optional)
    }
    """
    tool: Literal["ContainerListTool"] = "ContainerListTool"

    def execute(self) -> str:
        start_time = time.time()
        try:
            manager = self._make_manager()
            containers = manager.list_containers()
            return self._respond(
                True,
                containers=containers,
                count=len(containers),
                duration=time.time() - start_time,
            )
        except RuntimeError as e:
            return self._respond(False, error=str(e), duration=time.time() - start_time)
        except Exception as e:
            return self._respond(
                False,
                error=f"Unexpected error: {e}",
                duration=time.time() - start_time,
            )


class ContainerBuildTool(_ContainerControlBase):
    """Build a Docker image from the vault-managed Dockerfile.

    Vault-gated: always builds from the vault-managed ``<workspace>/Dockerfile``
    (the resolved registry workspace root — no ``dockerfile_path`` override).
    The build context contains ONLY that Dockerfile: it is copied into a
    temporary build directory, so workspace files are NOT available during
    builds (``COPY .`` cannot see the local tree). When ``tag`` is omitted it
    is auto-generated from the workspace path (the same
    ``agent-executor-<hash>`` convention ``docker_executor`` uses). This tool
    never raises — failures are returned in the JSON response.

    Returns JSON with structure:
    {
      "success": bool,
      "image_tag": str,
      "build_log": str,
      "duration": float,
      "error": str (optional)
    }
    """
    tool: Literal["ContainerBuildTool"] = "ContainerBuildTool"

    tag: Optional[str] = Field(
        None,
        description="Image tag to build (auto-generated from the workspace path when absent).",
    )

    def execute(self) -> str:
        start_time = time.time()
        try:
            manager = self._make_manager()
            result = manager.build_image(tag=self.tag)
            return self._respond(
                True,
                **result,
                duration=time.time() - start_time,
            )
        except RuntimeError as e:
            return self._respond(False, error=str(e), duration=time.time() - start_time)
        except Exception as e:
            return self._respond(
                False,
                error=f"Unexpected error: {e}",
                duration=time.time() - start_time,
            )


class ContainerLogsTool(_ContainerControlBase):
    """Fetch the stdout/stderr logs of a running (or exited) container.

    Returns the last ``tail`` lines of the container's log output as separate
    ``stdout`` and ``stderr`` strings (each truncated to 100KB). ``since`` is
    passed through to Docker unmodified — it accepts a duration such as
    ``'10m'``, an RFC3339 timestamp, or a Unix timestamp. This tool never
    raises — failures are returned in the JSON response.

    Returns JSON with structure:
    {
      "success": bool,
      "stdout": str,
      "stderr": str,
      "duration": float,
      "error": str (optional)
    }
    """
    tool: Literal["ContainerLogsTool"] = "ContainerLogsTool"

    container_id: str = Field(
        ...,
        min_length=1,
        description="Container ID or name to fetch logs for.",
    )
    tail: int = Field(
        100,
        description="Number of log lines to fetch from the end (default: 100).",
    )
    since: Optional[str] = Field(
        None,
        description=(
            "Fetch logs emitted after this time — a duration (e.g. '10m'), "
            "RFC3339 timestamp, or Unix timestamp; passed through to Docker."
        ),
    )

    def execute(self) -> str:
        start_time = time.time()
        try:
            manager = self._make_manager()
            result = manager.get_logs(
                container_id=self.container_id,
                tail=self.tail,
                since=self.since,
            )
            return self._respond(
                True,
                **result,
                duration=time.time() - start_time,
            )
        except RuntimeError as e:
            return self._respond(False, error=str(e), duration=time.time() - start_time)
        except Exception as e:
            return self._respond(
                False,
                error=f"Unexpected error: {e}",
                duration=time.time() - start_time,
            )

