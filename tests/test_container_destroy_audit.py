"""Destroy-audit telemetry: ``CONTAINER_DESTROY`` at every destroy site.

The ``CONTAINER_LIFECYCLE`` event records the DECISION; this suite pins the
DESTRUCTION.  The invariants:

  * a destroy that PROCEEDS emits exactly ONE ``CONTAINER_DESTROY`` event whose
    message carries the six reconcilable fields, in order --
    ``reason`` / ``source`` / ``container_id`` / ``record_id`` / ``name`` /
    ``state`` -- with ``state`` sampled BEFORE the stop (the real pre-destroy
    state, not the post-stop shell);
  * a REFUSED destroy emits its own event with ``reason=refused`` plus a
    ``detail="action=... decision_reason=..."`` tail appended AFTER the six
    fields, so a refusal can never be mistaken for a destruction and the
    destroy trail never claims a container it did not destroy;
  * a destroy of an already-vanished container still reports ``state=absent``
    (the destroy happened, just not here).

Sites exercised: ``ContainerManager.remove`` (destroy / absent / refused),
``DockerExecutor._reap_container`` (destroy / refused) and
``DockerExecutor.close`` (destroy / refused).
"""

from __future__ import annotations

import inspect
import re
from unittest.mock import MagicMock, patch

import pytest
from docker.errors import NotFound

import infra.container_manager as container_manager
import infra.containers.executor as executor_module
from infra.container_lifecycle import ContainerLifecycle, ReconcileDecision
from infra.container_manager import RECORD_LABEL_KEY, ContainerManager

WORKSPACE_ID = "ws-destroy-audit"
RECORD_ID = "rec-destroy-audit"
CONTAINER_ID = "c-destroy-audit"
CONTAINER_NAME = "tm-destroy-audit"

#: The six required fields, in their documented order.
REQUIRED_FIELDS = ("reason", "source", "container_id", "record_id", "name", "state")

_FIELD_RE = re.compile(r'(\w+)=("[^"]*"|\S+)')


# ---------------------------------------------------------------------------
# Capture / parsing helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def audit(monkeypatch):
    """Capture every audit event emitted by either module's ``_audit`` seam."""
    captured = []

    def _record(event, data):
        captured.append((event, data))

    monkeypatch.setattr(container_manager, "_audit", _record)
    monkeypatch.setattr(executor_module, "_audit", _record)
    return captured


def _destroy_messages(audit):
    """Every ``CONTAINER_DESTROY`` message captured, in emission order."""
    assert container_manager._DESTROY_AUDIT == "CONTAINER_DESTROY"
    assert executor_module._DESTROY_AUDIT == container_manager._DESTROY_AUDIT
    return [data for event, data in audit if event == "CONTAINER_DESTROY"]


def _fields(message):
    """Parse ``key=value`` / ``key="quoted value"`` pairs out of *message*."""
    parsed = {}
    for key, value in _FIELD_RE.findall(message):
        if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        parsed.setdefault(key, value)
    return parsed


def _one_destroy(audit, source, reason):
    """Assert exactly ONE destroy event, from *source* with *reason*."""
    messages = _destroy_messages(audit)
    assert len(messages) == 1, f"expected exactly ONE CONTAINER_DESTROY, got {messages}"
    message = messages[0]
    fields = _fields(message)
    assert fields["reason"] == reason
    assert fields["source"] == source
    return message, fields


def _assert_field_order(message):
    positions = [message.index(f"{key}=") for key in REQUIRED_FIELDS]
    assert positions == sorted(positions), f"field order drifted: {message}"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeContainer:
    """Minimal docker-container stand-in (real inspect ``attrs`` shape)."""

    def __init__(self, cid=CONTAINER_ID, name=CONTAINER_NAME, status="running",
                 labels=None):
        self.id = cid
        self.name = name
        self.status = status
        self.labels = dict(labels or {})
        self.attrs = {
            "Id": cid,
            "State": {"Status": status, "Running": status == "running"},
        }
        self.stop_calls = 0
        self.kill_calls = 0
        self.remove_calls = 0

    def reload(self):
        return None

    def stop(self, timeout=None, **kwargs):
        self.stop_calls += 1

    def kill(self):
        self.kill_calls += 1

    def remove(self, force=False, **kwargs):
        self.remove_calls += 1


class _FakeContainers:
    def __init__(self, containers):
        self._items = list(containers)

    def get(self, ref):
        for container in self._items:
            if ref in (container.id, container.name):
                return container
        raise NotFound(f"no such container: {ref}")

    def list(self, all=False, filters=None):  # noqa: A002 - docker SDK signature
        return list(self._items)


class _FakeClient:
    def __init__(self, containers):
        self.containers = _FakeContainers(containers)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """Keep the removal paths off the vault / tenant event bus / log sinks."""
    monkeypatch.setattr(container_manager, "_reap_bound_record",
                        lambda *a, **k: None)
    monkeypatch.setattr(container_manager, "_emit_container_removed",
                        lambda *a, **k: None)
    monkeypatch.setattr(container_manager, "log_container_event",
                        lambda *a, **k: None)


def _manager(containers):
    """A ``ContainerManager`` with only the attributes ``remove`` reads."""
    mgr = ContainerManager.__new__(ContainerManager)
    mgr.workspace_id = WORKSPACE_ID
    mgr.session_id = "sess-destroy-audit"
    mgr.session_permissions = {"container": True}
    mgr.client = _FakeClient(containers)
    mgr._containers = {}
    # The lifecycle class + agent-access gate are orthogonal here (and would
    # otherwise probe docker / the record store); the decision itself comes
    # from the real funnel.
    mgr.class_of = lambda container: "persistent"
    mgr._agent_access_denial = lambda lifecycle_class: None
    return mgr


def _make_executor(session_permissions=None):
    from infra.containers.executor import DockerExecutor

    client = MagicMock()
    with patch("infra.containers.executor.docker.from_env", return_value=client):
        return DockerExecutor(
            workspace_path="/tmp/tm-destroy-audit-ws",
            image="agent-executor-test",
            network="none",
            mem_limit="128m",
            cpu_quota=50000,
            force_rebuild=False,
            idle_timeout=600,
            session_permissions=session_permissions or {},
            workspace_id=WORKSPACE_ID,
        )


def _refuse(monkeypatch, reason="refused(unit-test)"):
    """Force a non-destroy decision out of the funnel."""
    fake = ReconcileDecision("refuse", reason, "default")
    monkeypatch.setattr(
        ContainerLifecycle, "reconcile", classmethod(lambda *a, **k: fake)
    )
    return fake


# ===========================================================================
# ContainerManager.remove -- destroy
# ===========================================================================
def test_remove_destroy_emits_one_destroy_event(audit):
    container = FakeContainer(labels={RECORD_LABEL_KEY: RECORD_ID})
    mgr = _manager([container])

    result = mgr.remove(CONTAINER_ID)

    assert result == {"status": "removed", "container_id": CONTAINER_ID}
    assert container.remove_calls == 1
    message, fields = _one_destroy(audit, "container_manager.remove",
                                   "explicit_remove")
    assert fields["container_id"] == CONTAINER_ID
    assert fields["record_id"] == RECORD_ID
    assert fields["name"] == CONTAINER_NAME
    assert fields["state"] == "running"
    _assert_field_order(message)
    assert 'detail="' not in message


def test_remove_destroy_state_is_sampled_before_the_stop(audit):
    """The audited state is the PRE-destroy state, not the stopped shell."""
    container = FakeContainer(labels={RECORD_LABEL_KEY: RECORD_ID})
    mgr = _manager([container])

    mgr.remove(CONTAINER_ID)

    _, fields = _one_destroy(audit, "container_manager.remove", "explicit_remove")
    # remove() stops the container (status -> exiting/exited) yet the audit
    # still reports the state that was actually destroyed.
    assert container.stop_calls == 1
    assert fields["state"] == "running"


# ===========================================================================
# ContainerManager.remove -- already absent
# ===========================================================================
def test_remove_absent_container_reports_state_absent(audit):
    mgr = _manager([])

    result = mgr.remove("c-vanished")

    assert result == {"status": "removed", "container_id": "c-vanished"}
    _, fields = _one_destroy(audit, "container_manager.remove", "explicit_remove")
    assert fields["state"] == "absent"
    assert fields["container_id"] == "c-vanished"
    assert fields["record_id"] == "unknown"
    assert fields["name"] == "unknown"


# ===========================================================================
# ContainerManager.remove -- refused
# ===========================================================================
def test_remove_refusal_emits_refused_event(audit, monkeypatch):
    decision = _refuse(monkeypatch)
    container = FakeContainer(labels={RECORD_LABEL_KEY: RECORD_ID})
    mgr = _manager([container])

    result = mgr.remove(CONTAINER_ID)

    # Return shape unchanged by the audit.
    assert result == {"status": "refused", "container_id": CONTAINER_ID,
                      "reason": decision.reason}
    # The container survives: no stop, no destroy.
    assert container.stop_calls == 0
    assert container.remove_calls == 0
    message, fields = _one_destroy(audit, "container_manager.remove", "refused")
    assert fields["container_id"] == CONTAINER_ID
    assert fields["record_id"] == RECORD_ID
    assert fields["name"] == CONTAINER_NAME
    assert fields["state"] == "running"
    _assert_field_order(message)
    # The refusing decision is appended AFTER the six required fields.
    assert message.endswith(
        f'detail="action=refuse decision_reason={decision.reason}"'
    )


# ===========================================================================
# ContainerManager.remove -- audit labels (source / reason)
# ===========================================================================
def test_remove_default_audit_labels_are_the_documented_pair(audit):
    """A plain ``remove()`` still reports reason=explicit_remove."""
    params = inspect.signature(ContainerManager.remove).parameters
    assert params["keep_record"].default is False
    assert params["audit_reason"].default == "explicit_remove"
    assert params["audit_source"].default == "container_manager.remove"

    container = FakeContainer(labels={RECORD_LABEL_KEY: RECORD_ID})
    mgr = _manager([container])

    mgr.remove(CONTAINER_ID)

    message, fields = _one_destroy(audit, "container_manager.remove",
                                   "explicit_remove")
    assert fields["container_id"] == CONTAINER_ID
    assert fields["record_id"] == RECORD_ID
    _assert_field_order(message)


def test_remove_audit_labels_can_be_overridden(audit):
    """A caller-injected label lands verbatim on the ONE destroy event."""
    container = FakeContainer(labels={RECORD_LABEL_KEY: RECORD_ID})
    mgr = _manager([container])

    result = mgr.remove(
        CONTAINER_ID, keep_record=True,
        audit_reason="resize_recreate",
        audit_source="server.workspace_container_resources")

    assert result == {"status": "removed", "container_id": CONTAINER_ID}
    assert container.remove_calls == 1
    message, fields = _one_destroy(audit,
                                   "server.workspace_container_resources",
                                   "resize_recreate")
    assert fields["container_id"] == CONTAINER_ID
    assert fields["record_id"] == RECORD_ID
    assert fields["name"] == CONTAINER_NAME
    assert fields["state"] == "running"
    _assert_field_order(message)


def test_remove_refusal_keeps_the_refused_reason_under_an_override(
        audit, monkeypatch):
    """An override relabels the SOURCE but can never claim a destroy."""
    decision = _refuse(monkeypatch)
    container = FakeContainer(labels={RECORD_LABEL_KEY: RECORD_ID})
    mgr = _manager([container])

    result = mgr.remove(
        CONTAINER_ID, keep_record=True,
        audit_reason="resize_recreate",
        audit_source="server.workspace_container_resources")

    assert result == {"status": "refused", "container_id": CONTAINER_ID,
                      "reason": decision.reason}
    assert container.stop_calls == 0
    assert container.remove_calls == 0
    message, fields = _one_destroy(audit,
                                   "server.workspace_container_resources",
                                   "refused")
    _assert_field_order(message)
    assert message.endswith(
        f'detail="action=refuse decision_reason={decision.reason}"')


# ===========================================================================
# DockerExecutor._reap_container -- destroy / refused
# ===========================================================================
def test_reap_container_destroy_emits_one_destroy_event(audit):
    container = FakeContainer(labels={RECORD_LABEL_KEY: RECORD_ID})
    executor = _make_executor()

    executor._reap_container(container)

    assert container.stop_calls == 1
    assert container.remove_calls == 1
    message, fields = _one_destroy(audit, "executor.reap",
                                   "ensure_container_recreate")
    assert fields["container_id"] == CONTAINER_ID
    assert fields["record_id"] == RECORD_ID
    assert fields["name"] == CONTAINER_NAME
    assert fields["state"] == "running"
    _assert_field_order(message)


def test_reap_container_refusal_claims_no_destroy(audit, monkeypatch):
    decision = _refuse(monkeypatch)
    container = FakeContainer(labels={RECORD_LABEL_KEY: RECORD_ID})
    executor = _make_executor()

    executor._reap_container(container)

    assert container.stop_calls == 0
    assert container.remove_calls == 0
    message, fields = _one_destroy(audit, "executor.reap", "refused")
    assert fields["container_id"] == CONTAINER_ID
    assert message.endswith(
        f'detail="action=refuse decision_reason={decision.reason}"'
    )


# ===========================================================================
# DockerExecutor.close -- destroy / refused
# ===========================================================================
def test_close_destroy_emits_one_destroy_event(audit):
    container = FakeContainer(labels={RECORD_LABEL_KEY: RECORD_ID})
    executor = _make_executor()
    executor.container = container

    executor.close()

    assert container.remove_calls == 1
    assert executor.container is None
    message, fields = _one_destroy(audit, "executor.close", "executor_close")
    assert fields["container_id"] == CONTAINER_ID
    assert fields["record_id"] == RECORD_ID
    assert fields["name"] == CONTAINER_NAME
    assert fields["state"] == "running"
    _assert_field_order(message)


def test_close_refusal_claims_no_destroy(audit, monkeypatch):
    decision = _refuse(monkeypatch)
    container = FakeContainer(labels={RECORD_LABEL_KEY: RECORD_ID})
    executor = _make_executor()
    executor.container = container

    executor.close()

    assert container.stop_calls == 0
    assert container.remove_calls == 0
    assert executor.container is container
    message, fields = _one_destroy(audit, "executor.close", "refused")
    assert fields["container_id"] == CONTAINER_ID
    assert fields["state"] == "running"
    assert message.endswith(
        f'detail="action=refuse decision_reason={decision.reason}"'
    )
