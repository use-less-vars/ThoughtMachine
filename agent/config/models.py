# agent/config/models.py
"""
Configuration models for the ThoughtMachine agent.
"""

from typing import Optional, Callable, List, Any, Dict, Literal
from pydantic import BaseModel, Field

from tools import SIMPLIFIED_TOOL_CLASSES


class AgentConfig(BaseModel):
    """Main configuration model for the ThoughtMachine agent."""
    
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-reasoner"
    provider_type: Literal["openai_compatible", "anthropic", "openai"] = "openai_compatible"
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
    system_prompt: Optional[str] = None  # Custom system prompt (overrides file)
    
    # Token monitoring configuration
    token_monitor_enabled: bool = Field(default=True, description="Enable automatic token usage warnings")
    token_monitor_warning_threshold: int = Field(default=35000, description="Token count threshold for warning (user)")
    token_monitor_critical_threshold: int = Field(default=50000, description="Token count threshold for critical warning (user)")

    # Turn monitoring configuration
    turn_monitor_enabled: bool = Field(default=True, description="Enable automatic turn limit warnings")
    turn_monitor_warning_threshold: float = Field(default=0.8, description="Warning threshold as fraction of max_turns (e.g., 0.8 = 80%)")
    turn_monitor_critical_threshold: float = Field(default=0.95, description="Critical threshold as fraction of max_turns (e.g., 0.95 = 95%)")
    critical_countdown_turns: int = Field(default=5, description="Number of turns to count down before tool restrictions apply after entering critical state")
    
    # Logging configuration
    enable_logging: bool = Field(default=True, description="Enable agent logging")
    log_dir: str = Field(default="./logs", description="Directory for log files")
    log_level: str = Field(default="INFO", description="Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)")
    enable_file_logging: bool = Field(default=True, description="Write logs to files")
    enable_console_logging: bool = Field(default=False, description="Print logs to console")
    jsonl_format: bool = Field(default=True, description="Use JSONL format for log files")
    max_file_size_mb: int = Field(default=10, description="Maximum log file size in MB before rotation")
    max_backup_files: int = Field(default=5, description="Maximum number of backup log files to keep")
    session_id: Optional[str] = Field(default=None, description="Unique session ID for logging (auto-generated if None)")
    
    # Workspace configuration for file system access restrictions
    workspace_path: Optional[str] = Field(default=None, description="Root directory for file operations (None = unrestricted)")

    # Tool output limit configuration
    tool_output_token_limit: int = Field(default=10000, description="Maximum token limit for tool outputs (default 10,000 tokens)")
    # UI detail level configuration
    detail: Literal["minimal", "normal", "verbose"] = Field(default="normal", description="Detail level for event display")

    # Enabled tools configuration
    enabled_tools: List[str] = Field(
        default_factory=lambda: [cls.__name__ for cls in SIMPLIFIED_TOOL_CLASSES], 
        description="List of enabled tool class names"
    )

    class Config:
        extra = "ignore"  # Allow backward compatibility with older configs