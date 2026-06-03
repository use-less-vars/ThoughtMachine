from typing import ClassVar, List, Literal
from .base import ToolBase
from pydantic import Field

class Thought(ToolBase):
    required_categories: ClassVar[List[str]] = ["filesystem:read"]
    """Write down reasoning"""
    tool: Literal["Thought"] = "Thought"
    content: str = Field(description="Thought content") 

    def execute(self) -> str:
        return self._truncate_output(self.content)
