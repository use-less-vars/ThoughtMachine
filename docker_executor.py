from agent.logging import log
import docker
import hashlib
import io
import os
import time
import threading
import queue
import json
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
log("DEBUG", "tools.docker_executor", "Module loaded", {"__file__": __file__})

def _compute_image_tag(workspace_path: str) -> str:
    """Derive a deterministic Docker image tag from the workspace path.

    Different workspaces (e.g., worktrees) get different tags so they
    never share or conflict on one tag like `agent-executor:latest`.
    """
    path_hash = hashlib.sha256(workspace_path.encode()).hexdigest()[:16]
    return f"agent-executor-{path_hash}"


def _resolve_workspace_id(workspace_path: str):
    """Resolve workspace ID from a workspace path, returning None on failure."""
    try:
        from thoughtmachine.workspace_capabilities import resolve_workspace_id
        return resolve_workspace_id(workspace_path)
    except Exception:
        return None


def _compute_desired_config(workspace_path: str, workspace_id, session_permissions):
    """Standalone version of DockerExecutor._compute_container_config().

    Computes the desired (network_mode, workspace_mode) tuple from
    session permissions, matching the same unified-gate logic that
    _ensure_container() uses at container creation time.
    """
    network_mode = "none"
    workspace_mode = "ro"

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
                f"Gate lookup failed (verify), using safe defaults: {e}")
            network_mode = "none"
            workspace_mode = "ro"
    elif session_permissions is not None:
        # Fallback: derive directly from session_permissions dict
        net = session_permissions.get("network", "banned")
        network_mode = "bridge" if net == "write" else "none"
        fs = session_permissions.get("filesystem", "read")
        workspace_mode = "rw" if fs in ("write", "full") else "ro"

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

    # Compute desired config (mirrors _ensure_container logic)
    desired_network, desired_mode = _compute_desired_config(
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
        idle_timeout: int = 600,
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

        Always runs the unified security gate — never falls through to a
        separate code path that bypasses permissions.

        Returns:
            Tuple of (network_mode: str, workspace_mode: str)
            where network_mode is "bridge" or "none"
            and workspace_mode is "rw" or "ro".
        """
        network_mode = "none"
        workspace_mode = "ro"

        if self.workspace_id and self.session_permissions is not None:
            try:
                from security.security_gate import (
                    get_workspace_capabilities,
                    get_effective_permissions,
                )
                from thoughtmachine.security import SessionPermissions
                caps = get_workspace_capabilities(self.workspace_id)
                eff = get_effective_permissions(SessionPermissions(**self.session_permissions), caps)

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
        elif self.session_permissions is not None:
            workspace_path_exists = os.path.isdir(self.workspace_path)
            cap_module_available = True
            try:
                import thoughtmachine.workspace_capabilities
            except ImportError:
                cap_module_available = False

            if workspace_path_exists and cap_module_available:
                # workspace_id resolution failed — fall back to session permissions
                # (they have already been vetted by ToolExecutor.check_required_categories).
                sp = self.session_permissions
                net = sp.get("network", "banned")
                if net == "write":
                    network_mode = "bridge"
                else:
                    network_mode = "none"
                fs = sp.get("filesystem", "read")
                workspace_mode = "rw" if fs in ("write", "full") else "ro"
                log("WARNING", "docker.security_gate",
                    f"workspace_id resolution failed for {self.workspace_path}; "
                    f"falling back to session_permissions (network={net}, fs={fs}).")
                audit_event("FALLBACK_NETWORK_RESTRICTION",
                           f"workspace={self.workspace_path} "
                           f"workspace_id=None session_network={net} session_fs={fs}")
            else:
                sp = self.session_permissions
                net = sp.get("network", "banned")
                if net == "write":
                    network_mode = "bridge"
                else:
                    network_mode = "none"

                fs = sp.get("filesystem", "read")
                workspace_mode = "rw" if fs in ("write", "full") else "ro"

        audit_event("NETWORK_DECISION", f"network_mode={network_mode}")

        return network_mode, workspace_mode

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
            log("DEBUG", "docker.filesystem",
                f".git is not a directory ({'file' if os.path.isfile(git_path) else 'absent'}), skipping tmpfs mask")

        log('INFO', 'tools.docker_executor.container',
            f"AUDIT: Creating container with network={network_mode}, mode={workspace_mode}, tmpfs={tmpfs}")

        audit_event("CONTAINER_CREATE",
                   f"image={self.image} network={network_mode} name={container_name}")
        self.container = self.client.containers.run(
            image=self.image,
            name=container_name,
            volumes={self.workspace_path: {"bind": "/workspace", "mode": workspace_mode}},
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
        """Build Docker image if it doesn't exist locally or force_rebuild is True.
        
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
            return image
        except docker.errors.ImageNotFound:
            pass
        image, _ = self._build_image(verbose_build=verbose_build)
        return image

    def _build_image(self, verbose_build=False, nocache=False):
        """Build Docker image from docker/executor.Dockerfile.

        Args:
            verbose_build: If True, log build output summary on success.
            nocache: If True, force rebuild without Docker layer cache.

        Returns:
            Tuple of (image, log_lines) where log_lines is a list of build output lines.
            The log_lines are also stored in the module-level ``_build_log_cache``.

        Raises:
            RuntimeError: If build fails, with concatenated build logs in the message.
        """
        global _build_in_progress
        _build_in_progress = True

        # Compute container_name for cache key alignment with get_container_status
        normalised = self.workspace_path.replace("\\", "/")
        digest = hashlib.sha256(normalised.encode()).hexdigest()[:12]
        _container_name = f"agent-exec-{digest}"

        dockerfile_dir = self.workspace_path
        dockerfile_path = os.path.join(dockerfile_dir, "docker", "executor.Dockerfile")
        if not os.path.exists(dockerfile_path):
            _build_in_progress = False
            raise RuntimeError(f"Dockerfile not found at {dockerfile_path}")

        log('DEBUG', 'tools.docker_executor.build', f"Building Docker image {self.image} from {dockerfile_path}")

        log_lines: list[str] = []
        image_id: str | None = None
        try:
            # Use the low-level API directly — the high-level images.build()
            # consumes the response generator internally to find the image ID,
            # which prevents live streaming.  The low-level api.build() returns
            # an unconsumed generator; we decode each JSON chunk and extract
            # the image ID from the final "aux" message ourselves.
            build_logs = self.client.api.build(
                path=dockerfile_dir,
                dockerfile="docker/executor.Dockerfile",
                tag=self.image,
                rm=True,
                pull=True,
                nocache=nocache,
                decode=True,
            )
            for chunk in build_logs:
                if "stream" in chunk:
                    line = chunk["stream"].strip()
                    if line:
                        log_lines.append(line)
                        log('DEBUG', 'tools.docker_executor.build', f"Build: {line}")
                        # Live-stream to the shared cache so the frontend polling
                        # loop sees each line as it arrives
                        with _build_log_cache_lock:
                            _build_log_cache[normalised] = '\n'.join(log_lines)
                        sys.stdout.write(line + '\n')
                        sys.stdout.flush()
                elif "aux" in chunk and "ID" in chunk["aux"]:
                    image_id = chunk["aux"]["ID"]
            if image_id is None:
                raise RuntimeError("Docker build completed but no image ID was returned")
            image = image_id
        except docker.errors.BuildError as e:
            build_log_str = "\n".join(str(line) for line in (e.build_log or []))
            log_lines.extend(str(line) for line in (e.build_log or []))
            _build_in_progress = False
            with _build_log_cache_lock:
                cache_value = "\n".join(log_lines)
                _build_log_cache[self.workspace_path] = cache_value
                _build_log_cache[_container_name] = cache_value
            raise RuntimeError(
                f"Docker build failed: {e}\n"
                f"Build logs:\n{build_log_str}"
            ) from e
        except Exception as e:
            _build_in_progress = False
            log_lines.append(str(e))
            with _build_log_cache_lock:
                cache_value = "\n".join(log_lines)
                _build_log_cache[self.workspace_path] = cache_value
                _build_log_cache[_container_name] = cache_value
            raise RuntimeError(f"Docker build failed: {e}") from e

        if verbose_build and log_lines:
            log('INFO', 'tools.docker_executor.build', f"Build complete for {self.image}:\n" + "\n".join(log_lines))

        # ── Store build log in shared cache ────────────────────────────────────
        with _build_log_cache_lock:
            cache_value = "\n".join(log_lines)
            _build_log_cache[self.workspace_path] = cache_value
            _build_log_cache[_container_name] = cache_value

        _build_in_progress = False
        return image, log_lines

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

