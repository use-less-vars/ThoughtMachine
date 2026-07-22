"""SessionConfig: Session-level configuration model.

SessionConfig holds the per-session configuration that the frontend can
modify.  It serves as the single source of truth for session-level settings
and enforces mode-based constraints via pydantic validators.

``workspace_path`` is stored separately on :class:`~web_ui.backend.bridge.WebAgentBridge`
as ``self._workspace_path`` because it is a bridge-level concern, not a session-level one.
The ``to_agent_config()`` method injects it back when constructing the full
:class:`~agent.config.models.AgentConfig`.
"""

from __future__ import annotations
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field, model_validator, ConfigDict


def _load_file_or_fallback(path: str, fallback: str = '') -> str:
    """Read a file if it exists, otherwise return the fallback."""
    try:
        from pathlib import Path
        p = Path(path)
        if p.exists():
            return p.read_text(encoding='utf-8')
    except Exception:
        pass
    return fallback


class SessionConfig(BaseModel):
    """Session-level configuration model.

    This is the subset of configuration that varies per session and can be
    modified by the frontend at runtime.  Fields that are global/infrastructure
    (e.g. RAG settings, logging, workspace path) live elsewhere.

    Mode enforcement:
        When ``mode`` is not ``'custom'``, :meth:`update_tools` and
        :meth:`update_prompt` are **no-ops** — the mode preset is enforced
        and the frontend cannot override it.
    """

    model_config = ConfigDict(extra="ignore")

    # ── Fields ──────────────────────────────────────────────────────────

    enabled_tools: List[str] = Field(
        default_factory=list,
        description='List of enabled tool names for this session.',
    )
    workspace_path: Optional[str] = Field(
        default=None,
        description='Workspace/project directory path for this session',
    )
    workspace_id: Optional[str] = Field(
        default=None,
        description='Workspace/project identifier for this session',
    )
    system_prompt: Optional[str] = Field(
        default=None,
        description='Custom system prompt for this session.  When None, the factory default is used.',
    )
    mode: Optional[str] = Field(
        default=None,
        description='Session mode — e.g. "engineer", "architect", or "custom".  When not "custom", tools and prompts are locked to the mode preset.',
    )
    max_turns: Optional[int] = Field(
        default=None,
        ge=1,
        description='Maximum conversation turns for this session.',
    )
    provider_id: Optional[str] = Field(
        default=None,
        description='Provider identifier for the LLM (e.g. "v4_flash").',
    )
    model: Optional[str] = Field(
        default=None,
        description='Model name for this session (e.g. "deepseek-v4-flash").',
    )
    api_key: Optional[str] = Field(
        default=None,
        description='API key for the provider.  Excluded from serialization.',
    )
    base_url: Optional[str] = Field(
        default=None,
        description='Base URL override for the LLM provider.',
    )
    model_override: Optional[str] = Field(
        default=None,
        description='Model name override (e.g. "deepseek-v4-flash").',
    )
    temperature: float = Field(
        default=0.7,
        description='Temperature for LLM sampling.',
    )
    session_permissions: Optional[Dict[str, Any]] = Field(
        default=None,
        description='Session-level permission overrides.',
    )

    # ── Validators ──────────────────────────────────────────────────────

    @model_validator(mode='after')
    def enforce_mode_presets(self) -> 'SessionConfig':
        """Lock tools and prompt to the factory preset for non‑custom modes."""
        if not self.mode or self.mode == 'custom':
            return self

        # ── tools ─────────────────────────────────────
        from agent.config.presets import get_tools_for_mode
        preset_tools = get_tools_for_mode(self.mode)
        if preset_tools:
            object.__setattr__(self, 'enabled_tools', list(preset_tools))

        # ── system prompt ────────────────────────────
        from pathlib import Path
        resources_dir = Path(__file__).resolve().parent.parent.parent / 'resources'
        prompt_map = {
            'agent':     resources_dir / 'default_system_prompt.txt',
            'engineer':  resources_dir / 'engineer_system_prompt.txt',
        }
        prompt_path = prompt_map.get(self.mode)
        if prompt_path:
            try:
                text = prompt_path.read_text(encoding='utf-8')
                if text:
                    object.__setattr__(self, 'system_prompt', text)
            except (FileNotFoundError, IOError):
                pass  # keep whatever prompt was already set

        return self

    # ── Mutators ────────────────────────────────────────────────────────

    def update_tools(self, new_tools: List[str]) -> None:
        """Update the enabled tools list.

        In ``'custom'`` mode this directly replaces the tool list.
        In any other mode this is a **no-op** — the mode preset is enforced.
        """
        if self.mode and self.mode != 'custom':
            return  # mode-locked — cannot override
        self.enabled_tools = list(new_tools)

    def update_prompt(self, new_prompt: Optional[str]) -> None:
        """Update the system prompt.

        In ``'custom'`` mode this replaces the prompt.
        In any other mode this is a **no-op** — the mode preset is enforced.
        """
        if self.mode and self.mode != 'custom':
            return  # mode-locked — cannot override
        self.system_prompt = new_prompt

    # ── Conversions ─────────────────────────────────────────────────────

    def to_agent_config(self, workspace_path: Optional[str] = None) -> 'AgentConfig':
        """Convert this ``SessionConfig`` to a full :class:`~agent.config.models.AgentConfig`.

        Lazy-imports ``AgentConfig`` at runtime to avoid circular imports.

        Args:
            workspace_path: Optional workspace path to inject into the AgentConfig.

        Returns:
            An ``AgentConfig`` instance populated from session-level fields.
        """
        from agent.config.models import AgentConfig

        kwargs: Dict[str, Any] = {
            'enabled_tools': list(self.enabled_tools),
            'max_turns': self.max_turns or 0,
            'system_prompt': self.system_prompt or '',
            'provider_id': self.provider_id or '',
            'api_key': self.api_key or '',
            'base_url': self.base_url or '',
            'model': self.model or '',
            'model_override': self.model_override or '',
            'temperature': self.temperature,
        }

        if self.mode:
            kwargs['mode'] = self.mode

        if workspace_path:
            kwargs['workspace_path'] = workspace_path

        if self.session_permissions is not None:
            kwargs['session_permissions'] = dict(self.session_permissions)

        return AgentConfig(**kwargs)

    @classmethod
    def from_factory(cls, mode: str = 'custom', workspace_path: str = '', **kwargs) -> 'SessionConfig':
        """Create a default SessionConfig."""
        return cls(
            mode=mode,
            workspace_path=workspace_path or None,
            **kwargs
        )
