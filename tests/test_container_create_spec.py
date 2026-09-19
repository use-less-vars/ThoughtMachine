"""Hermetic spec tests for :mod:`infra.container_create`.

These exercise the *pure spec* surface of the new container-creation
primitive without a live docker daemon:

* call-time resolution of the hardening user,
* frozen-dataclass semantics for every public spec type,
* command normalisation (list -> tuple, ``None`` -> :data:`DEFAULT_COMMAND`),
* construction that needs neither an admission gate nor a docker daemon,
* the recipe's agreement with :mod:`agent.config.defaults`,
* a fake-client ``create_hardened_container`` path (run kwargs + result),
* admission enforcement (deny / transform) and record-hook wiring.

The docker SDK is never contacted: a lightweight fake client mirrors the
pattern used in ``tests/test_container_registry.py``.
"""

from __future__ import annotations

import contextlib
from dataclasses import FrozenInstanceError
from unittest import mock

import pytest

import infra.container_create as cc
from agent.config.defaults import (
    DEFAULT_COMMAND,
    DEFAULT_CPU_QUOTA,
    DEFAULT_MEM_LIMIT,
    DEFAULT_RESOURCE_OOM_SCORE_ADJ,
    DEFAULT_USER_OOM_SCORE_ADJ,
    HARDENED_CAP_DROP,
    HARDENED_READ_ONLY,
    HARDENED_SECURITY_OPT,
    host_user,
)
from infra.container_create import (
    AdmissionContext,
    ContainerCreateSpec,
    CreatedContainer,
    HardeningRecipe,
    MountSpec,
    create_hardened_container,
)
from security.admission_gate import ContainerSpec
from thoughtmachine.container_record import LIFECYCLE_EPHEMERAL


# ── Fake docker client ──────────────────────────────────────────────────


class FakeContainer:
    def __init__(self, name):
        self.name = name
        self.id = f"id-{name}"
        self.stopped = False
        self.removed = False
        self.force_removed = False
        self.stop_timeout = None

    def stop(self, timeout=None):
        self.stop_timeout = timeout
        self.stopped = True

    def remove(self, force=False):
        if force:
            self.force_removed = True
        self.removed = True


class FakeClient:
    def __init__(self):
        self.gotten = {}
        self.containers = mock.Mock()
        self.images = mock.Mock()
        self.containers.run = mock.Mock(
            side_effect=lambda image, command=None, **kwargs: FakeContainer(kwargs["name"])
        )
        self.containers.get = mock.Mock(side_effect=self._get)
        self.containers.list = mock.Mock(return_value=[])

    def ping(self):
        return True

    def _get(self, name):
        c = FakeContainer(name)
        self.gotten[name] = c
        return c


@pytest.fixture
def fake_client():
    return FakeClient()


def _spec(**overrides):
    """Build a minimal valid :class:`ContainerCreateSpec`."""
    base = dict(
        image="agent-executor",
        command=tuple(DEFAULT_COMMAND),
        name="c1",
        container_type="user",
        lifecycle_class=LIFECYCLE_EPHEMERAL,
        tmpfs={},
    )
    base.update(overrides)
    return ContainerCreateSpec(**base)


# ── (a) call-time hardening ─────────────────────────────────────────────


class TestCallTimeHardening:
    def test_user_resolved_at_call_time(self, monkeypatch):
        monkeypatch.setattr(cc, "host_user", lambda: "4242:4343")
        spec = ContainerCreateSpec(
            image="agent-executor",
            command=tuple(DEFAULT_COMMAND),
            name="c1",
            container_type="user",
            lifecycle_class=LIFECYCLE_EPHEMERAL,
        )
        assert spec.hardening.user == "4242:4343"
        assert isinstance(spec.hardening, HardeningRecipe)

    def test_user_falls_back_to_root_when_host_user_is_none(self, monkeypatch):
        monkeypatch.setattr(cc, "host_user", lambda: None)
        spec = ContainerCreateSpec(
            image="agent-executor",
            command=tuple(DEFAULT_COMMAND),
            name="c1",
            container_type="user",
            lifecycle_class=LIFECYCLE_EPHEMERAL,
        )
        assert spec.hardening.user == "0:0"


# ── (b) frozen semantics ────────────────────────────────────────────────


class TestFrozenSemantics:
    def test_container_create_spec_frozen(self):
        spec = _spec()
        with pytest.raises(FrozenInstanceError):
            spec.name = "other"

    def test_mountspec_frozen(self):
        mount = MountSpec(source="/a", target="/b")
        with pytest.raises(FrozenInstanceError):
            mount.source = "/c"

    def test_hardening_recipe_frozen(self):
        recipe = cc._expected_hardening_recipe()
        with pytest.raises(FrozenInstanceError):
            recipe.read_only = False

    def test_admission_context_frozen(self):
        ctx = AdmissionContext(workspace_id="ws")
        with pytest.raises(FrozenInstanceError):
            ctx.workspace_id = "other"

    def test_created_container_frozen(self):
        spec = _spec()
        created = CreatedContainer(
            container=object(),
            name="c1",
            container_type="user",
            lifecycle_class=LIFECYCLE_EPHEMERAL,
            network_mode="none",
            expected_hardening=spec.hardening,
        )
        with pytest.raises(FrozenInstanceError):
            created.name = "x"


# ── (c) command normalisation ───────────────────────────────────────────


class TestCommandNormalisation:
    def test_list_normalised_to_tuple(self):
        spec = _spec(command=list(DEFAULT_COMMAND))
        assert isinstance(spec.command, tuple)
        assert spec.command == tuple(DEFAULT_COMMAND)

    def test_source_list_mutation_is_isolated(self):
        source = list(DEFAULT_COMMAND)
        spec = _spec(command=source)
        source.append("EXTRA")
        assert spec.command == tuple(DEFAULT_COMMAND)
        assert "EXTRA" not in spec.command

    def test_none_command_uses_default(self):
        spec = _spec(command=None)
        assert spec.command == tuple(DEFAULT_COMMAND)


# ── (d) construction needs no admission / daemon ────────────────────────


class TestConstructionRequiresNoDaemon:
    def test_construct_without_admission_or_docker(self):
        spec = _spec()
        assert spec.network_mode == "none"
        assert spec.mem_limit == DEFAULT_MEM_LIMIT
        assert spec.cpu_quota == DEFAULT_CPU_QUOTA

    def test_module_does_not_import_docker_top_level(self):
        # The docker SDK must not be pulled in at module import time.
        assert not hasattr(cc, "docker")

    def test_default_oom_score_adj_by_container_type(self):
        assert _spec(container_type="user").oom_score_adj == DEFAULT_USER_OOM_SCORE_ADJ
        assert (
            _spec(container_type="resource").oom_score_adj
            == DEFAULT_RESOURCE_OOM_SCORE_ADJ
        )

    def test_explicit_oom_score_adj_is_preserved(self):
        assert _spec(oom_score_adj=7).oom_score_adj == 7

    def test_unknown_container_type_rejected(self):
        with pytest.raises(ValueError):
            _spec(container_type="bogus")


# ── (e) recipe agrees with defaults ─────────────────────────────────────


class TestRecipeMatchesDefaults:
    def test_recipe_agrees_with_defaults(self):
        recipe = cc._expected_hardening_recipe()
        assert recipe.cap_drop == tuple(HARDENED_CAP_DROP)
        assert recipe.security_opt == tuple(HARDENED_SECURITY_OPT)
        assert recipe.read_only == HARDENED_READ_ONLY
        assert recipe.user == (host_user() or "0:0")


# ── create path (fake client) ───────────────────────────────────────────


class TestCreateHardenedContainer:
    def test_run_kwargs_and_result(self, fake_client):
        spec = _spec()
        created = create_hardened_container(fake_client, spec, record=False)

        assert fake_client.containers.run.call_count == 1
        args, kwargs = fake_client.containers.run.call_args
        assert args[0] == "agent-executor"
        assert args[1] == list(DEFAULT_COMMAND)
        assert kwargs["detach"] is True
        assert kwargs["network_mode"] == "none"
        assert kwargs["cap_drop"] == ["ALL"]
        assert kwargs["security_opt"] == ["no-new-privileges:true"]
        assert kwargs["read_only"] is True
        assert kwargs["user"] == spec.hardening.user
        assert kwargs["oom_score_adj"] == DEFAULT_USER_OOM_SCORE_ADJ
        assert kwargs["mem_limit"] == DEFAULT_MEM_LIMIT
        assert kwargs["cpu_quota"] == DEFAULT_CPU_QUOTA
        assert kwargs["mounts"] == []
        assert kwargs["restart_policy"] == {"Name": "no", "MaximumRetryCount": 0}

        assert isinstance(created, CreatedContainer)
        assert created.name == "c1"
        assert created.container_type == "user"
        assert created.lifecycle_class == LIFECYCLE_EPHEMERAL
        assert created.network_mode == "none"
        assert created.expected_hardening == spec.hardening
        assert isinstance(created.container, FakeContainer)

    def test_identity_env_is_merged(self, fake_client):
        spec = _spec(workspace_id="ws-1", session_id="sess-1", environment={"A": "B"})
        create_hardened_container(fake_client, spec, record=False)
        _, kwargs = fake_client.containers.run.call_args
        env = kwargs["environment"]
        assert env["A"] == "B"
        assert env["THOUGHTMACHINE_WORKSPACE_ID"] == "ws-1"
        assert env["THOUGHTMACHINE_SESSION_ID"] == "sess-1"


# ── admission enforcement ───────────────────────────────────────────────


class TestAdmissionEnforcement:
    def test_deny_raises_and_skips_run(self, fake_client, monkeypatch):
        spec = _spec(workspace_id="ws")
        monkeypatch.setattr(
            cc, "admit", lambda request, *, probes=None: cc.Deny("nope", "denied")
        )
        with pytest.raises(cc.AdmissionDenied) as exc:
            create_hardened_container(
                fake_client,
                spec,
                admission=AdmissionContext(workspace_id="ws"),
                record=False,
            )
        assert exc.value.code == "nope"
        fake_client.containers.run.assert_not_called()

    def test_transform_overrides_network_mode(self, fake_client, monkeypatch):
        spec = _spec(workspace_id="ws", network_mode="none")
        new_spec = ContainerSpec(
            container_type="user",
            lifecycle_class=LIFECYCLE_EPHEMERAL,
            workspace_id="ws",
            network_mode="bridge",
        )
        monkeypatch.setattr(
            cc, "admit", lambda request, *, probes=None: cc.Transform(new_spec, "t", "net")
        )
        create_hardened_container(
            fake_client,
            spec,
            admission=AdmissionContext(workspace_id="ws"),
            record=False,
        )
        _, kwargs = fake_client.containers.run.call_args
        assert kwargs["network_mode"] == "bridge"


# ── record-hook wiring ──────────────────────────────────────────────────


class TestRecording:
    def test_record_creation_invoked_when_workspace_present(
        self, fake_client, monkeypatch
    ):
        spec = _spec(workspace_id="ws")
        calls = {}

        class _Handle:
            def attach(self, container, state=None):
                calls["attached"] = container

        @contextlib.contextmanager
        def fake_record_creation(*, workspace_id, lifecycle_class, labels=None, name=""):
            calls["workspace_id"] = workspace_id
            calls["labels"] = labels
            if labels is not None:
                labels["tm.record"] = "rec-1"
            yield _Handle()

        monkeypatch.setattr(cc, "record_creation", fake_record_creation)
        created = create_hardened_container(fake_client, spec, record=True)

        assert calls["workspace_id"] == "ws"
        assert calls["attached"] is created.container
        _, kwargs = fake_client.containers.run.call_args
        assert kwargs["labels"].get("tm.record") == "rec-1"

    def test_no_record_when_workspace_absent(self, fake_client, monkeypatch):
        spec = _spec()  # workspace_id is None

        def boom(*args, **kwargs):
            raise AssertionError("record_creation must not be called")

        monkeypatch.setattr(cc, "record_creation", boom)
        create_hardened_container(fake_client, spec, record=True)
        assert fake_client.containers.run.call_count == 1
