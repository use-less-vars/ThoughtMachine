# Integration Guide: File Operation Tools

## Overview
Two primary file operation tools are available:

1. **FileEditor** - Unified tool for all file reading and editing operations
2. **FileLister** - List files in directories

Additionally, **FileMover** and **DirectoryCreator** are available for file system operations.

The FileEditor tool replaces six previous partial file operation tools, providing a simpler interface while maintaining safety and efficiency.

## FileEditor Tool

FileEditor is a unified tool that supports all common file operations:

### Operations
1. **read** - Read file contents (full file or specific lines)
2. **write** - Write content to file (full file or specific line)
3. **insert** - Insert lines at a specific position
4. **append** - Append lines to the end of a file
5. **replace** - Replace specific lines in a file
6. **delete** - Delete specific lines from a file

### Parameters
- `operation`: One of the above operations (required)
- `filename`: Path to a single file (optional, either filename or filenames must be provided)
- `filenames`: List of file paths to operate on (optional, either filename or filenames must be provided)
- `content`: String or list of strings for write/insert/append operations
- `line_number`: Line number for insert/write operations (1-indexed)
- `line_numbers`: Line number(s) for read/delete operations (single int, list, range string, or 'all')
- `replacements`: Dictionary mapping line numbers to new content for replace operation
- `mode`: For write operation with line_number: 'replace', 'insert', or 'append'
### Batch Operations

FileEditor supports batch operations across multiple files. When `filenames` parameter is provided, the same operation will be applied to each file in the list. This is useful for making identical changes across multiple files.

Example: Replace line 5 in three configuration files
```json
{
  "operation": "replace",
  "filenames": ["config1.yaml", "config2.yaml", "config3.yaml"],
  "replacements": {
    5: "enabled: true"
  }
}
```

Batch operations return a summary of successes and failures for each file.

### Usage Examples

#### Reading Specific Lines
```json
{
  "operation": "read",
  "filename": "config.py",
  "line_numbers": "10-20"
}
```

#### Fixing a Bug on Line 42
```json
{
  "operation": "replace",
  "filename": "app.py",
  "replacements": {
    42: "    fixed_result = process_data(data)  # Fixed bug here"
  }
}
```

#### Adding Documentation
```json
{
  "operation": "insert",
  "filename": "module.py",
  "line_number": 5,
  "content": [
    '    """',
    '    Process user input and return formatted result.',
    '    ',
    '    Args:',
    '        input_data: Raw user input',
    '    ',
    '    Returns:',
    '        Formatted string result',
    '    """'
  ]
}
```

#### Creating a New File
```json
{
  "operation": "write",
  "filename": "new_file.txt",
  "content": "Initial content"
}
```

## Legacy Tools (Still Available)
For backward compatibility, the following legacy tools are still available but not recommended for new code:
- FileLineReader, FileLineWriter, FileLineInserter, FileLineAppender, FileLineReplacer, FileLineDeleter
- FileReader, FileWriter

These tools are excluded from the simplified toolset but can be accessed if explicitly configured.

## Simplified Toolset
The simplified toolset includes:
- FileEditor (unified file operations)
- FileLister (list files)
- FileMover (move files/directories)
- DirectoryCreator (create directories)
- Calculator (basic arithmetic)
- DateTimeTool (date/time operations)
- Thought (reasoning)
- Final (output final answer)

This reduces tool count from 16+ to 8, making the agent interface cleaner and easier to use.

## Performance Benefits
- **Memory efficient**: Only reads/writes necessary parts of files
- **Fast**: No need to process entire files for small changes
- **Precise**: Exact control over which lines are modified
- **Safe**: Reduces risk of accidental file corruption

## Best Practices for Agent Instructions

1. **Always read first**: Before modifying, use FileEditor read operation to understand file structure
2. **Use appropriate operation**: Choose the right operation for the task
3. **Verify changes**: After modification, read the affected lines to confirm
4. **Handle errors**: Check tool responses for errors before proceeding
5. **Batch operations**: Use replace operation for multiple changes instead of multiple write calls

## Testing
Two test files are provided:
1. `test_partial_file_tools.py` - Comprehensive tests for all legacy tools
2. `agent_usage_example.py` - Example agent workflow

Run tests with:
```bash
python test_partial_file_tools.py
python agent_usage_example.py
```

## Session Protocol
A detailed session protocol is available in `session_log_4.txt` with:
- Complete tool specifications
- Usage examples
- Best practices
- Error handling details
- Performance comparisons

## Migration from Legacy Tools
When to use FileEditor vs legacy tools:

| Use Case | Recommended Tool |
|----------|-----------------|
| Any file operation | FileEditor |
| Listing files | FileLister |
| Moving files | FileMover |
| Creating directories | DirectoryCreator |
| Reading entire files | FileEditor read with line_numbers='all' |
| Creating new files | FileEditor write operation |
| Multiple scattered changes | FileEditor replace operation |
| Appending to files | FileEditor append operation |
| Bulk replacements | FileEditor write operation (if replacing >50% of file) |

## Workspace Isolation and Directory Structure

When using the agent in isolated environments (such as sandboxes or containers), the current working directory (`.`) may differ from the project root directory. This can cause file access errors. Follow these steps to diagnose and resolve:

### Two Operating Scenarios

1. **Normal Operation**: Project root is current directory (`.`)
   - Current directory contains: `agent_core.py`, `tools/`, `ai_docs/`, etc.
   - Stable workspace: `./`
   - Construction workspace: `./construction/`
   - File paths: Use normal relative paths (e.g., `"agent_core.py"`)

2. **Sandbox/Isolation**: Project root is parent directory (`..`)
   - Current directory is empty or `/`
   - Parent directory contains project files
   - Stable workspace: `../`
   - Construction workspace: `../construction/` (if created)
   - File paths: Prefix with `../` (e.g., `"../agent_core.py"`)

### Diagnostic Procedure

When you start or encounter file access errors:

1. **Initial Check**: Run FileLister on both `.` and `..` to understand directory structure.
2. **Identify Project Root**: Look for key project files: `agent_core.py`, `tools/`, `ai_docs/`.
   - If these are in `.`, you're in Normal Operation.
   - If these are in `..`, you're in Sandbox Operation.
   - If neither, use RequestUserInteraction to ask for guidance.
3. **Adjust File Paths**:
   - Normal: Use standard paths.
   - Sandbox: Prefix all file paths with `../`.
4. **Construction Workspace Consideration**:
   - In Sandbox mode, you can still use `workspace="construction"` but note that the construction directory will be at `../construction/`. The FileEditor tool automatically maps `workspace="construction"` to `./construction/`, so you may need to use `workspace="stable"` with `../` prefix for immediate access, or create construction workspace in parent directory.

### Quick Diagnostic Command
```json
{
  "operation": "read",
  "filename": "../ai_docs/integration_guide.md",
  "line_numbers": "1-10"
}
```

### Common Patterns
- Stable workspace = project root directory (either `.` or `..`)
- Construction workspace = project root + `/construction/`
- Temp directory = project root + `/temp/` for safe file operations

### Solution Template
```python
# Diagnostic first
FileLister(directory=".", workspace="stable")
FileLister(directory="..", workspace="stable")

# Based on findings:
# If project root is '..':
FileEditor(filename="../agent_core.py", operation="read", workspace="stable")
FileEditor(filename="../construction/agent_core.py", operation="read", workspace="stable")
```

### Preventive Measure
Always check both `.` and `..` during initial environment assessment before attempting file operations.

### Why This Happens
Workspace isolation for safety may place agent in different directory than project root. The agent must adapt to the actual file location structure.

## Security Considerations
- Tools validate all input parameters
- No arbitrary code execution
- File paths are validated
- Error messages don't expose sensitive information

## Support
For issues or questions:
1. Check `session_log_4.txt` for detailed documentation
2. Run test files to verify tool functionality
3. Review error messages for troubleshooting guidance

## Conclusion
The FileEditor tool provides agents with a unified, efficient interface for all file operations, significantly simplifying the tool ecosystem while maintaining the safety and precision of partial file operations.