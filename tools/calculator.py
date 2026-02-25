from typing import Literal
from pydantic import Field
from .base import ToolBase

class Calculator(ToolBase):
    """Perform basic arithmetic."""
    a: float = Field(description="First number")
    b: float = Field(description="Second number")
    operation: Literal["add", "subtract", "multiply", "divide"] = Field(description="Operation")

    def execute(self) -> str:
        if self.operation == "add":
            res = self.a + self.b
        elif self.operation == "subtract":
            res = self.a - self.b
        elif self.operation == "multiply":
            res = self.a * self.b
        elif self.operation == "divide":
            if self.b != 0:
                res = self.a / self.b
            else:
                return "invalid division"
        return f"{self.a} {self.operation} {self.b} = {res}"