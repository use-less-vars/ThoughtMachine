"""Streaming contract tests for ``OpenAICompatibleProvider.stream_completion``.

The provider is wired to a fake async SDK client at the ``_async_client``
seam -- the very attribute ``_ensure_async_client()`` lazily populates, and
which it only touches when it is ``None``. No network, no real API key, no
retry/timeout machinery.

Chunks are built from the *real* SDK types (``ChatCompletionChunk`` from
``openai.types.chat``) rather than ``SimpleNamespace`` stand-ins, so the
adapter is exercised against the shapes the SDK actually produces.

Async style follows the rest of this repo: plain ``def`` tests driving
``asyncio.run`` -- there is no pytest-asyncio (not installed) and no anyio
pytest plugin configured, so no new plugin is introduced here.
"""

import asyncio
import inspect
import json
from types import SimpleNamespace

import pytest

from openai import APITimeoutError
from openai.types import CompletionUsage
from openai.types.chat import ChatCompletionChunk
from openai.types.chat.chat_completion_chunk import (
    Choice,
    ChoiceDelta,
    ChoiceDeltaToolCall,
    ChoiceDeltaToolCallFunction,
)

from agent.core.stream_events import (
    Done,
    Interrupted,
    TextDelta,
    ToolCallArgumentsDelta,
    ToolCallComplete,
    ToolCallStart,
    UsageUpdate,
)
from llm_providers.base import ProviderConfig
from llm_providers.openai_compatible import OpenAICompatibleProvider

MODEL = 'gpt-4o-mini'
MESSAGES = [{'role': 'user', 'content': 'read /tmp/x.txt'}]

TOOLS = [
    {
        'type': 'function',
        'function': {
            'name': 'ReadFile',
            'description': 'Read a text file.',
            'parameters': {
                'type': 'object',
                'properties': {'file_path': {'type': 'string'}},
                'required': ['file_path'],
            },
        },
    }
]


# ---------------------------------------------------------------------------
# Fake async SDK surface (chat.completions.create -> FakeStream)
# ---------------------------------------------------------------------------

class FakeStream:
    """Stand-in for the SDK's ``AsyncStream``.

    Iterating yields the queued items in order; a queued ``BaseException``
    instance is raised from ``__anext__`` to simulate a failure *mid*-stream.
    """

    def __init__(self, items):
        self._items = list(items)
        self.closed = False
        self.close_calls = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        item = self._items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self):
        self.close_calls += 1
        self.closed = True


class _FakeCompletions:
    """``client.chat.completions`` stand-in: records every ``create`` call.

    ``attempts`` is an optional list of item lists, one per ``create()`` call:
    the n-th call drains ``attempts[n]``. It exists so a retry test can make
    the first attempt fail and the second succeed. Once ``attempts`` is
    exhausted (or when it is not given at all) every call replays ``items``.
    """

    def __init__(self, items=None, create_exc=None, attempts=None):
        self._items = list(items or [])
        self._create_exc = create_exc
        self._attempts = [list(attempt) for attempt in attempts] if attempts is not None else None
        self.calls = []
        self.streams = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._create_exc is not None:
            raise self._create_exc
        if self._attempts:
            items = self._attempts.pop(0)
        else:
            items = self._items
        stream = FakeStream(items)
        self.streams.append(stream)
        return stream


class FakeAsyncClient:
    def __init__(self, items=None, create_exc=None, attempts=None):
        self.chat = SimpleNamespace(completions=_FakeCompletions(items, create_exc, attempts))


def _provider(items=None, create_exc=None, attempts=None, max_retries=None):
    """Build a provider with the fake async client already installed."""
    overrides = {} if max_retries is None else {'max_retries': max_retries}
    provider = OpenAICompatibleProvider(
        ProviderConfig(api_key='sk-test', model=MODEL, timeout=5, **overrides)
    )
    # ``_ensure_async_client()`` only builds a real ``AsyncOpenAI`` when the
    # attribute is missing/None, so pre-seeding it is the supported seam.
    provider._async_client = FakeAsyncClient(items, create_exc, attempts)
    return provider


# ---------------------------------------------------------------------------
# Real-SDK chunk builders
# ---------------------------------------------------------------------------

def _chunk(choices, usage=None):
    return ChatCompletionChunk(
        id='chunk-1',
        object='chat.completion.chunk',
        created=0,
        model=MODEL,
        choices=choices,
        usage=usage,
    )


def _text_chunk(text, finish_reason=None):
    return _chunk([Choice(index=0, delta=ChoiceDelta(content=text), finish_reason=finish_reason)])


def _empty_chunk(finish_reason=None):
    return _chunk([Choice(index=0, delta=ChoiceDelta(), finish_reason=finish_reason)])


def _tool_chunk(index=0, call_id=None, name=None, arguments=None, finish_reason=None):
    return _chunk([
        Choice(
            index=0,
            delta=ChoiceDelta(
                tool_calls=[
                    ChoiceDeltaToolCall(
                        index=index,
                        id=call_id,
                        function=ChoiceDeltaToolCallFunction(name=name, arguments=arguments),
                    )
                ]
            ),
            finish_reason=finish_reason,
        )
    ])


def _usage_chunk(prompt_tokens, completion_tokens):
    # Providers send usage on a final chunk with no choices at all.
    return _chunk(
        [],
        usage=CompletionUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _drain(provider, **kwargs):
    """Collect every event from one stream (raising anything that escapes)."""
    async def _run():
        return [event async for event in provider.stream_completion(messages=MESSAGES, **kwargs)]

    return asyncio.run(_run())


def _kinds(events):
    return [type(event).__name__ for event in events]


def _only(events, cls):
    found = [event for event in events if isinstance(event, cls)]
    assert len(found) == 1, f'expected exactly one {cls.__name__}, got {_kinds(events)}'
    return found[0]


# ---------------------------------------------------------------------------
# 0. The shape of the adapter method itself
# ---------------------------------------------------------------------------

def test_stream_completion_is_an_async_generator_function():
    """The adapter entry point must stay a true async generator.

    ``LLMClient.stream_completion`` delegates with ``async for``; a coroutine
    returning an async iterator would silently break that delegate.
    """
    assert inspect.isasyncgenfunction(OpenAICompatibleProvider.stream_completion) is True


# ---------------------------------------------------------------------------
# 1. Text
# ---------------------------------------------------------------------------

def test_text_stream_emits_text_deltas_in_order_then_done():
    provider = _provider([
        _text_chunk('Hel'),
        _text_chunk('lo, '),
        _text_chunk('world', finish_reason='stop'),
    ])

    events = _drain(provider)

    assert _kinds(events) == ['TextDelta', 'TextDelta', 'TextDelta', 'Done']
    assert [event.text for event in events[:3]] == ['Hel', 'lo, ', 'world']
    assert events[-1] == Done()
    assert not any(isinstance(event, Interrupted) for event in events)

    # Request shape: streaming forced on, usage requested by default.
    call = provider._async_client.chat.completions.calls[0]
    assert call['stream'] is True
    assert call['stream_options'] == {'include_usage': True}
    assert call['model'] == MODEL
    assert call['messages'] == MESSAGES
    assert 'tools' not in call


# ---------------------------------------------------------------------------
# 2. One tool call spread across fragments
# ---------------------------------------------------------------------------

ARGUMENT_FRAGMENTS = ['{"file_', 'path": "/tmp/', 'x.txt"}']
FULL_ARGUMENTS = ''.join(ARGUMENT_FRAGMENTS)


def test_tool_call_fragments_yield_start_deltas_and_one_complete():
    provider = _provider([
        # first fragment: the only one carrying id + name
        _tool_chunk(index=0, call_id='call_1', name='ReadFile', arguments=ARGUMENT_FRAGMENTS[0]),
        # later fragments: index only (id stays None, as the real SDK sends it)
        _tool_chunk(index=0, arguments=ARGUMENT_FRAGMENTS[1]),
        _tool_chunk(index=0, arguments=ARGUMENT_FRAGMENTS[2]),
        _empty_chunk(finish_reason='tool_calls'),
    ])

    events = _drain(provider, tools=TOOLS)

    assert _kinds(events) == [
        'ToolCallStart',
        'ToolCallArgumentsDelta',
        'ToolCallArgumentsDelta',
        'ToolCallArgumentsDelta',
        'ToolCallComplete',
        'Done',
    ]

    start = _only(events, ToolCallStart)
    assert (start.id, start.name) == ('call_1', 'ReadFile')

    deltas = [event for event in events if isinstance(event, ToolCallArgumentsDelta)]
    assert len(deltas) == len(ARGUMENT_FRAGMENTS)
    assert all(delta.id == 'call_1' for delta in deltas)
    assert ''.join(delta.arguments_delta for delta in deltas) == FULL_ARGUMENTS

    complete = _only(events, ToolCallComplete)
    assert complete.tool_call.id == 'call_1'
    assert complete.tool_call.name == 'ReadFile'
    assert complete.tool_call.arguments == json.loads(FULL_ARGUMENTS) == {'file_path': '/tmp/x.txt'}
    assert complete.tool_call.raw_arguments == FULL_ARGUMENTS
    assert complete.parse_error is None
    assert complete.tool_call.parse_error is None

    call = provider._async_client.chat.completions.calls[0]
    assert call['tools'] == TOOLS
    assert call['tool_choice'] == 'auto'


# ---------------------------------------------------------------------------
# 3. Usage
# ---------------------------------------------------------------------------

def test_usage_chunk_emits_single_usage_update():
    provider = _provider([
        _text_chunk('hi', finish_reason='stop'),
        _usage_chunk(prompt_tokens=11, completion_tokens=7),
    ])

    events = _drain(provider)

    assert _kinds(events) == ['TextDelta', 'UsageUpdate', 'Done']
    usage = _only(events, UsageUpdate)
    # prompt_tokens -> input_tokens, completion_tokens -> output_tokens
    assert usage.input_tokens == 11
    assert usage.output_tokens == 7
    assert isinstance(events[-1], Done)


# ---------------------------------------------------------------------------
# 4. Malformed tool arguments
# ---------------------------------------------------------------------------

def test_malformed_arguments_complete_with_parse_error_and_empty_arguments():
    provider = _provider([
        _tool_chunk(index=0, call_id='call_bad', name='ReadFile', arguments='{"a": '),
        _empty_chunk(finish_reason='tool_calls'),
    ])

    events = _drain(provider, tools=TOOLS)

    complete = _only(events, ToolCallComplete)
    assert complete.parse_error, 'invalid JSON must set parse_error on the event'
    assert complete.tool_call.parse_error == complete.parse_error, (
        'both parse_error fields must come from the single parse attempt'
    )
    # inv 6: a malformed call carries no half-parsed arguments
    assert complete.tool_call.arguments == {}
    assert complete.tool_call.raw_arguments == '{"a": '
    assert complete.tool_call.id == 'call_bad'
    assert complete.tool_call.name == 'ReadFile'
    # a bad tool call is not an interruption
    assert not any(isinstance(event, Interrupted) for event in events)
    assert events[-1] == Done()


# ---------------------------------------------------------------------------
# 5. Errors
# ---------------------------------------------------------------------------

def test_error_mid_stream_ends_with_interrupted_event():
    provider = _provider([
        _text_chunk('partial'),
        RuntimeError('provider exploded'),
    ])

    events = _drain(provider)  # must not raise

    assert _kinds(events) == ['TextDelta', 'Interrupted']
    interrupted = events[-1]
    assert isinstance(interrupted, Interrupted)
    assert interrupted.reason == 'provider_error: RuntimeError'
    assert not any(isinstance(event, Done) for event in events)
    # the failed stream is still closed
    assert provider._async_client.chat.completions.streams[0].closed is True


def test_create_failure_also_ends_with_interrupted_event():
    """``create()`` itself raising lands in the same ``except`` clause.

    The adapter awaits ``create()`` *inside* the try block, so a connection
    failure is reported as an ``Interrupted`` event rather than escaping to
    the caller -- no divergence from the brief here.
    """
    provider = _provider(create_exc=RuntimeError('connect failed'))

    events = _drain(provider)

    assert _kinds(events) == ['Interrupted']
    assert events[0].reason == 'provider_error: RuntimeError'


# ---------------------------------------------------------------------------
# 6. stream_options opt-out
# ---------------------------------------------------------------------------

def test_explicit_stream_options_is_not_overwritten_and_stream_is_forced():
    explicit_options = {'include_usage': False}
    provider = _provider([_text_chunk('x', finish_reason='stop')])

    _drain(provider, stream_options=explicit_options)

    call = provider._async_client.chat.completions.calls[0]
    assert call['stream_options'] == explicit_options, 'an explicit opt-out must survive'
    assert call['stream'] is True, 'stream=True is forced by the adapter'


# ---------------------------------------------------------------------------
# 7. Cancellation
# ---------------------------------------------------------------------------

def test_cancellation_propagates_without_interrupted_event():
    """Breaking out of the stream must not fabricate an ``Interrupted``.

    Technique: explicit ``agen.aclose()`` after the ``break``. A bare
    ``break`` leaves the generator suspended at its yield until GC closes it
    (nondeterministic); ``Task.cancel()`` needs a wrapping task and makes the
    assertion about *how* the loop unwinds rather than about what the adapter
    emits. ``aclose()`` throws ``GeneratorExit`` at the yield point, which is
    exactly the path ``except (GeneratorExit, asyncio.CancelledError): raise``
    guards, and it makes the stream-close assertion below deterministic.
    """
    async def _run():
        provider = _provider([
            _text_chunk('one'),
            _text_chunk('two'),
            _text_chunk('three'),
        ])
        stream = provider.stream_completion(messages=MESSAGES)
        seen = []
        async for event in stream:
            seen.append(event)
            break
        await stream.aclose()
        return provider, seen

    provider, seen = asyncio.run(_run())

    assert seen == [TextDelta(text='one')]
    assert not any(isinstance(event, Interrupted) for event in seen)
    fake_stream = provider._async_client.chat.completions.streams[0]
    assert fake_stream.closed is True, 'the SDK stream must still be closed on cancellation'


# ---------------------------------------------------------------------------
# 8. Retry of a timeout that arrives before the first token
# ---------------------------------------------------------------------------
#
# The backoff lives in the provider, not in the SDK: ``_ensure_async_client()``
# pins the SDK at ``max_retries=0`` so a single attempt can never be retried
# twice. These tests neutralise the waits (base and cap to 0) so nothing
# actually sleeps.

def _no_backoff(monkeypatch):
    monkeypatch.setattr('llm_providers.openai_compatible._RETRY_BACKOFF_BASE_SECONDS', 0)
    monkeypatch.setattr('llm_providers.openai_compatible._RETRY_BACKOFF_MAX_SECONDS', 0)


def _timeout():
    """``APITimeoutError`` exactly as the SDK raises it mid-iteration."""
    return APITimeoutError(request=SimpleNamespace())


def test_timeout_before_first_token_is_retried(monkeypatch):
    _no_backoff(monkeypatch)
    provider = _provider(attempts=[
        [_timeout()],
        [_text_chunk('recovered', finish_reason='stop')],
    ])

    events = _drain(provider)  # must not raise

    assert _kinds(events) == ['TextDelta', 'Done']
    assert events[0] == TextDelta(text='recovered')
    assert not any(isinstance(event, Interrupted) for event in events)

    completions = provider._async_client.chat.completions
    assert len(completions.calls) == 2, 'a pre-first-token timeout must re-issue create()'
    # The dead stream is closed before the retry and is never reused.
    assert completions.streams[0].closed is True
    assert completions.streams[1] is not completions.streams[0]


def test_timeout_after_first_token_is_not_retried(monkeypatch):
    _no_backoff(monkeypatch)
    provider = _provider(attempts=[
        [_text_chunk('one'), _timeout()],
    ])

    events = _drain(provider)

    # The operator already saw 'one': exactly one error, no duplicated text.
    assert _kinds(events) == ['TextDelta', 'Interrupted']
    assert events[0] == TextDelta(text='one')
    assert events[1].reason == 'provider_error: APITimeoutError'
    assert len(provider._async_client.chat.completions.calls) == 1


def test_retry_resets_pending_tool_call_accumulator(monkeypatch):
    """A retry must not splice fragments from two different streams.

    Attempt 1 leaves an orphaned argument fragment in the per-attempt
    ``pending`` accumulator without emitting anything (the function name has
    not arrived, so no ``ToolCallStart`` can be built yet), then times out.
    Attempt 2 replays the same call in full. Without ``pending.clear()`` on
    retry the completed call would carry
    ``'{"file_path": "/tm{"file_path": "/tmp/x.txt"}'`` -- unparseable, i.e.
    silently corrupted arguments.
    """
    _no_backoff(monkeypatch)
    provider = _provider(attempts=[
        [
            _tool_chunk(index=0, call_id='call_1', arguments='{"file_path": "/tm'),
            _timeout(),
        ],
        [
            _tool_chunk(index=0, call_id='call_1', name='ReadFile', arguments='{"file_path": "/tmp/x.txt"}'),
            _empty_chunk(finish_reason='tool_calls'),
        ],
    ])

    events = _drain(provider, tools=TOOLS)

    start = _only(events, ToolCallStart)
    assert (start.id, start.name) == ('call_1', 'ReadFile')

    complete = _only(events, ToolCallComplete)
    assert complete.tool_call.arguments == {'file_path': '/tmp/x.txt'}
    assert complete.tool_call.raw_arguments == '{"file_path": "/tmp/x.txt"}'
    assert complete.parse_error is None
    assert complete.tool_call.parse_error is None
    assert events[-1] == Done()
    assert len(provider._async_client.chat.completions.calls) == 2


def test_max_retries_zero_disables_retry(monkeypatch):
    _no_backoff(monkeypatch)
    provider = _provider(attempts=[[_timeout()]], max_retries=0)

    events = _drain(provider)

    assert _kinds(events) == ['Interrupted']
    assert events[0].reason == 'provider_error: APITimeoutError'
    assert len(provider._async_client.chat.completions.calls) == 1, 'max_retries=0 means no retry'


def test_retry_budget_exhausted_yields_single_interrupted(monkeypatch):
    """Every attempt times out: retries stop at the budget, one error escapes.

    ``items`` (not ``attempts``) is replayed by every ``create()`` call, so
    attempt N+1 fails exactly like attempt N and the loop can only end through
    the budget/max_attempts guard -- never through a successful attempt.
    """
    _no_backoff(monkeypatch)
    provider = _provider(items=[_timeout()])

    events = _drain(provider)  # must not raise

    completions = provider._async_client.chat.completions
    assert len(completions.calls) == 4, 'default max_retries=3 means 1 initial + 3 retries'

    # Exactly one terminal error -- retries must not multiply the failure.
    assert _kinds(events) == ['Interrupted']
    assert events[0].reason == 'provider_error: APITimeoutError'
    # Nothing was ever streamed, so no text and no clean completion.
    assert not any(isinstance(event, TextDelta) for event in events)
    assert not any(isinstance(event, Done) for event in events)


def test_async_client_constructed_with_zero_sdk_retries(monkeypatch):
    """The SDK's own retry must be 0 -- otherwise one attempt is retried twice."""
    captured = {}

    class _RecordingAsyncClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.chat = SimpleNamespace(
                completions=_FakeCompletions([_text_chunk('hi', finish_reason='stop')])
            )

    monkeypatch.setattr('llm_providers.openai_compatible.AsyncOpenAI', _RecordingAsyncClient)

    provider = OpenAICompatibleProvider(
        ProviderConfig(api_key='sk-test', model=MODEL, timeout=5, max_retries=5)
    )
    # No pre-seeded client: force ``_ensure_async_client()`` to construct one.
    provider._async_client = None

    _drain(provider)

    assert captured['max_retries'] == 0, 'the provider owns streaming retries; the SDK must not add its own'
    assert captured['timeout'] == 5

