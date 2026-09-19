"""Spec-based primitive for creating one hardened docker container.

This module introduces :class:`ContainerCreateSpec` (a frozen, immutable
description of a container) and :func:`create_hardened_container` (the single
call site that turns a spec into a running, hardened container: identity env,
optional admission, mounts, hardened ``containers.run`` kwargs, and container
record bookkeeping).

The docker SDK is **never** imported at module import time: ``docker.types.Mount``
is imported lazily inside :func:`create_hardened_container`, and only when the
spec actually carries mounts.  This keeps the module importable in environments
where the docker SDK is absent.

The hardening recipe (:func:`_expected_hardening_recipe`) and the conformance
predicate (:func:`_hardening_conformance`) are defined here as the canonical
homes; :mod:`infra.container_manager` still holds a duplicate copy of the recipe
and the conformance check, and the canonical move (deleting those copies in
favour of a re-export shim) happens at step 1, not now.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from agent.config.defaults import (
    CONTAINER_TYPES,
    DEFAULT_COMMAND,
    DEFAULT_CPU_QUOTA,
    DEFAULT_MEM_LIMIT,
    DEFAULT_RESOURCE_OOM_SCORE_ADJ,
    DEFAULT_USER_OOM_SCORE_ADJ,
    host_tmpfs,
    host_user,
)
from infra.container_env import merge_container_identity_env
from security.admission_gate import (
    AdmissionDenied,
    AdmissionRequest,
    ClientProbes,
    ContainerSpec,
    Deny,
    Transform,
    admit,
)
from thoughtmachine.container_record import docker_restart_policy
from thoughtmachine.container_record.hook import record_creation

__all__ = [
    "AdmissionContext",
    "ContainerCreateSpec",
    "CreatedContainer",
    "HardeningRecipe",
    "MountSpec",
    "create_hardened_container",
]


@dataclass(frozen=True)
class HardeningRecipe:
    """The hardening envelope a container must be created with.

    Attributes:
        cap_drop: Linux capabilities to drop (``("ALL",)``).
        security_opt: Docker security options (``("no-new-privileges:true",)``).
        read_only: whether the container root filesystem is read-only.
        user: the ``"uid:gid"`` the container runs as.
    """

    cap_drop: tuple[str, ...]
    security_opt: tuple[str, ...]
    read_only: bool
    user: str


@dataclass(frozen=True)
class MountSpec:
    """A single mount to attach to a created container.

    Attributes:
        source: the host path (bind) or volume name.
        target: the in-container mount point.
        type: the docker mount type (``"bind"`` by default).
        read_only: whether the mount is mounted read-only.
    """

    source: str
    target: str
    type: str = "bind"
    read_only: bool = False


def _expected_hardening_recipe() -> HardeningRecipe:
    """Return the frozen :class:`HardeningRecipe` every container must carry.

    ``cap_drop`` is ``["ALL"]``, ``security_opt`` is
    ``["no-new-privileges:true"]``, ``read_only`` is ``True``, and ``user`` is
    the host user (``host_user()``) or ``"0:0"`` when the host user cannot be
    determined.  ``user`` is resolved at CALL time, so a process whose uid view
    changes between calls sees the current value.
    """
    return HardeningRecipe(
        cap_drop=("ALL",),
        security_opt=("no-new-privileges:true",),
        read_only=True,
        user=(host_user() or "0:0"),
    )


def _hardening_conformance(container: Any, recipe: HardeningRecipe) -> list:
    """Return the docker HARDENING axes on which *container* is too weak.

    ABSENT vs PRESENT rule (in words):

    * an ABSENT backing key means the daemon exposes no surface on that axis,
      so the axis is SKIPPED (never drift); this keeps fakes/daemons that
      predate an axis from ever drifting on it;
    * a PRESENT-but-WRONG value is a MISMATCH, and the axis name is returned.

    PURE predicate over ``container.attrs`` (no Manager dependency).  The four
    axes, each read from the docker section that owns it:

    * ``HostConfig.CapDrop`` present => ``"ALL"`` must be among the dropped caps
      (a SUPERSET is accepted);
    * ``HostConfig.SecurityOpt`` present => ``"no-new-privileges:true"`` must be
      among the options;
    * ``HostConfig.ReadonlyRootfs`` present => must be exactly ``True``;
    * ``Config.User`` present => must EQUAL ``recipe.user``.

    Returns the list of failing axis names (``[]`` == fully conformant).
    DELIBERATE DIVERGENCE from ``_user_drift_axis``: that axis treats a BLANK
    live user as "no user" and NOT drift, whereas here a PRESENT empty-string
    ``Config.User`` is a MISMATCH -- a container that requested no user did not
    request the hardened user we mandate.
    """
    try:
        attrs = getattr(container, "attrs", None)
    except Exception:
        return []
    if not isinstance(attrs, dict):
        return []
    host = attrs.get("HostConfig") or {}
    cfg = attrs.get("Config") or {}
    if not isinstance(host, dict):
        host = {}
    if not isinstance(cfg, dict):
        cfg = {}
    failures = []
    if "CapDrop" in host:
        cap_drop = host.get("CapDrop")
        if not (isinstance(cap_drop, (list, tuple)) and "ALL" in cap_drop):
            failures.append("cap_drop")
    if "SecurityOpt" in host:
        security_opt = host.get("SecurityOpt")
        if not (isinstance(security_opt, (list, tuple))
                and "no-new-privileges:true" in security_opt):
            failures.append("security_opt")
    if "ReadonlyRootfs" in host:
        if host.get("ReadonlyRootfs") is not True:
            failures.append("read_only")
    if "User" in cfg:
        if cfg.get("User") != recipe.user:
            failures.append("user")
    return failures


@dataclass(frozen=True)
class ContainerCreateSpec:
    """An immutable description of one container to create.

    Attributes:
        image: the docker image reference.
        command: the container command (normalised to a tuple in ``__post_init__``).
        name: the container name.
        container_type: one of :data:`agent.config.defaults.CONTAINER_TYPES`.
        lifecycle_class: the container-record lifecycle class.
        hardening: the hardening envelope (defaults to the expected recipe).
        workspace_id: owning workspace, or ``None`` (no record is written).
        session_id: owning session, or ``None``.
        labels: docker labels to apply.
        environment: environment variables merged with the identity env.
        mounts: mounts to attach.
        tmpfs: tmpfs mounts (defaults to :func:`agent.config.defaults.host_tmpfs`).
        network_mode: docker network mode.
        mem_limit: memory limit.
        cpu_quota: CPU quota.
        oom_score_adj: OOM score adjustment; ``None`` selects the per-type default.
    """

    image: str
    command: tuple[str, ...]
    name: str
    container_type: str
    lifecycle_class: str
    hardening: HardeningRecipe = field(default_factory=lambda: _expected_hardening_recipe())
    workspace_id: str | None = None
    session_id: str | None = None
    labels: Mapping[str, str] = field(default_factory=dict)
    environment: Mapping[str, str] = field(default_factory=dict)
    mounts: tuple[MountSpec, ...] = ()
    tmpfs: Mapping[str, str] = field(default_factory=host_tmpfs)
    network_mode: str = "none"
    mem_limit: str = DEFAULT_MEM_LIMIT
    cpu_quota: int = DEFAULT_CPU_QUOTA
    oom_score_adj: int | None = None

    def __post_init__(self) -> None:
        if self.container_type not in CONTAINER_TYPES:
            raise ValueError(f"unknown container_type: {self.container_type!r}")

        command = self.command
        if command is None:
            command = tuple(DEFAULT_COMMAND)
        elif isinstance(command, (list, tuple)):
            command = tuple(command)
        object.__setattr__(self, "command", command)

        if self.oom_score_adj is None:
            default = (
                DEFAULT_USER_OOM_SCORE_ADJ
                if self.container_type == "user"
                else DEFAULT_RESOURCE_OOM_SCORE_ADJ
            )
            object.__setattr__(self, "oom_score_adj", default)


@dataclass(frozen=True)
class CreatedContainer:
    """The result of :func:`create_hardened_container`.

    Attributes:
        container: the docker container object returned by ``containers.run``.
        name: the container name.
        container_type: the container type the spec requested.
        lifecycle_class: the lifecycle class the spec requested.
        network_mode: the network mode finally used.
        expected_hardening: the hardening envelope the container was created with.
    """

    container: Any
    name: str
    container_type: str
    lifecycle_class: str
    network_mode: str
    expected_hardening: HardeningRecipe


@dataclass(frozen=True)
class AdmissionContext:
    """The caller-owned context needed to run the admission gate.

    Attributes:
        workspace_id: the workspace the admission request is made for.
        session_id: the owning session, or ``None``.
        permissions: the caller's permission set, or ``None``.
        capabilities: the caller's capabilities, or ``None``.
        session_config: the caller's session configuration, or ``None``.
    """

    workspace_id: str
    session_id: str | None = None
    permissions: Any = None
    capabilities: Any = None
    session_config: Any = None


def create_hardened_container(
    client: Any,
    spec: ContainerCreateSpec,
    *,
    admission: AdmissionContext | None = None,
    record: bool = True,
) -> CreatedContainer:
    """Create one hardened container from *spec* and return its handle.

    The container is created with the spec's hardening envelope applied
    (``cap_drop``, ``security_opt``, ``read_only``, ``user``), the merged
    identity environment, the spec's tmpfs mounts and restart policy.

    When *admission* is provided the admission gate is run first: a
    :class:`~security.admission_gate.Deny` decision raises
    :class:`~security.admission_gate.AdmissionDenied`, and a
    :class:`~security.admission_gate.Transform` decision replaces the spec's
    ``network_mode`` before creation.

    When *record* is true and the spec carries a ``workspace_id``, the creation
    is wrapped in :func:`thoughtmachine.container_record.hook.record_creation`
    so the resulting container is attached to a durable record.

    Args:
        client: a docker client exposing ``containers.run``.
        spec: the container description.
        admission: admission context, or ``None`` to skip the gate.
        record: whether to record the creation against a container record.

    Returns:
        The :class:`CreatedContainer` wrapping the new container.
    """
    env = merge_container_identity_env(
        spec.environment, session_id=spec.session_id, workspace_id=spec.workspace_id
    )

    if admission is not None:
        admission_spec = ContainerSpec(
            container_type=spec.container_type,
            lifecycle_class=spec.lifecycle_class,
            workspace_id=admission.workspace_id,
            session_id=spec.session_id,
            image=spec.image,
            name=spec.name,
            mem_limit=spec.mem_limit,
            cpu_quota=spec.cpu_quota,
            oom_score_adj=spec.oom_score_adj,
            network_mode=spec.network_mode,
            read_only=spec.hardening.read_only,
        )
        request = AdmissionRequest(
            spec=admission_spec,
            permissions=admission.permissions,
            capabilities=admission.capabilities,
            session_config=admission.session_config,
        )
        decision = admit(request, probes=ClientProbes(client))
        if isinstance(decision, Deny):
            raise AdmissionDenied(decision.code, decision.message)
        if isinstance(decision, Transform):
            spec = replace(spec, network_mode=decision.spec.network_mode)

    if spec.mounts:
        from docker.types import Mount

        mounts = [
            Mount(target=m.target, source=m.source, type=m.type, read_only=m.read_only)
            for m in spec.mounts
        ]
    else:
        mounts = []

    run_labels = dict(spec.labels)

    def _run() -> Any:
        return client.containers.run(
            spec.image,
            list(spec.command),
            detach=True,
            tty=True,
            stdin_open=True,
            name=spec.name,
            cap_drop=list(spec.hardening.cap_drop),
            security_opt=list(spec.hardening.security_opt),
            read_only=spec.hardening.read_only,
            user=spec.hardening.user,
            oom_score_adj=spec.oom_score_adj,
            network_mode=spec.network_mode,
            mem_limit=spec.mem_limit,
            cpu_quota=spec.cpu_quota,
            tmpfs=dict(spec.tmpfs),
            labels=run_labels,
            environment=env,
            mounts=mounts,
            restart_policy=docker_restart_policy(spec.lifecycle_class),
        )

    if record and spec.workspace_id is not None:
        with record_creation(
            workspace_id=spec.workspace_id,
            lifecycle_class=spec.lifecycle_class,
            labels=run_labels,
            name=spec.name,
        ) as handle:
            container = _run()
            handle.attach(container)
    else:
        container = _run()

    return CreatedContainer(
        container=container,
        name=spec.name,
        container_type=spec.container_type,
        lifecycle_class=spec.lifecycle_class,
        network_mode=spec.network_mode,
        expected_hardening=spec.hardening,
    )
