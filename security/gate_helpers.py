"""
gate_helpers.py -- Import-free permission helpers for the security gate.

``_value_satisfies`` originally lived inside ``security.security_gate`` but
was moved here to break the order-dependent circular import:

    security.security_gate -> (transitively) tools -> tools.git_info_tool
    -> security.sandboxed_execution -> security.security_gate  (cycle!)

This module is a LEAF: it must never import ``security_gate``, ``agent.*``,
``tools.*``, ``thoughtmachine.*`` or any other project module -- the single
carve-out is ``security.resource_catalog`` (pure stdlib imports only:
logging/typing), which owns the shared grant-level rank table
(``GRANT_LEVEL_RANKS``).  That keeps every importer of ``_value_satisfies``
(``security_gate``, ``sandboxed_execution``) free of a
partially-initialized-module ImportError.
"""

from __future__ import annotations

from security.resource_catalog import GRANT_LEVEL_RANKS


def _value_satisfies(required: str, allowed: object) -> bool | str:
    """
    Check whether a single required value is satisfied by the allowed setting.
    (Mirrors the logic in ``tool_executor._value_satisfies``.)

    Returns:
        * ``True`` if permission is granted.
        * ``False`` if permission is denied.
        * ``"ASK"`` if the allowed value is ``'ask'`` and the required access
          is above the read level.

    ``write_on_feature_branch`` on the allowed side ranks at the write
    level (3, via ``GRANT_LEVEL_RANKS``): it satisfies ``read`` and
    ``write`` requirements (the ``git:write`` category gate passes) while
    denying ``full`` requirements (rank 4).  The feature-branch-only
    restriction itself is enforced inside the branch-aware git write tool,
    which permits commits only on non-protected branches.

    ``outbound`` ranks above plain ``write`` (3.5): a ``network:outbound``
    requirement is satisfied only by an ``outbound`` or ``full`` grant -- a
    bare ``write`` grant now denies outbound-only network access.
    """
    sentinel_ask = "ASK"
    required_lower = str(required).lower()

    # --- Determine required level ---
    if required_lower in ("false", "no", "banned"):
        required_level = 0
    elif required_lower in ("true", "yes"):
        required_level = GRANT_LEVEL_RANKS["write"]  # write-level
    elif required_lower == "outbound":
        required_level = GRANT_LEVEL_RANKS["outbound"]  # above write (3.5)
    else:
        aliases = {"deny": "banned", "denied": "banned", "all": "full"}
        level_name = aliases.get(required_lower, required_lower)
        required_level = GRANT_LEVEL_RANKS.get(level_name)

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
    allowed_level_name = aliases.get(allowed_str, allowed_str)
    # GRANT_LEVEL_RANKS covers write_on_feature_branch at the write tier (3)
    # and outbound above write (3.5): no per-level special-casing needed.
    allowed_level = GRANT_LEVEL_RANKS.get(allowed_level_name)

    if required_level is None or allowed_level is None:
        # Fall back to exact match
        return allowed_str == required_lower

    return allowed_level >= required_level


def resolve_network_mode(network_level) -> str:
    """
    Map a session/permission network grant to a Docker ``network_mode``.

    ``True`` and the strings ``"write"`` / ``"outbound"`` (case-insensitive)
    map to ``"bridge"``; everything else (``False``, ``"banned"``, ``"read"``,
    ``"ask"``, ``None``, empty, unknown values) maps to ``"none"``.

    Single source of truth for the network-grant -> ``network_mode`` mapping,
    consumed by ``security.security_gate.resolve_container_config``.
    """
    if network_level is True:
        return "bridge"
    if isinstance(network_level, str) and network_level.lower() in ("write", "outbound"):
        return "bridge"
    return "none"

