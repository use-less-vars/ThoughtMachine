"""W9b tests: live worker config push (handle -> HTTP route) and field tiers.

Three seams, one rule. ``config_requires_respawn`` decides the tier, the
runtime reports it in a ``config_ack``, and the HTTP route hands that ack
through without upgrading it. The tests are grouped the same way:

* ``TestHandleUpdateConfig`` -- ``WorkerRuntimeHandle.update_config`` in both
  modes: thread mode reuses the child-side handler, process mode reports a
  capability gap as ``ok=False`` instead of raising.
* ``TestConfigFieldTiers`` -- ``config_field_tiers`` must agree field-for-field
  with the authority, and must mark exactly the eight compared fields
  ``requires_respawn``.
* ``TestConfigRoute`` -- ``POST .../workers/{name}/config`` never turns "no
  runtime" / "stale fence" into a success, and reports ``applied_live`` only
  for ``ok`` + ``hot_swap``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict

import pytest

from tools.workspace.worker_runtime import (
    MODE_PROCESS,
    MODE_THREAD,
    StaleWorkerFence,
    WorkerRuntimeHandle,
)
from tools.workspace.worker_runtime_protocol import (
    CONFIG_TIER_HOT_SWAP,
    CONFIG_TIER_REQUIRES_RESPAWN,
    ConfigAck,
)


def _config(**overrides: Any) -> SimpleNamespace:
    """A stand-in config carrying exactly the attributes the authority reads."""
    values: Dict[str, Any] = dict(
        provider_type="openai",
        model="gpt-4o",
        api_key="sk-test",
        base_url="https://api.example.com/v1",
        system_prompt="be helpful",
        workspace_path="/workspace",
        worktree_root=None,
        provider_config={},
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class _FakeWorker:
    """The slice of ``WorkerThread`` the config handler touches."""

    def __init__(self, build=None):
        self._generation = 1
        self._agent_config_dict: Dict[str, Any] = {}
        self._agent = None
        self._build = build or (lambda: _config())

    def _build_agent_config(self):
        return self._build()

    def is_alive(self) -> bool:
        return True


def _handle(worker, mode=MODE_THREAD, name="worker") -> WorkerRuntimeHandle:
    return WorkerRuntimeHandle(
        worker, mode=mode, session_id="session-1", worker_name=name, instance_id=1
    )


class TestHandleUpdateConfig:
    def test_thread_mode_without_agent_stashes_the_patch(self):
        worker = _FakeWorker()
        ack = _handle(worker).update_config({"model": "gpt-4o-mini"})

        assert isinstance(ack, ConfigAck)
        assert ack.ok is True
        assert ack.tier == CONFIG_TIER_HOT_SWAP
        assert ack.status.startswith("stashed")
        # The patch was merged where the first turn will read it.
        assert worker._agent_config_dict["model"] == "gpt-4o-mini"

    def test_thread_mode_with_agent_queues_the_update(self):
        recorded = []
        worker = _FakeWorker()
        worker._agent = SimpleNamespace(
            config=_config(), request_config_update=recorded.append
        )

        ack = _handle(worker).update_config({"temperature": 0.2})

        assert len(recorded) == 1
        assert ack.ok is True
        assert ack.tier == CONFIG_TIER_HOT_SWAP

    def test_thread_mode_reports_requires_respawn_from_the_authority(self):
        recorded = []
        worker = _FakeWorker(build=lambda: _config(model="other-model"))
        worker._agent = SimpleNamespace(
            config=_config(), request_config_update=recorded.append
        )

        ack = _handle(worker).update_config({"model": "other-model"})

        # Accepted (queued) but NOT reported as live: the tier says so.
        assert ack.ok is True
        assert ack.tier == CONFIG_TIER_REQUIRES_RESPAWN
        assert len(recorded) == 1

    def test_stale_handle_refuses_to_push_a_config(self):
        worker = _FakeWorker()
        handle = _handle(worker)
        worker._generation = 2  # a successor now occupies the slot

        with pytest.raises(StaleWorkerFence):
            handle.update_config({"model": "gpt-4o-mini"})

    def test_process_capability_gap_is_an_ack_not_an_exception(self):
        from tools.workspace.worker_runtime_process import WorkerRuntimeUnsupported

        handle = _handle(_FakeWorker(), mode=MODE_PROCESS)

        def _no_capability(*_args, **_kwargs):
            raise WorkerRuntimeUnsupported("runtime did not advertise config_update")

        handle._ensure_process_runtime = _no_capability  # type: ignore[assignment]

        ack = handle.update_config({"model": "gpt-4o-mini"})

        assert ack.ok is False
        assert ack.tier == CONFIG_TIER_REQUIRES_RESPAWN
        assert "config_update" in ack.status

    def test_process_mode_delegates_to_the_runtime(self):
        sent = {}

        handle = _handle(_FakeWorker(), mode=MODE_PROCESS)
        runtime = SimpleNamespace(
            update_config=lambda config, timeout=30.0: (
                sent.update(config=config, timeout=timeout)
                or ConfigAck(
                    request_id="r",
                    ok=True,
                    tier=CONFIG_TIER_HOT_SWAP,
                    status="queued",
                )
            )
        )
        handle._ensure_process_runtime = lambda: runtime  # type: ignore[assignment]

        ack = handle.update_config({"temperature": 0.1}, timeout=5.0)

        assert sent == {"config": {"temperature": 0.1}, "timeout": 5.0}
        assert ack.tier == CONFIG_TIER_HOT_SWAP


class TestConfigFieldTiers:
    #: The fields ``config_requires_respawn`` compares -- spelled out HERE so the
    #: test fails if the authority's list silently changes (§4.9: the tier is the
    #: one thing a UI must not guess).
    RESPAWN_FIELDS = {
        "provider_type",
        "model",
        "api_key",
        "base_url",
        "system_prompt",
        "workspace_path",
        "worktree_root",
        "provider_config",
    }

    def test_every_field_is_classified(self):
        from agent.config import AgentConfig
        from agent.core.agent import config_field_tiers

        tiers = config_field_tiers()

        assert set(tiers) == set(AgentConfig.model_fields)

    def test_respawn_fields_match_the_authority(self):
        from agent.core.agent import config_field_tiers

        tiers = config_field_tiers()

        respawn = {name for name, tier in tiers.items() if tier == CONFIG_TIER_REQUIRES_RESPAWN}
        assert respawn == self.RESPAWN_FIELDS
        assert {tier for tier in tiers.values()} == {
            CONFIG_TIER_HOT_SWAP,
            CONFIG_TIER_REQUIRES_RESPAWN,
        }

    def test_non_compared_field_is_hot_swap(self):
        from agent.core.agent import config_field_tiers

        tiers = config_field_tiers()

        for name in ("temperature", "max_turns", "log_level", "stop_check"):
            assert tiers[name] == CONFIG_TIER_HOT_SWAP

    def test_map_equals_the_authority_verdict_for_a_real_config(self):
        from agent.config import AgentConfig
        from agent.core.agent import (
            _tier_probe_value,
            config_field_tiers,
            config_requires_respawn,
        )

        base = AgentConfig.model_construct()
        tiers = config_field_tiers(base)

        for name, tier in tiers.items():
            probe = _tier_probe_value(getattr(base, name))
            candidate = base.model_copy(update={name: probe})
            expected = (
                CONFIG_TIER_REQUIRES_RESPAWN
                if config_requires_respawn(base, candidate)
                else CONFIG_TIER_HOT_SWAP
            )
            assert tier == expected, name


# ── Route level ────────────────────────────────────────────────────────────


@pytest.fixture()
def route_module(tmp_path, monkeypatch):
    """``workspace_routes`` with a throwaway workspace on disk."""
    pytest.importorskip("fastapi")
    module = pytest.importorskip("web_ui.backend.workspace_routes")

    ws_dir = tmp_path / "workspaces" / "ws1"
    (ws_dir / "workers" / "session-1" / "analyst").mkdir(parents=True)

    monkeypatch.setattr(module, "ensure_workspace_dirs", lambda ws_id: None)
    monkeypatch.setattr(module, "_workspace_dir", lambda ws_id: ws_dir)
    return module


def _run(coro):
    import asyncio

    return asyncio.run(coro)


class TestConfigRoute:
    def test_no_runtime_handle_is_409_requires_respawn(self, route_module, monkeypatch):
        monkeypatch.setattr(
            route_module, "runtime_handle_for", lambda *a, **k: None
        )

        response = _run(
            route_module.update_worker_config(
                request=None,
                ws_id="ws1",
                name="analyst",
                body=route_module.WorkerConfigUpdateBody(config={"model": "m"}),
                instance_id=None,
            )
        )

        assert response.status_code == 409
        payload = route_module.json.loads(response.body)
        assert payload["ok"] is False
        assert payload["tier"] == CONFIG_TIER_REQUIRES_RESPAWN
        assert payload["applied_live"] is False

    def test_hot_swap_ack_is_reported_as_applied_live(self, route_module, monkeypatch):
        class _Handle:
            def update_config(self, config, *, timeout=30.0):
                assert config == {"temperature": 0.5}
                return ConfigAck(
                    request_id="req-1",
                    ok=True,
                    tier=CONFIG_TIER_HOT_SWAP,
                    status="queued for the next turn boundary",
                )

        monkeypatch.setattr(
            route_module, "runtime_handle_for", lambda *a, **k: _Handle()
        )

        payload = _run(
            route_module.update_worker_config(
                request=None,
                ws_id="ws1",
                name="analyst",
                body=route_module.WorkerConfigUpdateBody(config={"temperature": 0.5}),
                instance_id=None,
            )
        )

        assert payload["ok"] is True
        assert payload["tier"] == CONFIG_TIER_HOT_SWAP
        assert payload["applied_live"] is True
        assert payload["request_id"] == "req-1"
        assert payload["session_id"] == "session-1"

    def test_requires_respawn_ack_is_not_reported_as_applied(self, route_module, monkeypatch):
        class _Handle:
            def update_config(self, config, *, timeout=30.0):
                return ConfigAck(
                    request_id="req-2",
                    ok=True,
                    tier=CONFIG_TIER_REQUIRES_RESPAWN,
                    status="queued for the next turn boundary; the agent rebuilds itself there",
                )

        monkeypatch.setattr(
            route_module, "runtime_handle_for", lambda *a, **k: _Handle()
        )

        payload = _run(
            route_module.update_worker_config(
                request=None,
                ws_id="ws1",
                name="analyst",
                body=route_module.WorkerConfigUpdateBody(config={"model": "m"}),
                instance_id=None,
            )
        )

        assert payload["ok"] is True
        assert payload["tier"] == CONFIG_TIER_REQUIRES_RESPAWN
        assert payload["applied_live"] is False

    def test_stale_fence_is_409_not_500(self, route_module, monkeypatch):
        class _Handle:
            def update_config(self, config, *, timeout=30.0):
                raise StaleWorkerFence("the worker's _generation moved")

        monkeypatch.setattr(
            route_module, "runtime_handle_for", lambda *a, **k: _Handle()
        )

        response = _run(
            route_module.update_worker_config(
                request=None,
                ws_id="ws1",
                name="analyst",
                body=route_module.WorkerConfigUpdateBody(config={"model": "m"}),
                instance_id=None,
            )
        )

        assert response.status_code == 409
        payload = route_module.json.loads(response.body)
        assert payload["ok"] is False
        assert payload["tier"] == CONFIG_TIER_REQUIRES_RESPAWN
        assert "_generation moved" in payload["status"]

    def test_unknown_worker_is_404(self, route_module):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as excinfo:
            _run(
                route_module.update_worker_config(
                    request=None,
                    ws_id="ws1",
                    name="nobody",
                    body=route_module.WorkerConfigUpdateBody(config={}),
                    instance_id=None,
                )
            )

        assert excinfo.value.status_code == 404

    def test_config_tiers_route_returns_the_authority_map(self, route_module):
        payload = _run(route_module.get_config_tiers(request=None, ws_id="ws1"))

        assert payload["tiers"]["model"] == CONFIG_TIER_REQUIRES_RESPAWN
        assert payload["tiers"]["temperature"] == CONFIG_TIER_HOT_SWAP
        assert set(payload["tier_values"]) == {
            CONFIG_TIER_HOT_SWAP,
            CONFIG_TIER_REQUIRES_RESPAWN,
        }
