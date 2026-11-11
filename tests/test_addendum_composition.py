"""Tests for composing the session addendum into the LLM system prompt.

Composition point: ``LLMClient.ensure_system_prompt`` in
``agent/core/llm_client.py`` - the single place where the system prompt is
resolved and inserted at position 0 of the conversation. Every provider
adapter is handed that same conversation, so the addendum is rendered there,
once, in stored order.

The slice-1 session model is deliberately NOT imported: a session object
carrying only the contract shape (plus one plain dict) is stubbed here, so this
file stays independent of the model implementation.

Contract shape used throughout::

    addendum = {
        "entries": [{"id": str, "ts": str, "source": "operator"|"mailbox",
                     "text": str, "pinned": bool}],
        "budget_chars": int,
        "max_entries": int,
    }
"""

from types import SimpleNamespace

import pytest

from agent.core.llm_client import ADDENDUM_HEADING, LLMClient
from llm_providers.base import ProviderConfig
from llm_providers.exceptions import ProviderError
from llm_providers.factory import ProviderFactory


BASE_PROMPT = "You are ThoughtMachine.\nBe precise; never invent file contents."

TEXT_ONE = "Prefer terse answers while the migration is running."
TEXT_TWO = "The repository is frozen until Friday; do not open pull requests."


# ---------------------------------------------------------------------------
# Session / addendum stubs (contract shape only, no session model import)
# ---------------------------------------------------------------------------

class _StubSession:
    """Session exposing the addendum as an attribute (object shape)."""

    def __init__(self, addendum=None):
        self.session_id = 'stub-session'
        self.addendum = addendum


class _LegacySession:
    """A session that has never heard of the addendum field."""

    def __init__(self):
        self.session_id = 'legacy-session'


def _entry(entry_id, text, ts='2025-01-01T00:00:00Z', source='operator', pinned=False):
    return {'id': entry_id, 'ts': ts, 'source': source, 'text': text, 'pinned': pinned}


def _addendum(entries, budget_chars=4000, max_entries=50):
    return {'entries': list(entries), 'budget_chars': budget_chars, 'max_entries': max_entries}


TWO_ENTRIES = [
    _entry('e-001', TEXT_ONE, ts='2025-03-02T09:00:00Z', source='operator', pinned=False),
    # Deliberately EARLIER ts than the first entry and pinned=True: stored order
    # must survive (no sorting by ts, no pinned-first reordering).
    _entry('e-002', TEXT_TWO, ts='2025-01-05T17:30:00Z', source='mailbox', pinned=True),
]


# ---------------------------------------------------------------------------
# LLMClient construction with a stub provider factory (no network, no SDK call)
# ---------------------------------------------------------------------------

class _RecordingProvider:
    """Provider stub: records the messages it is asked to complete."""

    def __init__(self):
        self.calls = []

    def chat_completion(self, messages=None, tools=None, **kwargs):
        self.calls.append(list(messages or []))
        return SimpleNamespace(content='ok', reasoning=None, tool_calls=None, usage={}, model='stub')


def _make_config(system_prompt=BASE_PROMPT):
    return SimpleNamespace(
        provider_type='openai_compatible',
        api_key='test-key',
        base_url=None,
        model='test-model',
        temperature=0.2,
        provider_config={},
        system_prompt=system_prompt,
    )


def _make_client(monkeypatch, session):
    provider = _RecordingProvider()
    monkeypatch.setattr(
        ProviderFactory, 'create_provider', staticmethod(lambda **kwargs: provider)
    )
    return LLMClient(_make_config(), session=session), provider


def _compose(monkeypatch, session):
    """Run the real composition point for *session*; return the conversation."""
    client, _provider = _make_client(monkeypatch, session)
    conversation = [{'role': 'user', 'content': 'hello'}]
    return client.ensure_system_prompt(conversation)


def _system_text(conversation):
    system_messages = [m for m in conversation if m.get('role') == 'system']
    assert len(system_messages) == 1, 'exactly one system message expected'
    return system_messages[0]['content']


# ---------------------------------------------------------------------------
# (a)/(c) Absent addendum => composed prompt byte-identical to the baseline
# ---------------------------------------------------------------------------

def _baseline(monkeypatch):
    return _compose(monkeypatch, _LegacySession())


def test_legacy_session_without_addendum_attribute_is_byte_identical(monkeypatch):
    """(c) A session that predates the field: no exception, identical output."""
    conversation = _baseline(monkeypatch)
    assert conversation == [
        {'role': 'system', 'content': BASE_PROMPT},
        {'role': 'user', 'content': 'hello'},
    ]
    assert _system_text(conversation) == BASE_PROMPT


def test_addendum_attribute_none_is_byte_identical(monkeypatch):
    conversation = _compose(monkeypatch, _StubSession(addendum=None))
    assert conversation == _baseline(monkeypatch)


def test_zero_entry_addendum_is_byte_identical(monkeypatch):
    conversation = _compose(monkeypatch, _StubSession(_addendum([])))
    assert conversation == _baseline(monkeypatch)
    assert ADDENDUM_HEADING not in _system_text(conversation)


def test_missing_session_is_byte_identical(monkeypatch):
    conversation = _compose(monkeypatch, None)
    assert conversation == _baseline(monkeypatch)


def test_dict_session_without_addendum_key_is_byte_identical(monkeypatch):
    conversation = _compose(monkeypatch, {'session_id': 'dict-session'})
    assert conversation == _baseline(monkeypatch)


# ---------------------------------------------------------------------------
# (b) Entries render once, in stored order, text only
# ---------------------------------------------------------------------------

def test_two_entries_render_both_texts_in_stored_order(monkeypatch):
    conversation = _compose(monkeypatch, _StubSession(_addendum(TWO_ENTRIES)))
    system_text = _system_text(conversation)

    assert TEXT_ONE in system_text
    assert TEXT_TWO in system_text
    assert system_text.index(TEXT_ONE) < system_text.index(TEXT_TWO)
    # Placement: the addendum is appended after the prompt, never replacing it.
    assert system_text.startswith(BASE_PROMPT)


def test_dict_session_renders_addendum(monkeypatch):
    """The dict shape of the session field is handled too."""
    conversation = _compose(monkeypatch, {'session_id': 'dict-session',
                                          'addendum': _addendum(TWO_ENTRIES)})
    system_text = _system_text(conversation)
    assert TEXT_ONE in system_text
    assert TEXT_TWO in system_text
    assert system_text.index(TEXT_ONE) < system_text.index(TEXT_TWO)


def test_entry_metadata_is_never_rendered(monkeypatch):
    """id / ts / source / pinned stay internal; only entry text reaches the model."""
    conversation = _compose(monkeypatch, _StubSession(_addendum(TWO_ENTRIES)))
    system_text = _system_text(conversation)

    for leaked in ('e-001', 'e-002', '2025-03-02T09:00:00Z', '2025-01-05T17:30:00Z',
                   'operator', 'mailbox', 'pinned', 'entries', 'budget_chars',
                   'max_entries', 'addendum'):
        assert leaked not in system_text


def test_addendum_renders_exactly_once_across_repeated_turns(monkeypatch):
    """ensure_system_prompt rebuilds the prompt each turn: no accumulation."""
    client, _provider = _make_client(monkeypatch, _StubSession(_addendum(TWO_ENTRIES)))
    conversation = [{'role': 'user', 'content': 'hello'}]
    first = client.ensure_system_prompt(conversation)
    second = client.ensure_system_prompt(conversation)

    assert first == second
    assert _system_text(second).count(TEXT_ONE) == 1
    assert _system_text(second).count(TEXT_TWO) == 1
    assert _system_text(second).count(ADDENDUM_HEADING) == 1


def test_composition_reaches_the_provider_boundary(monkeypatch):
    """The composed conversation is what the client hands to the provider."""
    client, provider = _make_client(monkeypatch, _StubSession(_addendum(TWO_ENTRIES)))
    conversation = client.ensure_system_prompt([{'role': 'user', 'content': 'hello'}])
    client.chat_completion(messages=conversation)

    assert provider.calls, 'provider was never called'
    sent_system = _system_text(provider.calls[-1])
    assert TEXT_ONE in sent_system and TEXT_TWO in sent_system


# ---------------------------------------------------------------------------
# (d) Per-adapter assertions: both adapters on this path see the addendum
# ---------------------------------------------------------------------------

def test_openai_compatible_adapter_forwards_addendum_in_system_message(monkeypatch):
    """openai_compatible keeps the system message in ``messages``."""
    from llm_providers.openai_compatible import OpenAICompatibleProvider

    conversation = _compose(monkeypatch, _StubSession(_addendum(TWO_ENTRIES)))

    provider = OpenAICompatibleProvider(ProviderConfig(api_key='test-key', model='test-model'))
    sent = {}

    def fake_create(**kwargs):
        sent.update(kwargs)
        return SimpleNamespace(choices=[], usage={}, model='test-model', id='resp-1')

    provider.client = SimpleNamespace(
        base_url='http://stub.invalid/v1',
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)),
    )
    monkeypatch.setattr(
        provider, 'parse_response', lambda raw, start: SimpleNamespace(
            content='', reasoning=None, tool_calls=None, usage={}, model='test-model')
    )
    try:
        provider.chat_completion(messages=conversation)
    except (ProviderError, AttributeError):
        # Post-response logging is not what this test is about; the request
        # payload has already been captured by then.
        pass

    assert sent, 'the adapter never built a request payload'
    system_messages = [m for m in sent['messages'] if m.get('role') == 'system']
    assert len(system_messages) == 1
    system_text = system_messages[0]['content']
    assert TEXT_ONE in system_text and TEXT_TWO in system_text
    assert system_text.index(TEXT_ONE) < system_text.index(TEXT_TWO)


def test_anthropic_adapter_puts_addendum_in_system_param(monkeypatch):
    """anthropic lifts the system message into the top-level ``system`` argument."""
    from llm_providers.anthropic_provider import AnthropicProvider

    conversation = _compose(monkeypatch, _StubSession(_addendum(TWO_ENTRIES)))

    provider = AnthropicProvider(
        ProviderConfig(api_key='test-key', model='claude-3-haiku-20240307')
    )
    system_text, anthropic_messages = provider._serialize_messages(conversation)

    assert system_text is not None, 'anthropic adapter dropped the system prompt'
    assert TEXT_ONE in system_text and TEXT_TWO in system_text
    assert system_text.index(TEXT_ONE) < system_text.index(TEXT_TWO)
    # No system turn survives in the messages array, so the addendum cannot
    # leak into a user/assistant turn.
    assert all(m.get('role') != 'system' for m in anthropic_messages)
    for message in anthropic_messages:
        assert TEXT_ONE not in str(message.get('content', ''))
        assert TEXT_TWO not in str(message.get('content', ''))


# ---------------------------------------------------------------------------
# (e) A worker's spawn brief survives the strip, and stays put for 3 cycles
#
# The brief is injected by tools/workspace/worker_thread.py as a SECOND
# role='system' message: f"Initial context: {json.dumps(initial_context)}".
# Before this was pinned, ensure_system_prompt deleted it like any stale system
# prompt, so a worker never saw its own instructions on its first turn.
# ---------------------------------------------------------------------------

BRIEF = 'Initial context: {"query": "q", "briefing": "SENTINEL_BRIEF_ALPHA"}'
BRIEF_SENTINEL = 'SENTINEL_BRIEF_ALPHA'
SUMMARY_PREFIX = 'Summary of previous conversation:'
STALE = 'stale system prompt'


def _worker_session():
    """The real worker session: it never defines an addendum attribute."""
    from agent.core.worker_context import WorkerContext

    return WorkerContext(
        session_id='stub-session',
        workspace_id='stub-workspace',
        worker_name='stub-worker',
        user_history=[],
    )


def _system_messages(conversation):
    return [m for m in conversation if m.get('role') == 'system']


def _summarisation_cycle(client, conversation, round_index):
    """One SummarizeTool compaction, then the real composition point.

    Mirrors Agent._apply_summary_pruning: the turns that were summarised are
    replaced by a single role='system' message starting with the summary
    prefix. The config prompt and the brief are carried across untouched, so
    only the strip inside ensure_system_prompt can lose them.
    """
    config_prompt = dict(conversation[0])
    brief_messages = [m for m in conversation if str(m.get('content', '')).startswith('Initial context:')]

    conversation[:] = (
        [config_prompt]
        + brief_messages
        + [{'role': 'system', 'content': STALE}]
        + [{'role': 'system', 'content': f'{SUMMARY_PREFIX} round {round_index}: earlier turns compressed.'}]
        + [{'role': 'user', 'content': f'next step {round_index}'}]
        + [{'role': 'assistant', 'content': f'done {round_index}'}]
    )
    return client.ensure_system_prompt(conversation)


def test_worker_brief_survives_three_summarisation_cycles(monkeypatch):
    """The brief is still in the conversation after three summaries in a row."""
    client, _provider = _make_client(monkeypatch, _worker_session())

    conversation = [
        {'role': 'system', 'content': BASE_PROMPT},
        {'role': 'system', 'content': BRIEF},
        {'role': 'user', 'content': 'turn 0'},
        {'role': 'assistant', 'content': 'ok 0'},
    ]

    for round_index in (1, 2, 3):
        conversation = _summarisation_cycle(client, conversation, round_index)

        system_messages = _system_messages(conversation)
        brief_messages = [m for m in system_messages if BRIEF_SENTINEL in m['content']]
        summary_messages = [m for m in system_messages if m['content'].startswith(SUMMARY_PREFIX)]

        assert len(brief_messages) == 1, f'round {round_index}: brief lost or duplicated'
        assert len(summary_messages) == 1, f'round {round_index}: summary lost or duplicated'

        assert conversation[0].get('role') == 'system', f'round {round_index}: no system prompt first'
        assert conversation[0]['content'] == BASE_PROMPT, f'round {round_index}: config prompt not first'
        assert BRIEF_SENTINEL not in conversation[0]['content'], f'round {round_index}: brief leaked into prompt'

        # The brief keeps its own place in history: pinned, not merged.
        assert conversation[1]['content'] == BRIEF, f'round {round_index}: brief moved out of position 1'

        # Negative control: an unrelated system message is still stripped.
        assert not [m for m in conversation if m.get('content') == STALE], f'round {round_index}: stale kept'


def test_strip_preserves_brief_and_summary_but_drops_other_system_messages(monkeypatch):
    """The survivor rule itself: brief and summary in, every other system out."""
    client, _provider = _make_client(monkeypatch, _worker_session())

    conversation = [
        {'role': 'system', 'content': STALE},
        {'role': 'system', 'content': BASE_PROMPT},
        {'role': 'system', 'content': BRIEF},
        {'role': 'system', 'content': f'{SUMMARY_PREFIX} earlier turns.'},
        {'role': 'user', 'content': 'hello'},
    ]

    out = client.ensure_system_prompt(conversation)

    assert [m['role'] for m in out] == ['system', 'system', 'system', 'user']
    assert [m['content'] for m in out] == [
        BASE_PROMPT,
        BRIEF,
        f'{SUMMARY_PREFIX} earlier turns.',
        'hello',
    ]
    assert STALE not in [m['content'] for m in out]

