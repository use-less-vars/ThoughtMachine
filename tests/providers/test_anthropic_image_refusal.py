"""Tests for the Anthropic adapter's refusal of pasted images.

This adapter does not build ``image`` content blocks yet, so a conversation
carrying an image must fail loudly with ``UnsupportedContentError`` instead of
silently dropping the screenshot and answering as if the model had seen it.
No network: the refusal happens before the SDK is touched.
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pytest

from agent.core import image_store
from agent.core.ir import UnsupportedContentError
from llm_providers.anthropic_provider import AnthropicProvider
from llm_providers.base import ProviderConfig


@pytest.fixture(autouse=True)
def _clean_store():
    image_store.reset()
    yield
    image_store.reset()


@pytest.fixture
def provider():
    """A provider whose SDK client is never called (client construction is offline)."""
    return AnthropicProvider(ProviderConfig(api_key='test-key', model='claude-3-5-sonnet'))


@pytest.fixture
def image_id():
    return image_store.put(b'\x89PNG\r\n\x1a\nfake-body', 'image/png')


def test_an_image_message_is_refused(provider, image_id, monkeypatch):
    def _never_called(*args, **kwargs):  # pragma: no cover - the refusal is earlier
        raise AssertionError('the SDK must not be called for a refused image')

    monkeypatch.setattr(provider.client.messages, 'create', _never_called)

    with pytest.raises(UnsupportedContentError) as excinfo:
        provider.chat_completion([{'role': 'user', 'content': 'look', 'images': [image_id]}])

    assert 'claude-3-5-sonnet' in str(excinfo.value)


def test_the_refusal_is_not_rewritten_as_a_provider_error(provider, image_id):
    with pytest.raises(UnsupportedContentError) as excinfo:
        provider.chat_completion(
            [
                {'role': 'system', 'content': 'be brief'},
                {'role': 'user', 'content': 'look', 'images': [image_id]},
            ]
        )

    assert 'Unexpected error' not in str(excinfo.value)


def test_image_free_conversations_are_not_refused(provider):
    # The guard returns instead of raising; nothing here reaches the SDK.
    assert provider._reject_image_content(
        [{'role': 'user', 'content': 'hello'}], 'claude-3-5-sonnet'
    ) is None


def test_an_empty_images_list_is_not_refused(provider):
    assert provider._reject_image_content(
        [{'role': 'user', 'content': 'hello', 'images': []}], 'claude-3-5-sonnet'
    ) is None
