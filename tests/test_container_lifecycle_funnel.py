"""Unit tests for the Wave 9a container-lifecycle funnel (STEP 1).

Covers the four decisions (use / refuse / stop / reap), the fail-closed
"cannot decide" path, and the invariant that EVERY decision emits a
``CONTAINER_LIFECYCLE`` event.

No production call site is migrated in STEP 1 -- these tests exercise
``infra.container_lifecycle.ContainerLifecycle`` directly.
"""

import pytest

from infra.container_lifecycle import (
    ContainerIntent,
    ContainerLifecycle,
    ReconcileDecision,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeContainer:
    """Minimal Docker-container stand-in that records stop/remove calls."""

    def __init__(
        self,
        *,
        name="tm-c1",
        cid="c1",
        status="running",
        labels=None,
        attrs=None,
        network_mode="none",
        mount_mode="ro",
        workspace_mount=True,
    ):
        self.name = name
        self.id = cid
        self.status = status
        self.labels = labels if labels is not None else {}
        if attrs is None:
            host_config = {}
            if network_mode is not None:
                host_config["NetworkMode"] = network_mode
            mounts = []
            if workspace_mount:
                mount = {"Destination": "/workspace"}
                if mount_mode is not None:
                    mount["Mode"] = mount_mode
                mounts.append(mount)
            attrs = {"HostConfig": host_config, "Mounts": mounts}
        self.attrs = attrs
        self.stop_calls = []
        self.remove_calls = []

    def stop(self, timeout=None):
        self.stop_calls.append({"timeout": timeout})

    def remove(self, force=False):
        self.remove_calls.append({"force": force})


class ExplodingContainer:
    """Container whose attrs/labels read raises -- forces the fail-closed path."""

    name = "tm-c1"
    id = "c1"

    @property
    def labels(self):  # pragma: no cover - exercised via reconcile
        raise RuntimeError("boom-labels")

    @property
    def attrs(self):  # pragma: no cover - exercised via reconcile
        raise RuntimeError("boom-attrs")


# ---------------------------------------------------------------------------
# (a) use -- matching envelope
# ---------------------------------------------------------------------------
def test_use_on_matching_envelope_from_session():
    container = FakeContainer(network_mode="none", mount_mode="ro")
    decision = ContainerLifecycle.reconcile(
        container, {"network": "none", "mode": "ro"}
    )
    assert isinstance(decision, ReconcileDecision)
    assert decision.action == "use"
    assert decision.ends_life is False
    assert decision.destroys is False
    assert container.stop_calls == []
    assert container.remove_calls == []


def test_use_on_matching_envelope_from_intent():
    container = FakeContainer(network_mode="bridge", mount_mode="rw")
    decision = ContainerLifecycle.reconcile(
        container,
        None,
        ContainerIntent(
            kind="use",
            desired_network="bridge",
            desired_workspace_mode="rw",
        ),
    )
    assert decision.action == "use"
    assert decision.contributing_source == "workspace"


def test_use_when_no_container():
    decision = ContainerLifecycle.reconcile(None, {"network": "none", "mode": "ro"})
    assert decision.action == "use"
    assert decision.ends_life is False


# ---------------------------------------------------------------------------
# (b) refuse -- permission-envelope mismatch on a workspace-owned container
# ---------------------------------------------------------------------------
def test_refuse_on_workspace_owned_mismatch():
    container = FakeContainer(network_mode="bridge", mount_mode="rw")
    decision = ContainerLifecycle.reconcile(
        container, {"network": "none", "mode": "ro"}
    )
    assert decision.action == "refuse"
    assert decision.destroys is False
    assert decision.ends_life is False
    # reconcile is read-only: never touches the container
    assert container.stop_calls == []
    assert container.remove_calls == []


def test_non_owned_mismatch_stops_not_reaps():
    container = FakeContainer(network_mode="bridge", mount_mode="rw")
    decision = ContainerLifecycle.reconcile(
        container,
        {"network": "none", "mode": "ro"},
        ContainerIntent(kind="use", workspace_owned=False),
    )
    assert decision.action == "stop"
    assert decision.ends_life is True
    assert decision.destroys is False


# ---------------------------------------------------------------------------
# (c) stop -- idle but runnable container
# ---------------------------------------------------------------------------
def test_stop_for_idle_running_container():
    container = FakeContainer(status="running")
    decision = ContainerLifecycle.reconcile(
        container, None, ContainerIntent(kind="idle", idle=True)
    )
    assert decision.action == "stop"
    assert decision.ends_life is True
    assert decision.destroys is False


def test_use_for_idle_already_stopped_container():
    container = FakeContainer(status="exited")
    decision = ContainerLifecycle.reconcile(
        container, None, ContainerIntent(kind="idle", idle=True)
    )
    assert decision.action == "use"
    assert decision.ends_life is False


# ---------------------------------------------------------------------------
# (d) reap -- TTL-expired / orphaned / explicit removal
# ---------------------------------------------------------------------------
def test_reap_for_ttl_expired():
    container = FakeContainer()
    decision = ContainerLifecycle.reconcile(
        container, None, ContainerIntent(kind="reap", ttl_expired=True)
    )
    assert decision.action == "reap"
    assert decision.destroys is True
    assert decision.ends_life is True


def test_reap_for_orphaned():
    container = FakeContainer()
    decision = ContainerLifecycle.reconcile(
        container, None, ContainerIntent(kind="reap", orphaned=True)
    )
    assert decision.action == "reap"
    assert decision.contributing_source == "record"


def test_reap_for_explicit_removal():
    container = FakeContainer()
    decision = ContainerLifecycle.reconcile(
        container,
        None,
        ContainerIntent(kind="reap", explicit=True, reason="removed"),
    )
    assert decision.action == "reap"
    assert decision.reason == "removed"


# ---------------------------------------------------------------------------
# (e) fail-closed -- refuse (never destroy) when it cannot decide
# ---------------------------------------------------------------------------
def test_refuse_when_desired_envelope_undeterminable():
    container = FakeContainer()
    # No intent envelope, no session dict -> cannot determine what is desired.
    decision = ContainerLifecycle.reconcile(container, None)
    assert decision.action == "refuse"
    assert decision.destroys is False
    assert container.stop_calls == []
    assert container.remove_calls == []


def test_refuse_when_actual_envelope_unreadable():
    # HostConfig present but no NetworkMode -> structurally unreadable.
    container = FakeContainer(network_mode=None)
    decision = ContainerLifecycle.reconcile(
        container, {"network": "none", "mode": "ro"}
    )
    assert decision.action == "refuse"


def test_refuse_when_decide_raises():
    container = ExplodingContainer()
    decision = ContainerLifecycle.reconcile(
        container, {"network": "none", "mode": "ro"}
    )
    assert decision.action == "refuse"
    assert "failing closed" in decision.reason


def test_resource_container_yields_use_without_explicit_intent():
    container = FakeContainer(name="tm-res-shared")
    decision = ContainerLifecycle.reconcile(container, None)
    assert decision.action == "use"
    assert decision.contributing_source == "resource"


def test_resource_container_matched_by_label():
    container = FakeContainer(
        name="tm-c1", labels={"thoughtmachine.resource": "true"}
    )
    decision = ContainerLifecycle.reconcile(container, None)
    assert decision.action == "use"
    assert decision.contributing_source == "resource"


# ---------------------------------------------------------------------------
# (f) every decision emits a CONTAINER_LIFECYCLE event
# ---------------------------------------------------------------------------
def test_every_decision_emits_event(monkeypatch):
    from agent.events import EventType, global_event_bus

    assert global_event_bus is not None, "expected a live global event bus"

    captured = []
    monkeypatch.setattr(global_event_bus, "publish", lambda ev: captured.append(ev))

    match = FakeContainer(network_mode="none", mount_mode="ro")
    mismatch = FakeContainer(network_mode="bridge", mount_mode="rw")
    idle = FakeContainer(status="running")
    ttl = FakeContainer()
    explode = ExplodingContainer()

    scenarios = [
        lambda: ContainerLifecycle.reconcile(match, {"network": "none", "mode": "ro"}),
        lambda: ContainerLifecycle.reconcile(mismatch, {"network": "none", "mode": "ro"}),
        lambda: ContainerLifecycle.reconcile(idle, None, ContainerIntent(kind="idle", idle=True)),
        lambda: ContainerLifecycle.reconcile(ttl, None, ContainerIntent(kind="reap", ttl_expired=True)),
        lambda: ContainerLifecycle.reconcile(explode, {"network": "none", "mode": "ro"}),
    ]
    decisions = [scenario() for scenario in scenarios]

    assert len(captured) == len(scenarios)
    for ev in captured:
        assert ev.type == EventType.CONTAINER_LIFECYCLE
        assert ev.data["event_type"] == "container_lifecycle_decision"
        assert "action" in ev.data
        assert "reason" in ev.data
        assert "contributing_source" in ev.data

    # The event mirrors the decision's action 1:1, in order.
    assert [ev.data["action"] for ev in captured] == [d.action for d in decisions]


def test_decision_event_is_attached(monkeypatch):
    from agent.events import global_event_bus

    monkeypatch.setattr(global_event_bus, "publish", lambda ev: None)
    decision = ContainerLifecycle.reconcile(FakeContainer(), None)
    assert decision.event is not None
    assert decision.event["event_type"] == "container_lifecycle_decision"
    assert decision.event["action"] == decision.action


# ---------------------------------------------------------------------------
# apply() executes the decision (and only then)
# ---------------------------------------------------------------------------
def test_apply_reaps_container():
    container = FakeContainer(status="running")
    decision = ContainerLifecycle.apply(
        container,
        None,
        ContainerIntent(kind="reap", explicit=True, force=True, timeout=7),
    )
    assert decision.action == "reap"
    assert container.stop_calls == [{"timeout": 7}]
    assert container.remove_calls == [{"force": True}]


def test_apply_does_nothing_on_refuse():
    container = FakeContainer(network_mode="bridge", mount_mode="rw")
    decision = ContainerLifecycle.apply(container, {"network": "none", "mode": "ro"})
    assert decision.action == "refuse"
    assert container.stop_calls == []
    assert container.remove_calls == []


def test_apply_stops_but_does_not_remove_on_stop_decision():
    container = FakeContainer(status="running")
    decision = ContainerLifecycle.apply(
        container, None, ContainerIntent(kind="idle", idle=True)
    )
    assert decision.action == "stop"
    assert container.stop_calls == [{"timeout": 5}]
    assert container.remove_calls == []


def test_apply_swallows_lifecycle_errors():
    def boom(*_args, **_kwargs):
        raise RuntimeError("docker gone")

    container = FakeContainer()
    container.stop = boom
    container.remove = boom
    # Must not raise.
    decision = ContainerLifecycle.apply(
        container, None, ContainerIntent(kind="reap", explicit=True)
    )
    assert decision.action == "reap"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
