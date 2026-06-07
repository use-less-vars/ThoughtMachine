"""
Event Schema for ThoughtMachine event-sourced architecture.

All events follow the same structure:
{
    "type": Literal[EventType],
    "created_at": float,  # epoch seconds with microseconds
    "data": Dict[str, Any]  # type-specific content
}

Events are immutable once created. The 'data' field contains only the semantic content,
no styling hints or GUI-specific fields.
"""

import time
from typing import Dict, Any, Literal, TypedDict, Union
from datetime import datetime


# ============================================================================
# Event Type Definitions
# ============================================================================

EventType = Literal[
    "user_query",
    "assistant_turn",
    "tool_call",
    "tool_result",
    "token_update",
    "system",
    "summary",
    "execution_state_change",
    "paused",
    "stopped",
    "final",
    "agent_responded",
    "error",
    "token_warning",
    "rate_limit_warning",
    "user_interaction_requested",
]


# ============================================================================
# Event Data Schemas (TypedDict for each event type)
# ============================================================================

class UserQueryData(TypedDict):
    """Data for user_query events."""
    content: str
    turn: int  # Turn number for grouping


class AssistantTurnData(TypedDict):
    """Data for assistant_turn events."""
    content: str
    reasoning: str  # May be empty string
    token_usage: Dict[str, int]  # {"input": ..., "output": ..., "total_input": ..., "total_output": ...}
    tool_calls: list  # List of tool call dicts, empty if no tool calls


class ToolCallData(TypedDict):
    """Data for tool_call events."""
    tool_name: str
    arguments: str  # JSON string
    tool_call_id: str
    success: bool  # Always True for call initiation
    error: str  # Empty string initially


class ToolResultData(TypedDict):
    """Data for tool_result events."""
    tool_name: str
    result: str
    tool_call_id: str
    success: bool
    error: str  # Empty if success


class TokenUpdateData(TypedDict):
    """Data for token_update events."""
    total_input: int
    total_output: int
    context_length: int


class SystemData(TypedDict):
    """Data for system events."""
    content: str


class SummaryData(TypedDict):
    """Data for summary events."""
    content: str
    kept_turns: int


class ExecutionStateChangeData(TypedDict):
    """Data for execution_state_change events."""
    old_state: str
    new_state: str


class PausedData(TypedDict):
    """Data for paused events."""
    turn: int
    context_length: int


class StoppedData(TypedDict):
    """Data for stopped events."""
    turn: int
    context_length: int
    usage: Dict[str, int]  # {"input": ..., "output": ..., "total_input": ..., "total_output": ...}


class FinalData(TypedDict):
    """Data for final events."""
    content: str
    reasoning: str
    turn: int
    context_length: int
    usage: Dict[str, int]


class ErrorData(TypedDict):
    """Data for error events."""
    error_type: str
    message: str
    traceback: str
    turn: int
    context_length: int


class TokenWarningData(TypedDict):
    """Data for token_warning events."""
    message: str
    token_count: int
    old_state: str
    new_state: str
    state: str


class RateLimitWarningData(TypedDict):
    """Data for rate_limit_warning events."""
    message: str
    wait_time: float
    turn_delay: float
    rate_limit_count: int
    turn: int


class UserInteractionRequestedData(TypedDict):
    """Data for user_interaction_requested events."""
    message: str
    turn: int
    context_length: int


# Union of all data types
EventData = Union[
    UserQueryData,
    AssistantTurnData,
    ToolCallData,
    ToolResultData,
    TokenUpdateData,
    SystemData,
    SummaryData,
    ExecutionStateChangeData,
    PausedData,
    StoppedData,
    FinalData,
    ErrorData,
    TokenWarningData,
    RateLimitWarningData,
    UserInteractionRequestedData,
]


# ============================================================================
# Event Creation Helpers
# ============================================================================

def create_event(event_type: EventType, data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Create a new event with timestamp.
    
    Args:
        event_type: Type of event
        data: Event-specific data dictionary
        
    Returns:
        Complete event dictionary with type, created_at, and data fields
    """
    return {
        "type": event_type,
        "created_at": time.time(),
        "data": data
    }


def create_user_query(content: str, turn: int) -> Dict[str, Any]:
    """Create a user_query event."""
    return create_event("user_query", {
        "content": content,
        "turn": turn
    })


def create_assistant_turn(
    content: str,
    reasoning: str,
    token_usage: Dict[str, int],
    tool_calls: list
) -> Dict[str, Any]:
    """Create an assistant_turn event."""
    return create_event("assistant_turn", {
        "content": content,
        "reasoning": reasoning,
        "token_usage": token_usage,
        "tool_calls": tool_calls
    })


def create_tool_call(
    tool_name: str,
    arguments: str,
    tool_call_id: str
) -> Dict[str, Any]:
    """Create a tool_call event."""
    return create_event("tool_call", {
        "tool_name": tool_name,
        "arguments": arguments,
        "tool_call_id": tool_call_id,
        "success": True,
        "error": ""
    })


def create_tool_result(
    tool_name: str,
    result: str,
    tool_call_id: str,
    success: bool,
    error: str = ""
) -> Dict[str, Any]:
    """Create a tool_result event."""
    return create_event("tool_result", {
        "tool_name": tool_name,
        "result": result,
        "tool_call_id": tool_call_id,
        "success": success,
        "error": error
    })


def create_token_update(
    total_input: int,
    total_output: int,
    context_length: int
) -> Dict[str, Any]:
    """Create a token_update event."""
    return create_event("token_update", {
        "total_input": total_input,
        "total_output": total_output,
        "context_length": context_length
    })


def create_system(content: str) -> Dict[str, Any]:
    """Create a system event."""
    return create_event("system", {
        "content": content
    })


def create_summary(content: str, kept_turns: int) -> Dict[str, Any]:
    """Create a summary event."""
    return create_event("summary", {
        "content": content,
        "kept_turns": kept_turns
    })


def create_execution_state_change(old_state: str, new_state: str) -> Dict[str, Any]:
    """Create an execution_state_change event."""
    return create_event("execution_state_change", {
        "old_state": old_state,
        "new_state": new_state
    })


def create_paused(turn: int, context_length: int) -> Dict[str, Any]:
    """Create a paused event."""
    return create_event("paused", {
        "turn": turn,
        "context_length": context_length
    })


def create_stopped(
    turn: int,
    context_length: int,
    usage: Dict[str, int]
) -> Dict[str, Any]:
    """Create a stopped event."""
    return create_event("stopped", {
        "turn": turn,
        "context_length": context_length,
        "usage": usage
    })


def create_final(
    content: str,
    reasoning: str,
    turn: int,
    context_length: int,
    usage: Dict[str, int]
) -> Dict[str, Any]:
    """Create a final event."""
    return create_event("final", {
        "content": content,
        "reasoning": reasoning,
        "turn": turn,
        "context_length": context_length,
        "usage": usage
    })


def create_error(
    error_type: str,
    message: str,
    traceback: str,
    turn: int,
    context_length: int
) -> Dict[str, Any]:
    """Create an error event."""
    return create_event("error", {
        "error_type": error_type,
        "message": message,
        "traceback": traceback,
        "turn": turn,
        "context_length": context_length
    })


def create_token_warning(
    message: str,
    token_count: int,
    old_state: str,
    new_state: str,
    state: str
) -> Dict[str, Any]:
    """Create a token_warning event."""
    return create_event("token_warning", {
        "message": message,
        "token_count": token_count,
        "old_state": old_state,
        "new_state": new_state,
        "state": state
    })


def create_rate_limit_warning(
    message: str,
    wait_time: float,
    turn_delay: float,
    rate_limit_count: int,
    turn: int
) -> Dict[str, Any]:
    """Create a rate_limit_warning event."""
    return create_event("rate_limit_warning", {
        "message": message,
        "wait_time": wait_time,
        "turn_delay": turn_delay,
        "rate_limit_count": rate_limit_count,
        "turn": turn
    })


def create_user_interaction_requested(
    message: str,
    turn: int,
    context_length: int
) -> Dict[str, Any]:
    """Create a user_interaction_requested event."""
    return create_event("user_interaction_requested", {
        "message": message,
        "turn": turn,
        "context_length": context_length
    })


# ============================================================================
# Utility Functions
# ============================================================================

def is_message_event(event: Dict[str, Any]) -> bool:
    """
    Check if an event should be included in LLM conversation context.
    
    Returns True for: user_query, assistant_turn, tool_result, system, summary
    """
    return event["type"] in {
        "user_query",
        "assistant_turn", 
        "tool_result",
        "system",
        "summary"
    }


def event_to_llm_message(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert an event to an OpenAI-compatible message for LLM context.
    
    Returns a dict with 'role' and 'content' (and optional 'tool_calls',
    'tool_call_id', 'reasoning_content').
    """
    event_type = event["type"]
    data = event["data"]
    
    if event_type == "user_query":
        return {"role": "user", "content": data["content"]}
    
    elif event_type == "assistant_turn":
        message = {"role": "assistant", "content": data["content"]}
        if data.get("reasoning"):
            message["reasoning_content"] = data["reasoning"]
        if data.get("tool_calls"):
            message["tool_calls"] = data["tool_calls"]
        return message
    
    elif event_type == "tool_result":
        return {
            "role": "tool",
            "content": data["result"],
            "tool_call_id": data["tool_call_id"]
        }
    
    elif event_type == "system":
        return {"role": "system", "content": data["content"]}
    
    elif event_type == "summary":
        # Summaries are inserted as system messages
        return {"role": "system", "content": data["content"]}
    
    else:
        raise ValueError(f"Cannot convert event type {event_type} to LLM message")