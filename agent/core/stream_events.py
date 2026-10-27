"""Streaming event contract between provider adapters and the turn loop.

This is the type layer described in ``docs/design-streaming-canonical-ir.md``
§3. The turn loop consumes a stream of these events instead of one
``LLMResponse``; adapters emit them, and nothing else does.

The invariant: the events are provider-neutral. An event carries what happened,
never how a vendor spelled it — no wire field names, no vendor discriminators,
no ``raw_response``. A stream produced by one adapter is valid input to the same
turn loop as a stream produced by any other (§2 inv 5, applied to the stream).

Pure data, standard library only, no behaviour. Translation lives in the
adapters; classification lives in the turn loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from agent.core.ir import CanonicalToolCall

__all__ = [
    "TextDelta",
    "ReasoningDelta",
    "ToolCallStart",
    "ToolCallArgumentsDelta",
    "ToolCallComplete",
    "UsageUpdate",
    "Interrupted",
    "Done",
    "StreamEvent",
]


@dataclass(frozen=True)
class TextDelta:
    """A chunk of assistant text."""

    text: str


@dataclass(frozen=True)
class ReasoningDelta:
    """A chunk of reasoning text.

    May arrive interleaved with :class:`TextDelta` (Anthropic, DeepSeek). The
    field is ``text`` to match ``ReasoningBlock.text``, which is what the
    accumulated delta feeds.
    """

    text: str


@dataclass(frozen=True)
class ToolCallStart:
    """The model has begun producing a tool call."""

    id: str
    name: str


@dataclass(frozen=True)
class ToolCallArgumentsDelta:
    """Incremental argument bytes for one tool call, addressed by its id."""

    id: str
    arguments_delta: str


@dataclass(frozen=True)
class ToolCallComplete:
    """A tool call whose arguments are complete, or definitively malformed.

    ``tool_call`` is the canonical IR type, not a wire shape. When
    ``parse_error`` is set the call's ``arguments`` are ``{}`` (§2 inv 6) and
    the turn loop treats the call as a refusal-shape error result rather than
    an :class:`Interrupted`.
    """

    tool_call: CanonicalToolCall
    parse_error: str | None = None


@dataclass(frozen=True)
class UsageUpdate:
    """The provider reported authoritative token counts."""

    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class Interrupted:
    """The stream was closed by the caller (cancellation) or the provider (error).

    A first-class terminal and a signal, not an exception.
    """

    reason: str


@dataclass(frozen=True)
class Done:
    """The stream completed normally. Carries no payload."""


#: The union an adapter annotates its iterator with:
#: ``AsyncIterator[StreamEvent]``.
StreamEvent = Union[
    TextDelta,
    ReasoningDelta,
    ToolCallStart,
    ToolCallArgumentsDelta,
    ToolCallComplete,
    UsageUpdate,
    Interrupted,
    Done,
]
