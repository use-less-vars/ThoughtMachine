"""Fast JSON repair library using Rust for performance."""

from typing import Any, Optional, Union
import json as _json
import orjson
from fast_json_repair._fast_json_repair import _repair_json_rust

__version__ = "0.2.0"
__all__ = ["repair_json", "loads"]


def repair_json(
    json_string: str,
    *,
    return_objects: bool = False,
    skip_json_loads: bool = False,
    ensure_ascii: bool = True,
    indent: Optional[int] = None,
    **kwargs
) -> Union[str, Any]:
    """
    Repair invalid JSON string and return either the repaired JSON string or parsed object.
    
    Args:
        json_string: The potentially invalid JSON string to repair
        return_objects: If True, return the parsed Python object instead of JSON string
        skip_json_loads: If True, skip initial validation with orjson.loads
        ensure_ascii: If True, escape non-ASCII characters in output
        indent: Number of spaces for indentation (None for compact output)
        **kwargs: Additional arguments (for compatibility)
    
    Returns:
        Either the repaired JSON string or parsed Python object (if return_objects=True)
    """
    if not isinstance(json_string, str):
        raise TypeError(f"Expected string, got {type(json_string).__name__}")
    
    if not json_string.strip():
        if return_objects:
            return None
        return "null"
    
    # Fast path: if skip_json_loads is False, try parsing with orjson first
    if not skip_json_loads:
        try:
            parsed = orjson.loads(json_string)
            if return_objects:
                return parsed
            # Decide serializer:
            # - orjson for fast path only when (not ensure_ascii) and (indent is None or 2)
            if not ensure_ascii and (indent is None or indent == 2):
                opts = 0
                if indent == 2:
                    opts |= orjson.OPT_INDENT_2
                return orjson.dumps(parsed, option=opts).decode('utf-8')
            # Fallback to stdlib json to respect ensure_ascii and arbitrary indent
            return _json.dumps(parsed, ensure_ascii=ensure_ascii, indent=indent)
        except (orjson.JSONDecodeError, TypeError):
            pass  # Fall through to repair logic
    
    # Call Rust repair function
    repaired = _repair_json_rust(json_string, ensure_ascii, indent or 0)
    
    if return_objects:
        # Parse the repaired JSON string
        try:
            return orjson.loads(repaired)
        except orjson.JSONDecodeError:
            # If still invalid after repair, return None
            return None
    
    return repaired


def loads(json_string: str, **kwargs) -> Any:
    """
    Repair and parse invalid JSON string to Python object.
    
    This is a convenience wrapper around repair_json with return_objects=True.
    
    Args:
        json_string: The potentially invalid JSON string to repair and parse
        **kwargs: Additional arguments passed to repair_json
    
    Returns:
        The parsed Python object
    """
    return repair_json(json_string, return_objects=True, **kwargs)

