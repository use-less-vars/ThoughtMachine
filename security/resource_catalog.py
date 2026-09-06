"""
Canonical resource-permission catalog.

Single source of truth for the resource keys and allowed values a session
permission grant set may carry (permission-simplification effort, see
``.thoughtmachine/working_docs/impl_plan_permission_simplification.md``).

Session-grant keys are ``git``, ``filesystem``, ``container``, ``network``
and ``mcp`` -- every session-grant write AND read goes through
:func:`coerce_resource_permissions`.  ``host_bash`` remains in the catalog
as the vocabulary for workspace ceilings: it is a ceiling-only grain (the
session store pops it before coercion) and is never stored at the session
level.  ``system`` and ``execution`` are deliberately NOT catalog keys -- no
tool consumer sits above their safe defaults (``system: read``,
``execution: banned`` on ``SessionPermissions``), which the security gate
applies.  The workspace ceiling keeps the rest of the workspace vocabulary
(``docker``, ``git_read``, ``git_write``, ...) and is enforced separately by
the security gate; ``thoughtmachine/permission_store.workspace_ceiling`` is
therefore NOT coerced.

The canonical catalog::

    git          banned | ask | read | write | write_on_feature_branch
    filesystem   banned | read | write
    container    True | False
    network      banned | ask | write | outbound
    mcp          banned | connect | full
    host_bash    banned | ask | allow     (workspace-ceiling-only)
"""

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

#: Canonical resource name -> allowed values (validated verbatim).
RESOURCE_CATALOG = {
    "git": ["banned", "ask", "read", "write", "write_on_feature_branch"],
    "filesystem": ["banned", "read", "write"],
    "container": [True, False],
    "network": ["banned", "ask", "write", "outbound"],
    "mcp": ["banned", "connect", "full"],
    "host_bash": ["banned", "ask", "allow"],
}

#: Set of canonical resource keys (for membership checks / introspection).
canonical_resource_keys = set(RESOURCE_CATALOG)


def _value_is_valid(key: str, value: Any) -> bool:
    """True when *value* is an allowed level for the canonical *key*.

    ``container`` accepts real booleans ONLY: ``0``/``1`` would pass a bare
    ``value in [True, False]`` membership test through the int/bool equality
    pitfall, so non-bool ints must be rejected explicitly.
    """
    if key == "container":
        return isinstance(value, bool) and value in RESOURCE_CATALOG[key]
    return value in RESOURCE_CATALOG[key]


def coerce_resource_permissions(raw: dict) -> dict:
    """Return a clean copy of *raw* holding only valid catalog entries.

    Unknown keys are dropped (one warning each); keys whose value is not a
    valid level for that resource are dropped too (``container`` accepts only
    real booleans).  Valid entries are preserved in input order.
    """
    clean: Dict[str, Any] = {}
    for key, value in raw.items():
        if key not in canonical_resource_keys:
            logger.warning(
                "dropping unknown session permission key %r (not in "
                "RESOURCE_CATALOG)",
                key,
            )
            continue
        if not _value_is_valid(key, value):
            logger.warning(
                "dropping invalid value %r for session permission key %r "
                "(allowed: %s)",
                value,
                key,
                RESOURCE_CATALOG[key],
            )
            continue
        clean[key] = value
    return clean
