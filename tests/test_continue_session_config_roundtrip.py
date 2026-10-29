"""Turn-1 vs turn-N credential derivation must agree (round-trip regression).

``Bridge.start()`` derived ``api_key`` / ``base_url`` from the FULL config
chain (``resolve_full_config(workspace_id=..., session_id=..., provider_id=...)``
— workspace defaults layer + session metadata layer + provider profile) and
only FILLED BLANKS, while ``ConfigManager.apply_config()`` re-derived them
from a NARROWER chain (``resolve_full_config(provider_id=...)`` — provider
profile only) and OVERWROTE whatever the session config already held.

The frontend sends the identical payload on ``continue_session`` as on
``start_session`` (``QueryBar.jsx``: ``{...(config ?? {}), mode}``), and that
snapshot always carries the resolved ``base_url`` / ``provider_id``.  So from
turn 2 onwards ``apply_config`` clobbered the turn-1 ``base_url`` with the
provider-profile value -> the AgentConfig changed ->
``Agent._can_hot_swap`` said ``False`` -> forced restart + config-changed
notification on every turn.

Both entry points now share ``config_manager.resolve_provider_fields``.
"""

import pytest

from agent.config.models import AgentConfig
from agent.config.session_config import SessionConfig
from agent.core.agent import Agent

import web_ui.backend.config_manager as config_manager
from web_ui.backend.config_manager import (
    ConfigManager,
    resolve_provider_fields,
    translate_frontend_config,
)

# One distinct base_url per config layer, so a test can tell which layer won.
PROVIDER_BASE_URL = "https://provider-profile.example"
WORKSPACE_BASE_URL = "https://workspace-layer.example"
SESSION_BASE_URL = "https://session-layer.example"

WORKSPACE_ID = "ws-roundtrip"
SESSION_ID = "sess-roundtrip"


def _install_layered_resolver(monkeypatch):
    """Stand-in for ``resolve_full_config`` that mimics the real layer chain.

    The provider-profile layer always contributes a ``base_url``; the
    workspace layer (only reachable when ``workspace_id`` is passed) and the
    session metadata layer (only when ``session_id`` is passed) override it.
    That asymmetry is exactly what made turn 1 and turn 2 disagree.
    """
    calls = []

    def fake_resolve(
        workspace_id=None,
        session_id=None,
        provider_id=None,
        worker_overrides=None,
    ):
        calls.append(
            {
                "workspace_id": workspace_id,
                "session_id": session_id,
                "provider_id": provider_id,
            }
        )
        pid = provider_id or "p1"
        resolved = {
            "provider_id": pid,
            "api_key": f"sk-{pid}",
            "base_url": f"{PROVIDER_BASE_URL}/{pid}",
            "provider_config": {"timeout": 30},
            "model": "m1",
        }
        if workspace_id:
            resolved["base_url"] = f"{WORKSPACE_BASE_URL}/{pid}"
        if session_id:
            resolved["base_url"] = f"{SESSION_BASE_URL}/{pid}"
        return resolved

    monkeypatch.setattr(config_manager, "resolve_full_config", fake_resolve)
    return calls


def _start_like_session_config():
    """A ``SessionConfig`` as ``server.py`` builds it for the first turn.

    The browser payload carries no ``base_url``; the bridge resolves it.
    """
    return SessionConfig(
        provider_id="p1",
        model="m1",
        base_url="",
        mode="agent",
        workspace_id=WORKSPACE_ID,
        temperature=0.5,
        max_turns=10,
    )


def _resolve_as_start_does(session_config):
    """The turn-1 derivation (``Bridge.start`` -> ``resolve_provider_fields``)."""
    resolve_provider_fields(
        session_config,
        workspace_id=session_config.workspace_id,
        session_id=SESSION_ID,
        provider_id=session_config.provider_id,
    )
    return session_config


def _frontend_continue_payload(session_config):
    """The payload the browser sends on ``continue_session`` (translate step).

    ``QueryBar.jsx`` sends ``{...(config ?? {}), mode}`` where ``config`` is the
    server snapshot built by ``session_config_to_frontend`` — i.e. the resolved
    ``base_url`` / ``provider_id`` / ``session_permissions`` are echoed back.
    """
    snapshot = ConfigManager.session_config_to_frontend(session_config)
    snapshot["mode"] = session_config.mode
    return translate_frontend_config(snapshot)


def _apply_continue_payload(session_config, payload, **overrides):
    """The turn-2 path: ``Bridge.apply_config`` -> ``ConfigManager.apply_config``."""
    kwargs = dict(
        current_config=session_config,
        is_running=True,
        has_session=True,
        workspace_id=WORKSPACE_ID,
        session_id=SESSION_ID,
    )
    kwargs.update(overrides)
    return ConfigManager.apply_config(payload, **kwargs)


def _agent_for(agent_config):
    """Agent instance used only for its pure config-diff helpers."""
    return Agent(config=agent_config, session_id="cfg-roundtrip-test")


class TestEchoedConfigIsNoOp:
    """A pure echo of the turn-1 config must not change anything."""

    def test_echoed_frontend_config_is_noop_after_start(self, monkeypatch):
        calls = _install_layered_resolver(monkeypatch)
        sc_start = _resolve_as_start_does(_start_like_session_config())

        # Turn 1 really did take the deep (session-layer) value.
        assert sc_start.base_url == f"{SESSION_BASE_URL}/p1"
        assert sc_start.api_key == "sk-p1"
        agent_config_start = sc_start.to_agent_config()

        payload = _frontend_continue_payload(sc_start)
        _, sc_next = _apply_continue_payload(sc_start, payload)

        # Turn 2 resolved through the same chain, so nothing moved.
        assert calls[-1]["workspace_id"] == WORKSPACE_ID
        assert calls[-1]["session_id"] == SESSION_ID
        assert sc_next.base_url == f"{SESSION_BASE_URL}/p1", (
            "continue_session re-derived base_url from the narrower "
            f"provider-profile chain: {sc_next.base_url!r}"
        )
        assert sc_next.api_key == "sk-p1"

        agent = _agent_for(agent_config_start)
        assert agent._configs_are_identical(
            agent_config_start, sc_next.to_agent_config()
        ), "echoed config produced a semantically different AgentConfig"

    def test_pending_config_cleared_without_notification(self, monkeypatch):
        _install_layered_resolver(monkeypatch)
        sc_start = _resolve_as_start_does(_start_like_session_config())

        agent = _agent_for(sc_start.to_agent_config())
        messages_before = len(agent.conversation)

        payload = _frontend_continue_payload(sc_start)
        _, sc_next = _apply_continue_payload(sc_start, payload)
        agent.request_config_update(sc_next.to_agent_config())

        assert agent._apply_pending_config() is True
        assert agent._pending_config is None
        assert len(agent.conversation) == messages_before, (
            "an echoed (unchanged) config produced a config-change notification"
        )

    def test_payload_does_not_carry_api_key(self, monkeypatch):
        """The echo never carries ``api_key`` — the key stays server-side."""
        _install_layered_resolver(monkeypatch)
        sc_start = _resolve_as_start_does(_start_like_session_config())

        payload = _frontend_continue_payload(sc_start)

        assert "api_key" not in payload


class TestGenuineChangesStillFire:
    """The fix must not turn ``apply_config`` into a no-op."""

    def test_real_base_url_change_still_fires(self, monkeypatch):
        _install_layered_resolver(monkeypatch)
        sc_start = _resolve_as_start_does(_start_like_session_config())

        user_base_url = "https://user-typed.example/v1"
        payload = _frontend_continue_payload(sc_start)
        payload["base_url"] = user_base_url

        _, sc_next = _apply_continue_payload(sc_start, payload)

        # An explicit, differing payload value wins over chain resolution.
        assert sc_next.base_url == user_base_url

        agent = _agent_for(sc_start.to_agent_config())
        assert sc_next.to_agent_config().base_url == user_base_url
        # base_url is not hot-swappable -> restart path (not a silent no-op).
        assert agent._can_hot_swap(sc_next.to_agent_config()) is False

    def test_provider_change_still_resolves_credentials(self, monkeypatch):
        _install_layered_resolver(monkeypatch)
        sc_start = _resolve_as_start_does(_start_like_session_config())

        payload = _frontend_continue_payload(sc_start)
        payload["provider_id"] = "p2"

        _, sc_next = _apply_continue_payload(sc_start, payload)

        assert sc_next.provider_id == "p2"
        # The new provider profile's credentials are adopted...
        assert sc_next.api_key == "sk-p2"
        assert sc_next.base_url == f"{SESSION_BASE_URL}/p2"
        # ...including its provider_config.
        assert sc_next.provider_config.get("timeout") == 30


class TestBlankPayloadValuesDoNotBlockResolution:
    """A blank payload field is "not set", not an override."""

    def test_blank_base_url_still_resolves(self, monkeypatch):
        _install_layered_resolver(monkeypatch)
        sc_start = _resolve_as_start_does(_start_like_session_config())

        payload = _frontend_continue_payload(sc_start)
        payload["base_url"] = ""

        _, sc_next = _apply_continue_payload(sc_start, payload)

        assert sc_next.base_url == f"{SESSION_BASE_URL}/p1"
