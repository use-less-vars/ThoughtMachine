"""Tests for the OpenAI-compatible image path.

Two things are checked here, both without touching the network:

* ``build_wire_messages`` turns a canonical message that references a pasted
  image by id into an OpenAI ``content`` block list carrying a base64 data URL,
  and substitutes placeholder text when the bytes are gone;
* a model that is not known to accept images refuses the conversation with
  ``UnsupportedContentError`` -- and that type reaches the caller instead of
  being flattened into a ``ProviderError``.
"""

import base64
import sys
from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pytest

from agent.core import image_store
from agent.core.image_store import IMAGE_UNAVAILABLE_PLACEHOLDER
from agent.core.ir import UnsupportedContentError
from llm_providers.base import ProviderConfig, model_supports_images
from llm_providers.openai_compatible import OpenAICompatibleProvider, build_wire_messages

_PNG_BYTES = b'\x89PNG\r\n\x1a\nfake-body'


@pytest.fixture(autouse=True)
def _clean_store():
    image_store.reset()
    yield
    image_store.reset()


@pytest.fixture
def image_id():
    return image_store.put(_PNG_BYTES, 'image/png')


@pytest.fixture
def provider():
    """A provider whose SDK client is never called (client construction is offline)."""
    return OpenAICompatibleProvider(
        ProviderConfig(api_key='test-key', model='gpt-4o', supports_images=True)
    )


# ---------------------------------------------------------------------------
# Wire format
# ---------------------------------------------------------------------------


def test_image_message_becomes_a_text_block_plus_an_image_block(image_id):
    messages = [{'role': 'user', 'content': 'why is this red?', 'images': [image_id]}]

    wire = build_wire_messages(messages, model='gpt-4o')

    assert wire == [
        {
            'role': 'user',
            'content': [
                {'type': 'text', 'text': 'why is this red?'},
                {
                    'type': 'image_url',
                    'image_url': {
                        'url': 'data:image/png;base64,'
                        + base64.b64encode(_PNG_BYTES).decode('ascii')
                    },
                },
            ],
        }
    ]


def test_the_images_key_never_reaches_the_wire(image_id):
    wire = build_wire_messages(
        [{'role': 'user', 'content': 'hi', 'images': [image_id]}], model='gpt-4o'
    )

    assert 'images' not in wire[0]


def test_an_empty_prompt_omits_the_text_block(image_id):
    wire = build_wire_messages(
        [{'role': 'user', 'content': '', 'images': [image_id]}], model='gpt-4o'
    )

    assert [block['type'] for block in wire[0]['content']] == ['image_url']


def test_several_images_produce_one_block_each():
    first = image_store.put(b'first', 'image/png')
    second = image_store.put(b'second', 'image/jpeg')

    wire = build_wire_messages(
        [{'role': 'user', 'content': 'compare', 'images': [first, second]}], model='gpt-4o'
    )

    blocks = wire[0]['content']
    assert [block['type'] for block in blocks] == ['text', 'image_url', 'image_url']
    assert blocks[1]['image_url']['url'].startswith('data:image/png;base64,')
    assert blocks[2]['image_url']['url'].startswith('data:image/jpeg;base64,')


def test_the_media_type_from_the_store_is_used():
    image_id = image_store.put(b'webp-body', 'image/webp')

    wire = build_wire_messages(
        [{'role': 'user', 'content': 'x', 'images': [image_id]}], model='gpt-4o'
    )

    assert wire[0]['content'][1]['image_url']['url'].startswith('data:image/webp;base64,')


def test_the_callers_transcript_is_not_mutated(image_id):
    messages = [
        {'role': 'system', 'content': 'be brief'},
        {'role': 'user', 'content': 'look', 'images': [image_id]},
    ]

    build_wire_messages(messages, model='gpt-4o')

    assert messages[1]['images'] == [image_id]
    assert messages[1]['content'] == 'look'


def test_image_free_conversations_pass_through_untouched():
    messages = [
        {'role': 'system', 'content': 'be brief'},
        {'role': 'user', 'content': 'hello'},
    ]

    wire = build_wire_messages(messages, model='deepseek-chat')

    assert wire == messages
    assert wire is not messages


def test_an_empty_images_list_is_dropped_without_rewriting_content():
    wire = build_wire_messages(
        [{'role': 'user', 'content': 'hello', 'images': []}], model='gpt-4o'
    )

    assert wire == [{'role': 'user', 'content': 'hello'}]


# ---------------------------------------------------------------------------
# Missing bytes: a placeholder, never a failed turn
# ---------------------------------------------------------------------------


def test_an_evicted_image_becomes_placeholder_text():
    wire = build_wire_messages(
        [{'role': 'user', 'content': 'what is this?', 'images': ['0000000000000000']}],
        model='gpt-4o',
    )

    assert wire[0]['content'] == [
        {'type': 'text', 'text': 'what is this?'},
        {'type': 'text', 'text': IMAGE_UNAVAILABLE_PLACEHOLDER},
    ]


def test_a_missing_image_without_a_prompt_still_sends_something():
    wire = build_wire_messages(
        [{'role': 'user', 'content': '', 'images': ['0000000000000000']}], model='gpt-4o'
    )

    assert wire[0]['content'] == [{'type': 'text', 'text': IMAGE_UNAVAILABLE_PLACEHOLDER}]


def test_a_partial_miss_keeps_the_images_that_survived(image_id):
    wire = build_wire_messages(
        [{'role': 'user', 'content': 'both?', 'images': [image_id, '0000000000000000']}],
        model='gpt-4o',
    )

    types = [block['type'] for block in wire[0]['content']]
    assert types == ['text', 'image_url', 'text']


# ---------------------------------------------------------------------------
# Capability gate
# ---------------------------------------------------------------------------


def test_a_text_only_model_refuses_the_conversation(image_id):
    with pytest.raises(UnsupportedContentError) as excinfo:
        build_wire_messages(
            [{'role': 'user', 'content': 'look', 'images': [image_id]}], model='deepseek-chat'
        )

    assert 'deepseek-chat' in str(excinfo.value)


def test_the_gate_is_only_applied_when_images_are_present():
    wire = build_wire_messages([{'role': 'user', 'content': 'hello'}], model='deepseek-chat')

    assert wire == [{'role': 'user', 'content': 'hello'}]


def test_an_explicit_flag_overrides_the_model_name(image_id):
    # Opting a locally named vision model in ...
    build_wire_messages(
        [{'role': 'user', 'content': 'look', 'images': [image_id]}],
        model='house-model-v3',
        supports_images=True,
    )
    # ... and a known-vision name out.
    with pytest.raises(UnsupportedContentError):
        build_wire_messages(
            [{'role': 'user', 'content': 'look', 'images': [image_id]}],
            model='gpt-4o',
            supports_images=False,
        )


def test_model_capability_hints():
    assert model_supports_images('gpt-4o') is True
    assert model_supports_images('claude-3-5-sonnet') is True
    assert model_supports_images('deepseek-chat') is False
    assert model_supports_images('') is False
    assert model_supports_images(None) is False
    assert model_supports_images('opaque-name', explicit=True) is True
    assert model_supports_images('gpt-4o', explicit=False) is False


# ---------------------------------------------------------------------------
# The provider itself
# ---------------------------------------------------------------------------


def test_provider_refusal_keeps_its_type(provider, image_id, monkeypatch):
    """The refusal must not be rewritten as ProviderError('Unexpected error: ...')."""
    def _never_called(*args, **kwargs):  # pragma: no cover - the refusal is earlier
        raise AssertionError('the SDK must not be called for an unsupported model')

    monkeypatch.setattr(provider.client.chat.completions, 'create', _never_called)
    provider.config = ProviderConfig(api_key='test-key', model='deepseek-chat')

    with pytest.raises(UnsupportedContentError):
        provider.chat_completion([{'role': 'user', 'content': 'look', 'images': [image_id]}])


def test_provider_counts_images_as_images(provider, image_id):
    provider._load_encoding()
    message = {'role': 'user', 'content': 'look at this', 'images': [image_id]}

    with_image = provider.count_tokens([message])
    without_image = provider.count_tokens([{'role': 'user', 'content': 'look at this'}])

    assert with_image - without_image == 1024


def test_provider_sends_image_blocks_to_the_sdk(provider, image_id, monkeypatch):
    """End-to-end through chat_completion: the SDK sees the data URL, not the id."""
    captured = {}

    def _fake_create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='looks like a PNG', tool_calls=None),
                    finish_reason='stop',
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

    monkeypatch.setattr(provider.client.chat.completions, 'create', _fake_create)

    provider.chat_completion([{'role': 'user', 'content': 'what is this?', 'images': [image_id]}])

    sent = captured['messages'][0]
    assert 'images' not in sent
    assert sent['content'][1]['type'] == 'image_url'
    assert sent['content'][1]['image_url']['url'].startswith('data:image/png;base64,')
