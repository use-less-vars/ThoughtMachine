"""
PaginateTool - Add pagination to any ToolBase that returns list results.

This meta-tool wraps another tool's execution and adds pagination to large results.
It's designed to work with tools that return newline-delimited or list-based output.
"""
from .base import ToolBase
from typing import ClassVar, List, Any, Literal
from pydantic import Field

class PaginateTool(ToolBase):
    required_categories: ClassVar[List[str]] = ["filesystem:read"]
    """Wrap another tool's execution with pagination.

    Use this to page through large results from any file listing or search tool.

    Example workflow:
    1. First call: target_tool_name='GlobTool', page=1, per_page=50, directory='.', pattern='*.py'
    2. If result says "Page 1 of 5", call again with page=2

    Attributes:
        target_tool_name: Name of the tool class to paginate (e.g., 'GlobTool', 'DirectoryTreeTool')
        tool_params: Dictionary of parameters for that tool
        page: Page number (1-indexed)
        per_page: Results per page (0 = all)
    """
    tool: Literal["PaginateTool"] = "PaginateTool"


    target_tool_name: str = Field(
        description="Name of the tool class to paginate (e.g., 'GlobTool', 'DirectoryTreeTool')"
    )
    tool_params: dict = Field(
        default_factory=dict,
        description="Parameters to pass to the wrapped tool (as a dictionary)"
    )
    page: int = Field(
        default=1,
        ge=1,
        description="Page number to retrieve (1-indexed)"
    )
    per_page: int = Field(
        default=100,
        ge=0,
        description="Number of results per page (0 = all results, no pagination)"
    )

    # Context fields forwarded to the wrapped tool when it declares them.
    # ToolBase uses ``extra="forbid"``, so a field the wrapped class does not
    # declare must never be passed through.
    _CONTEXT_FIELDS: ClassVar[tuple] = (
        "workspace_path",
        "token_limit",
        "is_docker",
        "container_workspace_path",
        "session_permissions",
        "effective_permissions",
        "agent_config",
        "session_id",
        "is_worker_context",
        "worker_name",
    )

    def _build_params(self, tool_class: type) -> dict:
        """Build the wrapped tool's kwargs from tool_params + injected context.

        Pagination (``page``/``per_page``) is added only for tools that
        actually declare those fields; injecting them unconditionally made
        every non-paginating tool fail with ``extra_forbidden``.
        """
        field_names = set(tool_class.model_fields)

        # Injected context is authoritative: a caller must not be able to forge
        # it through ``tool_params`` (e.g. ``workspace_path``) and thereby
        # re-open the containment the wrapped tool applies.  Colliding caller
        # keys are dropped silently, mirroring the executor, which also strips
        # infrastructure keys instead of rejecting the call.
        params = {
            name: value
            for name, value in self.tool_params.items()
            if name not in self._CONTEXT_FIELDS
        }
        for name in self._CONTEXT_FIELDS:
            if name not in field_names:
                continue
            value = getattr(self, name, None)
            if value is not None:
                params[name] = value

        if "page" in field_names:
            params["page"] = self.page
        if "per_page" in field_names:
            params["per_page"] = self.per_page
        return params

    def execute(self) -> str:
        """Execute the wrapped tool with pagination applied."""
        from tools import TOOL_CLASSES

        # Find the tool class
        tool_class = None
        for cls in TOOL_CLASSES:
            if cls.__name__ == self.target_tool_name:
                tool_class = cls
                break

        if tool_class is None:
            return f"Error: Tool '{self.target_tool_name}' not found. Available tools: {[c.__name__ for c in TOOL_CLASSES]}"

        # Execute the wrapped tool with merged pagination params
        params = self._build_params(tool_class)

        try:
            # Instantiate and run
            tool_instance = tool_class(**params)
            return tool_instance.execute()
        except Exception as e:
            return f"Error executing {self.target_tool_name}: {e}"
