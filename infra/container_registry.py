"""
Container Registry — central container lifecycle registry + hardened create path.

Phase 2 implementation (per the Infra Engineer dispatch): the registry owns a
per-session container map, the single hardened container-creation path
(``create_hardened_container``), per-session container-limit enforcement, the
resource-container guard, and permission-change reconciliation
(``on_permission_changed``).

Design doc: ``docs/container_registry_design.md`` (§2 "Core Design").  The
dispatch contract wins where it differs from the doc (profile shape, factory
signature, name-keyed registry, no event-bus wiring in this phase).

NOT wired into the existing managers yet (that is phase 3).  This module is
standalone and imports nothing from the rest of the tree.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Optional

import docker

__all__ = [
    "CONTAINER_TYPES",
    "ContainerProfile",
    "ContainerRegistry",
    "DEFAULT_COMMAND",
    "DEFAULT_CPU_QUOTA",
    "DEFAULT_IMAGE",
    "DEFAULT_MAX_CONTAINERS",
    "DEFAULT_MEM_LIMIT",
    "DEFAULT_RESOURCE_OOM_SCORE_ADJ",
    "DEFAULT_TMPFS",
    "DEFAULT_USER_OOM_SCORE_ADJ",
    "HARDENED_CAP_DROP",
    "HARDENED_READ_ONLY",
    "HARDENED_SECURITY_OPT",
    "HARDENED_USER",
    "RESOURCE_IMAGE_TAG",
    "STOP_TIMEOUT",
    "create_hardened_container",
    "get_container_registry",
    "is_container_registry_enabled",
]

log = logging.getLogger("infra.container_registry")
from agent.logging.lifecycle import log_container_event

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Centralized literal defaults (Phase A consolidation). Re-exported here so
# existing importers/tests keep seeing the same names on this module.
from agent.config.defaults import (
    RESOURCE_IMAGE_TAG,
    RESOURCE_LABEL,
    RESOURCE_KIND,
    WORKSPACE_ID_LABEL,
    CONTAINER_NAME_LABEL,
    RESOURCE_MEM_LIMIT,
    RESOURCE_CPU_QUOTA,
    DEFAULT_IMAGE,
    DEFAULT_MAX_CONTAINERS,
    CONTAINER_TYPES,
    DEFAULT_USER_OOM_SCORE_ADJ,
    DEFAULT_RESOURCE_OOM_SCORE_ADJ,
    HARDENED_CAP_DROP,
    HARDENED_SECURITY_OPT,
    HARDENED_READ_ONLY,
    HARDENED_USER,
    DEFAULT_TMPFS,
    DEFAULT_COMMAND,
    DEFAULT_MEM_LIMIT,
    DEFAULT_CPU_QUOTA,
    STOP_TIMEOUT,
)


@dataclass
class ContainerProfile:
    """Description of a hardened container.

    ``oom_score_adj`` uses a ``None`` sentinel: ``__post_init__`` fills the
    container-type default (1000 for ``user``, 500 otherwise) so callers can
    rely on a concrete value while still being able to override it.
    """

    image: str = DEFAULT_IMAGE
    command: list = field(default_factory=lambda: list(DEFAULT_COMMAND))
    container_type: str = "user"
    mem_limit: str = DEFAULT_MEM_LIMIT
    cpu_quota: int = DEFAULT_CPU_QUOTA
    oom_score_adj: Optional[int] = None
    network_mode: str = "none"
    labels: dict = field(default_factory=dict)
    environment: dict = field(default_factory=dict)
    mounts: list = field(default_factory=list)
    tmpfs: dict = field(default_factory=lambda: dict(DEFAULT_TMPFS))
    extra_hosts: dict = field(default_factory=dict)
    volumes: list = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.container_type not in CONTAINER_TYPES:
            raise ValueError(f"Unknown container type: {self.container_type!r}")
        if self.command is None:
            self.command = list(DEFAULT_COMMAND)
        if self.oom_score_adj is None:
            if self.container_type == "user":
                self.oom_score_adj = DEFAULT_USER_OOM_SCORE_ADJ
            else:
                self.oom_score_adj = DEFAULT_RESOURCE_OOM_SCORE_ADJ


def create_hardened_container(client, profile: ContainerProfile, container_name: str):
    """THE single hardened create path (design doc §2.3, dispatch form).

    Pure function: takes a docker client, a profile and a container name and
    runs ``client.containers.run`` with EVERY profile field plus the full
    hardening recipe.  ``profile.mounts`` entries (``{"source", "target",
    "mode": "rw"|"ro"}`` dicts) are converted to docker bind-mount dicts
    ``{"source", "target", "type": "bind", "read_only": bool}``.
    """
    mounts = [
        {
            "source": m["source"],
            "target": m["target"],
            "type": "bind",
            "read_only": str(m.get("mode", "rw")).lower() == "ro",
        }
        for m in profile.mounts
    ]
    return client.containers.run(
        profile.image,
        profile.command,
        detach=True,
        tty=True,
        stdin_open=True,
        name=container_name,
        cap_drop=list(HARDENED_CAP_DROP),
        security_opt=list(HARDENED_SECURITY_OPT),
        read_only=HARDENED_READ_ONLY,
        user=HARDENED_USER,
        oom_score_adj=profile.oom_score_adj,
        network_mode=profile.network_mode,
        mem_limit=profile.mem_limit,
        cpu_quota=profile.cpu_quota,
        tmpfs=dict(profile.tmpfs),
        labels=dict(profile.labels),
        environment=dict(profile.environment),
        extra_hosts=dict(profile.extra_hosts),
        volumes=list(profile.volumes),
        mounts=mounts,
    )


class ContainerRegistry:
    """Central per-session container registry (design doc §2.5, dispatch form).

    State:
      ``_containers``  name -> state dict {"profile", "status", "session_id",
                        "workspace_id", "created_at", "quarantined",
                        "container_id"}
      ``_session_map`` session_id -> set[container_name]
    All mutations go through ``self._lock``.
    """

    def __init__(self, docker_client=None, feature_flag_check=None):
        self._feature_flag_check = feature_flag_check
        self._docker_client = None
        self._docker_available = False
        self._containers: dict = {}
        self._session_map: dict = {}
        self._lock = threading.Lock()

        if docker_client is not None:
            self._docker_client = docker_client
            self._docker_available = True
        elif self.is_enabled():
            # Flag on and no injected client: connect to the host daemon.
            try:
                self._docker_client = docker.from_env()
                self._docker_available = True
            except docker.errors.DockerException as exc:
                log.warning("docker.from_env() failed; registry runs docker-less: %s", exc)
                self._docker_available = False
        # else: flag off — never touch docker.from_env (dispatch).

    # -- feature flag ----------------------------------------------------

    def is_enabled(self) -> bool:
        """True unless a feature_flag_check is installed and returns False."""
        if self._feature_flag_check is None:
            return True
        return bool(self._feature_flag_check())

    # -- lifecycle -------------------------------------------------------

    def register(self, container_name, session_id, workspace_id, container_type, profile) -> None:
        """Adopt a container into the registry.  ValueError on duplicate name
        or unknown container_type."""
        if container_type not in CONTAINER_TYPES:
            raise ValueError(f"Unknown container type: {container_type!r}")
        with self._lock:
            if container_name in self._containers:
                raise ValueError(f"Container already registered: {container_name}")
            self._containers[container_name] = {
                "profile": profile,
                "status": "registered",
                "session_id": session_id,
                "workspace_id": workspace_id,
                "container_type": container_type,
                "created_at": time.time(),
                "quarantined": False,
                "container_id": "",
            }
            self._session_map.setdefault(session_id, set()).add(container_name)
        log_container_event(
            "registered",
            container_id=container_name,
            session_id=session_id or "",
            data={"container_type": container_type},
        )

    def unregister(self, container_name) -> bool:
        """Drop a container from the registry.  Graceful: returns False (and
        logs a warning) when the name is not registered."""
        with self._lock:
            state = self._containers.pop(container_name, None)
            if state is None:
                log.warning("unregister: container %s is not registered", container_name)
                return False
            session_id = state["session_id"]
            names = self._session_map.get(session_id)
            if names is not None:
                names.discard(container_name)
                if not names:
                    del self._session_map[session_id]
            log.debug("unregistered container %s", container_name)
            return True

    def request_container(self, worker_id, session_id, permissions, *,
                          image=None, command=None, **kwargs) -> dict:
        """Create + register a hardened container.

        Mirrors WorkerSupervisor.request_container
        (workspace_lifecycle_manager.py L532-563).  ``kwargs`` may carry
        profile fields (``mem_limit``, ``cpu_quota``, ``oom_score_adj``,
        ``labels``, ``environment``, ``mounts``, ``tmpfs``, ``extra_hosts``,
        ``volumes``), plus ``container_type`` (default "user"),
        ``workspace_id`` (default "ws") and ``session_config`` (used for
        ``container_limits.max_containers``).

        Raises:
          RuntimeError  when the registry is disabled, has no docker client,
                        or the session's container limit is reached.
          PermissionError  for resource-container requests (image ==
                        RESOURCE_IMAGE_TAG, container_type == "resource", or
                        a tm-res-* name hint).
          ValueError   for an unknown container_type.

        Returns a handle dict: {"id", "name", "status": "running",
        "container_type"}.
        """
        if not self.is_enabled():
            raise RuntimeError("ContainerRegistry is disabled")
        if not self._docker_available or self._docker_client is None:
            raise RuntimeError("Docker client unavailable")

        container_type = kwargs.pop("container_type", "user")
        if container_type not in CONTAINER_TYPES:
            raise ValueError(f"Unknown container type: {container_type!r}")

        session_config = kwargs.pop("session_config", None)
        workspace_id = kwargs.pop("workspace_id", None) or "ws"

        # Resource guard (design doc §2.7; mirrors WLM L575-594): worker
        # sub-agents cannot request resource containers.
        name_hint = kwargs.get("name")
        if (
            image == RESOURCE_IMAGE_TAG
            or container_type == "resource"
            or (isinstance(name_hint, str) and name_hint.startswith("tm-res-"))
        ):
            raise PermissionError("Resource container access denied")

        # Limits: session config container_limits.max_containers -> default 4.
        max_containers = self._get_max_containers(session_id, session_config)
        with self._lock:
            current = len(self._session_map.get(session_id, ()))
        if current >= max_containers:
            raise RuntimeError("Container limit reached")

        # Permission-derived network mode wins over any kwargs network_mode.
        network_mode = self.resolve_network_mode(permissions)

        profile_kwargs = {}
        for field_name in (
            "mem_limit", "cpu_quota", "oom_score_adj", "labels", "environment",
            "mounts", "tmpfs", "extra_hosts", "volumes",
        ):
            value = kwargs.get(field_name)
            if value is not None:
                profile_kwargs[field_name] = value

        profile = ContainerProfile(
            image=image or DEFAULT_IMAGE,
            command=command,
            container_type=container_type,
            network_mode=network_mode,
            **profile_kwargs,
        )

        container_name = "tm-{}-{}-{}".format(
            container_type, workspace_id[:12], uuid.uuid4().hex[:8]
        )

        container = create_hardened_container(self._docker_client, profile, container_name)
        container_id = getattr(container, "id", "") or ""
        self.register(container_name, session_id, workspace_id, container_type, profile)
        with self._lock:
            state = self._containers.get(container_name)
            if state is not None:
                state["status"] = "running"
                state["container_id"] = container_id
        log.info(
            "request_container: created %s (type=%s, network=%s) for session %s",
            container_name, container_type, network_mode, session_id,
        )
        return {
            "id": container_id,
            "name": container_name,
            "status": "running",
            "container_type": container_type,
        }

    def create_resource_container(self, session_id, workspace_id, network_mode, *,
                                  workspace_path=None, mounts=None, name=None) -> dict:
        """Create + register the workspace's hidden git resource container.

        The privileged counterpart to ``request_container`` for the
        main-agent / ResourceContainerManager path (design doc §6): bypasses
        the resource guard because THIS method is the sanctioned
        resource-container factory (``request_container`` keeps rejecting
        ``tm-res-*`` / ``resource`` requests, §2.7).

        Mirrors ``ResourceContainerManager.ensure_container``'s fresh-create
        shape (resource_container_manager.py L404-546):
          name       tm-res-<sha256(workspace_path)[:12]>-git (or caller name)
          image      RESOURCE_IMAGE_TAG ("tm-resource-git")
          labels     thoughtmachine.resource=git + workspace/container-name
          mem/cpu    "512m" / 50000; oom_score_adj 500 (resource default)
          mounts     /workspace bind ALWAYS rw (added from workspace_path)
                     + caller-supplied extras (linked-worktree main repo rw,
                     resolved by the caller)
          no package volume / no PYTHONUSERBASE (absent by design, §6.2)
        ``network_mode`` defaults to "none" when falsy (fail closed, §6.3).

        The image is ensured via ``resource_container_manager._ensure_resource_image``
        (single-flight, success-cached, never raises — §6.1: the registry
        never builds/copies the Dockerfile itself); an unavailable image
        raises RuntimeError with the manual build command.

        Raises:
            RuntimeError  when disabled / no docker client / image missing.
            ValueError    when neither ``workspace_path`` nor ``name`` is
                          given (no deterministic name can be derived).

        Returns a handle dict {"id", "name", "status": "running",
        "container_type": "resource"}.
        """
        if not self.is_enabled():
            raise RuntimeError("ContainerRegistry is disabled")
        if not self._docker_available or self._docker_client is None:
            raise RuntimeError("Docker client unavailable")
        if name is None and not workspace_path:
            raise ValueError(
                "create_resource_container requires workspace_path (or name)"
            )
        if name is None:
            ws_hash = hashlib.sha256(workspace_path.encode("utf-8")).hexdigest()[:12]
            name = f"tm-res-{ws_hash}-git"

        self._ensure_resource_image_or_raise()

        profile_mounts = []
        if workspace_path:
            # The workspace bind is ALWAYS read-write for the git sandbox
            # (resource_container_manager.py L467-480); never caller-tunable.
            profile_mounts.append(
                {"source": workspace_path, "target": "/workspace", "mode": "rw"}
            )
        for extra in mounts or []:
            profile_mounts.append(dict(extra))

        profile = ContainerProfile(
            image=RESOURCE_IMAGE_TAG,
            command=list(DEFAULT_COMMAND),
            container_type="resource",
            network_mode=network_mode or "none",
            mem_limit=RESOURCE_MEM_LIMIT,
            cpu_quota=RESOURCE_CPU_QUOTA,
            labels={
                RESOURCE_LABEL: RESOURCE_KIND,
                WORKSPACE_ID_LABEL: str(workspace_id),
                CONTAINER_NAME_LABEL: name,
            },
            mounts=profile_mounts,
        )
        container = create_hardened_container(self._docker_client, profile, name)
        container_id = getattr(container, "id", "") or ""
        self.register(name, session_id, workspace_id, "resource", profile)
        with self._lock:
            state = self._containers.get(name)
            if state is not None:
                state["status"] = "running"
                state["container_id"] = container_id
        log.info(
            "create_resource_container: created %s (network=%s) for session %s",
            name, profile.network_mode, session_id,
        )
        return {
            "id": container_id,
            "name": name,
            "status": "running",
            "container_type": "resource",
        }

    def _ensure_resource_image_or_raise(self) -> None:
        """Ensure the resource git image exists (auto-build if needed).

        Lazy import of ``resource_container_manager._ensure_resource_image``
        keeps this module standalone at import time (design doc §6.1); a
        defensive fallback checks the local image via our own docker client.
        Never caches a failure here — raises RuntimeError with the manual
        build command instead.
        """
        try:
            from infra.resource_container_manager import _ensure_resource_image
        except Exception:  # pragma: no cover - defensive
            _ensure_resource_image = None
        if _ensure_resource_image is not None:
            if _ensure_resource_image():
                return
        else:
            try:
                self._docker_client.images.get(RESOURCE_IMAGE_TAG)
                return
            except (docker.errors.ImageNotFound, docker.errors.DockerException, AttributeError):
                pass
        raise RuntimeError(
            f"Resource image '{RESOURCE_IMAGE_TAG}' is not available "
            f"(auto-build failed or Docker unreachable). "
            f"Build it manually (two stages, vault build sources): "
            f"docker build -t tm-workspace-runtime:latest -f "
            f"~/.thoughtmachine/docker/resource/default_runtime.Dockerfile "
            f"~/.thoughtmachine/docker/resource; then docker build -t "
            f"tm-resource-git -f "
            f"~/.thoughtmachine/docker/resource/git_overlay.Dockerfile "
            f"--build-arg BASE_IMAGE=tm-workspace-runtime:latest "
            f"~/.thoughtmachine/docker/resource"
        )

    def get_containers_for_session(self, session_id) -> list:
        """Handles (id/name/status/container_type) for a session's containers."""
        with self._lock:
            items = [
                (name, dict(state))
                for name, state in self._containers.items()
                if name in self._session_map.get(session_id, ())
            ]
        return [self._to_handle(name, state) for name, state in sorted(items)]

    def list_all(self) -> list:
        """Handles for every registered container, sorted by name."""
        with self._lock:
            items = [(name, dict(state)) for name, state in self._containers.items()]
        return [self._to_handle(name, state) for name, state in sorted(items)]

    def destroy_container(self, container_name) -> None:
        """Graceful stop (timeout=10) -> remove -> unregister.

        When stop/remove raises a DockerException, falls back to
        remove(force=True) (log warning).  Always unregisters; a container
        that was never registered is a logged no-op.
        """
        if container_name not in self._containers:
            log.warning("destroy_container: %s is not registered; nothing to do", container_name)
            return
        session_id = (self._containers.get(container_name) or {}).get("session_id") or ""
        container = None
        try:
            container = self._docker_client.containers.get(container_name)
            container.stop(timeout=STOP_TIMEOUT)
            container.remove()
        except docker.errors.DockerException as exc:
            log.warning("destroy_container: stop/remove of %s failed: %s", container_name, exc)
            if container is not None:
                try:
                    container.remove(force=True)
                    log.info("destroy_container: force-removed %s", container_name)
                except Exception as exc2:  # noqa: BLE001 - best-effort cleanup
                    log.error(
                        "destroy_container: force-remove of %s also failed: %s",
                        container_name, exc2,
                    )
        finally:
            log_container_event("destroyed", container_id=container_name, session_id=session_id)
            self.unregister(container_name)

    # -- live permission sync -------------------------------------------

    def on_permission_changed(self, session_id, new_permissions) -> None:
        """Reconcile a session's containers after a permission change.

        Dispatch form: ``(session_id, new_permissions)`` — the event-bus
        wiring (phase 3) adapts the config_changed payload into these
        arguments.  Idempotent: no-op when the resolved network mode is
        unchanged.  Teardown failure -> quarantine (removed from
        ``_session_map``, never recreated next to a stale twin).  Recreate
        failure -> log, keep the slot, retry on the next event.  One failure
        never blocks the rest.
        """
        if not self.is_enabled():
            return
        new_mode = self.resolve_network_mode(new_permissions)
        with self._lock:
            names = list(self._session_map.get(session_id, ()))
        for name in names:
            with self._lock:
                state = self._containers.get(name)
            if state is None:
                continue
            if state["profile"].network_mode == new_mode:
                continue  # still compliant — idempotent no-op

            # Teardown: graceful stop -> remove.  Failure => quarantine.
            try:
                container = self._docker_client.containers.get(name)
                container.stop(timeout=STOP_TIMEOUT)
                container.remove()
            except Exception as exc:  # noqa: BLE001 - one failure never blocks the rest
                log.warning(
                    "on_permission_changed: teardown of %s failed; quarantining: %s",
                    name, exc,
                )
                with self._lock:
                    state["quarantined"] = True
                    state["status"] = "quarantined"
                    self._session_map.get(session_id, set()).discard(name)
                continue

            # Recreate with the SAME name and an updated profile.
            try:
                new_profile = replace(state["profile"], network_mode=new_mode)
                fresh = create_hardened_container(self._docker_client, new_profile, name)
            except Exception as exc:  # noqa: BLE001 - do not raise; retry next event
                log.error(
                    "on_permission_changed: recreate of %s failed: %s", name, exc,
                )
                with self._lock:
                    state["status"] = "recreate_failed"
                continue

            with self._lock:
                self._containers[name] = {
                    "profile": new_profile,
                    "status": "running",
                    "session_id": session_id,
                    "workspace_id": state["workspace_id"],
                    "container_type": state.get("container_type") or new_profile.container_type,
                    "created_at": time.time(),
                    "quarantined": False,
                    "container_id": getattr(fresh, "id", "") or "",
                }
            log.info(
                "on_permission_changed: recreated %s with network_mode=%s",
                name, new_mode,
            )

    # -- limits / helpers -----------------------------------------------

    def _get_max_containers(self, session_id, session_config=None) -> int:
        """Session config ``container_limits.max_containers`` -> default 4,
        clamped to >= 1 (mirrors container_manager.py L273-291)."""
        if session_config:
            limits = session_config.get("container_limits") or {}
            raw = limits.get("max_containers")
            if raw is not None:
                try:
                    return max(1, int(raw))
                except (TypeError, ValueError):
                    log.warning(
                        "invalid max_containers %r; using default %d",
                        raw, DEFAULT_MAX_CONTAINERS,
                    )
        return DEFAULT_MAX_CONTAINERS

    @staticmethod
    def resolve_network_mode(permissions) -> str:
        """Permission -> network_mode: network in (True, "write") -> bridge,
        else none (mirrors docker_executor L128-201 / security_gate L159-223)."""
        perms = permissions or {}
        return "bridge" if perms.get("network") in (True, "write") else "none"

    def _to_handle(self, name, state) -> dict:
        return {
            "id": state.get("container_id", "") or "",
            "name": name,
            "status": state["status"],
            "container_type": state.get("container_type") or state["profile"].container_type,
        }

    # -- resource image availability -------------------------------------

    def is_resource_image_available(self) -> bool:
        """True when the resource git image exists locally.  Never raises."""
        if not self._docker_available or self._docker_client is None:
            return False
        try:
            self._docker_client.images.get(RESOURCE_IMAGE_TAG)
            return True
        except (docker.errors.ImageNotFound, docker.errors.DockerException, AttributeError):
            return False


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def is_container_registry_enabled(session_config) -> bool:
    """Session config flag (default False); mirrors the
    ``use_workspace_lifecycle_manager`` pattern (workspace_lifecycle_manager.py
    L97-101)."""
    return bool((session_config or {}).get("use_container_registry", False))


def get_container_registry(docker_client=None, session_config=None) -> ContainerRegistry:
    """Factory helper.

    Disabled config -> a docker-less registry whose feature flag is always
    False (docker.from_env is never called).  Enabled config -> a live
    registry (connect to the host daemon when no client is injected).
    """
    if not is_container_registry_enabled(session_config):
        return ContainerRegistry(docker_client=None, feature_flag_check=lambda: False)
    return ContainerRegistry(docker_client=docker_client, feature_flag_check=None)
