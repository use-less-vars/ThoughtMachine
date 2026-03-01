"""
High-level code modification tool using LibCST.
Supports operations: add_function, add_method, add_import, add_class, replace_function_body, modify_function.
"""
from typing import Optional, Literal
from pydantic import Field, model_validator
import libcst as cst
import libcst.matchers as m
import os

from .base import ToolBase
from pathlib import Path




class CodeModifier(ToolBase):
    """
    Modify Python code at a structural level.
     Currently supports: add_function, add_method, add_import, add_class, replace_function_body, modify_function.
    """

    operation: Literal[
         "add_function", "add_method", "add_import", "add_class", "replace_function_body", "modify_function"
    ] = Field(
        description="The code modification operation to perform."
    )
    file_path: str = Field(
        description="Path to the Python file to modify."
    )
    # Common parameters
    name: Optional[str] = Field(
        default=None,
        description="Name of the function/method/class to add (required for add_function/add_method/add_class)."
    )
    new_name: Optional[str] = Field(
        default=None,
        description="New name for the function (rename). Only used for modify_function."
    )
    body: Optional[str] = Field(
        default=None,
        description="Body content as a string (without indentation). Required for add_function/add_method/add_class."
    )
    parameters: Optional[list[str]] = Field(
        default=None,
        description="List of parameters, e.g., ['self', 'x: int', 'y=10']. If None, empty parentheses."
    )
    return_type: Optional[str] = Field(
        default=None,
        description="Return type annotation, e.g., 'bool' or 'List[str]'."
    )
    decorators: Optional[list[str]] = Field(
        default=None,
        description="Decorators to apply, e.g., ['@staticmethod', '@property']."
    )
    after: Optional[str] = Field(
        default=None,
        description="For add_function/add_class: insert after this named function/class. For add_method: insert after this named method."
    )

    # add_method specific
    class_name: Optional[str] = Field(
        default=None,
        description="Name of the class to add a method to (required for add_method)."
    )
    after_method: Optional[str] = Field(
        default=None,
        description="For add_method: insert after this named method within the class."
    )

    # add_import specific
    import_module: Optional[str] = Field(
        default=None,
        description="Module to import (required for add_import)."
    )
    import_names: Optional[list[str]] = Field(
        default=None,
        description="Names to import from the module (for 'from ... import ...')."
    )
    import_alias: Optional[str] = Field(
        default=None,
        description="Alias for the module (e.g., 'np' for 'import numpy as np')."
    )
    from_import: bool = Field(
        default=False,
        description="If True, generate 'from module import names'. Otherwise, generate 'import module'."
    )

    # add_class specific
    bases: Optional[list[str]] = Field(
        default=None,
        description="Base classes for the new class (e.g., ['object'], ['BaseModel'])."
    )

    # replace_function_body specific
    target: Optional[str] = Field(
        default=None,
        description="Name of the function or method to replace (required for replace_function_body)."
    )
    new_body: Optional[str] = Field(
        default=None,
        description="New body content for the function (required for replace_function_body)."
    )
    preserve_docstring: bool = Field(
        default=True,
        description="If True, keep the existing docstring when replacing the body."
    )

    @model_validator(mode='after')
    def validate_operation(self):
        if self.operation == "add_function":
            if not self.name:
                raise ValueError("name is required for add_function")
            if self.body is None:
                raise ValueError("body is required for add_function")
        elif self.operation == "add_method":
            if not self.name:
                raise ValueError("name is required for add_method")
            if not self.class_name:
                raise ValueError("class_name is required for add_method")
            if self.body is None:
                raise ValueError("body is required for add_method")
        elif self.operation == "add_import":
            if not self.import_module:
                raise ValueError("import_module is required for add_import")
            if self.from_import and not self.import_names:
                raise ValueError("import_names required when from_import is True")
        elif self.operation == "add_class":
            if not self.name:
                raise ValueError("name is required for add_class")
            if self.body is None:
                raise ValueError("body is required for add_class")
        elif self.operation == "replace_function_body":
            if not self.target:
                raise ValueError("target is required for replace_function_body")
            if self.new_body is None:
                raise ValueError("new_body is required for replace_function_body")
        elif self.operation == "modify_function":
            if not self.name:
                raise ValueError("name is required for modify_function")
            # body, parameters, return_type, decorators, new_name are optional
        return self


    def execute(self) -> str:
        actual_path = self.file_path

        if not os.path.exists(actual_path):
            return f"Error: File {actual_path} does not exist."

        try:
            with open(actual_path, 'r', encoding='utf-8') as f:
                source = f.read()
        except Exception as e:
            return f"Error reading file: {e}"

        try:
            module = cst.parse_module(source)
        except Exception as e:
            return f"Error parsing file: {e}"

        if self.operation == "add_function":
            new_module, msg = self._add_function(module)
        elif self.operation == "add_method":
            new_module, msg = self._add_method(module)
        elif self.operation == "add_import":
            new_module, msg = self._add_import(module)
        elif self.operation == "add_class":
            new_module, msg = self._add_class(module)
        elif self.operation == "replace_function_body":
            new_module, msg = self._replace_function_body(module)
        elif self.operation == "modify_function":
            new_module, msg = self._modify_function(module)
        else:
            return f"Error: Unsupported operation {self.operation}"

        if new_module is None:
            return msg  # error message

        # Write back
        new_source = new_module.code
        try:
            with open(actual_path, 'w', encoding='utf-8') as f:
                f.write(new_source)
        except Exception as e:
            return f"Error writing file: {e}"

        return f"Successfully performed {self.operation} on {self.file_path}. {msg}"

    # --------------------------------------------------------------------------
    # Helpers
    # --------------------------------------------------------------------------
    def _function_exists(self, module: cst.Module, name: str) -> bool:
        """Return True if a function with the given name exists at module level."""
        for stmt in module.body:
            if m.matches(stmt, m.FunctionDef(name=m.Name(name))):
                return True
        return False
    def _detect_indentation(self, source: str) -> tuple[str, int]:
        """
        Detect indentation style from source code.
        Returns (indent_char, width) where indent_char is either ' ' or '\\t',
        and width is number of spaces (if indent_char is ' ') or 1 (for tabs).
        Defaults to (' ', 4) if cannot detect.
        """
        lines = source.splitlines()

        # Collect indentation from non-empty lines
        indent_patterns = []
        for line in lines:
            if line.strip():  # non-empty line
                leading = line[:len(line) - len(line.lstrip())]
                if leading:
                    indent_patterns.append(leading)

        if not indent_patterns:
            return ' ', 4  # default

        # Check for tabs
        has_tabs = any('\t' in indent for indent in indent_patterns)
        has_spaces = any(' ' in indent for indent in indent_patterns)

        if has_tabs and not has_spaces:
            # Only tabs
            return '\t', 1

        # Prefer spaces, or mixed (use spaces)
        # Analyze space-based indentation
        space_widths = []
        for indent in indent_patterns:
            # Count consecutive spaces at start
            space_count = 0
            for char in indent:
                if char == ' ':
                    space_count += 1
                else:
                    break
            if space_count > 0:
                space_widths.append(space_count)

        if space_widths:
            # Find most common width (mode)
            from collections import Counter
            counter = Counter(space_widths)
            most_common = counter.most_common(1)[0][0]
            # Common widths: 2, 4, 8
            # If width is odd or not standard, round to nearest common
            if most_common in [2, 4, 8]:
                width = most_common
            else:
                # Round to nearest of 2, 4, 8
                if most_common <= 3:
                    width = 2
                elif most_common <= 6:
                    width = 4
                else:
                    width = 8
            return ' ', width

        return ' ', 4  # default

    # --------------------------------------------------------------------------
    # add_function
    # --------------------------------------------------------------------------
    def _add_function(self, module: cst.Module) -> tuple[Optional[cst.Module], str]:
        # Helper to parse body
        def parse_body_to_statements(body_str: str) -> list[cst.BaseStatement]:
            if not body_str.strip():
                return []
            dummy_func = f"def _dummy():\n" + "".join(f"    {line}\n" for line in body_str.splitlines())
            try:
                dummy_module = cst.parse_module(dummy_func)
                func_def = dummy_module.body[0]
                if not isinstance(func_def, cst.FunctionDef):
                    raise ValueError("Parsing did not yield a FunctionDef")
                return list(func_def.body.body)
            except Exception as e:
                raise ValueError(f"Failed to parse function body: {e}")

        # Build parameters
        params = []
        if self.parameters:
            for p in self.parameters:
                p = p.strip()
                if ':' in p and '=' in p:
                    name = p.split(':')[0].strip()
                    param = cst.Param(name=cst.Name(name))
                elif ':' in p:
                    name, annotation = p.split(':', 1)
                    name = name.strip()
                    annotation = annotation.strip()
                    try:
                        ann_expr = cst.parse_expression(annotation)
                    except:
                        ann_expr = cst.Name(annotation)
                    param = cst.Param(
                        name=cst.Name(name),
                        annotation=cst.Annotation(ann_expr)
                    )
                elif '=' in p:
                    name, default = p.split('=', 1)
                    name = name.strip()
                    default = default.strip()
                    try:
                        default_expr = cst.parse_expression(default)
                    except:
                        default_expr = cst.Name(default)
                    param = cst.Param(
                        name=cst.Name(name),
                        default=default_expr
                    )
                else:
                    param = cst.Param(name=cst.Name(p))
                params.append(param)
        else:
            params = []

        # Return annotation
        return_annotation = None
        if self.return_type:
            try:
                return_ann_expr = cst.parse_expression(self.return_type)
                return_annotation = cst.Annotation(return_ann_expr)
            except:
                return_annotation = cst.Annotation(cst.Name(self.return_type))

        # Parse body
        try:
            body_statements = parse_body_to_statements(self.body)
        except ValueError as e:
            return None, str(e)

        # Create FunctionDef node
        func = cst.FunctionDef(
            name=cst.Name(self.name),
            params=cst.Parameters(params=params),
            body=cst.IndentedBlock(body=body_statements),
            returns=return_annotation,
            decorators=[],
        )

        # Add decorators
        if self.decorators:
            decorator_nodes = []
            for deco in self.decorators:
                deco_str = deco.lstrip('@').strip()
                try:
                    deco_expr = cst.parse_expression(deco_str)
                except Exception as e:
                    return None, f"Error parsing decorator '{deco}': {e}"
                decorator_nodes.append(cst.Decorator(decorator=deco_expr))
            func = func.with_changes(decorators=decorator_nodes)

        # Check conflict
        if self._function_exists(module, self.name):
            return None, f"A function named '{self.name}' already exists."

        # Determine insertion position
        body = list(module.body)
        insert_idx = 0

        # Skip module docstring
        if len(body) > 0 and m.matches(body[0], m.SimpleStatementLine(body=[m.Expr(value=m.SimpleString())])):
            insert_idx = 1

        # Find last import
        last_import_idx = -1
        for i, stmt in enumerate(body):
            if m.matches(stmt, m.SimpleStatementLine()):
                for item in stmt.body:
                    if m.matches(item, m.Import() | m.ImportFrom()):
                        last_import_idx = i
                        break
        if last_import_idx >= 0:
            insert_idx = last_import_idx + 1

        if self.after:
            # Insert after a specific function
            found_idx = None
            for i, stmt in enumerate(body):
                if m.matches(stmt, m.FunctionDef(name=m.Name(self.after))):
                    found_idx = i
                    break
            if found_idx is None:
                return None, f"Function '{self.after}' not found to insert after."
            insert_idx = found_idx + 1

        body.insert(insert_idx, func)
        new_module = module.with_changes(body=body)
        return new_module, f"Inserted function '{self.name}' at position {insert_idx}."

    # --------------------------------------------------------------------------
    # add_method
    # --------------------------------------------------------------------------
    def _add_method(self, module: cst.Module) -> tuple[Optional[cst.Module], str]:
        # Helper to parse body (same as above)
        def parse_body_to_statements(body_str: str) -> list[cst.BaseStatement]:
            if not body_str.strip():
                return []
            dummy_func = f"def _dummy():\n" + "".join(f"    {line}\n" for line in body_str.splitlines())
            try:
                dummy_module = cst.parse_module(dummy_func)
                func_def = dummy_module.body[0]
                if not isinstance(func_def, cst.FunctionDef):
                    raise ValueError("Parsing did not yield a FunctionDef")
                return list(func_def.body.body)
            except Exception as e:
                raise ValueError(f"Failed to parse method body: {e}")

        # Build parameters (same as add_function)
        params = []
        if self.parameters:
            for p in self.parameters:
                p = p.strip()
                if ':' in p and '=' in p:
                    name = p.split(':')[0].strip()
                    param = cst.Param(name=cst.Name(name))
                elif ':' in p:
                    name, annotation = p.split(':', 1)
                    name = name.strip()
                    annotation = annotation.strip()
                    try:
                        ann_expr = cst.parse_expression(annotation)
                    except:
                        ann_expr = cst.Name(annotation)
                    param = cst.Param(
                        name=cst.Name(name),
                        annotation=cst.Annotation(ann_expr)
                    )
                elif '=' in p:
                    name, default = p.split('=', 1)
                    name = name.strip()
                    default = default.strip()
                    try:
                        default_expr = cst.parse_expression(default)
                    except:
                        default_expr = cst.Name(default)
                    param = cst.Param(
                        name=cst.Name(name),
                        default=default_expr
                    )
                else:
                    param = cst.Param(name=cst.Name(p))
                params.append(param)
        else:
            params = []

        # Return annotation
        return_annotation = None
        if self.return_type:
            try:
                return_ann_expr = cst.parse_expression(self.return_type)
                return_annotation = cst.Annotation(return_ann_expr)
            except:
                return_annotation = cst.Annotation(cst.Name(self.return_type))

        # Parse body
        try:
            body_statements = parse_body_to_statements(self.body)
        except ValueError as e:
            return None, str(e)

        # Create method node
        method = cst.FunctionDef(
            name=cst.Name(self.name),
            params=cst.Parameters(params=params),
            body=cst.IndentedBlock(body=body_statements),
            returns=return_annotation,
            decorators=[],
        )

        # Add decorators
        if self.decorators:
            decorator_nodes = []
            for deco in self.decorators:
                deco_str = deco.lstrip('@').strip()
                try:
                    deco_expr = cst.parse_expression(deco_str)
                except Exception as e:
                    return None, f"Error parsing decorator '{deco}': {e}"
                decorator_nodes.append(cst.Decorator(decorator=deco_expr))
            method = method.with_changes(decorators=decorator_nodes)

        # Find the target class
        class_node = None
        class_idx = None
        for i, stmt in enumerate(module.body):
            if m.matches(stmt, m.ClassDef(name=m.Name(self.class_name))):
                class_node = stmt
                class_idx = i
                break

        if class_node is None:
            return None, f"Class '{self.class_name}' not found."

        # Work with class body
        class_body = list(class_node.body.body)

        # Check if method already exists
        for item in class_body:
            if m.matches(item, m.FunctionDef(name=m.Name(self.name))):
                return None, f"Method '{self.name}' already exists in class '{self.class_name}'."

        # Determine insertion index within class body
        if self.after_method:
            insert_idx = None
            for j, item in enumerate(class_body):
                if m.matches(item, m.FunctionDef(name=m.Name(self.after_method))):
                    insert_idx = j + 1
                    break
            if insert_idx is None:
                return None, f"Method '{self.after_method}' not found in class '{self.class_name}'."
        else:
            # Append at the end
            insert_idx = len(class_body)

        class_body.insert(insert_idx, method)

        # Update class node
        new_class = class_node.with_changes(
            body=class_node.body.with_changes(body=class_body)
        )

        # Replace class in module body
        new_body = list(module.body)
        new_body[class_idx] = new_class
        new_module = module.with_changes(body=new_body)
        return new_module, f"Inserted method '{self.name}' into class '{self.class_name}' at position {insert_idx}."

    # --------------------------------------------------------------------------
    # add_import
    # --------------------------------------------------------------------------
    def _add_import(self, module: cst.Module) -> tuple[Optional[cst.Module], str]:
        # Helper to check if a statement is a module docstring
        def is_module_docstring(stmt) -> bool:
            return m.matches(
                stmt,
                m.SimpleStatementLine(
                    body=[
                        m.Expr(
                            value=m.SimpleString()
                        )
                    ]
                )
            )

        # Helper to check if a statement contains an import
        def contains_import(stmt) -> bool:
            if m.matches(stmt, m.SimpleStatementLine()):
                for item in stmt.body:
                    if m.matches(item, m.Import() | m.ImportFrom()):
                        return True
            return False

        # Build import node
        if self.from_import:
            # from module import name1, name2
            module_expr = cst.parse_expression(self.import_module)
            names = [
                cst.ImportAlias(name=cst.Name(name.strip()))
                for name in self.import_names
            ]
            import_node = cst.ImportFrom(
                module=module_expr,
                names=names,
                level=None
            )
        else:
            # import module [as alias]
            alias_node = cst.ImportAlias(
                name=cst.Name(self.import_module),
                asname=cst.Name(self.import_alias) if self.import_alias else None
            )
            import_node = cst.Import(names=[alias_node])

        # Determine insertion index
        body = list(module.body)
        insert_idx = 0

        # Skip module docstring if present
        if body and is_module_docstring(body[0]):
            insert_idx = 1

        # Find the last import
        last_import_idx = -1
        for i, stmt in enumerate(body):
            if contains_import(stmt):
                last_import_idx = i

        if last_import_idx >= 0:
            insert_idx = last_import_idx + 1

        # Duplicate check
        new_import_source = module.code_for_node(import_node).strip()
        for stmt in body:
            if contains_import(stmt):
                existing_source = module.code_for_node(stmt).strip()
                if existing_source == new_import_source:
                    return None, f"Import already exists: {new_import_source}"

        # Insert
        body.insert(insert_idx, import_node)
        new_module = module.with_changes(body=body)
        return new_module, f"Added import at position {insert_idx}: {new_import_source}"

    # --------------------------------------------------------------------------
    # add_class
    # --------------------------------------------------------------------------
    def _add_class(self, module: cst.Module) -> tuple[Optional[cst.Module], str]:
        # Helper to parse body (same as before)
        def parse_body_to_statements(body_str: str) -> list[cst.BaseStatement]:
            if not body_str.strip():
                return []
            dummy_func = f"def _dummy():\n" + "".join(f"    {line}\n" for line in body_str.splitlines())
            try:
                dummy_module = cst.parse_module(dummy_func)
                func_def = dummy_module.body[0]
                if not isinstance(func_def, cst.FunctionDef):
                    raise ValueError("Parsing did not yield a FunctionDef")
                return list(func_def.body.body)
            except Exception as e:
                raise ValueError(f"Failed to parse class body: {e}")

        # Parse body
        try:
            body_statements = parse_body_to_statements(self.body)
        except ValueError as e:
            return None, str(e)

        # Build base classes
        bases = []
        if self.bases:
            for base in self.bases:
                bases.append(cst.Arg(value=cst.parse_expression(base.strip())))

        # Build decorators
        decorators = []
        if self.decorators:
            for deco in self.decorators:
                deco_str = deco.lstrip('@').strip()
                try:
                    deco_expr = cst.parse_expression(deco_str)
                except Exception as e:
                    return None, f"Error parsing decorator '{deco}': {e}"
                decorators.append(cst.Decorator(decorator=deco_expr))

        # Create ClassDef node
        class_node = cst.ClassDef(
            name=cst.Name(self.name),
            bases=bases,
            keywords=[],
            body=cst.IndentedBlock(body=body_statements),
            decorators=decorators,
        )

        # Determine insertion position
        body = list(module.body)
        insert_idx = 0

        # Skip module docstring
        if len(body) > 0 and m.matches(body[0], m.SimpleStatementLine(body=[m.Expr(value=m.SimpleString())])):
            insert_idx = 1

        # Find last import
        last_import_idx = -1
        for i, stmt in enumerate(body):
            if m.matches(stmt, m.SimpleStatementLine()):
                for item in stmt.body:
                    if m.matches(item, m.Import() | m.ImportFrom()):
                        last_import_idx = i
                        break
        if last_import_idx >= 0:
            insert_idx = last_import_idx + 1

        # If after parameter given, insert after that class
        if self.after:
            found_idx = None
            for i, stmt in enumerate(body):
                if m.matches(stmt, m.ClassDef(name=m.Name(self.after))):
                    found_idx = i
                    break
            if found_idx is None:
                return None, f"Class '{self.after}' not found to insert after."
            insert_idx = found_idx + 1
        else:
            # If there are existing classes, insert at end of module
            last_class_idx = -1
            for i, stmt in enumerate(body):
                if m.matches(stmt, m.ClassDef()):
                    last_class_idx = i
            if last_class_idx >= 0:
                insert_idx = last_class_idx + 1

        # Check for existing class with same name
        for stmt in body:
            if m.matches(stmt, m.ClassDef(name=m.Name(self.name))):
                return None, f"A class named '{self.name}' already exists."

        body.insert(insert_idx, class_node)
        new_module = module.with_changes(body=body)
        return new_module, f"Inserted class '{self.name}' at position {insert_idx}."

    # --------------------------------------------------------------------------
    # replace_function_body
    # --------------------------------------------------------------------------
    def _replace_function_body(self, module: cst.Module) -> tuple[Optional[cst.Module], str]:
        # Helper to parse body (same as above)
        def parse_body_to_statements(body_str: str) -> list[cst.BaseStatement]:
            if not body_str.strip():
                return []
            dummy_func = f"def _dummy():\n" + "".join(f"    {line}\n" for line in body_str.splitlines())
            try:
                dummy_module = cst.parse_module(dummy_func)
                func_def = dummy_module.body[0]
                if not isinstance(func_def, cst.FunctionDef):
                    raise ValueError("Parsing did not yield a FunctionDef")
                return list(func_def.body.body)
            except Exception as e:
                raise ValueError(f"Failed to parse function body: {e}")

        # Parse new body
        try:
            new_body_statements = parse_body_to_statements(self.new_body)
        except ValueError as e:
            return None, str(e)

        if self.class_name:
            # Method inside a class
            class_node = None
            for stmt in module.body:
                if m.matches(stmt, m.ClassDef(name=m.Name(self.class_name))):
                    class_node = stmt
                    break
            if class_node is None:
                return None, f"Class '{self.class_name}' not found."

            # Find method inside class body
            method_node = None
            method_idx = None
            class_body = list(class_node.body.body)
            for idx, item in enumerate(class_body):
                if m.matches(item, m.FunctionDef(name=m.Name(self.target))):
                    method_node = item
                    method_idx = idx
                    break
            if method_node is None:
                return None, f"Method '{self.target}' not found in class '{self.class_name}'."

            # Preserve docstring if requested
            old_body = method_node.body.body
            if self.preserve_docstring and old_body and isinstance(old_body[0], cst.SimpleStatementLine):
                first_stmt = old_body[0]
                if m.matches(first_stmt, m.SimpleStatementLine(body=[m.Expr(value=m.SimpleString())])):
                    new_body_statements.insert(0, first_stmt)

            # Create new method with new body
            new_method = method_node.with_changes(
                body=method_node.body.with_changes(body=new_body_statements)
            )

            # Replace in class body
            class_body[method_idx] = new_method
            new_class = class_node.with_changes(
                body=class_node.body.with_changes(body=class_body)
            )

            # Replace class in module body
            new_body = []
            replaced = False
            for stmt in module.body:
                if stmt is class_node:
                    new_body.append(new_class)
                    replaced = True
                else:
                    new_body.append(stmt)
            if not replaced:
                return None, "Internal error: class not replaced."
            new_module = module.with_changes(body=new_body)
            return new_module, f"Replaced body of method '{self.target}' in class '{self.class_name}'."

        else:
            # Module-level function
            func_node = None
            func_idx = None
            for idx, stmt in enumerate(module.body):
                if m.matches(stmt, m.FunctionDef(name=m.Name(self.target))):
                    func_node = stmt
                    func_idx = idx
                    break
            if func_node is None:
                return None, f"Function '{self.target}' not found."

            # Preserve docstring if requested
            old_body = func_node.body.body
            if self.preserve_docstring and old_body and isinstance(old_body[0], cst.SimpleStatementLine):
                first_stmt = old_body[0]
                if m.matches(first_stmt, m.SimpleStatementLine(body=[m.Expr(value=m.SimpleString())])):
                    new_body_statements.insert(0, first_stmt)

            new_func = func_node.with_changes(
                body=func_node.body.with_changes(body=new_body_statements)
            )

            new_body = list(module.body)
            new_body[func_idx] = new_func
            new_module = module.with_changes(body=new_body)
            return new_module, f"Replaced body of function '{self.target}'."
    # --------------------------------------------------------------------------
    # modify_function
    # --------------------------------------------------------------------------
    def _modify_function(self, module: cst.Module) -> tuple[Optional[cst.Module], str]:
        # Helper to parse body (same as add_function)
        def parse_body_to_statements(body_str: str) -> list[cst.BaseStatement]:
            if not body_str.strip():
                return []
            dummy_func = f"def _dummy():\n" + "".join(f"    {line}\n" for line in body_str.splitlines())
            try:
                dummy_module = cst.parse_module(dummy_func)
                func_def = dummy_module.body[0]
                if not isinstance(func_def, cst.FunctionDef):
                    raise ValueError("Parsing did not yield a FunctionDef")
                return list(func_def.body.body)
            except Exception as e:
                raise ValueError(f"Failed to parse function body: {e}")

        # Find the target function/method
        if self.class_name:
            # Find class
            class_node = None
            for stmt in module.body:
                if m.matches(stmt, m.ClassDef(name=m.Name(self.class_name))):
                    class_node = stmt
                    break
            if class_node is None:
                return None, f"Class '{self.class_name}' not found."
            # Find method inside class
            target_node = None
            target_idx = None
            class_body = list(class_node.body.body)
            for idx, item in enumerate(class_body):
                if m.matches(item, m.FunctionDef(name=m.Name(self.name))):
                    target_node = item
                    target_idx = idx
                    break
            if target_node is None:
                return None, f"Method '{self.name}' not found in class '{self.class_name}'."
            is_method = True
        else:
            # Find module-level function
            target_node = None
            target_idx = None
            for idx, stmt in enumerate(module.body):
                if m.matches(stmt, m.FunctionDef(name=m.Name(self.name))):
                    target_node = stmt
                    target_idx = idx
                    break
            if target_node is None:
                return None, f"Function '{self.name}' not found."
            is_method = False

        existing_params = target_node.params
        existing_returns = target_node.returns
        existing_body = target_node.body
        existing_decorators = target_node.decorators

        # Build new parameters if provided
        if self.parameters is not None:
            params = []
            for p in self.parameters:
                p = p.strip()
                if ':' in p and '=' in p:
                    name = p.split(':')[0].strip()
                    param = cst.Param(name=cst.Name(name))
                elif ':' in p:
                    name, annotation = p.split(':', 1)
                    name = name.strip()
                    annotation = annotation.strip()
                    try:
                        ann_expr = cst.parse_expression(annotation)
                    except:
                        ann_expr = cst.Name(annotation)
                    param = cst.Param(
                        name=cst.Name(name),
                        annotation=cst.Annotation(ann_expr)
                    )
                elif '=' in p:
                    name, default = p.split('=', 1)
                    name = name.strip()
                    default = default.strip()
                    try:
                        default_expr = cst.parse_expression(default)
                    except:
                        default_expr = cst.Name(default)
                    param = cst.Param(
                        name=cst.Name(name),
                        default=default_expr
                    )
                else:
                    param = cst.Param(name=cst.Name(p))
                params.append(param)
            new_params = cst.Parameters(params=params)
        else:
            new_params = existing_params

        # Build new return annotation if provided
        if self.return_type is not None:
            try:
                return_ann_expr = cst.parse_expression(self.return_type)
                new_returns = cst.Annotation(return_ann_expr)
            except:
                new_returns = cst.Annotation(cst.Name(self.return_type))
        else:
            new_returns = existing_returns

        # Build new decorators if provided
        if self.decorators is not None:
            decorator_nodes = []
            for deco in self.decorators:
                deco_str = deco.lstrip('@').strip()
                try:
                    deco_expr = cst.parse_expression(deco_str)
                except Exception as e:
                    return None, f"Error parsing decorator '{deco}': {e}"
                decorator_nodes.append(cst.Decorator(decorator=deco_expr))
            new_decorators = decorator_nodes
        else:
            new_decorators = existing_decorators

        # Build new body if provided
        if self.body is not None:
            try:
                body_statements = parse_body_to_statements(self.body)
                new_body = cst.IndentedBlock(body=body_statements)
            except ValueError as e:
                return None, str(e)
            # Preserve docstring if requested
            if self.preserve_docstring:
                old_body = existing_body.body
                if old_body and isinstance(old_body[0], cst.SimpleStatementLine):
                    first_stmt = old_body[0]
                    if m.matches(first_stmt, m.SimpleStatementLine(body=[m.Expr(value=m.SimpleString())])):
                        # Insert docstring at start of new body
                        new_body_statements = list(new_body.body)
                        new_body_statements.insert(0, first_stmt)
                        new_body = cst.IndentedBlock(body=new_body_statements)
        else:
            new_body = existing_body

        # Determine new name
        new_name = self.name
        if self.new_name is not None:
            new_name = self.new_name.strip()
            if new_name == "":
                new_name = self.name  # treat empty string as no rename
        # Check for name conflict if renaming
        if new_name != self.name:
            if is_method:
                # Within same class, check if another method with new_name exists
                # (excluding the target method itself)
                for idx, item in enumerate(class_body):
                    if idx != target_idx and m.matches(item, m.FunctionDef(name=m.Name(new_name))):
                        return None, f"Cannot rename to '{new_name}': a method with that name already exists in class '{self.class_name}'."
            else:
                # Module level, use _function_exists
                if self._function_exists(module, new_name):
                    # Ensure it's not the same function (should not happen)
                    return None, f"Cannot rename to '{new_name}': a function with that name already exists."

        # Create modified function node
        modified_func = target_node.with_changes(
            name=cst.Name(new_name) if new_name != self.name else target_node.name,
            params=new_params,
            returns=new_returns,
            decorators=new_decorators,
            body=new_body
        )

        if is_method:
            class_body[target_idx] = modified_func
            new_class = class_node.with_changes(
                body=class_node.body.with_changes(body=class_body)
            )
            # Replace class in module body
            new_body_list = []
            replaced = False
            for stmt in module.body:
                if stmt is class_node:
                    new_body_list.append(new_class)
                    replaced = True
                else:
                    new_body_list.append(stmt)
            if not replaced:
                return None, "Internal error: class not replaced."
            new_module = module.with_changes(body=new_body_list)
        else:
            # Replace module-level function
            new_body_list = list(module.body)
            new_body_list[target_idx] = modified_func
            new_module = module.with_changes(body=new_body_list)

        rename_msg = f" renamed to '{new_name}'" if self.new_name else ""
        return new_module, f"Modified function '{self.name}'{rename_msg}."
