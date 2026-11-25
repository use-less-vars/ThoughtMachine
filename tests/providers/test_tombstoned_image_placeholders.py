"""What the adapters say about an image the save-time retention pass released.

``session/image_retention.py`` releases the ids of the oldest pasted images from
the durable transcript and marks the message with ``images_tombstoned``.  The
durable ``content`` is left byte-identical -- it is what the user typed, and a
pruned transcript must not put words in their mouth -- so the in-band statement
that the screenshot can no longer be seen is *derived at render time*.

Both adapters must derive the same sentence, exactly once, and neither may
invent a placeholder for a message that carries no marker:

* a tombstoned message reads as ``IMAGE_UNAVAILABLE_PLACEHOLDER`` whatever the
  target model is (the bytes are gone for every model -- telling a text-only
  model ``IMAGE_UNSUPPORTED_PLACEHOLDER`` would be a lie about a remedy that
  does not exist);
* a message without the marker is passed through untouched;
* a message carrying both live ids and the marker keeps its per-image
  placeholders and gains nothing extra (the marker could only be stale there,
  and one message must never say two different things about one screenshot).

No network: the SDK clients are constructed but never called.
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pytest

from agent.core import image_store
from agent.core.image_store import (
    IMAGE_UNAVAILABLE_PLACEHOLDER,
    IMAGE_UNSUPPORTED_PLACEHOLDER,
)
from llm_providers import anthropic_provider, openai_compatible
from llm_providers.anthropic_provider import AnthropicProvider
from llm_providers.base import ProviderConfig
from llm_providers.openai_compatible import build_wire_messages

_PNG_BYTES = b'\x89PNG\r\n\x1a\nfake-body'


@pytest.fixture(autouse=True)
def _clean_store():
    # The "warn once per image" memories are module state: without clearing them
    # the placeholder assertions would depend on test order.
    image_store.reset()
    openai_compatible._warned_missing_images.clear()
    openai_compatible._warned_unsupported_images.clear()
    anthropic_provider._warned_unsupported_images.clear()
    yield
    image_store.reset()
    openai_compatible._warned_missing_images.clear()
    openai_compatible._warned_unsupported_images.clear()
    anthropic_provider._warned_unsupported_images.clear()


@pytest.fixture
def provider():
    """A provider whose SDK client is never called (client construction is offline)."""
    return AnthropicProvider(
        ProviderConfig(api_key='test-key', model='claude-3-5-sonnet')
    )


@pytest.fixture
def image_id():
    return image_store.put(_PNG_BYTES, 'image/png')


def _openai_text(wire_message):
    """The text an OpenAI wire message would show, without its image blocks."""
    return '\n\n'.join(
        part['text'] for part in wire_message['content'] if part['type'] == 'text'
    )


# ---------------------------------------------------------------------------
# The two adapters agree
# ---------------------------------------------------------------------------


def test_both_adapters_say_the_same_thing_for_a_tombstoned_message(provider):
    transcript = [
        {
            'role': 'user',
            'content': 'why does this fail?',
            'images': [],
            'images_tombstoned': 1,
        }
    ]

    wire = build_wire_messages(transcript, model='gpt-4o')
    openai_text = _openai_text(wire[0])
    anthropic_text = provider._degrade_image_content(transcript, 'claude-3-5-sonnet')[0][
        'content'
    ]

    assert openai_text == anthropic_text
    assert openai_text.count(IMAGE_UNAVAILABLE_PLACEHOLDER) == 1
    assert anthropic_text.count(IMAGE_UNAVAILABLE_PLACEHOLDER) == 1
    assert 'why does this fail?' in openai_text
    assert 'why does this fail?' in anthropic_text


def test_a_tombstoned_message_does_not_become_unsupported_on_a_text_only_model():
    """The bytes are gone, so no model can be sent them: the remedy is not a model switch."""
    transcript = [
        {
            'role': 'user',
            'content': 'what is this?',
            'images': [],
            'images_tombstoned': 1,
        }
    ]

    wire = build_wire_messages(transcript, model='deepseek-v4-pro')
    text = _openai_text(wire[0])

    assert text.count(IMAGE_UNAVAILABLE_PLACEHOLDER) == 1
    assert IMAGE_UNSUPPORTED_PLACEHOLDER not in text
    assert 'image_url' not in str(wire)


# ---------------------------------------------------------------------------
# Nothing else moves
# ---------------------------------------------------------------------------


def test_a_message_without_the_marker_is_passed_through_untouched():
    transcript = [
        {'role': 'system', 'content': 'be brief'},
        {'role': 'user', 'content': 'hello'},
        {'role': 'assistant', 'content': 'ok'},
    ]

    wire = build_wire_messages(transcript, model='gpt-4o')

    assert wire == [dict(message) for message in transcript]
    assert transcript == [
        {'role': 'system', 'content': 'be brief'},
        {'role': 'user', 'content': 'hello'},
        {'role': 'assistant', 'content': 'ok'},
    ]
    assert IMAGE_UNAVAILABLE_PLACEHOLDER not in str(wire)
    assert IMAGE_UNSUPPORTED_PLACEHOLDER not in str(wire)


def test_a_message_with_live_images_and_the_marker_gains_nothing_extra(
    provider, image_id
):
    """One message must not say two different things about one screenshot."""
    transcript = [
        {
            'role': 'user',
            'content': 'look',
            'images': [image_id],
            'images_tombstoned': 1,
        }
    ]

    wire = build_wire_messages(transcript, model='gpt-4o')
    blocks = wire[0]['content']

    assert [block['type'] for block in blocks] == ['text', 'image_url']
    assert IMAGE_UNAVAILABLE_PLACEHOLDER not in str(wire)
    assert 'images_tombstoned' not in str(wire)

    degraded = provider._degrade_image_content(transcript, 'claude-3-5-sonnet')

    assert degraded[0]['content'] == f'look\n\n{IMAGE_UNSUPPORTED_PLACEHOLDER}'
    assert IMAGE_UNAVAILABLE_PLACEHOLDER not in degraded[0]['content']
    assert 'images_tombstoned' not in str(degraded)
