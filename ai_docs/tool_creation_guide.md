# Tool Creation Guide

This guide explains how to create new tools for the AI agent system. Tools are Python classes that inherit from `ToolBase` and implement the `execute()` method.

## Overview

Tools are the primary way the AI agent interacts with the environment. Each tool:
1. Represents a discrete action (file operations, calculations, etc.)
2. Has a well-defined schema for inputs (using Pydantic)
3. Returns a string result (success or error message)
4. Is automatically discovered and registered by the system

## File Structure

Create a new Python file in the `tools/` directory with the following naming convention:
- Use snake_case
- Descriptive name (e.g., `file_line_writer.py`, `calculator.py`)
- No spaces or special characters

## Tool Class Structure

```python
# tools/your_tool_name.py
from .base import ToolBase
from pydantic import Field

class YourToolName(ToolBase):
    """Brief description of what the tool does."""
    
    # Define input fields with Field() for descriptions
    param1: str = Field(description="Description of parameter 1")
    param2: int = Field(default=0, description="Description with default")
    
    def execute(self) -> str:
        """
        Implement the tool's functionality here.
        - Access parameters via self.param1, self.param2
        - Return a string result (success or error message)
        - Handle exceptions gracefully
        """
        try:
            # Your implementation
            result = f"Successfully did something with {self.param1}"
            return result
        except Exception as e:
            return f"Error: {e}"
```

## Required Elements

### 1. **Class Definition**
- Must inherit from `ToolBase`
- Class name should be CamelCase version of filename (e.g., `FileLineWriter` for `file_line_writer.py`)

### 2. **Docstring**
- First line: Brief description (appears in tool definitions sent to LLM)
- Can include additional details about usage

### 3. **Field Definitions**
- Use `pydantic.Field` for each parameter
- Provide clear, concise `description` (this is shown to the LLM)
- Set `default` values where appropriate
- Type hints are required (e.g., `str`, `int`, `List[str]`)

### 4. **Execute Method**
- Must return a string
- Should handle exceptions and return error messages
- Keep responses informative but concise
- For file operations, prefer partial tools (see Best Practices)

## Example: Simple Tool

```python
# tools/example_tool.py
from .base import ToolBase
from pydantic import Field

class ExampleTool(ToolBase):
    """Concatenates two strings with a separator."""
    
    string_a: str = Field(description="First string")
    string_b: str = Field(description="Second string")
    separator: str = Field(default=" ", description="Separator between strings")
    
    def execute(self) -> str:
        try:
            result = f"{self.string_a}{self.separator}{self.string_b}"
            return f"Concatenated result: {result}"
        except Exception as e:
            return f"Error concatenating strings: {e}"
```

## Example: File Operation Tool

```python
# tools/file_operation_example.py
from .base import ToolBase
from pydantic import Field
import os

class FileOperationExample(ToolBase):
    """Creates a file with optional content."""
    
    filename: str = Field(description="Name of the file to create")
    content: str = Field(default="", description="Optional content to write")
    
    def execute(self) -> str:
        try:
            with open(self.filename, 'w', encoding='utf-8') as f:
                f.write(self.content)
            
            size = os.path.getsize(self.filename)
            return f"Created file '{self.filename}' with {size} bytes"
        except Exception as e:
            return f"Error creating file: {e}"
```

## Best Practices

### 1. **Partial File Operations**
When modifying existing files, **always prefer partial file tools** over full file rewrites:
- Use `FileLineWriter`, `FileLineReader`, `FileLineInserter`, etc.
- Reason: Partial operations reduce risk of data loss and are more efficient for large files
- Exception: When creating new files or completely replacing small files

### 2. **Error Handling**
- Catch specific exceptions where possible
- Return informative error messages
- Don't raise exceptions from `execute()`

### 3. **Descriptions**
- Write clear, action-oriented descriptions
- Example: "Reads specific lines from a file" not "This tool reads lines"
- Mention units, constraints (e.g., "1-indexed line numbers")

### 4. **Testing**
- Test your tool manually before relying on it
- Consider edge cases (empty files, large inputs, etc.)

## Tool Discovery

The system automatically discovers tools:
1. All `.py` files in `tools/` directory (except those starting with `_`)
2. Classes inheriting from `ToolBase` (excluding `ToolBase` itself)
3. Tool names are the class names (e.g., `FileLineWriter`)

## Adding to Existing Tools

If you need to modify an existing tool:
1. Check if a partial tool already exists for the operation
2. Update descriptions or defaults as needed
3. Test backward compatibility

## Common Patterns

### File Operations
- Reading: `FileReader`, `FileLineReader`
- Writing: `FileWriter` (full), `FileLineWriter` (partial)
- Modifying: `FileLineReplacer`, `FileLineInserter`, `FileLineAppender`
- Moving: `FileMover`

### System Operations
- Directory creation: `DirectoryCreator`
- File listing: `FileLister`

### Utilities
- Calculations: `Calculator`
- Thinking: `Thought`
- Final answer: `Final`

## Debugging

If your tool isn't appearing:
1. Check inheritance (must inherit from `ToolBase`)
2. Verify the file is in `tools/` and doesn't start with `_`
3. Ensure the class is defined at module level
4. Check for syntax errors

## Next Steps

1. Review existing tools in `../tools/` for examples
2. Create your tool following this guide
3. Test with the agent system
4. Update documentation if needed

## See Also

- [Tool Directory](../tools/) – Existing implementations
- [Base Class](../tools/base.py) – ToolBase definition
- [System Prompt](../agent_core.py) – Agent behavior guidelines