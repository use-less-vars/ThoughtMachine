from typing import Literal
from .base import ToolBase
from pydantic import Field

class Thought(ToolBase):
    """Write down reasoning"""
    content: str = Field(description="Thought content") 

    def execute(self) -> str:
        return self.content
