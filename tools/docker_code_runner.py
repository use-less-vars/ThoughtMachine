# tools/docker_code_runner.py
import json
import os
import time
import datetime
import uuid
from typing import Literal, Optional, Dict, Any
from pydantic import Field, field_validator, model_validator
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
    
    Output format:
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
    
    Template variables:
    Command supports template variables using {variable_name} syntax.
    Built-in variables:
    - {workspace}: Workspace directory path
    - {timestamp}: ISO timestamp
    - {date}: Current date (YYYY-MM-DD)
    - {time}: Current time (HH:MM:SS)
    - {random_id}: Random 8-character hex string
    
    User variables can be provided via the 'variables' parameter.

    Multi-step scripts:
    - Use the 'script' field to provide multi-step scripts (multiple commands).
    - Script is written to a temporary file and executed with the specified interpreter.
    - The 'interpreter' field determines which interpreter to use (default: 'bash').
    - If 'script' is provided, the 'command' field is ignored.

    Container pooling:
    - Containers are pooled and reused across executions for performance.
    - Idle containers are automatically closed after the idle_timeout period (default 300s).
    - This reduces Docker container overhead while maintaining security isolation.
    """
    tool: Literal["DockerCodeRunner"] = "DockerCodeRunner"
    
    command: Optional[str] = Field(
        default=None,
        description="Shell command to execute inside the container (passed to /bin/sh -c). If script is provided, this field is ignored."
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
    #network: str = Field(
    #    default="none",
    #    description="Container network mode: 'none', 'host', 'bridge', or custom network name"
    #)
    mem_limit: str = Field(
        default="512m",
        description="Memory limit (e.g., '512m', '1g')"
    )
    cpu_quota: int = Field(
        default=50000,
        description="CPU quota in microseconds (default 50000 = 50ms per 100ms period)"
    )
    variables: Optional[Dict[str, str]] = Field(
        default=None,
        description="Template variables to substitute in command. Format: {'name': 'value'}"
    )
    script: Optional[str] = Field(
        default=None,
        description="Multi-step script to execute. If provided, command field is ignored. Script is written to a temporary file and executed with the specified interpreter."
    )
    interpreter: str = Field(
        default="bash",
        description="Interpreter to use for executing script (e.g., 'bash', 'python3', 'sh'). Default: 'bash'"
    )
    idle_timeout: int = Field(
        default=300,
        description="Idle timeout in seconds for container pooling. Container will be closed after this period of inactivity. Default: 300 seconds (5 minutes)."
    )
    
    @model_validator(mode='after')
    def validate_command_or_script(self):
        # Ensure at least one of command or script is provided
        if self.command is None and self.script is None:
            raise ValueError('Either command or script must be provided')
        return self

    def _substitute_variables(self, command: str) -> str:
        """Substitute template variables in command string."""
        # Built-in variables
        builtins = {
            "workspace": self.workspace_path or os.getcwd(),
            "timestamp": datetime.datetime.now().isoformat(),
            "date": datetime.datetime.now().strftime("%Y-%m-%d"),
            "time": datetime.datetime.now().strftime("%H:%M:%S"),
            "random_id": uuid.uuid4().hex[:8],
        }
        
        # Start with builtins
        variables = builtins.copy()
        
        # Add user variables if provided
        if self.variables:
            variables.update(self.variables)
        
        # Perform substitution
        result = command
        for var_name, var_value in variables.items():
            placeholder = "{" + var_name + "}"
            result = result.replace(placeholder, str(var_value))
        
        return result

    def _prepare_script_command(self, script_content: str, interpreter: str) -> str:
        """Convert script content to a shell command that writes and executes the script."""
        # Generate a random delimiter that doesn't appear in script content
        import random
        import string
        
        # Try up to 10 times to find a unique delimiter
        for _ in range(10):
            delimiter = ''.join(random.choices(string.ascii_uppercase + string.digits, k=16))
            if delimiter not in script_content:
                break
        else:
            # Fallback delimiter
            delimiter = 'SCRIPT_EOF'
        
        # Write script using heredoc, make executable, run with interpreter
        # Use /workspace/tmp directory with random name (writable location)
        script_dir = "/workspace/tmp"
        script_path = f"{script_dir}/script_{uuid.uuid4().hex[:8]}.sh"

        # Build command:
        # 0. Ensure script directory exists
        # 1. Write script content to file using heredoc
        # 2. Make executable
        # 3. Execute with specified interpreter
        command = f'''mkdir -p "{script_dir}" && cat > "{script_path}" << '{delimiter}'
{script_content}
{delimiter}
chmod +x "{script_path}"
"{interpreter}" "{script_path}"'''        
        # If interpreter is 'bash' or 'sh', we could also directly run the script
        # But using interpreter ensures proper execution.
        return command

    def _build_json_response(        self,
        success: bool,
        exit_code: int = 0,
        stdout: str = "",
        stderr: str = "",
        command: str = "",
        duration: float = 0.0,
        error: str = "",
        timed_out: bool = False
    ) -> str:
        """Build structured JSON response for Docker execution."""
        response = {
            "success": success,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "command": command,
            "duration": duration,
            "timed_out": timed_out,
            "error": error
        }
        # Remove empty optional fields
        if not error:
            response.pop("error")
        return json.dumps(response, indent=2)
    
    def execute(self) -> str:
        start_time = time.time()
        duration = 0.0
        
        if not DOCKER_AVAILABLE:
            duration = time.time() - start_time
            return self._build_json_response(
                success=False,
                exit_code=-1,
                error="Docker Python SDK not installed. Install with 'pip install docker'.",
                duration=duration
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
                duration = time.time() - start_time
                return self._build_json_response(
                    success=False,
                    exit_code=-1,
                    error=f"working_dir '{self.working_dir}' must be relative to workspace and not traverse upwards.",
                    duration=duration
                )
            workdir = os.path.join("/workspace", rel_path)
        
        try:
            # Import DockerExecutor from the existing module
            # Add parent directory to sys.path
            import sys
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + '/..')
            from docker_executor import DockerExecutor
        except ImportError as e:
            duration = time.time() - start_time
            return self._build_json_response(
                success=False,
                exit_code=-1,
                error=f"Could not import DockerExecutor: {e}. Make sure docker package is installed and docker_executor.py exists.",
                duration=duration
            )
        
        # Determine actual command to execute (script takes precedence)
        actual_command = None
        try:
            if self.script is not None:
                # Substitute variables in script content
                script_content = self._substitute_variables(self.script)
                # Convert script to executable command
                actual_command = self._prepare_script_command(script_content, self.interpreter)
            else:
                # Use command field
                actual_command = self._substitute_variables(self.command)
        except Exception as e:
            duration = time.time() - start_time
            return self._build_json_response(
                success=False,
                exit_code=-1,
                error=f"Failed to prepare command/script: {e}",
                duration=duration
            )
        
        try:
            executor = DockerExecutor(
                workspace_path=workspace,
                image=self.image,
                network="none",     #self.network, <-- Disable network for security
                mem_limit=self.mem_limit,
                cpu_quota=self.cpu_quota,
                force_rebuild=self.build,
                idle_timeout=self.idle_timeout
            )
            
            # Execute command with optional environment and working directory
            stdout, stderr, exit_code = executor.execute(
                command=actual_command,
                timeout=self.timeout,
                workdir=workdir,
                environment=self.environment
            )
            
            duration = time.time() - start_time
            
            # Check for timeout (docker_executor returns -2 for timeout)
            timed_out = exit_code == -2
            # Also check stderr for timeout message as additional safeguard
            if not timed_out and "timed out" in stderr.lower():
                timed_out = True
            
            return self._build_json_response(
                success=exit_code == 0,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                command=actual_command,
                duration=duration,
                timed_out=timed_out
            )
            
        except DockerException as e:
            duration = time.time() - start_time
            return self._build_json_response(
                success=False,
                exit_code=-1,
                error=f"Docker error: {e}",
                duration=duration
            )
        except TimeoutError as e:
            duration = time.time() - start_time
            return self._build_json_response(
                success=False,
                exit_code=-2,
                error=f"Command timed out after {self.timeout} seconds",
                duration=duration,
                timed_out=True
            )
        except Exception as e:
            duration = time.time() - start_time
            return self._build_json_response(
                success=False,
                exit_code=-1,
                error=f"Unexpected error: {e}",
                duration=duration
            )
    
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