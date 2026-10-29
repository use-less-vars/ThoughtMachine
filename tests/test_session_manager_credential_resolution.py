"""
Regression tests: provider-credential resolution for a loaded session has
exactly ONE owner -- ``ConfigManager.resolve_full_config``.

``SessionManager.extract_session_config`` used to derive credentials locally
(``ProviderManager().resolve_config(...)`` plus a hand-rolled
``list_profiles()`` api_key fallback loop).  That private derivation was folded
away in commit 8977d4629 ("refactor(worker): split worker.py into lifecycle
modules") and replaced by a single call into the shared resolution chain.

These tests pin that fold so it cannot silently regress:

1. ``extract_session_config`` DELEGATES: every credential field it returns is
   traceable to the ``resolve_full_config`` payload -- sentinel values that
   could not be produced by any local derivation.
2. ``ProviderManager`` is NOT touched while extracting a session config.  Any
   re-introduced private derivation would have to instantiate it (that is how
   the old code resolved keys), so this is the guard.
3. Behavioural parity, i.e. the apply-to-``SessionConfig`` semantics are
   unchanged by the fold:
   - an echoed config keeps its stored api_key / base_url (fill-missing only);
   - blank credential fields are filled from the resolved chain;
   - a genuine provider/credential change does NOT overwrite the stored
     api_key, while ``provider_config`` is merged with resolved values winning.

KEEP IN SYNC: the fill-missing / provider_config-wins behaviour asserted in (3)
is the behaviour that existed BEFORE the fold and is deliberately preserved.
``Bridge.start`` and ``ConfigManager.apply_config`` apply the same resolved
payload with *different* gates (bridge skips the whole block when a key is
already present; apply_config overwrites on an explicit provider switch).  Those
two sites are out of scope here -- do not "unify" the assertions below without
also deciding those gates.
"""

import sys
import uuid
from pathlib import Path

# Add project root so that imports work
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from session.models import Session

import web_ui.backend.session_manager as session_manager_module
from web_ui.backend.session_manager import SessionManager


RESOLVED_API_KEY = "sk-resolved-from-chain"
RESOLVED_BASE_URL = "https://resolved.example.com/v1"


class _StubConfigManager:
    """
    Duck-typed stand-in for ``ConfigManager`` exposing only
    ``resolve_full_config`` -- the single shared resolution entry point that
    ``extract_session_config`` must delegate to.

    ``merged`` is returned verbatim (copied); every call's kwargs are recorded
    so tests can assert what was forwarded.
    """

    def __init__(self, merged):
        self.merged = merged
        self.calls = []

    def resolve_full_config(self, workspace_id=None, session_id=None,
                            provider_id=None, **kwargs):
        self.calls.append(
            {
                "workspace_id": workspace_id,
                "session_id": session_id,
                "provider_id": provider_id,
                "kwargs": kwargs,
            }
        )
        return dict(self.merged)


class _RaisingConfigManager:
    """Resolution chain blows up -- the session must still load."""

    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def resolve_full_config(self, **kwargs):
        self.calls += 1
        raise self.exc


class _SpyProviderManager:
    """
    Any instantiation or use of ``ProviderManager`` during
    ``extract_session_config`` is a regression: credentials must come from the
    shared chain, not from a private derivation.
    """

    instances = []

    def __init__(self, *args, **kwargs):
        _SpyProviderManager.instances.append(self)
        raise AssertionError(
            "ProviderManager was instantiated during extract_session_config: "
            "provider-credential derivation must live in "
            "ConfigManager.resolve_full_config only"
        )


def _make_manager(config_manager):
    """SessionManager with a stub store (extract_session_config never stores)."""
    return SessionManager(session_store=None, config_manager=config_manager)


def _make_session(session_config_metadata, workspace_id="ws-test-1"):
    return Session(
        session_id=str(uuid.uuid4()),
        user_history=[],
        workspace_id=workspace_id,
        metadata={"source": "web_ui", "session_config": session_config_metadata},
    )


# ── 1. Delegation: credentials come from resolve_full_config ────────────────

def test_extract_session_config_delegates_to_resolve_full_config():
    stub = _StubConfigManager(
        {
            "api_key": RESOLVED_API_KEY,
            "base_url": RESOLVED_BASE_URL,
            "provider_config": {"api_style": "openai"},
        }
    )
    manager = _make_manager(stub)
    session = _make_session({"provider_id": "p-resolved", "model": "m-1"})

    sc = manager.extract_session_config(session)

    assert sc is not None
    # Every credential field is a sentinel only the shared chain can produce.
    assert sc.api_key == RESOLVED_API_KEY
    assert sc.base_url == RESOLVED_BASE_URL
    assert sc.provider_config == {"api_style": "openai"}

    # Exactly one call, with the session's identity forwarded.
    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["workspace_id"] == "ws-test-1"
    assert call["session_id"] == session.session_id
    assert call["provider_id"] == "p-resolved"


def test_extract_session_config_does_not_derive_credentials_locally(monkeypatch):
    monkeypatch.setattr(
        session_manager_module, "ProviderManager", _SpyProviderManager
    )
    _SpyProviderManager.instances = []

    stub = _StubConfigManager({"api_key": RESOLVED_API_KEY})
    manager = _make_manager(stub)
    session = _make_session({"provider_id": "p-resolved", "model": "m-1"})

    sc = manager.extract_session_config(session)

    assert sc is not None
    assert sc.api_key == RESOLVED_API_KEY
    assert _SpyProviderManager.instances == []


# ── 2. Behavioural parity of the apply step (unchanged by the fold) ─────────

def test_stored_credentials_are_preserved_not_overwritten():
    """Fill-missing: a stored key/base_url wins, provider_config merge does not."""
    stub = _StubConfigManager(
        {
            "api_key": RESOLVED_API_KEY,
            "base_url": RESOLVED_BASE_URL,
            "provider_config": {"api_style": "openai", "max_tokens": 4096},
        }
    )
    manager = _make_manager(stub)
    session = _make_session(
        {
            "provider_id": "p-stored",
            "model": "m-stored",
            "api_key": "sk-stored",
            "base_url": "https://stored.example.com/v1",
            "provider_config": {"api_style": "anthropic", "temperature": 0.2},
        }
    )

    sc = manager.extract_session_config(session)

    assert sc is not None
    assert sc.api_key == "sk-stored"
    assert sc.base_url == "https://stored.example.com/v1"
    # provider_config always merges, and the resolved layer wins per key.
    assert sc.provider_config == {
        "api_style": "openai",      # resolved wins
        "temperature": 0.2,         # stored-only key survives
        "max_tokens": 4096,         # resolved-only key added
    }


def test_blank_credential_fields_are_filled_from_resolved_chain():
    stub = _StubConfigManager(
        {
            "api_key": RESOLVED_API_KEY,
            "base_url": RESOLVED_BASE_URL,
            "provider_config": {"api_style": "openai"},
        }
    )
    manager = _make_manager(stub)
    session = _make_session(
        {
            "provider_id": "p-blank",
            "model": "m-blank",
            "api_key": "",
            "base_url": "",
            "provider_config": {},
        }
    )

    sc = manager.extract_session_config(session)

    assert sc is not None
    assert sc.api_key == RESOLVED_API_KEY
    assert sc.base_url == RESOLVED_BASE_URL
    assert sc.provider_config == {"api_style": "openai"}


def test_no_credentials_invented_when_chain_resolves_none():
    stub = _StubConfigManager({})
    manager = _make_manager(stub)
    session = _make_session({"provider_id": "p-none", "model": "m-none"})

    sc = manager.extract_session_config(session)

    assert sc is not None
    assert not sc.api_key
    assert not sc.base_url
    assert sc.provider_config == {}


def test_resolution_failure_still_returns_config(capsys):
    """A broken chain must not abort the load -- it degrades to stored values."""
    manager = _make_manager(_RaisingConfigManager(RuntimeError("boom")))
    session = _make_session(
        {"provider_id": "p-x", "model": "m-x", "api_key": "sk-stored"}
    )

    sc = manager.extract_session_config(session)

    assert sc is not None
    assert sc.api_key == "sk-stored"
    # agent.logging.log() renders through rich to stderr, not stdlib logging.
    assert "Could not resolve API key" in capsys.readouterr().err
