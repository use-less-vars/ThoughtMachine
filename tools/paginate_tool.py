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
        params = self.tool_params.copy()
        params['page'] = self.page
        params['per_page'] = self.per_page

        try:
            # Instantiate and run
            tool_instance = tool_class(**params)
            return tool_instance.execute()
        except Exception as e:
            return f"Error executing {self.target_tool_name}: {e}"
