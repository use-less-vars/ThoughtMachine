from typing import Literal
from .base import ToolBase
from pydantic import Field

class RequestUserInteraction(ToolBase):
    """Request user interaction tool. Use this when you need to ask the user a question, request clarification, or get additional information in an interactive session."""
    message: str = Field(description="The question or message to present to the user")

    def execute(self) -> str:
        # Return the message; the agent controller will detect this tool and pause for user input
        return self.message