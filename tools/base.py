# tools/base.py
from pydantic import BaseModel, Field, ConfigDict
from pydantic.json_schema import SkipJsonSchema

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


def _safe_stderr_print(message: str) -> None:
    """Print *message* to stderr without crashing when stderr is None.

    In headless daemon launches CPython may set ``sys.stderr`` to ``None``;
    ``print(x, file=None)`` then raises
    ``AttributeError: 'NoneType' object has no attribute 'write'``.
    This helper makes debug logging best-effort in any environment.
    """
    try:
        print(message, file=sys.stderr)
    except Exception:
        pass


class ToolBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    """
    All tools must inherit from this class.
    They must define a 'tool' field with a Literal of their unique name.
    They must implement execute() returning a string.
    """
    workspace_path: Optional[str] = Field(default=None, description="[DEPRECATED] Root directory for file operations (None = unrestricted). Tools should call _resolve_registry_workspace() instead.")
    token_limit: Optional[int] = Field(default=None, description="Maximum token limit for tool output (None = no limit)")
    is_docker: bool = Field(default=False, description="Whether the tool is executing in a Docker container")
    container_workspace_path: Optional[str] = Field(default=None, description="Workspace path as seen from inside the container (e.g., /workspace)")

    # Optional NARROWER root for path-anchored tool operations (e.g. a git
    # worktree living inside the registered workspace). Injected by
    # ToolExecutor only when AgentConfig.worktree_root is truthy; None means
    # "use the registered workspace root". Never inherited by sub-workers.
    worktree_root: Optional[str] = Field(
        default=None,
        description="Optional narrower workspace root (e.g. a git worktree) for path-anchored operations; None = registered workspace root.",
    )

    # Session permissions profile injected by ToolExecutor before execute()
    # Controls container network, filesystem mode, etc.
    session_permissions: SkipJsonSchema[Optional[Dict[str, Any]]] = Field(
        default=None,
        description='Session permissions dict injected by ToolExecutor.',
    )

    # Effective merged permissions injected by ToolExecutor for in-tool atomic re-checks.
    # Computed as session_permissions × workspace_capabilities.
    effective_permissions: SkipJsonSchema[Optional[Dict[str, Any]]] = Field(
        default=None,
        description='Effective merged permissions (session × workspace) for atomic checks.',
    )

    # Agent config dict injected by ToolExecutor before execute().
    # Contains runtime-observable settings such as temperature, max_turns,
    # provider, model, tool_output_token_limit, and workspace_path.
    agent_config: SkipJsonSchema[Optional[Dict[str, Any]]] = Field(default=None, exclude=True)

    # Session ID injected by ToolExecutor. Set only for tools that
    # declare this field; inherited by all tools that extend ToolBase.
    # SECURITY (C68): the model must not be told it may supply a session id.
    # ``SkipJsonSchema`` removes the field from every generated JSON schema
    # (``model_json_schema()`` and therefore ``model_to_openai_tool``); plain
    # ``exclude=True`` alone only affects ``model_dump``. The value is an
    # AUTHORITY FACT injected by ToolExecutor (it is popped out of the caller's
    # arguments before validation), never an argument.
    session_id: SkipJsonSchema[Optional[str]] = Field(
        default=None,
        exclude=True,
        description="Session ID injected by ToolExecutor",
    )

    # Worker-context flag injected by ToolExecutor before execute(). True when
    # the tool runs inside a worker sub-agent with no interactive user; tools
    # that self-gate on the 'ask' grain must deny instead of prompting.
    is_worker_context: SkipJsonSchema[bool] = Field(default=False, description="Whether tool runs in worker context (no interactive user); injected by ToolExecutor.")

    # Security capabilities required by this tool
    requires_capabilities: ClassVar[List[str]] = []

    # Permission categories required by this tool (e.g., ['container:true'])
    # Empty list means no special permissions needed.
    required_categories: ClassVar[List[str]] = []

    # SECURITY (C68): True for tools whose actions reach a SESSION-SCOPED
    # resource (worker spawn/park/journal). ToolExecutor REFUSES such a call
    # when it has no authenticated session id, instead of injecting ``''`` --
    # an empty id is not "no session needed", it is "no owner", and the worker
    # layer would silently degrade it to the shared '<no-session>' scope.
    requires_authenticated_session: ClassVar[bool] = False

    # True only for tools that build a CHILD AGENT in the same session trust
    # domain (e.g. the Worker tool).  Such a tool may receive the live provider
    # api key inside the injected ``agent_config`` dict; every handover is
    # audited as a CREDENTIAL_USE event.  All other tools get the key omitted.
    inherits_parent_api_key: ClassVar[bool] = False

    # Name of the hidden resource (see agent/config/defaults.RESOURCE_REGISTRY)
    # this tool must execute inside, or None for tools that need no resource
    # container. When set, tool execution is denied unless the session holds
    # the matching ``<resource>:read`` permission (see
    # security_gate.check_requires_resource).
    requires_resource: ClassVar[Optional[str]] = None

    # Stable registry/display name for this tool: used in LLM schemas
    # (model_to_openai_tool), preset lists, and the /api/tools endpoint.
    # Defaults to the class name when unset (legacy behavior).
    name: ClassVar[str] = ""

    @classmethod
    def tool_name(cls) -> str:
        """Return the stable tool identifier (``name`` ClassVar or class name)."""
        return cls.name or cls.__name__

    # If True, framework-level output truncation is skipped for this tool.
    # Use this for tools whose output must always be complete (e.g., Respond, SummarizeTool).
    skip_output_truncation: ClassVar[bool] = False

    @classmethod
    def get_required_categories(cls, params: dict | None = None) -> list[str]:
        """
        Return the permission categories required for this tool given params.

        Default implementation returns the static ``required_categories`` ClassVar.
        Subclasses can override to provide operation-level granularity
        (e.g., FileEditor returns ``["filesystem:read"]`` for read ops,
        ``["filesystem:write"]`` for write/delete ops).

        Args:
            params: The tool call arguments dict (may be None for static checks).

        Returns:
            List of category strings, e.g. ``["filesystem:write"]``.
        """
        return cls.required_categories

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
                trunc_limit = int(os.environ.get('TM_DEBUG_TRUNCATE_LENGTH', 100))
                msg = f"DEBUG: {message}"
                if trunc_limit > 0 and len(msg) > trunc_limit:
                    msg = msg[:trunc_limit] + "..."
                _safe_stderr_print(msg)

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
            import os
            if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                import sys
                _safe_stderr_print(f"WARNING: {message}")
    
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
            import os
            if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                import sys
                _safe_stderr_print(f"ERROR: {message}")
    
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
            import os
            if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                import sys
                _safe_stderr_print(f"INTERNAL: {message}")
    
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
            import os
            if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                import sys
                _safe_stderr_print(f"PERFORMANCE: {message} - {metrics}")
    
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
            import os
            if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                import sys
                _safe_stderr_print(f"TOOL [{level}]: {message}")
    
    def _resolve_registry_workspace(self) -> Optional[str]:
        """
        Resolve workspace path from registries (primary path).

        Queries SessionRegistry + WorkspaceRegistry first, falling back to
        the deprecated ``AgentConfig.workspace_path`` field.  Returns None if
        no workspace path can be resolved at all.

        All tools should call this instead of reading ``self.workspace_path``
        directly.
        """
        ws_path = None
        session_id = getattr(self, 'session_id', None)
        if session_id:
            try:
                from session.session_registry import SessionRegistry
                from thoughtmachine.workspace_registry import WorkspaceRegistry
                session_info = SessionRegistry.get_default().get(session_id)
                ws_id = session_info.get("workspace_id") if session_info else None
                if ws_id:
                    entry = WorkspaceRegistry.get_default().get_workspace(ws_id)
                    ws_path = entry.root_path if entry else None
            except Exception:
                pass

        if not ws_path:
            ws_path = getattr(self, 'workspace_path', None)
            if ws_path:
                logging.getLogger(self.__class__.__name__).warning(
                    "%s falling back to deprecated AgentConfig.workspace_path",
                    self.__class__.__name__,
                )

        return ws_path

    def _worktree_root(self) -> Optional[str]:
        """Return the validated ``worktree_root``, or None when unset.

        Fails closed: a configured value that cannot be proven to be a real
        directory inside the registered workspace root raises ``ValueError``.
        It NEVER falls back to the wider registered root, because a silent
        fallback would make a worktree-bound agent write into the main
        checkout.
        """
        raw = getattr(self, 'worktree_root', None)
        if not raw:
            return None

        wt = os.path.realpath(raw)
        if not os.path.isdir(wt):
            raise ValueError(
                f"Invalid worktree_root {raw!r}: not an existing directory"
            )

        registry_root = self._resolve_registry_workspace()
        if not registry_root:
            raise ValueError(
                f"Invalid worktree_root {raw!r}: workspace root could not be resolved"
            )

        root_real = os.path.realpath(registry_root)
        if not os.path.isdir(root_real):
            raise ValueError(
                f"Invalid worktree_root {raw!r}: workspace root "
                f"{registry_root!r} is not a directory"
            )

        # Component-aware containment: a bare prefix test would accept
        # '/ws-evil' for a root of '/ws'.
        if wt != root_real and not wt.startswith(root_real + os.sep):
            raise ValueError(
                f"worktree_root {raw!r} is outside workspace {registry_root!r}"
            )

        # Reuse the centralized vault barrier so a worktree that points into the
        # vault is rejected by exactly the same rules as any other path.
        if SECURITY_AVAILABLE:
            try:
                security_validate_path(wt, mode='read', workspace_path=root_real)
            except Exception as e:
                raise ValueError(str(e)) from e

        return wt

    def _resolve_workspace_root(self) -> Optional[str]:
        """
        Resolve the effective root for path-anchored tool operations.

        Returns the validated ``worktree_root`` when configured, else the
        registered workspace root. Path-anchored tools (file-walk bases,
        container bind-mount sources, git working dirs) should call this
        instead of :meth:`_resolve_registry_workspace` directly.
        """
        wt = self._worktree_root()
        if wt:
            return wt
        return self._resolve_registry_workspace()

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
        if self.skip_output_truncation:
            return output
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
        # Resolve the effective path root (worktree_root when configured,
        # else the registered workspace) from registries first
        resolved_ws = self._resolve_workspace_root()

        # Use centralized security validation if available
        if SECURITY_AVAILABLE:
            # Call security module's validate_path
            # It will log the access and raise appropriate exceptions
            try:
                return security_validate_path(path, mode='read', workspace_path=resolved_ws)
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
            # Fallback to original implementation using resolved workspace path
            if not resolved_ws:
                # No restrictions
                return os.path.abspath(path)

            # Convert to absolute paths
            workspace_abs = os.path.abspath(resolved_ws)
            # If workspace is provided, treat relative paths as relative to workspace
            if not os.path.isabs(path):
                path = os.path.join(workspace_abs, path)
            target_abs = os.path.abspath(path)

            # Ensure target is within workspace
            try:
                target_rel = os.path.relpath(target_abs, workspace_abs)
            except ValueError:
                # Paths are on different drives (Windows)
                raise ValueError(f"Path {path} is outside workspace {resolved_ws}")

            # Check for directory traversal attempts
            if target_rel.startswith("..") or os.path.isabs(target_rel):
                raise ValueError(f"Path {path} is outside workspace {resolved_ws}")

            return target_abs