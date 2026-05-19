from agent.logging import log
import docker
import hashlib
import os
import time
import threading
import queue
import json
import fnmatch
import sys
log("DEBUG", "tools.docker_executor", "Module loaded", {"__file__": __file__})

def _load_policy(workspace_path: str) -> dict:
    """Load security policy from security_policy.json.
    Checks in order:
    1. Same directory as this file (project root)
    2. ~/.thoughtmachine/security_policy.json
    Returns dict with keys 'docker_network_allowed' and 'writable_home'.
    """
    from pathlib import Path

    # Determine the directory where this file lives
    this_dir = Path(__file__).parent.resolve()
    candidate_paths = [
        this_dir / "security_policy.json",          # project root
        Path.home() / ".thoughtmachine" / "security_policy.json",  # home dir
    ]

    config_path = None
    for candidate in candidate_paths:
        if candidate.exists():
            config_path = candidate
            break

    log("DEBUG", "tools.docker_executor.policy",
        "Looking for security policy",
        {"workspace_path": workspace_path, "candidates": [str(p) for p in candidate_paths], "found": str(config_path)})

    if config_path is None:
        log("DEBUG", "tools.docker_executor.policy", "No policy file found, using defaults",
            {"docker_network_allowed": False, "writable_home": False})
        return {"docker_network_allowed": False, "writable_home": False}

    try:
        with open(config_path) as f:
            config = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        log("WARNING", "tools.docker_executor.policy", f"Error loading policy file: {e}",
            {"config_path": str(config_path)})
        return {"docker_network_allowed": False, "writable_home": False}

    log("DEBUG", "tools.docker_executor.policy", "Policy config loaded",
        {"config_path": str(config_path), "patterns": list(config.keys())})

    # Find matching workspace pattern (exact or glob)
    for pattern, policy in config.items():
        if pattern == "default":
            continue
        match_result = fnmatch.fnmatch(workspace_path, pattern)
        log("DEBUG", "tools.docker_executor.policy",
            f"Matching pattern {pattern!r} against {workspace_path!r}: {match_result}",
            {"pattern": pattern, "workspace_path": workspace_path, "match": match_result})
        if match_result:
            result = {
                "docker_network_allowed": policy.get("docker_network_allowed", False),
                "writable_home": policy.get("writable_home", False),
            }
            log("DEBUG", "tools.docker_executor.policy", "Policy matched, returning", result)
            return result
    # Fallback to default
    default = config.get("default", {})
    result = {
        "docker_network_allowed": default.get("docker_network_allowed", False),
        "writable_home": default.get("writable_home", False),
    }
    log("DEBUG", "tools.docker_executor.policy", "No pattern match, using default", result)
    return result

class DockerExecutor:
    def __init__(self, workspace_path, image="agent-executor",
                  network="none", mem_limit="512m", cpu_quota=50000, force_rebuild=False, idle_timeout=600):
        # Normalize path: absolute, no trailing slash — ensures deterministic container naming
        self.workspace_path = os.path.abspath(workspace_path).rstrip('/')
        self.image = image
        self.network = network
        self.mem_limit = mem_limit
        self.cpu_quota = cpu_quota
        self.force_rebuild = force_rebuild
        self.idle_timeout = idle_timeout
        self.client = docker.from_env()
        self.container = None
        self.last_used = time.time()
        self._timeout_warning_printed = False

    def _ensure_container(self):
        # Ensure the Docker image exists
        self._ensure_image()

        log("DEBUG", "tools.docker_executor.container",
            "_ensure_container called",
            {"workspace_path": self.workspace_path, "image": self.image,
             "has_container": self.container is not None,
             "force_rebuild": self.force_rebuild})

        if self.container:
            try:
                self.container.reload()
                if self.container.status == "running":
                    log("DEBUG", "tools.docker_executor.container", "Reusing running container",
                        {"container_id": self.container.id, "name": self.container.name})
                    return
            except docker.errors.NotFound:
                self.container = None
        # Deterministic container name based on workspace path
        safe_name = hashlib.sha256(self.workspace_path.encode()).hexdigest()[:12]
        container_name = f"agent-exec-{safe_name}"

        # When force_rebuild is True, skip container reuse: close old container
        # by name and create a fresh one from the newly built image.
        if self.force_rebuild:
            try:
                existing = self.client.containers.get(container_name)
                existing.reload()
                try:
                    existing.stop()
                    existing.remove()
                except docker.errors.NotFound:
                    pass
            except docker.errors.NotFound:
                pass
            existing = None
        else:
            # Try to get existing container and check against current policy
            try:
                existing = self.client.containers.get(container_name)
                existing.reload()

                # Check if existing container's config matches current policy
                policy = _load_policy(self.workspace_path)
                desired_network = "bridge" if policy.get("docker_network_allowed") else "none"
                current_network = existing.attrs['HostConfig']['NetworkMode']

                # Check if /home/agent tmpfs is mounted
                # Docker stores tmpfs in HostConfig.Tmpfs (dict), NOT in Mounts array
                tmpfs_mounts = existing.attrs.get('HostConfig', {}).get('Tmpfs', {})
                has_home_tmpfs = '/home/agent' in tmpfs_mounts
                needs_writable_home = policy.get("writable_home", False)

                if (current_network != desired_network) or (needs_writable_home != has_home_tmpfs):
                    # Config mismatch — remove and recreate
                    try:
                        existing.stop()
                        existing.remove()
                    except docker.errors.NotFound:
                        pass
                    existing = None

                if existing is not None:
                    # Check if the container's image matches the currently tagged image.
                    # After a Dockerfile rebuild, the image ID changes even if the tag
                    # stays the same, and container pooling would otherwise reuse the
                    # stale container with the old image.
                    try:
                        # Container's Image attribute stores the SHA256 of the image
                        # used at creation time.
                        container_image_id = existing.attrs.get('Image', '')
                        current_image = self.client.images.get(self.image)
                        current_image_id = current_image.id
                        if container_image_id and current_image_id and container_image_id != current_image_id:
                            log("INFO", "tools.docker_executor.container",
                                "Container built from stale image, recreating",
                                {"container_id": existing.id,
                                 "container_image": container_image_id[:19] + "...",
                                 "current_image": current_image_id[:19] + "..."})
                            try:
                                existing.stop()
                                existing.remove()
                            except docker.errors.NotFound:
                                pass
                            existing = None
                    except docker.errors.ImageNotFound:
                        # If the image can't be found for comparison, recreate to be safe
                        log("WARNING", "tools.docker_executor.container",
                            "Could not compare image IDs (image not found), recreating container",
                            {"container_id": existing.id})
                        try:
                            existing.stop()
                            existing.remove()
                        except docker.errors.NotFound:
                            pass
                        existing = None

                if existing is not None:
                    self.container = existing
                    # Handle non-running container states
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

            except docker.errors.NotFound:
                pass

        # Create new container with current policy
        policy = _load_policy(self.workspace_path)
        network_mode = "bridge" if policy.get("docker_network_allowed", False) else "none"
        tmpfs = {"/tmp": "rw,noexec,nosuid,size=64m"}
        if policy.get("writable_home", False):
            tmpfs["/home/agent"] = "rw,exec,size=256M,uid=1000,gid=1000"

        log('DEBUG', 'tools.docker_executor.container',
            f"Creating container with network={network_mode}, tmpfs={tmpfs}")

        self.container = self.client.containers.run(
            image=self.image,
            name=container_name,
            volumes={self.workspace_path: {"bind": "/workspace", "mode": "rw"}},
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
        
        Returns:
            Tuple of (image, log_lines) where log_lines is a list of build output lines.
        
        Raises:
            RuntimeError: If build fails, with concatenated build logs in the message.
        """
        dockerfile_dir = self.workspace_path
        dockerfile_path = os.path.join(dockerfile_dir, "docker", "executor.Dockerfile")
        if not os.path.exists(dockerfile_path):
            raise RuntimeError(f"Dockerfile not found at {dockerfile_path}")

        log('DEBUG', 'tools.docker_executor.build', f"Building Docker image {self.image} from {dockerfile_path}")
        log('DEBUG', 'tools.docker_executor.build', f"Build context directory: {dockerfile_dir}")
        log('DEBUG', 'tools.docker_executor.build', f"Absolute path: {os.path.abspath(dockerfile_dir)}")
        log('DEBUG', 'tools.docker_executor.build', f"Requirements.txt exists: {os.path.exists(os.path.join(dockerfile_dir, 'requirements.txt'))}")
        log('DEBUG', 'tools.docker_executor.build', f"Files in build context:")
        for f in os.listdir(dockerfile_dir):
            log('DEBUG', 'tools.docker_executor.build', f"  {f}")

        log_lines = []
        try:
            image, build_logs = self.client.images.build(
                path=dockerfile_dir,
                dockerfile="docker/executor.Dockerfile",
                tag=self.image,
                rm=True,
                pull=True,
                nocache=nocache  # Only nocache=True when force_rebuild
            )
            # Generator iteration must be inside try/except block because
            # Docker SDK raises BuildError from within the generator,
            # not from build() itself.
            for chunk in build_logs:
                if "stream" in chunk:
                    line = chunk["stream"].strip()
                    if line:
                        log_lines.append(line)
                        log('DEBUG', 'tools.docker_executor.build', f"Build: {line}")
        except docker.errors.BuildError as e:
            # BuildError already has build_log; include it in the error message
            build_log_str = "\n".join(str(line) for line in (e.build_log or []))
            raise RuntimeError(
                f"Docker build failed: {e}\n"
                f"Build logs:\n{build_log_str}"
            ) from e
        except Exception as e:
            raise RuntimeError(f"Docker build failed: {e}") from e

        if verbose_build and log_lines:
            log('INFO', 'tools.docker_executor.build', f"Build complete for {self.image}:\n" + "\n".join(log_lines))

        return image, log_lines