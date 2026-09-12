"""Admission control for container creation (feat/admission-gate, phase 1).

This module is a **pure policy** layer: given an :class:`AdmissionRequest` it
returns a :data:`Decision` -- :class:`Allow`, :class:`Deny` or
:class:`Transform`.  It performs no side effects of its own beyond consulting
its probe interface, and it **never raises**: every unexpected failure path
collapses to ``Deny("internal_error")`` so a caller (which is *not* wired here
-- call sites are out of scope for phase 1) can fail *closed*.

Phase-1 scope
-------------
This file defines the dataclasses, the :data:`REASON_CODES` vocabulary, the
:func:`admit` entry point, the :func:`_narrow` policy transform and the real
probe implementation.  Integration (wiring this into the container-create
call sites) is intentionally deferred.

Relationship to :mod:`security.security_gate`
---------------------------------------------
Lifecycle / permission / capability / network resolution is delegated to
:func:`security.security_gate.resolve_container_config`; this module does not
re-derive it.

Deviations from the original spec (see phase-1 report)
------------------------------------------------------
* ``ContainerConfig`` exposes no memory / CPU / OOM fields, so the *derived*
  policy can only constrain ``network_mode`` and ``read_only`` here; the
  memory / CPU / OOM axes remain unconstrained by the resolver.
* ``resolve_container_config`` *returns* a ``ContainerConfigError`` **value**
  (it is a plain frozen dataclass, not an exception), so the result is
  ``isinstance``-checked rather than caught.  An unexpected *raise* is still
  guarded.
* ``session_config`` is a plain ``dict`` in the live code
  (``{"container_limits": {"max_containers": N}}``), not the attribute-nested
  object the spec sketched; :func:`admit` accepts the dict shape (and, for
  robustness, the attribute form too).
* ``Probes.now()`` exists on the protocol for future use but is **not**
  consulted by :func:`admit` (no check depends on wall-clock time today).
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import time
from collections.abc import Mapping
from typing import Any, Dict, Optional, Protocol, Tuple, Union

from agent.config.defaults import (
    ADMISSION_DISK_MAX_USED_PCT,
    ADMISSION_DISK_MIN_FREE_BYTES,
    ADMISSION_DISK_WARN_MIN_FREE_BYTES,
    ADMISSION_DISK_WARN_USED_PCT,
    ADMISSION_IMAGE_ALLOWLIST,
    CONTAINER_TYPES,
    DEFAULT_MAX_CONTAINERS,
)
from security.security_gate import (
    ContainerConfig,
    ContainerConfigError,
    resolve_container_config,
)

__all__ = [
    "AdmissionRequest",
    "Allow",
    "ContainerSpec",
    "Decision",
    "Deny",
    "REASON_CODES",
    "Transform",
    "admit",
    "Probes",
]


# ─═ Reason-code vocabulary ═─────────────────────────────────────────────────
REASON_UNKNOWN_CONTAINER_TYPE = "unknown_container_type"
REASON_WORKSPACE_ID_REQUIRED = "workspace_id_required"
REASON_IMAGE_NOT_ALLOWED = "image_not_allowed"
REASON_INSUFFICIENT_DISK = "insufficient_disk"
REASON_DAEMON_UNREACHABLE = "daemon_unreachable"
REASON_CONTAINER_LIMIT_EXCEEDED = "container_limit_exceeded"
REASON_NARROWED_TO_POLICY = "narrowed_to_policy"
REASON_TRANSFORM_WIDEN_FORBIDDEN = "transform_widen_forbidden"
REASON_INTERNAL_ERROR = "internal_error"
REASON_UNKNOWN_LIFECYCLE_CLASS = "unknown_lifecycle_class"
REASON_CAPABILITIES_REQUIRED = "capabilities_required"
REASON_BAD_PERMISSIONS = "bad_permissions"
REASON_RESOLUTION_FAILED = "resolution_failed"

#: The complete, closed set of reason codes an admission decision may carry.
REASON_CODES = frozenset(
    {
        REASON_UNKNOWN_CONTAINER_TYPE,
        REASON_WORKSPACE_ID_REQUIRED,
        REASON_IMAGE_NOT_ALLOWED,
        REASON_INSUFFICIENT_DISK,
        REASON_DAEMON_UNREACHABLE,
        REASON_CONTAINER_LIMIT_EXCEEDED,
        REASON_NARROWED_TO_POLICY,
        REASON_TRANSFORM_WIDEN_FORBIDDEN,
        REASON_INTERNAL_ERROR,
        REASON_UNKNOWN_LIFECYCLE_CLASS,
        REASON_CAPABILITIES_REQUIRED,
        REASON_BAD_PERMISSIONS,
        REASON_RESOLUTION_FAILED,
    }
)


# ─═ Dataclasses ═════════════════════════════════════════════════════════════
@dataclasses.dataclass(frozen=True)
class ContainerSpec:
    """A proposed container to admit (the *request* half of admission)."""

    container_type: str
    lifecycle_class: str
    workspace_id: str
    session_id: Optional[str] = None
    image: Optional[str] = None
    name: Optional[str] = None
    purpose: Optional[str] = None
    mem_limit: Optional[Any] = None
    cpu_quota: Optional[Any] = None
    oom_score_adj: Optional[Any] = None
    network_mode: str = "none"
    worker_id: Optional[str] = None
    read_only: bool = True


@dataclasses.dataclass(frozen=True)
class AdmissionRequest:
    """Everything :func:`admit` needs to decide on a :class:`ContainerSpec`."""

    spec: ContainerSpec
    permissions: Any = None
    capabilities: Any = None
    session_config: Any = None


@dataclasses.dataclass(frozen=True)
class Allow:
    """The container is admitted (possibly with advisory ``notes``)."""

    spec: ContainerSpec
    notes: Tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class Deny:
    """The container is refused; ``code`` is one of :data:`REASON_CODES`."""

    code: str
    message: str = ""
    details: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class Transform:
    """The container is admitted only after narrowing its config."""

    spec: ContainerSpec
    code: str
    reason: str = ""


Decision = Union[Allow, Deny, Transform]


# ─═ Probes ══════════════════════════════════════════════════════════════════
class Probes(Protocol):
    """Host-observation seam so :func:`admit` is unit-testable and hermetic."""

    def disk_usage(self, path: Any) -> Any: ...
    def docker_reachable(self) -> bool: ...
    def workspace_container_count(self, workspace_id: str) -> int: ...
    def now(self) -> float: ...


class _RealProbes:
    """Live probes backed by ``shutil`` and the Docker SDK (lazy imports)."""

    def disk_usage(self, path: Any) -> Any:
        return shutil.disk_usage(path)

    def docker_reachable(self) -> bool:
        try:
            import docker  # lazy: only needed at runtime

            return bool(docker.from_env().ping())
        except Exception:
            return False

    def workspace_container_count(self, workspace_id: str) -> int:
        try:
            import docker  # lazy: only needed at runtime

            client = docker.from_env()
            containers = client.containers.list(
                all=True,
                filters={"label": f"thoughtmachine.workspace_id={workspace_id}"},
            )
            return len(containers)
        except Exception:
            return 0

    def now(self) -> float:
        return time.time()


# ─═ Workspace-root resolution ═══════════════════════════════════════════════
def _resolve_workspace_root() -> Any:
    """Resolve the ThoughtMachine vault root for the disk check.

    Mirrors the established pattern in :mod:`security.security_gate`: the vault
    root is resolved lazily via ``thoughtmachine.vault.vault_root()`` (honours
    ``THOUGHTMACHINE_VAULT_ROOT``, else ``~/.thoughtmachine``).  Any failure
    falls back to the current working directory so the disk probe always has a
    concrete path to measure.
    """
    try:
        import thoughtmachine.vault as _vault_module

        return _vault_module.vault_root()
    except Exception:
        return os.getcwd()


# ─═ Public entry point ══════════════════════════════════════════════════════
def admit(request: Any, *, probes: Optional[Any] = None) -> Decision:
    """Decide whether to admit the container described by *request*.

    Fail-closed: any unexpected exception is trapped and returned as
    ``Deny("internal_error")`` -- this function never raises.
    """
    try:
        return _admit(request, probes)
    except Exception as exc:  # pragma: no cover - exercised via fuzz/faults
        return Deny(
            code=REASON_INTERNAL_ERROR,
            message=f"admission failed closed: {exc!r}",
        )


def _admit(request: Any, probes: Optional[Any]) -> Decision:
    if probes is None:
        probes = _RealProbes()

    spec = getattr(request, "spec", None)

    # ── step 1: container_type + workspace_id ──────────────────────────────
    container_type = getattr(spec, "container_type", None)
    if container_type not in CONTAINER_TYPES:
        return Deny(
            REASON_UNKNOWN_CONTAINER_TYPE,
            f"unknown container_type: {container_type!r}",
        )

    workspace_id = getattr(spec, "workspace_id", None)
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        return Deny(REASON_WORKSPACE_ID_REQUIRED, "workspace_id is required")

    # ── step 2: image allowlist ────────────────────────────────────────────
    image = getattr(spec, "image", None)
    if image is not None and image != "" and image not in ADMISSION_IMAGE_ALLOWLIST:
        return Deny(REASON_IMAGE_NOT_ALLOWED, f"image not allowed: {image!r}")

    notes = []

    # ── step 3: disk headroom (two-tier) ───────────────────────────────────
    root = _resolve_workspace_root()
    total, used, free = probes.disk_usage(root)
    used_pct = (used / total) * 100.0 if total and total > 0 else 0.0
    if used_pct > ADMISSION_DISK_MAX_USED_PCT or free < ADMISSION_DISK_MIN_FREE_BYTES:
        return Deny(
            REASON_INSUFFICIENT_DISK,
            f"insufficient disk: used={used_pct:.1f}% free={free}",
        )
    if used_pct > ADMISSION_DISK_WARN_USED_PCT or free < ADMISSION_DISK_WARN_MIN_FREE_BYTES:
        notes.append("disk_usage_high")

    # ── step 4: docker daemon reachability ─────────────────────────────────
    try:
        reachable = bool(probes.docker_reachable())
    except Exception:
        reachable = False
    if not reachable:
        return Deny(REASON_DAEMON_UNREACHABLE, "docker daemon unreachable")

    # ── step 5: resolve container config (lifecycle/permissions/network) ────
    lifecycle_class = getattr(spec, "lifecycle_class", None)
    try:
        resolved = resolve_container_config(
            getattr(request, "permissions", None),
            getattr(request, "capabilities", None),
            lifecycle_class,
        )
    except Exception as exc:
        return Deny(REASON_RESOLUTION_FAILED, f"resolution raised: {exc!r}")
    if isinstance(resolved, ContainerConfigError):
        code = (
            resolved.code if resolved.code in REASON_CODES else REASON_RESOLUTION_FAILED
        )
        return Deny(code, resolved.message)

    # ── step 6: per-workspace container limit ──────────────────────────────
    limit = _container_limit(getattr(request, "session_config", None))
    count = probes.workspace_container_count(workspace_id)
    if count >= limit:
        return Deny(
            REASON_CONTAINER_LIMIT_EXCEEDED,
            f"container limit reached ({count}/{limit})",
        )

    # ── step 7: derived-policy narrowing ───────────────────────────────────
    derived = {
        "network_mode": resolved.network_mode,
        "mem_limit": None,
        "cpu_quota": None,
        "oom_score_adj": None,
        "read_only": True if resolved.workspace_mode == "ro" else None,
    }
    decision = _narrow(spec, derived)
    if isinstance(decision, Allow):
        return Allow(spec=decision.spec, notes=tuple(notes))
    return decision


# ─═ Policy narrowing ════════════════════════════════════════════════════════
_NETWORK_RANK = {"none": 0, "bridge": 1}


def _axis(obj: Any, name: str) -> Any:
    """Read one policy axis from a mapping *or* an attribute object."""
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _to_int(value: Any) -> Optional[int]:
    """Coerce an int / numeric string / docker memory string to an int.

    Returns ``None`` when the value is absent or not interpretable (treated as
    'unconstrained' by the comparison helpers).
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if not isinstance(value, str):
        return None
    s = value.strip().lower()
    if not s:
        return None
    units = {
        "": 1, "b": 1,
        "k": 1024, "kb": 1024,
        "m": 1024 ** 2, "mb": 1024 ** 2,
        "g": 1024 ** 3, "gb": 1024 ** 3,
    }
    num = ""
    for ch in s:
        if ch.isdigit() or ch == ".":
            num += ch
        else:
            break
    if not num:
        return None
    try:
        n = float(num)
    except ValueError:
        return None
    return int(n * units.get(s[len(num):].strip(), 1))


def _cmp_network(prop: Any, der: Any) -> Optional[int]:
    """-1 = derived narrower, +1 = derived wider, 0 = equal, None = skip."""
    if der is None:
        return None
    if der not in _NETWORK_RANK or prop not in _NETWORK_RANK:
        return None
    if _NETWORK_RANK[der] < _NETWORK_RANK[prop]:
        return -1
    if _NETWORK_RANK[der] > _NETWORK_RANK[prop]:
        return 1
    return 0


def _cmp_smaller(prop: Any, der: Any) -> Optional[int]:
    """Memory/CPU quota: a *smaller* derived value is narrower."""
    if der is None:
        return None
    if prop is None:
        return -1  # unconstrained proposal -> concrete derived is a cap
    try:
        if der < prop:
            return -1
        if der > prop:
            return 1
    except TypeError:
        return None
    return 0


def _cmp_larger(prop: Any, der: Any) -> Optional[int]:
    """OOM score: a *larger* derived value is narrower (killed first)."""
    if der is None:
        return None
    if prop is None:
        return -1
    try:
        if der > prop:
            return -1
        if der < prop:
            return 1
    except TypeError:
        return None
    return 0


def _cmp_read_only(prop: Any, der: Any) -> Optional[int]:
    """Read-only: ``True`` (ro) is narrower than ``False`` (rw)."""
    if der is None:
        return None
    prop_b = bool(prop)
    der_b = bool(der)
    if der_b == prop_b:
        return 0
    return -1 if der_b else 1


def _narrow(proposed: Any, derived: Any) -> Decision:
    """Apply the *derived* policy to *proposed*.

    ``derived`` is a mapping (or object) with optional axes ``network_mode``,
    ``mem_limit``, ``cpu_quota``, ``oom_score_adj`` and ``read_only``.  A
    missing / ``None`` axis is *unconstrained* and skipped.

    * A derived axis WIDER than proposed -> ``Deny("transform_widen_forbidden")``.
    * All concrete axes equal -> ``Allow(proposed)`` unchanged.
    * Otherwise (some narrower, none wider) -> ``Transform`` with each narrower
      axis replaced by the derived value.
    """
    checks = []  # (axis, comparison, derived_value)

    c = _cmp_network(_axis(proposed, "network_mode"), _axis(derived, "network_mode"))
    if c is not None:
        checks.append(("network_mode", c, _axis(derived, "network_mode")))

    c = _cmp_smaller(
        _to_int(_axis(proposed, "mem_limit")), _to_int(_axis(derived, "mem_limit"))
    )
    if c is not None:
        checks.append(("mem_limit", c, _axis(derived, "mem_limit")))

    c = _cmp_smaller(
        _to_int(_axis(proposed, "cpu_quota")), _to_int(_axis(derived, "cpu_quota"))
    )
    if c is not None:
        checks.append(("cpu_quota", c, _axis(derived, "cpu_quota")))

    c = _cmp_larger(
        _to_int(_axis(proposed, "oom_score_adj")),
        _to_int(_axis(derived, "oom_score_adj")),
    )
    if c is not None:
        checks.append(("oom_score_adj", c, _axis(derived, "oom_score_adj")))

    c = _cmp_read_only(_axis(proposed, "read_only"), _axis(derived, "read_only"))
    if c is not None:
        checks.append(("read_only", c, _axis(derived, "read_only")))

    wider = [axis for axis, cmp_, _ in checks if cmp_ > 0]
    if wider:
        return Deny(
            REASON_TRANSFORM_WIDEN_FORBIDDEN,
            f"derived policy would widen: {', '.join(wider)}",
            details={"axes": wider},
        )

    narrower = {axis: val for axis, cmp_, val in checks if cmp_ < 0}
    if not narrower:
        return Allow(spec=proposed)

    return Transform(
        spec=dataclasses.replace(proposed, **narrower),
        code=REASON_NARROWED_TO_POLICY,
        reason="derived policy narrowed: " + ", ".join(sorted(narrower)),
    )


# ─═ Helpers ═════════════════════════════════════════════════════════════════
def _container_limit(session_config: Any) -> int:
    """Resolve the per-workspace container limit, capped by configuration.

    Precedence: ``DEFAULT_MAX_CONTAINERS`` unless ``session_config`` carries a
    ``container_limits.max_containers`` value, which is clamped to ``>= 1``.
    Accepts the live dict shape and (defensively) the attribute-nested form.
    """
    if session_config is None:
        return DEFAULT_MAX_CONTAINERS

    if isinstance(session_config, Mapping):
        limits = session_config.get("container_limits")
    else:
        limits = getattr(session_config, "container_limits", None)

    if isinstance(limits, Mapping):
        raw = limits.get("max_containers")
    elif limits is not None:
        raw = getattr(limits, "max_containers", None)
    else:
        raw = None

    if raw is None:
        return DEFAULT_MAX_CONTAINERS
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MAX_CONTAINERS
