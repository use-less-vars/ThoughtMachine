from .base import ToolBase
import ast
import os
import pathlib
from pydantic import Field
from typing import Optional, List, Dict, Any, Literal
import logging

logger = logging.getLogger(__name__)

class FileSummaryTool(ToolBase):
    """Extract structural elements from code files using AST parsing."""
    tool: Literal["FileSummaryTool"] = "FileSummaryTool"
    
    filename: str = Field(description="Path to the file to analyze")
    include_imports: bool = Field(default=True, description="Include import statements in summary")
    include_classes: bool = Field(default=True, description="Include class definitions in summary")
    include_functions: bool = Field(default=True, description="Include function definitions in summary")
    max_methods_per_class: int = Field(default=10, description="Maximum number of methods to list per class")
    max_functions: int = Field(default=50, description="Maximum number of functions to list")
    
    def execute(self) -> str:
        try:
            # Resolve the file path
            file_path = pathlib.Path(self.filename)
            if not file_path.exists():
                return self._truncate_output(f"Error: File '{self.filename}' does not exist.")
            if not file_path.is_file():
                return self._truncate_output(f"Error: '{self.filename}' is not a file.")
            
            # Read file content
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
            except UnicodeDecodeError:
                # Try with different encoding
                try:
                    with open(file_path, 'r', encoding='latin-1') as f:
                        content = f.read()
                except Exception as e:
                    return self._truncate_output(f"Error reading file '{self.filename}': {e}")
            except Exception as e:
                return self._truncate_output(f"Error reading file '{self.filename}': {e}")
            
            # Get total line count
            total_lines = len(content.splitlines())
            
            # Try to parse as Python AST
            try:
                tree = ast.parse(content, filename=str(file_path))
            except SyntaxError as e:
                return self._truncate_output(f"Error parsing Python file '{self.filename}': {e}")
            except Exception as e:
                return self._truncate_output(f"Error parsing file '{self.filename}': {e}")
            
            # Initialize results
            imports = []
            classes = []
            functions = []
            
            # Walk through AST
            for node in ast.walk(tree):
                # Imports
                if self.include_imports:
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            imports.append({
                                'type': 'import',
                                'name': alias.name,
                                'alias': alias.asname,
                                'line': node.lineno,
                                'end_line': getattr(node, 'end_lineno', node.lineno)
                            })
                    elif isinstance(node, ast.ImportFrom):
                        module = node.module or ''
                        for alias in node.names:
                            imports.append({
                                'type': 'import_from',
                                'module': module,
                                'name': alias.name,
                                'alias': alias.asname,
                                'line': node.lineno,
                                'end_line': getattr(node, 'end_lineno', node.lineno)
                            })
                
                # Classes
                if self.include_classes and isinstance(node, ast.ClassDef):
                    class_info = {
                        'name': node.name,
                        'line': node.lineno,
                        'end_line': getattr(node, 'end_lineno', node.lineno),
                        'methods': [],
                        'decorators': [self._unparse_ast(decorator) for decorator in node.decorator_list]
                    }
                    
                    # Collect methods (functions defined in class)
                    for item in node.body:
                        if isinstance(item, ast.FunctionDef):
                            method_info = {
                                'name': item.name,
                                'line': item.lineno,
                                'end_line': getattr(item, 'end_lineno', item.lineno),
                                'decorators': [self._unparse_ast(decorator) for decorator in item.decorator_list],
                                'args': self._extract_function_args(item)
                            }
                            class_info['methods'].append(method_info)
                    
                    classes.append(class_info)
                
                # Functions (top-level functions only)
                if self.include_functions and isinstance(node, ast.FunctionDef):
                    # Check if this function is inside a class (skip those, they're handled as methods)
                    parent = self._get_parent(node, tree)
                    if not isinstance(parent, ast.ClassDef):
                        function_info = {
                            'name': node.name,
                            'line': node.lineno,
                            'end_line': getattr(node, 'end_lineno', node.lineno),
                            'decorators': [ast.unparse(decorator) for decorator in node.decorator_list] if hasattr(ast, 'unparse') else [],
                            'args': self._extract_function_args(node)
                        }
                        functions.append(function_info)
            
            # Build output
            output_lines = []
            output_lines.append(f"File: {self.filename} ({total_lines} lines)")
            output_lines.append("=" * 60)
            
            # Imports section
            if self.include_imports and imports:
                output_lines.append("IMPORTS:")
                for imp in imports[:20]:  # Limit imports for readability
                    if imp['type'] == 'import':
                        if imp['alias']:
                            line = f"  import {imp['name']} as {imp['alias']}"
                        else:
                            line = f"  import {imp['name']}"
                    else:  # import_from
                        if imp['alias']:
                            line = f"  from {imp['module']} import {imp['name']} as {imp['alias']}"
                        else:
                            line = f"  from {imp['module']} import {imp['name']}"
                    line += f" (lines {imp['line']}-{imp['end_line']})"
                    output_lines.append(line)
                if len(imports) > 20:
                    output_lines.append(f"  ... and {len(imports) - 20} more imports")
                output_lines.append("")
            
            # Classes section
            if self.include_classes and classes:
                output_lines.append("CLASSES:")
                for cls in classes:
                    # Class header
                    decorator_str = ""
                    if cls['decorators']:
                        decorator_str = f" [{', '.join(cls['decorators'])}]"
                    output_lines.append(f"  {cls['name']}{decorator_str} (lines {cls['line']}-{cls['end_line']}):")
                    
                    # Methods
                    for method in cls['methods'][:self.max_methods_per_class]:
                        method_decorator_str = ""
                        if method['decorators']:
                            method_decorator_str = f" [{', '.join(method['decorators'])}]"
                        args_str = self._format_args(method['args'])
                        output_lines.append(f"    • {method['name']}{method_decorator_str}({args_str}) (lines {method['line']}-{method['end_line']})")
                    
                    if len(cls['methods']) > self.max_methods_per_class:
                        output_lines.append(f"    ... and {len(cls['methods']) - self.max_methods_per_class} more methods")
                    
                    output_lines.append("")
            
            # Functions section
            if self.include_functions and functions:
                output_lines.append("FUNCTIONS:")
                for func in functions[:self.max_functions]:
                    decorator_str = ""
                    if func['decorators']:
                        decorator_str = f" [{', '.join(func['decorators'])}]"
                    args_str = self._format_args(func['args'])
                    output_lines.append(f"  • {func['name']}{decorator_str}({args_str}) (lines {func['line']}-{func['end_line']})")
                
                if len(functions) > self.max_functions:
                    output_lines.append(f"  ... and {len(functions) - self.max_functions} more functions")
                output_lines.append("")
            
            # Summary statistics
            output_lines.append("SUMMARY:")
            output_lines.append(f"  Total lines: {total_lines}")
            output_lines.append(f"  Imports: {len(imports)}")
            output_lines.append(f"  Classes: {len(classes)}")
            total_methods = sum(len(cls['methods']) for cls in classes)
            output_lines.append(f"  Methods: {total_methods}")
            output_lines.append(f"  Functions: {len(functions)}")
            
            return self._truncate_output("\n".join(output_lines))
            
        except Exception as e:
            logger.exception(f"Error in FileSummaryTool: {e}")
            return self._truncate_output(f"Error analyzing file '{self.filename}': {e}")
    
    def _extract_function_args(self, func_node: ast.FunctionDef) -> Dict[str, Any]:
        """Extract function arguments information."""
        args_info = {
            'args': [],
            'defaults': {},
            'vararg': None,
            'kwarg': None,
            'kwonlyargs': [],
            'posonlyargs': []
        }
        
        # Positional arguments
        for arg in func_node.args.args:
            args_info['args'].append(arg.arg)
        
        # Defaults
        defaults_start = len(func_node.args.args) - len(func_node.args.defaults)
        for i, default in enumerate(func_node.args.defaults):
            arg_name = func_node.args.args[defaults_start + i].arg
            default_str = self._unparse_ast(default) if hasattr(ast, 'unparse') else str(default)
            args_info['defaults'][arg_name] = default_str
        
        # *args
        if func_node.args.vararg:
            args_info['vararg'] = func_node.args.vararg.arg
        
        # **kwargs
        if func_node.args.kwarg:
            args_info['kwarg'] = func_node.args.kwarg.arg
        
        # Keyword-only arguments
        for arg in func_node.args.kwonlyargs:
            args_info['kwonlyargs'].append(arg.arg)
        
        # Positional-only arguments (Python 3.8+)
        if hasattr(func_node.args, 'posonlyargs'):
            for arg in func_node.args.posonlyargs:
                args_info['posonlyargs'].append(arg.arg)
        
        return args_info
    
    def _format_args(self, args_info: Dict[str, Any]) -> str:
        """Format arguments as a string."""
        parts = []
        
        # Positional-only args (Python 3.8+)
        if args_info.get('posonlyargs'):
            parts.extend(args_info['posonlyargs'])
            if args_info.get('args'):
                parts.append('/')
        
        # Regular args
        for arg in args_info.get('args', []):
            if arg in args_info.get('defaults', {}):
                default = args_info['defaults'][arg]
                parts.append(f"{arg}={default}")
            else:
                parts.append(arg)
        
        # *args
        if args_info.get('vararg'):
            parts.append(f"*{args_info['vararg']}")
        
        # Keyword-only args
        if args_info.get('kwonlyargs'):
            if not args_info.get('vararg'):
                parts.append('*')
            for arg in args_info['kwonlyargs']:
                parts.append(arg)
        
        # **kwargs
        if args_info.get('kwarg'):
            parts.append(f"**{args_info['kwarg']}")
        
        return ", ".join(parts)
    def _unparse_ast(self, node):
        if hasattr(ast, 'unparse'):
            return ast.unparse(node)
        else:
            # Simple fallback for common node types
            if isinstance(node, ast.Name):
                return node.id
            elif isinstance(node, ast.Attribute):
                return f"{self._unparse_ast(node.value)}.{node.attr}"
            elif isinstance(node, ast.Call):
                func = self._unparse_ast(node.func)
                args = [self._unparse_ast(arg) for arg in node.args]
                return f"{func}({', '.join(args)})"
            else:
                return str(node)
    
    def _get_parent(self, node: ast.AST, tree: ast.AST) -> Optional[ast.AST]:
        """Find the parent of a node in the AST."""
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                if child is node:
                    return parent
        return None
