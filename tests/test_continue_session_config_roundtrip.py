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

from agent.config.models import AgentConfig, normalize_base_url
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


class TestBaseUrlSpellingIsNotAConfigChange:
    """``base_url`` spelling (trailing slash, host casing) is not a change.

    The frontend fallback config ships ``.../v1/`` while the session/profile
    layers ship ``.../v1``.  Without normalisation that lone trailing slash
    made ``Agent._configs_are_identical`` say "different", ``_can_hot_swap``
    say "no" (``base_url`` is RESTART_REQUIRED) and every turn pay a full
    restart plus a "Configuration updated" notification for a config the user
    never touched.
    """

    def test_trailing_slash_is_not_a_config_change(self, monkeypatch):
        _install_layered_resolver(monkeypatch)
        sc_start = _resolve_as_start_does(_start_like_session_config())
        agent_config_start = sc_start.to_agent_config()
        assert agent_config_start.base_url == f"{SESSION_BASE_URL}/p1"

        slashed = agent_config_start.model_copy(
            update={"base_url": agent_config_start.base_url + "/"}
        )
        # The raw strings really do differ - only normalisation may hide it.
        assert slashed.base_url != agent_config_start.base_url

        agent = _agent_for(agent_config_start)
        messages_before = len(agent.conversation)

        assert agent._configs_are_identical(agent_config_start, slashed), (
            "a trailing slash on base_url was treated as a semantic change"
        )

        agent.request_config_update(slashed)
        assert agent._apply_pending_config() is True
        assert agent._pending_config is None
        assert len(agent.conversation) == messages_before, (
            "a trailing-slash-only base_url change produced a notification"
        )

    def test_host_case_is_not_a_config_change(self, monkeypatch):
        _install_layered_resolver(monkeypatch)
        sc_start = _resolve_as_start_does(_start_like_session_config())
        agent_config_start = sc_start.to_agent_config()

        cased = agent_config_start.model_copy(
            update={
                "base_url": agent_config_start.base_url.replace(
                    "session-layer.example", "SESSION-Layer.EXAMPLE"
                )
            }
        )
        assert cased.base_url != agent_config_start.base_url

        agent = _agent_for(agent_config_start)
        assert agent._configs_are_identical(agent_config_start, cased), (
            "a host-case difference in base_url was treated as a change"
        )

    def test_version_path_is_still_significant(self, monkeypatch):
        """Over-normalisation guard: bare host and ``/v1`` are NOT equal."""
        _install_layered_resolver(monkeypatch)
        sc_start = _resolve_as_start_does(_start_like_session_config())

        with_v1 = sc_start.to_agent_config().model_copy(
            update={"base_url": "https://api.openai.com/v1"}
        )
        bare = sc_start.to_agent_config().model_copy(
            update={"base_url": "https://api.openai.com"}
        )

        agent = _agent_for(with_v1)
        assert agent._configs_are_identical(with_v1, bare) is False
        assert agent._can_hot_swap(bare) is False

    def test_real_host_change_still_notifies(self, monkeypatch):
        _install_layered_resolver(monkeypatch)
        sc_start = _resolve_as_start_does(_start_like_session_config())

        old_config = sc_start.to_agent_config().model_copy(
            update={"base_url": "https://api.deepseek.com/v1"}
        )
        new_config = sc_start.to_agent_config().model_copy(
            update={"base_url": "https://other-provider.example/v1"}
        )

        agent = _agent_for(old_config)
        assert agent._configs_are_identical(old_config, new_config) is False

        messages_before = len(agent.conversation)
        agent._notify_config_change(old_config, new_config)

        assert len(agent.conversation) == messages_before + 1
        notification = agent.conversation[-1]
        assert notification.get("is_system_notification") is True
        assert "Configuration updated" in notification.get("content", "")
        assert "https://other-provider.example/v1" in notification["content"]


class TestNormalizeBaseUrl:
    """Direct unit coverage for the comparison helper."""

    @pytest.mark.parametrize("raw", [None, "", "   ", "\n\t "])
    def test_blank_values_become_none(self, raw):
        assert normalize_base_url(raw) is None

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("https://api.deepseek.com/v1/", "https://api.deepseek.com/v1"),
            ("https://api.deepseek.com/v1", "https://api.deepseek.com/v1"),
            ("https://api.deepseek.com/", "https://api.deepseek.com"),
            ("https://api.deepseek.com", "https://api.deepseek.com"),
            ("  https://api.deepseek.com/v1/  ", "https://api.deepseek.com/v1"),
        ],
    )
    def test_trailing_slashes_are_removed(self, raw, expected):
        result = normalize_base_url(raw)
        assert result == expected
        assert not result.endswith("/")

    def test_scheme_and_host_are_lowercased(self):
        assert (
            normalize_base_url("HTTPS://API.DeepSeek.COM/v1")
            == "https://api.deepseek.com/v1"
        )

    def test_path_case_is_preserved(self):
        assert (
            normalize_base_url("https://api.example.com/MyEndpoint/")
            == "https://api.example.com/MyEndpoint"
        )

    def test_version_suffix_is_never_collapsed(self):
        assert normalize_base_url("https://api.openai.com/v1") != normalize_base_url(
            "https://api.openai.com"
        )

    @pytest.mark.parametrize(
        "garbage", ["http://[::1", ":////", "not a url", "http://", "\x00"]
    )
    def test_garbage_never_raises(self, garbage):
        normalize_base_url(garbage)
