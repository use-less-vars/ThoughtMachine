"""Tests for the shared lifecycle-class policy table (Phase 1).

Covers the pure :mod:`thoughtmachine.container_record.lifecycle_policy` module,
its re-exports, the resource predicate that the migration now shares, and the
migration's resource detection via name / image.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from unittest import mock

import pytest

import infra.container_manager as cm_mod
from agent.config.defaults import CONTAINER_TYPE_RESOURCE
from infra.container_manager import ContainerManager
from infra.resource_container_manager import ResourceContainerManager
from security.admission_gate import REASON_UNKNOWN_LIFECYCLE_CLASS
from thoughtmachine.container_record import (
    POLICY_BY_CLASS,
    RECORD_LABEL_KEY,
    RESOURCE_IMAGE,
    RESOURCE_LABEL,
    RESOURCE_NAME_PREFIX,
    LifecyclePolicy,
    UnknownLifecycleClass,
    create_record,
    is_resource_like,
    policy_for,
)
from thoughtmachine.container_record import drift, migration
from thoughtmachine.container_record.models import (
    LIFECYCLE_CLASSES,
    LIFECYCLE_EPHEMERAL,
    LIFECYCLE_PERSISTENT,
    LIFECYCLE_RESOURCE,
    LIFECYCLE_SERVICE,
)

#: ``lifecycle_class -> (agent_reachable, own_lifecycle, gc_age_s, drift_axes)``.
_EXPECTED = {
    LIFECYCLE_PERSISTENT: (True, False, 86400, {"identity", "policy", "runtime", "image", "hardening"}),
    LIFECYCLE_EPHEMERAL: (True, False, None, {"image"}),
    LIFECYCLE_RESOURCE: (False, True, None, {"image"}),
    LIFECYCLE_SERVICE: (False, True, None, {"image"}),
}

#: ``lifecycle_class -> expected container restart policy``.
_EXPECTED_RESTART = {
    LIFECYCLE_PERSISTENT: "unless-stopped",
    LIFECYCLE_EPHEMERAL: "no",
    LIFECYCLE_RESOURCE: "unless-stopped",
    LIFECYCLE_SERVICE: "unless-stopped",
}


# ── (a) four-class field matrix ─────────────────────────────────────────────


@pytest.mark.parametrize("cls,expected", list(_EXPECTED.items()))
def test_policy_field_matrix(cls, expected):
    policy = POLICY_BY_CLASS[cls]
    agent_reachable, own_lifecycle, gc_age, axes = expected
    assert policy.class_name == cls
    assert policy.agent_reachable is agent_reachable
    assert policy.own_lifecycle is own_lifecycle
    assert policy.workspace_gc_max_age_s == gc_age
    assert policy.drift_axes == frozenset(axes)
    assert policy.restart_policy == _EXPECTED_RESTART[cls]


def test_policy_field_order_and_frozen():
    names = [f.name for f in dataclasses.fields(LifecyclePolicy)]
    assert names == [
        "class_name",
        "agent_reachable",
        "own_lifecycle",
        "workspace_gc_max_age_s",
        "drift_axes",
        "restart_policy",
    ]
    assert LifecyclePolicy.__dataclass_params__.frozen is True


def test_positional_construction_and_restart_default():
    policy = LifecyclePolicy("persistent", True, False, 86400, frozenset({"image"}))
    assert policy.restart_policy is None


def test_restart_policy_per_class():
    assert {c: p.restart_policy for c, p in POLICY_BY_CLASS.items()} == _EXPECTED_RESTART


# ── (b) keys == constants & policy_for round-trip ───────────────────────────


def test_policy_keys_equal_lifecycle_classes():
    assert set(POLICY_BY_CLASS) == set(LIFECYCLE_CLASSES)
    assert len(POLICY_BY_CLASS) == 4


@pytest.mark.parametrize("cls", LIFECYCLE_CLASSES)
def test_policy_for_round_trip(cls):
    assert policy_for(cls) is POLICY_BY_CLASS[cls]
    assert policy_for(cls).class_name == cls


# ── (c) fail-closed for unknown classes ─────────────────────────────────────


@pytest.mark.parametrize("bad", ["nonsense", "", None, "PERSISTENT", "persistent ", ["persistent"]])
def test_policy_for_fails_closed(bad):
    with pytest.raises(UnknownLifecycleClass):
        policy_for(bad)


def test_unknown_lifecycle_class_is_value_error():
    assert issubclass(UnknownLifecycleClass, ValueError)


# ── (d) is_resource_like truth table ────────────────────────────────────────


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        # label signal
        ({"labels": {RESOURCE_LABEL: "1"}}, True),
        ({"labels": {RESOURCE_LABEL: "true"}}, True),
        ({"labels": {RESOURCE_LABEL: ""}}, False),
        ({"labels": {RESOURCE_LABEL: True}}, False),  # non-string value
        ({"labels": {}}, False),
        ({"labels": None}, False),
        ({"labels": "not-a-mapping"}, False),
        # name signal (leading slash tolerated)
        ({"labels": {}, "name": "tm-res-abc-git"}, True),
        ({"labels": {}, "name": "/tm-res-abc-git"}, True),
        ({"labels": {}, "name": "agent-exec-123"}, False),
        ({"labels": {}, "name": "tm-result"}, False),  # not the "tm-res-" prefix
        ({"labels": {}, "name": None}, False),
        # image signal
        ({"labels": {}, "image_tags": RESOURCE_IMAGE}, True),
        ({"labels": {}, "image_tags": "tm-resource-git:latest"}, True),
        ({"labels": {}, "image_tags": ""}, False),
        ({"labels": {}, "image_tags": ["other", RESOURCE_IMAGE]}, True),
        ({"labels": {}, "image_tags": ["tm-resource-git:v1"]}, True),
        ({"labels": {}, "image_tags": ["tm-resource-gitx"]}, False),
        ({"labels": {}, "image_tags": ()}, False),
        ({"labels": {}, "image_tags": None}, False),
        ({"labels": {}, "image_tags": 123}, False),
    ],
)
def test_is_resource_like_truth_table(kwargs, expected):
    assert is_resource_like(**kwargs) is expected


def test_is_resource_like_all_signals_absent_is_false():
    assert is_resource_like({}) is False


def test_is_resource_like_label_wins_without_name_or_image():
    assert is_resource_like({RESOURCE_LABEL: "x"}) is True


# ── (e) constants == historical literals & drift-axis mirror ────────────────


def test_constants_match_historical_literals():
    assert RESOURCE_LABEL == "thoughtmachine.resource"
    assert RESOURCE_NAME_PREFIX == "tm-res-"
    assert RESOURCE_IMAGE == "tm-resource-git"


def test_resource_label_shared_with_migration():
    assert migration.RESOURCE_LABEL == RESOURCE_LABEL


def test_drift_axes_mirror_drift_class_constants():
    all_axes = {
        drift.CLASS_IDENTITY,
        drift.CLASS_POLICY,
        drift.CLASS_RUNTIME,
        drift.CLASS_IMAGE,
        drift.CLASS_HARDENING,
    }
    assert POLICY_BY_CLASS[LIFECYCLE_PERSISTENT].drift_axes == all_axes
    assert POLICY_BY_CLASS[LIFECYCLE_EPHEMERAL].drift_axes == {drift.CLASS_IMAGE}
    assert POLICY_BY_CLASS[LIFECYCLE_RESOURCE].drift_axes == {drift.CLASS_IMAGE}
    assert POLICY_BY_CLASS[LIFECYCLE_SERVICE].drift_axes == {drift.CLASS_IMAGE}


# ── (f) migration regression: name / image detection + unchanged cases ──────


def test_migration_detects_resource_by_name_only():
    labels = {"thoughtmachine.workspace_id": "ws-1"}
    assert migration._derive_class_owner(labels, name="/tm-res-deadbeef-git") == (
        LIFECYCLE_RESOURCE,
        "workspace-owned",
        False,
    )


def test_migration_detects_resource_by_image_only():
    labels = {"thoughtmachine.workspace_id": "ws-1"}
    assert migration._derive_class_owner(labels, image="tm-resource-git:latest") == (
        LIFECYCLE_RESOURCE,
        "workspace-owned",
        False,
    )


def test_migration_unchanged_cases_hold():
    # legacy resource label and legacy container_type both still map to resource
    assert migration._derive_class_owner({migration.RESOURCE_LABEL: "1"})[0] == LIFECYCLE_RESOURCE
    assert migration._derive_class_owner(
        {"thoughtmachine.container_type": "resource"}
    ) == (LIFECYCLE_RESOURCE, "workspace-owned", False)
    # free_use -> ephemeral
    assert migration._derive_class_owner({"thoughtmachine.container_type": "free_use"}) == (
        LIFECYCLE_EPHEMERAL,
        "workspace-owned",
        False,
    )
    # absent / unknown type -> persistent (flagged unknown)
    assert migration._derive_class_owner({}) == (LIFECYCLE_PERSISTENT, "workspace-owned", True)
    assert migration._derive_class_owner({"thoughtmachine.container_type": "weird"}) == (
        LIFECYCLE_PERSISTENT,
        "workspace-owned",
        True,
    )


def test_single_arg_call_still_supported():
    assert migration._derive_class_owner({"thoughtmachine.container_type": "free_use"}) == (
        LIFECYCLE_EPHEMERAL,
        "workspace-owned",
        False,
    )


# ── (g) migration reuses the shared predicate ───────────────────────────────


def test_migration_uses_shared_predicate():
    assert migration.is_resource_like is is_resource_like


# ── (h) ContainerManager classifier + access denial (record-driven) ──────────


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """Hermetic default vault (resolved via ``THOUGHTMACHINE_VAULT_ROOT``)."""
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(root))
    return root


def _container(labels=None, name="agent-exec-1", image="agent-executor"):
    """Minimal stand-in for a live Docker container object."""
    return SimpleNamespace(labels=labels or {}, name=name, image=image)


def _cm():
    """A ``ContainerManager`` without running its (docker-touching) ctor."""
    return object.__new__(ContainerManager)


# -- class_of / _container_lifecycle_class resolve the record's class ---------


def test_class_of_resolves_record_class_from_default_vault(vault):
    record = create_record("ws-1", LIFECYCLE_SERVICE, "workspace-owned", id="rec-svc")
    container = _container(labels={RECORD_LABEL_KEY: record.id})
    assert ContainerManager.class_of(_cm(), container) == LIFECYCLE_SERVICE
    assert cm_mod._container_lifecycle_class(container) == LIFECYCLE_SERVICE


def test_class_of_missing_record_falls_back_to_persistent(vault):
    container = _container(labels={RECORD_LABEL_KEY: "no-such-record"})
    assert ContainerManager.class_of(_cm(), container) == LIFECYCLE_PERSISTENT
    assert cm_mod._container_lifecycle_class(container) == LIFECYCLE_PERSISTENT


def test_class_of_without_record_label_is_persistent(vault):
    assert cm_mod._container_lifecycle_class(_container()) == LIFECYCLE_PERSISTENT


def test_class_of_record_invisible_when_vault_diverges(tmp_path, monkeypatch):
    """B2 pin: the record lookup uses the DEFAULT vault, so a divergent vault
    makes it silently miss and the class degrade to PERSISTENT (the fail-closed
    denial for the ``service``/unknown classes would be LOST)."""
    real = tmp_path / "real"
    real.mkdir()
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(real))
    record = create_record("ws-1", LIFECYCLE_SERVICE, "workspace-owned", id="rec-svc")
    container = _container(labels={RECORD_LABEL_KEY: record.id})
    assert cm_mod._container_lifecycle_class(container) == LIFECYCLE_SERVICE

    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(other))
    assert cm_mod._container_lifecycle_class(container) == LIFECYCLE_PERSISTENT


def test_class_of_resource_probe_wins_over_record(vault):
    record = create_record("ws-1", LIFECYCLE_SERVICE, "workspace-owned", id="rec-x")
    container = _container(
        labels={RECORD_LABEL_KEY: record.id, RESOURCE_LABEL: "1"},
        name="tm-res-abc-git",
    )
    assert cm_mod._container_lifecycle_class(container) == LIFECYCLE_RESOURCE
    assert ContainerManager.class_of(_cm(), container) == LIFECYCLE_RESOURCE


# -- class_of_handle (registry handle vocab) ---------------------------------


def test_class_of_handle_non_dict_is_persistent():
    assert _cm().class_of_handle(None) == LIFECYCLE_PERSISTENT
    assert _cm().class_of_handle("nope") == LIFECYCLE_PERSISTENT


def test_class_of_handle_reads_handle_container_type():
    # NOTE: the handle vocab (``container_type == "resource"``) differs from the
    # live-container vocab (the shared probe); class_of_handle maps the former.
    assert _cm().class_of_handle(
        {"container_type": CONTAINER_TYPE_RESOURCE, "name": "tm-res-abc-git"}
    ) == LIFECYCLE_RESOURCE


def test_class_of_handle_daemon_miss_falls_back_to_name_prefix():
    mgr = _cm()
    mgr.client = mock.Mock()
    mgr.client.containers.get.side_effect = Exception("no daemon")
    assert mgr.class_of_handle({"name": "tm-res-abc-git"}) == LIFECYCLE_RESOURCE
    assert mgr.class_of_handle({"name": "/tm-res-abc-git"}) == LIFECYCLE_RESOURCE
    assert mgr.class_of_handle({"name": "agent-exec-1"}) == LIFECYCLE_PERSISTENT


# -- _is_resource_container probe widenings ----------------------------------


def test_is_resource_container_probe_widenings():
    probe = ContainerManager._is_resource_container
    # image given as a bare (tagged) string reference
    assert probe(_container(image="tm-resource-git:latest")) is True
    # leading-slash name
    assert probe(_container(name="/tm-res-abc")) is True
    # image object carrying ``.tags``
    assert probe(_container(image=SimpleNamespace(tags=["tm-resource-git:latest"]))) is True
    assert probe(_container()) is False


def test_is_resource_container_probe_failure_is_false():
    class _Exploding:
        labels = None
        name = None

        @property
        def image(self):
            raise RuntimeError("boom")

    assert ContainerManager._is_resource_container(_Exploding()) is False


# -- _agent_access_denial fail-closed ----------------------------------------


def test_agent_access_denial_per_class():
    denial = ContainerManager._agent_access_denial
    assert denial(LIFECYCLE_PERSISTENT) is None
    assert denial(LIFECYCLE_EPHEMERAL) is None
    assert denial(LIFECYCLE_RESOURCE) == "Resource container access denied"
    assert denial(LIFECYCLE_SERVICE) == "Resource container access denied"
    assert denial("bogus") == REASON_UNKNOWN_LIFECYCLE_CLASS
    assert REASON_UNKNOWN_LIFECYCLE_CLASS == "unknown_lifecycle_class"


@pytest.mark.parametrize("bad", ["bogus", "", None])
def test_agent_access_denial_unknown_class_is_fail_closed(bad):
    with pytest.raises(UnknownLifecycleClass):
        policy_for(bad)
    assert ContainerManager._agent_access_denial(bad) == REASON_UNKNOWN_LIFECYCLE_CLASS


# -- _gc_should_skip (destructive-GC guard) ----------------------------------


def test_gc_should_skip_truth_table(monkeypatch):
    assert cm_mod._gc_should_skip(_container(labels={RESOURCE_LABEL: "1"})) is True
    assert cm_mod._gc_should_skip(_container()) is False

    def _patch(cls):
        monkeypatch.setattr(
            cm_mod,
            "find_by_docker_label",
            lambda label, vault_root=None: SimpleNamespace(lifecycle_class=cls),
        )

    _patch(LIFECYCLE_SERVICE)
    assert cm_mod._gc_should_skip(_container(labels={RECORD_LABEL_KEY: "r"})) is True
    _patch("bogus")  # unknown class -> fail-closed skip
    assert cm_mod._gc_should_skip(_container(labels={RECORD_LABEL_KEY: "r"})) is True
    _patch(LIFECYCLE_PERSISTENT)
    assert cm_mod._gc_should_skip(_container(labels={RECORD_LABEL_KEY: "r"})) is False
    _patch(LIFECYCLE_EPHEMERAL)
    assert cm_mod._gc_should_skip(_container(labels={RECORD_LABEL_KEY: "r"})) is False


# -- shared resource-label / name-prefix constants ---------------------------


def test_resource_label_single_sourced_in_container_manager():
    assert cm_mod._RESOURCE_LABEL == RESOURCE_LABEL == "thoughtmachine.resource"


def test_resource_container_manager_shares_resource_label():
    assert ResourceContainerManager.RESOURCE_LABEL == "thoughtmachine.resource"


def test_resource_container_name_uses_shared_prefix():
    mgr = object.__new__(ResourceContainerManager)
    mgr.workspace_path = "/tmp/ws"
    assert mgr.container_name.startswith(RESOURCE_NAME_PREFIX)


# -- import-cycle consequence: DockerCodeRunner stays registered --------------


def test_security_gate_first_import_keeps_docker_code_runner():
    import security.security_gate  # noqa: F401  (forces the cycle-closing import)
    import tools

    assert "DockerCodeRunner" in [c.__name__ for c in tools.TOOL_CLASSES]
    assert tools.get_import_failures() == []
