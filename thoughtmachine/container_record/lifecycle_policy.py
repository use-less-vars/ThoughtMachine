"""Lifecycle-class policy table (container-record subsystem).

A single, pure description of what each container *lifecycle class* means:
whether the agent may reach the container, whether the container owns its own
lifecycle, how long an unowned workspace container may linger before garbage
collection, and which drift axes apply to it.

This module is intentionally dependency-light and side-effect free: it imports
only the standard library and the peer
:mod:`thoughtmachine.container_record.models` schema constants — never a Docker
client, the infrastructure layer, or ``security.security_gate``.

Consequently the drift-axis names on :attr:`LifecyclePolicy.drift_axes` *mirror*
:mod:`thoughtmachine.container_record.drift` rather than importing it.  ``drift``
pulls in ``security.security_gate``, whose own module body imports
``container_record`` — importing ``drift`` here at import time would close the
cycle ``security_gate -> container_record.__init__ -> lifecycle_policy -> drift
-> security_gate`` and break a ``security_gate``-first import.  The break is
silent, not loud: ``security_gate`` builds the tool registry as an import side
effect, so closing the cycle silently DROPS ``DockerCodeRunner`` from that
registry rather than raising.
:mod:`tests.test_lifecycle_policy` asserts the two axis sets stay in lock-step
(``test_drift_axes_mirror_drift_class_constants``); that mirror check is the
guard for the hand-maintained axis names above.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .models import (
    LIFECYCLE_EPHEMERAL,
    LIFECYCLE_PERSISTENT,
    LIFECYCLE_RESOURCE,
    LIFECYCLE_SERVICE,
)

# ── Resource-container identity literals ────────────────────────────────────
#
# Single source of truth shared with the migration.  The live probe
# ``ContainerManager._is_resource_container`` and the resource/worker container
# managers use the same three values.

#: Label marking a container as a resource container.
RESOURCE_LABEL = "thoughtmachine.resource"

#: Name prefix of a resource container (``tm-res-<hash>-git``).
RESOURCE_NAME_PREFIX = "tm-res-"

#: Image name of a resource container (may carry a ``:<tag>`` suffix).
RESOURCE_IMAGE = "tm-resource-git"

# ── Drift axes (mirror container_record.drift.CLASS_*) ──────────────────────

_AXIS_IDENTITY = "identity"
_AXIS_POLICY = "policy"
_AXIS_RUNTIME = "runtime"
_AXIS_IMAGE = "image"
_AXIS_HARDENING = "hardening"

_ALL_DRIFT_AXES: frozenset[str] = frozenset(
    {_AXIS_IDENTITY, _AXIS_POLICY, _AXIS_RUNTIME, _AXIS_IMAGE, _AXIS_HARDENING}
)
_IMAGE_AXIS: frozenset[str] = frozenset({_AXIS_IMAGE})


def is_resource_like(labels, name=None, image_tags=None) -> bool:
    """Return whether a container is a *resource* container.

    Mirrors ``ContainerManager._is_resource_container``
    (``infra/container_manager.py``): a container is resource-like when **any**
    of the following holds

    * it carries a non-empty string :data:`RESOURCE_LABEL` label,
    * its ``name`` begins with :data:`RESOURCE_NAME_PREFIX` (a leading ``/`` —
      as produced by a Docker inspect ``Name`` field — is ignored),
    * one of its image references/tags is :data:`RESOURCE_IMAGE` (or
      ``tm-resource-git:<tag>``).

    ``image_tags`` is either a single reference string or an iterable of tags
    (a live ``Image`` object exposes ``.tags``); ``None`` means "no image
    information".  Every argument tolerates ``None`` and malformed values and
    this function never raises.
    """
    if isinstance(labels, Mapping):
        label_val = labels.get(RESOURCE_LABEL)
        if isinstance(label_val, str) and label_val:
            return True

    if isinstance(name, str) and name.lstrip("/").startswith(RESOURCE_NAME_PREFIX):
        return True

    if image_tags is None:
        return False
    if isinstance(image_tags, str):
        tags: Iterable[Any] = (image_tags,)
    elif isinstance(image_tags, (bytes, bytearray)):
        tags = ()
    else:
        try:
            tags = tuple(image_tags)
        except TypeError:
            tags = ()
    return any(
        isinstance(tag, str)
        and (tag == RESOURCE_IMAGE or tag.startswith(RESOURCE_IMAGE + ":"))
        for tag in tags
    )


@dataclass(frozen=True)
class LifecyclePolicy:
    """The policy attached to one lifecycle class.

    Attributes:
        class_name: the lifecycle-class string this policy describes.
        agent_reachable: whether the agent may address the container directly.
        own_lifecycle: whether the container manages its own lifecycle (the
            workspace does not stop/GC it).
        workspace_gc_max_age_s: age, in seconds, after which an unowned
            workspace container may be garbage-collected (``None`` = exempt).
        drift_axes: the drift classes compared for this lifecycle class.
        restart_policy: the Docker restart policy applied when a container of
            this class is created (``None`` = unset, i.e. Docker's default
            ``no``).
    """

    class_name: str
    agent_reachable: bool
    own_lifecycle: bool
    workspace_gc_max_age_s: int | None
    drift_axes: frozenset[str]
    restart_policy: str | None = None


#: The policy table, keyed by lifecycle class.
#:
#: ``workspace_gc_max_age_s = 86400`` mirrors the garbage collector's
#: ``TM_GC_ORPHAN_RESOURCE_CONTAINER_HOURS`` default of 24 h (``vault_gc``).
#:
#: ``workspace_gc_max_age_s = None`` is OVERLOADED and means two different
#: things depending on the class:
#:   * EPHEMERAL — exempt from workspace GC: the workspace garbage collector
#:     must NOT reap the container (``None`` = never ages out).
#:   * RESOURCE / SERVICE — not applicable, because ``own_lifecycle=True``:
#:     these classes manage their own lifecycle, so workspace GC never
#:     considers their age.
POLICY_BY_CLASS: dict[str, LifecyclePolicy] = {
    LIFECYCLE_PERSISTENT: LifecyclePolicy(
        class_name=LIFECYCLE_PERSISTENT,
        agent_reachable=True,
        own_lifecycle=False,
        workspace_gc_max_age_s=86400,
        drift_axes=_ALL_DRIFT_AXES,
        restart_policy="unless-stopped",
    ),
    LIFECYCLE_EPHEMERAL: LifecyclePolicy(
        class_name=LIFECYCLE_EPHEMERAL,
        agent_reachable=True,
        own_lifecycle=False,
        workspace_gc_max_age_s=None,
        drift_axes=_IMAGE_AXIS,
        restart_policy="no",
    ),
    LIFECYCLE_RESOURCE: LifecyclePolicy(
        class_name=LIFECYCLE_RESOURCE,
        agent_reachable=False,
        own_lifecycle=True,
        workspace_gc_max_age_s=None,
        drift_axes=_IMAGE_AXIS,
        restart_policy="unless-stopped",
    ),
    LIFECYCLE_SERVICE: LifecyclePolicy(
        class_name=LIFECYCLE_SERVICE,
        agent_reachable=False,
        own_lifecycle=True,
        workspace_gc_max_age_s=None,
        drift_axes=_IMAGE_AXIS,
        restart_policy="unless-stopped",
    ),
}


class UnknownLifecycleClass(ValueError):
    """Raised by :func:`policy_for` for a lifecycle class with no policy."""


def policy_for(lifecycle_class: str) -> LifecyclePolicy:
    """Return the :class:`LifecyclePolicy` for *lifecycle_class* (fail-closed).

    An unknown, empty or ``None`` class raises :class:`UnknownLifecycleClass`
    rather than silently defaulting to a permissive policy.
    """
    try:
        return POLICY_BY_CLASS[lifecycle_class]
    except (KeyError, TypeError) as exc:
        raise UnknownLifecycleClass(
            f"unknown lifecycle class: {lifecycle_class!r}"
        ) from exc


# ── Restart-policy plumbing ──────────────────────────────────────────────────


def normalise_restart_policy(value: Any) -> str:
    """Return the Docker restart-policy name for *value*.

    Docker reports an unset restart policy as ``""`` (live inspect) or
    ``None`` (a missing ``HostConfig.RestartPolicy``); both mean *do not
    restart* and normalise to ``"no"``.  Any other value is stripped and
    returned verbatim — unknown names are *not* mapped, the caller decides
    how to rank them.  Never raises.
    """
    if value is None:
        return "no"
    text = str(value).strip()
    return text or "no"


def docker_restart_policy(lifecycle_class: str) -> dict | None:
    """Return the docker-py ``HostConfig.restart_policy`` for a class.

    ``containers.run(restart_policy=...)`` takes the *dict* form of Docker's
    ``RestartPolicy`` (``{"Name": ..., "MaximumRetryCount": N}``), not the
    bare name.  Returns ``None`` — so the caller omits the keyword and Docker
    applies its own default (``no``) — when the class has no restart policy or
    cannot be resolved.  Never raises.
    """
    try:
        policy = policy_for(lifecycle_class).restart_policy
    except UnknownLifecycleClass:
        return None
    if policy is None:
        return None
    return {"Name": normalise_restart_policy(policy), "MaximumRetryCount": 0}
