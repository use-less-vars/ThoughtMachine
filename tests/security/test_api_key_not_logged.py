"""API key redaction: the raw key must never reach any log output.

Background
----------
``AgentConfig.api_key`` is wrapped in :class:`agent.config.secret.Secret`, a
``str`` subclass whose display forms (``str()``, ``repr()``, ``__format__``)
render ``<secret>``.  Provider adapters reveal the raw value only where it
crosses into a third-party SDK, and ``_redact_headers()`` scrubs
credential-shaped header values.

Why the capture points matter
-----------------------------
``agent/logging/redaction.py`` already scrubs ``sk-...`` substrings in the
*file* sink (it rewrites them to ``<REDACTED>``).  A test that only captures
file/JSONL output would therefore pass against the **unfixed** code and prove
nothing.  This module therefore captures the two pre-redaction sinks:

1. the console sink - ``print(console_msg, file=sys.stderr)`` in
   ``agent/logging/unified.py``, which receives the fully formatted message;
2. the structured sink - ``logger._log_event(message=..., data=...)``, called
   with the caller's message/data before any file redaction.

Case is diagnostic: ``<REDACTED>`` (uppercase) is the post-hoc file scrub,
``<secret>`` / ``<redacted>`` (lowercase) come from the wrapper and
``_redact_headers`` respectively.  The sinks captured here never run the file
scrub, so a lowercase marker can only come from the class-level redaction.
"""

from __future__ import annotations

import json

import pytest

from agent.config.models import AgentConfig
from agent.config.secret import Secret
from llm_providers.base import ProviderConfig, _redact_headers
from llm_providers.openai_compatible import OpenAICompatibleProvider


#: Canary key.  Matches the ``sk-[A-Za-z0-9_-]+`` shape so it also exercises
#: the header-redaction regex and the file-sink scrub (which we deliberately
#: do NOT rely on).
CANARY = "sk-canary-DO-NOT-USE-1234567890"


# ── capture plumbing ────────────────────────────────────────────────────────


class _RecordingLogger:
    """Stand-in for ``_AgentLogger`` that records pre-redaction log events."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.current_turn = 0

    def _log_event(self, event_type=None, level=None, message=None, data=None, turn=0):
        self.events.append(f"{level} {message} {data}")

    def text(self) -> str:
        return "\n".join(self.events)


class _StubCompletions:
    def create(self, **kwargs):  # pragma: no cover - never returns
        raise RuntimeError("stub client: no network in this test")


class _StubChat:
    def __init__(self) -> None:
        self.completions = _StubCompletions()


class _StubClient:
    """Minimal OpenAI-client stand-in: only what the log sites touch."""

    base_url = "https://stub.invalid/v1"

    def __init__(self) -> None:
        self.chat = _StubChat()


class _StubEncoding:
    def encode(self, text):
        return []


def _canary_log_text(capfd) -> str:
    """Drive provider construction + one call; return ALL captured log text.

    Captures both pre-redaction sinks (console stdout/stderr and the
    structured ``_log_event`` sink) and restores the logging configuration
    afterwards, so the test cannot leak state into the rest of the suite.
    """
    import agent.logging.unified as unified
    from agent.logging import set_log_level, set_log_tags
    from llm_providers.factory import ProviderFactory

    set_logger = unified.set_logger  # not re-exported from agent.logging

    recorder = _RecordingLogger()
    previous_logger = unified._logger
    previous_level = set_log_level("DEBUG")
    previous_tags = set_log_tags("*")
    set_logger(recorder)
    console = ""
    try:
        # The key enters through AgentConfig exactly as in production, then
        # travels the production path: llm_client -> ProviderFactory ->
        # provider adapter.
        config = AgentConfig(
            api_key=CANARY,
            base_url="https://stub.invalid/v1",
            model="gpt-4",
            provider_type="openai_compatible",
        )
        headers = {
            "Authorization": f"Bearer {CANARY}",
            "X-Api-Key": CANARY,
            "X-Trace-Id": "trace-123",
        }
        provider = ProviderFactory.create_provider(
            provider_type=config.provider_type,
            api_key=config.api_key,
            base_url=config.base_url,
            model=config.model,
            temperature=config.temperature,
            timeout=5,
            max_retries=0,
            extra_headers=headers,
        )
        # Keep tiktoken offline and the transport local.
        provider.encoding = _StubEncoding()
        provider.client = _StubClient()
        try:
            provider.chat_completion([{"role": "user", "content": "hello"}])
        except Exception:
            # The response path is out of scope: every log site of interest has
            # already fired by the time the stub transport raises.
            pass
        console = _read_console(capfd)
    finally:
        set_logger(previous_logger)
        set_log_level(previous_level)
        set_log_tags(previous_tags)
        if not console:
            console = _read_console(capfd)
    return console + "\n" + recorder.text()


def _read_console(capfd) -> str:
    captured = capfd.readouterr()
    return captured.out + captured.err


# ── the two-sided log assertions ────────────────────────────────────────────


def test_api_key_never_appears_in_log_output(capfd):
    """Negative side: the canary appears nowhere in any captured log text."""
    log_text = _canary_log_text(capfd)
    assert log_text.strip(), "test is vacuous: no log output was captured at all"
    assert CANARY not in log_text, (
        "RAW API KEY LEAKED INTO LOG OUTPUT\n"
        "Offending lines:\n"
        + "\n".join(line for line in log_text.splitlines() if CANARY in line)
    )
    # The key must not appear in any of its common mangled forms either.
    for fragment in (CANARY[3:], CANARY[-10:]):
        assert fragment not in log_text, (
            f"partial key fragment {fragment!r} leaked into log output"
        )


def test_log_output_contains_redaction_marker(capfd):
    """Positive side: redaction is visible, i.e. the leak was scrubbed, not
    merely never logged because logging was silent."""
    log_text = _canary_log_text(capfd)
    lowered = log_text.lower()
    assert "<secret>" in lowered or "<redacted>" in lowered, (
        "no redaction marker found - either the wrapper is not installed or "
        "the log sites were dropped entirely:\n" + log_text
    )


# ── wrapper surfaces (duty 2) ───────────────────────────────────────────────


def test_wrapper_redacts_every_display_surface():
    """f-string, %s, str(), repr(), format(), print(), container repr()."""
    config = AgentConfig(api_key=CANARY)
    key = config.api_key
    assert isinstance(key, str), "Secret must remain a str subclass"
    assert isinstance(key, Secret)
    assert f"{key}" == "<secret>"
    assert "%s" % key == "<secret>"
    assert str(key) == "<secret>"
    assert repr(key) == "<secret>"
    assert "{}".format(key) == "<secret>"
    assert f"{key:>10}" == "  <secret>"
    assert repr({"api_key": key}) == "{'api_key': <secret>}"
    assert repr([key]) == "[<secret>]"
    # The underlying value is still reachable - and only - via reveal().
    assert key.reveal() == CANARY
    assert key == CANARY  # comparisons keep working on the payload
    assert bool(key) is True


def test_wrapper_keeps_empty_key_behaviour():
    """No key configured behaves exactly as before."""
    config = AgentConfig()
    assert config.api_key == ""
    assert not config.api_key


def test_model_dump_never_carries_the_key():
    """Serialisation surface: the key stays out of every dump."""
    config = AgentConfig(api_key=CANARY)
    assert "api_key" not in config.model_dump()
    assert "api_key" not in config.model_dump(exclude_none=True)
    assert CANARY not in config.model_dump_json()
    assert CANARY not in json.dumps(config.model_dump(mode="json"))


# ── header redaction (duty 3) ───────────────────────────────────────────────


def test_redact_headers_covers_credential_shapes():
    headers = {
        "Authorization": f"Bearer {CANARY}",
        "Proxy-Authorization": "Basic dXNlcjpwYXNz",
        "X-Api-Key": CANARY,
        "X-Trace-Id": "trace-123",
        "X-Empty": "",
    }
    redacted = _redact_headers(headers)
    assert redacted["Authorization"] == "<redacted>"
    assert redacted["Proxy-Authorization"] == "<redacted>"
    assert redacted["X-Api-Key"] == "<redacted>"
    assert redacted["X-Trace-Id"] == "trace-123", "non-secret header must stay debuggable"
    assert redacted["X-Empty"] == ""
    # Input must not be mutated.
    assert headers["Authorization"] == f"Bearer {CANARY}"
    assert CANARY not in json.dumps(redacted)


def test_redact_headers_is_total():
    """Never raises on empty / None / odd input."""
    assert _redact_headers(None) == {}
    assert _redact_headers({}) == {}


# ── reveal sites (duty 1) ───────────────────────────────────────────────────


def test_openai_adapter_reveals_only_at_the_sdk_boundary():
    """The provider keeps the wrapper; the SDK receives the raw value."""
    provider = OpenAICompatibleProvider(ProviderConfig(api_key=Secret(CANARY)))
    assert isinstance(provider.config.api_key, Secret)
    assert provider.config.api_key.reveal() == CANARY
    # The OpenAI SDK stores the key it was constructed with.
    assert provider.client.api_key == CANARY


def test_anthropic_adapter_reveals_only_at_the_sdk_boundary():
    anthropic = pytest.importorskip("anthropic")
    from llm_providers.anthropic_provider import AnthropicProvider

    provider = AnthropicProvider(ProviderConfig(api_key=Secret(CANARY)))
    assert isinstance(provider.config.api_key, Secret)
    assert provider.client.api_key == CANARY
