from typing import Literal
from .base import ToolBase
from pydantic import Field


class Final(ToolBase):
    tool: Literal['final'] = Field(default = 'final',description="Final answer")
    content: str

    def execute(self) -> str:
        return self.content

