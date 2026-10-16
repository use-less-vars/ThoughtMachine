"""Container lifecycle reconciliation funnel (Wave 9a).

Single decision point for every state transition of a Docker container owned
or observed by ThoughtMachine.  Callers describe *intent* (what they want) and
the funnel decides the *action*, emits a ``CONTAINER_LIFECYCLE`` event for
every decision, and (via :meth:`ContainerLifecycle.apply`) executes
``stop``/``remove``.

The funnel is deliberately conservative: a workspace-owned container whose
envelope cannot be shown to match the desired envelope is **refused**, never
destroyed.  Destruction of a workspace-owned container requires an explicit
removal intent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from agent.logging import log


_RESOURCE_NAME_PREFIX = "tm-res-"
_RESOURCE_LABEL = "thoughtmachine.resource"


def _safe_attr(obj: Any, name: str) -> Any:
    """``getattr`` that never raises (returns ``None`` on any error)."""
    try:
        return getattr(obj, name, None)
    except Exception:  # noqa: BLE001 - a probe must never raise
        return None


def _as_str(value: Any) -> Optional[str]:
    """Coerce ``value`` to ``str`` (``None`` passes through)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


@dataclass
class ReconcileDecision:
    """Outcome of a reconciliation: what to do, why, and who contributed."""

    action: str
    reason: str
    contributing_source: str
    event: Optional[dict] = None

    @property
    def ends_life(self) -> bool:
        """True when the decision terminates the container's life."""
        return self.action in {"stop", "reap"}

    @property
    def destroys(self) -> bool:
        """True when the decision removes (destroys) the container."""
        return self.action == "reap"


@dataclass
class ContainerIntent:
    """What a caller wants to happen to a container.

    ``kind`` is a free-form label (``"use"``, ``"reap"``, ``"idle"`` ...) used
    only for observability.  The decision is driven by the boolean/intent
    fields, applied in a fixed precedence order by
    :meth:`ContainerLifecycle._decide`.
    """

    kind: str = "use"
    desired_network: Optional[str] = None
    desired_workspace_mode: Optional[str] = None
    workspace_owned: bool = True
    orphaned: bool = False
    ttl_expired: bool = False
    idle: bool = False
    explicit: bool = False
    force: bool = False
    reason: str = ""
    workspace_id: Optional[str] = None
    session_id: Optional[str] = None
    timeout: int = 5


class ContainerLifecycle:
    """Stateless decision funnel; all operations are classmethods."""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @classmethod
    def reconcile(
        cls,
        container: Any,
        session: Any,
        intent: Optional[ContainerIntent] = None,
        *,
        emit: bool = True,
    ) -> ReconcileDecision:
        """Decide what should happen to ``container``; never touches it.

        Always emits one ``CONTAINER_LIFECYCLE`` event, even when the decision
        is a failed-closed ``refuse``.  ``emit=False`` computes the decision
        WITHOUT publishing an event -- used by callers that record their own,
        differently-shaped action (e.g. a restart).
        """
        intent = intent or ContainerIntent()
        try:
            decision = cls._decide(container, session, intent)
        except Exception as exc:  # noqa: BLE001 - fail closed on any error
            decision = ReconcileDecision(
                "refuse",
                f"cannot decide ({exc}); failing closed",
                "default",
            )
        try:
            event = cls._build_event(decision, container, intent)
            decision.event = event
        except Exception:  # noqa: BLE001 - telemetry must never break a decision
            event = None
            decision.event = None
        if emit and event is not None:
            cls._emit(event)
        return decision

    @classmethod
    def apply(
        cls,
        container: Any,
        session: Any,
        intent: Optional[ContainerIntent] = None,
    ) -> ReconcileDecision:
        """Reconcile then execute the resulting stop/remove, best effort."""
        intent = intent or ContainerIntent()
        decision = cls.reconcile(container, session, intent)
        if decision.ends_life and container is not None:
            timeout = intent.timeout or 5
            try:
                container.stop(timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                log("WARNING", "container_lifecycle", f"container stop failed: {exc}")
        if decision.destroys and container is not None:
            try:
                container.remove(force=bool(intent.force))
            except Exception as exc:  # noqa: BLE001
                log("WARNING", "container_lifecycle", f"container remove failed: {exc}")
        return decision

    # ------------------------------------------------------------------
    # Non-destructive lifecycle actions (create / restart)
    # ------------------------------------------------------------------
    @classmethod
    def _record_action(
        cls,
        action: str,
        container: Any,
        *,
        reason: str,
        contributing_source: str,
        workspace_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Optional[dict]:
        """Emit a non-destructive ``CONTAINER_LIFECYCLE`` event for ``action``.

        ``create``/``restart`` do not terminate a container's life, so they are
        recorded directly (never through the destructive ``_decide``
        precedence), which gives each its own distinguishable ``action``.
        """
        intent = ContainerIntent(
            kind=action,
            reason=reason,
            workspace_id=workspace_id,
            session_id=session_id,
        )
        decision = ReconcileDecision(action, reason, contributing_source)
        event: Optional[dict]
        try:
            event = cls._build_event(decision, container, intent)
            decision.event = event
        except Exception:  # noqa: BLE001 - telemetry must never break a caller
            event = None
            decision.event = None
        if event is not None:
            cls._emit(event)
        return decision.event

    @classmethod
    def record_create(
        cls,
        container: Any,
        *,
        reason: str = "container_created",
        contributing_source: str = "container_manager.create",
        workspace_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Optional[dict]:
        """Record that ``container`` was CREATED (``action="create"``)."""
        return cls._record_action(
            "create",
            container,
            reason=reason,
            contributing_source=contributing_source,
            workspace_id=workspace_id,
            session_id=session_id,
        )

    @classmethod
    def record_restart(
        cls,
        container: Any,
        *,
        reason: str = "explicit_restart",
        contributing_source: str = "container_manager.restart",
        workspace_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Optional[dict]:
        """Record that ``container`` was RESTARTED (``action="restart"``)."""
        return cls._record_action(
            "restart",
            container,
            reason=reason,
            contributing_source=contributing_source,
            workspace_id=workspace_id,
            session_id=session_id,
        )

    # ------------------------------------------------------------------
    # Decision
    # ------------------------------------------------------------------
    @classmethod
    def _decide(
        cls,
        container: Any,
        session: Any,
        intent: ContainerIntent,
    ) -> ReconcileDecision:
        # (1) Explicit removal always wins.
        if intent.explicit:
            return ReconcileDecision(
                "reap", intent.reason or "explicit removal", "workspace"
            )
        # (2) Resource containers are not owned here.
        if cls._is_resource_container(container):
            return ReconcileDecision(
                "use", "resource container; not owned here", "resource"
            )
        # (3) Orphaned (recorded but unowned) containers are reaped.
        if intent.orphaned:
            return ReconcileDecision(
                "reap", intent.reason or "orphaned container", "record"
            )
        # (4) TTL-expired containers are reaped.
        if intent.ttl_expired:
            return ReconcileDecision(
                "reap", intent.reason or "ttl expired", "workspace"
            )
        # (5) Idle handling: only stop a running container.
        if intent.idle:
            if container is None:
                return ReconcileDecision(
                    "use", "no container; nothing to stop", "default"
                )
            if cls._container_status(container) == "running":
                return ReconcileDecision(
                    "stop", intent.reason or "idle container", "workspace"
                )
            return ReconcileDecision(
                "use", "idle container already stopped", "workspace"
            )
        # (6) Nothing to reconcile.
        if container is None:
            return ReconcileDecision("use", "no container to reconcile", "default")
        # (7) Desired envelope.
        desired = cls._desired_envelope(session, intent)
        if desired is None:
            return ReconcileDecision(
                "refuse", "cannot determine desired envelope", "default"
            )
        desired_env, dsource = desired
        # (8) Actual envelope.
        actual = cls._actual_envelope(container)
        if actual is None:
            return ReconcileDecision(
                "refuse", "cannot read actual container envelope", "default"
            )
        # (9) Match.
        if actual == desired_env:
            return ReconcileDecision("use", "envelope matches", dsource)
        # (10) Mismatch on a workspace-owned container: refuse to destroy.
        if intent.workspace_owned:
            return ReconcileDecision(
                "refuse",
                "workspace-owned envelope mismatch; refusing to destroy",
                dsource,
            )
        # (11) Mismatch on a non-owned container: stop it.
        return ReconcileDecision(
            "stop", "envelope mismatch; stopping non-owned container", "session"
        )

    # ------------------------------------------------------------------
    # Envelope helpers
    # ------------------------------------------------------------------
    @classmethod
    def _desired_envelope(
        cls,
        session: Any,
        intent: ContainerIntent,
    ) -> Optional[Tuple[Tuple[str, str], str]]:
        if (
            intent.desired_network is not None
            and intent.desired_workspace_mode is not None
        ):
            return (intent.desired_network, intent.desired_workspace_mode), "workspace"
        if isinstance(session, dict):
            net = None
            for key in ("network", "network_mode", "desired_network"):
                if session.get(key) is not None:
                    net = session.get(key)
                    break
            mode = None
            for key in ("mode", "workspace_mode", "desired_mode"):
                if session.get(key) is not None:
                    mode = session.get(key)
                    break
            if net is not None and mode is not None:
                return (net, mode), "session"
        return None

    @classmethod
    def _actual_envelope(cls, container: Any) -> Optional[Tuple[str, str]]:
        attrs = getattr(container, "attrs", None)
        if not isinstance(attrs, dict):
            return None
        host_config = attrs.get("HostConfig")
        if not isinstance(host_config, dict):
            return None
        net = host_config.get("NetworkMode")
        if not isinstance(net, str):
            return None
        mode = "ro"
        mounts = attrs.get("Mounts")
        if isinstance(mounts, list):
            for mount in mounts:
                if isinstance(mount, dict) and mount.get("Destination") == "/workspace":
                    m = mount.get("Mode")
                    if isinstance(m, str):
                        mode = m
        return (net, mode)

    # ------------------------------------------------------------------
    # Container helpers
    # ------------------------------------------------------------------
    @classmethod
    def _is_resource_container(cls, container: Any) -> bool:
        if container is None:
            return False
        name = _safe_attr(container, "name")
        if isinstance(name, str) and name.lstrip("/").startswith(_RESOURCE_NAME_PREFIX):
            return True
        labels = _safe_attr(container, "labels")
        if isinstance(labels, dict) and labels.get(_RESOURCE_LABEL):
            return True
        attrs = _safe_attr(container, "attrs")
        if isinstance(attrs, dict):
            config = attrs.get("Config")
            if isinstance(config, dict):
                cfg_labels = config.get("Labels")
                if isinstance(cfg_labels, dict) and cfg_labels.get(_RESOURCE_LABEL):
                    return True
        return False

    @classmethod
    def _container_status(cls, container: Any) -> Optional[str]:
        if container is None:
            return None
        status = _safe_attr(container, "status")
        if isinstance(status, str):
            return status
        attrs = _safe_attr(container, "attrs")
        if isinstance(attrs, dict):
            state = attrs.get("State")
            if isinstance(state, dict):
                s = state.get("Status")
                if isinstance(s, str):
                    return s
        return None

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------
    @classmethod
    def _build_event(
        cls,
        decision: ReconcileDecision,
        container: Any,
        intent: ContainerIntent,
    ) -> dict:
        if container is None:
            cid: Any = None
            cname: Any = None
        else:
            cid = _safe_attr(container, "id")
            cname = _safe_attr(container, "name")
        return {
            "event_type": "container_lifecycle_decision",
            "action": decision.action,
            "reason": decision.reason,
            "contributing_source": decision.contributing_source,
            "intent_kind": intent.kind,
            "container_id": _as_str(cid),
            "container_name": _as_str(cname),
            "container_status": _as_str(cls._container_status(container)),
            "workspace_id": intent.workspace_id,
            "session_id": intent.session_id,
        }

    @classmethod
    def _emit(cls, event: dict) -> None:
        try:
            from agent.events import EventType, create_event, global_event_bus
        except Exception:  # noqa: BLE001 - best-effort telemetry
            return
        if global_event_bus is None:
            return
        try:
            ev = create_event(
                EventType.CONTAINER_LIFECYCLE,
                event,
                source="infra.container_lifecycle",
                session_id=event.get("session_id"),
            )
            global_event_bus.publish(ev)
        except Exception:  # noqa: BLE001
            pass
