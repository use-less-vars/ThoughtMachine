# agent_logging.py
"""
Logging system for the ThoughtMachine agent.
Records verbatim conversation between agent and LLM for analysis and replay.
"""
import json
import logging as python_logging
import os
import threading
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Union, TYPE_CHECKING
from queue import Queue, Empty

if TYPE_CHECKING:
    from agent_core import AgentConfig


# Session file version
CURRENT_SESSION_VERSION = "1.0"


class LogLevel(Enum):
    """Logging verbosity levels."""
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class LogEventType(Enum):
    """Types of events that can be logged."""
    # Agent lifecycle
    AGENT_START = "agent_start"
    AGENT_END = "agent_end"
    
    # LLM interaction
    LLM_REQUEST = "llm_request"
    LLM_RESPONSE = "llm_response"
    RAW_RESPONSE = "raw_response"
    
    # Tool execution
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    
    # Conversation management
    CONVERSATION_UPDATE = "conversation_update"
    CONVERSATION_PRUNE = "conversation_prune"
    TOKEN_WARNING = "token_warning"
    TURN_WARNING = "turn_warning"
    
    # Turn lifecycle
    TURN_START = "turn_start"
    TURN_COMPLETE = "turn_complete"
    
    # Control flow
    USER_INTERACTION_REQUESTED = "user_interaction_requested"
    FINAL_DETECTED = "final_detected"
    STOP_SIGNAL = "stop_signal"
    MAX_TURNS_REACHED = "max_turns_reached"
    EXECUTION_STATE_CHANGE = "execution_state_change"
    SESSION_STATE_CHANGE = "session_state_change"
    ERROR = "error"


class AgentLogger:
    """
    Main logging class for the agent.
    
    Supports multiple output formats and log levels.
    Thread-safe for use in the agent's background thread.
    """
    
    def __init__(
        self,
        config: 'AgentConfig',
        log_dir: str = "./logs",
        log_level: Union[str, LogLevel] = LogLevel.INFO,
        enable_file_logging: bool = True,
        enable_console_logging: bool = False,
        jsonl_format: bool = True,
        max_file_size_mb: int = 10,
        max_backup_files: int = 5,
        session_id: Optional[str] = None,
    ):
        """
        Initialize the logger.
        
        Args:
            config: AgentConfig instance
            log_dir: Directory to store log files
            log_level: Minimum log level to record
            enable_file_logging: Whether to write logs to file
            enable_console_logging: Whether to print logs to console
            jsonl_format: Whether to use JSONL format for file logging
            max_file_size_mb: Maximum log file size in MB before rotation
            max_backup_files: Maximum number of backup files to keep
            session_id: Unique identifier for this agent session (auto-generated if None)
        """
        self.config = config
        self.log_dir = os.path.abspath(log_dir)
        self.log_level = LogLevel(log_level) if isinstance(log_level, str) else log_level
        self.enable_file_logging = enable_file_logging
        self.enable_console_logging = enable_console_logging
        self.jsonl_format = jsonl_format
        self.max_file_size_bytes = max_file_size_mb * 1024 * 1024
        self.max_backup_files = max_backup_files
        
        # Generate session ID if not provided
        self.session_id = session_id or datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        
        # Thread safety
        self._lock = threading.RLock()
        self._file_handle = None
        self._current_file_size = 0
        
        # Setup log directory
        os.makedirs(self.log_dir, exist_ok=True)
        
        # Current log file path
        self.log_file_path = os.path.join(
            self.log_dir, 
            f"agent_{self.session_id}.{'jsonl' if jsonl_format else 'log'}"
        )
        
        # Python logging integration
        self.py_logger = python_logging.getLogger(f"agent_{self.session_id}")
        self.py_logger.setLevel(self._to_python_log_level(self.log_level))
        
        # Initialize
        self._initialize_logging()
        
        # Session metadata
        self.session_start_time = datetime.now()
        self.current_turn = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        
    def _to_python_log_level(self, level: LogLevel) -> int:
        """Convert LogLevel to Python logging level."""
        level_map = {
            LogLevel.DEBUG: python_logging.DEBUG,
            LogLevel.INFO: python_logging.INFO,
            LogLevel.WARNING: python_logging.WARNING,
            LogLevel.ERROR: python_logging.ERROR,
            LogLevel.CRITICAL: python_logging.CRITICAL,
        }
        return level_map.get(level, python_logging.INFO)
    
    def _initialize_logging(self):
        """Initialize logging handlers."""
        # Clear any existing handlers
        self.py_logger.handlers.clear()
        
        # Console handler if enabled
        if self.enable_console_logging:
            console_handler = python_logging.StreamHandler()
            console_formatter = python_logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            console_handler.setFormatter(console_formatter)
            self.py_logger.addHandler(console_handler)
        
        # File handler if enabled
        if self.enable_file_logging:
            try:
                self._file_handle = open(self.log_file_path, 'a', encoding='utf-8')
                self._current_file_size = os.path.getsize(self.log_file_path) if os.path.exists(self.log_file_path) else 0
            except Exception as e:
                print(f"Failed to open log file {self.log_file_path}: {e}")
                self.enable_file_logging = False
    
    def _should_log(self, level: LogLevel) -> bool:
        """Check if a message at given level should be logged."""
        level_priority = {
            LogLevel.DEBUG: 10,
            LogLevel.INFO: 20,
            LogLevel.WARNING: 30,
            LogLevel.ERROR: 40,
            LogLevel.CRITICAL: 50,
        }
        current_priority = level_priority.get(self.log_level, 20)
        msg_priority = level_priority.get(level, 20)
        return msg_priority >= current_priority
    
    def _write_jsonl(self, event: Dict[str, Any]):
        """Write a JSONL entry to the log file."""
        if not self.enable_file_logging or not self._file_handle:
            return
        
        with self._lock:
            try:
                # Add common metadata
                event["timestamp"] = datetime.now().isoformat()
                event["session_id"] = self.session_id
                event["version"] = CURRENT_SESSION_VERSION
                
                # Write as JSON line
                json_line = json.dumps(event, ensure_ascii=False) + "\n"
                self._file_handle.write(json_line)
                self._file_handle.flush()
                
                # Update file size
                self._current_file_size += len(json_line.encode('utf-8'))
                
                # Rotate if needed
                if self._current_file_size >= self.max_file_size_bytes:
                    self._rotate_log_file()
                    
            except Exception as e:
                print(f"Failed to write log entry: {e}")
    
    def _rotate_log_file(self):
        """Rotate log file when it reaches maximum size."""
        if not self.enable_file_logging or not self._file_handle:
            return
        
        with self._lock:
            try:
                self._file_handle.close()
                
                # Create backup
                for i in range(self.max_backup_files - 1, 0, -1):
                    old_file = f"{self.log_file_path}.{i}"
                    new_file = f"{self.log_file_path}.{i + 1}"
                    if os.path.exists(old_file):
                        os.rename(old_file, new_file)
                
                # Move current to .1
                if os.path.exists(self.log_file_path):
                    os.rename(self.log_file_path, f"{self.log_file_path}.1")
                
                # Open new file
                self._file_handle = open(self.log_file_path, 'a', encoding='utf-8')
                self._current_file_size = 0
                
            except Exception as e:
                print(f"Failed to rotate log file: {e}")
                # Try to reopen original file
                try:
                    self._file_handle = open(self.log_file_path, 'a', encoding='utf-8')
                except:
                    self.enable_file_logging = False
    
    def _log_event(
        self,
        event_type: LogEventType,
        level: LogLevel,
        message: str = "",
        data: Optional[Dict[str, Any]] = None,
        turn: Optional[int] = None,
    ):
        """
        Internal method to log an event.
        
        Args:
            event_type: Type of event
            level: Log level
            message: Human-readable message
            data: Structured data for the event
            turn: Current turn number (if applicable)
        """
        if not self._should_log(level):
            return
        
        # Build event dictionary
        event = {
            "type": event_type.value,
            "level": level.value,
            "message": message,
            "data": data or {},
            "turn": turn if turn is not None else self.current_turn,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
        }
        
        # Write to file in JSONL format
        if self.enable_file_logging and self.jsonl_format:
            self._write_jsonl(event)
        
        # Also use Python logging for console output
        log_method = getattr(self.py_logger, level.value.lower())
        log_msg = f"[{event_type.value}] {message}"
        if data:
            # Truncate large data for console output
            data_str = str(data)
            if len(data_str) > 200:
                data_str = data_str[:200] + "..."
            log_msg += f" | Data: {data_str}"
        log_method(log_msg)
    
    # Public logging methods for specific event types
    
    def log_agent_start(self, query: str, config_data: Dict[str, Any]):
        """Log agent startup."""
        self._log_event(
            LogEventType.AGENT_START,
            LogLevel.INFO,
            "Agent started",
            {
                "query": query,
                "config": config_data,
                "session_start_time": self.session_start_time.isoformat(),
                "log_file": self.log_file_path,
            }
        )
    
    def log_agent_end(self, end_type: str, reason: str = "", final_content: Optional[str] = None):
        """Log agent completion."""
        session_duration = (datetime.now() - self.session_start_time).total_seconds()
        self._log_event(
            LogEventType.AGENT_END,
            LogLevel.INFO,
            f"Agent ended: {end_type} - {reason}",
            {
                "end_type": end_type,
                "reason": reason,
                "final_content": final_content,
                "session_duration_seconds": session_duration,
                "total_turns": self.current_turn,
                "total_input_tokens": self.total_input_tokens,
                "total_output_tokens": self.total_output_tokens,
            }
        )
    
    def log_turn_start(self, turn: int):
        """Log start of a new turn."""
        self.current_turn = turn
        self._log_event(
            LogEventType.TURN_START,
            LogLevel.DEBUG,
            f"Starting turn {turn}",
            {"turn": turn}
        )
    
    def log_turn_complete(self, turn: int, usage: Dict[str, int]):
        """Log completion of a turn."""
        self.total_input_tokens += usage.get("input", 0)
        self.total_output_tokens += usage.get("output", 0)
        self._log_event(
            LogEventType.TURN_COMPLETE,
            LogLevel.DEBUG,
            f"Turn {turn} completed",
            {
                "turn": turn,
                "turn_input_tokens": usage.get("input", 0),
                "turn_output_tokens": usage.get("output", 0),
                "cumulative_input_tokens": self.total_input_tokens,
                "cumulative_output_tokens": self.total_output_tokens,
            }
        )
    
    def log_llm_request(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]):
        """Log LLM request being sent."""
        # Sanitize large data for logging
        sanitized_messages = []
        for msg in messages:
            sanitized_msg = msg.copy()
            if "content" in sanitized_msg and len(sanitized_msg["content"]) > 1000:
                sanitized_msg["content"] = sanitized_msg["content"][:1000] + "... [truncated]"
            sanitized_messages.append(sanitized_msg)
        
        # Count tools but don't include full schemas
        tool_names = [tool.get("function", {}).get("name", "unknown") for tool in tools]
        
        self._log_event(
            LogEventType.LLM_REQUEST,
            LogLevel.DEBUG,
            f"Sending LLM request with {len(messages)} messages and {len(tools)} tools",
            {
                "message_count": len(messages),
                "tool_count": len(tools),
                "tool_names": tool_names,
                "messages": sanitized_messages,
                "full_messages": messages if self.log_level == LogLevel.DEBUG else None,
                "full_tools": tools if self.log_level == LogLevel.DEBUG else None,
            },
            self.current_turn
        )
    
    def log_llm_response(
        self,
        content: str,
        reasoning: Optional[str],
        tool_calls: Optional[List[Dict[str, Any]]],
        usage: Dict[str, int],
        raw_response: Any = None
    ):
        """Log LLM response received."""
        self._log_event(
            LogEventType.LLM_RESPONSE,
            LogLevel.DEBUG,
            f"Received LLM response with {len(tool_calls) if tool_calls else 0} tool calls",
            {
                "content": content,
                "reasoning": reasoning,
                "tool_call_count": len(tool_calls) if tool_calls else 0,
                "tool_calls": tool_calls,
                "usage": usage,
            },
            self.current_turn
        )
        
        # Also log raw response for debugging if provided
        if raw_response is not None:
            self.log_raw_response(raw_response)
    
    def log_raw_response(self, raw_response: Any):
        """Log raw LLM response for debugging."""
        # Convert raw response to string representation
        raw_str = str(raw_response)
        # Truncate very large responses
        if len(raw_str) > 5000:
            raw_str = raw_str[:5000] + "... [truncated]"
        
        self._log_event(
            LogEventType.RAW_RESPONSE,
            LogLevel.DEBUG,
            "Raw LLM response for debugging",
            {
                "raw_response": raw_str,
                "raw_response_type": type(raw_response).__name__,
            },
            self.current_turn
        )
    
    def log_tool_call(self, tool_name: str, arguments: Dict[str, Any], tool_call_id: str):
        """Log tool execution."""
        self._log_event(
            LogEventType.TOOL_CALL,
            LogLevel.INFO,
            f"Executing tool: {tool_name}",
            {
                "tool_name": tool_name,
                "arguments": arguments,
                "tool_call_id": tool_call_id,
            },
            self.current_turn
        )
    
    def log_tool_result(self, tool_name: str, result: Any, tool_call_id: str):
        """Log tool result."""
        # Truncate large results
        result_str = str(result)
        if len(result_str) > 2000:
            result_str = result_str[:2000] + "... [truncated]"
        
        self._log_event(
            LogEventType.TOOL_RESULT,
            LogLevel.INFO,
            f"Tool {tool_name} completed",
            {
                "tool_name": tool_name,
                "result": result_str,
                "result_type": type(result).__name__,
                "tool_call_id": tool_call_id,
            },
            self.current_turn
        )
    
    def log_conversation_update(self, conversation: List[Dict[str, Any]], action: str = "append"):
        """Log conversation update."""
        self._log_event(
            LogEventType.CONVERSATION_UPDATE,
            LogLevel.DEBUG,
            f"Conversation updated: {action}",
            {
                "action": action,
                "conversation_length": len(conversation),
                "latest_message": conversation[-1] if conversation else None,
            },
            self.current_turn
        )
    
    def log_conversation_prune(self, original_len: int, new_len: int, reason: str):
        """Log conversation pruning."""
        self._log_event(
            LogEventType.CONVERSATION_PRUNE,
            LogLevel.DEBUG,
            f"Conversation pruned from {original_len} to {new_len} messages",
            {
                "original_length": original_len,
                "new_length": new_len,
                "reason": reason,
            },
            self.current_turn
        )
    
    def log_token_warning(self, old_state: str, new_state: str, token_count: int, warning_message: str):
        """Log token usage warning."""
        self._log_event(
            LogEventType.TOKEN_WARNING,
            LogLevel.WARNING,
            f"Token usage warning: {old_state} -> {new_state} ({token_count} tokens)",
            {
                "old_state": old_state,
                "new_state": new_state,
                "token_count": token_count,
                "warning_message": warning_message,
            },
            self.current_turn
        )

    def log_turn_warning(self, old_state: str, new_state: str, turn_count: int, warning_message: str):
        """Log turn limit warning."""
        self._log_event(
            LogEventType.TURN_WARNING,
            LogLevel.WARNING,
            f"Turn limit warning: {old_state} -> {new_state} ({turn_count} turns)",
            {
                "old_state": old_state,
                "new_state": new_state,
                "turn_count": turn_count,
                "warning_message": warning_message,
            },
            self.current_turn
        )

    def log_user_interaction_requested(self, message: str):
        """Log when user interaction is requested."""
        self._log_event(
            LogEventType.USER_INTERACTION_REQUESTED,
            LogLevel.INFO,
            "User interaction requested",
            {"message": message},
            self.current_turn
        )
    
    def log_final_detected(self, content: str):
        """Log when final tool is detected."""
        self._log_event(
            LogEventType.FINAL_DETECTED,
            LogLevel.INFO,
            "Final tool detected, agent stopping",
            {"final_content": content},
            self.current_turn
        )
    
    def log_stop_signal(self):
        """Log when stop signal is received."""
        self._log_event(
            LogEventType.STOP_SIGNAL,
            LogLevel.WARNING,
            "Stop signal received",
            {},
            self.current_turn
        )
    
    def log_max_turns_reached(self):
        """Log when max turns reached."""
        self._log_event(
            LogEventType.MAX_TURNS_REACHED,
            LogLevel.WARNING,
            f"Maximum turns ({self.config.max_turns}) reached",
            {"max_turns": self.config.max_turns},
            self.current_turn
        )
    
    def log_execution_state_change(self, old_state: str, new_state: str):
        """Log execution state change."""
        self._log_event(
            LogEventType.EXECUTION_STATE_CHANGE,
            LogLevel.DEBUG,
            f"Execution state changed: {old_state} -> {new_state}",
            {
                "old_state": old_state,
                "new_state": new_state
            },
            self.current_turn
        )
    
    def log_session_state_change(self, old_state: str, new_state: str):
        """Log session state change."""
        self._log_event(
            LogEventType.SESSION_STATE_CHANGE,
            LogLevel.DEBUG,
            f"Session state changed: {old_state} -> {new_state}",
            {
                "old_state": old_state,
                "new_state": new_state
            },
            self.current_turn
        )
    
    def log_error(self, error_type: str, message: str, traceback: Optional[str] = None):
        """Log an error."""
        self._log_event(
            LogEventType.ERROR,
            LogLevel.ERROR,
            f"{error_type}: {message}",
            {
                "error_type": error_type,
                "message": message,
                "traceback": traceback,
            },
            self.current_turn
        )
    
    def close(self):
        """Close log file and cleanup."""
        with self._lock:
            if self._file_handle:
                try:
                    self._file_handle.close()
                except:
                    pass
                self._file_handle = None


# Convenience function to create logger from config
def create_logger(config: 'AgentConfig') -> Optional[AgentLogger]:
    """Create a logger based on config settings."""
    if not getattr(config, 'enable_logging', False):
        return None
    
    try:
        logger = AgentLogger(
            config=config,
            log_dir=getattr(config, 'log_dir', './logs'),
            log_level=getattr(config, 'log_level', LogLevel.INFO),
            enable_file_logging=getattr(config, 'enable_file_logging', True),
            enable_console_logging=getattr(config, 'enable_console_logging', False),
            jsonl_format=getattr(config, 'jsonl_format', True),
            max_file_size_mb=getattr(config, 'max_file_size_mb', 10),
            max_backup_files=getattr(config, 'max_backup_files', 5),
            session_id=getattr(config, 'session_id', None),
        )
        return logger
    except Exception as e:
        print(f"Failed to create logger: {e}")
        return None