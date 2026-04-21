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
import threading
import queue
import uuid

# Try to import the logging facade and types
try:
    from agent.logging import log, LogEventType, LogLevel
    LOGGING_AVAILABLE = True
except ImportError:
    LOGGING_AVAILABLE = False
    log = None
    LogEventType = None
    LogLevel = None

# Try to import event system for interactive security prompts
try:
    from agent.events import global_event_bus, EventType, create_event
    EVENT_SYSTEM_AVAILABLE = True
except ImportError:
    EVENT_SYSTEM_AVAILABLE = False
    global_event_bus = None
    EventType = None
    create_event = None

# Global logger instance (set by agent at startup)
_logger: Optional[Any] = None

# Security prompt management
_pending_security_requests: Dict[str, queue.Queue] = {}
_pending_requests_lock = threading.Lock()

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


def set_logger(logger: Optional[Any]) -> None:
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
    if log is None or not LOGGING_AVAILABLE:
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
        # Use unified logging facade
        if log is not None:
            # Convert level to string if needed
            level_str = level.value if hasattr(level, 'value') else str(level)
            # Map to appropriate tag
            tag = "security.operation"
            log(level_str, tag, message, redacted_data, event_type)
        elif _logger is not None and hasattr(_logger, '_log_event'):
            # Fallback to old logger if available
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
    
    # Hardcoded capability requirements for each tool (fallback)
    # Format: tool_class_name -> list of capability strings
    _HARDCODED_REQUIRED = {
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
    
    _discovered_required: Optional[Dict[str, List[str]]] = None
    
    @classmethod
    def _build_capability_map(cls) -> Dict[str, List[str]]:
        """Build capability map by discovering tool classes and their requires_capabilities."""
        if cls._discovered_required is not None:
            return cls._discovered_required
            
        discovered = {}
        
        try:
            # Import tools module to get SIMPLIFIED_TOOL_CLASSES
            from tools import SIMPLIFIED_TOOL_CLASSES
            
            for tool_cls in SIMPLIFIED_TOOL_CLASSES:
                tool_name = tool_cls.__name__
                # Get requires_capabilities class variable (default empty list)
                required = getattr(tool_cls, 'requires_capabilities', [])
                # Only include non-empty capability lists (allows tools to override hardcoded)
                if required:
                    discovered[tool_name] = required
                
        except ImportError as e:
            # If tools module not available, fall back to hardcoded
            discovered = cls._HARDCODED_REQUIRED.copy()
        
        cls._discovered_required = discovered
        return discovered
    
    @classmethod
    def get_required_map(cls) -> Dict[str, List[str]]:
        """Get the complete capability requirement map."""
        # Start with hardcoded as base (ensures all tools have at least something)
        combined = cls._HARDCODED_REQUIRED.copy()
        # Update with discovered values (may override hardcoded)
        discovered = cls._build_capability_map()
        combined.update(discovered)
        return combined
    
    @classmethod
    def check(cls, agent_id: str, tool_name: str, security_config: Optional[Dict[str, Any]] = None, **kwargs) -> bool:
        """
        Check if the agent is allowed to execute the given tool.
        Uses security configuration and policy evaluation.
        
        Args:
            agent_id: Identifier for the agent
            tool_name: Name of the tool class (e.g., "FileEditor")
            security_config: Security configuration dict (optional)
            **kwargs: Additional context (e.g., operation, path)
        
        Returns:
            True if allowed, False otherwise.
        """
        # Delegate to is_allowed function
        return is_allowed(agent_id, tool_name, security_config, **kwargs)
    
    @classmethod
    def get_required_capabilities(cls, tool_name: str) -> List[str]:
        """Get the list of required capabilities for a tool."""
        return cls.get_required_map().get(tool_name, [])


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


def _ensure_security_response_handler() -> None:
    """Ensure the security response handler is registered with the event bus."""
    if not EVENT_SYSTEM_AVAILABLE or global_event_bus is None:
        return
    
    # Check if already registered
    if hasattr(_ensure_security_response_handler, '_handler_registered'):
        return
    
    def _handle_security_response(event):
        """Handle security response events from GUI."""
        if event.type != EventType.SECURITY_RESPONSE:
            return
        
        request_id = event.data.get("request_id")
        approved = event.data.get("approved", False)
        remember = event.data.get("remember", False)
        
        with _pending_requests_lock:
            q = _pending_security_requests.pop(request_id, None)
        
        if q is not None:
            q.put((approved, remember))
        else:
            # Log warning about orphaned response
            if _logger and LOGGING_AVAILABLE:
                _logger.warning(f"Received security response for unknown request: {request_id}")
    
    # Register handler
    global_event_bus.subscribe(_handle_security_response)
    _ensure_security_response_handler._handler_registered = True


def _request_security_prompt(
    agent_id: str,
    tool_name: str,
    required_capabilities: List[str],
    arguments: Dict[str, Any],
    security_config: Dict[str, Any],
    policy_type: str,  # "tool_override", "capability", "default", "agent_override"
    policy_target: str,  # tool_name or capability name or "default" or agent_id
    session_id: Optional[str] = None
) -> bool:
    """
    Request user approval via security prompt.
    
    Returns:
        True if approved, False if denied.
    """
    if not EVENT_SYSTEM_AVAILABLE or global_event_bus is None:
        # Fallback: allow with warning
        _log_capability_check(
            agent_id=agent_id,
            tool_name=tool_name,
            required_capabilities=required_capabilities,
            granted=True,
            reason=f"{policy_type}_ask_fallback_allow",
            data=arguments
        )
        return True
    
    # Ensure response handler is registered
    _ensure_security_response_handler()
    
    # Generate unique request ID
    request_id = str(uuid.uuid4())
    
    # Create queue for response
    response_queue = queue.Queue(maxsize=1)
    with _pending_requests_lock:
        _pending_security_requests[request_id] = response_queue
    
    # Emit security prompt event
    try:
        event = create_event(
            event_type=EventType.SECURITY_PROMPT,
            data={
                "request_id": request_id,
                "agent_id": agent_id,
                "tool_name": tool_name,
                "capabilities": required_capabilities,
                "arguments": arguments,
                "session_id": session_id,
                "policy_type": policy_type,
                "policy_target": policy_target
            },
            source="security",
            session_id=session_id
        )
        global_event_bus.publish(event)
    except Exception as e:
        # Log error and fallback to allow
        logging.getLogger(__name__).error(f"Failed to emit security prompt: {e}")
        with _pending_requests_lock:
            _pending_security_requests.pop(request_id, None)
        _log_capability_check(
            agent_id=agent_id,
            tool_name=tool_name,
            required_capabilities=required_capabilities,
            granted=True,
            reason=f"{policy_type}_ask_event_failed",
            data=arguments
        )
        return True
    
    # Wait for response (blocking)
    try:
        approved, remember = response_queue.get(timeout=300)  # 5 minute timeout
    except queue.Empty:
        # Timeout - fallback to deny for safety
        with _pending_requests_lock:
            _pending_security_requests.pop(request_id, None)
        _log_capability_check(
            agent_id=agent_id,
            tool_name=tool_name,
            required_capabilities=required_capabilities,
            granted=False,
            reason=f"{policy_type}_ask_timeout",
            data=arguments
        )
        return False
    
    # If remember is True, update security configuration
    if remember and security_config is not None:
        _update_security_config(
            security_config=security_config,
            policy_type=policy_type,
            policy_target=policy_target,
            approved=approved
        )
    
    # Log result
    _log_capability_check(
        agent_id=agent_id,
        tool_name=tool_name,
        required_capabilities=required_capabilities,
        granted=approved,
        reason=f"{policy_type}_ask_user_{'approved' if approved else 'denied'}",
        data=arguments
    )
    
    return approved


def _update_security_config(
    security_config: Dict[str, Any],
    policy_type: str,
    policy_target: str,
    approved: bool
) -> None:
    """Update security configuration based on user's 'remember' choice."""
    policy_value = "allow" if approved else "deny"
    
    session_policy = security_config.setdefault("session_policy", {})
    
    if policy_type == "tool_override":
        tool_overrides = session_policy.setdefault("tool_overrides", {})
        tool_overrides[policy_target] = policy_value
    elif policy_type == "capability":
        capability_requirements = session_policy.setdefault("capability_requirements", {})
        capability_requirements[policy_target] = policy_value
    elif policy_type == "agent_override":
        agent_overrides = security_config.setdefault("agent_overrides", {})
        agent_overrides[policy_target] = policy_value
    # default policy is not stored per-target
    
    # Log the update
    if _logger and LOGGING_AVAILABLE:
        _logger.info(
            f"Security configuration updated: {policy_type} {policy_target} = {policy_value}"
        )


def is_allowed(
    agent_id: str,
    tool_name: str,
    security_config: Optional[Dict[str, Any]] = None,
    **kwargs
) -> bool:
    """
    Determine if a tool is allowed based on security configuration.
    
    Args:
        agent_id: Identifier for the agent
        tool_name: Name of the tool class
        security_config: Security configuration dict (defaults to default config)
        **kwargs: Additional context (e.g., path, operation)
    
    Returns:
        True if allowed, False otherwise.
    """
    # Use default config if none provided
    if security_config is None:
        security_config = get_default_security_config()
    
    # Get required capabilities for the tool
    required_caps = CapabilityRegistry.get_required_capabilities(tool_name)
    
    # Check session read-only restriction
    session_policy = security_config.get("session_policy", {})
    if session_policy.get("read_only", False):
        # Check if any required capability implies write/modification
        write_capabilities = {"fs:write", "fs:delete", "fs:move", "fs:create", "container:exec", "conversation:modify"}
        for cap in required_caps:
            if cap in write_capabilities:
                _log_capability_check(
                    agent_id=agent_id,
                    tool_name=tool_name,
                    required_capabilities=required_caps,
                    granted=False,
                    reason="read_only_session",
                    data=kwargs
                )
                return False
    
    # Check tool-specific overrides
    tool_overrides = session_policy.get("tool_overrides", {})
    if tool_name in tool_overrides:
        policy = tool_overrides[tool_name]
        if policy == "deny":
            _log_capability_check(
                agent_id=agent_id,
                tool_name=tool_name,
                required_capabilities=required_caps,
                granted=False,
                reason="tool_override_deny",
                data=kwargs
            )
            return False
        elif policy == "allow":
            _log_capability_check(
                agent_id=agent_id,
                tool_name=tool_name,
                required_capabilities=required_caps,
                granted=True,
                reason="tool_override_allow",
                data=kwargs
            )
            return True
        elif policy == "ask":
            # Request user approval via security prompt
            return _request_security_prompt(
                agent_id=agent_id,
                tool_name=tool_name,
                required_capabilities=required_caps,
                arguments=kwargs,
                security_config=security_config,
                policy_type="tool_override",
                policy_target=tool_name,
                session_id=security_config.get("session_id")
            )
        # Unknown policy falls through
    
    # Check capability requirements
    capability_requirements = session_policy.get("capability_requirements", {})
    for cap in required_caps:
        if cap in capability_requirements:
            policy = capability_requirements[cap]
            if policy == "deny":
                _log_capability_check(
                    agent_id=agent_id,
                    tool_name=tool_name,
                    required_capabilities=required_caps,
                    granted=False,
                    reason=f"capability_deny:{cap}",
                    data=kwargs
                )
                return False
            elif policy == "ask":
                # Request user approval for this specific capability
                approved = _request_security_prompt(
                    agent_id=agent_id,
                    tool_name=tool_name,
                    required_capabilities=required_caps,
                    arguments=kwargs,
                    security_config=security_config,
                    policy_type="capability",
                    policy_target=cap,
                    session_id=security_config.get("session_id")
                )
                if not approved:
                    return False
                # If approved, continue checking other capabilities
            # policy == "allow" falls through
    
    # Check agent-specific overrides
    agent_overrides = security_config.get("agent_overrides", {})
    if agent_id in agent_overrides:
        agent_policy = agent_overrides[agent_id]
        # Similar logic as tool_overrides but for agent
        if agent_policy == "deny":
            _log_capability_check(
                agent_id=agent_id,
                tool_name=tool_name,
                required_capabilities=required_caps,
                granted=False,
                reason="agent_override_deny",
                data=kwargs
            )
            return False
        elif agent_policy == "allow":
            _log_capability_check(
                agent_id=agent_id,
                tool_name=tool_name,
                required_capabilities=required_caps,
                granted=True,
                reason="agent_override_allow",
                data=kwargs
            )
            return True
        elif agent_policy == "ask":
            # Request user approval for agent override
            return _request_security_prompt(
                agent_id=agent_id,
                tool_name=tool_name,
                required_capabilities=required_caps,
                arguments=kwargs,
                security_config=security_config,
                policy_type="agent_override",
                policy_target=agent_id,
                session_id=security_config.get("session_id")
            )
    
    # Apply default policy
    default_policy = session_policy.get("default_policy", "ask")
    if default_policy == "deny":
        _log_capability_check(
            agent_id=agent_id,
            tool_name=tool_name,
            required_capabilities=required_caps,
            granted=False,
            reason="default_policy_deny",
            data=kwargs
        )
        return False
    elif default_policy == "ask":
        # Request user approval for default policy
        return _request_security_prompt(
            agent_id=agent_id,
            tool_name=tool_name,
            required_capabilities=required_caps,
            arguments=kwargs,
            security_config=security_config,
            policy_type="default",
            policy_target="default",
            session_id=security_config.get("session_id")
        )
    # default_policy == "allow"
    
    # Allow by default
    _log_capability_check(
        agent_id=agent_id,
        tool_name=tool_name,
        required_capabilities=required_caps,
        granted=True,
        reason="default_allow",
        data=kwargs
    )
    return True


# ============================================================================
# Security Configuration
# ============================================================================

def get_default_security_config() -> Dict[str, Any]:
    """Return the default security configuration for a session."""
    return {
        "version": 1,
        "session_policy": {
            "read_only": False,
            "allowed_networks": [],  # list of domain patterns
            "tool_overrides": {},    # {"FileEditor": "ask", "DockerCodeRunner": "deny"}
            "default_policy": "allow",  # "allow", "ask", "deny"
            "capability_requirements": {}  # {"fs:write": "ask", "container:exec": "deny"}
        },
        "agent_overrides": {}  # for future multi-agent support
    }


def merge_security_config(user_config: Dict[str, Any]) -> Dict[str, Any]:
    """Merge user-provided security config with defaults."""
    default = get_default_security_config()
    # Deep merge: update nested dictionaries
    merged = default.copy()
    # Handle version
    merged["version"] = user_config.get("version", default["version"])
    # Merge session_policy
    if "session_policy" in user_config:
        merged["session_policy"] = default["session_policy"].copy()
        merged["session_policy"].update(user_config["session_policy"])
    # Merge agent_overrides
    if "agent_overrides" in user_config:
        merged["agent_overrides"] = user_config["agent_overrides"]
    return merged


def get_security_profile(profile_name: str) -> Dict[str, Any]:
    """
    Get a predefined security profile configuration.
    
    Available profiles:
    - "default": Default configuration (ask policy)
    - "read_only": Read-only assistant (no write operations)
    - "file_editor": Allows file operations, denies container and network
    - "sandboxed": Allows everything but with strong sandboxing
    - "permissive": Allow all operations (no restrictions)
    - "restricted": Deny all by default, explicit allow list
    """
    profiles = {
        "default": get_default_security_config(),
        
        "read_only": {
            "version": 1,
            "session_policy": {
                "read_only": True,
                "allowed_networks": [],
                "default_policy": "ask",
                "tool_overrides": {
                    "DockerCodeRunner": "deny",
                    "MCPValidator": "deny",
                    "McpechoEcho": "deny",
                    "McpechoAdd": "deny"
                },
                "capability_requirements": {}
            },
            "agent_overrides": {}
        },
        
        "file_editor": {
            "version": 1,
            "session_policy": {
                "read_only": False,
                "allowed_networks": [],
                "default_policy": "ask",
                "tool_overrides": {
                    "DockerCodeRunner": "deny",
                    "MCPValidator": "deny",
                    "McpechoEcho": "deny",
                    "McpechoAdd": "deny"
                },
                "capability_requirements": {
                    "fs:write": "ask",
                    "container:exec": "deny",
                    "mcp:access": "deny"
                }
            },
            "agent_overrides": {}
        },
        
        "sandboxed": {
            "version": 1,
            "session_policy": {
                "read_only": False,
                "allowed_networks": [],
                "default_policy": "ask",
                "tool_overrides": {},
                "capability_requirements": {
                    "container:exec": "ask",
                    "mcp:access": "ask",
                    "git:access": "ask"
                }
            },
            "agent_overrides": {}
        },
        
        "permissive": {
            "version": 1,
            "session_policy": {
                "read_only": False,
                "allowed_networks": [],
                "default_policy": "allow",
                "tool_overrides": {},
                "capability_requirements": {}
            },
            "agent_overrides": {}
        },
        
        "restricted": {
            "version": 1,
            "session_policy": {
                "read_only": False,
                "allowed_networks": [],
                "default_policy": "deny",
                "tool_overrides": {
                    "Final": "allow",
                    "FinalReport": "allow",
                    "RequestUserInteraction": "allow",
                    "SummarizeTool": "allow"
                },
                "capability_requirements": {}
            },
            "agent_overrides": {}
        }
    }
    
    return profiles.get(profile_name, get_default_security_config())


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
