"""Provider-level round-trip test for Anthropic structured tool blocks.

Two consecutive turns through ``AnthropicProvider.chat_completion`` against a
fake Anthropic SDK client -- the same ``provider.client = FakeClient()`` seam
used by ``tests/test_llm_timeout_config.py``, so no network and no real API key:

  turn 1: assistant answers with text blocks + a ``tool_use`` block
          (``stop_reason='tool_use'``); the harness feeds the tool result back
  turn 2: assistant answers in plain text.

Turn 1 pins the *inbound* contract: ``parse_response`` must yield ``tool_calls``
entries whose ``arguments`` is a JSON **string** (OpenAI-compatible shape).

Turn 2 pins the *outbound* contract: the replayed history must carry an
assistant turn with a structured ``tool_use`` block and a **user** turn holding
the matching ``tool_result`` block.  The Messages API has no ``role='tool'``
turn and rejects a stringified ``tool_calls`` blob, so the flattened form the
provider used to send is rejected by the real API.
"""

import json
from types import SimpleNamespace

import pytest

from llm_providers.base import ProviderConfig

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

TOOL_USE_ID = 'toolu_01'


# ---------------------------------------------------------------------------
# Fake Anthropic SDK surface
# ---------------------------------------------------------------------------

def _text(text):
    return SimpleNamespace(type='text', text=text)


def _tool_use(block_id, name, tool_input):
    return SimpleNamespace(type='tool_use', id=block_id, name=name, input=tool_input)


def _response(blocks, stop_reason):
    return SimpleNamespace(
        content=blocks,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=11, output_tokens=7),
        model='claude-3-haiku-20240307',
    )


class _RecordingMessages:
    """``client.messages`` stand-in: records every ``create(**kwargs)`` call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses[len(self.calls) - 1]


class _FakeClient:
    def __init__(self, responses):
        self.messages = _RecordingMessages(responses)


def _provider(responses):
    anthropic = pytest.importorskip('anthropic')
    assert anthropic  # SDK is importable; the seam below fakes it.
    from llm_providers.anthropic_provider import AnthropicProvider

    provider = AnthropicProvider(
        ProviderConfig(api_key='test-key', model='claude-3-haiku-20240307', timeout=5)
    )
    provider.client = _FakeClient(responses)
    return provider


# ---------------------------------------------------------------------------
# The round-trip
# ---------------------------------------------------------------------------

def test_tool_use_roundtrip_emits_structured_blocks():
    provider = _provider([
        _response(
            [
                _text('Let me read '),
                _text('that file.'),
                _tool_use(TOOL_USE_ID, 'ReadFile', {'file_path': '/tmp/x.txt'}),
            ],
            stop_reason='tool_use',
        ),
        _response([_text('The file says: hello world')], stop_reason='end_turn'),
    ])

    history = [
        {'role': 'system', 'content': 'You are terse.'},
        {'role': 'user', 'content': 'What is in /tmp/x.txt?'},
    ]

    # ---- turn 1: inbound parse ------------------------------------------
    first = provider.chat_completion(messages=history, tools=TOOLS)

    assert first.content == 'Let me read that file.'  # both text blocks kept
    assert first.tool_calls is not None and len(first.tool_calls) == 1
    call = first.tool_calls[0]
    assert call['id'] == TOOL_USE_ID
    assert call['type'] == 'function'
    assert call['function']['name'] == 'ReadFile'
    # `arguments` must be a JSON *string*, not a dict (OpenAI-compatible shape).
    assert isinstance(call['function']['arguments'], str)
    assert call['function']['arguments'] == '{"file_path": "/tmp/x.txt"}'
    assert json.loads(call['function']['arguments']) == {'file_path': '/tmp/x.txt'}

    # ---- harness: replay the assistant turn + the tool result ------------
    history.append(
        {'role': 'assistant', 'content': first.content, 'tool_calls': first.tool_calls}
    )
    history.append({'role': 'tool', 'tool_call_id': call['id'], 'content': 'hello world'})

    # ---- turn 2: outbound serialisation ---------------------------------
    second = provider.chat_completion(messages=history, tools=TOOLS)
    assert second.content == 'The file says: hello world'
    assert not second.tool_calls

    payload = provider.client.messages.calls[1]
    assert payload['system'] == 'You are terse.'
    messages = payload['messages']

    # 'system' extracted, role='tool' folded into a *user* turn -> strict
    # user/assistant alternation with no two consecutive same-role turns.
    assert [m['role'] for m in messages] == ['user', 'assistant', 'user']
    assert messages[0] == {'role': 'user', 'content': 'What is in /tmp/x.txt?'}

    # assistant turn carries a real tool_use block (not a stringified blob)
    assistant_blocks = messages[1]['content']
    assert isinstance(assistant_blocks, list)
    assert [b['type'] for b in assistant_blocks] == ['text', 'tool_use']
    assert assistant_blocks[0] == {'type': 'text', 'text': 'Let me read that file.'}
    assert assistant_blocks[1] == {
        'type': 'tool_use',
        'id': TOOL_USE_ID,
        'name': 'ReadFile',
        'input': {'file_path': '/tmp/x.txt'},
    }

    # the tool result travels back as a tool_result block in a user turn
    tool_result_blocks = messages[2]['content']
    assert isinstance(tool_result_blocks, list)
    assert tool_result_blocks == [
        {'type': 'tool_result', 'tool_use_id': TOOL_USE_ID, 'content': 'hello world'}
    ]


def test_parallel_tool_results_merge_into_one_user_turn():
    """Two tool results in a row must share a single user turn.

    Anthropic rejects consecutive same-role turns, so parallel tool calls
    cannot become two separate ``role='user'`` messages.
    """
    provider = _provider([_response([_text('done')], stop_reason='end_turn')])

    history = [
        {'role': 'user', 'content': 'read both'},
        {
            'role': 'assistant',
            'content': None,
            'tool_calls': [
                {'id': 'toolu_a', 'type': 'function',
                 'function': {'name': 'ReadFile', 'arguments': '{"file_path": "/a"}'}},
                {'id': 'toolu_b', 'type': 'function',
                 'function': {'name': 'ReadFile', 'arguments': '{"file_path": "/b"}'}},
            ],
        },
        {'role': 'tool', 'tool_call_id': 'toolu_a', 'content': 'A'},
        {'role': 'tool', 'tool_call_id': 'toolu_b', 'content': 'B'},
    ]

    provider.chat_completion(messages=history, tools=TOOLS)

    messages = provider.client.messages.calls[0]['messages']
    assert [m['role'] for m in messages] == ['user', 'assistant', 'user']
    assert [b['type'] for b in messages[1]['content']] == ['tool_use', 'tool_use']
    assert [b['input'] for b in messages[1]['content']] == [
        {'file_path': '/a'},
        {'file_path': '/b'},
    ]
    assert [b['tool_use_id'] for b in messages[2]['content']] == ['toolu_a', 'toolu_b']
    assert [b['content'] for b in messages[2]['content']] == ['A', 'B']
