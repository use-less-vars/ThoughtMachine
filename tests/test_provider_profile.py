"""Tests for provider profile timeout / max_retries plumbing.

Covers the fix where ``ProviderManager.resolve_config()`` dropped the
profile's ``timeout`` / ``max_retries`` fields, so LLMClient silently fell
back to its 120s / 3 defaults:

- ``resolve_config()`` copies profile ``timeout`` / ``max_retries`` into
  ``provider_config``; missing / None values create no keys.
- End-to-end: resolved ``provider_config`` reaches LLMClient and the
  LLM_TIMEOUT / LLM_MAX_RETRIES env vars still take precedence over the
  profile values.
"""
from types import SimpleNamespace

import pytest

from agent.config.provider_profile import ProviderProfile, ProviderManager
from agent.core.llm_client import LLMClient
from llm_providers.factory import ProviderFactory


def _manager(tmp_path, profiles):
    """A ProviderManager preloaded with profiles, no real file."""
    mgr = ProviderManager(file_path=tmp_path / "test_providers.json")
    mgr._profiles = profiles
    return mgr


def _profile(**overrides):
    defaults = dict(
        id="test-provider",
        label="Test Provider",
        provider_type="openai_compatible",
        base_url="https://api.example.com/v1",
        api_key="sk-test-key",
        default_model="test-model",
    )
    defaults.update(overrides)
    return ProviderProfile(**defaults)


def _llm_stub(resolved):
    """Minimal AgentConfig stub carrying only the attrs LLMClient touches."""
    return SimpleNamespace(
        provider_type=resolved.get("provider_type", "openai_compatible"),
        api_key=resolved.get("api_key", "test-key"),
        base_url=resolved.get("base_url"),
        model=resolved.get("model", "test-model"),
        temperature=0.2,
        provider_config=resolved.get("provider_config") or {},
    )


def _capture_create_provider(monkeypatch):
    """Replace ProviderFactory.create_provider with a kwargs recorder."""
    captured = {}

    def fake_create(provider_type, api_key=None, **kwargs):
        captured['call'] = dict(provider_type=provider_type, api_key=api_key, **kwargs)
        return object()

    monkeypatch.setattr(ProviderFactory, 'create_provider', staticmethod(fake_create))
    return captured


# ---------------------------------------------------------------------------
# resolve_config(): profile timeout / max_retries -> provider_config
# ---------------------------------------------------------------------------

def test_profile_timeout_and_max_retries_copied(tmp_path):
    """Profile timeout/max_retries land in provider_config when set."""
    mgr = _manager(tmp_path, {"p": _profile(timeout=5, max_retries=2)})
    result = mgr.resolve_config({"provider_id": "p"})
    assert result["provider_config"] == {"timeout": 5, "max_retries": 2}


def test_missing_max_retries_inserts_no_key(tmp_path):
    """max_retries=None must not insert an empty 'max_retries' key."""
    mgr = _manager(tmp_path, {"p": _profile(timeout=30)})  # max_retries defaults to None
    result = mgr.resolve_config({"provider_id": "p"})
    assert result["provider_config"]["timeout"] == 30
    assert "max_retries" not in result["provider_config"]


def test_default_profile_timeout_kept(tmp_path):
    """A profile with default timeout (120) still yields timeout=120."""
    mgr = _manager(tmp_path, {"p": _profile()})
    result = mgr.resolve_config({"provider_id": "p"})
    assert result["provider_config"] == {"timeout": 120}


def test_no_provider_id_returns_unchanged(tmp_path):
    """Without a provider_id, resolve_config returns the dict as-is."""
    mgr = _manager(tmp_path, {"p": _profile(timeout=5, max_retries=2)})
    config = {"model": "gpt-4"}
    result = mgr.resolve_config(config)
    assert result == config
    assert "provider_config" not in result


def test_existing_provider_config_preserved(tmp_path):
    """User-supplied provider_config keys survive, profile keys are merged in."""
    mgr = _manager(tmp_path, {"p": _profile(timeout=5, max_retries=2)})
    result = mgr.resolve_config({"provider_id": "p", "provider_config": {"custom": "keep"}})
    assert result["provider_config"] == {"custom": "keep", "timeout": 5, "max_retries": 2}


# ---------------------------------------------------------------------------
# End-to-end: resolve_config -> LLMClient (env still wins)
# ---------------------------------------------------------------------------

def test_profile_values_reach_llm_client(tmp_path, monkeypatch):
    """Resolved profile timeout/max_retries are used by LLMClient."""
    monkeypatch.delenv("LLM_TIMEOUT", raising=False)
    monkeypatch.delenv("LLM_MAX_RETRIES", raising=False)
    mgr = _manager(tmp_path, {"p": _profile(timeout=5, max_retries=2)})
    resolved = mgr.resolve_config({"provider_id": "p"})
    captured = _capture_create_provider(monkeypatch)
    LLMClient(_llm_stub(resolved))
    assert captured["call"]["timeout"] == 5
    assert captured["call"]["max_retries"] == 2


def test_env_still_overrides_profile_values(tmp_path, monkeypatch):
    """LLM_TIMEOUT / LLM_MAX_RETRIES env vars beat profile values."""
    monkeypatch.setenv("LLM_TIMEOUT", "99")
    monkeypatch.setenv("LLM_MAX_RETRIES", "5")
    mgr = _manager(tmp_path, {"p": _profile(timeout=5, max_retries=2)})
    resolved = mgr.resolve_config({"provider_id": "p"})
    captured = _capture_create_provider(monkeypatch)
    LLMClient(_llm_stub(resolved))
    assert captured["call"]["timeout"] == 99
    assert captured["call"]["max_retries"] == 5


def test_profile_without_retries_uses_llm_default(tmp_path, monkeypatch):
    """No max_retries on the profile -> LLMClient default (3), timeout=profile."""
    monkeypatch.delenv("LLM_TIMEOUT", raising=False)
    monkeypatch.delenv("LLM_MAX_RETRIES", raising=False)
    mgr = _manager(tmp_path, {"p": _profile(timeout=25)})  # max_retries None
    resolved = mgr.resolve_config({"provider_id": "p"})
    captured = _capture_create_provider(monkeypatch)
    LLMClient(_llm_stub(resolved))
    assert captured["call"]["timeout"] == 25
    assert captured["call"]["max_retries"] == 3
