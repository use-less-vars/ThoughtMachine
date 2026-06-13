"""
Configuration models for the ThoughtMachine agent.
"""
from typing import ClassVar, Optional, Callable, List, Any, Dict, Literal
from pathlib import Path
from pydantic import BaseModel, Field, field_validator, model_validator, model_serializer, ConfigDict
from agent.logging import log
from tools import SIMPLIFIED_TOOL_CLASSES
from thoughtmachine.security import SessionPermissions

# Category constants for AgentConfig fields
RESTART_REQUIRED = "restart_required"
HOT_SWAPPABLE = "hot_swappable"
SESSION_IDENTITY = "session_identity"
GLOBAL_STATIC = "global_static"


class AgentConfig(BaseModel):
    """Main configuration model for the ThoughtMachine agent."""

    #: Maps each field name to its category for driving GUI behavior and update logic.
    FIELD_CATEGORIES: ClassVar[Dict[str, str]] = {
        'provider_type': RESTART_REQUIRED,
        'model': RESTART_REQUIRED,
        'api_key': RESTART_REQUIRED,
        'base_url': RESTART_REQUIRED,
        'temperature': HOT_SWAPPABLE,
        'stop_check': RESTART_REQUIRED,
        'provider_config': RESTART_REQUIRED,
        'max_turns': HOT_SWAPPABLE,
        'system_prompt': RESTART_REQUIRED,
        'token_monitor_warning_threshold': HOT_SWAPPABLE,
        'token_monitor_critical_threshold': HOT_SWAPPABLE,
        'turn_monitor_enabled': HOT_SWAPPABLE,
        'enable_logging': GLOBAL_STATIC,
        'log_dir': GLOBAL_STATIC,
        'log_level': GLOBAL_STATIC,
        'enable_file_logging': GLOBAL_STATIC,
        'jsonl_format': GLOBAL_STATIC,
        'log_categories': GLOBAL_STATIC,
        'max_file_size_mb': GLOBAL_STATIC,
        'max_backup_files': GLOBAL_STATIC,
        'workspace_path': RESTART_REQUIRED,
        'detail': HOT_SWAPPABLE,
        'rag_enabled': RESTART_REQUIRED,
        'rag_embedding_model': RESTART_REQUIRED,
        'rag_vector_store_path': RESTART_REQUIRED,
        'rag_chunk_size': RESTART_REQUIRED,
        'rag_chunk_overlap': RESTART_REQUIRED,
        'rag_batch_size': RESTART_REQUIRED,
        'rag_truncate_dim': RESTART_REQUIRED,
        'kb_enabled': RESTART_REQUIRED,
        'kb_path': RESTART_REQUIRED,
        'tool_output_token_limit': HOT_SWAPPABLE,
        'enabled_tools': HOT_SWAPPABLE,
        'provider_id': RESTART_REQUIRED,
        'model_override': RESTART_REQUIRED,
        'session_permissions': HOT_SWAPPABLE,
    }

    api_key: str = Field(default='', exclude=True)
    base_url: str = 'https://api.deepseek.com'
    model: str = 'deepseek-reasoner'
    # NOTE: str (not Literal) so that provider plugins registered via
    # ProviderFactory.register_provider() — e.g. ``mock`` during tests —
    # are accepted by Pydantic validation.  Runtime validation happens
    # inside ProviderFactory.create_provider().
    provider_type: str = 'openai_compatible'
    provider_config: Dict[str, Any] = Field(default_factory=dict)
    provider_id: Optional[str] = Field(default=None, description='Active provider profile id from providers.json')
    model_override: Optional[str] = Field(default=None, description='Override model from the profile (leaves provider_id intact)')
    temperature: float = 0.2
    max_turns: int = 100
    stop_check: Optional[Callable[[], bool]] = Field(default=None, description='Runtime stop-check callback. Called periodically during agent execution to check if processing should be aborted. Return True to signal stop. Not serialised to/from JSON config.')
    system_prompt: Optional[str] = None
    token_monitor_warning_threshold: int = Field(default=35000, description='Token count threshold for warning (user)')
    token_monitor_critical_threshold: int = Field(default=50000, description='Token count threshold for critical warning (user)')
    turn_monitor_enabled: bool = Field(default=True, description='Enable automatic turn limit warnings')

    enable_logging: bool = Field(default=True, description='Enable agent logging')
    log_dir: str = Field(default='./logs', description='Directory for log files')
    log_level: str = Field(default='INFO', description='Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)')
    enable_file_logging: bool = Field(default=True, description='Write logs to files')
    jsonl_format: bool = Field(default=True, description='Use JSONL format for log files')
    log_categories: List[str] = Field(default_factory=lambda: ['SESSION', 'LLM', 'TOOLS'], description='List of log categories to enable (SESSION, UI, LLM, TOOLS, SECURITY, PERFORMANCE). Can be overridden by AGENT_LOG_CATEGORIES environment variable.')
    max_file_size_mb: int = Field(default=10, description='Maximum log file size in MB before rotation')
    max_backup_files: int = Field(default=5, description='Maximum number of backup log files to keep')
    workspace_path: Optional[str] = Field(default=None, description='Root directory for file operations (None = unrestricted)')
    rag_enabled: bool = Field(default=False, description='Enable RAG functionality')
    rag_embedding_model: str = Field(default='BAAI/bge-small-en-v1.5', description='Model name for sentence-transformers embeddings')
    rag_vector_store_path: Optional[str] = Field(default=None, description='Path to vector store database (None = default .thoughtmachine/rag/)')
    rag_chunk_size: int = Field(default=1500, description='Size of text chunks for RAG indexing (characters)')
    rag_chunk_overlap: int = Field(default=200, description='Overlap between chunks for RAG indexing (characters)')
    rag_batch_size: int = Field(default=16, description='Batch size for embedding generation in RAG indexing')
    rag_truncate_dim: int = Field(default=256, description='Dimension to truncate embeddings to for memory efficiency')
    kb_enabled: bool = Field(default=True, description="Enable the project knowledge base")
    kb_path: Optional[str] = Field(default=None, description="Path to knowledge base directory (None = default .thoughtmachine/knowledge/)")
    tool_output_token_limit: int = Field(default=10000, description='Maximum token limit for tool outputs (default 10,000 tokens)')
    detail: Literal['minimal', 'normal', 'verbose'] = Field(default='normal', description='Detail level for event display')
    enabled_tools: List[str] = Field(default_factory=lambda: [cls.__name__ for cls in SIMPLIFIED_TOOL_CLASSES], description='List of enabled tool class names')
    session_permissions: SessionPermissions = Field(
        default_factory=SessionPermissions,
        description='Session permissions profile controlling tool access categories.',
    )

    @field_validator('system_prompt')
    def load_default_system_prompt(cls, v):
        """Load the system prompt with the following precedence:

        1. ``~/.thoughtmachine/custom_system_prompt.txt`` (if it exists and is non-empty)
        2. Explicit value passed to constructor (if non-empty)
        3. Fallback to ``resources/default_system_prompt.txt``
        """
        from pathlib import Path
        # 1. Check custom_system_prompt.txt first
        custom_path = Path.home() / '.thoughtmachine' / 'custom_system_prompt.txt'
        if custom_path.exists():
            try:
                text = custom_path.read_text(encoding='utf-8').strip()
                if text:
                    return text
            except (FileNotFoundError, IOError) as exc:
                log.warning('Could not read custom system prompt from %s: %s', custom_path, exc)
        # 2. Use the explicit value if non-empty
        if v is not None and (isinstance(v, str) and v.strip() != ''):
            return v
        # 3. Fallback to factory default
        prompt_path = Path(__file__).resolve().parent.parent.parent / 'resources' / 'default_system_prompt.txt'
        try:
            return prompt_path.read_text(encoding='utf-8')
        except (FileNotFoundError, IOError) as exc:
            log.warning('Could not load default system prompt from %s: %s', prompt_path, exc)
            return 'You are ThoughtMachine, an AI agent.'

    @field_validator('enabled_tools')
    def filter_search_codebase_tool(cls, v, info):
        """Ensure SearchCodebaseTool and KnowledgeBaseTool are only available when their respective features are enabled."""
        rag_enabled = info.data.get('rag_enabled', False)
        kb_enabled = info.data.get('kb_enabled', True)
        filtered = list(v)
        if not rag_enabled:
            filtered = [tool for tool in filtered if tool != 'SearchCodebaseTool']
        if not kb_enabled:
            filtered = [tool for tool in filtered if tool != 'KnowledgeBaseTool']
        if filtered != v:
            return filtered
        return v

    @model_validator(mode='after')
    def filter_default_enabled_tools(self):
        """Filter SearchCodebaseTool and KnowledgeBaseTool from default enabled_tools when their respective features are disabled."""
        if self.enabled_tools:
            filtered = list(self.enabled_tools)
            if not self.rag_enabled:
                filtered = [tool for tool in filtered if tool != 'SearchCodebaseTool']
            if not self.kb_enabled:
                filtered = [tool for tool in filtered if tool != 'KnowledgeBaseTool']
            if filtered != self.enabled_tools:
                object.__setattr__(self, 'enabled_tools', filtered)
        return self

    @model_serializer(mode='wrap')
    def _serialize_stop_check(self, handler):
        """Exclude stop_check from serialization — it's a runtime-only callable."""
        d = handler(self)
        d.pop('stop_check', None)
        return d

    def get_filtered_tool_classes(self, enabled_tools=None):
        """Get tool classes filtered based on rag_enabled and enabled_tools.

        Args:
            enabled_tools: Optional override list of enabled tool names.
                          If None, uses self.enabled_tools.

        Returns:
            List of tool class objects.
        """
        from tools import SIMPLIFIED_TOOL_CLASSES
        tool_classes = list(SIMPLIFIED_TOOL_CLASSES)
        if not self.rag_enabled:
            tool_classes = [cls for cls in tool_classes if cls.__name__ != 'SearchCodebaseTool']
        if not self.kb_enabled:
            tool_classes = [cls for cls in tool_classes if cls.__name__ != 'KnowledgeBaseTool']
        active_tools = enabled_tools if enabled_tools is not None else self.enabled_tools
        if active_tools:
            tool_classes = [cls for cls in tool_classes if cls.__name__ in active_tools]
        return tool_classes

    def resolve_from_profile(self, manager) -> 'AgentConfig':
        """Resolve provider fields from the active profile.

        Uses the ``provider_id`` on this config to look up the matching profile
        via *manager* (a :class:`~agent.config.provider_profile.ProviderManager`)
        and fills in ``provider_type``, ``base_url``, ``api_key``, and
        ``model`` accordingly.

        The ``model_override`` field, if set, takes precedence over the
        profile's ``default_model``.  If ``model`` is explicitly set on this
        config it is preserved; the profile's ``default_model`` is only used
        as a fallback when neither ``model_override`` nor ``model`` is set.

        Returns a *new* ``AgentConfig`` instance (this object is unchanged).
        """
        if not self.provider_id:
            return self.model_copy(deep=True)

        profile = manager.get_profile(self.provider_id)
        if profile is None:
            return self.model_copy(deep=True)

        # Provider fields: overwrite from profile when non-empty.
        # When switching providers, non-empty profile values clear stale
        # values (e.g. base_url, api_key from the previous provider).
        # Empty profile values leave the config's values intact — this
        # prevents blanking out valid config values when the profile
        # has empty optional fields.
        updates = {}
        if profile.provider_type:
            updates['provider_type'] = profile.provider_type
        if profile.base_url:
            updates['base_url'] = profile.base_url
        if profile.api_key:
            updates['api_key'] = profile.api_key

        # Model: model_override > explicit user model > profile.default_model
        if self.model_override:
            updates['model'] = self.model_override
        elif self.model and self.model != 'deepseek-reasoner':
            # User explicitly set a non-default model — preserve it
            pass
        elif profile.default_model:
            updates['model'] = profile.default_model

        return self.model_copy(update=updates)

    model_config = ConfigDict(extra='ignore')
