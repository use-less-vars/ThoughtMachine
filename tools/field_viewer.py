"""
FieldViewer - Extract Pydantic model fields with types, defaults, and docstrings.
Uses LibCST for structural analysis.
"""

import libcst as cst
import libcst.matchers as m
import libcst.metadata
from typing import Dict, List, Optional, Set, Tuple, Any, Union, Literal
from pydantic import Field
from .base import ToolBase
import os
import sys
import json
import difflib


class ImportAnalyzer(cst.CSTVisitor):
    """Analyze imports to determine what names refer to BaseModel."""
    
    def __init__(self):
        # Maps name -> (module, original_name)
        self.imported_names: Dict[str, Tuple[str, str]] = {}
        # Maps module alias -> module
        self.module_aliases: Dict[str, str] = {}
    
    def visit_Import(self, node: cst.Import) -> None:
        for name in node.names:
            module_name = self._get_module_name(name.name)
            if name.asname:
                alias = name.asname.name.value
                self.module_aliases[alias] = module_name
            # For 'import pydantic', we don't have a direct name for BaseModel
            # but we can track that pydantic is imported
    
    def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
        if not node.module:
            return
        module_name = self._get_module_name(node.module)
        for name in node.names:
            imported_name = name.name.value
            if name.asname:
                alias = name.asname.name.value
            else:
                alias = imported_name
            self.imported_names[alias] = (module_name, imported_name)
    
    def _get_module_name(self, node: Union[cst.Name, cst.Attribute]) -> str:
        """Convert module node to string."""
        if isinstance(node, cst.Name):
            return node.value
        elif isinstance(node, cst.Attribute):
            # Recursively build dotted name
            return self._get_module_name(node.value) + '.' + node.attr.value
        return ""
    
    def get_base_model_names(self) -> Set[str]:
        """Return all names that refer to BaseModel."""
        names = set()
        for name, (module, original) in self.imported_names.items():
            if original == 'BaseModel' and ('pydantic' in module or module == 'pydantic'):
                names.add(name)
        # Also add BaseModel directly (common case)
        names.add('BaseModel')
        return names
    
    def is_base_model(self, node: cst.BaseExpression) -> bool:
        """Check if an expression refers to BaseModel."""
        if isinstance(node, cst.Name):
            return node.value in self.get_base_model_names()
        elif isinstance(node, cst.Attribute):
            # Handle pydantic.BaseModel
            if isinstance(node.value, cst.Name):
                # Check if module alias points to pydantic
                actual_module = self.module_aliases.get(node.value.value, '')
                if 'pydantic' in actual_module or actual_module == 'pydantic':
                    return node.attr.value == 'BaseModel'
            # Handle dotted import like from pydantic import BaseModel as BM
            # Already covered by imported_names
            return False
        return False


class PydanticModelVisitor(cst.CSTVisitor):
    """Extract Pydantic model information."""
    
    METADATA_DEPENDENCIES = (cst.metadata.PositionProvider,)
    
    def __init__(self, import_analyzer: ImportAnalyzer):
        self.import_analyzer = import_analyzer
        self.models: List[Dict[str, Any]] = []
        self.current_model: Optional[Dict[str, Any]] = None
        self.current_class_node: Optional[cst.ClassDef] = None
        self.pydantic_model_names: Set[str] = set()
        # Stack to track class nesting (True for Pydantic models, False otherwise)
        self.class_stack: List[bool] = []
        # Debug flag for logging
        self.debug = False

    def _get_line_number(self, node: cst.CSTNode) -> int:
        """Safely extract line number from node using PositionProvider."""
        # First try metadata (official API)
        try:
            pos = self.get_metadata(cst.metadata.PositionProvider, node)
            if pos:
                return pos.start.line
        except:
            pass
        
        # Fallback: check for start_line attribute (some LibCST versions)
        try:
            if hasattr(node, 'start_line'):
                return node.start_line
        except:
            pass
        
        # Fallback: check for line attribute
        try:
            if hasattr(node, 'line'):
                return node.line
        except:
            pass
        
        return 0
    
    def visit_ClassDef(self, node: cst.ClassDef) -> None:
        # Check if class inherits from BaseModel or another Pydantic model
        is_pydantic = False
        if node.bases:
            for base in node.bases:
                if self.import_analyzer.is_base_model(base.value):
                    is_pydantic = True
                    break
                # Check if base is another Pydantic model we've already found
                base_name = self._get_base_name(base.value)
                if base_name and base_name in self.pydantic_model_names:
                    is_pydantic = True
                    break
        
        # Push to class stack
        self.class_stack.append(is_pydantic)
        
        if is_pydantic and not self.current_model:
            # This is a Pydantic model at some nesting level
            # We only set current_model if we're not already inside another Pydantic model
            # (to avoid capturing fields from nested classes)
            self.current_model = {
                'name': node.name.value,
                'line': self._get_line_number(node),
                'fields': [],
                'docstring': self._get_docstring(node.body)
            }
            self.current_class_node = node
            self.models.append(self.current_model)
            self.pydantic_model_names.add(node.name.value)
    
    def leave_ClassDef(self, node: cst.ClassDef) -> None:
        # Pop from class stack
        if self.class_stack:
            was_pydantic = self.class_stack.pop()
            # If we're leaving the current Pydantic model, reset it
            if was_pydantic and self.current_model and self.current_model['name'] == node.name.value:
                self.current_model = None
                self.current_class_node = None
    
    def visit_AnnAssign(self, node: cst.AnnAssign) -> None:
        """Capture annotated assignments (fields with types)."""
        if not self._is_in_pydantic_model():
            return
        
        if isinstance(node.target, cst.Name):
            field_name = node.target.value
            field_type = self._code_for_node(node.annotation) if node.annotation else None
            default = self._code_for_node(node.value) if node.value else None
            
            # Try to get docstring (simple string immediately after)
            # This is a simplification - proper docstring extraction would need
            # to track position relative to next statement
            docstring = None
            
            field_info = {
                'name': field_name,
                'type': field_type,
                'default': default,
                'line': self._get_line_number(node),
                'docstring': docstring
            }
            self.current_model['fields'].append(field_info)
    
    def visit_Assign(self, node: cst.Assign) -> None:
        """Capture simple assignments (fields without type annotations)."""
        if not self._is_in_pydantic_model():
            return
        
        # Only capture assignments where target is a simple name
        # (not tuple unpacking or attribute assignment)
        if len(node.targets) == 1 and isinstance(node.targets[0].target, cst.Name):
            field_name = node.targets[0].target.value
            default = self._code_for_node(node.value) if node.value else None
            
            field_info = {
                'name': field_name,
                'type': None,
                'default': default,
                'line': self._get_line_number(node),
                'docstring': None
            }
            self.current_model['fields'].append(field_info)
    
    def _get_docstring(self, body: cst.BaseSuite) -> Optional[str]:
        """Extract class docstring."""
        if isinstance(body, cst.IndentedBlock):
            if body.body:
                first_stmt = body.body[0]
                if isinstance(first_stmt, cst.SimpleStatementLine):
                    stmt = first_stmt
                    if len(stmt.body) == 1 and isinstance(stmt.body[0], cst.Expr):
                        expr = stmt.body[0]
                        if isinstance(expr.value, cst.SimpleString):
                            return expr.value.value
        return None
    
    def _code_for_node(self, node: cst.CSTNode) -> Optional[str]:
        """Convert node to source code."""
        if self.debug:
            sys.stderr.write(f"DEBUG _code_for_node: {type(node).__name__}\n")
        # Handle Annotation nodes separately (they wrap type annotations)
        if isinstance(node, cst.Annotation):
            # Extract the actual type annotation
            node = node.annotation
            if self.debug:
                sys.stderr.write(f"DEBUG   -> unwrapped to {type(node).__name__}\n")
        try:
            result = cst.Module([]).code_for_node(node).strip()
            if self.debug:
                sys.stderr.write(f"DEBUG   -> result: '{result}'\n")
            return result
        except Exception as e:
            if self.debug:
                sys.stderr.write(f"DEBUG   -> error: {e}\n")
            return None

    def _is_in_pydantic_model(self) -> bool:
        """Check if we're directly inside a Pydantic model (not in a nested class)."""
        if not self.current_model or not self.class_stack:
            return False
        # We're in a Pydantic model if the top of stack is True
        # (meaning the current class is a Pydantic model)
        return self.class_stack[-1]
    
    def _get_base_name(self, node: cst.BaseExpression) -> Optional[str]:
        """Extract the name from a base expression."""
        if isinstance(node, cst.Name):
            return node.value
        elif isinstance(node, cst.Attribute):
            # For dotted names like pydantic.BaseModel, return just the last part
            # This is a simplification - might need full dotted name
            return node.attr.value
        return None


class FieldViewer(ToolBase):
    """
    Parse Python files, locate Pydantic model definitions, and display their fields
    with types, docstrings, and line numbers.
    """
    tool: Literal["FieldViewer"] = "FieldViewer"
    
    file_path: str = Field(description="Path to the Python file to inspect.")
    class_name: Optional[str] = Field(None, description="If provided, show fields only for this class.")
    include_docstrings: bool = Field(True, description="Include field docstrings.")
    show_line_numbers: bool = Field(True, description="Include line numbers.")
    format: str = Field("text", description="Output format: 'text' (human-readable table) or 'json'.")
    
    def execute(self) -> str:
        try:
            # Validate file path is within workspace
            try:
                validated_path = self._validate_path(self.file_path)
            except ValueError as e:
                return self._truncate_output(f"Error: {e}")
            
            # Validate file exists
            if not os.path.exists(validated_path):
                return self._truncate_output(f"Error: File '{validated_path}' does not exist.")
            if not os.path.isfile(validated_path):
                return self._truncate_output(f"Error: '{validated_path}' is not a file.")
            
            # Read file content
            try:
                with open(validated_path, 'r', encoding='utf-8') as f:
                    content = f.read()
            except UnicodeDecodeError:
                # Try with different encoding
                try:
                    with open(validated_path, 'r', encoding='latin-1') as f:
                        content = f.read()
                except Exception as e:
                    return self._truncate_output(f"Error reading file '{self.file_path}': {e}")
            except Exception as e:
                return self._truncate_output(f"Error reading file '{self.file_path}': {e}")
            
            # Parse with LibCST and metadata
            try:
                module = cst.parse_module(content)
                wrapper = cst.metadata.MetadataWrapper(module)
            except cst.ParserSyntaxError as e:
                return self._truncate_output(f"Syntax error in file '{self.file_path}': {e}")
            except Exception as e:
                return self._truncate_output(f"Failed to parse file '{self.file_path}': {e}")
            
            # Analyze imports
            import_analyzer = ImportAnalyzer()
            module.visit(import_analyzer)
            
            # Find Pydantic models with metadata
            visitor = PydanticModelVisitor(import_analyzer)
            wrapper.visit(visitor)
            
            models = visitor.models
            
            # Filter by class_name if specified
            if self.class_name:
                matching_models = [m for m in models if m['name'] == self.class_name]
                if not matching_models:
                    # Suggest closest matches
                    available = [m['name'] for m in models]
                    suggestion = self._suggest_closest(self.class_name, available)
                    return self._truncate_output(f"Class '{self.class_name}' not found.{suggestion}")
                models = matching_models
            
            # Prepare output based on format
            if self.format.lower() == 'json':
                return self._truncate_output(self._format_json(models))
            else:
                return self._truncate_output(self._format_text(models))
                
        except Exception as e:
            return self._truncate_output(f"Unexpected error in FieldViewer: {e}")
    
    def _suggest_closest(self, target: str, options: List[str]) -> str:
        """Suggest closest match from options."""
        if not options:
            return " No classes found in file."
        matches = difflib.get_close_matches(target, options, n=3, cutoff=0.6)
        if matches:
            return f" Did you mean: {', '.join(matches)}?"
        return f" Available classes: {', '.join(options)}"
    
    def _format_json(self, models: List[Dict[str, Any]]) -> str:
        """Format output as JSON."""
        output = {
            "file": self.file_path,
            "classes": models
        }
        return json.dumps(output, indent=2, default=str)
    
    def _format_text(self, models: List[Dict[str, Any]]) -> str:
        """Format output as human-readable text table."""
        if not models:
            return f"No Pydantic models found in '{self.file_path}'."
        
        lines = []
        lines.append(f"Pydantic models in '{self.file_path}':")
        lines.append("")
        
        for model in models:
            lines.append(f"Class: {model['name']} (line {model['line']})")
            if model.get('docstring'):
                lines.append(f"  Docstring: {model['docstring']}")
            
            if not model['fields']:
                lines.append("  No fields found.")
            else:
                # Determine column widths
                headers = ["Name", "Type", "Default", "Line"]
                if self.include_docstrings:
                    headers.append("Docstring")
                
                # Calculate column widths
                col_widths = [len(h) for h in headers]
                rows = []
                for field in model['fields']:
                    row = [
                        field['name'],
                        field['type'] or '',
                        field['default'] or '',
                        str(field['line'])
                    ]
                    if self.include_docstrings:
                        row.append(field.get('docstring') or '')
                    rows.append(row)
                    for i, val in enumerate(row):
                        col_widths[i] = max(col_widths[i], len(str(val)))
                
                # Build separator
                separator = "+-" + "-+-".join("-" * w for w in col_widths) + "-+"
                
                # Build header
                header = "| " + " | ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers)) + " |"
                
                lines.append(separator)
                lines.append(header)
                lines.append(separator)
                
                # Add rows
                for row in rows:
                    line = "| " + " | ".join(str(val).ljust(col_widths[i]) for i, val in enumerate(row)) + " |"
                    lines.append(line)
                lines.append(separator)
            
            lines.append("")  # Empty line between classes
        
        return "\n".join(lines)


# Note: The tool will be automatically discovered by tools/__init__.py
