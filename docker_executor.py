import docker
import hashlib
import os
import time

class DockerExecutor:
    def __init__(self, workspace_path, image="agent-executor", 
                 network="none", mem_limit="512m", cpu_quota=50000):
        self.workspace_path = os.path.abspath(workspace_path)
        self.image = image
        self.network = network
        self.mem_limit = mem_limit
        self.cpu_quota = cpu_quota
        self.client = docker.from_env()
        self.container = None
        self.last_used = time.time()

    def _ensure_container(self):
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
            if self.container.status != "running":
                self.container.start()
        except docker.errors.NotFound:
            self.container = self.client.containers.run(
                image=self.image,
                name=container_name,
                volumes={self.workspace_path: {"bind": "/workspace", "mode": "rw"}},
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

    def execute(self, command, timeout=30):
        self._ensure_container()
        self.last_used = time.time()
        try:
            exit_code, output = self.container.exec_run(
                ["/bin/sh", "-c", command],
                demux=True,
                workdir="/workspace",
                timeout=timeout
            )
            stdout = output[0].decode() if output[0] else ""
            stderr = output[1].decode() if output[1] else ""
            return stdout, stderr, exit_code
        except docker.errors.APIError as e:
            return "", str(e), -1

    def close(self):
        if self.container:
            try:
                self.container.stop()
                self.container.remove()
            except docker.errors.NotFound:
                pass
            self.container = None

    def __del__(self):
        self.close()