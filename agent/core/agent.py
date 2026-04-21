"""
Refactored Agent class - facade coordinating modular components.

This is the main Agent class that delegates to specialized modules:
- TokenCounter for token management
- LLMClient for LLM communication  
- ConversationManager for history management
- ToolExecutor for tool execution
- DebugContext for debugging

The original Agent class (1972 lines) is reduced to a coordinator.
"""
from __future__ import annotations
import json
import os
import queue
import time
import traceback
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any, TYPE_CHECKING, Generator
from agent.logging import log
from agent.logging_helpers import dump_messages
from pydantic import ValidationError
from llm_providers.exceptions import ProviderError, RateLimitExceeded
from tools import SIMPLIFIED_TOOL_CLASSES
from tools.utils import model_to_openai_tool
from tools.final import Final
from tools.request_user_interaction import RequestUserInteraction
from tools.summarize_tool import SummarizeTool
from fast_json_repair import loads as repair_loads
from session.models import RuntimeParams
from session.context_builder import ContextBuilder
from agent.core.state import AgentState, ExecutionState, SessionState
from agent import events as ev
from .token_counter import TokenCounter
from .llm_client import LLMClient, LLMError
from .conversation_manager import ConversationManager
from .tool_executor import ToolExecutor
from .turn_transaction import TurnTransaction
from .debug_context import DebugContext
try:
    from agent.logging import log
    DEBUG_PRUNING_AVAILABLE = True
except ImportError:
    DEBUG_PRUNING_AVAILABLE = False
    log = lambda *args, **kwargs: None
if TYPE_CHECKING:
    from agent.config import AgentConfig
    from session.models import Session
logger = logging.getLogger(__name__)
from .debug_context import PAUSE_DEBUG, pause_debug

class Agent:
    """Modular agent coordinating specialized components."""
    SAFETY_MARGIN = 1000
    DEFAULT_RESPONSE_TOKENS = 4096

    def __init__(self, config: AgentConfig, session=None, initial_conversation=None, session_id: str=None):
        """
        Initialize modular agent.
        
        Args:
            config: Agent configuration.
            session: Optional session object.
            initial_conversation: Optional initial conversation history.
            session_id: Session ID if no session provided.
        """
        self.config = config
        self._session = session
        self._conversation = []
        self.logger = None
        try:
            from agent.logging import create_logger
            LOGGING_AVAILABLE = True
        except ImportError:
            LOGGING_AVAILABLE = False
            create_logger = None
        if LOGGING_AVAILABLE and config.enable_logging:
            self.logger = create_logger(config)
            try:
                from thoughtmachine.security import CapabilityRegistry, set_logger as security_set_logger
                self.security_available = True
                security_set_logger(self.logger)
            except ImportError:
                self.security_available = False
        else:
            self.security_available = False
        if session is not None:
            self.session_id = session.session_id
            self._conversation = session.user_history
        else:
            self._session = None
            self.session_id = session_id
            self._conversation = initial_conversation.copy() if initial_conversation else []
        user_msg_count = 0
        for msg in self.conversation:
            if msg.get('role') == 'user':
                content = msg.get('content', '')
                if not content.startswith('[SYSTEM]'):
                    user_msg_count += 1
        self._display_turn = user_msg_count
        self._conversation_start_time = time.time()
        self.token_counter = TokenCounter(config)
        self.llm_client = LLMClient(config, session, self.logger)
        self.conversation_manager = ConversationManager(session, None, self.logger)
        self.debug_context = DebugContext(self.logger)
        self.tool_classes = config.tool_classes if config.tool_classes is not None else config.get_filtered_tool_classes()
        self.tool_definitions = [model_to_openai_tool(cls) for cls in self.tool_classes]
        self.tool_executor = ToolExecutor(self.tool_classes, config, None, self.logger, self.security_available)
        self.provider = self.llm_client.provider
        self.runtime_params = RuntimeParams(temperature=config.temperature, max_tokens=config.max_tokens, top_p=None)
        self._token_encoder = None
        if session is not None:
            self._token_counts = {'input': session.total_input_tokens, 'output': session.total_output_tokens}
        else:
            self._token_counts = {'input': config.initial_input_tokens, 'output': config.initial_output_tokens}
        self.conversation = self.llm_client.ensure_system_prompt(self.conversation)
        max_context_tokens = self._get_max_context_tokens()
        log('DEBUG', 'core.context_builder', f'Agent init: session is None={session is None}, max_context_tokens={max_context_tokens}')
        self.context_builder = self.llm_client.create_context_builder(token_limit=max_context_tokens)
        log('DEBUG', 'core.context_builder', f'Agent init: context_builder created, is None={self.context_builder is None}')
        if self.context_builder:
            self.conversation_manager.context_builder = self.context_builder
        self.stop_check = config.stop_check
        self._next_query_queue = queue.Queue()
        self._paused = False
        self._should_reset = False
        self._pause_requested = False
        self.rate_limit_delay = 1.0
        self.rate_limit_base_wait = 10.0
        self.rate_limit_backoff_factor = 1.2
        self.rate_limit_count = 0
        self.rate_limit_max_wait = 60.0
        self.rate_limit_active = False
        self.state = AgentState(self.config, self.logger)
        self.tool_executor.state = self.state
        self._initialize_session_state()
        self._update_conversation_token_estimate()

    @property
    def session(self):
        """Get session property."""
        return self._session

    @session.setter
    def session(self, value):
        """Set session property, updating context_builder if needed."""
        log('DEBUG', 'core.context_builder', f'session setter called: value is None={value is None}')
        self._session = value
        if hasattr(self, 'state') and self.state is not None:
            if value is not None and hasattr(value, 'security_config'):
                self.state.security_config = value.security_config
            else:
                self.state.security_config = None
        if hasattr(self, 'llm_client') and self.llm_client is not None:
            self.llm_client.session = value
        if hasattr(self, 'context_builder') and self.context_builder is not None and hasattr(self.context_builder, 'session'):
            self.context_builder.session = value
            log('DEBUG', 'core.context_builder', f'Updated existing context_builder.session')
        elif value is not None and hasattr(self, 'llm_client') and (self.llm_client is not None):
            max_context_tokens = self._get_max_context_tokens()
            self.context_builder = self.llm_client.create_context_builder(token_limit=max_context_tokens)
            log('DEBUG', 'core.context_builder', f'Created new context_builder with token_limit={max_context_tokens}: is None={self.context_builder is None}')
            if hasattr(self, 'conversation_manager') and self.conversation_manager is not None:
                self.conversation_manager.context_builder = self.context_builder
        if hasattr(self, 'conversation_manager') and self.conversation_manager is not None:
            self.conversation_manager.session = value

    @property
    def conversation(self):
        """Single source of truth for conversation data.
        
        Returns:
            When session exists: session.user_history
            When no session: internal _conversation list
        """
        if self._session is not None:
            return self._session.user_history
        return self._conversation

    @conversation.setter
    def conversation(self, value):
        """Control how conversation is replaced.

        When session exists: replaces contents of session.user_history in-place,
        updates session.updated_at, and invalidates HistoryProvider cache.

        When no session: assigns to _conversation.
        """
        if self._session is not None:
            self._session.user_history[:] = value
            self._session.updated_at = datetime.now()
            self._session._on_conversation_changed()
            if hasattr(self, 'context_builder') and self.context_builder is not None and hasattr(self.context_builder, '_cached_context'):
                self.context_builder._cached_context = None
        else:
            self._conversation = value

    def _initialize_session_state(self):
        """Initialize session state based on existing history."""
        if self.session is not None:
            if len(self.session.user_history) > 0:
                events = self.state.set_session_state(SessionState.CONTINUING)
                for event in events:
                    list(self._handle_state_event(event))
        elif self.conversation is not None and len(self.conversation) > 0:
            events = self.state.set_session_state(SessionState.CONTINUING)
            for event in events:
                list(self._handle_state_event(event))

    def _handle_state_event(self, event):
        """Process a state event (e.g., token warning, turn warning).

        Events are dictionaries with 'type' field.
        For warning events, inject warning message into conversation.
        """
        if event.get('type') == 'token_warning':
            pass
        elif event.get('type') == 'turn_warning':
            pass
        elif event.get('type') in ('token_critical_countdown_start', 'turn_critical_countdown_start', 'token_critical_countdown_expired', 'turn_critical_countdown_expired'):
            message = event.get('message', '')
            sender = event.get('sender', 'system')
            warning_msg = {'role': sender, 'content': '[SYSTEM NOTIFICATION] ' + message}
            self._add_to_conversation(warning_msg)
            warning_tokens = self._estimate_tokens(warning_msg)
            self.state.current_conversation_tokens += warning_tokens
            yield self._create_token_update_event()
        elif event.get('type') == 'execution_state_change':
            old_state = event.get('old_state')
            new_state = event.get('new_state')
            if self.logger:
                self.logger.py_logger.debug(f'Execution state change: {old_state} -> {new_state}')
            self._add_conversation_data_to_event(event)
            yield event
        elif event.get('type') == 'session_state_change':
            old_state = event.get('old_state')
            new_state = event.get('new_state')
            if self.logger:
                self.logger.py_logger.debug(f'Session state change: {old_state} -> {new_state}')
            self._add_conversation_data_to_event(event)
            yield event
        elif event.get('type') == 'state_change':
            if self.logger:
                self.logger.py_logger.debug(f"State change: {event.get('old_state')} -> {event.get('new_state')}")
            self._add_conversation_data_to_event(event)
            yield event

    def _update_conversation_token_estimate(self):
        """Update current_conversation_tokens by estimating tokens for runtime context."""
        log('DEBUG', 'core.context_builder', f"_update_conversation_token_estimate: has context_builder={hasattr(self, 'context_builder')}, context_builder is None={(self.context_builder if hasattr(self, 'context_builder') else 'no attr')}")
        log('DEBUG', 'core.context_builder', f'conversation length: {len(self.conversation)}')
        if self.session is not None:
            correct_token_limit = self._get_max_context_tokens()
            needs_update = False
            if not hasattr(self, 'context_builder') or self.context_builder is None:
                log('DEBUG', 'core.context_builder', 'Creating missing context_builder for token estimation')
                needs_update = True
            elif hasattr(self.context_builder, 'token_limit'):
                current_limit = self.context_builder.token_limit
                if current_limit != correct_token_limit:
                    log('DEBUG', 'core.context_builder', f'Context builder token_limit mismatch: {current_limit} != {correct_token_limit}, recreating')
                    needs_update = True
            if needs_update:
                if hasattr(self, 'llm_client') and self.llm_client is not None:
                    self.llm_client.session = self.session
                    self.context_builder = self.llm_client.create_context_builder(token_limit=correct_token_limit)
                    log('DEBUG', 'core.context_builder', f'Created/updated context_builder with token_limit={correct_token_limit}')
                    if self.context_builder and hasattr(self, 'conversation_manager') and (self.conversation_manager is not None):
                        self.conversation_manager.context_builder = self.context_builder
        if not hasattr(self, 'context_builder') or self.context_builder is None:
            runtime_context = self.conversation
            log('DEBUG', 'core.context_builder', f'Token estimation path: context_builder is None, using full conversation, length={len(runtime_context)}')
        elif hasattr(self.context_builder, 'get_context_for_llm'):
            runtime_context = self.context_builder.get_context_for_llm()
            log('DEBUG', 'core.context_builder', f'Token estimation path: used context_builder.get_context_for_llm, length={len(runtime_context)}')
        else:
            runtime_context = self.context_builder.build(self.conversation)
            log('DEBUG', 'core.context_builder', f'Token estimation path: used context_builder.build, length={len(runtime_context)}')
        original_len = len(runtime_context)
        runtime_context = ContextBuilder._cleanup_orphaned_tool_messages(runtime_context)
        if original_len != len(runtime_context):
            logger.warning(f'[DEBUG_CONTEXT] Token estimate: cleaned {original_len - len(runtime_context)} orphaned tool messages')
        estimated_tokens = 0
        for msg in runtime_context:
            estimated_tokens += self.token_counter.estimate_tokens(msg)
        self.state.current_conversation_tokens = estimated_tokens
        log('DEBUG', 'core.token_estimate', f'Estimated tokens: {estimated_tokens}, runtime_context length: {len(runtime_context)}, conversation length: {len(self.conversation)}')
        if hasattr(self, 'context_builder') and self.context_builder is not None and hasattr(self.context_builder, 'token_limit'):
            log('DEBUG', 'core.token_estimate', f'context_builder.token_limit: {self.context_builder.token_limit}')
        log('DEBUG', 'core.pruning', f'Runtime context token estimate (from {len(runtime_context)}/{len(self.conversation)} messages)', {'tokens': estimated_tokens})
        if self.logger and hasattr(self.logger, 'py_logger'):
            self.logger.py_logger.info(f'[TOKEN_ESTIMATE] Updated runtime context token estimate: {estimated_tokens} tokens (from {len(runtime_context)}/{len(self.conversation)} messages)')

    def _add_to_conversation(self, message):
        """Add a message via conversation_manager (ensures cache invalidation)."""
        pause_debug(f"_add_to_conversation called for {message.get('role')}...")
        pause_debug(f'Before add, conversation length: {len(self.conversation)}')
        updated = self.conversation_manager.add_message(message, self.conversation)
        self.conversation = updated
        # Phase 1 logging: user_history after add
        log("DEBUG", "core.history", "Message added", {"role": message.get("role"), "content_preview": message.get("content", "")[:100]})
        dump_messages(self.conversation, "user_history after add")
        pause_debug(f'After add, conversation length: {len(self.conversation)}')
        if hasattr(self, 'context_builder') and self.context_builder is not None:
            if hasattr(self.context_builder, '_cached_context'):
                self.context_builder._cached_context = None
                pause_debug(f"Cleared context builder cache after adding {message.get('role')} message")

    def _estimate_tokens(self, message):
        """Estimate tokens for a message."""
        return self.token_counter.estimate_tokens(message)

    def _update_tokens_and_yield(self, tool_tokens=None):
        """Update token count and yield token update event.
        
        Args:
            tool_tokens: Optional token count for tool result (ignored, we recalculate).
        """
        self._update_conversation_token_estimate()
        event_dict = {'type': 'token_update', 'context_length': self.state.current_conversation_tokens, 'usage': {'input': 0, 'output': 0, 'total_input': self.total_input_tokens, 'total_output': self.total_output_tokens}}
        self._add_conversation_data_to_event(event_dict)
        yield event_dict

    @property
    def total_input_tokens(self):
        if self.session is not None:
            return self.session.total_input_tokens
        return self._token_counts['input']

    @total_input_tokens.setter
    def total_input_tokens(self, value):
        if self.session is not None:
            self.session.total_input_tokens = value
        self._token_counts['input'] = value

    @property
    def total_output_tokens(self):
        if self.session is not None:
            return self.session.total_output_tokens
        return self._token_counts['output']

    @total_output_tokens.setter
    def total_output_tokens(self, value):
        if self.session is not None:
            self.session.total_output_tokens = value
        self._token_counts['output'] = value

    def _get_max_context_tokens(self) -> int:
        """Calculate maximum tokens available for context."""
        context_window = self.token_counter.get_model_context_window()
        max_context = context_window - self.SAFETY_MARGIN
        if self.config.max_tokens is not None:
            max_context -= self.config.max_tokens
        else:
            max_context -= self.DEFAULT_RESPONSE_TOKENS
        return max_context

    def _get_conversation_data_for_event(self) -> Dict[str, Any]:
        """
        Get conversation data for events with version tracking.
        Returns dict with conversation metadata for version tracking.
        """
        if self.session is not None:
            base_data = {'conversation_version': self.session.conversation_version, 'conversation_hash': self.session.conversation_hash}
        else:
            import hashlib
            from session.utils import normalize_conversation_for_hash
            conv_str = normalize_conversation_for_hash(self.conversation)
            version_hash = hashlib.md5(conv_str.encode()).hexdigest()[:8]
            version = int(version_hash, 16) if version_hash else 0
            base_data = {'conversation_version': version, 'conversation_hash': version_hash}
        conversation_id = self.session_id if self.session_id else base_data.get('conversation_hash', '')
        conversation_timestamp = getattr(self, '_conversation_start_time', time.time())
        conversation_tokens = self.state.current_conversation_tokens
        conversation_turns = self.state.current_turn
        return {**base_data, 'conversation_id': conversation_id, 'conversation_timestamp': conversation_timestamp, 'conversation_tokens': conversation_tokens, 'conversation_turns': conversation_turns}

    def _add_conversation_data_to_event(self, event: Dict[str, Any]) -> None:
        """Add conversation version and history to event."""
        current_time = time.time()
        event['created_at'] = current_time
        if 'timestamp' not in event:
            event['timestamp'] = current_time
        if self.session:
            event['seq'] = self.session._get_next_seq()
        conv_data = self._get_conversation_data_for_event()
        event.update(conv_data)

    def _create_token_update_event(self) -> dict:
        """Create token update event."""
        event = {'type': 'token_update', 'context_length': self.state.current_conversation_tokens, 'total_input': self.total_input_tokens, 'total_output': self.total_output_tokens}
        self._add_conversation_data_to_event(event)
        return event

    def reset_rate_limiting(self):
        """Reset rate limiting state."""
        self.rate_limit_delay = 1.0
        self.rate_limit_count = 0
        self.rate_limit_active = False

    def restart(self, new_config: AgentConfig):
        """
        Reload agent configuration while preserving conversation history.
        
        This allows switching LLM providers, models, or other configuration
        without losing the conversation context.
        
        Args:
            new_config: New AgentConfig to apply
        """
        from agent.config import AgentConfig
        if not isinstance(new_config, AgentConfig):
            raise TypeError(f'new_config must be AgentConfig, got {type(new_config)}')
        current_conversation = self.conversation.copy()
        token_counts = self._token_counts.copy()
        self.state.set_execution_state(ExecutionState.IDLE)
        self._next_query_queue = queue.Queue()
        self._paused = False
        self._pause_requested = False
        self._should_reset = False
        self.config = new_config
        self.token_counter = TokenCounter(new_config)
        self.llm_client = LLMClient(new_config, self.session, self.logger)
        self.provider = self.llm_client.provider
        self.runtime_params = RuntimeParams(temperature=new_config.temperature, max_tokens=new_config.max_tokens, top_p=None)
        self.tool_classes = new_config.tool_classes if new_config.tool_classes is not None else new_config.get_filtered_tool_classes()
        self.tool_definitions = [model_to_openai_tool(cls) for cls in self.tool_classes]
        self.tool_executor = ToolExecutor(self.tool_classes, new_config, self.state, self.logger, self.security_available)
        max_context_tokens = self._get_max_context_tokens()
        self.context_builder = self.llm_client.create_context_builder(token_limit=max_context_tokens)
        if self.context_builder:
            self.conversation_manager.context_builder = self.context_builder
        self.conversation = self.llm_client.ensure_system_prompt(current_conversation)
        self._token_counts = token_counts
        self.reset_rate_limiting()
        if self.logger:
            self.logger.log_info('AGENT_RESTART', f'Configuration reloaded, provider: {self.provider}')

    def request_pause(self):
        """Request pause at the next atomic turn boundary."""
        pause_debug(f'request_pause called, setting _pause_requested=True')
        self._pause_requested = True

    def process_query(self, query):
        """Process a user query, appending it to conversation and running the agent.
        Yields events as dicts."""
        pause_debug(f"process_query called with query: '{query[:50]}...'")
        pause_debug(f'Current execution state: {self.state.execution_state}')
        pause_debug(f'Conversation length before adding query: {len(self.conversation)}')
        pause_debug(f'context_builder exists: {self.context_builder is not None}')
        if self.context_builder and hasattr(self.context_builder, 'session'):
            pause_debug(f'context_builder.session: {self.context_builder.session}')
            if self.context_builder.session:
                pause_debug(f'context_builder.session.session_id: {self.context_builder.session.session_id}')
        pause_debug(f'agent.session: {self.session}')
        if self.session:
            pause_debug(f'agent.session.session_id: {self.session.session_id}')
        self.conversation = self.llm_client.ensure_system_prompt(self.conversation)
        pause_debug(f'Clearing _pause_requested (was {self._pause_requested})')
        self._pause_requested = False
        current_exec_state = self.state.execution_state
        if current_exec_state == ExecutionState.RUNNING:
            if self.logger:
                self.logger.log_error('EXECUTION_STATE', 'process_query called while already RUNNING')
        elif current_exec_state in (ExecutionState.PAUSED, ExecutionState.WAITING_FOR_USER):
            events = self.state.set_execution_state(ExecutionState.RUNNING)
            for event in events:
                for yielded_event in self._handle_state_event(event):
                    yield yielded_event
        else:
            events = self.state.set_execution_state(ExecutionState.RUNNING)
            for event in events:
                for yielded_event in self._handle_state_event(event):
                    yield yielded_event
        if self.logger:
            config_data = {'model': self.config.model, 'temperature': self.config.temperature, 'max_turns': self.config.max_turns, 'max_history_turns': self.config.max_history_turns, 'max_tokens': self.config.max_tokens, 'keep_initial_query': self.config.keep_initial_query, 'keep_system_messages': self.config.keep_system_messages}
            self.logger.log_agent_start(query, config_data)
            self.logger.log_system_resources()
        pause_debug(f"Adding user message to conversation: '{query[:50]}...'")
        user_msg = {'role': 'user', 'content': query}
        self._add_to_conversation(user_msg)
        pause_debug(f'After adding user message, conversation length: {len(self.conversation)}')
        estimated_tokens = self._estimate_tokens(user_msg)
        self.state.current_conversation_tokens += estimated_tokens
        yield self._create_token_update_event()
        self._display_turn = getattr(self, '_display_turn', 0) + 1
        log('WARNING', 'debug.agent.user_query', f"query='{query[:50]}...', _display_turn={self._display_turn}")
        event_dict = {'type': 'user_query', 'content': query, 'turn': self._display_turn}
        self._add_conversation_data_to_event(event_dict)
        if 'timestamp' not in event_dict:
            event_dict['timestamp'] = event_dict.get('created_at', time.time())
        yield event_dict
        prev_conversation_len = len(self.conversation)
        last_input_tokens = 0
        last_output_tokens = 0
        for turn in range(self.config.max_turns):
            turn_start_time = time.time()
            if self.logger:
                self.logger.log_turn_start(turn)
                if turn % 5 == 0:
                    self.logger.log_system_resources()
            countdown_events = self.state.decrement_critical_countdown()
            for event in countdown_events:
                for yielded_event in self._handle_state_event(event):
                    yield yielded_event
            if self.stop_check and self.stop_check():
                events = self.state.set_execution_state(ExecutionState.PAUSING)
                for event in events:
                    for yielded_event in self._handle_state_event(event):
                        yield yielded_event
                events = self.state.set_execution_state(ExecutionState.PAUSED)
                for event in events:
                    for yielded_event in self._handle_state_event(event):
                        yield yielded_event
                if self.logger:
                    self.logger.log_stop_signal()
                    self.logger.log_system_resources()
                    self.logger.log_agent_end('stopped', 'Stop signal received')
                    self.logger.close()
                stopped_event = {'type': 'stopped', 'turn': self._display_turn, 'context_length': self.state.current_conversation_tokens, 'usage': {'input': last_input_tokens, 'output': last_output_tokens, 'total_input': self.total_input_tokens, 'total_output': self.total_output_tokens}}
                self._add_conversation_data_to_event(stopped_event)
                yield stopped_event
                return
            turn_events = self.state.update_turn_state(turn)
            for event in turn_events:
                if event['type'] == 'turn_warning':
                    warning_msg = {'role': 'user', 'content': '[SYSTEM NOTIFICATION] ' + event.get('message', event.get('warning', ''))}
                    self._add_to_conversation(warning_msg)
                    warning_tokens = self._estimate_tokens(warning_msg)
                    self.state.current_conversation_tokens += warning_tokens
                    yield self._create_token_update_event()
                event_dict = {'type': event['type'], 'message': event.get('message', event.get('warning', '')), 'turn_count': event.get('turn_count', turn), 'turn': self._display_turn, 'context_length': self.state.current_conversation_tokens, 'usage': {'input': last_input_tokens, 'output': last_output_tokens, 'total_input': self.total_input_tokens, 'total_output': self.total_output_tokens}}
                self._add_conversation_data_to_event(event_dict)
                yield event_dict
            self._update_conversation_token_estimate()
            yield self._create_token_update_event()
            token_events = self.state.update_token_state(self.state.current_conversation_tokens)
            for event in token_events:
                if event['type'] == 'token_warning':
                    warning_msg = {'role': 'user', 'content': '[SYSTEM NOTIFICATION] ' + event.get('message', event.get('warning', ''))}
                    self._add_to_conversation(warning_msg)
                    warning_tokens = self._estimate_tokens(warning_msg)
                    self.state.current_conversation_tokens += warning_tokens
                    yield self._create_token_update_event()
                event_dict = {'type': event['type'], 'message': event.get('message', event.get('warning', '')), 'token_count': event.get('token_count', self.state.current_conversation_tokens), 'turn': self._display_turn, 'context_length': self.state.current_conversation_tokens, 'usage': {'input': last_input_tokens, 'output': last_output_tokens, 'total_input': self.total_input_tokens, 'total_output': self.total_output_tokens}}
                self._add_conversation_data_to_event(event_dict)
                yield event_dict
            for msg in self.conversation:
                if msg.get('role') == 'assistant' and 'tool_calls' in msg:
                    if msg.get('reasoning_content') is None:
                        msg['reasoning_content'] = ''
            if self.logger and hasattr(self.logger, 'py_logger'):
                system_msgs = [msg for msg in self.conversation if msg.get('role') == 'system']
                self.logger.py_logger.info(f'[CONVERSATION] Total messages: {len(self.conversation)}, system messages: {len(system_msgs)}')
            max_context_tokens = self._get_max_context_tokens()
            if self.logger and hasattr(self.logger, 'py_logger'):
                self.logger.py_logger.info(f'[CONTEXT] Max context tokens: {max_context_tokens}, model: {self.config.model}')
            self.debug_context.debug_context('before_build', context_builder=self.context_builder)
            if hasattr(self, 'context_builder') and self.context_builder is not None:
                messages = self.context_builder.build(self.conversation, max_tokens=max_context_tokens)
            else:
                messages = self.conversation
            self.debug_context.debug_context('after_build', messages=messages, context_builder=self.context_builder)
            pause_debug(f'Messages being sent to LLM ({len(messages)}):')
            for i, msg in enumerate(messages):
                role = msg.get('role', 'unknown')
                content_preview = str(msg.get('content', ''))[:100]
                pause_debug(f'  [{i}] {role}: {content_preview}...')
            original_len = len(messages)
            messages = ContextBuilder._cleanup_orphaned_tool_messages(messages)
            if original_len != len(messages):
                logger.warning(f'[DEBUG_CONTEXT] Agent: cleaned {original_len - len(messages)} orphaned tool messages from final context')
            if self.logger and hasattr(self.logger, 'py_logger'):
                import tiktoken
                try:
                    encoder = tiktoken.get_encoding('cl100k_base')
                except Exception:
                    encoder = None
                total_tokens = sum((self.token_counter.estimate_tokens(msg) for msg in messages))
                self.logger.py_logger.info(f'[CONTEXT] Built context: {len(messages)} messages, ~{total_tokens} tokens')
            if self.logger:
                self.logger.log_llm_request(messages, self.tool_definitions)
            if self.rate_limit_active:
                delay = min(self.rate_limit_delay, self.rate_limit_max_wait)
                if delay > 0:
                    if self.logger and hasattr(self.logger, 'py_logger'):
                        self.logger.py_logger.info(f'[RATE_LIMIT] Applying rate limit delay: {delay}s between turns')
                    time.sleep(delay)
            tools = self.llm_client.format_tools(self.tool_definitions)
            request_tokens = self.token_counter.estimate_request_tokens(messages, tools)
            model_context_window = self.token_counter.get_model_context_window()
            critical_threshold = int(model_context_window * 0.95)
            warning_threshold = int(model_context_window * 0.85)
            if request_tokens > model_context_window:
                error = f'Request token count ({request_tokens}) exceeds model context window ({model_context_window}). Cannot make API call. Please use SummarizeTool to reduce context size.'
                error_msg = {'role': 'user', 'content': '[SYSTEM NOTIFICATION] ' + error}
                self._add_to_conversation(error_msg)
                error_tokens = self._estimate_tokens(error_msg)
                self.state.current_conversation_tokens += error_tokens
                yield self._create_token_update_event()
                event_dict = {'type': 'token_warning', 'message': error, 'token_count': request_tokens, 'old_state': 'low', 'new_state': 'critical', 'state': 'critical', 'request_tokens': request_tokens, 'model_context_window': model_context_window}
                self._add_conversation_data_to_event(event_dict)
                yield event_dict
                if self.logger:
                    self.logger.log_token_warning('low', 'critical', request_tokens, f'Request tokens {request_tokens} exceed model context window {model_context_window}')
            elif request_tokens > critical_threshold:
                warning = f'Request token count ({request_tokens}) is near model context window limit ({model_context_window}). Please use SummarizeTool immediately to reduce context size.'
                warning_msg = {'role': 'user', 'content': '[SYSTEM NOTIFICATION] ' + warning}
                self._add_to_conversation(warning_msg)
                warning_tokens = self._estimate_tokens(warning_msg)
                self.state.current_conversation_tokens += warning_tokens
                yield self._create_token_update_event()
                event_dict = {'type': 'token_warning', 'message': warning, 'token_count': request_tokens, 'old_state': 'low', 'new_state': 'critical', 'state': 'critical', 'request_tokens': request_tokens, 'model_context_window': model_context_window}
                self._add_conversation_data_to_event(event_dict)
                yield event_dict
                if self.logger:
                    self.logger.log_token_warning('low', 'critical', request_tokens, f'Request tokens {request_tokens} near model context window {model_context_window}')
            elif request_tokens > warning_threshold:
                warning = f'Request token count ({request_tokens}) is approaching model context window ({model_context_window}). Consider using SummarizeTool soon.'
                warning_msg = {'role': 'user', 'content': '[SYSTEM NOTIFICATION] ' + warning}
                self._add_to_conversation(warning_msg)
                warning_tokens = self._estimate_tokens(warning_msg)
                self.state.current_conversation_tokens += warning_tokens
                yield self._create_token_update_event()
                event_dict = {'type': 'token_warning', 'message': warning, 'token_count': request_tokens, 'old_state': 'low', 'new_state': 'warning', 'state': 'warning', 'request_tokens': request_tokens, 'model_context_window': model_context_window}
                self._add_conversation_data_to_event(event_dict)
                yield event_dict
                if self.logger:
                    self.logger.log_token_warning('low', 'warning', request_tokens, f'Request tokens {request_tokens} approaching model context window {model_context_window}')
            try:
                chat_kwargs = {'temperature': self.runtime_params.temperature}
                if self.runtime_params.max_tokens is not None:
                    chat_kwargs['max_tokens'] = self.runtime_params.max_tokens
                if self.runtime_params.top_p is not None:
                    chat_kwargs['top_p'] = self.runtime_params.top_p
                llm_start_time = time.time()
                response = self.llm_client.chat_completion(messages=messages, tools=tools if tools else None, **chat_kwargs)
                llm_duration_ms = (time.time() - llm_start_time) * 1000
                if self.logger:
                    self.logger.log_latency('llm_call', llm_duration_ms, {'turn': turn, 'request_tokens': request_tokens, 'model': self.config.model, 'has_tools': bool(tools)})
                input_tokens = response.usage.get('prompt_tokens', 0) if response.usage else 0
                output_tokens = response.usage.get('completion_tokens', 0) if response.usage else 0
                last_input_tokens = input_tokens
                last_output_tokens = output_tokens
                self.total_input_tokens += input_tokens
                self.total_output_tokens += output_tokens
            except RateLimitExceeded as e:
                self.rate_limit_count += 1
                self.rate_limit_active = True
                if self.rate_limit_count > 1:
                    self.rate_limit_delay = min(self.rate_limit_delay * self.rate_limit_backoff_factor, self.rate_limit_max_wait)
                wait_time = self.rate_limit_base_wait
                if self.logger:
                    self.logger.log_error('RATE_LIMIT', f'Rate limit exceeded, waiting {wait_time}s, delay between turns: {self.rate_limit_delay}s')
                event_dict = {'type': 'rate_limit_warning', 'message': f'Rate limit exceeded. Waiting {wait_time}s before retrying. Delay between turns: {self.rate_limit_delay}s', 'wait_time': wait_time, 'turn_delay': self.rate_limit_delay, 'rate_limit_count': self.rate_limit_count, 'turn': self._display_turn}
                self._add_conversation_data_to_event(event_dict)
                yield event_dict
                time.sleep(wait_time)
                break
            except (ProviderError, LLMError) as e:
                events = self.state.set_execution_state(ExecutionState.STOPPED)
                for event in events:
                    for yielded_event in self._handle_state_event(event):
                        yield yielded_event
                error_type = 'PROVIDER_ERROR'
                if isinstance(e, LLMError):
                    error_type = e.error_type.upper()
                if self.logger:
                    self.logger.log_error(error_type, str(e))
                    self.logger.log_system_resources()
                    self.logger.log_agent_end('provider_error', f'Provider error: {e}')
                    self.logger.close()
                event_dict = {'type': 'error', 'error_type': error_type, 'message': str(e), 'traceback': traceback.format_exc(), 'turn': self._display_turn, 'context_length': self.state.current_conversation_tokens, 'usage': {'input': last_input_tokens, 'output': last_output_tokens, 'total_input': self.total_input_tokens, 'total_output': self.total_output_tokens}}
                self._add_conversation_data_to_event(event_dict)
                yield event_dict
                return
            except Exception as e:
                logger.exception(f'[Agent] Unexpected exception in process_query: {e}')
                events = self.state.set_execution_state(ExecutionState.STOPPED)
                for event in events:
                    for yielded_event in self._handle_state_event(event):
                        yield yielded_event
                if self.logger:
                    self.logger.log_error('UNEXPECTED_ERROR', str(e))
                    self.logger.log_system_resources()
                    self.logger.log_agent_end('unexpected_error', f'Unexpected error: {e}')
                    self.logger.close()
                event_dict = {'type': 'error', 'error_type': 'UNEXPECTED_ERROR', 'message': str(e), 'traceback': traceback.format_exc(), 'turn': self._display_turn, 'context_length': self.state.current_conversation_tokens, 'usage': {'input': last_input_tokens, 'output': last_output_tokens, 'total_input': self.total_input_tokens, 'total_output': self.total_output_tokens}}
                self._add_conversation_data_to_event(event_dict)
                yield event_dict
                return
            content = response.content or ''
            reasoning = response.reasoning
            tool_calls = response.tool_calls
            user_interaction_message = None
            pause_debug(f'Checking pause request before turn: _pause_requested={self._pause_requested}')
            if self._pause_requested:
                pause_debug(f'Pause detected! Transitioning to PAUSING then PAUSED')
                events = self.state.set_execution_state(ExecutionState.PAUSING)
                for event in events:
                    for yielded_event in self._handle_state_event(event):
                        yield yielded_event
                events = self.state.set_execution_state(ExecutionState.PAUSED)
                for event in events:
                    for yielded_event in self._handle_state_event(event):
                        yield yielded_event
                pause_debug(f'Clearing _pause_requested after pause')
                self._pause_requested = False
                pause_event = {'type': 'paused', 'turn': self._display_turn, 'context_length': self.state.current_conversation_tokens}
                self._add_conversation_data_to_event(pause_event)
                yield pause_event
                turn_duration = time.time() - turn_start_time
                if self.logger:
                    self.logger.log_system_resources()
                    self.logger.log_turn_complete(turn, {'input': last_input_tokens, 'output': last_output_tokens, 'duration_ms': turn_duration * 1000, 'context_tokens': self.state.current_conversation_tokens})
                return
            turn_transaction = TurnTransaction(self.session, self.context_builder)
            assistant_msg = {'role': 'assistant', 'content': content}
            if reasoning is not None:
                assistant_msg['reasoning_content'] = reasoning
            elif tool_calls:
                assistant_msg['reasoning_content'] = ''
            if tool_calls:
                assistant_msg['tool_calls'] = tool_calls
            turn_transaction.add_assistant_message(assistant_msg)
            for event in self._update_tokens_and_yield():
                yield event
            turn_event = {'type': 'turn', 'content': content, 'assistant_content': content, 'tool_calls': [], 'turn': self._display_turn, 'context_length': self.state.current_conversation_tokens, 'usage': {'input': last_input_tokens, 'output': last_output_tokens, 'total_input': self.total_input_tokens, 'total_output': self.total_output_tokens}}
            if reasoning is not None:
                turn_event['reasoning'] = reasoning
            elif tool_calls:
                turn_event['reasoning'] = ''
            self._add_conversation_data_to_event(turn_event)
            yield turn_event
            if tool_calls:
                executed_tools, final_detected, final_content, user_interaction_message, summary_text, summary_keep_recent_turns = self.tool_executor.execute_tool_calls(tool_calls, add_to_conversation_func=self._add_to_conversation, update_token_func=self._update_tokens_and_yield, agent_id=0, turn_transaction=turn_transaction)
                processed_tools = []
                for tool_info in executed_tools:
                    result = tool_info.get('result', '')
                    success = True
                    error = None
                    if isinstance(result, str):
                        if result.startswith('❌') or 'TOOL CALL REJECTED' in result or 'Error executing tool' in result:
                            success = False
                            error = result
                    processed_tools.append({'name': tool_info.get('name'), 'arguments': tool_info.get('arguments'), 'result': result, 'success': success, 'error': error, 'turn': self._display_turn})
                for tool in processed_tools:
                    event_dict = {'type': 'tool_call', 'tool_name': tool['name'], 'arguments': tool['arguments'], 'success': tool['success'], 'error': tool['error'], 'turn': tool['turn']}
                    self._add_conversation_data_to_event(event_dict)
                    yield event_dict
                for tool in processed_tools:
                    event_dict = {'type': 'tool_result', 'tool_name': tool['name'], 'result': tool['result'], 'success': tool['success'], 'error': tool['error'], 'turn': tool['turn']}
                    self._add_conversation_data_to_event(event_dict)
                    yield event_dict
                if turn_transaction:
                    turn_transaction.commit()
                if final_detected:
                    events = self.state.set_execution_state(ExecutionState.FINALIZED)
                    for event in events:
                        for yielded_event in self._handle_state_event(event):
                            yield yielded_event
                    final_event = {'type': 'final', 'content': final_content if final_content is not None else content, 'turn': self._display_turn, 'context_length': self.state.current_conversation_tokens, 'usage': {'input': last_input_tokens, 'output': last_output_tokens, 'total_input': self.total_input_tokens, 'total_output': self.total_output_tokens}}
                    if reasoning is not None:
                        final_event['reasoning'] = reasoning
                    elif tool_calls:
                        final_event['reasoning'] = ''
                    self._add_conversation_data_to_event(final_event)
                    yield final_event
                    turn_duration = time.time() - turn_start_time
                    if self.logger:
                        self.logger.log_system_resources()
                        self.logger.log_turn_complete(turn, {'input': last_input_tokens, 'output': last_output_tokens, 'duration_ms': turn_duration * 1000, 'context_tokens': self.state.current_conversation_tokens})
                    return
                if user_interaction_message is not None:
                    events = self.state.set_execution_state(ExecutionState.WAITING_FOR_USER)
                    for event in events:
                        for yielded_event in self._handle_state_event(event):
                            yield yielded_event
                    turn_duration = time.time() - turn_start_time
                    if self.logger:
                        self.logger.log_system_resources()
                        self.logger.log_turn_complete(turn, {'input': last_input_tokens, 'output': last_output_tokens, 'duration_ms': turn_duration * 1000, 'context_tokens': self.state.current_conversation_tokens})
                    return
                if summary_text is not None:
                    log('DEBUG', 'core.summary', f'Processing summary request: summary length={len(summary_text)}, keep_recent_turns={summary_keep_recent_turns}')
                    self._apply_summary_pruning(summary_text, summary_keep_recent_turns)
                    for event in self._update_tokens_and_yield():
                        yield event
            pause_debug(f'Checking pause request after turn processing: _pause_requested={self._pause_requested}')
            if self._pause_requested:
                pause_debug(f'Pause detected after turn processing! Transitioning to PAUSING then PAUSED')
                events = self.state.set_execution_state(ExecutionState.PAUSING)
                for event in events:
                    for yielded_event in self._handle_state_event(event):
                        yield yielded_event
                events = self.state.set_execution_state(ExecutionState.PAUSED)
                for event in events:
                    for yielded_event in self._handle_state_event(event):
                        yield yielded_event
                pause_debug(f'Clearing _pause_requested after pause (after turn processing)')
                self._pause_requested = False
                pause_event = {'type': 'paused', 'turn': self._display_turn, 'context_length': self.state.current_conversation_tokens}
                self._add_conversation_data_to_event(pause_event)
                yield pause_event
                turn_duration = time.time() - turn_start_time
                if self.logger:
                    self.logger.log_system_resources()
                    self.logger.log_turn_complete(turn, {'input': last_input_tokens, 'output': last_output_tokens, 'duration_ms': turn_duration * 1000, 'context_tokens': self.state.current_conversation_tokens})
                return
            if not tool_calls:
                if turn_transaction and turn_transaction.has_assistant_message():
                    turn_transaction.commit()
                events = self.state.set_execution_state(ExecutionState.PAUSING)
                for event in events:
                    for yielded_event in self._handle_state_event(event):
                        yield yielded_event
                events = self.state.set_execution_state(ExecutionState.PAUSED)
                for event in events:
                    for yielded_event in self._handle_state_event(event):
                        yield yielded_event
                if self.logger:
                    self.logger.log_system_resources()
                    self.logger.log_agent_end('completed', 'Assistant provided direct answer with no tool calls')
                    self.logger.close()
                final_event = {'type': 'final', 'content': content, 'turn': self._display_turn, 'context_length': self.state.current_conversation_tokens, 'usage': {'input': last_input_tokens, 'output': last_output_tokens, 'total_input': self.total_input_tokens, 'total_output': self.total_output_tokens}}
                if reasoning is not None:
                    final_event['reasoning'] = reasoning
                elif tool_calls:
                    final_event['reasoning'] = ''
                self._add_conversation_data_to_event(final_event)
                yield final_event
                return

    def _apply_summary_pruning(self, summary: str, keep_recent_turns: int):
        """Add summary message to append-only history with metadata.
        
        This implements the HistoryProvider pattern: session.user_history is append-only.
        We insert a summary message with metadata (pruning_keep_recent_turns, pruning_insertion_idx)
        that indicates where pruning conceptually occurred. The HistoryProvider will use this
        metadata to build runtime context: main prompt + latest summary + recent turns after summary.
        """
        log('DEBUG', 'core.pruning', f'_apply_summary_pruning called with summary length={len(summary)}, keep_recent_turns={keep_recent_turns}')
        log('DEBUG', 'core.session_history', f'self.session exists: {self.session is not None}')
        # Phase 4 logging: summarization
        log('DEBUG', 'core.pruning', f'Conversation length: {len(self.conversation) if self.conversation else 0}')
        if self.conversation:
            dump_messages(self.conversation, "Conversation before summarization")
        if self.session is None:
            logger.warning('[DEBUG_PRUNING] No session available, using fallback pruning')
            log('DEBUG', 'core.pruning', 'No session available, using fallback pruning')
            self._apply_summary_pruning_fallback(summary, keep_recent_turns)
            old_token_count = self.state.current_conversation_tokens
            self._update_conversation_token_estimate()
            if DEBUG_PRUNING_AVAILABLE:
                log_pruning_operation('Fallback pruning', f'{old_token_count} -> {self.state.current_conversation_tokens}')
            if self.logger and hasattr(self.logger, 'py_logger'):
                self.logger.py_logger.info(f'[PRUNING] Updated token estimate after fallback: {self.state.current_conversation_tokens} tokens (was {old_token_count})')
            return
        user_history = self.session.user_history
        log('DEBUG', 'core.session_history', f'session.user_history length: {len(user_history)}')
        log('DEBUG', 'core.session_history', f'session.summary set: {self.session.summary is not None}')
        insertion_idx = self._find_summary_insertion_index(user_history, keep_recent_turns)
        log('DEBUG', 'core.pruning', f'Computed insertion_idx={insertion_idx} for keep_recent_turns={keep_recent_turns}')
        other_messages = [msg for msg in user_history if msg.get('role') != 'system']
        turns = self._group_messages_into_turns(other_messages) if other_messages else []
        kept_turns_count = min(keep_recent_turns, len(turns)) if keep_recent_turns > 0 else 0
        log('DEBUG', 'core.pruning', f'Found {len(turns)} turns total, keeping {kept_turns_count} turns')
        if insertion_idx >= len(user_history):
            discarded_msg_count = len(user_history)
        else:
            discarded_msg_count = 0
            for i in range(insertion_idx):
                if user_history[i].get('role') != 'system':
                    discarded_msg_count += 1
        MAX_SUMMARY_LENGTH = 4000
        truncated_summary = summary
        if len(truncated_summary) > MAX_SUMMARY_LENGTH:
            truncated_summary = truncated_summary[:MAX_SUMMARY_LENGTH] + '... (truncated)'
        summary_msg = {'role': 'system', 'content': f'Summary of previous conversation: {truncated_summary}', 'summary': True, 'pruning_keep_recent_turns': keep_recent_turns, 'pruning_discarded_msg_count': discarded_msg_count, 'pruning_insertion_idx': insertion_idx}
        if insertion_idx >= len(user_history):
            user_history.append(summary_msg)
            log('DEBUG', 'core.message_insertion', f'Appended summary at end (insertion_idx={insertion_idx} >= len={len(user_history)})')
        else:
            user_history.insert(insertion_idx, summary_msg)
            log('DEBUG', 'core.message_insertion', f'Inserted summary at index {insertion_idx}')
        context_cleared_msg = {'role': 'user', 'content': '[SYSTEM NOTIFICATION] Context has been summarized. You now have a fresh context window and full access to tools.'}
        # Append unwarning after the tool result (at the end of user_history)
        user_history.append(context_cleared_msg)
        log('DEBUG', 'core.message_insertion', f'Appended context cleared message at end (history length: {len(user_history)})')
        self.session.summary = summary_msg
        self.session.updated_at = datetime.now()
        if self.conversation is not user_history:
            self.conversation = user_history
        log('DEBUG', 'core.context_builder', f"_apply_summary_pruning: clearing context_builder cache, exists={hasattr(self, 'context_builder')}, is None={(self.context_builder if hasattr(self, 'context_builder') else 'no attr')}, has _cached_context={(hasattr(self.context_builder, '_cached_context') if hasattr(self, 'context_builder') and self.context_builder is not None else False)}")
        # Phase 4 logging: after summarization
        log('DEBUG', 'core.pruning', f'Conversation length after summarization: {len(self.conversation) if self.conversation else 0}')
        if self.conversation:
            dump_messages(self.conversation, "Conversation after summarization")
        if hasattr(self, 'context_builder') and self.context_builder is not None and hasattr(self.context_builder, '_cached_context'):
            self.context_builder._cached_context = None
            log('DEBUG', 'core.context_builder', f'_apply_summary_pruning: cleared _cached_context')
        if self.logger:
            self.logger.log_conversation_prune(len(user_history) - 1, len(user_history), 'summary_pruning_append_only')
        if DEBUG_PRUNING_AVAILABLE:
            log_summary_operation(f'Added summary to append-only history: kept {kept_turns_count} turns, inserted at index {insertion_idx}')
        if self.logger and hasattr(self.logger, 'py_logger'):
            self.logger.py_logger.info(f'[PRUNING] Added summary to append-only history: kept {kept_turns_count} turns, inserted summary at index {insertion_idx}, history length: {len(user_history)} messages')
        old_token_count = self.state.current_conversation_tokens
        self._update_conversation_token_estimate()
        if DEBUG_PRUNING_AVAILABLE:
            log_pruning_operation('Summary pruning', f'tokens: {old_token_count} -> {self.state.current_conversation_tokens}, summary_idx={insertion_idx}, kept_turns={kept_turns_count}')
        if self.logger and hasattr(self.logger, 'py_logger'):
            self.logger.py_logger.info(f'[PRUNING] Updated token estimate: {self.state.current_conversation_tokens} tokens (was {old_token_count})')
        log('DEBUG', 'core.pruning', f'_apply_summary_pruning completed. History length: {len(user_history)} messages')
        log('DEBUG', 'core.session_history', f'session.summary exists: {self.session.summary is not None}')

    def _apply_summary_pruning_fallback(self, summary: str, keep_recent_turns: int):
        """Fallback pruning for when no session is available (legacy behavior)."""
        logger.warning('[DEBUG_PRUNING] Using fallback pruning (no session)')
        system_messages = [msg for msg in self.conversation if msg.get('role') == 'system']
        other_messages = [msg for msg in self.conversation if msg.get('role') != 'system']
        if not other_messages:
            return
        turns = self._group_messages_into_turns(other_messages)
        if keep_recent_turns <= 0:
            kept_turns = []
        else:
            kept_turns = turns[-keep_recent_turns:] if keep_recent_turns <= len(turns) else turns
        MAX_SUMMARY_LENGTH = 4000
        truncated_summary = summary
        if len(truncated_summary) > MAX_SUMMARY_LENGTH:
            truncated_summary = truncated_summary[:MAX_SUMMARY_LENGTH] + '... (truncated)'
        if kept_turns:
            first_kept_turn_idx = len(turns) - len(kept_turns)
            discarded_msg_count = sum((len(turns[i]) for i in range(first_kept_turn_idx)))
        else:
            discarded_msg_count = len(other_messages)
        insertion_idx = discarded_msg_count + len(system_messages)
        summary_msg = {'role': 'system', 'content': f'Summary of previous conversation: {truncated_summary}', 'summary': True, 'pruning_keep_recent_turns': keep_recent_turns, 'pruning_discarded_msg_count': discarded_msg_count, 'pruning_insertion_idx': insertion_idx}
        cleaned_system_messages = []
        if system_messages:
            cleaned_system_messages.append(system_messages[0])
        pruned_other = []
        for turn in kept_turns:
            pruned_other.extend(turn)
        new_conversation = cleaned_system_messages + [summary_msg] + pruned_other
        context_cleared_msg = {'role': 'user', 'content': '[SYSTEM NOTIFICATION] Context has been summarized. You now have a fresh context window and full access to tools.'}
        # Append unwarning after the tool result (at the end of new_conversation)
        new_conversation.append(context_cleared_msg)
        self.conversation = new_conversation
        logger.debug(f'[DEBUG_PRUNING] Fallback pruning: new conversation length {len(self.conversation)}')

    def _find_summary_insertion_index(self, user_history: List[Dict[str, Any]], keep_recent_turns: int) -> int:
        """Find index in user_history where summary should be inserted.
        
        Returns the index of the first message of the first kept turn.
        If keep_recent_turns is 0 or no turns to keep, returns len(user_history).
        """
        if keep_recent_turns <= 0:
            return len(user_history)
        turns = []
        current_turn = []
        turn_start_indices = []
        for i, msg in enumerate(user_history):
            role = msg.get('role')
            if role == 'system':
                continue
            if role == 'user':
                if current_turn:
                    turns.append(current_turn)
                current_turn = [msg]
                turn_start_indices.append(i)
            elif role == 'assistant' and msg.get('tool_calls'):
                if current_turn:
                    turns.append(current_turn)
                current_turn = [msg]
                turn_start_indices.append(i)
            elif current_turn:
                if role == 'tool':
                    if current_turn and current_turn[-1].get('role') == 'assistant' and current_turn[-1].get('tool_calls'):
                        current_turn.append(msg)
                    else:
                        continue
                else:
                    current_turn.append(msg)
            else:
                continue
        if current_turn:
            turns.append(current_turn)
        valid_turn_indices = []
        for idx, turn in enumerate(turns):
            if not turn:
                continue
            first_msg = turn[0]
            first_role = first_msg.get('role')
            if first_role == 'user' or (first_role == 'assistant' and first_msg.get('tool_calls')):
                valid_turn_indices.append(idx)
        if not valid_turn_indices:
            return len(user_history)
        if keep_recent_turns > len(valid_turn_indices):
            keep_recent_turns = len(valid_turn_indices)
        first_kept_valid_idx = len(valid_turn_indices) - keep_recent_turns
        first_kept_turn_idx = valid_turn_indices[first_kept_valid_idx]
        if first_kept_turn_idx < len(turn_start_indices):
            return turn_start_indices[first_kept_turn_idx]
        else:
            return len(user_history)

    def _group_messages_into_turns(self, messages: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        """Group non-system messages into turns.
        
        Rules:
        - User messages always start a new turn
        - Assistant messages with tool_calls can also start a turn (after pruning)
        - All messages after a turn start belong to that turn until next user message
        - System messages should be filtered out before calling this method
        - Turns that don't start with user or assistant-with-tools are discarded
        
        This ensures tool call sequences stay together, even when they start with
        an assistant (due to pruning cutting off the user part of the turn).
        """
        turns = []
        current_turn = []
        import os
        debug = os.environ.get('DEBUG_TURN_GROUPING')
        if debug:
            logger.debug(f'[DEBUG_TURN_GROUPING] Grouping {len(messages)} messages')
            max_to_show = 10
            for i, msg in enumerate(messages[:max_to_show]):
                role = msg.get('role')
                content_preview = str(msg.get('content', ''))[:50]
                has_tool_calls = 'tool_calls' in msg and msg['tool_calls']
                logger.debug(f'  [{i}] {role}: {content_preview}... tool_calls={has_tool_calls}')
            if len(messages) > max_to_show:
                logger.debug(f'  ... and {len(messages) - max_to_show} more messages')
        for msg in messages:
            role = msg.get('role')
            if role == 'system':
                continue
            if role == 'user':
                if current_turn:
                    turns.append(current_turn)
                current_turn = [msg]
            elif role == 'assistant' and msg.get('tool_calls'):
                if current_turn:
                    turns.append(current_turn)
                current_turn = [msg]
            elif current_turn:
                if role == 'tool':
                    if current_turn and current_turn[-1].get('role') == 'assistant' and current_turn[-1].get('tool_calls'):
                        current_turn.append(msg)
                    else:
                        if debug:
                            tool_call_id = msg.get('tool_call_id', 'unknown')
                            logger.debug(f'[DEBUG_TURN_GROUPING] Discarding orphaned tool message: {tool_call_id}')
                        continue
                else:
                    current_turn.append(msg)
            else:
                if debug:
                    logger.debug(f'[DEBUG_TURN_GROUPING] Discarding orphaned {role} message')
                continue
        if current_turn:
            turns.append(current_turn)
        valid_turns = []
        for turn in turns:
            if not turn:
                continue
            first_msg = turn[0]
            first_role = first_msg.get('role')
            if first_role == 'user':
                valid_turns.append(turn)
            elif first_role == 'assistant' and first_msg.get('tool_calls'):
                valid_turns.append(turn)
            elif debug:
                logger.debug(f'[DEBUG_TURN_GROUPING] Discarding turn starting with {first_role}')
        if debug:
            logger.debug(f'[DEBUG_TURN_GROUPING] Returned {len(valid_turns)} valid turns')
            max_to_show = 10
            for i, turn in enumerate(valid_turns[:max_to_show]):
                logger.debug(f"  Turn {i}: {[msg.get('role') for msg in turn]}")
            if len(valid_turns) > max_to_show:
                logger.debug(f'  ... and {len(valid_turns) - max_to_show} more turns')
        return valid_turns

    def reset(self):
        """Reset agent state."""
        pass

    def update_runtime_params(self, **kwargs):
        """Update runtime parameters."""
        pass

    def submit_next_query(self, query: str):
        """Submit next query to waiting agent."""
        pass

    def request_reset(self):
        """Request agent reset."""
        pass

    def _wait_for_next_query(self):
        """Wait for next query."""
        pass

    @classmethod
    def from_preset(cls, preset_name_or_obj, api_key: str='', base_url: str='https://api.deepseek.com', session: Optional['Session']=None, **overrides):
        """Create an Agent instance from a preset configuration."""
        from agent.config.preset import get_preset_loader
        if isinstance(preset_name_or_obj, str):
            loader = get_preset_loader()
            preset = loader.get_preset(preset_name_or_obj)
            if preset is None:
                raise ValueError(f"Preset '{preset_name_or_obj}' not found. Available: {loader.list_presets()}")
        else:
            preset = preset_name_or_obj
        tool_classes = []
        preset_tool_names = set(preset.tools or [])
        for tool_cls in SIMPLIFIED_TOOL_CLASSES:
            if tool_cls.__name__ in preset_tool_names:
                tool_classes.append(tool_cls)
        config_data = {'api_key': api_key or '', 'base_url': base_url, 'model': preset.model, 'temperature': preset.temperature, 'tool_classes': tool_classes, 'enabled_tools': list(preset_tool_names), 'system_prompt': preset.system_prompt, 'provider_type': 'openai_compatible', 'max_turns': overrides.get('max_turns', 100), 'detail': overrides.get('detail', 'normal'), 'workspace_path': overrides.get('workspace_path'), 'tool_output_token_limit': overrides.get('tool_output_token_limit', 10000), 'token_monitor_enabled': overrides.get('token_monitor_enabled', True), 'token_monitor_warning_threshold': overrides.get('token_monitor_warning_threshold', 35000), 'token_monitor_critical_threshold': overrides.get('token_monitor_critical_threshold', 50000), 'turn_monitor_enabled': overrides.get('turn_monitor_enabled', True), 'turn_monitor_warning_threshold': overrides.get('turn_monitor_warning_threshold', 0.8), 'turn_monitor_critical_threshold': overrides.get('turn_monitor_critical_threshold', 0.95), 'critical_countdown_turns': overrides.get('critical_countdown_turns', 5), 'enable_logging': overrides.get('enable_logging', True), 'log_dir': overrides.get('log_dir', './logs'), 'log_level': overrides.get('log_level', 'INFO'), 'enable_file_logging': overrides.get('enable_file_logging', True), 'enable_console_logging': overrides.get('enable_console_logging', False), 'jsonl_format': overrides.get('jsonl_format', True), 'log_categories': overrides.get('log_categories', ['SESSION', 'LLM', 'TOOLS']), 'max_file_size_mb': overrides.get('max_file_size_mb', 10), 'max_backup_files': overrides.get('max_backup_files', 5)}
        config_data.update(overrides)
        from agent.config import AgentConfig
        config = AgentConfig(**config_data)
        return cls(config, session=session)