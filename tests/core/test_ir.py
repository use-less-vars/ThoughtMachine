"""Tests for the canonical IR type layer (``agent/core/ir.py``).

One test per invariant from ``docs/design-streaming-canonical-ir.md`` §2, plus a
round-trip through the plain-dict helpers. The IR is pure data, so every case
here is a plain constructor call -- no provider, no network, no fixtures.
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pytest

from agent.core.ir import (
    ROLE_VALUES,
    CanonicalMessage,
    CanonicalToolCall,
    CanonicalToolDefinition,
    ImageBlock,
    ReasoningBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UnsupportedContentError,
    block_from_dict,
    block_to_dict,
    message_from_dict,
)


# ---------------------------------------------------------------------------
# Shape of the type layer
# ---------------------------------------------------------------------------


def test_every_public_type_instantiates():
    text = TextBlock(text="hello")
    image = ImageBlock(source="iVBORw0KGgo=", media_type="image/png")
    tool_use = ToolUseBlock(id="tu_1", name="read_file", input={"path": "a.txt"})
    tool_result = ToolResultBlock(tool_use_id="tu_1", content="file body")
    reasoning = ReasoningBlock(text="weighing options", signature="sig-1")
    message = CanonicalMessage(role="user", content=[text])
    call = CanonicalToolCall(id="tu_1", name="read_file", arguments={"path": "a.txt"})
    definition = CanonicalToolDefinition(
        name="read_file",
        description="Read a file.",
        parameters={"type": "object", "properties": {}},
    )

    assert text.text == "hello"
    assert (image.source, image.media_type) == ("iVBORw0KGgo=", "image/png")
    assert (tool_use.id, tool_use.name, tool_use.input) == (
        "tu_1",
        "read_file",
        {"path": "a.txt"},
    )
    assert (tool_result.tool_use_id, tool_result.content) == ("tu_1", "file body")
    assert (reasoning.text, reasoning.signature) == ("weighing options", "sig-1")
    assert message.content == [text]
    assert (call.id, call.name, call.arguments) == (
        "tu_1",
        "read_file",
        {"path": "a.txt"},
    )
    assert definition.parameters == {"type": "object", "properties": {}}
    assert ROLE_VALUES == ("system", "user", "assistant", "tool")
    assert message.role in ROLE_VALUES


def test_single_text_block_is_still_a_list():
    message = CanonicalMessage(role="assistant", content=[TextBlock(text="only")])

    assert isinstance(message.content, list)
    assert not isinstance(message.content, str)
    assert len(message.content) == 1
    assert isinstance(message.content[0], TextBlock)
    assert message.content[0].text == "only"


# ---------------------------------------------------------------------------
# Round-trip: IR -> dict -> IR
# ---------------------------------------------------------------------------


def _roundtrip(message: CanonicalMessage) -> CanonicalMessage:
    return message_from_dict(message.to_dict())


def test_roundtrip_with_block_level_reasoning():
    original = CanonicalMessage(
        role="assistant",
        content=[
            ReasoningBlock(text="weighing options", signature="sig-abc"),
            TextBlock(text="I will read the file."),
            ToolUseBlock(
                id="tu_9",
                name="read_file",
                input={"path": "b.txt", "lines": [1, 2]},
            ),
        ],
    )

    restored = _roundtrip(original)

    assert restored == original
    assert restored.to_dict() == original.to_dict()
    assert restored.reasoning is None
    assert restored.reasoning_signature is None


def test_roundtrip_with_message_level_reasoning():
    original = CanonicalMessage(
        role="assistant",
        content=[TextBlock(text="the answer")],
        reasoning="chain of thought",
        reasoning_signature="sig-xyz",
    )

    restored = _roundtrip(original)

    assert restored == original
    assert restored.to_dict() == original.to_dict()
    assert restored.reasoning == "chain of thought"
    assert restored.reasoning_signature == "sig-xyz"


@pytest.mark.parametrize(
    "block",
    [
        pytest.param(TextBlock(text="hi"), id="text"),
        pytest.param(
            ImageBlock(source="https://example.test/x.png", media_type="image/png"),
            id="image",
        ),
        pytest.param(
            ToolUseBlock(id="tu_1", name="read_file", input={"path": "a.txt"}),
            id="tool-use",
        ),
        pytest.param(
            ToolResultBlock(tool_use_id="tu_1", content="body", is_error=True),
            id="tool-result",
        ),
        pytest.param(ReasoningBlock(text="hm", signature="sig-1"), id="reasoning"),
    ],
)
def test_each_block_round_trips(block):
    assert block_from_dict(block_to_dict(block)) == block


# ---------------------------------------------------------------------------
# Invariants: violations are rejected at construction time
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "factory",
    [
        pytest.param(
            lambda: CanonicalToolCall(id="c1", name="read_file", arguments='{"a": 1}'),
            id="sec2-inv1-arguments-is-json-string",
        ),
        pytest.param(
            lambda: CanonicalToolCall(id="c1", name="read_file", arguments=None),
            id="sec2-inv1-arguments-is-none",
        ),
        pytest.param(
            lambda: CanonicalMessage(role="user", content="plain string"),
            id="sec2-inv2-content-is-string",
        ),
        pytest.param(
            lambda: ToolResultBlock(tool_use_id="", content="x"),
            id="sec2-inv4-empty-tool-use-id",
        ),
        pytest.param(
            lambda: CanonicalToolDefinition(
                name="read_file",
                description="Read a file.",
                parameters='{"type": "object"}',
            ),
            id="tightening-parameters-is-json-string",
        ),
        pytest.param(
            lambda: CanonicalToolCall(
                id="c1",
                name="read_file",
                arguments={"path": "a.txt"},
                parse_error="unterminated JSON",
            ),
            id="sec2-inv6-parse-error-with-arguments",
        ),
        pytest.param(
            lambda: ReasoningBlock(text="hm", signature=""),
            id="tightening-empty-signature",
        ),
        pytest.param(
            lambda: CanonicalMessage(
                role="assistant",
                content=[ReasoningBlock(text="hm", signature="sig-1")],
                reasoning="also here",
            ),
            id="micro-ruling-reasoning-block-and-message",
        ),
        pytest.param(
            lambda: CanonicalMessage(
                role="assistant",
                content=[ReasoningBlock(text="hm")],
                reasoning_signature="sig-1",
            ),
            id="micro-ruling-reasoning-block-and-message-signature",
        ),
    ],
)
def test_invariant_violation_raises(factory):
    with pytest.raises(ValueError):
        factory()


# ---------------------------------------------------------------------------
# Boundaries that must stay legal
# ---------------------------------------------------------------------------


def test_valid_boundaries_are_accepted():
    # An empty JSON object is a legitimate argument set.
    empty_call = CanonicalToolCall(id="c1", name="read_file", arguments={})
    assert empty_call.arguments == {}
    assert empty_call.raw_arguments is None
    assert empty_call.parse_error is None

    # A parse failure carries the raw text and no arguments, never a partial dict.
    failed_call = CanonicalToolCall(
        id="c1",
        name="read_file",
        arguments={},
        raw_arguments='{"path": ',
        parse_error="unterminated JSON",
    )
    assert failed_call.arguments == {}
    assert failed_call.parse_error == "unterminated JSON"

    # is_error defaults to False; empty result text is still a result.
    result = ToolResultBlock(tool_use_id="tu_1", content="")
    assert result.is_error is False
    assert result.content == ""

    # A signature-less reasoning block is an annotation, not state.
    annotation = ReasoningBlock(text="hm")
    assert annotation.signature is None

    # Either reasoning home on its own is fine.
    block_home = CanonicalMessage(
        role="assistant",
        content=[ReasoningBlock(text="hm", signature="sig-1")],
    )
    assert block_home.reasoning is None
    assert block_home.reasoning_signature is None

    message_home = CanonicalMessage(
        role="assistant",
        content=[TextBlock(text="the answer")],
        reasoning="chain of thought",
        reasoning_signature="sig-1",
    )
    assert message_home.reasoning == "chain of thought"

    # An empty content list is a list.
    assert CanonicalMessage(role="system", content=[]).content == []


# ---------------------------------------------------------------------------
# Mixed content
# ---------------------------------------------------------------------------


def test_mixed_content_keeps_block_order_and_types():
    message = CanonicalMessage(
        role="assistant",
        content=[
            TextBlock(text="Let me look."),
            ToolUseBlock(id="tu_1", name="read_file", input={"path": "a.txt"}),
            ToolResultBlock(tool_use_id="tu_1", content="file body", is_error=False),
        ],
    )

    assert [type(block) for block in message.content] == [
        TextBlock,
        ToolUseBlock,
        ToolResultBlock,
    ]

    restored = _roundtrip(message)

    assert restored == message
    assert [type(block) for block in restored.content] == [
        TextBlock,
        ToolUseBlock,
        ToolResultBlock,
    ]
    assert restored.content[1].id == restored.content[2].tool_use_id == "tu_1"


# ---------------------------------------------------------------------------
# UnsupportedContentError
# ---------------------------------------------------------------------------


def test_unsupported_content_error_is_catchable():
    assert issubclass(UnsupportedContentError, Exception)

    def adapter_meeting_an_image(block):
        # Stand-in for a text-only adapter, per spec §2 invariant 5.
        raise UnsupportedContentError(f"cannot express {type(block).__name__}")

    with pytest.raises(UnsupportedContentError):
        adapter_meeting_an_image(ImageBlock(source="x", media_type="image/png"))

    try:
        adapter_meeting_an_image(ImageBlock(source="x", media_type="image/png"))
    except UnsupportedContentError as exc:
        assert "ImageBlock" in str(exc)
    else:  # pragma: no cover - the raise above already proves this
        pytest.fail("UnsupportedContentError was not raised")
