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
denies it).  The ceiling surface is the six canonical resources only
(``container`` boolean; ``git``, ``filesystem``, ``network``, ``mcp``,
``host_bash``); the legacy ceiling grains
``git_read``/``git_write``/``system``/``execution`` were removed from it
-- legacy stored values are folded onto ``git`` or dropped by the
workspace-permission loader, and workspace-permission PUT validation
rejects the names.  The ceiling is enforced separately by the security
gate; ``thoughtmachine/permission_store.workspace_ceiling`` is therefore
NOT coerced by :func:`coerce_resource_permissions`.

One canonical per-resource vocabulary backs both the session-grant
catalog (:data:`RESOURCE_CATALOG` below) and the workspace-ceiling
whitelist (:data:`WORKSPACE_CEILING_VOCAB`); the same level string means
the same thing in either place.  Session-grant values per canonical key::

    git          banned | ask | read | write | write_on_feature_branch
    filesystem   banned | ask | read | write
    container    True | False
    network      banned | ask | write | outbound
    mcp          banned | connect | full
    host_bash    banned | ask | allow     (host shell; ceiling-capped by the gate)

Workspace-ceiling values, validated by the workspace-permission loader
(``agent/config/resource_catalog.py``) against the same level strings::

    git          banned | ask | read | write_on_feature_branch | write
    filesystem   banned | ask | read | write
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

      banned(0) < read(1) < ask(2) < write(3) == write_on_feature_branch(3)
          < outbound(3.5) < full(4)

  ``read`` ranks strictly BELOW ``ask`` because read is a strict subset of
  ask: a read grant performs read-only work silently, while ask additionally
  authorises interactive approval for the write tier.
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
  read-capable filesystem/git keys,
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
legacy ``full`` ceilings for ``git``/``filesystem`` are stored as
``write``, legacy container string ceilings are stored as booleans, and
the removed legacy grains are folded or dropped (``git_read`` +
``git_write`` fold onto the single ``git`` ceiling; ``system`` and
``execution`` are dropped).  Any stray ``full`` ceiling or
container string that still reaches the gate at runtime is handled
fail-open: ``full`` ranks 4.0 (unlimited; the session value stands) and
an out-of-vocab container string is warned about and ignored (no
ceiling applied).

"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: Canonical resource name -> allowed values (validated verbatim).
RESOURCE_CATALOG = {
    "git": ["banned", "ask", "read", "write", "write_on_feature_branch"],
    "filesystem": ["banned", "ask", "read", "write"],
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
#:     banned(0) < read(1) < ask(2) < write(3) == write_on_feature_branch(3)
#:         < outbound(3.5) < full(4)
#:
#: ``read`` ranks strictly BELOW ``ask`` because read is a strict subset of
#: ask (read-only work needs no prompt; ask additionally authorises the write
#: tier interactively).  ``write_on_feature_branch`` ranks AT the write tier
#: (branch restriction is enforced tool-side at commit time); ``outbound``
#: ranks above plain ``write`` because it is a strict superset grant on
#: network's scale.
GRANT_LEVEL_RANKS: Dict[str, float] = {
    "banned": 0,
    "read": 1,
    "ask": 2,
    # TODO(edge3.1): connect rank pending Main
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
    # TODO(edge3.1): connect rank pending Main
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
#: a ceiling level outside the key's vocabulary is ignored fail-open).  The
#: ceiling surface is exactly the six canonical session-grant resources; the
#: legacy ceiling grains ``git_read``/``git_write``/``system``/``execution``
#: were removed (legacy stored values are folded/dropped by the loader and
#: PUT validation rejects the names with a legacy hint).  ``container`` is
#: NOT a string vocabulary key: its ceiling is boolean-only (dedicated gate
#: branch), and legacy string ceilings are normalised to booleans by the
#: workspace-permission loader; a stray string that still reaches the gate is
#: ranked or warned about fail-open.  ``full`` is likewise not a ceiling
#: level for ``git``/``filesystem`` -- legacy stored ``full`` ceilings are
#: loader-normalised to ``write``; a stray runtime ``full`` ceiling ranks
#: 4.0 (unlimited) so it is behaviourally identical to fail-open (session
#: value stands).
WORKSPACE_CEILING_VOCAB: Dict[str, tuple] = {
    "git": ("banned", "ask", "read", "write_on_feature_branch", "write"),
    "filesystem": ("banned", "ask", "read", "write"),
    "network": ("banned", "ask", "write", "outbound"),
    "mcp": ("banned", "connect", "full"),
    "host_bash": ("banned", "ask", "allow"),
}

#: Git-grain *breadth* ranks (broader grant = SMALLER number), consumed by
#: ``thoughtmachine.vault_repair`` to fold the legacy ``git_read`` /
#: ``git_write`` grains onto the canonical ``git`` grain:
#:
#:     write(0) < write_on_feature_branch(1) < ask(2) < read(3) < banned(4)
#:
#: DERIVED tie-break ordering -- NOT independent truth.  It is the git level
#: vocabulary sorted by ``WORKSPACE_CEILING_LEVELS_RANKS`` descending (strict
#: across the git vocab), re-expressed as integer breadth ranks.  The flat
#: ``GRANT_LEVEL_RANKS`` cannot order these (it ties ``write`` ==
#: ``write_on_feature_branch``), so this import-backed view exists purely so the
#: fold order can never drift from the ceiling scale.
GIT_BREADTH_RANKS: Dict[str, int] = {
    level: idx
    for idx, level in enumerate(
        sorted(
            WORKSPACE_CEILING_VOCAB["git"],
            key=lambda lvl: WORKSPACE_CEILING_LEVELS_RANKS[lvl],
            reverse=True,
        )
    )
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


#: Legacy ``container``/``docker`` ceiling strings and their canonical
#: boolean ceiling (mirrors the workspace-permission loader's string fold).
_LEGACY_CONTAINER_CEILING_BOOL: Dict[str, bool] = {
    "True": True,
    "False": False,
    "banned": False,
    "read": False,
    "ask": False,
    "write": True,
    "full": True,
}

#: Ceiling-permissiveness ranking used only to fold the removed legacy git
#: grains (``git_read`` / ``git_write``) onto the single canonical ``git``
#: ceiling.  DERIVED from :data:`WORKSPACE_CEILING_LEVELS_RANKS` (restricted to
#: the git ceiling vocabulary: banned < read < ask < write_on_feature_branch
#: < write) so the fold order can never drift from the ceiling scale.
_GIT_FOLD_RANKS: Dict[str, float] = {
    level: WORKSPACE_CEILING_LEVELS_RANKS[level]
    for level in (
        "banned",
        "read",
        "ask",
        "write_on_feature_branch",
        "write",
    )
}


def fold_legacy_workspace_ceiling(raw: dict) -> dict:
    """Map legacy stored workspace ceiling values to canonical catalog values.

    Single source of truth for the legacy->canonical workspace-ceiling fold,
    shared by the read-time loader
    (``web_ui.backend.config_manager.normalize_legacy_workspace_ceiling``,
    which delegates here).  Old workspace ``config.json`` ``permissions``
    maps were written with pre-catalog vocabulary: ``container``/``docker``
    ceilings as strings or under the legacy ``docker`` alias key, ``full``
    where ``write`` is canonical, and per-grain values that predate the
    canonical level vocabulary.  This helper rewrites them to the canonical
    values the PUT validator (``validate_workspace_permissions``) and
    ``apply_workspace_ceiling`` understand.  The removed legacy ceiling
    grains (``git_read``/``git_write``/``system``/``execution``) never appear
    in the result:

    - container & docker (docker is the legacy alias of container and is
      emitted under the canonical ``container`` key, mirroring the PUT
      validator): real bool passes through; 'True'/'False' -> bool;
      'banned'|'read'|'ask' -> False; 'write'|'full' -> True; anything
      else is left untouched.  The container ceiling is NEVER stringified.
    - network: 'read' -> 'ask'
    - mcp: 'read'|'ask' -> 'banned'; 'write' -> 'full'
    - filesystem: 'full' -> 'write'
    - git: 'full' -> 'write'; 'write_feature_branches' ->
      'write_on_feature_branch'
    - git_read / git_write (removed ceiling grains): folded onto the
      single canonical ``git`` ceiling -- the folded level is the strongest
      (most permissive) across every present git/git_read/git_write entry,
      ranked on the workspace-ceiling scale (banned < read < ask <
      write_on_feature_branch < write; mirrors
      :data:`WORKSPACE_CEILING_LEVELS_RANKS`), so stored legacy intent
      survives (e.g. {git_read: read, git_write: ask} -> git: ask;
      {git_read: read, git_write: banned} -> git: read).  A git-grain value
      outside {banned, ask, read, write} is dropped.
    - system / execution (removed ceiling grains): always dropped --
      system inspection is unconditionally available and execution is not
      a user-configurable ceiling.
    - host_bash: kept iff the value is in {banned, ask, allow}, else left
      untouched
    - unknown keys and values: left untouched, EXCEPT the four removed
      legacy grains above which are always folded or dropped (the runtime
      gate treats unknown keys as fail-open with a WARN, so they degrade
      safely)

    Returns a NEW dict (the input is never mutated).  Idempotent: canonical
    input round-trips unchanged.  Logs a WARNING naming the resource, the
    old value and the new value on every actual rewrite/fold/drop.
    """

    def _git_rank(level: Any) -> Optional[float]:
        if isinstance(level, bool):
            return 3.0 if level else 0.0
        return _GIT_FOLD_RANKS.get(str(level).lower())

    def _fold_git_ceiling(level: Any) -> None:
        """Merge a canonical git ceiling level into *result* (strongest wins)."""
        rank = _git_rank(level)
        if rank is None:
            return
        current = result.get("git")
        current_rank = _git_rank(current) if current is not None else None
        if current is None or current_rank is None or rank > current_rank:
            result["git"] = level

    result: Dict[str, Any] = {}
    for k, v in raw.items():
        key = str(k)
        if key in ("git_read", "git_write"):
            # Removed ceiling grain: fold onto the single ``git`` ceiling.
            # Only canonical git ceiling levels fold; anything else
            # (legacy 'full', 'write_feature_branches', ...) could never
            # cap a session grant canonically, so the grain is dropped.
            if isinstance(v, str) and v in _GIT_FOLD_RANKS:
                _fold_git_ceiling(v)
                logger.warning(
                    "legacy workspace ceiling %r: %r -> folded onto "
                    "'git' (removed grain)", key, v,
                )
            else:
                logger.warning(
                    "legacy workspace ceiling %r: %r -> dropped "
                    "(removed grain; git ceiling governed by 'git')", key, v,
                )
            continue
        if key in ("system", "execution"):
            # Removed ceiling grain: always dropped -- system inspection is
            # unconditionally available and execution is not a
            # user-configurable ceiling.
            logger.warning(
                "legacy workspace ceiling %r: %r -> dropped "
                "(removed grain)", key, v,
            )
            continue
        out_key = key
        new_v = v
        if key in ("container", "docker"):
            # docker is the legacy alias of container; like the PUT
            # validator, emit the result under 'container' only so the
            # gate never sees a non-canonical docker key with a bool value.
            out_key = "container"
            if isinstance(v, bool):
                new_v = v
            elif isinstance(v, str):
                new_v = _LEGACY_CONTAINER_CEILING_BOOL.get(v, v)
            else:
                new_v = v
        elif key == "network" and v == "read":
            new_v = "ask"
        elif key == "mcp":
            if v in ("read", "ask"):
                new_v = "banned"
            elif v == "write":
                new_v = "full"
        elif key == "filesystem" and v == "full":
            new_v = "write"
        elif key == "git":
            if v == "full":
                new_v = "write"
            elif v == "write_feature_branches":
                new_v = "write_on_feature_branch"
            # A raw 'git' entry may follow (or precede) folded git grains;
            # merge so the strongest canonical git ceiling wins and the
            # result never carries a weaker overwrite.
            current = result.get("git")
            if current is not None:
                current_rank = _git_rank(current)
                new_rank = _git_rank(new_v)
                if current_rank is not None and (
                    new_rank is None or current_rank >= new_rank
                ):
                    continue  # existing (folded/raw) level is no weaker
        elif key == "host_bash":
            if v not in ("banned", "ask", "allow"):
                new_v = v
        if new_v is not v:
            logger.warning(
                "legacy workspace ceiling %r: %r -> %r", key, v, new_v,
            )
        result[out_key] = new_v
    return result
