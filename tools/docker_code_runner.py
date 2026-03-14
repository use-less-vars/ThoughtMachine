# tools/docker_code_runner.py
import os
import time
from typing import Literal, Optional, Dict
from pydantic import Field
from .base import ToolBase

try:
    import docker
    from docker.errors import DockerException, NotFound, APIError
    DOCKER_AVAILABLE = True
except ImportError:
    DOCKER_AVAILABLE = False
    docker = None
    DockerException = Exception
    NotFound = Exception
    APIError = Exception


class DockerCodeRunner(ToolBase):
    """Execute code, scripts, and shell commands in a secure Docker container.
    
    Primary uses:
    - Run Python scripts and code snippets
    - Execute shell commands and programs  
    - Test scripts with full workspace file access
    - Run build tools, linters, or any executables
    
    Security features:
    - Docker isolation with read-only root filesystem
    - Dropped Linux capabilities
    - No network access by default (configurable)
    - Non-root user execution
    
    Workspace access:
    - Project directory mounted at /workspace
    - Read/write access to all project files
    - Environment variable support
    - Working directory specification
    """
    tool: Literal["DockerCodeRunner"] = "DockerCodeRunner"
    
    command: str = Field(
        description="Shell command to execute inside the container (passed to /bin/sh -c)"
    )
    timeout: int = Field(
        default=30,
        description="Maximum execution time in seconds (default 30)"
    )
    working_dir: Optional[str] = Field(
        default=None,
        description="Working directory relative to workspace (default: workspace root)"
    )
    environment: Optional[Dict[str, str]] = Field(
        default=None,
        description="Environment variables to set inside container (key=value)"
    )
    build: bool = Field(
        default=False,
        description="Force rebuild of Docker image before execution"
    )
    image: str = Field(
        default="agent-executor",
        description="Docker image name (default: agent-executor)"
    )
    network: str = Field(
        default="none",
        description="Container network mode: 'none', 'host', 'bridge', or custom network name"
    )
    mem_limit: str = Field(
        default="512m",
        description="Memory limit (e.g., '512m', '1g')"
    )
    cpu_quota: int = Field(
        default=50000,
        description="CPU quota in microseconds (default 50000 = 50ms per 100ms period)"
    )
    
    def execute(self) -> str:
        if not DOCKER_AVAILABLE:
            return self._truncate_output(
                "Error: Docker Python SDK not installed. Install with 'pip install docker'."
            )
        
        # Validate workspace path
        if self.workspace_path is None:
            workspace = os.getcwd()
        else:
            workspace = self.workspace_path
        
        # Build absolute path for working directory
        workdir = "/workspace"
        if self.working_dir:
            # Ensure working_dir is safe (no path traversal)
            rel_path = os.path.normpath(self.working_dir)
            if rel_path.startswith("..") or os.path.isabs(rel_path):
                return self._truncate_output(
                    f"Error: working_dir '{self.working_dir}' must be relative to workspace and not traverse upwards."
                )
            workdir = os.path.join("/workspace", rel_path)
        
        try:
            # Import DockerExecutor from the existing module
            # Add parent directory to sys.path
            import sys
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + '/..')
            from docker_executor import DockerExecutor
        except ImportError as e:
            return self._truncate_output(
                f"Error: Could not import DockerExecutor: {e}. Make sure docker package is installed and docker_executor.py exists."
            )
        
        try:
            executor = DockerExecutor(
                workspace_path=workspace,
                image=self.image,
                network=self.network,
                mem_limit=self.mem_limit,
                cpu_quota=self.cpu_quota,
                force_rebuild=self.build
            )
            
            # Execute command with optional environment and working directory
            stdout, stderr, exit_code = executor.execute(
                command=self.command,
                timeout=self.timeout,
                workdir=workdir,
                environment=self.environment
            )
            
            # Format output
            result_lines = []
            result_lines.append(f"Command: {self.command}")
            result_lines.append(f"Exit code: {exit_code}")
            if stdout:
                result_lines.append("--- stdout ---")
                result_lines.append(stdout.rstrip())
            if stderr:
                result_lines.append("--- stderr ---")
                result_lines.append(stderr.rstrip())
            
            return self._truncate_output("\n".join(result_lines))
            
        except DockerException as e:
            return self._truncate_output(f"Docker error: {e}")
        except Exception as e:
            return self._truncate_output(f"Unexpected error: {e}")
    
    def _build_image(self, client, image_name):
        """Build Docker image from docker/executor.Dockerfile"""
        dockerfile_path = os.path.join(os.path.dirname(__file__), "..", "docker", "executor.Dockerfile")
        if not os.path.exists(dockerfile_path):
            dockerfile_path = "docker/executor.Dockerfile"
        
        try:
            # Build the image
            image, build_logs = client.images.build(
                path=os.path.dirname(dockerfile_path),
                dockerfile=os.path.basename(dockerfile_path),
                tag=image_name,
                rm=True,
                pull=True
            )
            # Log build output (optional)
            for chunk in build_logs:
                if "stream" in chunk:
                    line = chunk["stream"].strip()
                    if line:
                        print(f"Build: {line}")
            return image
        except DockerException as e:
            raise RuntimeError(f"Failed to build Docker image: {e}")