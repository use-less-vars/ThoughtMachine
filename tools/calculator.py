from typing import Literal
from .base import ToolBase
from pydantic import Field

class Calculator(ToolBase):
    tool: Literal['calculator'] = Field(default = 'calculator', description="Perform basic arithmetic")
    a: float
    b: float
    operation: Literal["add", "subtract", "multiply", "divide"]

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

