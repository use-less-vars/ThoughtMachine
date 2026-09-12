"""
security_gate.py — Unified permission gate for the ThoughtMachine agent.

Replaces the ad-hoc ``_check_permissions`` / ``is_allowed`` flow with a single
entry point that merges **session permissions** (what the user chose for this
session) with **workspace capabilities** (what the workspace operator has
declared the workspace may do).

Design
------
1. ``get_effective_permissions()`` merges the two sources into one flat dict.
2. ``check_required_categories()`` compares a tool's declared requirements
   against that merged dict and, if needed, fires a ``SecurityPromptEvent``
   and waits for the user to approve or deny.

This module is always active — there is no fallback path.
"""

from __future__ import annotations

import dataclasses
import logging
import queue
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from thoughtmachine.workspace_capabilities import (
    WorkspaceCapabilities,
    load_workspace_capabilities,
)
from thoughtmachine.security import SessionPermissions, PERMISSION_SCHEMA, _pending_security_requests, _pending_requests_lock, resolve_security_prompt
from agent.config.defaults import PROMPT_TIMEOUT, RESOURCE_REGISTRY
from agent.events import SecurityPromptEvent, EventType, NullEventBus
from thoughtmachine.container_record.models import LIFECYCLE_CLASSES
from security.gate_helpers import _value_satisfies, resolve_network_mode
from security.resource_catalog import (
    RESOURCE_CATALOG,
    coerce_resource_permissions,
    GRANT_LEVEL_RANKS,
    WORKSPACE_CEILING_LEVELS_RANKS,
    WORKSPACE_CEILING_VOCAB,
)

logger = logging.getLogger(__name__)

# ── Pending-prompt registry ──────────────────────────────────────────────

# Uses shared _pending_security_requests from thoughtmachine.security




# ══════════════════════════════════════════════════════════════════════════
#  Loader
# ══════════════════════════════════════════════════════════════════════════


# ── Fail-closed capabilities constant ──
# When a workspace has no capabilities file (or it cannot be parsed), the
# loader returns None. Instead of falling back to a fully-permissive
# default, we return RESTRICTIVE capabilities: filesystem read-only, no
# docker, no network, no git. Session permissions can still be more
# restrictive; this constant only lowers the ceiling, never raises it.
_FAIL_CLOSED_CAPABILITIES = WorkspaceCapabilities(
    filesystem_write=False,
    allow_docker=False,
    allow_network=False,
    git_available=False,
)

# ── Fail-closed disk-mode constants ──────────────────────────────────────────────────────────────────────
# Used when ``get_effective_permissions()`` runs in disk mode (both
# ``session_id`` and ``workspace_id`` supplied, no in-memory
# ``workspace_permissions``) and the vault permission store cannot be read
# (missing/corrupt sidecar or config, I/O error, unexpected exception).  The
# deny-all session plus deny-all ceiling flow through the SAME merge below
# and yield the all-banned 6-key result shape — a disk-mode caller never
# receives default grants because the store was unreadable.
_DISK_FAIL_CLOSED_SESSION = SessionPermissions(
    container=False,
    network="banned",
    filesystem="banned",
    git="banned",
    mcp="banned",
    host_bash="banned",
)
_DISK_FAIL_CLOSED_CEILING: Dict[str, Any] = {
    "filesystem": "banned",
    "container": False,
    "host_bash": "banned",
    "git": "banned",
    "network": "banned",
}


def get_workspace_capabilities(workspace_id: str) -> WorkspaceCapabilities:
    """
    Load workspace capabilities via the canonical loader.

    Returns a fail-closed (restrictive) ``WorkspaceCapabilities`` when the
    file does not exist or cannot be parsed.
    """
    caps = load_workspace_capabilities(workspace_id)
    if caps is None:
        return _FAIL_CLOSED_CAPABILITIES
    return caps


# ══════════════════════════════════════════════════════════════════════════
#  Merge (session × workspace)
# ══════════════════════════════════════════════════════════════════════════


def _min_permission(
    effective_val: Any,
    override_val: Any,
) -> Any:
    """
    Combine two permission values, returning the more restrictive one.

    Used for two purposes:

    1. **Session × workspace** (existing callers):
       *override_val* is ``Optional[bool]``.
       * ``None`` → *effective_val* unchanged.
       * ``False`` → hard deny (``False``).
       * ``True`` → *effective_val* unchanged.

    2. **Effective × worker** (worker_permissions):
       *override_val* is a string level (``"banned"``, ``"read"``,
       ``"write"``, ``"full"``).  Compared by permission level;
       the lower (more restrictive) level wins.
    """
    if override_val is None:
        return effective_val
    if isinstance(override_val, bool):
        if not override_val:
            return False  # hard deny
        return effective_val  # True → passthrough

    # override_val is a string — compare permission levels on the shared
    # session-grant rank space (security/resource_catalog.py
    # GRANT_LEVEL_RANKS): write == write_on_feature_branch at the write tier
    # (3), outbound above them (3.5), full at 4.  A worker footprint
    # restricting git to read therefore caps a branch-write session level to
    # read; 'none' is a legacy preset alias ranking with read.
    _LEVEL_MAP: dict[str, float] = {
        level: float(rank) for level, rank in GRANT_LEVEL_RANKS.items()
    }
    _LEVEL_MAP["none"] = 1.0

    def _level(v: Any) -> float:
        if isinstance(v, bool):
            return 3.0 if v else 0.0
        if v is None:
            return 4.0
        return _LEVEL_MAP.get(str(v).lower(), 2.0)

    return override_val if _level(override_val) < _level(effective_val) else effective_val


# ── Workspace permission ceiling ──────────────────────────────────────────
# The workspace's saved permission map (vault ``workspaces/<id>/config.json``
# ``permissions`` key, with the purpose preset as fallback) declares the
# maximum each resource may be used at inside that workspace.  The ceiling
# ordering is the shared rank table in security/resource_catalog.py
# (WORKSPACE_CEILING_LEVELS_RANKS):
#     banned(0) < read(1) == connect(1) == none(1) < ask(2)
#         < write_on_feature_branch(2.5) < write(3)
#         < outbound(4) == full(4)
# Ceilings at rank 4.0 (``outbound``/``full``) are unlimited.  As a *ceiling*
# value ``write_on_feature_branch`` ranks between ask and write (2.5): it
# caps a session ``write`` grant down to branch-restricted write -- never
# unlimited.  Note this ordering is NOT the same as ``_min_permission``'s
# grant-level map (where ask < read); ceiling comparisons follow the
# workspace contract above.  Which ceiling levels may apply to which session
# key is additionally whitelisted by WORKSPACE_CEILING_VOCAB.
_WORKSPACE_CEILING_LEVELS = WORKSPACE_CEILING_LEVELS_RANKS

# Workspace permission-map resource names -> session-permissions keys they cap.
# The canonical workspace resource for sandboxed execution is ``container``
# (boolean ceiling); the legacy alias ``docker`` maps onto it here, so a
# docker ceiling of write-rank allows the container session grant while
# anything stricter (banned/read/ask) denies it.  Also accepts the
# session-catalog resources (filesystem, git, network, mcp -- capped on
# their own scales) plus the canonical ``host_bash`` key.
_WORKSPACE_RESOURCE_MAP: Dict[str, str] = {
    "filesystem": "filesystem",
    "docker": "container",
    "container": "container",
    "host_bash": "host_bash",
    "git": "git",
    "network": "network",
    "mcp": "mcp",
}

#: Recognised workspace-ceiling resource names: the canonical catalog
#: resources (session-grant keys git/filesystem/container/network/mcp/
#: host_bash -- see security/resource_catalog.py) plus the legacy alias
#: ``docker`` (kept recognised -- normalised onto ``container`` by
#: _WORKSPACE_RESOURCE_MAP so legacy disk ceilings never fail open).
#: A ceiling key outside this set is an unknown resource: it is logged and
#: ignored (fail-open), so forward-compatible workspace maps never break
#: session resolution.
_WORKSPACE_CEILING_KEYS = frozenset(RESOURCE_CATALOG) | {
    "docker",
}

#: Session-permission keys whose level scale has a ``read`` tier BELOW
#: ``ask``.  A workspace ceiling of ``ask`` caps a more-permissive session
#: grant to ``read`` for these keys; session scales without a read tier
#: (``network``) cap to ``banned``.  Either way an ask ceiling never
#: fabricates an effective ``ask`` grant -- interactive prompting stays
#: reserved for genuine session-level ``ask``.
_ASK_CEILING_READ_TIER_KEYS = frozenset({
    "filesystem",
    "git",
})


def apply_workspace_ceiling(
    workspace_permissions: Dict[str, Any],
    session_permissions: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Cap session permission levels at the workspace permission ceiling.

    Returns a NEW dict; ``session_permissions`` is not mutated.  For every
    resource present in ``workspace_permissions`` the result is the more
    restrictive of the workspace ceiling and the session value, following the
    workspace ceiling ordering::

        banned(0) < read(1) == connect(1) == none(1) < ask(2)
            < write_on_feature_branch(2.5) < write(3)
            < outbound(4) == full(4)

    Rules:
        * A resource missing from ``workspace_permissions`` has no ceiling —
          the session value stands.
        * A ``write_on_feature_branch`` ceiling (rank 2.5) caps a session
          ``write`` grant down to branch-restricted write — never unlimited.
          Ceilings at rank 4.0 (``outbound``/``full``) are unlimited — the
          session value stands.
        * An unknown ceiling level is treated as no ceiling (fail-open), so
          forward-compatible workspace maps never break session resolution.
        * An unknown ceiling resource (outside ``RESOURCE_CATALOG`` and the
          legacy workspace alias ``docker``) is logged and ignored
          (fail-open) -- the session value stands.
        * ``docker`` is a legacy alias for ``container``: it is normalised
          onto ``container`` by ``_WORKSPACE_RESOURCE_MAP`` before ranking,
          so a docker ceiling of ``write`` (or ``full``/``True``) allows the
          container session grant, while ``banned``/``read``/``ask`` (or
          ``False``) deny it.
        * Boolean session values (``container``) survive only when the
          ceiling is write-level or unlimited; any stricter ceiling forces
          ``False``.  The ``container`` key is always emitted as a boolean.
        * A workspace ceiling of ``ask`` caps a more-permissive session
          value to the most permissive tier BELOW ask: ``read`` for
          resources whose session scale has a read tier (filesystem, git),
          else ``banned`` (network).  An ask ceiling
          therefore NEVER yields an effective ``ask`` grant -- interactive
          prompting stays reserved for genuine session-level ``ask``
          grants, which rank at the ceiling and pass through unchanged.
        * ``host_bash`` is capped on its own scale -- ``banned < ask <
          allow`` -- so a workspace ceiling of ``banned``/``ask``/``allow``
          (or a boolean, ``True`` ~ ``allow``, ``False`` ~ ``banned``) caps
          the session value accordingly.  The ``host_bash`` key is always
          emitted as one of ``banned``/``ask``/``allow``.
        * ``mcp`` is capped on its own session scale -- ``banned < connect
          < full`` (``connect`` ranks 1.0, between ``read`` and ``ask``) --
          so a workspace ceiling of ``banned``/``connect``/``full`` caps a
          more permissive session mcp grant to the ceiling level.
        * Unknown session keys are passed through untouched.
    """
    if not workspace_permissions:
        return dict(session_permissions)

    result = dict(session_permissions)
    for resource, ceiling in workspace_permissions.items():
        if resource not in _WORKSPACE_CEILING_KEYS:
            logger.warning(
                "ignoring workspace ceiling for unknown resource %r "
                "(not a catalog resource or legacy workspace grain); "
                "fail-open: no ceiling applied",
                resource,
            )
            continue
        key = _WORKSPACE_RESOURCE_MAP.get(resource)
        if key is None or key not in result:
            continue  # unknown resource / not a session key: no ceiling
        session_val = result[key]

        # host_bash ranks on its own scale -- banned < ask < allow -- which
        # is NOT part of _WORKSPACE_CEILING_LEVELS ('allow' is not a generic
        # ceiling level there, and 'ask' means something else).  It must be
        # intercepted before the generic rank normalisation below, or a
        # host_bash 'allow' ceiling would hit the unknown-level fail-open
        # path and never cap the session value.  Legacy ceiling values like
        # 'read'/'write' are not part of the host_bash vocabulary and are
        # treated as unknown (warn + fail-open).
        if key == "host_bash":
            _HOST_BASH_RANKS = {"banned": 0.0, "ask": 1.0, "allow": 2.0}
            if isinstance(ceiling, bool):
                # Boolean host_bash ceiling: True ~ allow-level (2.0),
                # False ~ banned (0.0, caps any session grant to banned).
                ceiling_rank = 2.0 if ceiling else 0.0
            elif isinstance(ceiling, str):
                ceiling_rank = _HOST_BASH_RANKS.get(ceiling.lower())
            else:
                ceiling_rank = None
            if ceiling_rank is None:
                logger.warning(
                    "ignoring workspace ceiling level %r for host_bash "
                    "(not a host_bash level: banned/ask/allow); "
                    "fail-open: no ceiling applied",
                    ceiling,
                )
                continue
            if isinstance(session_val, bool):
                # Boolean session host_bash: True ~ allow (2.0).
                session_rank = 2.0 if session_val else 0.0
            else:
                session_rank = _HOST_BASH_RANKS.get(
                    str(session_val).lower()
                )
            if session_rank is None:
                continue  # unknown session value: leave untouched
            if ceiling_rank < session_rank:
                # Only ever emit host_bash vocabulary values; a boolean
                # ceiling caps to 'banned' (never a bare bool/"true").
                # host_bash has no tier between 'banned' and 'ask', so any
                # ceiling-fabricated cap falls to the below-ask tier:
                # 'banned' -- an 'ask' ceiling over an 'allow' grant must
                # never emit 'ask' (prompts come only from genuine
                # session-level 'ask' grants, which pass through at equal
                # rank in the branch above).
                result[key] = "banned"
            continue

        # Normalise the ceiling to a rank; unknown ceilings are fail-open.
        if isinstance(ceiling, bool):
            ceiling_rank = 3.0 if ceiling else 0.0
        elif isinstance(ceiling, str):
            ceiling_lower = ceiling.lower()
            # Per-key vocab unification: only levels on this session key's
            # own scale may cap it (e.g. an mcp 'read' ceiling, a docker
            # 'write_on_feature_branch' ceiling or the legacy
            # 'write_feature_branches' marker are NOT valid for these keys)
            # -- anything else is ignored fail-open so the session value
            # stands.
            ceiling_vocab = WORKSPACE_CEILING_VOCAB.get(key)
            if ceiling_vocab is not None and ceiling_lower not in ceiling_vocab:
                logger.warning(
                    "ignoring workspace ceiling level %r for resource %r "
                    "(not a valid ceiling level for %s; allowed: %s); "
                    "fail-open: no ceiling applied",
                    ceiling,
                    resource,
                    key,
                    sorted(ceiling_vocab),
                )
                continue
            ceiling_rank = _WORKSPACE_CEILING_LEVELS.get(ceiling_lower)
        else:
            ceiling_rank = None
        if ceiling_rank is None:
            logger.warning(
                "ignoring workspace ceiling level %r for resource %r "
                "(not a known ceiling level); fail-open: no ceiling applied",
                ceiling,
                resource,
            )
            continue
        if ceiling_rank >= 4.0:
            continue  # outbound/full ceilings -> unlimited

        # Container is a boolean in SessionPermissions; always emit a bool.
        if key == "container":
            if isinstance(session_val, bool):
                result[key] = session_val and ceiling_rank >= 3.0
            else:
                session_rank = _WORKSPACE_CEILING_LEVELS.get(str(session_val).lower())
                session_rank = session_rank if session_rank is not None else 0.0
                result[key] = min(session_rank, ceiling_rank) >= 3.0
            continue

        if isinstance(session_val, bool):
            # Non-container boolean — hard allow survives only write-level
            # ceilings.
            if session_val and ceiling_rank < 3.0:
                result[key] = False
            continue

        # String session values: the more restrictive (lower) rank wins.
        session_rank = _WORKSPACE_CEILING_LEVELS.get(str(session_val).lower())
        if session_rank is None:
            continue
        if ceiling_rank < session_rank:
            # A workspace 'ask' ceiling (rank 2.0) must never fabricate an
            # effective 'ask' grant: cap to the most permissive tier BELOW
            # ask -- 'read' where the session scale has one, else 'banned'.
            # Genuine session-level 'ask' grants rank equal to the ceiling
            # and pass through unchanged above (prompt flow preserved).
            if ceiling_rank == 2.0:
                result[key] = (
                    "read" if key in _ASK_CEILING_READ_TIER_KEYS else "banned"
                )
            else:
                result[key] = (
                    ceiling
                    if isinstance(ceiling, bool)
                    else str(ceiling).lower()
                )
    return result


def split_git_permission(level: Any) -> tuple:
    """Derive legacy ``(read, write)`` git sub-levels from a merged ``git`` level.

    Internal helper (kept for direct callers/tests).  The canonical
    effective-permission dict exposes a single ``git`` value --
    ``git_read``/``git_write`` are no longer emitted as effective keys -- but
    the legacy two-grain view is still useful where a read-only git path must
    run on a ``read`` session while the write path stays denied:

    ===================  ===================  ====================
    merged ``git``       read sub-level       write sub-level
    ===================  ===================  ====================
    ``False``/``None``   ``False``            ``False``
    ``banned``           ``banned``           ``banned``
    ``ask``              ``ask``              ``ask``
    ``read``             ``read``             ``banned``
    ``write``            ``write``            ``write``
    ``full``             ``full``             ``full``
    ===================  ===================  ====================

    ``write_on_feature_branch`` (a session ``git`` level, branch-restricted
    writes) splits into read ``read`` + write ``write_on_feature_branch``:
    reads are allowed on any branch, while the write side keeps the verbatim
    branch-aware level so a branch-aware consumer can admit feature-branch
    commits and deny plain ``git:write`` requests.

    ``ask`` maps to ``ask`` on both sub-levels so the interactive prompt
    flow for a write request is preserved (an ``ask`` write is prompted
    exactly as before).
    """
    if level is False or level is None:
        return (False, False)
    s = str(level).lower()
    if s in ("banned", "ask"):
        return (s, s)
    if s == "read":
        return ("read", "banned")
    if s in ("write", "full"):
        return (s, s)
    if s == "write_on_feature_branch":
        # Feature-branch write grant: reads are allowed everywhere; the
        # write grain keeps the verbatim level so the branch-aware git
        # write tool (Phase 3) can allow feature-branch commits while the
        # gate denies plain git:write requests (fail closed until then).
        return ("read", "write_on_feature_branch")
    # Unknown level: fail closed on both sub-levels.
    return (False, False)


class _CeilingAnnotatedDict(dict):
    """Plain ``dict`` carrying workspace-ceiling provenance annotations.

    Identical to a normal dict for equality, iteration, indexing, ``len``,
    membership and JSON serialisation; it additionally carries a
    ``_ceiling_annotations`` attribute mapping each effective-permission
    category the workspace ceiling actually restricted to
    ``{"pre": <pre-ceiling value>, "level": <ceiling level label>}``.
    The attribute survives in-place mutation (the worker-footprint merge in
    ``check_required_categories``) and injection into tool arguments, so a
    denial can be attributed to the workspace ceiling without any signature
    changes anywhere.
    """

    __slots__ = ("_ceiling_annotations",)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._ceiling_annotations: Dict[str, Dict[str, Any]] = {}


def _ceiling_level_label(value: Any) -> str:
    """Render a raw workspace ceiling value as a stable message label."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).lower()


def _merge_with_capabilities(
    session: SessionPermissions, workspace: WorkspaceCapabilities
) -> Dict[str, Any]:
    """Merge a session profile with workspace capabilities (no ceiling).

    This is the capability-merge tail of :func:`get_effective_permissions`:
    the filesystem_write / allow_network / allow_docker / git_available caps
    applied on top of a session profile.  Kept as a helper so the same merge
    can be run over the pre-ceiling and post-ceiling session profiles; the
    diff between the two results isolates the categories the workspace
    ceiling actually restricted.
    """

    # ── Filesystem ──────────────────────────────────────────────────────
    # Workspace filesystem_write caps write access; if False, downgrade to read.
    fs = session.filesystem
    if not workspace.filesystem_write and fs == "write":
        fs = "read"  # downgrade write → read
    # (read / none / banned / ask pass through unchanged)

    # ── Network ─────────────────────────────────────────────────────────
    net: Any = _min_permission(session.network, workspace.allow_network)

    # ── Container ───────────────────────────────────────────────────────
    container: Any = session.container and workspace.allow_docker

    # ── Git ─────────────────────────────────────────────────────────────
    # The single ``git`` level is capped by the workspace capability.
    git: Any = _min_permission(session.git, workspace.git_available)

    return {
        "filesystem": fs,
        "network": net,
        "container": container,
        "git": git,
        "mcp": session.mcp,
        "host_bash": session.host_bash,
    }


def _annotate_ceiling_changes(
    base_eff: Dict[str, Any],
    capped_eff: Dict[str, Any],
    workspace_permissions: Dict[str, Any],
) -> Optional["_CeilingAnnotatedDict"]:
    """Return an annotated copy of *capped_eff*, or ``None`` if no category
    changed (or none is attributable to a ceiling resource).

    Every category whose merged value differs between the pre-ceiling
    (*base_eff*) and post-ceiling (*capped_eff*) capability merges was
    restricted by the workspace ceiling — the merge is identical for both
    profiles, so capability-driven caps (filesystem_write / allow_docker /
    allow_network / git_available) affect both equally and never show up in
    the diff.  Each annotation records the pre-ceiling value (``pre``) and
    the responsible ceiling's level label.
    """

    changed = [key for key in base_eff if base_eff[key] != capped_eff.get(key)]
    if not changed:
        return None

    # Raw ceiling level per session key it caps (later entries win, matching
    # apply_workspace_ceiling's iteration order).
    level_by_session_key: Dict[str, str] = {}
    for resource, value in workspace_permissions.items():
        session_key = _WORKSPACE_RESOURCE_MAP.get(resource)
        if session_key is None:
            continue  # unknown resource: apply_workspace_ceiling ignores it
        level_by_session_key[session_key] = _ceiling_level_label(value)

    annotated = _CeilingAnnotatedDict(capped_eff)
    for key in changed:
        label = level_by_session_key.get(key)
        if label is None:
            continue  # no attributable ceiling: leave unannotated
        annotated._ceiling_annotations[key] = {
            "pre": base_eff[key],
            "level": label,
        }
    if not annotated._ceiling_annotations:
        return None
    return annotated


def _ceiling_denial_note(
    category: str,
    required_value: str,
    effective: Dict[str, Any],
    permission_footprint: Optional[Dict[str, Any]] = None,
) -> str:
    """Return the ceiling level label when *this* denial is caused by the
    workspace ceiling, else an empty string.

    The denial is attributed to the workspace ceiling only when the
    pre-ceiling session value (annotation ``pre``, computed without the
    worker footprint) satisfies *required_value* AND the worker footprint
    (when present) does not independently deny it — i.e. removing the
    ceiling would flip the denial into an allow.  In every other case the
    caller returns its current message byte-identical.
    """
    annotations = getattr(effective, "_ceiling_annotations", None)
    if not annotations:
        return ""
    annotation = annotations.get(category)
    if annotation is None:
        return ""
    if _value_satisfies(required_value, annotation["pre"]) is not True:
        # The session grant alone was already insufficient — the denial is
        # not caused by the workspace ceiling.
        return ""
    if permission_footprint:
        footprint_value = permission_footprint.get(category)
        if (
            footprint_value is not None
            and _value_satisfies(required_value, footprint_value) is not True
        ):
            # The worker footprint independently denies the request.
            return ""
    return str(annotation["level"])


# ── Session-exceeds-workspace-ceiling contradiction ───────────────────────────────────
# A first-class classification for the case where the SESSION grant sits
# strictly ABOVE the workspace ceiling for a resource: the ceiling (not the
# session grant) is what denies the call, yet the session profile itself is
# the inconsistency -- it was granted more than the workspace permits, so one
# of the two sides must be corrected.  The human denial text and the REST/GUI
# contract both derive from this value rather than a string match on the
# message, so the reason is machine-readable.
REASON_SESSION_EXCEEDS_WORKSPACE_CEILING = "session_exceeds_workspace_ceiling"

#: Operator-facing corrective guidance emitted with every
#: :data:`REASON_SESSION_EXCEEDS_WORKSPACE_CEILING` contradiction.
SESSION_EXCEEDS_WORKSPACE_CEILING_GUIDANCE = (
    "Correct either the workspace ceiling or the session grant."
)

#: ``host_bash`` ranks on its own scale -- ``banned < ask < allow`` -- which is
#: NOT the shared ``WORKSPACE_CEILING_LEVELS_RANKS`` table (``allow`` is absent
#: there and ``ask`` means something else).  Mirrors the private map used
#: inside :func:`apply_workspace_ceiling` so the strict-above test agrees with
#: the capping the gate actually performed.
_HOST_BASH_CEILING_RANKS: Dict[str, float] = {
    "banned": 0.0,
    "ask": 1.0,
    "allow": 2.0,
}


def _ceiling_scale_rank(session_key: str, value: Any) -> Optional[float]:
    """Rank a session *or* ceiling *value* on *session_key*'s ceiling scale.

    Uses the SAME rank table the ceiling machinery uses
    (``WORKSPACE_CEILING_LEVELS_RANKS`` for the generic scale; the private
    ``banned < ask < allow`` scale for ``host_bash``; the boolean scale for
    ``container``) so a "session is strictly above the ceiling" test agrees
    with :func:`apply_workspace_ceiling`.  Returns ``None`` when the value is
    not on the key's scale (rank undecidable).
    """
    if session_key == "host_bash":
        if isinstance(value, bool):
            return 2.0 if value else 0.0
        label = str(value).lower()
        if label == "true":
            return 2.0
        if label == "false":
            return 0.0
        return _HOST_BASH_CEILING_RANKS.get(label)
    if isinstance(value, bool):
        return 3.0 if value else 0.0
    label = str(value).lower()
    if label == "true":
        return 3.0
    if label == "false":
        return 0.0
    return _WORKSPACE_CEILING_LEVELS.get(label)


def _ceiling_contradiction_for(
    session_key: str, annotation: Optional[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """Classify one ceiling annotation as a session-exceeds-ceiling case.

    Returns the structured reason dict when the annotation's pre-ceiling
    SESSION value ranks strictly ABOVE the annotation's ceiling level, else
    ``None`` (equal / below / undecidable ranks are not a contradiction).
    """
    if not annotation:
        return None
    pre = annotation.get("pre")
    level = annotation.get("level")
    session_rank = _ceiling_scale_rank(session_key, pre)
    ceiling_rank = _ceiling_scale_rank(session_key, level)
    if session_rank is None or ceiling_rank is None:
        return None
    if session_rank <= ceiling_rank:
        return None
    return {
        "resource": session_key,
        "session_value": pre,
        "workspace_value": level,
        "reason": REASON_SESSION_EXCEEDS_WORKSPACE_CEILING,
        "guidance": SESSION_EXCEEDS_WORKSPACE_CEILING_GUIDANCE,
    }


def _ceiling_annotations_of(effective: Dict[str, Any]) -> Dict[str, Any]:
    """Return the ceiling-annotation map carried by *effective*, or ``{}``."""
    return getattr(effective, "_ceiling_annotations", None) or {}


def ceiling_levels(effective: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``{resource: ceiling_level}`` for every resource the workspace
    ceiling actually restricted (derived from the gate's ceiling provenance),
    or ``{}`` when no ceiling applied.

    Read-only: this only reads ``_ceiling_annotations``; it never changes how
    the gate writes or interprets them.
    """
    return {
        key: annotation.get("level")
        for key, annotation in _ceiling_annotations_of(effective).items()
        if isinstance(annotation, dict)
    }


def ceiling_contradictions(effective: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return one structured reason dict per resource whose session grant sits
    strictly above its workspace ceiling (see
    :data:`REASON_SESSION_EXCEEDS_WORKSPACE_CEILING`).

    Each entry carries ``resource``, ``session_value`` (the pre-ceiling SESSION
    grant), ``workspace_value`` (the ceiling level), ``reason`` (the canonical
    code) and ``guidance`` (operator-facing corrective text).  Dependency-free
    and first-class -- it reads the gate's ceiling provenance, not the human
    message.  Empty list when there is no contradiction.
    """
    result: List[Dict[str, Any]] = []
    for session_key, annotation in _ceiling_annotations_of(effective).items():
        contradiction = _ceiling_contradiction_for(session_key, annotation)
        if contradiction is not None:
            result.append(contradiction)
    return result


def _session_exceeds_ceiling_message(
    session_key: str, effective: Dict[str, Any]
) -> str:
    """Return the operator-approved contradiction sentence for *session_key*
    when its SESSION grant sits strictly above the workspace ceiling, else ``""``.

    The sentence names the SESSION's pre-ceiling value and the CEILING value --
    never the capped/effective value -- so the denial no longer mislabels the
    post-cap value as the session's own grant.
    """
    annotation = _ceiling_annotations_of(effective).get(session_key)
    contradiction = _ceiling_contradiction_for(session_key, annotation)
    if contradiction is None:
        return ""
    return (
        f"Session permission for {contradiction['resource']} is "
        f"{contradiction['session_value']}, but workspace ceiling is "
        f"{contradiction['workspace_value']}. The session exceeds the ceiling. "
        f"{contradiction['guidance']}"
    )


def get_effective_permissions(
    session: SessionPermissions,
    workspace: WorkspaceCapabilities,
    workspace_permissions: Optional[Dict[str, Any]] = None,
    *,
    session_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Merge the session's permission profile with the workspace's capabilities.

    Returns a flat dict with the six canonical permission category keys::

        {"filesystem": ..., "network": ..., "container": ...,
         "git": ..., "mcp": ..., "host_bash": ...}

    ``host_bash`` is capped by the workspace ceiling inside
    :func:`apply_workspace_ceiling` (its own ``banned < ask < allow``
    scale) and is otherwise the session value directly.

    Each value is either a boolean (``True`` / ``False``) for hard allow/deny,
    a string level (``"write"``, ``"read"``, ``"none"``, ``"banned"``, ``"ask"``, ``"outbound"``),
    or ``False`` if the workspace forbids the operation.  ``git`` is merged
    with ``workspace.git_available`` via ``_min_permission``.

    Args:
        workspace_permissions:
            Optional workspace-level permission ceilings — a dict mapping
            workspace resource names (``filesystem``, ``container``,
            ``host_bash``, ``git``, ``network``, ``mcp``) to their maximum
            allowed level (e.g.
            ``{"filesystem": "read", "container": False}``).  ``container``
            is the canonical boolean ceiling; the legacy alias ``docker`` is
            also accepted and normalised onto ``container`` (write-level
            allows the container session grant, anything stricter denies it).
            Applied to the session profile BEFORE the workspace-capability
            merge via :func:`apply_workspace_ceiling`, so a session can never
            exceed the workspace's declared ceiling for this workspace.
        session_id / workspace_id:
            Keyword-only arguments enabling **disk mode**.  When BOTH are
            supplied AND *workspace_permissions* is None, the session grant
            profile and the workspace permission ceiling are loaded from the
            vault permission store (``thoughtmachine.permission_store``)
            under ``<vault>/workspaces/<workspace_id>/sessions/<session_id>``
            and ``<vault>/workspaces/<workspace_id>/config.json``
            respectively, instead of being taken from the *session* and
            *workspace_permissions* arguments.  The stored grants replace the
            *session* argument; the stored ceiling replaces
            *workspace_permissions*; the *workspace* capabilities argument
            is still merged below.

    Precedence rule:
        An explicit in-memory ``workspace_permissions`` dict always wins.
        Disk mode engages ONLY when both ``session_id`` and ``workspace_id``
        are supplied AND ``workspace_permissions`` is None; supplying just
        one of the ids keeps the legacy behaviour unchanged.

    Fail-closed contract:
        Disk mode never fabricates permissive defaults.  If the store is
        missing, corrupt, or raises for any reason, the session resolves to
        a deny-all profile (every category ``banned`` / ``False``) and the
        ceiling to a deny-all ceiling, so the merged result is the
        all-denied shape rather than an accidental grant.

    Absent-grant rule:
        ``SessionPermissions`` carries safe pydantic defaults (filesystem
        ``read``, git ``read``; network / mcp / host_bash ``banned``;
        container ``False``).  A grant set that is missing a
        key resolves to that key's default -- never an accidental denial of
        a default.  Ceilings and workspace capabilities only ever lower
        those values further; a ceiling-only passthrough that would lift a
        default-banned key (e.g. ``network``) into a grant would be a
        regression, which is why the full default-filled profile is kept.
    """
    # ── Disk-mode dispatch ──────────────────────────────────────────────────────────────────────────────────────────────────
    # Both ids supplied and no explicit in-memory ceiling: the grant profile
    # and the ceiling come from the vault permission store.  Imports are
    # lazy so the legacy in-memory path never depends on permission_store /
    # vault.  Any store error fails CLOSED (deny-all session + deny-all
    # ceiling); the merged result below can then only be restrictive.
    if workspace_permissions is None and session_id is not None and workspace_id is not None:
        import thoughtmachine.vault as _vault_module
        from thoughtmachine.permission_store import (
            read_session_permissions,
            workspace_ceiling,
        )

        try:
            _vault_root = _vault_module.vault_root()
            disk_grants = read_session_permissions(_vault_root, workspace_id, session_id)
            disk_ceiling = workspace_ceiling(_vault_root, workspace_id)
        except Exception:
            session = _DISK_FAIL_CLOSED_SESSION
            workspace_permissions = _DISK_FAIL_CLOSED_CEILING
        else:
            try:
                # Belt-and-braces: read_session_permissions already returns
                # catalog-clean grants (the store coerces on every read and
                # write), but re-coercing at the gate boundary keeps a
                # non-catalog key from ever reaching SessionPermissions.
                # coerce_resource_permissions never raises; the except below
                # still guards the constructor.
                session = SessionPermissions(
                    **coerce_resource_permissions(disk_grants)
                )
            except Exception:
                # Unreadable grant record -> deny-all session; the disk
                # ceiling still applies on top of it.
                session = _DISK_FAIL_CLOSED_SESSION
            workspace_permissions = disk_ceiling

    # ── Workspace permission ceiling ────────────────────────────────────
    # The workspace's permission map is a hard ceiling on the session's
    # permission levels; apply it to the raw session profile first.  The
    # workspace-capability merge below can then only make the result more
    # restrictive, never raise the session back above the ceiling.
    # ``base_session`` is kept so the ceiling's effect can be isolated: the
    # same capability merge over the pre-ceiling profile reveals exactly
    # which categories the workspace ceiling restricted (see
    # _annotate_ceiling_changes).
    base_session = session
    if workspace_permissions:
        raw = session.model_dump() if hasattr(session, "model_dump") else dict(session.__dict__)
        capped = apply_workspace_ceiling(workspace_permissions, raw)
        try:
            session = SessionPermissions(**capped)
        except Exception:
            pass  # keep the original session if the capped profile is not schema-valid

    # ── Capability merge over the (possibly ceiling-capped) session ───────
    effective = _merge_with_capabilities(session, workspace)

    # When the workspace ceiling actually lowered the session profile, run
    # the SAME capability merge over the pre-ceiling profile: the diff then
    # isolates exactly which categories the ceiling restricted (capability
    # caps — filesystem_write / allow_docker / allow_network / git_available
    # — hit both merges equally and are never attributed to the ceiling).
    # The annotated result is a plain-dict subclass whose content is
    # identical; when nothing changed (or there is no ceiling) the plain
    # dict is returned, so the common path is byte-for-byte unchanged.
    result: Dict[str, Any] = effective
    if workspace_permissions and session is not base_session:
        base_eff = _merge_with_capabilities(base_session, workspace)
        annotated = _annotate_ceiling_changes(
            base_eff, effective, workspace_permissions
        )
        if annotated is not None:
            result = annotated

    # ── Workspace host-resource ceiling (allow_host_resources) ───────────
    # The workspace operator's top-level ``allow_host_resources`` policy is a
    # hard, absolute gate on the ``host_bash`` resource: a missing key, an
    # absent / null / false value, or a missing/unreadable workspace config
    # denies host execution regardless of what the session grant or worker
    # footprint says, while ANY truthy value enables it -- the reader returns
    # ``bool(data.get("allow_host_resources", False))``, so e.g. the string
    # "yes" or the number 1 also enable host resources.  See
    # tools.host_resource_policy.workspace_allows_host_resources, the single
    # source of truth for the exact type semantics.  It is enforced HERE, in
    # the resolution layer, so every consumer of the effective dict sees the
    # denial -- the in-tool check in tools/host_bash_tool.py is skipped
    # whenever no workspace id is attached, and resolving it here closes that
    # gap without duplicating policy.  The reader is fail-closed; any reader
    # error therefore denies.  The override
    # is applied AFTER the ceiling annotation so a host_bash ban is never
    # misattributed to the workspace *permissions* ceiling.
    try:
        from tools.host_resource_policy import workspace_allows_host_resources

        _workspace_allows_host_resources = workspace_allows_host_resources(
            workspace_id
        )
    except Exception:
        _workspace_allows_host_resources = False
    if not _workspace_allows_host_resources:
        result["host_bash"] = "banned"

    return result


# ══════════════════════════════════════════════════════════════════════════
#  Container config resolver
# ══════════════════════════════════════════════════════════════════════════


@dataclasses.dataclass(frozen=True)
class ContainerConfig:
    """Resolved, immutable container configuration for a single container.

    Produced by :func:`resolve_container_config`. All fields are derived purely
    from the supplied permissions + capabilities; nothing here performs IO.
    """

    network_mode: str
    workspace_mode: str
    effective: dict
    lifecycle_class: str


@dataclasses.dataclass(frozen=True)
class ContainerConfigError:
    """Fail-closed error result for :func:`resolve_container_config`.

    A plain frozen dataclass — it deliberately implements *no* mapping
    protocol (no ``__getitem__``/``__contains__``/``get``) so that callers
    cannot accidentally treat an error as a successful config.
    """

    code: str
    message: str = ""


def resolve_container_config(
    permissions: Any,
    capabilities: Optional[WorkspaceCapabilities],
    lifecycle_class: str,
) -> ContainerConfig | ContainerConfigError:
    """Resolve the container configuration from permissions + capabilities.

    This is a **pure**, **total** and **fail-closed** replacement for the old
    ``get_expected_container_config`` helper:

    * **Pure** — no filesystem, vault, environment or network IO. Everything is
      derived from the three arguments supplied by the caller.
    * **Total** — it never raises; every input maps to exactly one
      :class:`ContainerConfig` or :class:`ContainerConfigError` value.
    * **Fail-closed** — any ambiguity (unknown lifecycle class, missing
      capabilities, malformed permissions) yields a
      :class:`ContainerConfigError` rather than a permissive default. In
      particular ``capabilities`` is *required* and is **never** silently
      replaced by a permissive ``WorkspaceCapabilities()`` default.

    Args:
        permissions: Either an existing ``SessionPermissions`` instance or a
            mapping of ``SessionPermissions`` field values (e.g. ``"network"``,
            ``"filesystem"``, ``"container"``).
        capabilities: A ``WorkspaceCapabilities`` instance. ``None`` is rejected
            with ``ContainerConfigError("capabilities_required")``.
        lifecycle_class: One of :data:`LIFECYCLE_CLASSES`.

    Returns:
        A :class:`ContainerConfig` on success, otherwise a
        :class:`ContainerConfigError`.
    """
    # 1. Validate lifecycle class first — cheap, deterministic, no IO.
    if not isinstance(lifecycle_class, str) or lifecycle_class not in LIFECYCLE_CLASSES:
        return ContainerConfigError(
            "unknown_lifecycle_class",
            f"lifecycle_class must be one of {LIFECYCLE_CLASSES}, got {lifecycle_class!r}",
        )

    # 2. Capabilities are required — reject rather than defaulting permissively.
    if capabilities is None:
        return ContainerConfigError(
            "capabilities_required",
            "capabilities must not be None; callers must supply WorkspaceCapabilities",
        )

    try:
        # 3. Normalise permissions into a SessionPermissions instance.
        if isinstance(permissions, SessionPermissions):
            session = permissions
        elif isinstance(permissions, Mapping):
            try:
                session = SessionPermissions(**permissions)
            except Exception as exc:
                return ContainerConfigError("bad_permissions", str(exc))
        else:
            return ContainerConfigError(
                "bad_permissions",
                "permissions must be a Mapping or SessionPermissions, "
                f"got {type(permissions).__name__}",
            )

        # 4. Merge permissions with capabilities.
        eff = get_effective_permissions(session, capabilities)

        # 5. Network mode.
        network_mode = resolve_network_mode(eff.get("network"))

        # 6. Workspace mount mode.
        fs = eff.get("filesystem", "read")
        workspace_mode = "rw" if fs in ("write", "full") else "ro"

        # 7. Success.
        return ContainerConfig(
            network_mode=network_mode,
            workspace_mode=workspace_mode,
            effective=dict(eff),
            lifecycle_class=lifecycle_class,
        )
    except Exception as exc:  # 8. Any unexpected failure is fail-closed.
        return ContainerConfigError("resolution_failed", str(exc))


# ══════════════════════════════════════════════════════════════════════════
#  Internal helpers
# ══════════════════════════════════════════════════════════════════════════


# _value_satisfies lives in security/gate_helpers.py (an import-free leaf
# module) so that neither security_gate nor its transitive imports can ever
# hit a partially-initialized-module ImportError.  It is imported at module
# level above and re-exported here, so ``security_gate._value_satisfies``
# remains a valid attribute (compat alias).


def check_atomic_operation(
    operation_required: str,
    effective_permissions: dict,
    tool_name: str,
    description: str = "",
) -> bool:
    """
    Synchronous in-tool re-check of an operation.
    Returns True if allowed, False if denied.
    If the permission level is 'ask', treats it as DENIED (escalation not pre-approved).
    """
    parts = operation_required.split(":", 1)
    if len(parts) != 2:
        logger.warning(
            "check_atomic_operation: malformed required '%s' for %s",
            operation_required, tool_name
        )
        return False

    category, required_value = parts
    allowed = effective_permissions.get(category)
    if allowed is None:
        logger.warning(
            "check_atomic_operation: unknown category '%s' for %s",
            category, tool_name
        )
        return False

    result = _value_satisfies(required_value, allowed)
    if result == "ASK":
        logger.warning(
            "check_atomic_operation: '%s' for %s would require user prompt (ASK) — denying by policy",
            operation_required, tool_name
        )
        return False
    return bool(result)


def check_requires_resource(
    requires_resource: str,
    effective_permissions: dict,
    tool_name: str,
    description: str = "",
) -> tuple:
    """Check that the session is allowed to use the resource a tool is bound to.

    Tools bound to a hidden resource (``requires_resource`` set, e.g. git)
    may only execute when the effective session permissions grant the
    resource's read grain (``RESOURCE_REGISTRY[resource]["permission"]:read``).
    Unknown resources fail closed.

    Returns:
        (bool, str): (True, "") when allowed; (False, denial message) otherwise.
    """
    entry = RESOURCE_REGISTRY.get(requires_resource)
    if entry is None:
        logger.warning(
            "check_requires_resource: unknown resource '%s' required by %s",
            requires_resource, tool_name,
        )
        return (
            False,
            f"Permission denied: unknown resource '{requires_resource}' required by tool '{tool_name}'.",
        )
    permission = entry["permission"]
    allowed = check_atomic_operation(
        f"{permission}:read",
        effective_permissions,
        tool_name,
        description=description,
    )
    if not allowed:
        ceiling_level = _ceiling_denial_note(permission, "read", effective_permissions)
        contradiction = (
            _session_exceeds_ceiling_message(permission, effective_permissions)
            if ceiling_level
            else ""
        )
        if contradiction:
            # The session grant sits ABOVE the workspace ceiling: the ceiling
            # (not the capped session value) denies the call, and the session
            # profile itself is the inconsistency -- name both sides truthfully.
            return (False, f"Permission denied: {contradiction}")
        message = (
            f"Permission denied: Tool requires resource '{requires_resource}' "
            f"(permission {permission}:read), but session does not allow it."
        )
        if ceiling_level:
            # The workspace permission ceiling is what blocks this call (the
            # session grant alone would allow it) — name it so the denial
            # explains why a permissive-looking session profile still refuses.
            message += f" (workspace ceiling: {ceiling_level})"
        return (False, message)
    return (True, "")


# ══════════════════════════════════════════════════════════════════════════
#  Main gate
# ══════════════════════════════════════════════════════════════════════════


def check_required_categories(
    required: List[str],
    effective: Dict[str, Any],
    tool_name: str,
    tool_args: Dict[str, Any],
    description: str,
    event_bus: Any = None,
    agent_id: str = "0",
    session_id: str = "",
    permission_footprint: Optional[Dict[str, Any]] = None,
    is_worker_context: bool = False,
) -> Tuple[bool, str]:
    """
    Check a tool's required categories against the effective permission dict.

    Args:
        required:
            List of ``"category:value"`` strings (e.g. ``["filesystem:write"]``).
        effective:
            Output of ``get_effective_permissions()``.
        tool_name:
            Name of the tool being checked (for prompt context).
        tool_args:
            The tool call arguments (for prompt context).
        description:
            Human-readable description of what the tool is about to do
            (obtained from ``tool_class.describe_action()``).
        event_bus:
            An ``EventBus`` instance used to publish ``SecurityPromptEvent``.
            Pass ``agent.events.global_event_bus`` in production.
        agent_id:
            Agent identifier string (for prompt context).
        session_id:
            Session identifier (for prompt context).
        permission_footprint:
            Optional dict of worker-level permission overrides using the
            same string hierarchy as session/workspace permissions
            (e.g. ``{"network": "read", "filesystem": "banned"}``).
            Each value is a string level (``"banned"``, ``"read"``,
            ``"write"``, ``"full"``).  Applied via ``_min_permission``
            which returns the more restrictive of the effective and
            worker value.  If a key exists in *permission_footprint* but
            not in *effective* — or its value is not a known level for
            that category — the category resolves to a hard deny
            (fail-closed): a worker never gains access to a category the
            session does not explicitly expose.
        is_worker_context:
            If True, the call originates from a worker where no interactive
            user is available — deny immediately without prompting.

    Returns:
        ``(True, "")`` if all checks pass.
        ``(False, error_message)`` if any check fails or the user denies.
    """

    # ── Apply worker-level restrictions ─────────────────────────────────
    if permission_footprint is not None:
        for category, worker_val in permission_footprint.items():
            valid_levels = PERMISSION_SCHEMA.get(category)
            if valid_levels is None or worker_val not in valid_levels:
                # Unknown category or unknown level — fail closed: deny.
                effective[category] = False
            elif category in effective:
                effective[category] = _min_permission(
                    effective[category], worker_val
                )
            else:
                # Category absent from the session's effective dict: the
                # worker may NOT grant itself a category the session does
                # not expose — resolve to a hard deny (fail-closed).
                effective[category] = False

    ask_categories: List[str] = []
    prompts_needed = False

    for req in required:
        if ":" not in req:
            continue  # malformed, skip

        category, required_value = req.split(":", 1)
        allowed = effective.get(category)

        if allowed is None:
            return False, f"Permission denied: Unknown category '{category}' required by tool."

        result = _value_satisfies(required_value, allowed)

        if result is False:
            ceiling_level = _ceiling_denial_note(
                category, required_value, effective, permission_footprint
            )
            contradiction = (
                _session_exceeds_ceiling_message(category, effective)
                if ceiling_level
                else ""
            )
            if contradiction:
                # The session grant sits ABOVE the workspace ceiling: name the
                # SESSION's pre-ceiling value and the CEILING value instead of
                # mislabelling the capped value as the session's own grant.
                return False, f"Permission denied: {contradiction}"
            message = (
                f"Permission denied: Tool requires {category}:{required_value}, "
                f"but session allows {category}:{allowed}"
            )
            if ceiling_level:
                # The workspace permission ceiling is what blocks this call
                # (the session grant alone would allow it) — name it so the
                # denial explains why a permissive-looking session profile
                # still refuses.
                message = f"{message} (workspace ceiling: {ceiling_level})"
            return False, message

        if result == "ASK":
            prompts_needed = True
            ask_categories.append(req)

    if not prompts_needed:
        return True, ""

    # ── Worker context (no interactive user): deny immediately without blocking ──
    if event_bus is None or is_worker_context or isinstance(event_bus, NullEventBus):
        return False, (
            f"Permission denied: {', '.join(ask_categories)} required by "
            f"'{tool_name}' — ask requires interactive approval; not available in worker context."
        )

    # ── Prompt the user for approval ────────────────────────────────────
    request_id = str(uuid.uuid4())
    response_queue: queue.Queue = queue.Queue()
    if _pending_requests_lock is not None:
        with _pending_requests_lock:
            _pending_security_requests[request_id] = response_queue

    # Publish SecurityPromptEvent
    event = SecurityPromptEvent(
        data={
            "request_id": request_id,
            "agent_id": agent_id,
            "tool_name": tool_name,
            "capabilities": ask_categories,
            "arguments": tool_args,
            "session_id": session_id,
            "description": description,
        }
    )
    if event_bus is not None:
        event_bus.publish(event)

    # Wait for response
    try:
        response = response_queue.get(timeout=PROMPT_TIMEOUT)
        approved = response.get("approved", False)
        if approved:
            return True, ""
        else:
            reason = response.get("reason", "User denied the request")
            return False, (
                f"Permission denied: {', '.join(ask_categories)} required by "
                f"'{tool_name}' — {reason}"
            )
    except queue.Empty:
        return False, (
            f"Permission denied: {', '.join(ask_categories)} required by "
            f"'{tool_name}' — security prompt timed out."
        )
    finally:
        if _pending_requests_lock is not None:
            with _pending_requests_lock:
                _pending_security_requests.pop(request_id, None)


def resolve_prompt(request_id: str, approved: bool, remember: bool = False) -> bool:
    """
    Resolve a pending security prompt.

    Delegates to ``resolve_security_prompt`` from ``thoughtmachine.security``
    which uses the shared ``_pending_security_requests`` registry.

    Returns ``True`` if the prompt was found and resolved, ``False`` otherwise.
    """
    # Check if the request exists before delegating
    found = False
    if _pending_requests_lock is not None:
        with _pending_requests_lock:
            found = request_id in _pending_security_requests
    else:
        found = request_id in _pending_security_requests

    if found:
        resolve_security_prompt(request_id, approved, remember)
    return found
