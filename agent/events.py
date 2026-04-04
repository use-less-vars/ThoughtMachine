"""
Unified Event Schema for ThoughtMachine Agent System.

This module defines a standardized event format for communication between
agent components, logging, and GUI presentation.

Key concepts:
- Event: A typed data structure representing something that happened
- EventBus: Pub/sub system for loose coupling between components
- EventSchema: Type definitions and validation for all event types
"""

from __future__ import annotations
from agent.logging.debug_log import debug_log

import enum
import json
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Union, TypedDict, Protocol, runtime_checkable
from dataclasses import dataclass, field, asdict
from pydantic import BaseModel, Field, validator

# -----------------------------------------------------------------------------
# Core Event Types
# -----------------------------------------------------------------------------

class EventType(enum.Enum):
    """Standardized event types for the agent system."""
    
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
    
    # State changes
    EXECUTION_STATE_CHANGE = "execution_state_change"
    SESSION_STATE_CHANGE = "session_state_change"
    TOKEN_WARNING = "token_warning"
    TURN_WARNING = "turn_warning"
    TOKEN_UPDATE = "token_update"
    TURN_UPDATE = "turn_update"
    
    # Resource monitoring
    TOKEN_CRITICAL_COUNTDOWN_START = "token_critical_countdown_start"
    TURN_CRITICAL_COUNTDOWN_START = "turn_critical_countdown_start"
    TOKEN_CRITICAL_COUNTDOWN_EXPIRED = "token_critical_countdown_expired"
    TURN_CRITICAL_COUNTDOWN_EXPIRED = "turn_critical_countdown_expired"
    
    # User interaction
    USER_INTERACTION_REQUESTED = "user_interaction_requested"
    USER_QUERY = "user_query"
    
    # Terminal events
    FINAL_DETECTED = "final_detected"
    FINAL = "final"
    STOP_SIGNAL = "stop_signal"
    MAX_TURNS_REACHED = "max_turns_reached"
    MAX_TURNS = "max_turns"
    PAUSED = "paused"
    STOPPED = "stopped"
    THREAD_FINISHED = "thread_finished"
    
    # Errors
    ERROR = "error"
    
    # File operations
    FILE_ACCESS = "file_access"
    SECURITY_VIOLATION = "security_violation"
    
    # Docker operations
    DOCKER_SANDBOX = "docker_sandbox"
    
    # Capability checks
    CAPABILITY_CHECK = "capability_check"
    SECURITY_PROMPT = "security_prompt"
    SECURITY_RESPONSE = "security_response"
    
    # GUI events (for presentation layer)
    TURN = "turn"
    RATE_LIMIT_WARNING = "rate_limit_warning"
    
    # Legacy/compatibility event types
    TOOL_CALL_LEGACY = "tool_call"  # legacy format with "name" instead of "tool_name"
    TOOL_RESULT_LEGACY = "tool_result"


# -----------------------------------------------------------------------------
# Core Event Data Models
# -----------------------------------------------------------------------------

class EventMetadata(BaseModel):
    """Metadata common to all events."""
    event_id: str = Field(default_factory=lambda: f"evt_{int(time.time() * 1000)}_{hash(time.time())}")
    timestamp: datetime = Field(default_factory=datetime.now)
    source: str = "unknown"  # Component that generated the event
    session_id: Optional[str] = None
    turn: Optional[int] = None
    
    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }


class BaseEvent(BaseModel):
    """Base class for all typed events."""
    type: EventType
    metadata: EventMetadata = Field(default_factory=EventMetadata)
    data: Dict[str, Any] = Field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert event to dictionary format compatible with existing code."""
        result = {
            "type": self.type.value,
            "timestamp": self.metadata.timestamp.isoformat(),
            "event_id": self.metadata.event_id,
            "source": self.metadata.source,
            **self.data
        }
        if self.metadata.session_id:
            result["session_id"] = self.metadata.session_id
        if self.metadata.turn is not None:
            result["turn"] = self.metadata.turn
        return result
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> BaseEvent:
        """Create event from dictionary (backward compatibility)."""
        # If called on BaseEvent directly, use create_event to get appropriate subclass
        if cls == BaseEvent:
            # Extract metadata
            source = data.get("source", "unknown")
            session_id = data.get("session_id")
            turn = data.get("turn")
            
            # Determine event type
            event_type_str = data.get("type")
            try:
                event_type = EventType(event_type_str)
            except ValueError:
                # Try to map legacy event types
                event_type = _map_legacy_event_type(event_type_str)
            
            # Remove metadata fields from data
            event_data = {k: v for k, v in data.items() 
                         if k not in ["event_id", "timestamp", "source", "session_id", "turn", "type"]}
            
            # Use create_event to get appropriate subclass
            return create_event(event_type, event_data, source, session_id, turn)
        else:
            # Subclass is calling, use standard constructor
            # Extract metadata fields
            metadata = EventMetadata(
                event_id=data.get("event_id", f"evt_{int(time.time() * 1000)}"),
                timestamp=datetime.fromisoformat(data.get("timestamp", datetime.now().isoformat())),
                source=data.get("source", "unknown"),
                session_id=data.get("session_id"),
                turn=data.get("turn")
            )
            
            # Remove metadata fields from data
            event_data = {k: v for k, v in data.items() 
                         if k not in ["event_id", "timestamp", "source", "session_id", "turn", "type"]}
            
            # Determine event type
            event_type_str = data.get("type")
            try:
                event_type = EventType(event_type_str)
            except ValueError:
                # Try to map legacy event types
                event_type = _map_legacy_event_type(event_type_str)
            
            return cls(type=event_type, metadata=metadata, data=event_data)


# -----------------------------------------------------------------------------
# Specialized Event Models
# -----------------------------------------------------------------------------

class AgentStartEvent(BaseEvent):
    """Agent started with query and configuration."""
    type: EventType = Field(default=EventType.AGENT_START)
    
    @validator('data')
    def validate_data(cls, v):
        if "query" not in v:
            raise ValueError("AgentStartEvent requires 'query' in data")
        if "config" not in v:
            raise ValueError("AgentStartEvent requires 'config' in data")
        return v


class AgentEndEvent(BaseEvent):
    """Agent completed execution."""
    type: EventType = Field(default=EventType.AGENT_END)
    
    @validator('data')
    def validate_data(cls, v):
        if "end_type" not in v:
            raise ValueError("AgentEndEvent requires 'end_type' in data")
        return v


class ToolCallEvent(BaseEvent):
    """Tool execution request."""
    type: EventType = Field(default=EventType.TOOL_CALL)
    
    @validator('data')
    def validate_data(cls, v):
        if "tool_name" not in v and "name" not in v:
            raise ValueError("ToolCallEvent requires 'tool_name' or 'name' in data")
        if "arguments" not in v:
            raise ValueError("ToolCallEvent requires 'arguments' in data")
        
        # Ensure both name and tool_name exist for compatibility
        if "tool_name" in v and "name" not in v:
            v["name"] = v["tool_name"]
        elif "name" in v and "tool_name" not in v:
            v["tool_name"] = v["name"]
        
        return v
    
    def to_dict(self) -> Dict[str, Any]:
        """Ensure both name and tool_name are present for compatibility."""
        data = self.data.copy()
        # Ensure both fields exist
        if "tool_name" in data and "name" not in data:
            data["name"] = data["tool_name"]
        elif "name" in data and "tool_name" not in data:
            data["tool_name"] = data["name"]
        return super().to_dict()


class ToolResultEvent(BaseEvent):
    """Tool execution result."""
    type: EventType = Field(default=EventType.TOOL_RESULT)
    
    @validator('data')
    def validate_data(cls, v):
        if "tool_name" not in v and "name" not in v:
            raise ValueError("ToolResultEvent requires 'tool_name' or 'name' in data")
        if "result" not in v:
            raise ValueError("ToolResultEvent requires 'result' in data")
        
        # Ensure both name and tool_name exist for compatibility
        if "tool_name" in v and "name" not in v:
            v["name"] = v["tool_name"]
        elif "name" in v and "tool_name" not in v:
            v["tool_name"] = v["name"]
        
        return v
    
    def to_dict(self) -> Dict[str, Any]:
        """Ensure both name and tool_name are present for compatibility."""
        data = self.data.copy()
        # Ensure both fields exist
        if "tool_name" in data and "name" not in data:
            data["name"] = data["tool_name"]
        elif "name" in data and "tool_name" not in data:
            data["tool_name"] = data["name"]
        return super().to_dict()


class TokenWarningEvent(BaseEvent):
    """Token usage warning."""
    type: EventType = Field(default=EventType.TOKEN_WARNING)
    
    @validator('data')
    def validate_data(cls, v):
        required = ["old_state", "new_state", "token_count", "warning_message"]
        for field in required:
            if field not in v:
                raise ValueError(f"TokenWarningEvent requires '{field}' in data")
        return v


class TurnWarningEvent(BaseEvent):
    """Turn limit warning."""
    type: EventType = Field(default=EventType.TURN_WARNING)
    
    @validator('data')
    def validate_data(cls, v):
        required = ["old_state", "new_state", "turn_count", "warning_message"]
        for field in required:
            if field not in v:
                raise ValueError(f"TurnWarningEvent requires '{field}' in data")
        return v


class ErrorEvent(BaseEvent):
    """Error occurrence."""
    type: EventType = Field(default=EventType.ERROR)
    
    @validator('data')
    def validate_data(cls, v):
        if "error_type" not in v:
            raise ValueError("ErrorEvent requires 'error_type' in data")
        if "message" not in v:
            raise ValueError("ErrorEvent requires 'message' in data")
        return v


class TurnEvent(BaseEvent):
    """Turn progression event."""
    type: EventType = Field(default=EventType.TURN)
    
    @validator('data')
    def validate_data(cls, v):
        # Turn and history are optional for backward compatibility
        # turn is also available in metadata.turn
        # history may be missing from legacy turn events
        return v


class SecurityPromptEvent(BaseEvent):
    """Security prompt for user approval."""
    type: EventType = Field(default=EventType.SECURITY_PROMPT)
    
    @validator('data')
    def validate_data(cls, v):
        required = ["request_id", "agent_id", "tool_name", "capabilities", "arguments", "session_id"]
        for field in required:
            if field not in v:
                raise ValueError(f"SecurityPromptEvent requires '{field}' in data")
        return v


class SecurityResponseEvent(BaseEvent):
    """User response to security prompt."""
    type: EventType = Field(default=EventType.SECURITY_RESPONSE)
    
    @validator('data')
    def validate_data(cls, v):
        required = ["request_id", "approved", "remember"]
        for field in required:
            if field not in v:
                raise ValueError(f"SecurityResponseEvent requires '{field}' in data")
        return v


# -----------------------------------------------------------------------------
# Event Factory Functions
# -----------------------------------------------------------------------------

def create_event(
    event_type: Union[EventType, str],
    data: Dict[str, Any],
    source: str = "unknown",
    session_id: Optional[str] = None,
    turn: Optional[int] = None
) -> BaseEvent:
    """Create a typed event with proper validation."""
    if isinstance(event_type, str):
        try:
            event_type = EventType(event_type)
        except ValueError:
            # Try legacy mapping
            event_type = _map_legacy_event_type(event_type)
    
    metadata = EventMetadata(
        source=source,
        session_id=session_id,
        turn=turn
    )
    
    # Create appropriate event subclass based on type
    event_class_map = {
        EventType.AGENT_START: AgentStartEvent,
        EventType.AGENT_END: AgentEndEvent,
        EventType.TOOL_CALL: ToolCallEvent,
        EventType.TOOL_RESULT: ToolResultEvent,
        EventType.TOKEN_WARNING: TokenWarningEvent,
        EventType.TURN_WARNING: TurnWarningEvent,
        EventType.ERROR: ErrorEvent,
        EventType.TURN: TurnEvent,
        EventType.CAPABILITY_CHECK: BaseEvent,
        EventType.SECURITY_PROMPT: SecurityPromptEvent,
        EventType.SECURITY_RESPONSE: SecurityResponseEvent,
        EventType.FINAL: BaseEvent,
        EventType.MAX_TURNS: BaseEvent,
        EventType.STOPPED: BaseEvent,
        EventType.PAUSED: BaseEvent,
        EventType.THREAD_FINISHED: BaseEvent,
        EventType.USER_INTERACTION_REQUESTED: BaseEvent,
        EventType.RATE_LIMIT_WARNING: BaseEvent,
        EventType.TOKEN_UPDATE: BaseEvent,
        EventType.EXECUTION_STATE_CHANGE: BaseEvent,
        EventType.SESSION_STATE_CHANGE: BaseEvent,
    }
    
    event_class = event_class_map.get(event_type, BaseEvent)
    return event_class(type=event_type, metadata=metadata, data=data)


def create_tool_call_event(
    tool_name: str,
    arguments: Dict[str, Any],
    tool_call_id: str,
    source: str = "tool_executor",
    session_id: Optional[str] = None,
    turn: Optional[int] = None
) -> ToolCallEvent:
    """Create a standardized tool call event."""
    return ToolCallEvent(
        type=EventType.TOOL_CALL,
        metadata=EventMetadata(
            source=source,
            session_id=session_id,
            turn=turn
        ),
        data={
            "tool_name": tool_name,
            "arguments": arguments,
            "tool_call_id": tool_call_id
        }
    )


def create_tool_result_event(
    tool_name: str,
    result: Any,
    tool_call_id: str,
    success: bool = True,
    error: Optional[str] = None,
    source: str = "tool_executor",
    session_id: Optional[str] = None,
    turn: Optional[int] = None
) -> ToolResultEvent:
    """Create a standardized tool result event."""
    data = {
        "tool_name": tool_name,
        "result": result,
        "tool_call_id": tool_call_id,
        "success": success
    }
    if error:
        data["error"] = error
    
    return ToolResultEvent(
        type=EventType.TOOL_RESULT,
        metadata=EventMetadata(
            source=source,
            session_id=session_id,
            turn=turn
        ),
        data=data
    )


def create_token_warning_event(
    old_state: str,
    new_state: str,
    token_count: int,
    warning_message: str,
    source: str = "agent_state",
    session_id: Optional[str] = None,
    turn: Optional[int] = None
) -> TokenWarningEvent:
    """Create a standardized token warning event."""
    return TokenWarningEvent(
        type=EventType.TOKEN_WARNING,
        metadata=EventMetadata(
            source=source,
            session_id=session_id,
            turn=turn
        ),
        data={
            "old_state": old_state,
            "new_state": new_state,
            "token_count": token_count,
            "warning_message": warning_message
        }
    )


# -----------------------------------------------------------------------------
# Event Bus (Pub/Sub System)
# -----------------------------------------------------------------------------

class EventBus:
    """Simple pub/sub event bus for loose coupling between components."""
    
    def __init__(self):
        self._subscribers: Dict[EventType, List[callable]] = {}
        self._wildcard_subscribers: List[callable] = []
    
    def subscribe(self, event_type: Optional[EventType] = None, callback: callable = None):
        """Subscribe to events of specific type or all events."""
        if callback is None:
            # Decorator style
            def decorator(func):
                self.subscribe(event_type, func)
                return func
            return decorator
        
        if event_type is None:
            self._wildcard_subscribers.append(callback)
        else:
            if event_type not in self._subscribers:
                self._subscribers[event_type] = []
            self._subscribers[event_type].append(callback)
    
    def publish(self, event: BaseEvent):
        """Publish an event to all subscribers."""
        # Notify type-specific subscribers
        if event.type in self._subscribers:
            for callback in self._subscribers[event.type]:
                try:
                    callback(event)
                except Exception as e:
                    debug_log(f"Error in event subscriber for {event.type}: {e}", level="ERROR", component="EventBus")
        
        # Notify wildcard subscribers
        for callback in self._wildcard_subscribers:
            try:
                callback(event)
            except Exception as e:
                debug_log(f"Error in wildcard event subscriber: {e}", level="ERROR", component="EventBus")
    
    def publish_dict(self, event_dict: Dict[str, Any]):
        """Publish an event from dictionary format."""
        event = BaseEvent.from_dict(event_dict)
        self.publish(event)


# -----------------------------------------------------------------------------
# Legacy Compatibility
# -----------------------------------------------------------------------------

def _map_legacy_event_type(event_type_str: str) -> EventType:
    """Map legacy event type strings to standardized EventType."""
    import os
    import sys
    if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
        import os
        trunc_limit = int(os.environ.get('THOUGHTMACHINE_DEBUG_TRUNCATION', 100))
        msg = f"[events] _map_legacy_event_type: '{event_type_str}'"
        if trunc_limit > 0 and len(msg) > trunc_limit:
            msg = msg[:trunc_limit] + "..."
        sys.stderr.write(msg + '\n')
    mapping = {
        "tool_call": EventType.TOOL_CALL,
        "tool_result": EventType.TOOL_RESULT,
        "token_warning": EventType.TOKEN_WARNING,
        "turn_warning": EventType.TURN_WARNING,
        "final": EventType.FINAL,
        "stopped": EventType.STOPPED,
        "max_turns": EventType.MAX_TURNS,
        "thread_finished": EventType.THREAD_FINISHED,
        "paused": EventType.PAUSED,
        "error": EventType.ERROR,
        "turn": EventType.TURN,
        "token_update": EventType.TOKEN_UPDATE,
        "user_interaction_requested": EventType.USER_INTERACTION_REQUESTED,
        "user_query": EventType.USER_QUERY,
        "rate_limit_warning": EventType.RATE_LIMIT_WARNING,
        "execution_state_change": EventType.EXECUTION_STATE_CHANGE,
        "session_state_change": EventType.SESSION_STATE_CHANGE,
    }
    result = mapping.get(event_type_str)
    if result is None:
        if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
            import os
            trunc_limit = int(os.environ.get('THOUGHTMACHINE_DEBUG_TRUNCATION', 100))
            msg = f"[events] No mapping for '{event_type_str}', attempting direct EventType creation"
            if trunc_limit > 0 and len(msg) > trunc_limit:
                msg = msg[:trunc_limit] + "..."
            sys.stderr.write(msg + '\n')
        result = EventType(event_type_str)
    return result


def convert_to_legacy_format(event: BaseEvent) -> Dict[str, Any]:
    """Convert typed event to legacy dictionary format."""
    legacy = event.to_dict()
    
    # Ensure backward compatibility for tool events
    if event.type in (EventType.TOOL_CALL, EventType.TOOL_RESULT):
        # Ensure both name and tool_name fields exist with consistent values
        # Prefer tool_name as the source of truth for new events
        if "tool_name" in legacy:
            # Ensure name matches tool_name
            legacy["name"] = legacy["tool_name"]
        elif "name" in legacy:
            # Only name exists (legacy event), ensure tool_name matches it
            legacy["tool_name"] = legacy["name"]
        # If both exist but differ, prefer tool_name
        if "tool_name" in legacy and "name" in legacy and legacy["tool_name"] != legacy["name"]:
            legacy["name"] = legacy["tool_name"]

    # Ensure backward compatibility for warning events
    if event.type == EventType.TOKEN_WARNING or event.type == EventType.TURN_WARNING:
        # Map warning_message to message for legacy compatibility
        if "warning_message" in legacy and "message" not in legacy:
            legacy["message"] = legacy["warning_message"]
        # Also ensure "warning" field exists for compatibility
        if "warning_message" in legacy and "warning" not in legacy:
            legacy["warning"] = legacy["warning_message"]

    return legacy

def convert_from_legacy_format(legacy_dict: Dict[str, Any]) -> BaseEvent:
    """Convert legacy dictionary to typed event."""
    # Clean up compatibility fields before conversion
    cleaned_dict = legacy_dict.copy()
    
    # Map message/warning fields back to warning_message for warning events
    event_type_str = cleaned_dict.get("type")
    if event_type_str in ("token_warning", "turn_warning"):
        # If we have message but not warning_message, use message
        if "message" in cleaned_dict and "warning_message" not in cleaned_dict:
            cleaned_dict["warning_message"] = cleaned_dict["message"]
        # If we have warning but not warning_message, use warning
        elif "warning" in cleaned_dict and "warning_message" not in cleaned_dict:
            cleaned_dict["warning_message"] = cleaned_dict["warning"]

        # Remove compatibility fields to avoid duplicate data
        # Now that we have warning_message, remove message/warning to prevent duplicates
        # (they may still be in the dict from legacy conversion)
        cleaned_dict.pop("message", None)
        cleaned_dict.pop("warning", None)
        
        # Ensure required fields for warning events
        if event_type_str == "token_warning":
            # Ensure old_state and new_state exist
            if "old_state" not in cleaned_dict:
                # Try to infer from state field or use defaults
                state = cleaned_dict.get("state", "warning")
                cleaned_dict["old_state"] = "low"
                cleaned_dict["new_state"] = state
            if "new_state" not in cleaned_dict:
                # If new_state missing but old_state present, infer from state
                state = cleaned_dict.get("state", "warning")
                cleaned_dict["new_state"] = state
            # Ensure token_count exists (default 0)
            if "token_count" not in cleaned_dict:
                cleaned_dict["token_count"] = 0
            # Ensure warning_message exists (default empty)
            if "warning_message" not in cleaned_dict:
                cleaned_dict["warning_message"] = ""
        elif event_type_str == "turn_warning":
            # Ensure old_state and new_state exist
            if "old_state" not in cleaned_dict:
                # Try to infer from state field or use defaults
                state = cleaned_dict.get("state", "warning")
                cleaned_dict["old_state"] = "low"
                cleaned_dict["new_state"] = state
            if "new_state" not in cleaned_dict:
                # If new_state missing but old_state present, infer from state
                state = cleaned_dict.get("state", "warning")
                cleaned_dict["new_state"] = state
            # Ensure turn_count exists (default 0)
            if "turn_count" not in cleaned_dict:
                cleaned_dict["turn_count"] = 0
            # Ensure warning_message exists (default empty)
            if "warning_message" not in cleaned_dict:
                cleaned_dict["warning_message"] = ""

    # Handle error events missing error_type
    elif event_type_str == "error":
        # If error_type is missing, try to infer from message or use default
        if "error_type" not in cleaned_dict:
            # Try to extract from message if it starts with known error type
            message = cleaned_dict.get("message", "")
            if message.startswith("PROVIDER_ERROR"):
                cleaned_dict["error_type"] = "PROVIDER_ERROR"
            elif message.startswith("UNEXPECTED_ERROR"):
                cleaned_dict["error_type"] = "UNEXPECTED_ERROR"
            elif message.startswith("CONTROLLER_ERROR"):
                cleaned_dict["error_type"] = "CONTROLLER_ERROR"
            else:
                # Default error type
                cleaned_dict["error_type"] = "UNKNOWN_ERROR"
    # Handle turn events missing required fields
    elif event_type_str == "turn":
        # Ensure "history" exists (for event processor)
        if "history" not in cleaned_dict:
            cleaned_dict["history"] = []
    return BaseEvent.from_dict(cleaned_dict)

# -----------------------------------------------------------------------------
# Global Event Bus Instance
# -----------------------------------------------------------------------------

# Global event bus for application-wide event distribution
global_event_bus = EventBus()