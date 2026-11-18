"""Tests for the single message-token estimator and its four call sites.

``estimate_message_tokens`` is the one place a canonical message is priced: the
token counter, the context builder, the worker context and the provider
adapters all delegate to it, so a message costs the same everywhere -- and a
pasted screenshot costs a flat per-image allowance instead of the JSON text of
its id.

Every test runs hermetically: where an exact number is asserted, a one-token-
per-character stub encoder is injected, so nothing here downloads a tiktoken
BPE file and nothing depends on which estimator backend is installed.
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pytest

from agent.core.token_counter import (
    IMAGE_TOKEN_ESTIMATE,
    TokenCounter,
    estimate_message_tokens,
)
from agent.core.worker_context import WorkerContext
from session.context_builder import SummaryBuilder


class _StubEncoder:
    """One token per character, so expected counts are exact and offline."""

    def encode(self, text):
        return list(text)


_ENCODER = _StubEncoder()


class _Config:
    """Minimal stand-in for AgentConfig (TokenCounter only reads ``model``)."""

    model = 'gpt-4o'


# ---------------------------------------------------------------------------
# What the estimator counts
# ---------------------------------------------------------------------------


def test_text_only_message_counts_exactly_its_content():
    message = {'role': 'user', 'content': 'why does this test fail?'}

    assert estimate_message_tokens(message, encoder=_ENCODER) == len('why does this test fail?')


def test_each_image_adds_exactly_the_flat_allowance():
    text = {'role': 'user', 'content': 'look at this'}
    one = {'role': 'user', 'content': 'look at this', 'images': ['0123456789abcdef']}
    two = {'role': 'user', 'content': 'look at this', 'images': ['0123456789abcdef', 'fedcba9876543210']}

    assert estimate_message_tokens(one, encoder=_ENCODER) == estimate_message_tokens(text, encoder=_ENCODER) + IMAGE_TOKEN_ESTIMATE
    assert estimate_message_tokens(two, encoder=_ENCODER) == estimate_message_tokens(text, encoder=_ENCODER) + 2 * IMAGE_TOKEN_ESTIMATE


def test_image_ids_themselves_cost_nothing():
    """Ids are opaque handles; their length must not move the estimate."""
    short = {'role': 'user', 'content': 'see', 'images': ['a']}
    long = {'role': 'user', 'content': 'see', 'images': ['a' * 4096]}

    assert estimate_message_tokens(short, encoder=_ENCODER) == estimate_message_tokens(long, encoder=_ENCODER)


def test_empty_images_list_costs_nothing():
    assert estimate_message_tokens({'role': 'user', 'content': 'hi', 'images': []}, encoder=_ENCODER) == 2
    assert estimate_message_tokens({'role': 'user', 'content': 'hi', 'images': None}, encoder=_ENCODER) == 2


def test_structural_keys_are_not_priced():
    """Serialising the dict (the old behaviour) must not creep back in.

    A tool-call payload or a long id is not prompt text; counting it is how the
    previous json.dumps-based estimator inflated messages.
    """
    plain = {'role': 'user', 'content': 'run the tests'}
    noisy = {
        'role': 'user',
        'content': 'run the tests',
        'id': 'x' * 500,
        'tool_call_id': 'call_' + 'y' * 200,
        'tool_calls': [{'id': 'z' * 100, 'function': {'name': 'read_file', 'arguments': '{"path": "a"}'}}],
    }

    assert estimate_message_tokens(noisy, encoder=_ENCODER) == estimate_message_tokens(plain, encoder=_ENCODER)


def test_empty_or_missing_content_is_zero():
    assert estimate_message_tokens({'role': 'system', 'content': ''}, encoder=_ENCODER) == 0
    assert estimate_message_tokens({'role': 'system'}, encoder=_ENCODER) == 0
    assert estimate_message_tokens({}, encoder=_ENCODER) == 0


def test_non_dict_input_is_tokenised_as_text():
    assert estimate_message_tokens('hello world', encoder=_ENCODER) == len('hello world')


# ---------------------------------------------------------------------------
# Delegation: one estimator, four call sites
# ---------------------------------------------------------------------------


def test_token_counter_delegates_message_dicts():
    counter = TokenCounter(_Config())
    message = {'role': 'user', 'content': 'a screenshot question', 'images': ['0123456789abcdef']}

    # Same answer whether or not tiktoken is installed: both paths share it.
    assert counter.estimate_tokens(message) == estimate_message_tokens(message)
    assert (
        counter.estimate_tokens(message)
        - counter.estimate_tokens({'role': 'user', 'content': 'a screenshot question'})
        == IMAGE_TOKEN_ESTIMATE
    )
    assert counter.estimate_tokens('hello world') > 0


def test_context_builder_delegates_message_dicts():
    builder = SummaryBuilder(default_keep_turns=5)
    message = {'role': 'user', 'content': 'a screenshot question', 'images': ['0123456789abcdef']}

    assert builder._estimate_tokens(message, encoder=_ENCODER) == estimate_message_tokens(message, encoder=_ENCODER)
    # No encoder handed over => the builder resolves one itself; no crash.
    assert builder._estimate_tokens(message) == estimate_message_tokens(message)


def test_worker_context_delegates_message_dicts():
    context = WorkerContext(session_id='worker-test')
    context.user_history = [
        {'role': 'user', 'content': 'look', 'images': ['0123456789abcdef', 'fedcba9876543210']},
        {'role': 'assistant', 'content': 'I see two screenshots.'},
    ]

    assert context.estimated_context_tokens() == sum(
        estimate_message_tokens(message) for message in context.user_history
    )


def test_image_estimate_stays_a_round_kilobyte():
    assert IMAGE_TOKEN_ESTIMATE == 1024


@pytest.mark.parametrize(
    'message',
    [
        pytest.param({'role': 'user', 'content': 'x' * 10000, 'images': ['a']}, id='large-text-one-image'),
        pytest.param({'role': 'tool', 'content': '', 'images': ['a', 'b', 'c']}, id='images-only'),
    ],
)
def test_estimates_are_positive_and_monotonic(message):
    without_images = dict(message, images=[])

    assert estimate_message_tokens(message, encoder=_ENCODER) > estimate_message_tokens(without_images, encoder=_ENCODER)
