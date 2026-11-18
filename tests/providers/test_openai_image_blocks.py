"""Tests for the OpenAI-compatible image path.

Three things are checked here, all without touching the network:

* ``build_wire_messages`` turns a canonical message that references a pasted
  image by id into an OpenAI ``content`` block list carrying a base64 data URL,
  and substitutes placeholder text when the bytes are gone;
* an image the target model is not known to accept is *withheld* -- replaced by
  ``IMAGE_UNSUPPORTED_PLACEHOLDER`` -- instead of the conversation being
  refused.  Refusing it bricked the session: the image stays in the persisted
  transcript and the wire messages are rebuilt on every turn, so the refusal
  outlived the paste that caused it;
* a withheld image is not consumed, so switching the same session to a
  vision-capable model still sends it.
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
from agent.core.image_store import (
    IMAGE_UNAVAILABLE_PLACEHOLDER,
    IMAGE_UNSUPPORTED_PLACEHOLDER,
)
from llm_providers import openai_compatible
from llm_providers.base import ProviderConfig, model_supports_images
from llm_providers.openai_compatible import OpenAICompatibleProvider, build_wire_messages

_PNG_BYTES = b'\x89PNG\r\n\x1a\nfake-body'


@pytest.fixture(autouse=True)
def _clean_store():
    # The "warn once per image" memories are module state: without clearing them
    # the warn-once assertions would pass or fail depending on test order.
    image_store.reset()
    openai_compatible._warned_missing_images.clear()
    openai_compatible._warned_unsupported_images.clear()
    yield
    image_store.reset()
    openai_compatible._warned_missing_images.clear()
    openai_compatible._warned_unsupported_images.clear()


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


def test_a_text_only_model_gets_a_placeholder_instead_of_a_refusal(image_id):
    """The regression that bricked live sessions.

    The raise was not a per-turn failure: the image lives on in the persisted
    transcript, so every later turn rebuilt the same request and raised again,
    and the session could never be used for anything else.
    """
    wire = build_wire_messages(
        [{'role': 'user', 'content': 'look', 'images': [image_id]}], model='deepseek-v4-flash'
    )

    assert wire == [
        {
            'role': 'user',
            'content': [
                {'type': 'text', 'text': 'look'},
                {'type': 'text', 'text': IMAGE_UNSUPPORTED_PLACEHOLDER},
            ],
        }
    ]


def test_a_withheld_image_leaves_no_image_block_for_the_model_to_reject(image_id):
    wire = build_wire_messages(
        [{'role': 'user', 'content': 'look', 'images': [image_id]}], model='deepseek-v4-flash'
    )

    assert 'images' not in wire[0]
    assert [block['type'] for block in wire[0]['content']] == ['text', 'text']


def test_an_image_only_turn_still_has_content_to_send(image_id):
    # An empty content list is rejected by the API, so the placeholder has to
    # stand on its own when there is no prompt to carry it.
    wire = build_wire_messages(
        [{'role': 'user', 'content': '', 'images': [image_id]}], model='deepseek-v4-flash'
    )

    assert wire[0]['content'] == [{'type': 'text', 'text': IMAGE_UNSUPPORTED_PLACEHOLDER}]


def test_every_image_is_represented_by_its_own_placeholder():
    first = image_store.put(b'first', 'image/png')
    second = image_store.put(b'second', 'image/png')

    wire = build_wire_messages(
        [{'role': 'user', 'content': 'compare', 'images': [first, second]}],
        model='deepseek-v4-flash',
    )

    assert wire[0]['content'] == [
        {'type': 'text', 'text': 'compare'},
        {'type': 'text', 'text': IMAGE_UNSUPPORTED_PLACEHOLDER},
        {'type': 'text', 'text': IMAGE_UNSUPPORTED_PLACEHOLDER},
    ]


def test_withholding_an_image_does_not_consume_it(image_id):
    """The bytes survive, so a later vision-capable turn can still send them."""
    build_wire_messages(
        [{'role': 'user', 'content': 'look', 'images': [image_id]}], model='deepseek-v4-flash'
    )

    assert image_store.get(image_id) == (_PNG_BYTES, 'image/png')

    wire = build_wire_messages(
        [{'role': 'user', 'content': 'look', 'images': [image_id]}], model='gpt-4o'
    )

    assert [block['type'] for block in wire[0]['content']] == ['text', 'image_url']


def test_the_callers_transcript_is_not_mutated_when_the_image_is_withheld(image_id):
    messages = [{'role': 'user', 'content': 'look', 'images': [image_id]}]

    build_wire_messages(messages, model='deepseek-v4-flash')

    assert messages == [{'role': 'user', 'content': 'look', 'images': [image_id]}]


def test_the_degradation_is_logged_once_per_image_not_once_per_turn(image_id, monkeypatch):
    warnings = []

    def _record(level, category, message):
        if level == 'WARNING':
            warnings.append(message)

    monkeypatch.setattr(openai_compatible, 'log', _record)
    messages = [{'role': 'user', 'content': 'look', 'images': [image_id]}]

    for _ in range(3):
        build_wire_messages(messages, model='deepseek-v4-flash')

    assert len(warnings) == 1
    assert image_id in warnings[0]
    assert 'deepseek-v4-flash' in warnings[0]


def test_the_gate_is_only_applied_when_images_are_present():
    wire = build_wire_messages([{'role': 'user', 'content': 'hello'}], model='deepseek-chat')

    assert wire == [{'role': 'user', 'content': 'hello'}]


def test_an_explicit_flag_overrides_the_model_name(image_id):
    # Opting a locally named vision model in ...
    opted_in = build_wire_messages(
        [{'role': 'user', 'content': 'look', 'images': [image_id]}],
        model='house-model-v3',
        supports_images=True,
    )
    assert [block['type'] for block in opted_in[0]['content']] == ['text', 'image_url']

    # ... and a known-vision name out: withheld, but the turn still happens.
    opted_out = build_wire_messages(
        [{'role': 'user', 'content': 'look', 'images': [image_id]}],
        model='gpt-4o',
        supports_images=False,
    )
    assert [block['type'] for block in opted_out[0]['content']] == ['text', 'text']


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


def test_a_transcript_carrying_an_unsendable_image_does_not_block_later_turns(
    provider, image_id, monkeypatch
):
    """The outage, reproduced and then survived.

    The paste stays in the transcript for good and the provider rebuilds the
    whole transcript on every turn, so the point is not that the first turn
    survives -- it is that the *third* one still reaches the SDK.
    """
    captured = {}

    def _fake_create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='I cannot see images.', tool_calls=None),
                    finish_reason='stop',
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

    monkeypatch.setattr(provider.client.chat.completions, 'create', _fake_create)
    provider.config = ProviderConfig(api_key='test-key', model='deepseek-v4-flash')

    transcript = [
        {'role': 'user', 'content': 'what is this?', 'images': [image_id]},
        {'role': 'assistant', 'content': 'I cannot see images.'},
        {'role': 'user', 'content': 'then just say hello'},
    ]
    response = provider.chat_completion(transcript)

    assert response.content == 'I cannot see images.'
    sent = captured['messages']
    assert len(sent) == len(transcript)
    assert all('images' not in message for message in sent)
    sent_text = str(sent)
    assert IMAGE_UNSUPPORTED_PLACEHOLDER in sent_text
    assert 'image_url' not in sent_text
    assert 'then just say hello' in sent_text


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
