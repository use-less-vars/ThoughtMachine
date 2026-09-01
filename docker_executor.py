"""
docker_executor.py — Docker container lifecycle and integrity verification.

Call chain for container integrity checks
══════════════════════════════════════════

When a session is loaded or configuration is changed, the container is
checked to ensure it still matches the current session permissions:

    WebAgentBridge.load_session() / WebAgentBridge.apply_config()
        → _maybe_re_sync_container()
            → verify_container_integrity()
                → _compute_container_config_from_permissions()
                    → get_workspace_capabilities() + get_effective_permissions()
                        (security/security_gate.py)

The unified function ``_compute_container_config_from_permissions`` is the
single source of truth for computing desired (network_mode, workspace_mode)
from session permissions. It is called by both ``verify_container_integrity``
and ``DockerExecutor._compute_container_config``, ensuring consistency
between integrity checks and container creation/recreation.

"""

from thoughtmachine.timeout_constants import IDLE_TIMEOUT_SECONDS
from agent.logging import log
from agent.config.defaults import CONTAINER_TYPE_FREE_USE, CONTAINER_TYPE_LABEL
import docker
import docker.types
import hashlib
import os
import time
import threading
import queue
import sys

# ── Audit log for network_mode debugging ───────────────────────────────────
from thoughtmachine.audit_logger import audit_event

_audit = lambda e, d: audit_event(e, d)  # module-level convenience
_audit("MODULE_LOAD", f"file={__file__} pid={os.getpid()}")


# ── Build log cache (thread-safe) ────────────────────────────────────────────
# Populated by _build_image() during Docker image builds; consumed by
# get_container_status() to return build logs to the frontend.
_build_log_cache: dict[str, str] = {}
_build_log_cache_lock = threading.Lock()
_build_in_progress: bool = False

# ── Executor build-drift tracking ─────────────────────────────────────
# Every executor image is labeled with a content hash of its build
# sources (repo requirements.txt + resources/default_dockerfile.txt) so an
# existing image is reused unless those sources actually changed. The
# algorithm is identical to infra/resource_container_manager.py so the two
# systems' hashes are comparable.
EXECUTOR_BUILD_HASH_LABEL = "thoughtmachine.build_hash"
_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
EXECUTOR_REQUIREMENTS = os.path.join(_REPO_ROOT, "requirements.txt")
EXECUTOR_RUNTIME_DOCKERFILE = os.path.join(_REPO_ROOT, "resources", "default_dockerfile.txt")


def _hash_executor_build_bytes(req_b: bytes, df_b: bytes) -> str:
    """Hash executor build sources (same algorithm as resource_container_manager)."""
    return hashlib.sha256(req_b + b"\n" + df_b).hexdigest()


def compute_executor_build_hash(requirements_path=None, dockerfile_path=None) -> str:
    """Compute the content hash of the executor image build sources.

    Args:
        requirements_path: path to requirements.txt (defaults to the repo one).
        dockerfile_path: path to the runtime Dockerfile (defaults to the repo one).

    Returns:
        sha256 hex digest of ``requirements_bytes + b"\n" + dockerfile_bytes``.

    Raises:
        OSError: If either source file is missing or unreadable.
    """
    req_path = requirements_path or EXECUTOR_REQUIREMENTS
    df_path = dockerfile_path or EXECUTOR_RUNTIME_DOCKERFILE
    with open(req_path, "rb") as f:
        req_b = f.read()
    with open(df_path, "rb") as f:
        df_b = f.read()
    return _hash_executor_build_bytes(req_b, df_b)


def _compute_image_tag(workspace_path: str) -> str:
    """Derive a deterministic Docker image tag from the workspace path.

    Different workspaces (e.g., worktrees) get different tags so they
    never share or conflict on one tag like `agent-executor:latest`.
    """
    path_hash = hashlib.sha256(workspace_path.encode()).hexdigest()[:16]
    return f"agent-executor-{path_hash}"


def _run_image_build(client, build_path, dockerfile, tag, nocache=False, on_line=None, labels=None):
    """Run ``client.api.build`` over a host directory, streaming log lines.

    Shared by :meth:`DockerExecutor._build_image` and
    ``ContainerManager.build_image`` so the build-stream handling (and its
    BuildError wrapping) lives in exactly one place.

    Args:
        client: docker client whose ``api.build`` performs the build.
        build_path: host path used as the build context.
        dockerfile: Dockerfile name within the build context.
        tag: image tag for the built image.
        nocache: if True, bypass the Docker layer cache.
        on_line: optional callback invoked with each non-empty log line.
        labels: optional dict of labels applied to the built image (e.g.
            ``{EXECUTOR_BUILD_HASH_LABEL: hash}``); only passed to the
            daemon when not None.

    Returns:
        (image_id, log_lines) where log_lines is the list of non-empty
        ``stream`` chunks (plus BuildError lines on failure).

    Raises:
        RuntimeError: If the build fails (BuildError wrapped with its logs) or
            the daemon returns no image ID.
    """
    log_lines: list[str] = []
    image_id = None
    try:
        build_kwargs = dict(
            path=build_path,
            dockerfile=dockerfile,
            tag=tag,
            rm=True,
            pull=True,
            nocache=nocache,
            decode=True,
        )
        if labels is not None:
            build_kwargs["labels"] = labels
        build_logs = client.api.build(**build_kwargs)
        for chunk in build_logs:
            if "stream" in chunk:
                line = chunk["stream"].strip()
                if line:
                    log_lines.append(line)
                    if on_line:
                        on_line(line)
            elif "aux" in chunk and "ID" in chunk["aux"]:
                image_id = chunk["aux"]["ID"]
    except docker.errors.BuildError as e:
        error_lines = [str(line) for line in (e.build_log or [])]
        log_lines.extend(error_lines)
        for line in error_lines:
            if on_line:
                on_line(line)
        raise RuntimeError(
            f"Docker build failed: {e}\n"
            f"Build logs:\n" + "\n".join(error_lines)
        ) from e

    if image_id is None:
        raise RuntimeError("Docker build completed but no image ID was returned")
    return image_id, log_lines


def _resolve_workspace_id(workspace_path: str):
    """Resolve workspace ID from a workspace path, returning None on failure."""
    try:
        from thoughtmachine.workspace_capabilities import resolve_workspace_id
        return resolve_workspace_id(workspace_path)
    except Exception:
        return None


def _compute_container_config_from_permissions(
    workspace_path: str,
    workspace_id,
    session_permissions,
) -> tuple:
    """Unified function: compute desired (network_mode, workspace_mode) from session permissions.

    Replaces both ``_compute_desired_config`` and ``DockerExecutor._compute_container_config``
    with a single standalone implementation that all callers share.

    Logic:
    1. If workspace_id and session_permissions are available, use the unified security gate
       (``get_effective_permissions``) to compute the config.
    2. If workspace_id is None but session_permissions are available, fall back to deriving
       directly from the session_permissions dict (with audit events for visibility).
    3. If neither is available, return safe defaults ("none", "ro").

    Args:
        workspace_path: Absolute path to the workspace (used for audit logging).
        workspace_id: Resolved workspace ID (str), or None — coerced to str.
        session_permissions: The session permissions dict, or None.

    Returns:
        Tuple of (network_mode: str, workspace_mode: str).
    """
    network_mode = "none"
    workspace_mode = "ro"

    # Normalize workspace_id to str: callers may pass a uuid.UUID object
    # (e.g. integration tests); pathlib.Path rejects non-str operands with
    # TypeError, which previously short-circuited the gate into fail-closed
    # ("none","ro") instead of falling through to session permissions.
    workspace_id = str(workspace_id) if workspace_id is not None else None

    if workspace_id and session_permissions is not None:
        try:
            from security.security_gate import (
                get_workspace_capabilities,
                get_effective_permissions,
            )
            from thoughtmachine.security import SessionPermissions
            caps = get_workspace_capabilities(workspace_id)
            eff = get_effective_permissions(SessionPermissions(**session_permissions), caps)

            if eff.get("network") is True or eff.get("network") == "write":
                network_mode = "bridge"
            else:
                network_mode = "none"

            fs = eff.get("filesystem", "read")
            workspace_mode = "rw" if fs in ("write", "full") else "ro"
        except Exception as e:
            log("WARN", "docker.security_gate",
                f"Gate lookup failed, using safe defaults: {e}")
            network_mode = "none"
            workspace_mode = "ro"
    elif session_permissions is not None:
        # workspace_id resolution failed — fall back to session permissions
        # (they have already been vetted by ToolExecutor.check_required_categories).
        sp = session_permissions
        net = sp.get("network", "banned")
        network_mode = "bridge" if net == "write" else "none"
        fs = sp.get("filesystem", "read")
        workspace_mode = "rw" if fs in ("write", "full") else "ro"
        log("WARNING", "docker.security_gate",
            f"workspace_id resolution failed for {workspace_path}; "
            f"falling back to session_permissions (network={net}, fs={fs}).")
        audit_event("FALLBACK_NETWORK_RESTRICTION",
                   f"workspace={workspace_path} "
                   f"workspace_id=None session_network={net} session_fs={fs}")

    audit_event("NETWORK_DECISION", f"workspace={workspace_path} network_mode={network_mode}")

    return network_mode, workspace_mode


def verify_container_integrity(
    workspace_path: str,
    session_permissions: dict = None,
) -> dict:
    """Check an existing container against the expected security config.

    If permissions have been tightened since the container was created, the
    container is stopped and removed so it will be recreated with the new
    settings on next use.

    This is called during:
      - Server startup (lifespan) to catch stale containers from previous runs
      - Session load to catch containers whose permissions have changed
        since the session was last active

    Args:
        workspace_path: Absolute path to the workspace.
        session_permissions: The session permissions dict (e.g. from a loaded
            session's config).  If None, the most restrictive defaults are used.

    Returns:
        dict with keys:
            - container_exists (bool | None): None if Docker unavailable
            - container_name (str | None)
            - matches_config (bool | None): None if no container
            - desired (dict): {"network": ..., "mode": ...}
            - actual (dict | None): {"network": ..., "mode": ...}
            - action_taken (str): "none", "removed", or "error"
            - mismatch_reason (str | None)
    """
    import hashlib
    import os

    workspace_path = os.path.abspath(workspace_path).rstrip("/")
    safe_name = hashlib.sha256(workspace_path.encode()).hexdigest()[:12]
    container_name = f"agent-exec-{safe_name}"

    # Resolve workspace_id for config computation
    workspace_id = _resolve_workspace_id(workspace_path)

    # Compute desired config via unified function
    desired_network, desired_mode = _compute_container_config_from_permissions(
        workspace_path, workspace_id, session_permissions
    )

    # Connect to Docker
    try:
        import docker
        client = docker.from_env()
    except Exception as exc:
        log("WARNING", "docker.verify_integrity",
            f"Cannot connect to Docker: {exc}")
        return {
            "container_exists": None,
            "container_name": container_name,
            "matches_config": None,
            "desired": {"network": desired_network, "mode": desired_mode},
            "actual": None,
            "action_taken": "error",
            "mismatch_reason": f"Docker unavailable: {exc}",
        }

    # Look up container
    try:
        container = client.containers.get(container_name)
        container.reload()
    except docker.errors.NotFound:
        log("DEBUG", "docker.verify_integrity",
            f"No existing container for {workspace_path}",
            {"container_name": container_name})
        return {
            "container_exists": False,
            "container_name": container_name,
            "matches_config": None,
            "desired": {"network": desired_network, "mode": desired_mode},
            "actual": None,
            "action_taken": "none",
            "mismatch_reason": None,
        }

    # Get actual config from running container
    actual_network = container.attrs["HostConfig"]["NetworkMode"]
    actual_mounts = container.attrs.get("Mounts", [])
    actual_mode = "ro"
    for m in actual_mounts:
        if m.get("Destination") == "/workspace":
            actual_mode = m.get("Mode", "ro")
            break

    # Compare
    if actual_network == desired_network and actual_mode == desired_mode:
        audit_event("INTEGRITY_CHECK",
                   f"workspace={workspace_path} container={container.id[:12]} "
                   f"network={actual_network} mode={actual_mode} status=match")
        log("INFO", "docker.verify_integrity",
            f"Container {container.id[:12]} matches expected config",
            {"container_name": container_name,
             "network": actual_network, "mode": actual_mode})
        return {
            "container_exists": True,
            "container_name": container_name,
            "matches_config": True,
            "desired": {"network": desired_network, "mode": desired_mode},
            "actual": {"network": actual_network, "mode": actual_mode},
            "action_taken": "none",
            "mismatch_reason": None,
        }

    # Mismatch — log warning, stop, and remove
    mismatch_reason = (
        f"network={actual_network}->{desired_network}, "
        f"mode={actual_mode}->{desired_mode}"
    )
    log("WARNING", "docker.verify_integrity",
        f"Container {container.id[:12]} config mismatch — removing",
        {"container_name": container_name,
         "actual_network": actual_network, "desired_network": desired_network,
         "actual_mode": actual_mode, "desired_mode": desired_mode})

    audit_event("VERIFY_RECREATE",
               f"workspace={workspace_path} container={container.id[:12]} "
               f"network={actual_network}->{desired_network} "
               f"mode={actual_mode}->{desired_mode}")

    try:
        container.stop(timeout=5)
        container.remove()
    except Exception as exc:
        log("ERROR", "docker.verify_integrity",
            f"Failed to remove mismatched container: {exc}")
        return {
            "container_exists": True,
            "container_name": container_name,
            "matches_config": False,
            "desired": {"network": desired_network, "mode": desired_mode},
            "actual": {"network": actual_network, "mode": actual_mode},
            "action_taken": "error",
            "mismatch_reason": mismatch_reason,
        }

    return {
        "container_exists": True,
        "container_name": container_name,
        "matches_config": False,
        "desired": {"network": desired_network, "mode": desired_mode},
        "actual": {"network": actual_network, "mode": actual_mode},
        "action_taken": "removed",
        "mismatch_reason": mismatch_reason,
    }


class DockerExecutor:
    def __init__(
        self,
        workspace_path: str,
        image: str = "agent-executor",
        network: str = "none",
        mem_limit: str = "1g",
        cpu_quota: int = 100000,
        force_rebuild: bool = False,
        idle_timeout: int = IDLE_TIMEOUT_SECONDS,
        session_permissions=None,
        workspace_id: str = None,
    ):
        # Normalize path: absolute, no trailing slash — ensures deterministic container naming
        self.workspace_path = os.path.abspath(workspace_path).rstrip('/')
        self.image = image or _compute_image_tag(self.workspace_path)
        self.network = network
        self.mem_limit = mem_limit
        self.cpu_quota = cpu_quota
        self.force_rebuild = force_rebuild
        self.idle_timeout = idle_timeout
        self.session_permissions = session_permissions
        self.workspace_id = workspace_id
        self.client = docker.from_env()
        self.container = None
        self.last_used = time.time()
        self._timeout_warning_printed = False

        if self.workspace_id is None:
            try:
                from thoughtmachine.workspace_capabilities import resolve_workspace_id
                self.workspace_id = resolve_workspace_id(self.workspace_path)
            except Exception:
                self.workspace_id = None

    def _compute_container_config(self):
        """Compute desired network_mode and workspace mount mode from session permissions.

        Thin wrapper that delegates to the unified standalone function
        ``_compute_container_config_from_permissions`` so all callers share
        a single implementation.

        Returns:
            Tuple of (network_mode: str, workspace_mode: str)
            where network_mode is "bridge" or "none"
            and workspace_mode is "rw" or "ro".
        """
        return _compute_container_config_from_permissions(
            self.workspace_path,
            self.workspace_id,
            self.session_permissions,
        )

    def _ensure_container(self):
        # Ensure the Docker image exists
        self._ensure_image()

        log("DEBUG", "tools.docker_executor.container",
            "_ensure_container called",
            {"workspace_path": self.workspace_path, "image": self.image,
             "has_container": self.container is not None,
             "force_rebuild": self.force_rebuild})

        # ── Fast path: self.container already exists and is running ──
        if self.container:
            try:
                self.container.reload()
                if self.container.status == "running":
                    # ── Compute desired config via unified gate ──
                    desired_network, desired_workspace_mode = self._compute_container_config()

                    # ── Read actual config from container ──
                    actual_network = self.container.attrs['HostConfig']['NetworkMode']
                    actual_mounts = self.container.attrs.get('Mounts', [])
                    actual_workspace_mode = "ro"
                    for m in actual_mounts:
                        if m.get('Destination') == '/workspace':
                            actual_workspace_mode = m.get('Mode', 'ro')
                            break

                    # ── Compare desired vs actual ──
                    if actual_network != desired_network or actual_workspace_mode != desired_workspace_mode:
                        log("INFO", "tools.docker_executor.container",
                            "Cached container config mismatch, recreating",
                            {"container_id": self.container.id[:12],
                             "actual_network": actual_network, "desired_network": desired_network,
                             "actual_mode": actual_workspace_mode, "desired_mode": desired_workspace_mode})
                        audit_event("CONTAINER_RECREATE_MISMATCH",
                                   f"workspace={self.workspace_path} container={self.container.id[:12]} "
                                   f"network={actual_network}->{desired_network} "
                                   f"mode={actual_workspace_mode}->{desired_workspace_mode} source=live_object")
                        try:
                            self.container.stop()
                            self.container.remove()
                        except docker.errors.NotFound:
                            pass
                        self.container = None
                    else:
                        log("DEBUG", "tools.docker_executor.container",
                            "Reusing running container (fast path — config match)",
                            {"container_id": self.container.id, "name": self.container.name})
                        audit_event("CONTAINER_REUSE_OK",
                                   f"workspace={self.workspace_path} container={self.container.id[:12]} source=live_object")
                        return
            except docker.errors.NotFound:
                self.container = None

        # ── Always compute desired config via unified gate ──
        network_mode, workspace_mode = self._compute_container_config()

        # ── Deterministic container name based on workspace path ──
        safe_name = hashlib.sha256(self.workspace_path.encode()).hexdigest()[:12]
        container_name = f"agent-exec-{safe_name}"

        # ── Try to find existing container by name ──
        existing = None
        try:
            existing = self.client.containers.get(container_name)
            existing.reload()
        except docker.errors.NotFound:
            pass

        # ── Force rebuild: always remove existing ──
        if self.force_rebuild and existing:
            try:
                existing.stop()
                existing.remove()
            except docker.errors.NotFound:
                pass
            existing = None
            audit_event("CONTAINER_RECREATE",
                       f"workspace={self.workspace_path} reason=force_rebuild")

        # ── Check if existing container matches desired config ──
        if existing is not None:
            current_network = existing.attrs['HostConfig']['NetworkMode']
            current_mounts = existing.attrs.get('Mounts', [])
            current_workspace_mode = "ro"
            for m in current_mounts:
                if m.get('Destination') == '/workspace':
                    current_workspace_mode = m.get('Mode', 'ro')
                    break

            config_mismatch = (
                current_network != network_mode or
                current_workspace_mode != workspace_mode
            )

            if config_mismatch:
                log("INFO", "tools.docker_executor.container",
                    "Container config mismatch, recreating",
                    {"container_id": existing.id[:12],
                     "current_network": current_network, "desired_network": network_mode,
                     "current_mode": current_workspace_mode, "desired_mode": workspace_mode})
                audit_event("CONTAINER_RECREATE_MISMATCH",
                           f"workspace={self.workspace_path} container={existing.id[:12]} "
                           f"network={current_network}->{network_mode} "
                           f"mode={current_workspace_mode}->{workspace_mode}")
                try:
                    existing.stop()
                    existing.remove()
                except docker.errors.NotFound:
                    pass
                existing = None
            else:
                try:
                    container_image_id = existing.attrs.get('Image', '')
                    current_image = self.client.images.get(self.image)
                    current_image_id = current_image.id
                    if container_image_id and current_image_id and container_image_id != current_image_id:
                        log("INFO", "tools.docker_executor.container",
                            "Container built from stale image, recreating",
                            {"container_id": existing.id[:12],
                             "container_image": container_image_id[:19] + "...",
                             "current_image": current_image_id[:19] + "..."})
                        audit_event("CONTAINER_RECREATE_MISMATCH",
                                   f"workspace={self.workspace_path} container={existing.id[:12]} reason=stale_image")
                        try:
                            existing.stop()
                            existing.remove()
                        except docker.errors.NotFound:
                            pass
                        existing = None
                    else:
                        log("DEBUG", "tools.docker_executor.container",
                            "Reusing existing container (config + image match)",
                            {"container_id": existing.id[:12], "name": container_name})
                        audit_event("CONTAINER_REUSE_OK",
                                   f"workspace={self.workspace_path} container={existing.id[:12]} source=name_match")
                except docker.errors.ImageNotFound:
                    log("WARNING", "tools.docker_executor.container",
                        "Could not compare image IDs (image not found), recreating container",
                        {"container_id": existing.id[:12]})
                    try:
                        existing.stop()
                        existing.remove()
                    except docker.errors.NotFound:
                        pass
                    existing = None

        # ── Reuse existing if still valid ──
        if existing is not None:
            self.container = existing
            if self.container.status == "dead":
                self.container.remove()
                self.container = None
                raise docker.errors.NotFound(f"Container {container_name} was dead and removed")
            elif self.container.status != "running":
                try:
                    self.container.start()
                except docker.errors.APIError:
                    self.container.remove()
                    self.container = None
                    raise docker.errors.NotFound(f"Container {container_name} failed to start and was removed")
            self.last_used = time.time()
            return

        # ── Create new container ──
        tmpfs = {
            "/tmp": "rw,noexec,nosuid,size=64m",
            "/home/agent": "rw,exec,size=256M,uid=1000,gid=1000",
        }
        git_path = os.path.join(self.workspace_path, ".git")
        if os.path.isdir(git_path):
            tmpfs["/workspace/.git"] = ""
        else:
            pass

        log('INFO', 'tools.docker_executor.container',
            f"AUDIT: Creating container with network={network_mode}, mode={workspace_mode}, tmpfs={tmpfs}")

        audit_event("CONTAINER_CREATE",
                   f"image={self.image} network={network_mode} name={container_name}")
        # Workspace bind mount via docker.types.Mount so it can be combined
        # with a named volume for the persistent package cache.
        host_workspace = self.workspace_path
        read_only = workspace_mode == "ro"
        workspace_mount = docker.types.Mount(
            target="/workspace", source=host_workspace, type="bind", read_only=read_only
        )
        pkg_mount = None
        try:
            pkg_volume_name = f"tm-packages-{self.workspace_id or 'default'}"
            try:
                self.client.volumes.get_or_create(name=pkg_volume_name)
            except (docker.errors.APIError, docker.errors.NotFound):
                self.client.volumes.create(name=pkg_volume_name)
            pkg_mount = docker.types.Mount(
                target="/home/agent/.local", source=pkg_volume_name, type="volume"
            )
        except Exception as e:
            log("WARNING", "tools.docker_executor.container",
                f"Package volume setup failed; continuing without persistent package cache: {e}")
        mounts = [workspace_mount]
        if pkg_mount is not None:
            mounts.append(pkg_mount)
        container_env = ["PYTHONUSERBASE=/home/agent/.local"]

        self.container = self.client.containers.run(
            image=self.image,
            name=container_name,
            mounts=mounts,
            tmpfs=tmpfs,
            network=network_mode,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            read_only=True,
            user="1000:1000",  # must match the user in Dockerfile
            detach=True,
            tty=True,
            stdin_open=True,
            command=["tail", "-f", "/dev/null"],
            mem_limit=self.mem_limit,
            cpu_quota=self.cpu_quota,
            environment=container_env,
            labels={
                "thoughtmachine.workspace_id": str(self.workspace_id)
                if self.workspace_id is not None else "default",
                "thoughtmachine.note": "",
                CONTAINER_TYPE_LABEL: CONTAINER_TYPE_FREE_USE,
            },
        )
        # Workspace bind mount already has correct UID (matches host)
        self.last_used = time.time()

    def execute(self, command, timeout=30, workdir="/workspace", environment=None):
        # Check idle timeout and close container if expired
        if self.container and (time.time() - self.last_used) > self.idle_timeout:
            self.close()
        
        self._ensure_container()
        self.last_used = time.time()
        # Auto-create working directory with correct ownership if needed
        if workdir != "/workspace":
            self.container.exec_run(
                cmd=["sh", "-c", f"mkdir -p {workdir} && chown agent:agent {workdir}"],
                workdir="/workspace"
            )
        try:
            exit_code, output = self._exec_with_timeout(
                command=command,
                timeout=timeout,
                workdir=workdir,
                environment=environment
            )
            stdout = output[0].decode() if output[0] else ""
            stderr = output[1].decode() if output[1] else ""
            return stdout, stderr, exit_code
        except TimeoutError as e:
            # Timeout occurred - container was killed and recreated
            return "", f"Command timed out after {timeout} seconds", -2
        except docker.errors.APIError as e:
            return "", str(e), -1

    def close(self):
        # Safely check if container attribute exists and is not None
        if hasattr(self, 'container') and self.container:
            try:
                self.container.stop()
                self.container.remove()
            except docker.errors.NotFound:
                pass
            self.container = None

    def _exec_with_timeout(self, command, timeout=30, workdir="/workspace", environment=None):
        """Execute command with timeout support using threading."""
        exec_kwargs = {
            "cmd": ["/bin/sh", "-c", command],
            "demux": True,
            "workdir": workdir,
        }
        if environment:
            exec_kwargs["environment"] = environment

        # Use a queue to pass result from thread
        result_queue = queue.Queue()
        
        def run_exec():
            try:
                exit_code, output = self.container.exec_run(**exec_kwargs)
                result_queue.put((exit_code, output, None))
            except Exception as e:
                result_queue.put((None, None, e))
        
        # Start thread
        exec_thread = threading.Thread(target=run_exec)
        exec_thread.daemon = True
        exec_thread.start()
        
        # Wait for thread to complete with timeout
        exec_thread.join(timeout)
        
        if exec_thread.is_alive():
            # Timeout occurred - try to kill the container to stop the command
            try:
                if self.container:
                    self.container.kill()
                    self.container = None
            except Exception:
                pass
            # Recreate container for future use
            self._ensure_container()
            raise TimeoutError(f"Command timed out after {timeout} seconds")
        
        # Get result from queue
        if result_queue.empty():
            # Thread finished but didn't put result (shouldn't happen)
            raise RuntimeError("Execution thread finished but no result")
        
        exit_code, output, error = result_queue.get()
        if error:
            raise error
        
        return exit_code, output
    def _ensure_image(self, verbose_build=False):
        """Build Docker image if it doesn't exist locally, is stale, or force_rebuild is True.

        An existing image is reused only when its ``EXECUTOR_BUILD_HASH_LABEL``
        label matches the current build-source hash; a missing or mismatched
        label means the build sources drifted and the image is rebuilt.

        Args:
            verbose_build: If True, log build output summary on success.

        Returns:
            The Docker image object.
        """
        if self.force_rebuild:
            self.close()
            image, _ = self._build_image(verbose_build=verbose_build, nocache=True)
            return image
        try:
            image = self.client.images.get(self.image)
        except docker.errors.ImageNotFound:
            image = None
        if image is not None:
            try:
                build_hash = compute_executor_build_hash()
            except OSError as e:
                log("WARNING", "tools.docker_executor.image",
                    f"Cannot read executor build sources ({e}) \u2014 reusing existing image")
                return image
            labels = getattr(image, "labels", None) or {}
            if labels.get(EXECUTOR_BUILD_HASH_LABEL) == build_hash:
                return image
            log("INFO", "tools.docker_executor.image",
                f"Image {self.image} build sources drifted (label mismatch) \u2014 rebuilding")
            image, _ = self._build_image(verbose_build=verbose_build)
            return image
        image, _ = self._build_image(verbose_build=verbose_build)
        return image

    def _build_image(self, verbose_build=False, nocache=False):
        """Build Docker image from committed requirements.txt and vault Dockerfile.

        Creates a temporary build context with a generated Dockerfile and the
        workspace's requirements.txt (retrieved from Git HEAD). This ensures
        builds are reproducible and independent of local file system layout.

        Args:
            verbose_build: If True, log build output summary on success.
            nocache: If True, force rebuild without Docker layer cache.

        Returns:
            Tuple of (image, log_lines) where log_lines is a list of build output lines.
            The log_lines are also stored in the module-level ``_build_log_cache``.

        Raises:
            RuntimeError: If requirements.txt is not committed or build fails.
        """
        import subprocess
        import tempfile
        import shutil

        global _build_in_progress
        _build_in_progress = True

        try:
            build_hash = compute_executor_build_hash()
        except OSError as e:
            raise RuntimeError(
                f"Cannot read executor build sources ({e}); "
                "requirements.txt and resources/default_dockerfile.txt must exist."
            ) from e

        # Compute container_name for cache key alignment with get_container_status
        normalised = self.workspace_path.replace("\\", "/")
        digest = hashlib.sha256(normalised.encode()).hexdigest()[:12]
        _container_name = f"agent-exec-{digest}"

        log_lines: list[str] = []
        image_id: str | None = None

        # Create temporary build context
        tmpdir = tempfile.mkdtemp(prefix="docker_build_")
        try:
            # 1. Copy vault Dockerfile into build context
            vault_dockerfile = os.path.expanduser(
                f"~/.thoughtmachine/workspaces/{self.workspace_id}/Dockerfile"
            )
            if not os.path.exists(vault_dockerfile):
                raise RuntimeError(
                    f"Vault Dockerfile not found at {vault_dockerfile}. "
                    "Please run the vault bootstrap to create it."
                )
            shutil.copy2(vault_dockerfile, os.path.join(tmpdir, "Dockerfile"))

            # 2. Extract requirements.txt from Git HEAD
            result = subprocess.run(
                ["git", "show", "HEAD:requirements.txt"],
                capture_output=True,
                text=True,
                cwd=self.workspace_path,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    "requirements.txt is not committed. Please commit it before rebuilding."
                )

            req_path = os.path.join(tmpdir, "requirements.txt")
            with open(req_path, "w") as f:
                f.write(result.stdout)

            # 2b. Guard: the build context must contain only the staged files
            staged = set(os.listdir(tmpdir))
            if not staged <= {"Dockerfile", "requirements.txt"}:
                raise RuntimeError(
                    f"Unexpected files staged in build context: {sorted(staged)}"
                )

            # 3. Build Docker image from temp context
            log('DEBUG', 'tools.docker_executor.build',
                f"Building Docker image {self.image} from temp context {tmpdir}")

            def _stream_line(line: str) -> None:
                log_lines.append(line)
                log('DEBUG', 'tools.docker_executor.build', f"Build: {line}")
                # Live-stream to the shared cache so the frontend polling
                # loop sees each line as it arrives
                with _build_log_cache_lock:
                    _build_log_cache[normalised] = '\n'.join(log_lines)
                try:
                    sys.stdout.write(line + '\n')
                    sys.stdout.flush()
                except Exception:
                    pass  # stdout may be None/closed in headless runtimes

            image_id, _ = _run_image_build(
                self.client, tmpdir, "Dockerfile", self.image,
                nocache=nocache, on_line=_stream_line,
                labels={EXECUTOR_BUILD_HASH_LABEL: build_hash},
            )

        except RuntimeError:
            # Re-raise the committed-requirements error as-is, but keep any
            # partial build output visible in the shared cache
            if log_lines:
                with _build_log_cache_lock:
                    cache_value = "\n".join(log_lines)
                    _build_log_cache[self.workspace_path] = cache_value
                    _build_log_cache[_container_name] = cache_value
            raise
        except Exception as e:
            log_lines.append(str(e))
            with _build_log_cache_lock:
                cache_value = "\n".join(log_lines)
                _build_log_cache[self.workspace_path] = cache_value
                _build_log_cache[_container_name] = cache_value
            raise RuntimeError(f"Docker build failed: {e}") from e
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            _build_in_progress = False

        if verbose_build and log_lines:
            log('INFO', 'tools.docker_executor.build',
                f"Build complete for {self.image}:\n" + "\n".join(log_lines))

        # \u2500\u2500 Store build log in shared cache \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        with _build_log_cache_lock:
            cache_value = "\n".join(log_lines)
            _build_log_cache[self.workspace_path] = cache_value
            _build_log_cache[_container_name] = cache_value

        return image_id, log_lines


# ══════════════════════════════════════════════════════════════════════════════
#  Container status helper (used by Flask endpoint)
# ══════════════════════════════════════════════════════════════════════════════


# ── Container rebuild helper ─────────────────────────────────────────────


def _remove_container_by_workspace(workspace_path: str) -> None:
    """Stop and remove the Docker container for the given workspace, if it exists.

    Uses the same deterministic naming scheme as :class:`DockerExecutor`
    (SHA-256 of the normalised absolute path, first 12 hex digits prefixed by
    ``agent-exec-``).  This is safe to call even when no container exists.
    """
    normalised = os.path.abspath(workspace_path).replace("\\", "/")
    digest = hashlib.sha256(normalised.encode()).hexdigest()[:12]
    container_name = f"agent-exec-{digest}"
    try:
        client = docker.from_env()
        container = client.containers.get(container_name)
        container.stop()
        container.remove()
        log("INFO", "tools.docker_executor.rebuild",
            f"Removed old container {container_name} after rebuild")
    except docker.errors.NotFound:
        pass  # container didn't exist — nothing to do
    except docker.errors.DockerException as exc:
        log("WARNING", "tools.docker_executor.rebuild",
            f"Could not remove container {container_name}: {exc}")


# Background build results cache — populated by rebuild_container background
# thread, consumed by get_container_status() to surface build results.
_background_build_results: dict[str, dict] = {}
_background_build_results_lock = threading.Lock()


def rebuild_container(workspace_path: str) -> dict:
    """
    Start a **background** rebuild of the Docker image for *workspace_path*
    with --no-cache and return immediately.

    The background thread stores its result in ``_background_build_results``;
    ``get_container_status()`` merges that result into its return value so the
    frontend can pick it up via its polling loop.

    Returns:
        A dict with:
        - **status** -- ``"building"``, ``"ok"``, or ``"error"``
        - **build_log** -- full build log from the build attempt
    """
    global _build_in_progress

    normalised = os.path.abspath(workspace_path).replace("\\", "/")

    if _build_in_progress:
        with _background_build_results_lock:
            result = _background_build_results.get(normalised, {})
        return result or {
            "status": "building",
            "build_log": "Build already in progress...",
        }

    def _run_build():
        global _build_in_progress
        _build_in_progress = True
        try:
            executor = DockerExecutor(
                workspace_path=normalised,
                force_rebuild=True,   # triggers nocache build
                idle_timeout=0,       # ephemeral, no pooling needed
            )
            executor.close()  # ensure any old container is gone
            image, log_lines = executor._build_image(verbose_build=True, nocache=True)

            # ── Remove the old container so the next execution creates a fresh one ──
            # The rebuild only builds the image; the old container with the previous
            # image continues to run unless we explicitly tear it down.
            _remove_container_by_workspace(normalised)

            result = {
                "status": "ok",
                "build_log": "\n".join(log_lines) if log_lines else "Build completed successfully.",
            }
        except RuntimeError as exc:
            result = {"status": "error", "build_log": str(exc)}
        except Exception as exc:
            log("ERROR", "tools.docker_executor.rebuild", f"Unexpected rebuild error: {exc}")
            result = {"status": "error", "build_log": f"Unexpected error: {exc}"}
        finally:
            _build_in_progress = False

        with _background_build_results_lock:
            _background_build_results[normalised] = result

    thread = threading.Thread(target=_run_build, daemon=True)
    thread.start()

    return {
        "status": "building",
        "build_log": "Build started in background...",
    }





def get_container_status(workspace_path: str) -> dict:
    """
    Return the status of the Docker container for *workspace_path*.

    The returned dict has the following keys:

    - **status** — ``"running"``, ``"stopped"``, ``"building"``, or ``"error"``
    - **capabilities** — workspace capability flags (see below)
    - **build_log** — full build log from the most recent ``_build_image()`` call

    Capabilities (fallback to fully-permissive defaults when no workspace
    capabilities file is found):

    .. code-block:: python

        {
            "allow_network": bool,
            "allow_docker": bool,
            "allowed_file_extensions": list[str],
            "max_file_size_bytes": int,
            "allowed_workspace_dirs": list[str],
        }
    """
    global _build_in_progress

    # ── 1. Normalise path ───────────────────────────────────────────────────
    normalised = os.path.abspath(workspace_path).replace("\\", "/")

    # ── 2. Compute container name ──────────────────────────────────────────
    digest = hashlib.sha256(normalised.encode()).hexdigest()[:12]
    container_name = f"agent-exec-{digest}"

    # ── 3. Check build-in-progress ──────────────────────────────────────────
    with _build_log_cache_lock:
        is_building = _build_in_progress

    if is_building:
        caps = _compute_effective_capabilities(normalised)
        with _build_log_cache_lock:
            live_log = _build_log_cache.get(normalised, "")
        return {
            "status": "building",
            "capabilities": caps,
            "image": _compute_image_tag(normalised),
            "build_log": live_log or "Build started...",
        }

    # ── 4. Query Docker for container status ────────────────────────────────
    status = "stopped"
    try:
        client = docker.from_env()
        try:
            container = client.containers.get(container_name)
            container.reload()
            if container.status == "running":
                status = "running"
            else:
                status = "stopped"
        except docker.errors.NotFound:
            status = "stopped"
    except docker.errors.DockerException as exc:
        status = "error"
        # Retrieve any existing build log; append the error
        with _build_log_cache_lock:
            log_text = _build_log_cache.get(container_name, "")
        log_text += f"\nDocker query error: {exc}"
        caps = _compute_effective_capabilities(normalised)
        return {
            "status": status,
            "capabilities": caps,
            "image": _compute_image_tag(normalised),
            "build_log": log_text.strip(),
        }

    # ── 5. Load effective capabilities (caps × policy) ──────────────────────
    caps = _compute_effective_capabilities(normalised)

    # ── 6. Retrieve build log ───────────────────────────────────────────────
    with _build_log_cache_lock:
        build_log = _build_log_cache.get(container_name, "")

    # ── 7. Check background rebuild result ───────────────────────────────────
    with _background_build_results_lock:
        bg_result = _background_build_results.get(normalised)
    if bg_result and bg_result.get("status") in ("ok", "error"):
        # A background rebuild finished — surface its log as the primary one
        build_log = bg_result.get("build_log", build_log)
        status = bg_result["status"] if bg_result["status"] == "error" else status

    # ── 8. Return ───────────────────────────────────────────────────────────
    return {
        "status": status,
        "capabilities": caps,
        "image": _compute_image_tag(normalised),
        "build_log": build_log,
    }


def get_integrity_status(
    workspace_path: str,
    session_permissions: dict = None,
) -> dict:
    """Return a lightweight integrity-check result for the given workspace.

    Wraps ``verify_container_integrity()`` into a dict suitable for the
    frontend API.  The returned dict always includes at least:

    - **status** -- ``"ok"``, ``"mismatch"``, ``"removed"``, or ``"error"``
    - **container_name** -- the expected container name (str)
    - **desired** -- ``{"network": ..., "mode": ...}``
    - **actual** -- ``{"network": ..., "mode": ...}`` or ``None`` if not found

    When a mismatch was detected and the container was removed, ``status``
    is ``"removed"`` and ``mismatch_reason`` explains why.

    Args:
        workspace_path: Absolute path to the workspace.
        session_permissions: Session permissions dict, or None for
            restrictive defaults.

    Returns:
        dict with ``status``, ``container_name``, ``desired``, ``actual``,
        and optionally ``mismatch_reason``.
    """
    result = verify_container_integrity(workspace_path, session_permissions)
    # Map action_taken to frontend-friendly status
    action_map = {
        "none": "ok" if result.get("matches_config") in (True, None) else "mismatch",
        "removed": "removed",
        "error": "error",
    }
    return {
        "status": action_map.get(result["action_taken"], "error"),
        "container_name": result["container_name"],
        "desired": result["desired"],
        "actual": result["actual"],
        "mismatch_reason": result.get("mismatch_reason"),
    }


def _load_capabilities(workspace_path: str) -> dict:
    """Load workspace capabilities, falling back to fully-permissive defaults."""
    try:
        from thoughtmachine.workspace_capabilities import (
            load_workspace_capabilities,
            resolve_workspace_id,
        )

        workspace_id = resolve_workspace_id(workspace_path)
        if workspace_id is None:
            return _default_capabilities()

        caps = load_workspace_capabilities(workspace_id)
        if caps is None:
            return _default_capabilities()

        return {
            "allow_network": caps.allow_network,
            "allow_docker": caps.allow_docker,
            "allowed_file_extensions": list(caps.allowed_file_extensions),
            "max_file_size_bytes": caps.max_file_size_bytes,
            "allowed_workspace_dirs": list(caps.allowed_workspace_dirs),
        }
    except Exception:
        return _default_capabilities()


def _compute_effective_capabilities(workspace_path: str) -> dict:
    """Return workspace capabilities directly — no policy merging.

    The old security_policy.json merging layer has been removed as part
    of Phase 1 of the security refactor.  Workspace capabilities are now
    the sole source of truth for what a workspace is allowed to do.
    Container-level enforcement is handled by _compute_container_config()
    via the security gate.
    """
    return _load_capabilities(workspace_path)


def _default_capabilities() -> dict:
    """Return fully-permissive capability defaults."""
    return {
        "allow_network": True,
        "allow_docker": True,
        "allowed_file_extensions": ["*"],
        "max_file_size_bytes": 0,
        "allowed_workspace_dirs": ["."],
    }

