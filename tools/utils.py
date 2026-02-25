# tools/utils.py
from typing import Any, Dict, Type
from pydantic import BaseModel

def model_to_openai_tool(model: Type[BaseModel]) -> Dict[str, Any]:
    """
    Convert a Pydantic model to an OpenAI tool definition.
    Uses the model's JSON schema for parameters.
    """
    schema = model.model_json_schema()
    # Remove the top-level title and description from parameters
    parameters = {k: v for k, v in schema.items() if k not in ("title", "description")}
    return {
        "type": "function",
        "function": {
            "name": model.__name__,
            "description": schema.get("description", ""),
            "parameters": parameters,
        }
    }