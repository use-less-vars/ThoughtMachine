# agent_core.py
import json
import logging
import os
from typing import Optional, Callable, List, Any, Dict
from openai import OpenAI
from pydantic import BaseModel, ValidationError, Field

from tools import TOOL_CLASSES, SIMPLIFIED_TOOL_CLASSES
from tools.base import ToolBase
from tools.final import Final
from tools.request_user_interaction import RequestUserInteraction
from tools.utils import model_to_openai_tool
from fast_json_repair import loads as repair_loads


# Import logging module
try:
    from agent_logging import create_logger, AgentLogger, LogEventType, LogLevel
    LOGGING_AVAILABLE = True
except ImportError:
    LOGGING_AVAILABLE = False
    create_logger = None
    AgentLogger = None
    LogEventType = None
    LogLevel = None 

class AgentConfig(BaseModel):
    api_key: str
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-reasoner"
    temperature: float = 0.2
    max_turns: int = 30
    stop_check: Optional[Callable[[], bool]] = None
    tool_classes: Optional[List[type]] = None   #
    initial_conversation: Optional[List[Dict[str, Any]]] = None
    max_history_turns: Optional[int] = None
    max_tokens: Optional[int] = None
    keep_initial_query: bool = True
    keep_system_messages: bool = True
    initial_input_tokens: int = 0
    initial_output_tokens: int = 0
    
    # Token monitoring configuration
    token_monitor_enabled: bool = Field(default=True, description="Enable automatic token usage warnings")
    token_monitor_warning_threshold: int = Field(default=35000, description="Token count threshold for warning (user)")
    token_monitor_critical_threshold: int = Field(default=50000, description="Token count threshold for critical warning (user)")
    
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
    
    class Config:
        extra = "ignore"  # Allow backward compatibility with older configs
def run_agent_stream(query: str, config: AgentConfig):
    """
    Backward compatibility wrapper that creates an Agent instance and processes the query.
    """
    from agent import Agent
    agent = Agent(config, initial_conversation=config.initial_conversation)
    yield from agent.process_query(query)
    
