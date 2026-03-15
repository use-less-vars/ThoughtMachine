import docker
import hashlib
import os
import time
import threading
import queue

class DockerExecutor:
    def __init__(self, workspace_path, image="agent-executor",
                  network="none", mem_limit="512m", cpu_quota=50000, force_rebuild=False, idle_timeout=300):
        self.workspace_path = os.path.abspath(workspace_path)
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
        
        if self.container:
            try:
                self.container.reload()
                if self.container.status == "running":
                    return
            except docker.errors.NotFound:
                self.container = None

        # Deterministic container name based on workspace path
        safe_name = hashlib.sha256(self.workspace_path.encode()).hexdigest()[:12]
        container_name = f"agent-exec-{safe_name}"

        try:
            self.container = self.client.containers.get(container_name)
            # Handle non-running container states
            if self.container.status == "dead":
                # Dead container cannot be started, remove and recreate
                self.container.remove()
                self.container = None
                raise docker.errors.NotFound(f"Container {container_name} was dead and removed")
            elif self.container.status != "running":
                # Exited or created container, try to start
                try:
                    self.container.start()
                except docker.errors.APIError:
                    # Failed to start, remove and recreate
                    self.container.remove()
                    self.container = None
                    raise docker.errors.NotFound(f"Container {container_name} failed to start and was removed")
        except docker.errors.NotFound:
            self.container = self.client.containers.run(
                image=self.image,
                name=container_name,
                volumes={self.workspace_path: {"bind": "/workspace", "mode": "rw"}},
                tmpfs={"/tmp": "rw,noexec,nosuid,size=64m"},
                network=self.network,
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
        self.last_used = time.time()

    def execute(self, command, timeout=30, workdir="/workspace", environment=None):
        # Check idle timeout and close container if expired
        if self.container and (time.time() - self.last_used) > self.idle_timeout:
            self.close()
        
        self._ensure_container()
        self.last_used = time.time()
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
    def __del__(self):
        try:
            self.close()
        except Exception:
            # Ignore errors during cleanup
            pass
    def _ensure_image(self):
        """Build Docker image if it doesn't exist locally or force_rebuild is True."""
        if self.force_rebuild:
            self.close()
            self._build_image()
            return
        try:
            self.client.images.get(self.image)
            return
        except docker.errors.ImageNotFound:
            pass
        self._build_image()
    
    def _build_image(self):
        """Build Docker image from docker/executor.Dockerfile."""
        dockerfile_dir = self.workspace_path
        dockerfile_path = os.path.join(dockerfile_dir, "docker", "executor.Dockerfile")
        if not os.path.exists(dockerfile_path):
            raise RuntimeError(f"Dockerfile not found at {dockerfile_path}")

        import os
        print(f"Building Docker image {self.image} from {dockerfile_path}")
        print(f"Build context directory: {dockerfile_dir}")
        print(f"Absolute path: {os.path.abspath(dockerfile_dir)}")
        print(f"Requirements.txt exists: {os.path.exists(os.path.join(dockerfile_dir, 'requirements.txt'))}")
        print(f"Files in build context:")
        for f in os.listdir(dockerfile_dir):
            print(f"  {f}")
        image, build_logs = self.client.images.build(
            path=dockerfile_dir,
            dockerfile="executor.Dockerfile",
            tag=self.image,
            rm=True,
            pull=False
        )
        for chunk in build_logs:
            if "stream" in chunk:
                line = chunk["stream"].strip()
                if line:
                    print(f"Build: {line}")
        return image