"""
Configuration models for the ThoughtMachine agent.
"""
from typing import Optional, Callable, List, Any, Dict, Literal
from pydantic import BaseModel, Field, field_validator, model_validator, ConfigDict
from agent.logging import log
from tools import SIMPLIFIED_TOOL_CLASSES

class AgentConfig(BaseModel):
    """Main configuration model for the ThoughtMachine agent."""
    api_key: str = ''
    base_url: str = 'https://api.deepseek.com'
    model: str = 'deepseek-reasoner'
    provider_type: Literal['openai_compatible', 'anthropic', 'openai'] = 'openai_compatible'
    provider_config: Dict[str, Any] = Field(default_factory=dict)
    temperature: float = 0.2
    max_turns: int = 100
    stop_check: Optional[Callable[[], bool]] = None
    tool_classes: Optional[List[type]] = None
    initial_conversation: Optional[List[Dict[str, Any]]] = None
    max_history_turns: Optional[int] = None
    max_tokens: Optional[int] = None
    keep_initial_query: bool = True
    keep_system_messages: bool = True
    initial_input_tokens: int = 0
    initial_output_tokens: int = 0
    system_prompt: Optional[str] = None
    token_monitor_enabled: bool = Field(default=True, description='Enable automatic token usage warnings')
    token_monitor_warning_threshold: int = Field(default=35000, description='Token count threshold for warning (user)')
    token_monitor_critical_threshold: int = Field(default=50000, description='Token count threshold for critical warning (user)')
    turn_monitor_enabled: bool = Field(default=True, description='Enable automatic turn limit warnings')
    turn_monitor_warning_threshold: float = Field(default=0.8, description='Warning threshold as fraction of max_turns (e.g., 0.8 = 80%)')
    turn_monitor_critical_threshold: float = Field(default=0.95, description='Critical threshold as fraction of max_turns (e.g., 0.95 = 95%)')
    enable_logging: bool = Field(default=True, description='Enable agent logging')
    log_dir: str = Field(default='./logs', description='Directory for log files')
    log_level: str = Field(default='INFO', description='Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)')
    enable_file_logging: bool = Field(default=True, description='Write logs to files')
    enable_console_logging: bool = Field(default=False, description='Print logs to console')
    jsonl_format: bool = Field(default=True, description='Use JSONL format for log files')
    log_categories: List[str] = Field(default_factory=lambda: ['SESSION', 'LLM', 'TOOLS'], description='List of log categories to enable (SESSION, UI, LLM, TOOLS, SECURITY, PERFORMANCE). Can be overridden by AGENT_LOG_CATEGORIES environment variable.')
    max_file_size_mb: int = Field(default=10, description='Maximum log file size in MB before rotation')
    max_backup_files: int = Field(default=5, description='Maximum number of backup log files to keep')
    session_id: Optional[str] = Field(default=None, description='Unique session ID for logging (auto-generated if None)')
    workspace_path: Optional[str] = Field(default=None, description='Root directory for file operations (None = unrestricted)')
    use_qml_ui: bool = Field(default=False, description='Use QML-based UI instead of Qt Widgets')
    rag_enabled: bool = Field(default=False, description='Enable RAG functionality')
    rag_embedding_model: str = Field(default='BAAI/bge-small-en-v1.5', description='Model name for sentence-transformers embeddings')
    rag_vector_store_path: Optional[str] = Field(default=None, description='Path to vector store database (None = default .thoughtmachine/rag/)')
    rag_chunk_size: int = Field(default=1500, description='Size of text chunks for RAG indexing (characters)')
    rag_chunk_overlap: int = Field(default=200, description='Overlap between chunks for RAG indexing (characters)')
    rag_batch_size: int = Field(default=16, description='Batch size for embedding generation in RAG indexing')
    rag_truncate_dim: int = Field(default=256, description='Dimension to truncate embeddings to for memory efficiency')
    tool_output_token_limit: int = Field(default=10000, description='Maximum token limit for tool outputs (default 10,000 tokens)')
    detail: Literal['minimal', 'normal', 'verbose'] = Field(default='normal', description='Detail level for event display')
    enabled_tools: List[str] = Field(default_factory=lambda: [cls.__name__ for cls in SIMPLIFIED_TOOL_CLASSES], description='List of enabled tool class names')

    @field_validator('enabled_tools')
    def filter_search_codebase_tool(cls, v, info):
        """Ensure SearchCodebaseTool is only available when rag_enabled is True."""
        rag_enabled = info.data.get('rag_enabled', False)
        if not rag_enabled:
            filtered = [tool for tool in v if tool != 'SearchCodebaseTool']
            if filtered != v:
                return filtered
        return v

    @model_validator(mode='after')
    def filter_default_enabled_tools(self):
        """Filter SearchCodebaseTool from default enabled_tools when rag_enabled=False."""
        if not self.rag_enabled and self.enabled_tools:
            filtered = [tool for tool in self.enabled_tools if tool != 'SearchCodebaseTool']
            if filtered != self.enabled_tools:
                object.__setattr__(self, 'enabled_tools', filtered)
        return self

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
        active_tools = enabled_tools if enabled_tools is not None else self.enabled_tools
        if active_tools:
            tool_classes = [cls for cls in tool_classes if cls.__name__ in active_tools]
        return tool_classes

    model_config = ConfigDict(extra='ignore')