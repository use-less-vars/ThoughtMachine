from pydantic import Field
from typing import Literal
from .base import ToolBase

class Final(ToolBase):
    """Final answer tool. Use this when you answer a user question and want to output the final answer."""
    tool: Literal["Final"] = "Final"
    content: str = Field(description="The final answer text")

    def execute(self) -> str:
        # Final tools should not truncate their output
        return self.content
