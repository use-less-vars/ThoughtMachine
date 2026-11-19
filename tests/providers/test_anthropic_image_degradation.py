"""Tests for the Anthropic adapter's handling of pasted images.

This adapter does not build ``image`` content blocks yet, so an image cannot be
sent.  Two easy answers are both wrong:

* dropping the id silently leaves the model answering a question about a
  screenshot it never saw;
* refusing the conversation (what this used to do) is worse than either,
  because the image stays in the persisted transcript and the adapter is handed
  it again on every turn -- one paste made the session permanently unusable.

So the adapter substitutes ``IMAGE_UNSUPPORTED_PLACEHOLDER`` where the
screenshot was, leaves the bytes in the store, and logs the fact once per image.
No network: the SDK is only ever a fake here.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pytest

from agent.core import image_store
from agent.core.image_store import IMAGE_UNSUPPORTED_PLACEHOLDER
from llm_providers import anthropic_provider
from llm_providers.anthropic_provider import AnthropicProvider
from llm_providers.base import ProviderConfig

_PNG_BYTES = b'\x89PNG\r\n\x1a\nfake-body'


@pytest.fixture(autouse=True)
def _clean_store():
    # The "warn once per image" memory is module state: without clearing it the
    # warn-once assertions would depend on test order.
    image_store.reset()
    anthropic_provider._warned_unsupported_images.clear()
    yield
    image_store.reset()
    anthropic_provider._warned_unsupported_images.clear()


@pytest.fixture
def provider():
    """A provider whose SDK client is never called (client construction is offline)."""
    return AnthropicProvider(ProviderConfig(api_key='test-key', model='claude-3-5-sonnet'))


@pytest.fixture
def image_id():
    return image_store.put(_PNG_BYTES, 'image/png')


def _fake_response(text='I cannot see images.'):
    return SimpleNamespace(
        content=[SimpleNamespace(type='text', text=text)],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )


# ---------------------------------------------------------------------------
# Through chat_completion: the turn happens, the SDK sees no image
# ---------------------------------------------------------------------------


def test_an_image_message_is_degraded_instead_of_refused(provider, image_id, monkeypatch):
    captured = {}

    def _fake_create(**kwargs):
        captured.update(kwargs)
        return _fake_response()

    monkeypatch.setattr(provider.client.messages, 'create', _fake_create)

    response = provider.chat_completion(
        [{'role': 'user', 'content': 'look', 'images': [image_id]}]
    )

    assert response.content == 'I cannot see images.'
    sent = captured['messages'][0]
    assert sent == {
        'role': 'user',
        'content': f'look\n\n{IMAGE_UNSUPPORTED_PLACEHOLDER}',
    }


def test_the_placeholder_does_not_replace_the_question(provider, image_id):
    degraded = provider._degrade_image_content(
        [{'role': 'user', 'content': 'why is this red?', 'images': [image_id]}],
        'claude-3-5-sonnet',
    )

    assert degraded[0]['content'] == f'why is this red?\n\n{IMAGE_UNSUPPORTED_PLACEHOLDER}'


def test_an_image_only_turn_still_has_content_to_send(provider, image_id):
    # The SDK rejects an empty content string, so the placeholder stands alone.
    degraded = provider._degrade_image_content(
        [{'role': 'user', 'content': '', 'images': [image_id]}], 'claude-3-5-sonnet'
    )

    assert degraded[0]['content'] == IMAGE_UNSUPPORTED_PLACEHOLDER


def test_several_images_get_one_placeholder_each(provider):
    first = image_store.put(b'first', 'image/png')
    second = image_store.put(b'second', 'image/png')

    degraded = provider._degrade_image_content(
        [{'role': 'user', 'content': 'compare', 'images': [first, second]}],
        'claude-3-5-sonnet',
    )

    assert degraded[0]['content'] == (
        f'compare\n\n{IMAGE_UNSUPPORTED_PLACEHOLDER}\n\n{IMAGE_UNSUPPORTED_PLACEHOLDER}'
    )


# ---------------------------------------------------------------------------
# What the change must not break
# ---------------------------------------------------------------------------


def test_the_callers_transcript_is_not_mutated(provider, image_id):
    messages = [{'role': 'user', 'content': 'look', 'images': [image_id]}]

    provider._degrade_image_content(messages, 'claude-3-5-sonnet')

    assert messages == [{'role': 'user', 'content': 'look', 'images': [image_id]}]


def test_the_bytes_are_left_in_the_store(provider, image_id):
    # Withholding is per turn: switching to a vision-capable adapter later must
    # still find the image.
    provider._degrade_image_content(
        [{'role': 'user', 'content': 'look', 'images': [image_id]}], 'claude-3-5-sonnet'
    )

    assert image_store.get(image_id) == (_PNG_BYTES, 'image/png')


def test_image_free_conversations_are_returned_unchanged(provider):
    messages = [
        {'role': 'system', 'content': 'be brief'},
        {'role': 'user', 'content': 'hello'},
    ]

    assert provider._degrade_image_content(messages, 'claude-3-5-sonnet') is messages


def test_an_empty_images_list_is_ignored(provider):
    messages = [{'role': 'user', 'content': 'hello', 'images': []}]

    assert provider._degrade_image_content(messages, 'claude-3-5-sonnet') is messages


def test_an_unaccompanied_image_message_does_not_stop_the_others(provider, image_id):
    degraded = provider._degrade_image_content(
        [
            {'role': 'system', 'content': 'be brief'},
            {'role': 'user', 'content': 'look', 'images': [image_id]},
            {'role': 'assistant', 'content': 'ok'},
        ],
        'claude-3-5-sonnet',
    )

    assert degraded[0] == {'role': 'system', 'content': 'be brief'}
    assert 'images' not in degraded[1]
    assert degraded[2] == {'role': 'assistant', 'content': 'ok'}


def test_the_degradation_is_logged_once_per_image_not_once_per_turn(provider, image_id, monkeypatch):
    warnings = []

    def _record(level, category, message):
        if level == 'WARNING':
            warnings.append(message)

    monkeypatch.setattr(anthropic_provider, 'log', _record)
    messages = [{'role': 'user', 'content': 'look', 'images': [image_id]}]

    for _ in range(3):
        provider._degrade_image_content(messages, 'claude-3-5-sonnet')

    assert len(warnings) == 1
    assert image_id in warnings[0]
    assert 'claude-3-5-sonnet' in warnings[0]
