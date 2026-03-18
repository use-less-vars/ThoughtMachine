from typing import Literal
from .base import ToolBase
from pydantic import Field

class Thought(ToolBase):
    """Write down reasoning"""
    tool: Literal["Thought"] = "Thought"
    content: str = Field(description="Thought content") 

    def execute(self) -> str:
        return self._truncate_output(self.content)
