"""
Canonical resource-permission catalog.

Single source of truth for the resource keys and allowed values a session
permission grant set may carry (permission-simplification effort, see
``.thoughtmachine/working_docs/impl_plan_permission_simplification.md``).

Session-grant keys are ``git``, ``filesystem``, ``container``, ``network``,
``mcp`` and ``host_bash`` -- every session-grant write AND read goes
through :func:`coerce_resource_permissions`.  ``host_bash`` is the
supervised host-shell grain: it is storable at the session level like any
catalog key, its ``allow`` level doubles as the workspace-ceiling
vocabulary, and the security gate always caps the session value by the
workspace ceiling at enforcement time.  ``system`` and ``execution`` are
deliberately NOT catalog keys -- no
tool consumer sits above their safe defaults (``system: read``,
``execution: banned`` on ``SessionPermissions``), which the security gate
applies.  The workspace ceiling's canonical key for sandboxed execution is
``container`` (boolean); the legacy alias ``docker`` is accepted by the
security gate and normalised onto ``container`` at enforcement time (a
write-level docker ceiling allows the container grant, banned/read/ask
denies it).  The ceiling keeps the wider workspace vocabulary
(``container``, ``host_bash``, ``git_read``, ``git_write``, ...) and is
enforced separately by the security gate;
``thoughtmachine/permission_store.workspace_ceiling`` is therefore NOT
coerced.

One canonical per-resource vocabulary backs both the session-grant
catalog (:data:`RESOURCE_CATALOG` below) and the workspace-ceiling
whitelist (:data:`WORKSPACE_CEILING_VOCAB`); the same level string means
the same thing in either place.  Session-grant values per canonical key::

    git          banned | ask | read | write | write_on_feature_branch
    filesystem   banned | read | write
    container    True | False
    network      banned | ask | write | outbound
    mcp          banned | connect | full
    host_bash    banned | ask | allow     (host shell; ceiling-capped by the gate)

Workspace-ceiling values, validated by the workspace-permission loader
(``agent/config/resource_catalog.py``) against the same level strings::

    git          banned | ask | read | write_on_feature_branch | write
    filesystem   banned | ask | read | write
    system       banned | ask | read | write   (legacy ceiling grain)
    execution    banned | ask | read | write   (legacy ceiling grain)
    git_read     banned | ask | read | write   (legacy ceiling grain)
    git_write    banned | ask | read | write   (legacy ceiling grain)
    network      banned | ask | write | outbound
    mcp          banned | connect | full
    host_bash    banned | ask | allow
    container    True | False   (boolean ceiling; no string vocabulary)

Two DERIVED flat rank projections back every comparison helper in this
package (``security.gate_helpers`` / ``security.security_gate``); both
project the vocabulary above onto numbers with DIFFERENT orderings
because each answers a different question:

* :data:`GRANT_LEVEL_RANKS` -- session-grant-vs-grant comparison used by
  ``_value_satisfies`` / ``_min_permission`` ("does grant A satisfy a
  request requiring grant B?"):

      banned(0) < ask(1) < read(2) < write(3) == write_on_feature_branch(3)
          < outbound(3.5) < full(4)

  ``write_on_feature_branch`` is a session git level ranking AT the write
  tier (the branch-only restriction is enforced tool-side at commit time);
  ``outbound`` (network outbound-only connectivity) ranks above plain
  ``write`` as a strict superset grant on network's scale.

* :data:`WORKSPACE_CEILING_LEVELS_RANKS` -- workspace-ceiling-vs-grant
  comparison used by ``apply_workspace_ceiling`` ("does the workspace
  ceiling allow this session grant?"):

      banned(0) < read(1) == connect(1) == none(1) < ask(2)
          < write_on_feature_branch(2.5) < write(3)
          < outbound(4) == full(4)

  The ask inversion: a session ``read`` grant ranks 1.0, so an ``ask``
  ceiling (2.0) does NOT cap it -- read-only sessions are never forced
  into an interactive ask loop.  A session ``write`` grant ranks 3.0, so
  an ``ask`` ceiling caps it to the read tier (``read`` for the
  read-capable filesystem/git/git_read/git_write/system/execution keys,
  ``banned`` for the others), and a ``write_on_feature_branch`` *ceiling*
  (2.5, strictly between ask and write) caps ``write`` down to
  branch-restricted write -- never unlimited.  Ceilings at rank 4.0
  (``outbound``/``full``) are unlimited and leave the session grant
  standing.  :data:`WORKSPACE_CEILING_VOCAB` additionally whitelists,
  per session key, which ceiling levels may ever apply; a ceiling level
  outside the key's vocabulary is ignored fail-open by the gate (never
  coerced).

Legacy-load-normalisation policy: the workspace-permission loader
normalises legacy stored ceilings onto the canonical vocabularies --
legacy ``full`` ceilings for ``git``/``filesystem``/``system``/
``execution`` are stored as ``write``, and legacy container string
ceilings are stored as booleans.  Any stray ``full`` ceiling or
container string that still reaches the gate at runtime is handled
fail-open: ``full`` ranks 4.0 (unlimited; the session value stands) and
an out-of-vocab container string is warned about and ignored (no
ceiling applied).

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

#: Session-grant level ranks (single flat order used by the comparison
#: helpers in ``security.gate_helpers`` / ``security.security_gate``).
#:
#:     banned(0) < ask(1) < read(2) < write(3) == write_on_feature_branch(3)
#:         < outbound(3.5) < full(4)
#:
#: ``write_on_feature_branch`` ranks AT the write tier (branch restriction is
#: enforced tool-side at commit time); ``outbound`` ranks above plain
#: ``write`` because it is a strict superset grant on network's scale.
GRANT_LEVEL_RANKS: Dict[str, float] = {
    "banned": 0,
    "ask": 1,
    "read": 2,
    "connect": 3,
    "write": 3,
    "write_on_feature_branch": 3,
    "outbound": 3.5,
    "full": 4,
}

#: Workspace-ceiling level ranks (``apply_workspace_ceiling`` ordering).
#:
#:     banned(0) < read(1) == connect(1) == none(1) < ask(2)
#:         < write_on_feature_branch(2.5) < write(3)
#:         < outbound(4) == full(4)
#:
#: Ceilings at rank 4.0 (``outbound``/``full``) are unlimited.
#: ``write_on_feature_branch`` as a *ceiling* ranks between ask and write
#: (2.5): it caps a session ``write`` grant down to branch-restricted write,
#: never unlimited.  Session-side values are ranked on this same table when
#: a ceiling comparison needs them (``full``/``outbound`` session values
#: rank 4.0 and survive any stricter ceiling).
WORKSPACE_CEILING_LEVELS_RANKS: Dict[str, float] = {
    "banned": 0.0,
    "read": 1.0,
    "connect": 1.0,  # session-side mcp level (below ask/2.0)
    "none": 1.0,  # alias used by some purpose presets
    "ask": 2.0,
    "write_on_feature_branch": 2.5,  # ceiling value: caps write -> branch-write
    "write": 3.0,
    "outbound": 4.0,  # ceiling value: unlimited (above plain write)
    "full": 4.0,  # ceiling value: unlimited
}

#: Per-session-key whitelist of workspace-ceiling levels that may ever be
#: applied to that key (validation vocab used by ``apply_workspace_ceiling``;
#: a ceiling level outside the key's vocabulary is ignored fail-open).
#: ``git_read``/``git_write``/``system``/``execution`` are legacy ceiling
#: grains the gate caps onto session keys.  ``container`` is NOT a string
#: vocabulary key: its ceiling is boolean-only (dedicated gate branch), and
#: legacy string ceilings are normalised to booleans by the
#: workspace-permission loader; a stray string that still reaches the gate is
#: ranked or warned about fail-open.  ``full`` is likewise not a ceiling
#: level for ``git``/``filesystem``/``system``/``execution`` -- legacy stored
#: ``full`` ceilings are loader-normalised to ``write``; a stray runtime
#: ``full`` ceiling ranks 4.0 (unlimited) so it is behaviourally identical to
#: fail-open (session value stands).
WORKSPACE_CEILING_VOCAB: Dict[str, tuple] = {
    "filesystem": ("banned", "ask", "read", "write"),
    "system": ("banned", "ask", "read", "write"),
    "execution": ("banned", "ask", "read", "write"),
    "git": ("banned", "ask", "read", "write_on_feature_branch", "write"),
    "git_read": ("banned", "ask", "read", "write"),
    "git_write": ("banned", "ask", "read", "write"),
    "network": ("banned", "ask", "write", "outbound"),
    "mcp": ("banned", "connect", "full"),
    "host_bash": ("banned", "ask", "allow"),
}


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
