"""
Utility functions for the ThoughtMachine agent.

Provides ``deep_merge`` for recursive dict merging (used by config
and session permission handling) and other shared helpers.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping


def deep_merge(base: dict, overlay: Mapping[str, Any]) -> dict:
    """
    Recursively merge *overlay* into *base*, returning a new dict.

    Rules:
    - For keys present in both, if both values are dicts → recurse.
    - If overlay value is ``None`` → delete the key from result (unless
      ``preserve_none`` is set).
    - Otherwise → overlay wins (scalar replacement, list replacement, etc.).

    Args:
        base: The base dictionary (used as starting point).
        overlay: The overlay dictionary whose values take precedence.

    Returns:
        A new merged dictionary.
    """
    result = dict(base)
    for key, overlay_val in overlay.items():
        if key in result:
            base_val = result[key]
            if isinstance(base_val, dict) and isinstance(overlay_val, dict):
                result[key] = deep_merge(base_val, overlay_val)
            elif overlay_val is None:
                result.pop(key, None)
            else:
                result[key] = overlay_val
        else:
            result[key] = overlay_val
    return result
