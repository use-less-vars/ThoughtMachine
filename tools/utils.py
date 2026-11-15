# tools/utils.py
from typing import Any, Dict, Type, Tuple
from pydantic import BaseModel
import copy


# Shared default directory exclusions for the traversal tools (GlobTool,
# FileSearchTool, DirectoryTreeTool).  Single source of truth: a tool must not
# carry its own copy, or a new exclusion (e.g. ``.worktrees``) silently applies
# to some tools and not others.
#
# ``.worktrees`` is the git-worktree container: it holds full checkouts of the
# same tree, so traversing it duplicates every hit -- and for a worker or
# session bound to one of those worktrees, it leaks the sibling checkouts into
# its own results.
DEFAULT_EXCLUDE_DIRS: Tuple[str, ...] = (
    "__pycache__",
    ".git",
    ".svn",
    ".hg",
    "node_modules",
    ".idea",
    ".vscode",
    ".pytest_cache",
    "build",
    "dist",
    "*.egg-info",
    "venv",
    "env",
    ".venv",
    ".worktrees",
)


def _simplify_schema(schema: dict) -> dict:
    """
    Recursively remove titles, defaults, and simplify nullable anyOf.
    Returns a new simplified schema.
    """
    # Work on a copy
    schema = copy.deepcopy(schema)
    
    def process(node) -> Tuple[Any, bool]:
        """
        Process a schema node, return (processed_node, nullable).
        nullable indicates if the original node allowed null (i.e., we removed a null type).
        """
        nullable = False
        
        if isinstance(node, dict):
            # Remove title and default keys
            node.pop('title', None)
            node.pop('default', None)
            
            # Handle anyOf
            if 'anyOf' in node:
                any_of = node['anyOf']
                non_null_elements = []
                has_null = False
                for elem in any_of:
                    processed_elem, elem_nullable = process(elem)
                    if elem_nullable:
                        has_null = True
                    else:
                        non_null_elements.append(processed_elem)
                
                if has_null:
                    nullable = True
                
                if len(non_null_elements) == 1:
                    # Replace node with the single element
                    node = non_null_elements[0]
                    # Continue processing the new node, preserving nullable flag
                    processed_node, sub_nullable = process(node)
                    nullable = nullable or sub_nullable
                    return processed_node, nullable
                else:
                    node['anyOf'] = non_null_elements
                    # If anyOf becomes empty (should not happen), remove it
                    if not node['anyOf']:
                        del node['anyOf']            
            # Process properties and required
            if 'properties' in node:
                props = node['properties']
                nullable_props = []
                for prop_name, prop_schema in props.items():
                    processed_schema, prop_nullable = process(prop_schema)
                    props[prop_name] = processed_schema
                    if prop_nullable:
                        nullable_props.append(prop_name)
                
                # Remove nullable properties from required list
                if 'required' in node and nullable_props:
                    required = node['required']
                    node['required'] = [r for r in required if r not in nullable_props]
                    if not node['required']:
                        del node['required']
            
            # Process other keys recursively
            for key, value in list(node.items()):
                if key not in ('anyOf', 'properties', 'required'):
                    processed_value, _ = process(value)
                    node[key] = processed_value
        
        elif isinstance(node, list):
            processed_list = []
            for item in node:
                processed_item, _ = process(item)
                processed_list.append(processed_item)
            node = processed_list
        
        # Determine if node is a null type (should have been removed earlier)
        if isinstance(node, dict) and node.get('type') == 'null':
            # This should not happen, but if it does, treat as nullable and remove
            return None, True
        
        return node, nullable
    
    simplified, _ = process(schema)
    return simplified


def model_to_openai_tool(model: Type[BaseModel]) -> Dict[str, Any]:
    """
    Convert a Pydantic model to an OpenAI tool definition.
    Uses the model's JSON schema for parameters, then simplifies it.
    """
    schema = model.model_json_schema()
    # Exclude internal infrastructure fields from tool schema (set by agent)
    infra_fields = {"workspace_path", "worktree_root", "token_limit", "is_docker", "container_workspace_path", "tool"}
    if "properties" in schema:
        for field in infra_fields:
            if field in schema["properties"]:
                del schema["properties"][field]
                if "required" in schema and field in schema["required"]:
                    schema["required"].remove(field)
    # Remove the top-level title and description from parameters
    parameters = {k: v for k, v in schema.items() if k not in ("title", "description")}
    # Simplify the parameters schema
    parameters = _simplify_schema(parameters)
    # Stable tool name: prefer the tool's ``name`` ClassVar (tool_name());
    # fall back to the class name for legacy/non-tool models.
    if hasattr(model, "tool_name"):
        tool_name = model.tool_name()
    else:
        tool_name = model.__name__
    return {
        "type": "function",
        "function": {
            "name": tool_name,
            "description": schema.get("description", ""),
            "parameters": parameters,
        }
    }