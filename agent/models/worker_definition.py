"""
Pydantic V2 model for worker definitions.

A WorkerDefinition describes a reusable worker configuration that
can be spawned as a background agent. Optional fields default to
``None`` and inherit session-level values at spawn time.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Lazy import helper — avoids circular imports at module load time
# ---------------------------------------------------------------------------
def _get_valid_tool_names() -> set[str]:
    """Return the set of all registered tool class names."""
    from tools import TOOL_CLASSES  # noqa: PLC0415 — deferred import

    return {cls.__name__ for cls in TOOL_CLASSES}


class WorkerDefinition(BaseModel):
    """
    Reusable worker configuration.

    All fields are flat (no nesting).  Optional fields (``timeout_seconds``,
    ``max_context_tokens``, etc.) default to ``None``, meaning the value is
    inherited from the spawning session at spawn time.
    """

    name: str = Field(
        description="Unique name for this worker definition."
    )
    description: str = Field(
        description="Human-readable description of what this worker does."
    )
    system_prompt: str = Field(
        description="System prompt used to initialise the worker agent."
    )
    tools: list[str] = Field(
        description="List of tool names this worker is allowed to call."
    )
    permission_footprint: dict[str, str] = Field(
        description=(
            'Permission categories required by this worker, e.g. '
            '``{"filesystem": "read"}``.'
        )
    )
    timeout_seconds: Optional[int] = Field(
        default=None,
        description="Maximum wall-clock time for a single worker turn."
    )
    max_context_tokens: Optional[int] = Field(
        default=None,
        description="Maximum tokens for the worker's conversation context."
    )
    warning_threshold_tokens: Optional[int] = Field(
        default=None,
        description="Token count at which the worker emits a warning."
    )
    turn_limit: Optional[int] = Field(
        default=None,
        description="Maximum number of turns before the worker stops."
    )
    temperature: Optional[float] = Field(
        default=None,
        description="LLM temperature override for this worker."
    )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    @model_validator(mode="after")
    def _validate_tools_exist(self) -> WorkerDefinition:
        """Check every tool name in *tools* exists in the tool registry."""
        unknown = [t for t in self.tools if t not in _get_valid_tool_names()]
        if unknown:
            raise ValueError(
                f"Unknown tool(s): {', '.join(sorted(unknown))}. "
                f"Valid tools are: {sorted(_get_valid_tool_names())}"
            )
        return self
