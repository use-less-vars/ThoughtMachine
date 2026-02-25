from pydantic import Field
from .base import ToolBase

class Final(ToolBase):
    """Final answer tool. Use this when you have completed the task and want to output the final answer."""
    content: str = Field(description="The final answer text")

    def execute(self) -> str:
        return self.content