"""Canonical internal representation (IR) for messages, content and tool calls.

This is the type layer described in ``docs/design-streaming-canonical-ir.md`` §2.
It is pure data: it imports nothing from ``agent.core.agent`` and nothing from
``llm_providers``. Provider adapters convert IR -> wire and wire -> IR; the turn
loop never sees a wire shape.

Standard library only — the core does not use pydantic, and these are internal
types, so ``dataclasses`` is the whole dependency surface.

**MICRO-RULING (invariant 7).** The spec's type list gives reasoning two homes: a
``ReasoningBlock`` inside ``content``, and the message-level ``reasoning`` /
``reasoning_signature`` fields. Allowing both on one message would recreate the
two-representation shape this design pass exists to remove, so a message uses one
home or the other, never both. ``CanonicalMessage.__post_init__`` enforces it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Union

__all__ = [
    "ROLE_VALUES",
    "TextBlock",
    "ImageBlock",
    "ToolUseBlock",
    "ToolResultBlock",
    "ReasoningBlock",
    "ContentBlock",
    "CanonicalMessage",
    "CanonicalToolCall",
    "CanonicalToolDefinition",
    "UnsupportedContentError",
    "block_from_dict",
    "block_to_dict",
    "message_from_dict",
]

# The four roles the canonical layer recognises (spec §2).
ROLE_VALUES = ("system", "user", "assistant", "tool")


# ---------------------------------------------------------------------------
# Content blocks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TextBlock:
    """A run of assistant or user text."""

    text: str


@dataclass(frozen=True)
class ImageBlock:
    """An image.

    ``source`` is deliberately a ``str``: either an inline base64 payload or a
    URL, exactly as the provider handed it over. Classifying it further would
    bake one vendor's discriminator into the IR; the adapter that produced the
    block knows which form it is.
    """

    source: str
    media_type: str  # e.g. "image/png"


@dataclass(frozen=True)
class ToolUseBlock:
    """A tool call as it appears inside a message's content."""

    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ToolResultBlock:
    """The result of a tool call, answering the ``ToolUseBlock`` with the same id.

    ``content`` is a ``str``: the flattened result the executor already produces.
    Structured results stay in the executor's own dict; the IR carries the text
    the provider will be shown.
    """

    tool_use_id: str
    content: str
    is_error: bool = False

    def __post_init__(self) -> None:
        # §2 inv 4 (tightened): a tool result without an id answers nothing.
        if not isinstance(self.tool_use_id, str) or not self.tool_use_id:
            raise ValueError(
                "ToolResultBlock.tool_use_id must be a non-empty str, "
                f"got {self.tool_use_id!r}"
            )


@dataclass(frozen=True)
class ReasoningBlock:
    """A chunk of reasoning text, optionally carrying a provider signature.

    A block with a signature is state and must be replayed verbatim; a block
    without one is an annotation and may be dropped (spec §2, invariant 3).
    """

    text: str
    signature: str | None = None

    def __post_init__(self) -> None:
        # tightening: signature is str | None, and non-empty when present.
        if self.signature is not None:
            if not isinstance(self.signature, str) or not self.signature:
                raise ValueError(
                    "ReasoningBlock.signature must be None or a non-empty str, "
                    f"got {self.signature!r}"
                )


ContentBlock = Union[
    TextBlock,
    ImageBlock,
    ToolUseBlock,
    ToolResultBlock,
    ReasoningBlock,
]


# ---------------------------------------------------------------------------
# Messages and tool types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CanonicalMessage:
    """One message in the canonical shape.

    ``content`` is always a list of blocks, even when it holds exactly one text
    block (§2 inv 2). ``reasoning`` / ``reasoning_signature`` are the
    message-level home for reasoning; a ``ReasoningBlock`` in ``content`` is the
    other home, and a message uses exactly one of them (micro-ruling).
    """

    role: Literal["system", "user", "assistant", "tool"]
    content: list[ContentBlock]
    reasoning: str | None = None
    reasoning_signature: str | None = None

    def __post_init__(self) -> None:
        # §2 inv 2: a single string is a wire-format special case, not an IR
        # state.
        if not isinstance(self.content, list):
            raise ValueError(
                "CanonicalMessage.content must be a list of ContentBlock, "
                f"got {type(self.content).__name__}: {self.content!r}"
            )

        # micro-ruling: one home for reasoning, never both.
        has_block_reasoning = any(
            isinstance(block, ReasoningBlock) for block in self.content
        )
        has_message_reasoning = (
            self.reasoning is not None or self.reasoning_signature is not None
        )
        if has_block_reasoning and has_message_reasoning:
            raise ValueError(
                "CanonicalMessage carries reasoning twice: a ReasoningBlock in "
                "content and message-level reasoning/reasoning_signature. Use "
                "one home (micro-ruling)."
            )

    # -- serialisation ------------------------------------------------------
    # Plain dicts, for round-trip tests and for debug dumps. This is *not* a
    # wire shape and no adapter should treat it as one.

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": [block_to_dict(block) for block in self.content],
            "reasoning": self.reasoning,
            "reasoning_signature": self.reasoning_signature,
        }


@dataclass(frozen=True)
class CanonicalToolCall:
    """A complete tool call produced by the model.

    ``arguments`` is always a ``dict`` (§2 inv 1). A vendor that streams a
    JSON string is parsed by its adapter; a tool call whose arguments never
    parse carries ``parse_error`` and ``arguments == {}`` (§2 inv 6) rather
    than a half-parsed dict.
    """

    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str | None = None
    parse_error: str | None = None

    def __post_init__(self) -> None:
        # §2 inv 1: always a dict, never a string, never None.
        if not isinstance(self.arguments, dict):
            raise ValueError(
                "CanonicalToolCall.arguments must be a dict, "
                f"got {type(self.arguments).__name__}: {self.arguments!r}"
            )

        # §2 inv 6: a parse error means no arguments, not partial ones.
        if self.parse_error is not None and self.arguments != {}:
            raise ValueError(
                "CanonicalToolCall with parse_error must have arguments == {}, "
                f"got {self.arguments!r}"
            )


@dataclass(frozen=True)
class CanonicalToolDefinition:
    """A tool the model may call. This *is* today's OpenAI tool shape."""

    name: str
    description: str
    parameters: dict[str, Any]

    def __post_init__(self) -> None:
        # tightening: the JSON schema is a dict, not a JSON string.
        if not isinstance(self.parameters, dict):
            raise ValueError(
                "CanonicalToolDefinition.parameters must be a dict, "
                f"got {type(self.parameters).__name__}: {self.parameters!r}"
            )


class UnsupportedContentError(Exception):
    """Raised by an adapter that meets a block it cannot express.

    Spec §2, invariant 5: an adapter that cannot express a block (a text-only
    provider meeting an ``ImageBlock``, say) raises this rather than silently
    dropping the content.
    """


# ---------------------------------------------------------------------------
# Deserialisation helpers (the inverse of to_dict / block_to_dict)
# ---------------------------------------------------------------------------

_BLOCK_TYPES = {
    "text": TextBlock,
    "image": ImageBlock,
    "tool_use": ToolUseBlock,
    "tool_result": ToolResultBlock,
    "reasoning": ReasoningBlock,
}


def block_to_dict(block: ContentBlock) -> dict[str, Any]:
    """Serialise one content block to a plain dict."""
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ImageBlock):
        return {
            "type": "image",
            "source": block.source,
            "media_type": block.media_type,
        }
    if isinstance(block, ToolUseBlock):
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": block.input,
        }
    if isinstance(block, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "content": block.content,
            "is_error": block.is_error,
        }
    if isinstance(block, ReasoningBlock):
        return {
            "type": "reasoning",
            "text": block.text,
            "signature": block.signature,
        }
    raise ValueError(f"Unknown content block: {block!r}")


def block_from_dict(data: dict[str, Any]) -> ContentBlock:
    """Rebuild one content block from :func:`block_to_dict` output."""
    kind = data.get("type")
    if kind == "text":
        return TextBlock(text=data["text"])
    if kind == "image":
        return ImageBlock(source=data["source"], media_type=data["media_type"])
    if kind == "tool_use":
        return ToolUseBlock(id=data["id"], name=data["name"], input=data["input"])
    if kind == "tool_result":
        return ToolResultBlock(
            tool_use_id=data["tool_use_id"],
            content=data["content"],
            is_error=data.get("is_error", False),
        )
    if kind == "reasoning":
        return ReasoningBlock(text=data["text"], signature=data.get("signature"))
    raise ValueError(f"Unknown content block type: {kind!r}")


def message_from_dict(data: dict[str, Any]) -> CanonicalMessage:
    """Rebuild a :class:`CanonicalMessage` from :meth:`CanonicalMessage.to_dict`."""
    return CanonicalMessage(
        role=data["role"],
        content=[block_from_dict(block) for block in data["content"]],
        reasoning=data.get("reasoning"),
        reasoning_signature=data.get("reasoning_signature"),
    )
