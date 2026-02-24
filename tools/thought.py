from typing import Literal
from .base import ToolBase
from pydantic import Field

class Thought(ToolBase):
    tool: Literal['thought'] = Field(default = 'thought', description="Write down reasoning")
    content: str

    def execute(self) -> str:
        return self.content
