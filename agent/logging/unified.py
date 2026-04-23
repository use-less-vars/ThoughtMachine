"""
Unified logging facade for ThoughtMachine.

This module provides a single logging interface that consolidates:
- AgentLogger (JSONL file logging with session tracking)
- debug_log (component-based debug logging with console output)

Environment variables control behavior:
    TM_LOG_LEVEL: Minimum log level for console output (default: INFO)
    TM_LOG_TAGS: Comma-separated list of tags to show (default: empty = show WARNING+)
    TM_TOOL_ARGUMENTS_TRUNCATE: Max length for tool argument JSON strings (default: 100)
    TM_TOOL_RESULT_TRUNCATE: Max length for tool result strings (default: 100)
    TM_RAW_RESPONSE_TRUNCATE: Max length for raw LLM response strings (default: 100)
    TM_CONSOLE_DATA_TRUNCATE: Max length for console data dict strings (default: 200)
    TM_CONVERSATION_CONTENT_TRUNCATE: Max length for conversation content (default: 10000)
    TM_DOCKER_OUTPUT_TRUNCATE: Max length for Docker output (default: 10000)
    TM_DEBUG_TRUNCATE_LENGTH: Global fallback truncation length (default: 100)

Tag format: area.component (e.g., core.session, tools.file_editor)
Use '*' to show all tags in DEBUG/INFO level.

Console output filtering logic:
    If TM_LOG_TAGS is empty (default):
        Show WARNING, ERROR, CRITICAL regardless of tag
    If TM_LOG_TAGS is set:
        Show only logs where tag matches (supports wildcard '*' for all tags)
        Level must be >= TM_LOG_LEVEL

All logs are always written to JSONL file (AgentLogger) regardless of console filtering.
"""

import os
import sys
from enum import Enum
from typing import Optional, Dict, Any, List, Union
import json

# AgentLogger imports are done lazily in _get_logger() to avoid circular imports
# with agent/logging/__init__.py which imports from this module.
_agent_logger_classes = None


def _get_agent_logger_classes():
    """Lazily import agent logging classes to avoid circular imports."""
    global _agent_logger_classes
    if _agent_logger_classes is not None:
        return _agent_logger_classes if _agent_logger_classes else None
    try:
        from agent.logging import AgentLogger, LogLevel as AgentLogLevel, LogCategory, LogEventType
        from agent.config import AgentConfig
        _agent_logger_classes = {
            'AgentLogger': AgentLogger,
            'AgentLogLevel': AgentLogLevel,
            'LogCategory': LogCategory,
            'LogEventType': LogEventType,
            'AgentConfig': AgentConfig,
        }
        return _agent_logger_classes
    except ImportError:
        _agent_logger_classes = False
        return None


class LogLevel(Enum):
    """Log levels for unified facade."""
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


# Environment variable defaults
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_TRUNCATE_LENGTH = 100
DEFAULT_CONSOLE_DATA_TRUNCATE = 200
DEFAULT_CONVERSATION_CONTENT_TRUNCATE = 10000
DEFAULT_DOCKER_OUTPUT_TRUNCATE = 10000


def _get_env_int(name: str, default: int) -> int:
    """Get integer from environment variable."""
    value = os.environ.get(name)
    if value is not None:
        try:
            return int(value)
        except ValueError:
            pass
    return default


def _get_env_str(name: str, default: str) -> str:
    """Get string from environment variable."""
    return os.environ.get(name, default)


# Load environment variables
_LOG_LEVEL = _get_env_str("TM_LOG_LEVEL", DEFAULT_LOG_LEVEL)
_LOG_TAGS_STR = _get_env_str("TM_LOG_TAGS", "")

# Backward compatibility with THOUGHTMACHINE_DEBUG
if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
    _LOG_LEVEL = "DEBUG"
    if not _LOG_TAGS_STR:
        _LOG_TAGS_STR = "*"  # Show all tags
    # Also set truncation length from old variable
    old_trunc = os.environ.get('THOUGHTMACHINE_DEBUG_TRUNCATION')
    if old_trunc:
        os.environ['TM_DEBUG_TRUNCATE_LENGTH'] = old_trunc

# Parse log level
try:
    CURRENT_LOG_LEVEL = LogLevel(_LOG_LEVEL.upper())
except ValueError:
    CURRENT_LOG_LEVEL = LogLevel.INFO

# Parse tags
_LOG_TAGS: List[str] = []
if _LOG_TAGS_STR:
    _LOG_TAGS = [tag.strip() for tag in _LOG_TAGS_STR.split(",") if tag.strip()]

# Load truncation lengths
TM_TOOL_ARGUMENTS_TRUNCATE = _get_env_int("TM_TOOL_ARGUMENTS_TRUNCATE", DEFAULT_TRUNCATE_LENGTH)
TM_TOOL_RESULT_TRUNCATE = _get_env_int("TM_TOOL_RESULT_TRUNCATE", DEFAULT_TRUNCATE_LENGTH)
TM_RAW_RESPONSE_TRUNCATE = _get_env_int("TM_RAW_RESPONSE_TRUNCATE", DEFAULT_TRUNCATE_LENGTH)
TM_CONSOLE_DATA_TRUNCATE = _get_env_int("TM_CONSOLE_DATA_TRUNCATE", DEFAULT_CONSOLE_DATA_TRUNCATE)
TM_CONVERSATION_CONTENT_TRUNCATE = _get_env_int("TM_CONVERSATION_CONTENT_TRUNCATE", DEFAULT_CONVERSATION_CONTENT_TRUNCATE)
TM_DOCKER_OUTPUT_TRUNCATE = _get_env_int("TM_DOCKER_OUTPUT_TRUNCATE", DEFAULT_DOCKER_OUTPUT_TRUNCATE)
TM_DEBUG_TRUNCATE_LENGTH = _get_env_int("TM_DEBUG_TRUNCATE_LENGTH", DEFAULT_TRUNCATE_LENGTH)


def get_limit_for_hint(truncate_hint: Optional[str]) -> int:
    """
    Return the appropriate truncation limit for a given hint.
    
    If truncate_hint is None (generic debug), use TM_DEBUG_TRUNCATE_LENGTH.
    For structured data hints, return the corresponding type-specific limit.
    """
    if truncate_hint is None:
        return TM_DEBUG_TRUNCATE_LENGTH
    
    mapping = {
        "tool_arguments": TM_TOOL_ARGUMENTS_TRUNCATE,
        "tool_result": TM_TOOL_RESULT_TRUNCATE,
        "raw_response": TM_RAW_RESPONSE_TRUNCATE,
        "console_data": TM_CONSOLE_DATA_TRUNCATE,
        "conversation_content": TM_CONVERSATION_CONTENT_TRUNCATE,
        "docker_output": TM_DOCKER_OUTPUT_TRUNCATE,
    }
    return mapping.get(truncate_hint, TM_DEBUG_TRUNCATE_LENGTH)


# Global fallback truncation
DEBUG_TRUNCATE_LENGTH = _get_env_int("DEBUG_TRUNCATE_LENGTH", DEFAULT_TRUNCATE_LENGTH)


def _tag_matches(tag: str, pattern: str) -> bool:
    """
    Check if tag matches pattern.
    Supports exact match and wildcard '*' at end of pattern.
    """
    if not pattern or not tag:
        return False
    if pattern == "*":
        return True
    if pattern.endswith("*"):
        prefix = pattern[:-1]
        return tag.startswith(prefix)
    return tag == pattern


def _is_component_debug_enabled(tag: str) -> bool:
    """
    Check if debug is enabled for a tag via component-specific environment variables.
    
    Supports:
    - DEBUG_{COMPONENT}=1 (where component is last part of tag after dot, uppercase)
    - DEBUG_{AREA}_{COMPONENT}=1 (where area is first part, component last part)
    - THOUGHTMACHINE_DEBUG=1 (already handled globally)
    
    Also handles underscore variations (e.g., DEBUG_EVENTBUS matches component 'event_bus').
    """
    if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
        return True
    
    # Extract component name from tag (e.g., core.event_bus -> event_bus)
    parts = tag.split('.')
    if len(parts) >= 2:
        component = parts[-1]
        area = parts[0]
        
        # Generate component variants for underscore handling
        component_no_underscore = component.replace('_', '')
        
        # Check DEBUG_{COMPONENT} (with underscores as in tag)
        env_var = f'DEBUG_{component.upper()}'
        if os.environ.get(env_var) == '1':
            return True
        
        # Check DEBUG_{COMPONENT_NO_UNDERSCORE} (without underscores)
        if component_no_underscore:
            env_var_no_us = f'DEBUG_{component_no_underscore.upper()}'
            if os.environ.get(env_var_no_us) == '1':
                return True
        
        # Check DEBUG_{AREA}_{COMPONENT}
        env_var2 = f'DEBUG_{area.upper()}_{component.upper()}'
        if os.environ.get(env_var2) == '1':
            return True
        
        # Check DEBUG_{AREA}_{COMPONENT_NO_UNDERSCORE}
        if component_no_underscore:
            env_var2_no_us = f'DEBUG_{area.upper()}_{component_no_underscore.upper()}'
            if os.environ.get(env_var2_no_us) == '1':
                return True
    
    # Also check DEBUG_{TAG_WITH_UNDERSCORES} (e.g., DEBUG_CORE_EVENT_BUS)
    tag_underscore = tag.replace('.', '_')
    env_var_tag = f'DEBUG_{tag_underscore.upper()}'
    if os.environ.get(env_var_tag) == '1':
        return True
        
    return False

def _should_log_to_console(level: LogLevel, tag: str) -> bool:
    """
    Determine if a log should be printed to console.
    
    Logic:
    1. If TM_LOG_TAGS is empty (default):
        - Show WARNING, ERROR, CRITICAL regardless of tag
        - Don't show DEBUG, INFO (unless TM_LOG_LEVEL is DEBUG/INFO? Actually default shows WARNING+)
    2. If TM_LOG_TAGS is set:
        - Show only logs where tag matches one of the patterns
        - Level must be >= TM_LOG_LEVEL
    """
    # Component-specific debug override
    if _is_component_debug_enabled(tag):
        return True

    # Convert level to priority
    level_priority = {
        LogLevel.DEBUG: 10,
        LogLevel.INFO: 20,
        LogLevel.WARNING: 30,
        LogLevel.ERROR: 40,
        LogLevel.CRITICAL: 50,
    }
    current_priority = level_priority.get(CURRENT_LOG_LEVEL, 20)
    msg_priority = level_priority.get(level, 20)
    
    # If no tags specified, default behavior: show WARNING+
    if not _LOG_TAGS:
        return msg_priority >= level_priority[LogLevel.WARNING]
    
    # Tags specified: check if tag matches any pattern
    tag_matched = False
    for pattern in _LOG_TAGS:
        if _tag_matches(tag, pattern):
            tag_matched = True
            break
    
    if not tag_matched:
        return False
    
    # Tag matches, now check level
    return msg_priority >= current_priority


# Color codes for console output
COLORS = {
    LogLevel.DEBUG: "\033[36m",     # Cyan
    LogLevel.INFO: "\033[32m",      # Green
    LogLevel.WARNING: "\033[33m",   # Yellow
    LogLevel.ERROR: "\033[31m",     # Red
    LogLevel.CRITICAL: "\033[41m",  # Red background
}
RESET = "\033[0m"


def _truncate_string(text: str, limit: int) -> str:
    """Truncate string if longer than limit."""
    if limit <= 0:
        return text
    if len(text) > limit:
        return text[:limit] + "... [truncated]"
    return text


def _truncate_data(data: Any, truncate_hint: Optional[str] = None) -> Any:
    """
    Apply early truncation to data based on truncate_hint.
    
    truncate_hint can be:
        - "tool_arguments": truncate JSON representation
        - "tool_result": truncate string representation
        - "raw_response": truncate string representation
        - "console_data": truncate for console output
        - "conversation_content": truncate string
        - "docker_output": truncate string
        - None: no truncation
    
    Returns truncated data (could be dict with __truncated__ flag for JSON).
    """
    if data is None or truncate_hint is None:
        return data
    
    limit = None
    if truncate_hint == "tool_arguments":
        limit = TM_TOOL_ARGUMENTS_TRUNCATE
    elif truncate_hint == "tool_result":
        limit = TM_TOOL_RESULT_TRUNCATE
    elif truncate_hint == "raw_response":
        limit = TM_RAW_RESPONSE_TRUNCATE
    elif truncate_hint == "console_data":
        limit = TM_CONSOLE_DATA_TRUNCATE
    elif truncate_hint == "conversation_content":
        limit = TM_CONVERSATION_CONTENT_TRUNCATE
    elif truncate_hint == "docker_output":
        limit = TM_DOCKER_OUTPUT_TRUNCATE
    
    if limit is None or limit <= 0:
        return data
    
    # Special handling for tool arguments (JSON)
    if truncate_hint == "tool_arguments" and isinstance(data, dict):
        try:
            args_str = json.dumps(data, default=str)
            if len(args_str) > limit:
                return {
                    "__truncated__": True,
                    "original_length": len(args_str),
                    "preview": args_str[:limit]
                }
        except Exception:
            # Fall back to string truncation
            pass
    
    # For strings, truncate directly
    if isinstance(data, str):
        return _truncate_string(data, limit)
    
    # For other types, convert to string and truncate
    return _truncate_string(str(data), limit)


# Singleton AgentLogger instance
_logger_instance = None


def _get_logger() -> Optional[object]:
    """Get or create singleton AgentLogger instance."""
    global _logger_instance
    if _logger_instance is not None:
        return _logger_instance
    
    # Lazy import to avoid circular import with agent/logging/__init__.py
    classes = _get_agent_logger_classes()
    if not classes:
        return None
    AgentLogger = classes['AgentLogger']    
    # Create minimal config for AgentLogger
    # This is a temporary solution until Phase 2 refactoring
    class MinimalConfig:
        log_categories = ["SESSION", "UI", "LLM", "TOOLS", "SECURITY", "PERFORMANCE"]
        log_level = "INFO"
        enable_logging = True
        log_dir = "./logs"
        enable_file_logging = True
        enable_console_logging = False  # We handle console output ourselves
        jsonl_format = True
        max_file_size_mb = 10
        max_backup_files = 5
        session_id = None
    
    try:
        config = MinimalConfig()
        _logger_instance = AgentLogger(
            config=config,
            log_dir=config.log_dir,
            log_level=config.log_level,
            enable_file_logging=config.enable_file_logging,
            enable_console_logging=config.enable_console_logging,
            jsonl_format=config.jsonl_format,
            max_file_size_mb=config.max_file_size_mb,
            max_backup_files=config.max_backup_files,
            session_id=config.session_id
        )
    except Exception as e:
        print(f"[LOGGING ERROR] Failed to create AgentLogger: {e}", file=sys.stderr)
        _logger_instance = None
    
    return _logger_instance


def log(
    level: Union[str, LogLevel],
    tag: str,
    message: str,
    data: Optional[Dict[str, Any]] = None,
    event_type: Optional[Any] = None,
    truncate_hint: Optional[str] = None
) -> None:
    """
    Unified logging function.
    
    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        tag: Tag for filtering (format: area.component)
        message: Human-readable message
        data: Structured data (will be truncated based on truncate_hint)
        event_type: Optional LogEventType for structured logging (default: None)
        truncate_hint: Hint for truncation type (see _truncate_data)
    """
    # Convert string level to LogLevel enum
    if isinstance(level, str):
        try:
            level_enum = LogLevel(level.upper())
        except ValueError:
            level_enum = LogLevel.INFO
    else:
        level_enum = level
    
    # Validate level
    if level_enum not in LogLevel:
        level_enum = LogLevel.INFO
    
    # Apply early truncation to data
    truncated_data = None
    if data is not None:
        truncated_data = _truncate_data(data, truncate_hint)
    
    # Console output
    if _should_log_to_console(level_enum, tag):
        # Format console line
        color = COLORS.get(level_enum, "")
        level_str = level_enum.value
        tag_str = f"[{tag}]" if tag else ""
        
        # Build message
        console_msg = f"{color}{level_str:8s} {tag_str} {message}{RESET}"
        if truncated_data:
            # Format data for console (truncate further if needed)
            data_str = str(truncated_data)
            console_limit = get_limit_for_hint(truncate_hint)
            if len(data_str) > console_limit:
                data_str = _truncate_string(data_str, console_limit)
            console_msg += f" | {data_str}"
        
        print(console_msg, file=sys.stderr)

    # Forward to AgentLogger (if available)
    logger = _get_logger()
    if logger is not None:
        # Map to AgentLogger's internal methods
        # This is a simplified forwarding - will be improved in Phase 2
        try:
            # Lazily import AgentLogLevel and LogEventType
            classes = _get_agent_logger_classes()
            AgentLogLevel = classes.get('AgentLogLevel') if classes else None
            LogEventType = classes.get('LogEventType') if classes else None

            # Convert our LogLevel to AgentLogger's LogLevel if available
            if AgentLogLevel is not None:
                try:
                    agent_level = AgentLogLevel(level_enum.value)
                except ValueError:
                    agent_level = AgentLogLevel.INFO
            else:
                agent_level = level_enum

            # Use appropriate LogEventType if available
            log_event_type = event_type
            if log_event_type is None:
                log_event_type = LogEventType.TOOL_DEBUG if LogEventType else None

            logger._log_event(
                event_type=log_event_type,
                level=agent_level,
                message=f"[{tag}] {message}",
                data=truncated_data,
                turn=logger.current_turn if hasattr(logger, 'current_turn') else 0
            )
        except Exception as e:
            print(f"[LOGGING ERROR] Failed to forward to AgentLogger: {e}", file=sys.stderr)


# Convenience functions
def debug(tag: str, message: str, data: Optional[Dict[str, Any]] = None, event_type: Optional[Any] = None, truncate_hint: Optional[str] = None) -> None:
    """Log at DEBUG level."""
    log(LogLevel.DEBUG, tag, message, data, event_type, truncate_hint)

def info(tag: str, message: str, data: Optional[Dict[str, Any]] = None, event_type: Optional[Any] = None, truncate_hint: Optional[str] = None) -> None:
    """Log at INFO level."""
    log(LogLevel.INFO, tag, message, data, event_type, truncate_hint)

def warning(tag: str, message: str, data: Optional[Dict[str, Any]] = None, event_type: Optional[Any] = None, truncate_hint: Optional[str] = None) -> None:
    """Log at WARNING level."""
    log(LogLevel.WARNING, tag, message, data, event_type, truncate_hint)

def error(tag: str, message: str, data: Optional[Dict[str, Any]] = None, event_type: Optional[Any] = None, truncate_hint: Optional[str] = None) -> None:
    """Log at ERROR level."""
    log(LogLevel.ERROR, tag, message, data, event_type, truncate_hint)

def critical(tag: str, message: str, data: Optional[Dict[str, Any]] = None, event_type: Optional[Any] = None, truncate_hint: Optional[str] = None) -> None:
    """Log at CRITICAL level."""
    log(LogLevel.CRITICAL, tag, message, data, event_type, truncate_hint)