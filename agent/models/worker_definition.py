"""
Pydantic V2 model for worker definitions.

A WorkerDefinition describes a reusable worker configuration that
can be spawned as a background agent. Optional fields default to
``None`` and inherit session-level values at spawn time.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class WorkerDefinition(BaseModel):
    """
    Reusable worker configuration.

    All fields are flat (no nesting).  Optional fields (``timeout_seconds``,
    ``max_turns``, etc.) default to ``None``, meaning the value is inherited
    from the spawning session at spawn time.
    """

    model_config = ConfigDict(populate_by_name=True)

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
        alias="worker_permissions",
        description=(
            'Permission categories required by this worker, e.g. '
            '``{"filesystem": "read"}``.'
        )
    )
    timeout_seconds: Optional[int] = Field(
        default=None,
        description="Maximum wall-clock time for a single worker turn."
    )
    max_turns: Optional[int] = Field(
        default=None,
        description="Maximum number of turns before the worker stops."
    )
    temperature: Optional[float] = Field(
        default=None,
        description="LLM temperature override for this worker."
    )
    warning_threshold_tokens: Optional[int] = Field(
        default=65000,
        description=(
            'Token count threshold for warning state. '
            'Used as ``token_monitor_warning_threshold`` in this '
            'worker\'s AgentConfig. Defaults to 65000.'
        )
    )
    critical_threshold_tokens: Optional[int] = Field(
        default=80000,
        description=(
            'Token count threshold for critical warning. '
            'Used as ``token_monitor_critical_threshold`` in this '
            'worker\'s AgentConfig. Defaults to 80000.'
        )
    )
