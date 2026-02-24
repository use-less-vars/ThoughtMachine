# tools/base.py
from pydantic import BaseModel
from typing import Literal, Any

class ToolBase(BaseModel):
    """
    All tools must inherit from this class.
    They must define a 'tool' field with a Literal of their unique name.
    They must implement execute() returning a string.
    """
    tool: str  # will be overridden by Literal in subclasses

    def execute(self) -> str:
        raise NotImplementedError

    def model_dump_tool(self) -> dict:
        """Dump all fields except 'execute' method."""
        return self.model_dump(exclude={'execute'})
