# tools/base.py
from pydantic import BaseModel, Field, ConfigDict

from typing import Literal, Any, Optional, ClassVar, List, Dict

import os

from pathlib import Path

import logging


# Import centralized security
try:
    from thoughtmachine.security import validate_path as security_validate_path, set_logger as security_set_logger
    SECURITY_AVAILABLE = True
except ImportError:
    SECURITY_AVAILABLE = False
import sys


class ToolBase(BaseModel):
    model_config = ConfigDict(extra="ignore")
    """
    All tools must inherit from this class.
    They must define a 'tool' field with a Literal of their unique name.
    They must implement execute() returning a string.
    """
    workspace_path: Optional[str] = Field(default=None, description="Root directory for file operations (None = unrestricted)")
    token_limit: Optional[int] = Field(default=None, description="Maximum token limit for tool output (None = no limit)")
    is_docker: bool = Field(default=False, description="Whether the tool is executing in a Docker container")
    container_workspace_path: Optional[str] = Field(default=None, description="Workspace path as seen from inside the container (e.g., /workspace)")

    # Security capabilities required by this tool
    requires_capabilities: ClassVar[List[str]] = []

    # Logger instance for tool debugging
    _logger: Optional[logging.Logger] = None
    _agent_logger: Optional[Any] = None

    def model_post_init(self, __context):
        super().model_post_init(__context)
        self._logger = None

    def _set_logger(self, logger: logging.Logger):
        """Set logger for this tool instance."""
        self._logger = logger

    def _set_agent_logger(self, logger: Any):
        """Set agent logger for structured tool logging."""
        self._agent_logger = logger

    def _log_debug(self, message: str, data: Optional[Dict[str, Any]] = None, tool_call_id: Optional[str] = None):
        """Log debug message using structured logging or fallback."""
        # Get tool name for structured logging
        tool_name = getattr(self, 'tool', None)
        if tool_name is None:
            tool_name = self.__class__.__name__
        
        # Try structured agent logger first
        if self._agent_logger and hasattr(self._agent_logger, 'log_tool_debug'):
            self._agent_logger.log_tool_debug(tool_name, message, data=data, tool_call_id=tool_call_id)
            return
        
        # Fallback to traditional Python logger
        if self._logger:
            self._logger.debug(message)
        else:
            # Fallback to old behavior: check THOUGHTMACHINE_DEBUG environment variable
            import os
            if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                import sys
                trunc_limit = int(os.environ.get('THOUGHTMACHINE_DEBUG_TRUNCATION', 100))
                msg = f"DEBUG: {message}"
                if trunc_limit > 0 and len(msg) > trunc_limit:
                    msg = msg[:trunc_limit] + "..."
                print(msg, file=sys.stderr)

    def _log_tool_warning(self, message: str, data: Optional[Dict[str, Any]] = None, tool_call_id: Optional[str] = None):
        """Log tool warning using structured logging or fallback."""
        tool_name = getattr(self, 'tool', None)
        if tool_name is None:
            tool_name = self.__class__.__name__
        
        if self._agent_logger and hasattr(self._agent_logger, 'log_tool_warning'):
            self._agent_logger.log_tool_warning(tool_name, message, data=data, tool_call_id=tool_call_id)
            return
        
        if self._logger:
            self._logger.warning(message)
        else:
            import sys
            print(f"WARNING: {message}", file=sys.stderr)
    
    def _log_tool_error(self, message: str, data: Optional[Dict[str, Any]] = None, tool_call_id: Optional[str] = None):
        """Log tool error using structured logging or fallback."""
        tool_name = getattr(self, 'tool', None)
        if tool_name is None:
            tool_name = self.__class__.__name__
        
        if self._agent_logger and hasattr(self._agent_logger, 'log_tool_error'):
            self._agent_logger.log_tool_error(tool_name, message, data=data, tool_call_id=tool_call_id)
            return
        
        if self._logger:
            self._logger.error(message)
        else:
            import sys
            print(f"ERROR: {message}", file=sys.stderr)
    
    def _log_tool_internal(self, message: str, data: Optional[Dict[str, Any]] = None, tool_call_id: Optional[str] = None):
        """Log tool internal event using structured logging or fallback."""
        tool_name = getattr(self, 'tool', None)
        if tool_name is None:
            tool_name = self.__class__.__name__
        
        if self._agent_logger and hasattr(self._agent_logger, 'log_tool_internal'):
            self._agent_logger.log_tool_internal(tool_name, message, data=data, tool_call_id=tool_call_id)
            return
        
        if self._logger:
            self._logger.info(f"Internal: {message}")
        else:
            import sys
            print(f"INTERNAL: {message}", file=sys.stderr)
    
    def _log_tool_performance(self, message: str, metrics: Dict[str, Any], tool_call_id: Optional[str] = None):
        """Log tool performance metrics using structured logging or fallback."""
        tool_name = getattr(self, 'tool', None)
        if tool_name is None:
            tool_name = self.__class__.__name__
        
        if self._agent_logger and hasattr(self._agent_logger, 'log_tool_performance'):
            self._agent_logger.log_tool_performance(tool_name, message, metrics, tool_call_id=tool_call_id)
            return
        
        if self._logger:
            self._logger.info(f"Performance: {message} - {metrics}")
        else:
            import sys
            print(f"PERFORMANCE: {message} - {metrics}", file=sys.stderr)
    
    def _log_tool_event(self, event_type: Any, level: Any, message: str, data: Optional[Dict[str, Any]] = None, tool_call_id: Optional[str] = None):
        """Generic tool event logging using structured logging or fallback."""
        tool_name = getattr(self, 'tool', None)
        if tool_name is None:
            tool_name = self.__class__.__name__
        
        if self._agent_logger and hasattr(self._agent_logger, 'log_tool_event'):
            self._agent_logger.log_tool_event(event_type, level, tool_name, message, data=data, tool_call_id=tool_call_id)
            return
        
        # Fallback: map level to appropriate python logging level
        if self._logger:
            level_str = str(level).lower()
            if 'error' in level_str:
                self._logger.error(message)
            elif 'warning' in level_str:
                self._logger.warning(message)
            elif 'debug' in level_str:
                self._logger.debug(message)
            else:
                self._logger.info(message)
        else:
            import sys
            print(f"TOOL [{level}]: {message}", file=sys.stderr)
    
    def execute(self) -> str:
        raise NotImplementedError

    def model_dump_tool(self) -> dict:
        """Dump all fields except 'execute' method."""
        return self.model_dump(exclude={'execute'})
    
    def _estimate_tokens(self, text: str) -> int:
        """Estimate token count for text using simple character approximation.
        Original tiktoken implementation disabled due to network issues in Docker.
        """
        # Approximation: ~4 chars per token
        return len(text) // 4
    
    def _truncate_output(self, output: str, limit: Optional[int] = None) -> str:
        """Truncate output to token limit if specified."""
        if limit is None:
            limit = self.token_limit
        if limit is None or limit <= 0:
            return output
        
        # Estimate tokens
        estimated_tokens = self._estimate_tokens(output)
        if estimated_tokens <= limit:
            return output
        
        # Need to truncate - first get approximate character limit
        # Average tokens per char ~ 0.25, but we need to be safe
        # Use binary search to find proper truncation point
        target_chars = int(limit * 4)  # Approximate upper bound
        truncated = output[:target_chars]
        
        # Ensure we don't cut in middle of multi-byte char or line
        # Find last newline before limit
        last_newline = truncated.rfind('\n')
        if last_newline > target_chars * 0.8:  # If we have a recent newline
            truncated = truncated[:last_newline]
        
        # Re-estimate and adjust if still over limit
        while self._estimate_tokens(truncated) > limit and len(truncated) > 10:
            truncated = truncated[:-100]  # Remove 100 chars at a time
        
        # Add truncation notice
        return truncated + f"\n... (output truncated to {limit} tokens, original was {estimated_tokens} tokens)"

    def _validate_path(self, path: str) -> str:
        """
        Validate that a given path is within the workspace.
        Returns absolute normalized path if valid.
        Raises ValueError if path is outside workspace.
        """
        # Use centralized security validation if available
        if SECURITY_AVAILABLE:
            # Call security module's validate_path
            # It will log the access and raise appropriate exceptions
            try:
                return security_validate_path(path, mode='read', workspace_path=self.workspace_path)
            except Exception as e:
                # Convert security exceptions to ValueError for backward compatibility
                # Try to import security exception classes
                try:
                    from thoughtmachine.security import PathOutsideWorkspaceError, SecurityError
                    if isinstance(e, (PathOutsideWorkspaceError, SecurityError)):
                        # Convert to ValueError with same message
                        raise ValueError(str(e)) from e
                except ImportError:
                    # Security module not available, just re-raise original
                    pass
                raise
        else:
            # Fallback to original implementation
            if self.workspace_path is None:
                # No restrictions
                return os.path.abspath(path)

            # Convert to absolute paths
            workspace_abs = os.path.abspath(self.workspace_path)
            # If workspace is provided, treat relative paths as relative to workspace
            if not os.path.isabs(path):
                path = os.path.join(workspace_abs, path)
            target_abs = os.path.abspath(path)

            # Ensure target is within workspace
            try:
                target_rel = os.path.relpath(target_abs, workspace_abs)
            except ValueError:
                # Paths are on different drives (Windows)
                raise ValueError(f"Path {path} is outside workspace {self.workspace_path}")

            # Check for directory traversal attempts
            if target_rel.startswith("..") or os.path.isabs(target_rel):
                raise ValueError(f"Path {path} is outside workspace {self.workspace_path}")

            return target_abs