# tools/utils.py
from typing import Any, Dict, Type, Tuple
from pydantic import BaseModel
import copy


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
    # Exclude workspace_path from tool schema (automatically set by agent)
    if "properties" in schema and "workspace_path" in schema["properties"]:
        del schema["properties"]["workspace_path"]
        if "required" in schema and "workspace_path" in schema["required"]:
            schema["required"].remove("workspace_path")
    # Remove the top-level title and description from parameters
    parameters = {k: v for k, v in schema.items() if k not in ("title", "description")}
    # Simplify the parameters schema
    parameters = _simplify_schema(parameters)
    return {
        "type": "function",
        "function": {
            "name": model.__name__,
            "description": schema.get("description", ""),
            "parameters": parameters,
        }
    }