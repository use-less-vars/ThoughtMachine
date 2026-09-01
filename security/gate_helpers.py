"""
gate_helpers.py -- Import-free permission helpers for the security gate.

``_value_satisfies`` originally lived inside ``security.security_gate`` but
was moved here to break the order-dependent circular import:

    security.security_gate -> (transitively) tools -> tools.git_info_tool
    -> security.sandboxed_execution -> security.security_gate  (cycle!)

This module is a LEAF: it must never import ``security_gate``, ``agent.*``,
``tools.*``, ``thoughtmachine.*`` or any other project module, so that every
importer of ``_value_satisfies`` (``security_gate``, ``sandboxed_execution``)
gets it without triggering a partially-initialized-module ImportError.
"""

from __future__ import annotations


def _value_satisfies(required: str, allowed: object) -> bool | str:
    """
    Check whether a single required value is satisfied by the allowed setting.
    (Mirrors the logic in ``tool_executor._value_satisfies``.)

    Returns:
        * ``True`` if permission is granted.
        * ``False`` if permission is denied.
        * ``"ASK"`` if the allowed value is ``'ask'`` and the required access
          is above the read level.
    """
    sentinel_ask = "ASK"
    required_lower = str(required).lower()

    # --- Determine required level ---
    if required_lower in ("false", "no", "banned"):
        required_level = 0
    elif required_lower in ("true", "yes", "outbound"):
        required_level = 3  # write-level
    else:
        aliases = {"deny": "banned", "denied": "banned", "all": "full"}
        level_name = aliases.get(required_lower, required_lower)
        level_map = {"banned": 0, "ask": 1, "read": 2, "connect": 3, "write": 3, "full": 4}
        required_level = level_map.get(level_name)

    # --- Handle allowed value ---
    if isinstance(allowed, bool):
        # True satisfies everything; False satisfies nothing
        return allowed is True

    allowed_str = str(allowed).lower()

    # --- Handle 'ask' ---
    if allowed_str == "ask":
        if required_level is None:
            return sentinel_ask
        if required_level <= 2:  # banned/read
            return True
        return sentinel_ask

    # --- String comparison ---
    aliases = {"deny": "banned", "denied": "banned", "all": "full"}
    level_map = {"banned": 0, "ask": 1, "read": 2, "connect": 3, "write": 3, "full": 4}
    allowed_level_name = aliases.get(allowed_str, allowed_str)
    allowed_level = level_map.get(allowed_level_name)

    if required_level is None or allowed_level is None:
        # Fall back to exact match
        return allowed_str == required_lower

    return allowed_level >= required_level
