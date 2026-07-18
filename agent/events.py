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
from agent.logging import log
import enum
import json
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Union, TypedDict, Protocol, runtime_checkable
from dataclasses import dataclass, field, asdict
from pydantic import BaseModel, Field, validator
import threading

class EventType(enum.Enum):
    """Standardized event types for the agent system."""
    AGENT_START = 'agent_start'
    AGENT_END = 'agent_end'
    LLM_REQUEST = 'llm_request'
    LLM_RESPONSE = 'llm_response'
    RAW_RESPONSE = 'raw_response'
    TOOL_CALL = 'tool_call'
    TOOL_RESULT = 'tool_result'
    CONVERSATION_UPDATE = 'conversation_update'
    CONVERSATION_PRUNE = 'conversation_prune'
    EXECUTION_STATE_CHANGE = 'execution_state_change'
    SESSION_STATE_CHANGE = 'session_state_change'
    TOKEN_WARNING = 'token_warning'
    TURN_WARNING = 'turn_warning'
    TIME_WARNING = 'time_warning'
    TOKEN_UPDATE = 'token_update'
    TURN_UPDATE = 'turn_update'
    AGENT_RESPONDED = 'agent_responded'
    USER_INTERACTION_REQUESTED = 'user_interaction_requested'
    USER_QUERY = 'user_query'
    FINAL_DETECTED = 'final_detected'
    FINAL = 'final'
    STOP_SIGNAL = 'stop_signal'
    MAX_TURNS_REACHED = 'max_turns_reached'
    MAX_TURNS = 'max_turns'
    PAUSED = 'paused'
    STOPPED = 'stopped'
    THREAD_FINISHED = 'thread_finished'
    SESSION_STOP = 'session_stop'
    ERROR = 'error'
    FILE_ACCESS = 'file_access'
    SECURITY_VIOLATION = 'security_violation'
    DOCKER_SANDBOX = 'docker_sandbox'
    CAPABILITY_CHECK = 'capability_check'
    SECURITY_PROMPT = 'security_prompt'
    SECURITY_RESPONSE = 'security_response'
    TURN = 'turn'
    RATE_LIMIT_WARNING = 'rate_limit_warning'
    TOOL_CALL_LEGACY = 'tool_call'
    TOOL_RESULT_LEGACY = 'tool_result'
    SYSTEM_NOTIFICATION = 'system_notification'
    WORKER_SPAWNED = 'worker_spawned'
    WORKER_STATUS = 'worker_status'
    WORKER_COMPLETED = 'worker_completed'
    WORKER_ERROR = 'worker_error'
    WORKER_MESSAGE = 'worker_message'
    ASSISTANT_MESSAGE = 'assistant_message'
    USER_MESSAGE = 'user_message'
    WEBSOCKET_CONNECT = 'websocket_connect'
    WEBSOCKET_DISCONNECT = 'websocket_disconnect'
    CONFIG_LOADED = 'config_loaded'
    # Worker-specific status/notification event types (for WorkerBusAdapter)
    TOKENS_UPDATED = 'tokens_updated'
    CONTEXT_UPDATED = 'context_updated'
    STATUS_MESSAGE = 'status_message'
    ERROR_OCCURRED = 'error_occurred'
    CONFIG_CHANGED = 'config_changed'
    CONVERSATION_CHANGED = 'conversation_changed'
    TOKEN_RECOVERY = 'token_recovery'
    CONTEXT_CLEARED = 'context_cleared'
    CONTEXT_SUMMARIZED = 'context_summarized'


class EventMetadata(BaseModel):
    """Metadata common to all events."""
    event_id: str = Field(default_factory=lambda: f'evt_{int(time.time() * 1000)}_{hash(time.time())}')
    timestamp: datetime = Field(default_factory=datetime.now)
    source: str = 'unknown'
    session_id: Optional[str] = None
    turn: Optional[int] = None

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}

class BaseEvent(BaseModel):
    """Base class for all typed events."""
    type: EventType
    metadata: EventMetadata = Field(default_factory=EventMetadata)
    data: Dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert event to dictionary format compatible with existing code."""
        result = {'type': self.type.value, 'timestamp': self.metadata.timestamp.isoformat(), 'event_id': self.metadata.event_id, 'source': self.metadata.source, **self.data}
        if self.metadata.session_id:
            result['session_id'] = self.metadata.session_id
        if self.metadata.turn is not None:
            result['turn'] = self.metadata.turn
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> BaseEvent:
        """Create event from dictionary (backward compatibility)."""
        if cls == BaseEvent:
            source = data.get('source', 'unknown')
            session_id = data.get('session_id')
            turn = data.get('turn')
            event_type_str = data.get('type')
            try:
                event_type = EventType(event_type_str)
            except ValueError:
                event_type = _map_legacy_event_type(event_type_str)
            event_data = {k: v for k, v in data.items() if k not in ['event_id', 'timestamp', 'source', 'session_id', 'turn', 'type']}
            return create_event(event_type, event_data, source, session_id, turn)
        else:
            metadata = EventMetadata(event_id=data.get('event_id', f'evt_{int(time.time() * 1000)}'), timestamp=datetime.fromisoformat(data.get('timestamp', datetime.now().isoformat())), source=data.get('source', 'unknown'), session_id=data.get('session_id'), turn=data.get('turn'))
            event_data = {k: v for k, v in data.items() if k not in ['event_id', 'timestamp', 'source', 'session_id', 'turn', 'type']}
            event_type_str = data.get('type')
            try:
                event_type = EventType(event_type_str)
            except ValueError:
                event_type = _map_legacy_event_type(event_type_str)
            return cls(type=event_type, metadata=metadata, data=event_data)

class AgentStartEvent(BaseEvent):
    """Agent started with query and configuration."""
    type: EventType = Field(default=EventType.AGENT_START)

    @validator('data')
    def validate_data(cls, v):
        if 'query' not in v:
            raise ValueError("AgentStartEvent requires 'query' in data")
        if 'config' not in v:
            raise ValueError("AgentStartEvent requires 'config' in data")
        return v

class AgentEndEvent(BaseEvent):
    """Agent completed execution."""
    type: EventType = Field(default=EventType.AGENT_END)

    @validator('data')
    def validate_data(cls, v):
        if 'end_type' not in v:
            raise ValueError("AgentEndEvent requires 'end_type' in data")
        return v

class ToolCallEvent(BaseEvent):
    """Tool execution request."""
    type: EventType = Field(default=EventType.TOOL_CALL)

    @validator('data')
    def validate_data(cls, v):
        if 'tool_name' not in v and 'name' not in v:
            raise ValueError("ToolCallEvent requires 'tool_name' or 'name' in data")
        if 'arguments' not in v:
            raise ValueError("ToolCallEvent requires 'arguments' in data")
        if 'tool_name' in v and 'name' not in v:
            v['name'] = v['tool_name']
        elif 'name' in v and 'tool_name' not in v:
            v['tool_name'] = v['name']
        return v

    def to_dict(self) -> Dict[str, Any]:
        """Ensure both name and tool_name are present for compatibility."""
        data = self.data.copy()
        if 'tool_name' in data and 'name' not in data:
            data['name'] = data['tool_name']
        elif 'name' in data and 'tool_name' not in data:
            data['tool_name'] = data['name']
        return super().to_dict()

class ToolResultEvent(BaseEvent):
    """Tool execution result."""
    type: EventType = Field(default=EventType.TOOL_RESULT)

    @validator('data')
    def validate_data(cls, v):
        if 'tool_name' not in v and 'name' not in v:
            raise ValueError("ToolResultEvent requires 'tool_name' or 'name' in data")
        if 'result' not in v:
            raise ValueError("ToolResultEvent requires 'result' in data")
        if 'tool_name' in v and 'name' not in v:
            v['name'] = v['tool_name']
        elif 'name' in v and 'tool_name' not in v:
            v['tool_name'] = v['name']
        return v

    def to_dict(self) -> Dict[str, Any]:
        """Ensure both name and tool_name are present for compatibility."""
        data = self.data.copy()
        if 'tool_name' in data and 'name' not in data:
            data['name'] = data['tool_name']
        elif 'name' in data and 'tool_name' not in data:
            data['tool_name'] = data['name']
        return super().to_dict()

class TokenWarningEvent(BaseEvent):
    """Token usage warning."""
    type: EventType = Field(default=EventType.TOKEN_WARNING)

    @validator('data')
    def validate_data(cls, v):
        required = ['old_state', 'new_state', 'token_count', 'warning_message']
        for field in required:
            if field not in v:
                raise ValueError(f"TokenWarningEvent requires '{field}' in data")
        return v

class TurnWarningEvent(BaseEvent):
    """Turn limit warning."""
    type: EventType = Field(default=EventType.TURN_WARNING)

    @validator('data')
    def validate_data(cls, v):
        required = ['old_state', 'new_state', 'turn_count', 'warning_message']
        for field in required:
            if field not in v:
                raise ValueError(f"TurnWarningEvent requires '{field}' in data")
        return v

class ErrorEvent(BaseEvent):
    """Error occurrence."""
    type: EventType = Field(default=EventType.ERROR)

    @validator('data')
    def validate_data(cls, v):
        if 'error_type' not in v:
            raise ValueError("ErrorEvent requires 'error_type' in data")
        if 'message' not in v:
            raise ValueError("ErrorEvent requires 'message' in data")
        return v

class TurnEvent(BaseEvent):
    """Turn progression event."""
    type: EventType = Field(default=EventType.TURN)

    @validator('data')
    def validate_data(cls, v):
        return v

class SecurityPromptEvent(BaseEvent):
    """Security prompt for user approval."""
    type: EventType = Field(default=EventType.SECURITY_PROMPT)

    @validator('data')
    def validate_data(cls, v):
        required = ['request_id', 'agent_id', 'tool_name', 'capabilities', 'arguments', 'session_id']
        for field in required:
            if field not in v:
                raise ValueError(f"SecurityPromptEvent requires '{field}' in data")
        return v

class SecurityResponseEvent(BaseEvent):
    """User response to security prompt."""
    type: EventType = Field(default=EventType.SECURITY_RESPONSE)

    @validator('data')
    def validate_data(cls, v):
        required = ['request_id', 'approved', 'remember']
        for field in required:
            if field not in v:
                raise ValueError(f"SecurityResponseEvent requires '{field}' in data")
        return v

class WorkerSpawnedEvent(BaseEvent):
    """Worker thread spawned and ready."""
    type: EventType = Field(default=EventType.WORKER_SPAWNED)

    @validator('data')
    def validate_data(cls, v):
        if 'worker_name' not in v:
            raise ValueError("WorkerSpawnedEvent requires 'worker_name' in data")
        return v

class WorkerStatusEvent(BaseEvent):
    """Worker runtime status changed."""
    type: EventType = Field(default=EventType.WORKER_STATUS)

    @validator('data')
    def validate_data(cls, v):
        required = ['worker_name', 'status']
        for field in required:
            if field not in v:
                raise ValueError(f"WorkerStatusEvent requires '{field}' in data")
        return v

class WorkerCompletedEvent(BaseEvent):
    """Worker finished execution."""
    type: EventType = Field(default=EventType.WORKER_COMPLETED)

    @validator('data')
    def validate_data(cls, v):
        if 'worker_name' not in v:
            raise ValueError("WorkerCompletedEvent requires 'worker_name' in data")
        return v

class WorkerMessageEvent(BaseEvent):
    """Worker message event."""
    type: EventType = Field(default=EventType.WORKER_MESSAGE)

    @validator('data')
    def validate_data(cls, v):
        if 'worker_name' not in v:
            raise ValueError("WorkerMessageEvent requires 'worker_name' in data")
        return v


class AssistantMessageEvent(BaseEvent):
    """Assistant message from a worker."""
    type: EventType = Field(default=EventType.ASSISTANT_MESSAGE)

    @validator('data')
    def validate_data(cls, v):
        if 'worker_name' not in v:
            raise ValueError("AssistantMessageEvent requires 'worker_name' in data")
        return v


class WorkerErrorEvent(BaseEvent):
    """Worker encountered an error."""
    type: EventType = Field(default=EventType.WORKER_ERROR)

    @validator('data')
    def validate_data(cls, v):
        required = ['worker_name', 'error']
        for field in required:
            if field not in v:
                raise ValueError(f"WorkerErrorEvent requires '{field}' in data")
        return v

def create_event(event_type: Union[EventType, str], data: Dict[str, Any], source: str='unknown', session_id: Optional[str]=None, turn: Optional[int]=None) -> BaseEvent:
    """Create a typed event with proper validation."""
    if isinstance(event_type, str):
        try:
            event_type = EventType(event_type)
        except ValueError:
            event_type = _map_legacy_event_type(event_type)
    metadata = EventMetadata(source=source, session_id=session_id, turn=turn)
    event_class_map = {EventType.AGENT_START: AgentStartEvent, EventType.AGENT_END: AgentEndEvent, EventType.TOOL_CALL: ToolCallEvent, EventType.TOOL_RESULT: ToolResultEvent, EventType.TOKEN_WARNING: TokenWarningEvent, EventType.TURN_WARNING: TurnWarningEvent, EventType.ERROR: ErrorEvent, EventType.TURN: TurnEvent, EventType.CAPABILITY_CHECK: BaseEvent, EventType.SECURITY_PROMPT: SecurityPromptEvent, EventType.SECURITY_RESPONSE: SecurityResponseEvent, EventType.FINAL: BaseEvent, EventType.MAX_TURNS: BaseEvent, EventType.STOPPED: BaseEvent, EventType.PAUSED: BaseEvent, EventType.THREAD_FINISHED: BaseEvent, EventType.USER_INTERACTION_REQUESTED: BaseEvent, EventType.RATE_LIMIT_WARNING: BaseEvent, EventType.TOKEN_UPDATE: BaseEvent, EventType.TOKEN_RECOVERY: BaseEvent, EventType.CONTEXT_CLEARED: BaseEvent, EventType.CONTEXT_SUMMARIZED: BaseEvent, EventType.EXECUTION_STATE_CHANGE: BaseEvent, EventType.SESSION_STATE_CHANGE: BaseEvent, EventType.WORKER_SPAWNED: WorkerSpawnedEvent, EventType.WORKER_STATUS: WorkerStatusEvent, EventType.WORKER_COMPLETED: WorkerCompletedEvent, EventType.WORKER_ERROR: WorkerErrorEvent, EventType.WORKER_MESSAGE: WorkerMessageEvent, EventType.ASSISTANT_MESSAGE: AssistantMessageEvent,
    EventType.WEBSOCKET_CONNECT: BaseEvent,
    EventType.WEBSOCKET_DISCONNECT: BaseEvent,
    EventType.CONFIG_LOADED: BaseEvent,}
    event_class = event_class_map.get(event_type, BaseEvent)
    return event_class(type=event_type, metadata=metadata, data=data)

def create_tool_call_event(tool_name: str, arguments: Dict[str, Any], tool_call_id: str, source: str='tool_executor', session_id: Optional[str]=None, turn: Optional[int]=None) -> ToolCallEvent:
    """Create a standardized tool call event."""
    return ToolCallEvent(type=EventType.TOOL_CALL, metadata=EventMetadata(source=source, session_id=session_id, turn=turn), data={'tool_name': tool_name, 'arguments': arguments, 'tool_call_id': tool_call_id})

def create_tool_result_event(tool_name: str, result: Any, tool_call_id: str, success: bool=True, error: Optional[str]=None, source: str='tool_executor', session_id: Optional[str]=None, turn: Optional[int]=None) -> ToolResultEvent:
    """Create a standardized tool result event."""
    data = {'tool_name': tool_name, 'result': result, 'tool_call_id': tool_call_id, 'success': success}
    if error:
        data['error'] = error
    return ToolResultEvent(type=EventType.TOOL_RESULT, metadata=EventMetadata(source=source, session_id=session_id, turn=turn), data=data)

def create_token_warning_event(old_state: str, new_state: str, token_count: int, warning_message: str, source: str='agent_state', session_id: Optional[str]=None, turn: Optional[int]=None) -> TokenWarningEvent:
    """Create a standardized token warning event."""
    return TokenWarningEvent(type=EventType.TOKEN_WARNING, metadata=EventMetadata(source=source, session_id=session_id, turn=turn), data={'old_state': old_state, 'new_state': new_state, 'token_count': token_count, 'warning_message': warning_message})

class EventBus:
    """Simple pub/sub event bus for loose coupling between components."""

    def __init__(self):
        self._subscribers: Dict[EventType, List[callable]] = {}
        self._wildcard_subscribers: List[callable] = []
        self._lock = threading.Lock()

    def subscribe(self, event_type: Optional[EventType]=None, callback: callable=None):
        """Subscribe to events of specific type or all events."""
        if callback is None:

            def decorator(func):
                self.subscribe(event_type, func)
                return func
            return decorator
        with self._lock:
            if event_type is None:
                self._wildcard_subscribers.append(callback)
            else:
                if event_type not in self._subscribers:
                    self._subscribers[event_type] = []
                self._subscribers[event_type].append(callback)

    def publish(self, event: BaseEvent):
        """Publish an event to all subscribers."""
        with self._lock:
            subscribers = list(self._subscribers.get(event.type, []))
            wildcard = list(self._wildcard_subscribers)
        for callback in subscribers:
            try:
                callback(event)
            except Exception as e:
                log('ERROR', 'core.event_bus', f'Error in event subscriber for {event.type}: {e}')
        for callback in wildcard:
            try:
                callback(event)
            except Exception as e:
                log('ERROR', 'core.event_bus', f'Error in wildcard event subscriber: {e}')

    def publish_dict(self, event_dict: Dict[str, Any]):
        """Publish an event from dictionary format."""
        event = BaseEvent.from_dict(event_dict)
        self.publish(event)

    def unsubscribe(self, event_type: Optional[EventType]=None, callback: callable=None):
        """Unsubscribe a callback from events of specific type or all events."""
        with self._lock:
            if event_type is None:
                try:
                    self._wildcard_subscribers.remove(callback)
                except ValueError:
                    pass
            else:
                if event_type in self._subscribers:
                    try:
                        self._subscribers[event_type].remove(callback)
                    except ValueError:
                        pass
                    if not self._subscribers[event_type]:
                        del self._subscribers[event_type]


def _map_legacy_event_type(event_type_str: str) -> EventType:
    """Map legacy event type strings to standardized EventType."""
    import os
    log('DEBUG', 'core.events', f"_map_legacy_event_type: '{event_type_str}'")
    mapping = {'tool_call': EventType.TOOL_CALL, 'tool_result': EventType.TOOL_RESULT, 'token_warning': EventType.TOKEN_WARNING, 'turn_warning': EventType.TURN_WARNING, 'time_warning': EventType.TIME_WARNING, 'agent_responded': EventType.AGENT_RESPONDED, 'final': EventType.FINAL, 'stopped': EventType.STOPPED, 'max_turns': EventType.MAX_TURNS, 'thread_finished': EventType.THREAD_FINISHED, 'paused': EventType.PAUSED, 'error': EventType.ERROR, 'turn': EventType.TURN, 'token_update': EventType.TOKEN_UPDATE, 'token_recovery': EventType.TOKEN_RECOVERY, 'context_cleared': EventType.CONTEXT_CLEARED, 'context_summarized': EventType.CONTEXT_SUMMARIZED, 'user_interaction_requested': EventType.USER_INTERACTION_REQUESTED, 'user_query': EventType.USER_QUERY, 'rate_limit_warning': EventType.RATE_LIMIT_WARNING, 'execution_state_change': EventType.EXECUTION_STATE_CHANGE, 'session_state_change': EventType.SESSION_STATE_CHANGE, 'worker_spawned': EventType.WORKER_SPAWNED, 'worker_status': EventType.WORKER_STATUS, 'worker_completed': EventType.WORKER_COMPLETED, 'worker_error': EventType.WORKER_ERROR,
    'websocket_connect': EventType.WEBSOCKET_CONNECT,
    'websocket_disconnect': EventType.WEBSOCKET_DISCONNECT,
    'config_loaded': EventType.CONFIG_LOADED,
    'tokens_updated': EventType.TOKENS_UPDATED,
    'context_updated': EventType.CONTEXT_UPDATED,
    'status_message': EventType.STATUS_MESSAGE,
    'error_occurred': EventType.ERROR_OCCURRED,
    'config_changed': EventType.CONFIG_CHANGED,
    'conversation_changed': EventType.CONVERSATION_CHANGED,}
    result = mapping.get(event_type_str)
    if result is None:
        log('DEBUG', 'core.events', f"No mapping for '{event_type_str}', attempting direct EventType creation")
        result = EventType(event_type_str)
    return result

def convert_to_legacy_format(event: BaseEvent) -> Dict[str, Any]:
    """Convert typed event to legacy dictionary format."""
    legacy = event.to_dict()
    if event.type in (EventType.TOOL_CALL, EventType.TOOL_RESULT):
        if 'tool_name' in legacy:
            legacy['name'] = legacy['tool_name']
        elif 'name' in legacy:
            legacy['tool_name'] = legacy['name']
        if 'tool_name' in legacy and 'name' in legacy and (legacy['tool_name'] != legacy['name']):
            legacy['name'] = legacy['tool_name']
    if event.type == EventType.TOKEN_WARNING or event.type == EventType.TURN_WARNING:
        if 'warning_message' in legacy and 'message' not in legacy:
            legacy['message'] = legacy['warning_message']
        if 'warning_message' in legacy and 'warning' not in legacy:
            legacy['warning'] = legacy['warning_message']
    return legacy

def convert_from_legacy_format(legacy_dict: Dict[str, Any]) -> BaseEvent:
    """Convert legacy dictionary to typed event."""
    cleaned_dict = legacy_dict.copy()
    event_type_str = cleaned_dict.get('type')
    if event_type_str in ('token_warning', 'turn_warning'):
        if 'message' in cleaned_dict and 'warning_message' not in cleaned_dict:
            cleaned_dict['warning_message'] = cleaned_dict['message']
        elif 'warning' in cleaned_dict and 'warning_message' not in cleaned_dict:
            cleaned_dict['warning_message'] = cleaned_dict['warning']
        cleaned_dict.pop('message', None)
        cleaned_dict.pop('warning', None)
        if event_type_str == 'token_warning':
            if 'old_state' not in cleaned_dict:
                state = cleaned_dict.get('state', 'warning')
                cleaned_dict['old_state'] = 'low'
                cleaned_dict['new_state'] = state
            if 'new_state' not in cleaned_dict:
                state = cleaned_dict.get('state', 'warning')
                cleaned_dict['new_state'] = state
            if 'token_count' not in cleaned_dict:
                cleaned_dict['token_count'] = 0
            if 'warning_message' not in cleaned_dict:
                cleaned_dict['warning_message'] = ''
        elif event_type_str == 'turn_warning':
            if 'old_state' not in cleaned_dict:
                state = cleaned_dict.get('state', 'warning')
                cleaned_dict['old_state'] = 'low'
                cleaned_dict['new_state'] = state
            if 'new_state' not in cleaned_dict:
                state = cleaned_dict.get('state', 'warning')
                cleaned_dict['new_state'] = state
            if 'turn_count' not in cleaned_dict:
                cleaned_dict['turn_count'] = 0
            if 'warning_message' not in cleaned_dict:
                cleaned_dict['warning_message'] = ''
    elif event_type_str == 'error':
        if 'error_type' not in cleaned_dict:
            message = cleaned_dict.get('message', '')
            if message.startswith('PROVIDER_ERROR'):
                cleaned_dict['error_type'] = 'PROVIDER_ERROR'
            elif message.startswith('UNEXPECTED_ERROR'):
                cleaned_dict['error_type'] = 'UNEXPECTED_ERROR'
            elif message.startswith('CONTROLLER_ERROR'):
                cleaned_dict['error_type'] = 'CONTROLLER_ERROR'
            else:
                cleaned_dict['error_type'] = 'UNKNOWN_ERROR'
    elif event_type_str == 'turn':
        if 'history' not in cleaned_dict:
            cleaned_dict['history'] = []
    return BaseEvent.from_dict(cleaned_dict)


class NullEventBus:
    """
    No-op EventBus stub for testing or when pub/sub is disabled.

    Provides the same interface as ``EventBus`` but silently discards all
    publishes and accepts (but ignores) subscribes.

    Unlike the real ``EventBus``, this class also implements an ``ask()``
    method that returns ``"deny"`` instantly with no blocking — this is
    critical for worker sub-agents where there is no human to answer
    security prompts.
    """

    def subscribe(self, event_type=None, callback=None):
        """No-op subscribe — does nothing."""
        pass

    def publish(self, event):
        """No-op publish — silently discards the event."""
        pass

    def publish_dict(self, event_dict):
        """No-op publish_dict — silently discards."""
        pass

    def unsubscribe(self, event_type=None, callback=None):
        """No-op unsubscribe — does nothing."""
        pass

    def ask(self, request):
        """
        Return ``"deny"`` instantly with no blocking.

        In a worker context there is no human to answer a security prompt,
        so we always deny immediately.  This method matches the signature
        expected by callers that interact with the security prompt queue.

        Args:
            request: A tuple of ``(title, message)`` or any request object.

        Returns:
            ``"deny"`` (always).
        """
        return "deny"


global_event_bus = EventBus()