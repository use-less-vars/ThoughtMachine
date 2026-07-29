"""Deep-merge utility for nested dictionaries."""

from __future__ import annotations

import copy


def deep_merge(base: dict, overlay: dict) -> dict:
    """Deep-merge *overlay* into *base* and return a new dict.

    Rules:
    - Nested dicts are recursively merged.
    - Lists in *overlay* replace lists in *base* (not concatenated).
    - Scalar values in *overlay* override scalars in *base*.
    - The original dicts are not mutated.
    - Keys present only in *overlay* are added.
    - Keys present only in *base* are preserved.
    """
    result = copy.deepcopy(base)

    for key, overlay_value in overlay.items():
        if key in result:
            base_value = result[key]
            if isinstance(base_value, dict) and isinstance(overlay_value, dict):
                result[key] = deep_merge(base_value, overlay_value)
            else:
                # Lists replace, scalars override
                result[key] = copy.deepcopy(overlay_value)
        else:
            result[key] = copy.deepcopy(overlay_value)

    return result
