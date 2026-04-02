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

# Optional dependency for system monitoring
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    psutil = None
    PSUTIL_AVAILABLE = False
from typing import Any, Dict, List, Optional, Union, TYPE_CHECKING, Deque, Tuple
from collections import deque
from queue import Queue, Empty

if TYPE_CHECKING:
    from agent.config import AgentConfig


# Session file version
CURRENT_SESSION_VERSION = "1.0"


class LogLevel(Enum):
    """Logging verbosity levels."""
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class LogCategory(Enum):
    """Categories for log events."""
    SESSION = "SESSION"
    UI = "UI"
    LLM = "LLM"
    TOOLS = "TOOLS"
    SECURITY = "SECURITY"
    PERFORMANCE = "PERFORMANCE"


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

    # Security and observability
    FILE_ACCESS = "file_access"
    SECURITY_VIOLATION = "security_violation"
    DOCKER_SANDBOX = "docker_sandbox"
    CAPABILITY_CHECK = "capability_check"
    ERROR = "error"
    
    # Performance monitoring
    LATENCY_MEASUREMENT = "latency_measurement"
    TOKEN_USAGE_TREND = "token_usage_trend"
    MEMORY_USAGE = "memory_usage"
    THROUGHPUT_METRIC = "throughput_metric"
    RESOURCE_UTILIZATION = "resource_utilization"


# Mapping from LogEventType to LogCategory
EVENT_TYPE_TO_CATEGORY = {
    # Agent lifecycle
    LogEventType.AGENT_START: LogCategory.SESSION,
    LogEventType.AGENT_END: LogCategory.SESSION,
    
    # LLM interaction
    LogEventType.LLM_REQUEST: LogCategory.LLM,
    LogEventType.LLM_RESPONSE: LogCategory.LLM,
    LogEventType.RAW_RESPONSE: LogCategory.LLM,
    
    # Tool execution
    LogEventType.TOOL_CALL: LogCategory.TOOLS,
    LogEventType.TOOL_RESULT: LogCategory.TOOLS,
    
    # Conversation management
    LogEventType.CONVERSATION_UPDATE: LogCategory.SESSION,
    LogEventType.CONVERSATION_PRUNE: LogCategory.SESSION,
    LogEventType.TOKEN_WARNING: LogCategory.UI,
    LogEventType.TURN_WARNING: LogCategory.UI,
    
    # Turn lifecycle
    LogEventType.TURN_START: LogCategory.SESSION,
    LogEventType.TURN_COMPLETE: LogCategory.SESSION,
    
    # Control flow
    LogEventType.USER_INTERACTION_REQUESTED: LogCategory.UI,
    LogEventType.FINAL_DETECTED: LogCategory.SESSION,
    LogEventType.STOP_SIGNAL: LogCategory.SESSION,
    LogEventType.MAX_TURNS_REACHED: LogCategory.SESSION,
    LogEventType.EXECUTION_STATE_CHANGE: LogCategory.SESSION,
    LogEventType.SESSION_STATE_CHANGE: LogCategory.SESSION,
    
    # Security and observability
    LogEventType.FILE_ACCESS: LogCategory.SECURITY,
    LogEventType.SECURITY_VIOLATION: LogCategory.SECURITY,
    LogEventType.DOCKER_SANDBOX: LogCategory.SECURITY,
    LogEventType.CAPABILITY_CHECK: LogCategory.SECURITY,
    
    # Error handling
    LogEventType.ERROR: LogCategory.UI,
    
    # Performance monitoring
    LogEventType.LATENCY_MEASUREMENT: LogCategory.PERFORMANCE,
    LogEventType.TOKEN_USAGE_TREND: LogCategory.PERFORMANCE,
    LogEventType.MEMORY_USAGE: LogCategory.PERFORMANCE,
    LogEventType.THROUGHPUT_METRIC: LogCategory.PERFORMANCE,
    LogEventType.RESOURCE_UTILIZATION: LogCategory.PERFORMANCE,
}

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
        # Convert config categories to LogCategory enum
        self.enabled_categories = [LogCategory(cat) for cat in config.log_categories]
        
        # Override with environment variable if set
        env_categories = os.environ.get('AGENT_LOG_CATEGORIES')
        if env_categories:
            env_list = [cat.strip().upper() for cat in env_categories.split(',') if cat.strip()]
            if env_list:
                self.enabled_categories = [LogCategory(cat) for cat in env_list]
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
        
        # Performance monitoring: track recent token usage for trend analysis
        self.recent_token_usage: Deque[Tuple[int, int, float]] = deque(maxlen=10)  # (input_tokens, output_tokens, timestamp)
        
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
    
    def _should_log_event(self, event_type: LogEventType, level: LogLevel) -> bool:
        """Check if an event should be logged based on level and category."""
        if not self._should_log(level):
            return False
        category = EVENT_TYPE_TO_CATEGORY.get(event_type)
        if category is None:
            # If mapping missing, allow logging (default behavior)
            return True
        return category in self.enabled_categories

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
        if not self._should_log_event(event_type, level):
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
        turn_input = usage.get("input", 0)
        turn_output = usage.get("output", 0)
        self.total_input_tokens += turn_input
        self.total_output_tokens += turn_output
        
        # Record token usage for trend analysis
        self.recent_token_usage.append((turn_input, turn_output, datetime.now().timestamp()))
        
        # Analyze token trends periodically (every 5 turns or when deque is full)
        if len(self.recent_token_usage) >= 5 and len(self.recent_token_usage) % 5 == 0:
            self._analyze_token_trends()
        
        self._log_event(
            LogEventType.TURN_COMPLETE,
            LogLevel.DEBUG,
            f"Turn {turn} completed",
            {
                "turn": turn,
                "turn_input_tokens": turn_input,
                "turn_output_tokens": turn_output,
                "cumulative_input_tokens": self.total_input_tokens,
                "cumulative_output_tokens": self.total_output_tokens,
            }
        )
    
    def _analyze_token_trends(self):
        """Analyze recent token usage patterns and log trends."""
        if len(self.recent_token_usage) < 2:
            return  # Need at least 2 data points
        
        # Extract data
        inputs = [item[0] for item in self.recent_token_usage]
        outputs = [item[1] for item in self.recent_token_usage]
        timestamps = [item[2] for item in self.recent_token_usage]
        
        # Calculate totals per turn
        totals = [inp + out for inp, out in zip(inputs, outputs)]
        
        # Compute simple trends
        avg_input = sum(inputs) / len(inputs) if inputs else 0
        avg_output = sum(outputs) / len(outputs) if outputs else 0
        avg_total = sum(totals) / len(totals) if totals else 0
        
        # Determine trend direction
        if len(totals) >= 3:
            recent_totals = totals[-3:]
            if recent_totals[2] > recent_totals[1] > recent_totals[0]:
                trend = "increasing"
            elif recent_totals[2] < recent_totals[1] < recent_totals[0]:
                trend = "decreasing"
            else:
                # Check for spikes (sudden increase > 2x average)
                if any(t > avg_total * 2 for t in recent_totals):
                    trend = "spiking"
                else:
                    trend = "stable"
        else:
            trend = "stable"
        
        # Calculate period (time window covered)
        period_seconds = timestamps[-1] - timestamps[0] if len(timestamps) > 1 else 60.0
        
        # Log token usage trend
        self.log_token_usage_trend(
            current_tokens=int(avg_total),
            trend=trend,
            period_seconds=period_seconds,
            metadata={
                "avg_input_tokens": avg_input,
                "avg_output_tokens": avg_output,
                "avg_total_tokens": avg_total,
                "sample_size": len(self.recent_token_usage),
                "input_tokens_history": inputs,
                "output_tokens_history": outputs,
            }
        )

    def log_llm_request(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]):
        """Log LLM request being sent."""
        # Sanitize large data for logging
        sanitized_messages = []
        for msg in messages:
            sanitized_msg = msg.copy()
            if "content" in sanitized_msg and len(sanitized_msg["content"]) > 10000:
                sanitized_msg["content"] = sanitized_msg["content"][:10000] + "... [truncated]"
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
        if len(raw_str) > 50000:
            raw_str = raw_str[:50000] + "... [truncated]"
        
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
        if len(result_str) > 20000:
            result_str = result_str[:20000] + "... [truncated]"
        
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
    def log_file_access(self, path: str, operation: str, allowed: bool, size_bytes: Optional[int] = None, additional_data: Optional[Dict[str, Any]] = None):
        """Log file access event."""
        data = {
            "path": path,
            "operation": operation,
            "allowed": allowed,
        }
        if size_bytes is not None:
            data["size_bytes"] = size_bytes
        if additional_data:
            data.update(additional_data)
        
        self._log_event(
            LogEventType.FILE_ACCESS,
            LogLevel.INFO if allowed else LogLevel.WARNING,
            f"File access: {operation} on {path} - {'allowed' if allowed else 'denied'}",
            data,
            self.current_turn
        )
    
    def log_security_violation(self, violation_type: str, message: str, path: str, details: Optional[Dict[str, Any]] = None):
        """Log security violation."""
        data = {
            "violation_type": violation_type,
            "path": path,
        }
        if details:
            data.update(details)
        
        self._log_event(
            LogEventType.SECURITY_VIOLATION,
            LogLevel.WARNING,
            message,
            data,
            self.current_turn
        )
    
    def log_docker_sandbox(self, container_id: str, container_name: str, image: str, command: List[str], action: str, status: str, exit_code: Optional[int] = None, output_preview: Optional[str] = None):
        """Log Docker sandbox event."""
        data = {
            "container_id": container_id,
            "container_name": container_name,
            "image": image,
            "command": command,
            "action": action,
            "status": status,
        }
        if exit_code is not None:
            data["exit_code"] = exit_code
        if output_preview is not None:
            # Truncate output preview
            if len(output_preview) > 10000:
                output_preview = output_preview[:10000] + "... [truncated]"
            data["output_preview"] = output_preview
        
        self._log_event(
            LogEventType.DOCKER_SANDBOX,
            LogLevel.INFO,
            f"Docker {action}: container {container_id} ({status})",
            data,
            self.current_turn
        )
    
    def log_capability_check(self, agent_id: str, tool_name: str, required_capabilities: List[str], granted: bool, reason: str = "", additional_data: Optional[Dict[str, Any]] = None):
        """Log capability check."""
        data = {
            "agent_id": agent_id,
            "tool": tool_name,
            "required_capabilities": required_capabilities,
            "granted": granted,
            "reason": reason,
        }
        if additional_data:
            data.update(additional_data)
        
        self._log_event(
            LogEventType.CAPABILITY_CHECK,
            LogLevel.DEBUG,
            f"Capability check for {tool_name}: {'granted' if granted else 'denied'}",
            data,
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
    
    def log_performance_metric(
        self,
        metric_name: str,
        value: float,
        unit: str,
        tags: Optional[Dict[str, Any]] = None,
        description: str = "",
    ):
        """
        Log a performance metric.
        
        Args:
            metric_name: Name of the metric (e.g., "latency", "throughput", "memory")
            value: Numeric value of the metric
            unit: Unit of measurement (e.g., "ms", "tokens", "MB", "requests/sec")
            tags: Key-value pairs for categorization
            description: Human-readable description
        """
        data = {
            "metric_name": metric_name,
            "value": value,
            "unit": unit,
            "tags": tags or {},
            "description": description,
        }
        self._log_event(
            LogEventType.LATENCY_MEASUREMENT if "latency" in metric_name.lower() else LogEventType.THROUGHPUT_METRIC,
            LogLevel.INFO,
            f"Performance metric: {metric_name} = {value} {unit}",
            data,
            self.current_turn
        )

    def log_latency(self, operation: str, duration_ms: float, metadata: Optional[Dict[str, Any]] = None):
        """Log latency measurement for an operation."""
        self.log_performance_metric(
            metric_name=f"latency.{operation}",
            value=duration_ms,
            unit="ms",
            tags={"operation": operation},
            description=f"Latency for {operation}",
        )
        if metadata:
            # Also log detailed event with metadata
            self._log_event(
                LogEventType.LATENCY_MEASUREMENT,
                LogLevel.DEBUG,
                f"Latency for {operation}: {duration_ms} ms",
                {"operation": operation, "duration_ms": duration_ms, "metadata": metadata},
                self.current_turn
            )

    def log_memory_usage(self, memory_mb: float, memory_percent: Optional[float] = None, 
                         process_memory: bool = True, metadata: Optional[Dict[str, Any]] = None):
        """Log memory usage."""
        tags = {"process_memory": process_memory}
        if metadata:
            tags.update(metadata)
        
        self.log_performance_metric(
            metric_name="memory.usage",
            value=memory_mb,
            unit="MB",
            tags=tags,
            description=f"Memory usage: {memory_mb} MB" + 
                       (f" ({memory_percent}%)" if memory_percent else ""),
        )
        
        if memory_percent is not None:
            self.log_performance_metric(
                metric_name="memory.percent",
                value=memory_percent,
                unit="%",
                tags=tags,
                description=f"Memory percentage: {memory_percent}%",
            )

    def log_token_usage_trend(self, current_tokens: int, trend: str, 
                             period_seconds: float = 60.0, 
                             metadata: Optional[Dict[str, Any]] = None):
        """Log token usage trend over time."""
        valid_trends = ["increasing", "decreasing", "stable", "spiking", "draining"]
        if trend not in valid_trends:
            trend = "unknown"
        
        tags = {"trend": trend, "period_seconds": period_seconds}
        if metadata:
            tags.update(metadata)
        
        self.log_performance_metric(
            metric_name="tokens.usage",
            value=float(current_tokens),
            unit="tokens",
            tags=tags,
            description=f"Token usage: {current_tokens} tokens, trend: {trend}",
        )
        
        # Also log specific event
        self._log_event(
            LogEventType.TOKEN_USAGE_TREND,
            LogLevel.INFO,
            f"Token usage trend: {trend} ({current_tokens} tokens over {period_seconds}s)",
            {"current_tokens": current_tokens, "trend": trend, "period_seconds": period_seconds},
            self.current_turn
        )

    def log_throughput(self, metric_name: str, value: float, 
                      window_seconds: float = 60.0, 
                      metadata: Optional[Dict[str, Any]] = None):
        """Log throughput metric (e.g., turns per minute)."""
        tags = {"window_seconds": window_seconds}
        if metadata:
            tags.update(metadata)
        
        self.log_performance_metric(
            metric_name=f"throughput.{metric_name}",
            value=value,
            unit=f"{metric_name}/sec",
            tags=tags,
            description=f"Throughput: {value} {metric_name}/sec over {window_seconds}s",
        )
        
        # Also log specific event
        self._log_event(
            LogEventType.THROUGHPUT_METRIC,
            LogLevel.INFO,
            f"Throughput: {value} {metric_name}/sec over {window_seconds}s",
            {"metric_name": metric_name, "value": value, "window_seconds": window_seconds},
            self.current_turn
        )

    def log_resource_utilization(self, 
                               cpu_percent: Optional[float] = None,
                               memory_percent: Optional[float] = None,
                               disk_usage: Optional[Dict[str, Any]] = None,
                               network_io: Optional[Dict[str, Any]] = None,
                               metadata: Optional[Dict[str, Any]] = None):
        """Log system resource utilization."""
        data = {}
        if cpu_percent is not None:
            data["cpu_percent"] = cpu_percent
            self.log_performance_metric(
                metric_name="cpu.usage",
                value=cpu_percent,
                unit="%",
                tags={"resource": "cpu"},
                description=f"CPU usage: {cpu_percent}%",
            )
        
        if memory_percent is not None:
            data["memory_percent"] = memory_percent
            self.log_performance_metric(
                metric_name="memory.system_percent",
                value=memory_percent,
                unit="%",
                tags={"resource": "memory"},
                description=f"System memory usage: {memory_percent}%",
            )
        
        if disk_usage:
            data["disk_usage"] = disk_usage
            # Log disk usage metrics
            for key, val in disk_usage.items():
                if isinstance(val, (int, float)):
                    self.log_performance_metric(
                        metric_name=f"disk.{key}",
                        value=float(val),
                        unit="bytes" if "bytes" in key else "count",
                        tags={"resource": "disk"},
                        description=f"Disk {key}: {val}",
                    )
        
        if network_io:
            data["network_io"] = network_io
            # Log network metrics
            for key, val in network_io.items():
                if isinstance(val, (int, float)):
                    self.log_performance_metric(
                        metric_name=f"network.{key}",
                        value=float(val),
                        unit="bytes" if "bytes" in key else "packets",
                        tags={"resource": "network"},
                        description=f"Network {key}: {val}",
                    )
        
        if data:
            self._log_event(
                LogEventType.RESOURCE_UTILIZATION,
                LogLevel.INFO,
                "System resource utilization",
                data,
                self.current_turn
            )

    def log_system_resources(self):
        """Log system resource usage (memory, CPU, etc.) if psutil is available."""
        if not PSUTIL_AVAILABLE:
            return
        
        try:
            # Get process memory
            process = psutil.Process()
            process_memory = process.memory_info()
            memory_mb = process_memory.rss / (1024 * 1024)  # RSS in MB
            
            # Get process memory percent
            memory_percent = process.memory_percent()
            
            # Log process memory
            self.log_memory_usage(
                memory_mb=memory_mb,
                memory_percent=memory_percent,
                process_memory=True,
                metadata={
                    "process_id": process.pid,
                    "process_name": process.name(),
                }
            )
            
            # Get system-wide memory
            system_memory = psutil.virtual_memory()
            system_memory_percent = system_memory.percent
            system_memory_total = system_memory.total / (1024 * 1024)  # MB
            system_memory_used = system_memory.used / (1024 * 1024)    # MB
            
            # Get CPU usage
            cpu_percent = psutil.cpu_percent(interval=0.1)
            
            # Log resource utilization
            self.log_resource_utilization(
                cpu_percent=cpu_percent,
                memory_percent=system_memory_percent,
                disk_usage=None,  # Could add disk usage if needed
                network_io=None,  # Could add network IO if needed
                metadata={
                    "system_memory_total_mb": system_memory_total,
                    "system_memory_used_mb": system_memory_used,
                    "process_memory_mb": memory_mb,
                }
            )
            
        except Exception as e:
            # Don't crash if monitoring fails
            self._log_event(
                LogEventType.PERFORMANCE_METRIC,
                LogLevel.WARNING,
                f"Failed to collect system resources: {e}",
                {"error": str(e)},
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