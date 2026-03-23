# security.py - Centralized security layer for the ThoughtMachine agent
"""
Provides path validation, Docker sandboxing, and capability checking
with comprehensive logging.
"""

import os
import sys
import logging
import hashlib
import docker
from pathlib import Path
from typing import Optional, Dict, List, Any
from datetime import datetime

# Try to import the AgentLogger for structured logging
try:
    from agent_logging import AgentLogger, LogEventType, LogLevel
    LOGGING_AVAILABLE = True
except ImportError:
    LOGGING_AVAILABLE = False
    AgentLogger = None
    LogEventType = None
    LogLevel = None

# Global logger instance (set by agent at startup)
_logger: Optional[AgentLogger] = None

# Security exceptions
class SecurityError(Exception):
    """Base security exception."""
    pass

class PathOutsideWorkspaceError(SecurityError):
    """Raised when a path is outside the allowed workspace."""
    pass

class DockerSetupError(SecurityError):
    """Raised when Docker sandbox setup fails."""
    pass

class CapabilityDeniedError(SecurityError):
    """Raised when a capability check fails."""
    pass


def set_logger(logger: Optional[AgentLogger]) -> None:
    """Set the global logger for security module."""
    global _logger
    _logger = logger


def _redact_sensitive_data(data: Any) -> Any:
    """
    Redact potentially sensitive data from log entries.
    Currently redacts API keys and tokens.
    """
    if isinstance(data, dict):
        redacted = {}
        for key, value in data.items():
            # Redact keys that look like secrets
            key_lower = str(key).lower()
            if any(secret_word in key_lower for secret_word in ['key', 'token', 'secret', 'password', 'auth']):
                redacted[key] = '***REDACTED***'
            else:
                redacted[key] = _redact_sensitive_data(value)
        return redacted
    elif isinstance(data, list):
        return [_redact_sensitive_data(item) for item in data]
    elif isinstance(data, str):
        # Don't redact strings here as they might be part of paths, etc.
        return data
    else:
        return data


def validate_path(path: str, mode: str = 'read', workspace_path: Optional[str] = None) -> str:
    """
    Validate that a given path is within the allowed workspace.

    Args:
        path: The path to validate (can be relative or absolute)
        mode: Access mode ('read', 'write', etc.) for logging
        workspace_path: Root directory for file operations. If None, no restrictions.

    Returns:
        Absolute normalized path if valid.

    Raises:
        PathOutsideWorkspaceError: If path is outside workspace.
        ValueError: For invalid inputs.
    """
    original_path = path
    
    # If workspace_path is provided, treat relative paths as relative to workspace_path
    if workspace_path is not None and not os.path.isabs(path):
        # Join with workspace_path
        path = os.path.join(workspace_path, path)
    
    # First, get absolute path of the requested location (without following symlinks)
    try:
        requested_abs = os.path.abspath(path)
    except Exception as e:
        raise ValueError(f"Invalid path '{original_path}': {e}")
    
    # Use requested_abs as the path to validate
    target_abs = requested_abs
    
    # If no workspace restriction, return canonical path if possible
    if workspace_path is None:
        try:
            return os.path.realpath(target_abs)
        except Exception:
            return target_abs

    workspace_abs = os.path.abspath(workspace_path)
    workspace_abs = os.path.realpath(workspace_abs)
    
    # Ensure target is within workspace
    try:
        target_rel = os.path.relpath(target_abs, workspace_abs)
    except ValueError:
        # Paths are on different drives (Windows)
        _log_security_event(
            event_type=LogEventType.SECURITY_VIOLATION if LOGGING_AVAILABLE else None,
            message=f"Path violation attempt: '{original_path}' is outside workspace '{workspace_abs}' (different drive)",
            level=LogLevel.WARNING if LOGGING_AVAILABLE else logging.WARNING,
            data={
                "path": original_path,
                "resolved_path": target_abs,
                "workspace": workspace_abs,
                "mode": mode,
                "reason": "different_drive"
            }
        )
        raise PathOutsideWorkspaceError(f"Path {original_path} is outside workspace {workspace_abs}")
    
    # Check for directory traversal attempts
    if target_rel.startswith("..") or os.path.isabs(target_rel):
        _log_security_event(
            event_type=LogEventType.SECURITY_VIOLATION if LOGGING_AVAILABLE else None,
            message=f"Path violation attempt: '{original_path}' resolves to outside workspace '{workspace_abs}'",
            level=LogLevel.WARNING if LOGGING_AVAILABLE else logging.WARNING,
            data={
                "path": original_path,
                "resolved_path": target_abs,
                "workspace": workspace_abs,
                "mode": mode,
                "relative_path": target_rel,
                "reason": "traversal"
            }
        )
        raise PathOutsideWorkspaceError(f"Path {original_path} is outside workspace {workspace_abs}")
    
    # Try to get canonical path (following symlinks) for return value
    # If symlink points outside workspace or is broken, that's OK - we already validated
    # the symlink itself is within workspace
    canonical_abs = target_abs
    try:
        canonical_abs = os.path.realpath(target_abs)
    except Exception:
        # Broken symlink or other issue - keep the absolute path
        pass
    
    # Log successful access
    try:
        file_size = os.path.getsize(canonical_abs) if os.path.exists(canonical_abs) and os.path.isfile(canonical_abs) else None
    except Exception:
        file_size = None
    
    _log_security_event(
        event_type=LogEventType.FILE_ACCESS if LOGGING_AVAILABLE else None,
        message=f"File access allowed: {mode} on '{original_path}'",
        level=LogLevel.INFO if LOGGING_AVAILABLE else logging.INFO,
        data={
            "path": original_path,
            "resolved_path": canonical_abs,
            "workspace": workspace_abs,
            "operation": mode,
            "size_bytes": file_size
        }
    )
    
    return canonical_abs

def setup_docker_sandbox(
    image: str,
    workspace_path: str,
    network: str = "none",
    mem_limit: str = "512m",
    cpu_quota: int = 50000,
    force_rebuild: bool = False,
    idle_timeout: int = 300
) -> 'DockerExecutor':
    """
    Set up a Docker sandbox container using the centralized DockerExecutor.
    
    Args:
        image: Docker image name
        workspace_path: Host workspace path to mount
        network: Network mode ('none', 'host', 'bridge', etc.)
        mem_limit: Memory limit (e.g., '512m', '1g')
        cpu_quota: CPU quota in microseconds
        force_rebuild: Force rebuild of the Docker image
        idle_timeout: Seconds of inactivity before container can be stopped
    
    Returns:
        DockerExecutor instance with security constraints applied.
    
    Raises:
        DockerSetupError: If container setup fails.
    """
    try:
        # Import DockerExecutor (lazy import to avoid circular dependencies)
        import sys
        import os
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from docker_executor import DockerExecutor
    except ImportError as e:
        raise DockerSetupError(f"Could not import DockerExecutor: {e}")
    
    try:
        executor = DockerExecutor(
            workspace_path=workspace_path,
            image=image,
            network=network,
            mem_limit=mem_limit,
            cpu_quota=cpu_quota,
            force_rebuild=force_rebuild,
            idle_timeout=idle_timeout
        )
        _log_docker_sandbox_event(
            container_id="<lazy>",
            container_name=f"agent-exec-{hashlib.sha256(workspace_path.encode()).hexdigest()[:12]}",
            image=image,
            command=[],
            action="setup",
            status="ready"
        )
        return executor
    except Exception as e:
        raise DockerSetupError(f"Failed to create DockerExecutor: {e}")




def _log_security_event(
    event_type: Optional[Any],
    message: str,
    level: Any,
    data: Dict[str, Any]
) -> None:
    """
    Log a security-related event using the global logger if available.
    Redacts sensitive data before logging.
    """
    if _logger is None or not LOGGING_AVAILABLE:
        # Fallback to standard logging
        # Convert level to integer logging level
        if hasattr(level, 'value'):
            # LogLevel enum with string value
            level_str = level.value
            level_map = {
                "DEBUG": logging.DEBUG,
                "INFO": logging.INFO,
                "WARNING": logging.WARNING,
                "ERROR": logging.ERROR,
                "CRITICAL": logging.CRITICAL
            }
            log_level = level_map.get(level_str, logging.INFO)
        else:
            # Assume level is already an integer (logging constant)
            log_level = level
        logging.getLogger(__name__).log(log_level, message)
        return
    
    # Redact sensitive data
    redacted_data = _redact_sensitive_data(data)
    
    try:
        _logger._log_event(
            event_type=event_type,
            level=level,
            message=message,
            data=redacted_data
        )
    except Exception as e:
        # Don't let logging failures break security operations
        logging.getLogger(__name__).error(f"Failed to log security event: {e}")


def _log_docker_sandbox_event(
    container_id: str,
    container_name: str,
    image: str,
    command: List[str],
    action: str,
    status: str
) -> None:
    """Log Docker sandbox creation/start event."""
    _log_security_event(
        event_type=LogEventType.DOCKER_SANDBOX if LOGGING_AVAILABLE else None,
        message=f"Docker sandbox {action}: container {container_id}",
        level=LogLevel.INFO if LOGGING_AVAILABLE else logging.INFO,
        data={
            "container_id": container_id,
            "container_name": container_name,
            "image": image,
            "command": command,
            "action": action,
            "status": status
        }
    )


# ============================================================================
# Capability Registry
# ============================================================================

class CapabilityRegistry:
    """
    Registry for tool capabilities.
    Maps tool names to the list of capabilities they require.
    Currently uses hardcoded mappings for all tools.
    """
    
    # Hardcoded capability requirements for each tool
    # Format: tool_class_name -> list of capability strings
    REQUIRED = {
        # File operations
        "FileEditor": ["fs:read", "fs:write"],  # depends on operation, but requires both potentially
        "FilePreviewTool": ["fs:read"],
        "FileSearchTool": ["fs:read"],
        "DirectoryTreeTool": ["fs:read"],
        "FileMover": ["fs:read", "fs:write"],
        "FileSummaryTool": ["fs:read"],
        "FieldViewer": ["fs:read"],
        
        # Git operations
        "GitInfoTool": ["fs:read", "git:access"],
        
        # Docker operations
        "DockerCodeRunner": ["container:exec", "fs:read", "fs:write"],
        
        # Agent control
        "Final": [],
        "RequestUserInteraction": [],
        "SummarizeTool": ["conversation:modify"],
        
        # Utilities
        "DateTimeTool": [],
        "GlobTool": ["fs:read"],
        "PaginateTool": [],
        "ProgressReport": [],
        "FinalReport": [],
        "Thought": [],
        "RefactorTool": ["fs:read", "fs:write"],
        "ApplyEdits": ["fs:read", "fs:write"],
        "CodeModifier": ["fs:read", "fs:write"],
        "DirectoryCreator": ["fs:write"],
        
        # MCP tools (when available)
        "MCPValidator": ["mcp:access"],
        "McpechoEcho": ["mcp:access"],
        "McpechoAdd": ["mcp:access"],
    }
    
    @classmethod
    def check(cls, agent_id: str, tool_name: str, **kwargs) -> bool:
        """
        Check if the agent is allowed to execute the given tool.
        Currently always returns True (hardcoded), but logs the check.
        
        Args:
            agent_id: Identifier for the agent
            tool_name: Name of the tool class (e.g., "FileEditor")
            **kwargs: Additional context (e.g., operation, path)
        
        Returns:
            True if allowed, False otherwise.
        """
        # Get required capabilities for this tool (default empty list)
        required = cls.REQUIRED.get(tool_name, [])
        
        # For now, always grant - but log the check
        _log_capability_check(
            agent_id=agent_id,
            tool_name=tool_name,
            required_capabilities=required,
            granted=True,
            reason="hardcoded"
        )
        return True
    
    @classmethod
    def get_required_capabilities(cls, tool_name: str) -> List[str]:
        """Get the list of required capabilities for a tool."""
        return cls.REQUIRED.get(tool_name, [])


def _log_capability_check(
    agent_id: str,
    tool_name: str,
    required_capabilities: List[str],
    granted: bool,
    reason: str = "",
    data: Optional[Dict[str, Any]] = None
) -> None:
    """
    Log a capability check event.
    """
    log_data = {
        "agent_id": agent_id,
        "tool": tool_name,
        "required_capabilities": required_capabilities,
        "granted": granted,
        "reason": reason
    }
    if data:
        log_data.update(data)
    
    _log_security_event(
        event_type=LogEventType.CAPABILITY_CHECK if LOGGING_AVAILABLE else None,
        message=f"Capability check for {tool_name}: {'granted' if granted else 'denied'} (reason: {reason})",
        level=LogLevel.INFO if LOGGING_AVAILABLE else logging.INFO,
        data=log_data
    )


# ============================================================================
# Utility: Sanitize paths for logging (avoid PII if needed)
# ============================================================================

def sanitize_path_for_log(path: str, workspace_path: str) -> str:
    """
    Make a path safe for logging by potentially shortening or anonymizing.
    Currently returns the absolute path; could be enhanced to remove usernames etc.
    """
    # Optionally, replace home directory with ~ or remove username prefixes
    return path
