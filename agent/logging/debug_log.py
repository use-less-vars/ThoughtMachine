"""Debug logging utilities for the agent."""
import os
import datetime
from pathlib import Path
import logging
from logging.handlers import RotatingFileHandler

# DEBUG_ENABLED determined at runtime from environment variable
DEBUG_LOG_PATH = Path("debug_agent.log")
DEBUG_TRUNCATION_LIMIT = int(os.environ.get('THOUGHTMACHINE_DEBUG_TRUNCATION', 100))

# Module-level logger instance
_logger = None


def is_debug_enabled(component: str = "") -> bool:
    """Check if debug output is enabled for a component.

    Returns True if THOUGHTMACHINE_DEBUG=1 or DEBUG_{component} is truthy.
    """
    if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
        return True
    if component:
        # Check component-specific flag (any non-empty value)
        flag_value = os.environ.get(f'DEBUG_{component.upper()}')
        if flag_value is not None and flag_value != '':
            return True
    return False


class DebugLogFormatter(logging.Formatter):
    """Custom formatter that matches the original debug log timestamp format."""
    def formatTime(self, record, datefmt=None):
        # Create timestamp matching original format: HH:MM:SS.mmm (milliseconds)
        dt = datetime.datetime.fromtimestamp(record.created)
        # Format with milliseconds (3 digits)
        return dt.strftime("%H:%M:%S.") + f"{int(dt.microsecond / 1000):03d}"


def _get_logger():
    """Get or create the rotating file logger."""
    global _logger
    if _logger is None:
        _logger = logging.getLogger("debug_log")
        _logger.setLevel(logging.DEBUG)
        # Remove any existing handlers to avoid duplicates
        _logger.handlers.clear()
        
        handler = RotatingFileHandler(
            str(DEBUG_LOG_PATH),
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=3,
            encoding="utf-8"
        )
        # Custom formatter to match original format: [{timestamp}][component][level] message
        formatter = DebugLogFormatter('[%(asctime)s]%(message)s')
        handler.setFormatter(formatter)
        _logger.addHandler(handler)
        # Prevent propagation to root logger (avoid double logging)
        _logger.propagate = False
    return _logger


def debug_log(msg: str, level: str = "DEBUG", component: str = "") -> None:
    """Log message to file (and optionally to console based on level and THOUGHTMACHINE_DEBUG).

    Levels: DEBUG, INFO, WARNING, ERROR
    When THOUGHTMACHINE_DEBUG=1: all levels go to console
    When THOUGHTMACHINE_DEBUG=0: only WARNING and ERROR go to console
    Component-specific debug flags (DEBUG_{component}) also enable console output.
    """
    # Convert message to string for processing
    msg_str = str(msg)

    # Truncate for console output if limit is set (>0)
    console_msg = msg_str
    if DEBUG_TRUNCATION_LIMIT > 0 and len(msg_str) > DEBUG_TRUNCATION_LIMIT:
        console_msg = msg_str[:DEBUG_TRUNCATION_LIMIT] + "..."

    # Map level string to logging level and numeric value
    level_map = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR
    }
    log_level = level_map.get(level.upper(), logging.DEBUG)
    
    # Prepare log record components
    component_prefix = f"[{component}]" if component else ""
    level_prefix = f"[{level}]"
    # Build message for file logging (includes component and level prefixes in message body)
    log_message = f"{component_prefix}{level_prefix} {msg_str}"
    
    # Write to file using rotating logger
    try:
        logger = _get_logger()
        # Use the logger with appropriate level
        logger.log(log_level, log_message)
    except Exception as e:
        # If file writing fails, just print to console
        print(f"[DEBUG_LOG ERROR] Failed to write to log file: {e}")

    # Console output based on level and debug flags (THOUGHTMACHINE_DEBUG or DEBUG_{component})
    debug_enabled = is_debug_enabled(component)
    if debug_enabled:
        # In debug mode, print everything (truncated)
        print(f"{component_prefix}{level_prefix} {console_msg}")
    elif level in ("WARNING", "ERROR"):
        # Always print warnings and errors even without debug mode (truncated)
        print(f"{component_prefix}{level_prefix} {console_msg}")