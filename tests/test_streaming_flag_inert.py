"""The ``streaming`` flag must be inert: ``Agent(streaming=True)`` behaves exactly
like ``Agent(streaming=False)``.

Wave 2 introduces the flag (on both ``Agent`` and ``LLMClient``) purely so wave 3
can flip the default; today the sync turn path must be bit-for-bit unaffected.
These tests pin that:

* the flag is stored on both objects and defaults to ``False``;
* the same puppet scenario produces an *identical* event stream either way
  (after stripping run-to-run volatile fields) -- the test that catches
  accidental wiring;
* ``process_query`` never yields an ``agent.core.stream_events`` object and
  never touches ``LLMClient.stream_completion``;
* ``LLMClient.stream_completion`` still raises ``NotImplementedError`` for a
  provider without streaming support -- on first iteration, because it is an
  async generator.

Harness copied from ``tests/test_puppet_agent_basic.py``: a real ``Agent`` with
``tests/mocks/puppet_agent.PuppetLLM`` installed at
``agent.llm_client.chat_completion``. No network, no real key, no logging.
Async style is the repo's: plain ``def`` tests plus ``asyncio.run`` (pytest-asyncio
is not installed).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from datetime import datetime as _real_datetime

import pytest

import agent.core.agent as agent_module
import agent.core.conversation_manager as conversation_manager_module
from agent.config.models import AgentConfig
from agent.core.agent import Agent
from agent.core.llm_client import LLMClient
from agent.core.stream_events import __all__ as STREAM_EVENT_EXPORTS
from agent.core import stream_events as stream_events_module
from agent.core.token_counter import TokenCounter
from tests.mocks.puppet_agent import PuppetLLM
from tests.assertions.event_assertions import (
    assert_event_sequence,
    assert_respond,
    assert_tool_call,
)

SESSION_ID = "test-streaming-flag"

#: Two one-call scenarios, each finishing the turn on the first LLM call.
#: NOTE: a plain ``assistant`` content response ENDS the turn (the agent
#: synthesises ``agent_responded`` from it), so a scenario of
#: ``[assistant, respond]`` would silently drop its second turn -- hence two
#: separate single-call scenarios instead of one two-turn scenario.
SCENARIOS = {
    # tool-dispatch path: the LLM calls Respond, the agent emits tool events
    "tool": [
        {"type": "respond", "content": "Done.", "status": "final", "confidence": "high"},
    ],
    # text path: plain assistant content
    "text": [
        {"type": "assistant", "content": "Hello from the puppet!"},
    ],
}

EXPECTED_TYPES = {
    "tool": [
        "token_update",
        "user_query",
        "execution_state_change",
        "token_update",
        "token_update",
        "turn",
        "tool_call_start",
        "tool_call",
        "tool_result",
        "agent_responded",
    ],
    "text": [
        "token_update",
        "user_query",
        "execution_state_change",
        "token_update",
        "token_update",
        "turn",
        "agent_responded",
    ],
}

#: Fields that legitimately differ between two runs (wall clock, and the
#: conversation identity the agent mints per instance). Everything else -- the
#: event order, the payloads, the response content, the tool calls -- must be
#: identical.
VOLATILE_KEYS = frozenset({
    "created_at",
    "timestamp",
    "conversation_timestamp",
    "conversation_hash",
    "conversation_id",
    "event_id",
})


def _agent(streaming=None):
    config = AgentConfig(api_key="test-key", enable_logging=False)
    kwargs = {} if streaming is None else {"streaming": streaming}
    return Agent(config=config, session_id=SESSION_ID, **kwargs)


def _run_scenario(streaming, scenario_key):
    """Run one canned scenario once and return (events, puppet)."""
    agent = _agent(streaming)
    puppet = PuppetLLM(scenario=[dict(turn) for turn in SCENARIOS[scenario_key]])
    agent.llm_client.chat_completion = puppet.chat_completion
    return list(agent.process_query("Do the thing")), puppet


#: The one instant both runs are frozen to -- a realistic 18-character float.
#: A round value such as ``1761600000.0`` reprs as 12 characters and shifts the
#: token estimate for reasons unrelated to the code under test.
FROZEN_INSTANT = 1793115664.7395215


@contextlib.contextmanager
def _frozen_clock(instant=FROZEN_INSTANT):
    """Freeze every wall-clock read the turn path stamps into the payload.

    Patches ``time.time`` (the source of ``created_at``:
    ``agent.core.conversation_manager:46``) and the *module-level* ``datetime``
    name in ``agent.core.conversation_manager`` and ``agent.core.agent``
    (``session.updated_at``). ``datetime.datetime`` itself is immutable and is
    deliberately not touched.

    Frozen because ``created_at`` is inside the tokenized payload -- without a
    freeze, OFF and ON runs differ by wall-clock instant, not by flag. The flag
    is inert on the estimate path; the input is not. See diagnosis doc
    (``.thoughtmachine/working_docs/diagnosis-streaming-flag-inert-2026-10-27.md``).
    Environment note: the local sandbox has no tiktoken vocab and no network, so
    it estimates via the ``len(json)//4`` fallback while CI uses
    ``cl100k_base``; the observed flake rate therefore differs between
    environments even though the root cause is the same, and a future reader
    must not expect identical rates.
    """

    class _FrozenDatetime(_real_datetime):
        @classmethod
        def now(cls, tz=None):
            return _real_datetime.fromtimestamp(instant, tz)

        @classmethod
        def utcnow(cls):
            return _real_datetime.utcfromtimestamp(instant)

        @classmethod
        def today(cls):
            return _real_datetime.fromtimestamp(instant)

    real_time = time.time
    real_time_ns = time.time_ns
    patched_modules = (conversation_manager_module, agent_module)
    original_datetimes = {module: module.datetime for module in patched_modules}

    time.time = lambda: instant
    time.time_ns = lambda: int(instant * 1_000_000_000)
    for module in patched_modules:
        module.datetime = _FrozenDatetime
    try:
        yield instant
    finally:
        time.time = real_time
        time.time_ns = real_time_ns
        for module, original in original_datetimes.items():
            module.datetime = original


@contextlib.contextmanager
def _record_estimator_input(sink):
    """Record the exact string ``TokenCounter.estimate_tokens`` encodes.

    The estimator hands non-``str`` messages through ``json.dumps`` before
    encoding, so the sink records that serialised form: two runs agreeing here
    must agree on ``context_length`` under any encoder -- the local
    ``len(json)//4`` fallback and CI's ``cl100k_base`` alike.
    """
    original = TokenCounter.estimate_tokens

    def recording_estimate(self, text, *args, **kwargs):
        if not isinstance(text, str):
            try:
                text = json.dumps(text)
            except (TypeError, ValueError):
                text = repr(text)
        sink.append(text)
        return original(self, text, *args, **kwargs)

    TokenCounter.estimate_tokens = recording_estimate
    try:
        yield sink
    finally:
        TokenCounter.estimate_tokens = original


def _normalise(obj):
    """Strip volatile fields recursively so two turns compare equal."""
    if isinstance(obj, dict):
        return {k: _normalise(v) for k, v in obj.items() if k not in VOLATILE_KEYS}
    if isinstance(obj, list):
        return [_normalise(v) for v in obj]
    return obj


STREAM_EVENT_CLASSES = tuple(
    getattr(stream_events_module, name)
    for name in STREAM_EVENT_EXPORTS
    if name != "StreamEvent"  # typing.Union, not a class
)


class TestStreamingFlagInert:
    # -- 1. default ------------------------------------------------------

    def test_default_is_off_on_agent_and_client(self):
        agent = _agent()

        assert agent.streaming is False
        assert agent.llm_client.streaming is False

    # -- 2. the flag is stored and threaded to the client ----------------

    def test_flag_is_stored_and_threaded_to_llm_client(self):
        agent = _agent(streaming=True)

        assert agent.streaming is True
        assert agent.llm_client.streaming is True

    # -- 3. THE KEY ONE: identical turn output either way -----------------

    @pytest.mark.parametrize("scenario_key", ["tool", "text"])
    def test_turn_output_is_identical_with_flag_off_and_on(self, scenario_key):
        # Both runs share one frozen instant, so any OFF/ON token difference can
        # only come from the flag -- not from the wall clock inside the payload.
        off_payloads = []
        on_payloads = []
        with _frozen_clock():
            with _record_estimator_input(off_payloads):
                off_events, off_puppet = _run_scenario(streaming=False, scenario_key=scenario_key)
            with _record_estimator_input(on_payloads):
                on_events, on_puppet = _run_scenario(streaming=True, scenario_key=scenario_key)

        # The puppet really drove both turns (this is not an empty comparison).
        assert off_puppet.call_count == 1, "the puppet must have answered"
        assert on_puppet.call_count == 1, "the puppet must have answered"

        # Same ordered event types...
        expected_types = EXPECTED_TYPES[scenario_key]
        assert [event["type"] for event in off_events] == expected_types
        assert [event["type"] for event in on_events] == expected_types

        # ...and the same payloads once volatile fields are stripped.
        assert [_normalise(event) for event in off_events] == [
            _normalise(event) for event in on_events
        ]

        # The event comparison above runs on *event* payloads; this one runs on
        # the estimator's own input, which is what the token count is computed
        # from. It is encoder-route independent, so it holds on the local
        # ``len//4`` fallback and on CI's ``cl100k_base`` alike: a real BPE
        # encoder can turn a same-length string change into a +/-1 token delta
        # that ``len//4`` cannot see, which is why the event-level comparison
        # alone cannot prove determinism.
        assert len(off_payloads) == len(on_payloads), (
            "the estimator was called "
            f"{len(off_payloads)}x with the streaming flag off but "
            f"{len(on_payloads)}x with it on -- the estimator input differed "
            "between runs, so this is a payload leak rather than a flag effect: "
            f"off={off_payloads!r} on={on_payloads!r}"
        )
        for index, (off_payload, on_payload) in enumerate(zip(off_payloads, on_payloads)):
            if off_payload == on_payload:
                continue
            offset = next(
                (
                    position
                    for position, (left, right) in enumerate(zip(off_payload, on_payload))
                    if left != right
                ),
                min(len(off_payload), len(on_payload)),
            )
            window = slice(max(0, offset - 80), offset + 80)
            pytest.fail(
                f"estimator input #{index} differed between the off and on runs "
                f"(first difference at character {offset}; lengths "
                f"{len(off_payload)} vs {len(on_payload)}): "
                f"off{window.start}:{window.stop}={off_payload[window]!r} vs "
                f"on{window.start}:{window.stop}={on_payload[window]!r}. "
                "The estimator input differed between runs, so this is a payload "
                "leak rather than a flag effect. If this fails on CI as a bare "
                "+/-1 context_length, the field quoted above is the culprit."
            )

        # Both turns satisfy the same behavioural assertions.
        for events in (off_events, on_events):
            if scenario_key == "tool":
                assert_event_sequence(events, ["turn", "tool_call", "agent_responded"])
                assert_tool_call(events, "Respond")
                assert_respond(events, expected_content="Done.", expected_status="final")
            else:
                assert_event_sequence(events, ["turn", "agent_responded"])
                assert_respond(events, expected_content="Hello from the puppet!")

    # -- 4. no streaming machinery leaks into the sync path --------------

    def test_sync_path_emits_no_stream_events_and_never_streams(self):
        agent = _agent(streaming=True)
        puppet = PuppetLLM(scenario=[dict(turn) for turn in SCENARIOS["tool"]])
        agent.llm_client.chat_completion = puppet.chat_completion

        stream_calls = []
        original_stream_completion = agent.llm_client.stream_completion

        def spy(*args, **kwargs):  # pragma: no cover - must never run
            stream_calls.append((args, kwargs))
            raise AssertionError(
                "LLMClient.stream_completion was called on the sync path with streaming=True"
            )

        agent.llm_client.stream_completion = spy
        try:
            events = list(agent.process_query("Do the thing"))
        finally:
            agent.llm_client.stream_completion = original_stream_completion

        assert stream_calls == [], "the sync path must not touch stream_completion"

        # process_query yields plain event dicts: nothing here is an instance of
        # a class exported by agent.core.stream_events.
        assert not any(isinstance(event, STREAM_EVENT_CLASSES) for event in events)
        assert all(isinstance(event, dict) for event in events)

        # Hardened form of the same claim: no dict claims to *be* a stream
        # event either (a leak could arrive as a serialised payload).
        stream_event_names = {cls.__name__ for cls in STREAM_EVENT_CLASSES}
        assert not any(event.get("type") in stream_event_names for event in events)

    # -- 5. NotImplementedError for a non-streaming provider -------------

    def test_stream_completion_raises_when_provider_cannot_stream(self):
        client = LLMClient(AgentConfig(api_key="test-key", enable_logging=False))

        class _NoStreamProvider:
            """Provider with no ``stream_completion`` attribute at all."""

            def chat_completion(self, messages, tools=None, **kwargs):  # pragma: no cover
                raise AssertionError("not used")

        client.provider = _NoStreamProvider()

        # The method is an async generator, so building it must NOT raise...
        stream = client.stream_completion(messages=[{"role": "user", "content": "hi"}])

        async def _drain():
            return [event async for event in stream]

        # ...the error surfaces on first iteration.
        with pytest.raises(NotImplementedError):
            asyncio.run(_drain())
