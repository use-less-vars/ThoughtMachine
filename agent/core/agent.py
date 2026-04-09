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
from agent.logging.debug_log import debug_log

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

# Import our clean debug logging for pruning/history flow
try:
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/../../")
    from debug_pruning import (
        debug_log, log_session_history, log_history_provider_reconstruction,
        log_message_insertion, log_pruning_operation, log_token_count,
        log_summary_operation, truncate_message
    )
    DEBUG_PRUNING_AVAILABLE = True
except ImportError:
    DEBUG_PRUNING_AVAILABLE = False
    debug_log = lambda *args, **kwargs: None
    log_session_history = lambda *args, **kwargs: None
    log_history_provider_reconstruction = lambda *args, **kwargs: None
    log_message_insertion = lambda *args, **kwargs: None
    log_pruning_operation = lambda *args, **kwargs: None
    log_token_count = lambda *args, **kwargs: None
    log_summary_operation = lambda *args, **kwargs: None
    truncate_message = lambda *args, **kwargs: None

if TYPE_CHECKING:
    from agent.config import AgentConfig
    from session.models import Session

logger = logging.getLogger(__name__)

# Debug flag for pause/resume debugging
from .debug_context import PAUSE_DEBUG, pause_debug


class Agent:
    """Modular agent coordinating specialized components."""
    
    # Constants for context window management
    SAFETY_MARGIN = 1000  # tokens reserved for safety
    DEFAULT_RESPONSE_TOKENS = 4096  # default response tokens if max_tokens not set
    
    def __init__(self, config: AgentConfig, session=None, initial_conversation=None, session_id: str = None):
        """
        Initialize modular agent.
        
        Args:
            config: Agent configuration.
            session: Optional session object.
            initial_conversation: Optional initial conversation history.
            session_id: Session ID if no session provided.
        """
        self.config = config
        self._session = session  # Use private attribute with property
        self._conversation = []  # Private storage for session-less mode
        
        # Initialize logging if available
        self.logger = None
        try:
            from agent.logging import create_logger
            LOGGING_AVAILABLE = True
        except ImportError:
            LOGGING_AVAILABLE = False
            create_logger = None
        
        if LOGGING_AVAILABLE and config.enable_logging:
            self.logger = create_logger(config)
            
            # Initialize security module with logger if available
            try:
                from thoughtmachine.security import CapabilityRegistry, set_logger as security_set_logger
                self.security_available = True
                security_set_logger(self.logger)
            except ImportError:
                self.security_available = False
        else:
            self.security_available = False
        
        # Set up conversation
        if session is not None:
            self.session_id = session.session_id
            self._conversation = session.user_history
            # Reconstruct pruned conversation: sysprompt + most recent summary + recent turns
            # No-op with HistoryProvider: runtime context is built dynamically.
        else:
            self._session = None
            self.session_id = session_id
            self._conversation = initial_conversation.copy() if initial_conversation else []
        
        # Initialize display turn counter based on existing conversation
        # Count user messages (excluding system warnings) to get current turn number
        user_msg_count = 0
        for msg in self.conversation:
            if msg.get('role') == 'user':
                content = msg.get('content', '')
                # Don't count system warning messages
                if not content.startswith('[SYSTEM]'):
                    user_msg_count += 1
        self._display_turn = user_msg_count
        self._conversation_start_time = time.time()
        
        # Initialize modular components
        self.token_counter = TokenCounter(config)
        self.llm_client = LLMClient(config, session, self.logger)
        self.conversation_manager = ConversationManager(session, None, self.logger)  # context_builder set later
        self.debug_context = DebugContext(self.logger)
        
        # Tool setup
        self.tool_classes = config.tool_classes if config.tool_classes is not None else SIMPLIFIED_TOOL_CLASSES
        self.tool_definitions = [model_to_openai_tool(cls) for cls in self.tool_classes]
        
        # Initialize tool executor with security availability
        self.tool_executor = ToolExecutor(
            self.tool_classes, 
            config, 
            None,  # state will be set after initialization
            self.logger,
            self.security_available
        )
        
        # Initialize provider via LLMClient
        self.provider = self.llm_client.provider
        
        # Initialize runtime parameters
        self.runtime_params = RuntimeParams(
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            top_p=None  # not in config
        )
        
        # Initialize token encoder via TokenCounter
        self._token_encoder = None  # Managed by TokenCounter
        
        # Token totals - unified with session
        if session is not None:
            # Session is source of truth
            self._token_counts = {'input': session.total_input_tokens, 'output': session.total_output_tokens}
        else:
            # No session, use internal storage
            self._token_counts = {'input': config.initial_input_tokens, 'output': config.initial_output_tokens}
        
        # Ensure system prompt is present
        self.conversation = self.llm_client.ensure_system_prompt(self.conversation)
        
        # Initialize context builder
        max_context_tokens = self._get_max_context_tokens()
        self.context_builder = self.llm_client.create_context_builder(token_limit=max_context_tokens)
        if self.context_builder:
            self.conversation_manager.context_builder = self.context_builder
        
        # Stop check
        self.stop_check = config.stop_check
        
        # Keep-alive queue and flags
        self._next_query_queue = queue.Queue()
        self._paused = False
        self._should_reset = False
        self._pause_requested = False
        
        # Rate limiting state (to be moved to LLMClient in future)
        self.rate_limit_delay = 1.0  # seconds between turns when rate limited
        self.rate_limit_base_wait = 10.0  # initial wait when rate limit hit
        self.rate_limit_backoff_factor = 1.2  # multiply delay by this factor on repeated errors
        self.rate_limit_count = 0  # number of consecutive rate limit errors
        self.rate_limit_max_wait = 60.0  # maximum wait between turns
        self.rate_limit_active = False  # whether we're currently in rate limited mode
        
        # State management
        self.state = AgentState(self.config, self.logger)
        # Update tool executor with state
        self.tool_executor.state = self.state
        
        # Set session state based on whether we have existing history
        self._initialize_session_state()
        
        # Calculate initial token count for the conversation
        self._update_conversation_token_estimate()
    
    @property
    def session(self):
        """Get session property."""
        return self._session
    
    @session.setter
    def session(self, value):
        """Set session property, updating context_builder if needed."""
        self._session = value
        # Update security configuration from session
        if hasattr(self, 'state') and self.state is not None:
            if value is not None and hasattr(value, 'security_config'):
                self.state.security_config = value.security_config
            else:
                self.state.security_config = None
        # Update llm_client session
        if hasattr(self, 'llm_client') and self.llm_client is not None:
            self.llm_client.session = value
        # If context_builder exists and has session attribute, update it
        if hasattr(self, 'context_builder') and self.context_builder is not None and hasattr(self.context_builder, 'session'):
            self.context_builder.session = value
        # Create context_builder if it doesn't exist and we have a session
        elif value is not None and hasattr(self, 'llm_client') and self.llm_client is not None:
            # Create a new context_builder with the session
            self.context_builder = self.llm_client.create_context_builder()
            # Update conversation_manager.context_builder
            if hasattr(self, 'conversation_manager') and self.conversation_manager is not None:
                self.conversation_manager.context_builder = self.context_builder
        # Update conversation_manager session
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
            # Replace contents of session.user_history in-place to maintain reference
            self._session.user_history[:] = value
            self._session.updated_at = datetime.now()
            # Manually trigger conversation changed to update version/hash
            self._session._on_conversation_changed()
            # Invalidate HistoryProvider cache
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
        else:
            if self.conversation is not None and len(self.conversation) > 0:
                events = self.state.set_session_state(SessionState.CONTINUING)
                for event in events:
                    list(self._handle_state_event(event))
    
    
    def _handle_state_event(self, event):
        """Process a state event (e.g., token warning, turn warning).

        Events are dictionaries with 'type' field.
        For warning events, inject warning message into conversation.
        """
        if event.get("type") == "token_warning":
            # Note: token_warning events from AgentState have "message" field
            # Warning messages are already added to conversation in the main loop
            # (see user_query method), so we don't need to add them here.
            # Token counting is already done in the main loop.
            # Just yield the event for GUI display.
            pass
        elif event.get("type") == "turn_warning":
            # Note: turn_warning events from AgentState have "message" field
            # Warning messages are already added to conversation in the main loop
            # (see user_query method), so we don't need to add them here.
            # Token counting is already done in the main loop.
            # Just yield the event for GUI display.
            pass
        elif event.get("type") in ("token_critical_countdown_start", "turn_critical_countdown_start", "token_critical_countdown_expired", "turn_critical_countdown_expired"):
            # Handle countdown events - inject as system message
            message = event.get("message", "")
            sender = event.get("sender", "system")
            warning_msg = {"role": sender, "content": message}
            self._add_to_conversation(warning_msg)
            warning_tokens = self._estimate_tokens(warning_msg)
            self.state.current_conversation_tokens += warning_tokens
            # Emit token update event for real-time tracking
            yield self._create_token_update_event()
        elif event.get("type") == "execution_state_change":
            # Log execution state changes
            old_state = event.get("old_state")
            new_state = event.get("new_state")
            if self.logger:
                self.logger.py_logger.debug(
                    f"Execution state change: {old_state} -> {new_state}"
                )
            # Pass through to controller for GUI updates
            # Always add conversation data (version and history)
            self._add_conversation_data_to_event(event)
            yield event
        elif event.get("type") == "session_state_change":
            # Log session state changes
            old_state = event.get("old_state")
            new_state = event.get("new_state")
            if self.logger:
                self.logger.py_logger.debug(
                    f"Session state change: {old_state} -> {new_state}"
                )
            # Pass through to controller for GUI updates
            # Use full conversation data with version tracking
            self._add_conversation_data_to_event(event)
            yield event
        elif event.get("type") == "state_change":
            # Just log state changes for now
            if self.logger:
                self.logger.py_logger.debug(
                    f"State change: {event.get('old_state')} -> {event.get('new_state')}"
                )
            # Pass through to controller for GUI updates
            # Always add conversation data (version and history)
            self._add_conversation_data_to_event(event)
            yield event
    
    def _update_conversation_token_estimate(self):
        """Update current_conversation_tokens by estimating tokens for runtime context."""
        # Get runtime context from HistoryProvider (main prompt + latest summary + recent turns)
        if not hasattr(self, 'context_builder') or self.context_builder is None:
            runtime_context = self.conversation
        elif hasattr(self.context_builder, 'get_context_for_llm'):
            runtime_context = self.context_builder.get_context_for_llm()
        else:
            runtime_context = self.context_builder.build(self.conversation)
        
        # Clean orphaned tool messages for accurate token estimation
        original_len = len(runtime_context)
        runtime_context = ContextBuilder._cleanup_orphaned_tool_messages(runtime_context)
        if original_len != len(runtime_context):
            logger.warning(f'[DEBUG_CONTEXT] Token estimate: cleaned {original_len - len(runtime_context)} orphaned tool messages')
        
        estimated_tokens = 0
        for msg in runtime_context:
            estimated_tokens += self.token_counter.estimate_tokens(msg)
        
        self.state.current_conversation_tokens = estimated_tokens

        if DEBUG_PRUNING_AVAILABLE:
            log_token_count("Runtime context token estimate", estimated_tokens, f"from {len(runtime_context)}/{len(self.conversation)} messages")
        if self.logger and hasattr(self.logger, 'py_logger'):
            self.logger.py_logger.info(
                f"[TOKEN_ESTIMATE] Updated runtime context token estimate: {estimated_tokens} tokens (from {len(runtime_context)}/{len(self.conversation)} messages)"
            )    
    def _add_to_conversation(self, message):
        """Add a message via conversation_manager (ensures cache invalidation)."""
        pause_debug(f"_add_to_conversation called for {message.get('role')}...")
        pause_debug(f"Before add, conversation length: {len(self.conversation)}")
        # Delegate to conversation_manager
        updated = self.conversation_manager.add_message(message, self.conversation)
        # Update reference via property setter
        self.conversation = updated
        pause_debug(f"After add, conversation length: {len(self.conversation)}")
        # Invalidate context builder cache to ensure fresh snapshot for events
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
        event_dict = {
            "type": "token_update",
            "context_length": self.state.current_conversation_tokens,
            "usage": {
                "input": 0,
                "output": 0,
                "total_input": self.total_input_tokens,
                "total_output": self.total_output_tokens
            }
        }
        self._add_conversation_data_to_event(event_dict)
        yield event_dict
    
    # Token tracking properties (unified with session)
    @property
    def total_input_tokens(self):
        if self.session is not None:
            return self.session.total_input_tokens
        return self._token_counts['input']
    
    @total_input_tokens.setter
    def total_input_tokens(self, value):
        if self.session is not None:
            self.session.total_input_tokens = value
            self.session.context_length = self.session.total_input_tokens + self.session.total_output_tokens
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
            self.session.context_length = self.session.total_input_tokens + self.session.total_output_tokens
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
        # Base conversation data
        if self.session is not None:
            base_data = {
                'conversation_version': self.session.conversation_version,
                'conversation_hash': self.session.conversation_hash,
            }
        else:
            # No session, use local conversation with synthetic version
            import hashlib
            from session.utils import normalize_conversation_for_hash
            conv_str = normalize_conversation_for_hash(self.conversation)
            version_hash = hashlib.md5(conv_str.encode()).hexdigest()[:8]
            version = int(version_hash, 16) if version_hash else 0
            base_data = {
                'conversation_version': version,
                'conversation_hash': version_hash,
            }
        
        # Add required conversation metadata for GUI
        conversation_id = self.session_id if self.session_id else base_data.get('conversation_hash', '')
        conversation_timestamp = getattr(self, '_conversation_start_time', time.time())
        conversation_tokens = self.state.current_conversation_tokens
        conversation_turns = self.state.current_turn
        
        # Merge all data
        return {
            **base_data,
            'conversation_id': conversation_id,
            'conversation_timestamp': conversation_timestamp,
            'conversation_tokens': conversation_tokens,
            'conversation_turns': conversation_turns,
        }

    def _add_conversation_data_to_event(self, event: Dict[str, Any]) -> None:
        """Add conversation version and history to event."""
        # Add timestamp for chronological ordering
        current_time = time.time()
        event["created_at"] = current_time
        # Also add timestamp field for compatibility with GUI
        if "timestamp" not in event:
            event["timestamp"] = current_time
        
        # Add monotonic sequence number if session exists
        if self.session:
            event["seq"] = self.session._get_next_seq()
        
        conv_data = self._get_conversation_data_for_event()
        event.update(conv_data)

    def _create_token_update_event(self) -> dict:
        """Create token update event."""
        event = {
            "type": "token_update",
            "context_length": self.state.current_conversation_tokens,
            "total_input": self.total_input_tokens,
            "total_output": self.total_output_tokens
        }
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
        from agent.config import AgentConfig  # Import here to avoid circular imports
        
        # Validate config
        if not isinstance(new_config, AgentConfig):
            raise TypeError(f"new_config must be AgentConfig, got {type(new_config)}")
        
        # Preserve conversation history
        current_conversation = self.conversation.copy()
        
        # Preserve token counts
        token_counts = self._token_counts.copy()
        
        # Reset execution state to IDLE
        self.state.set_execution_state(ExecutionState.IDLE)
        
        # Clear input queue
        self._next_query_queue = queue.Queue()
        
        # Reset pause flags
        self._paused = False
        self._pause_requested = False
        self._should_reset = False
        
        # Update config reference
        self.config = new_config
        
        # Reinitialize components with new config
        self.token_counter = TokenCounter(new_config)
        self.llm_client = LLMClient(new_config, self.session, self.logger)
        self.provider = self.llm_client.provider
        
        # Update runtime parameters
        self.runtime_params = RuntimeParams(
            temperature=new_config.temperature,
            max_tokens=new_config.max_tokens,
            top_p=None  # not in config
        )
        
        # Update tool classes
        self.tool_classes = new_config.tool_classes if new_config.tool_classes is not None else SIMPLIFIED_TOOL_CLASSES
        self.tool_definitions = [model_to_openai_tool(cls) for cls in self.tool_classes]
        
        # Recreate tool executor
        self.tool_executor = ToolExecutor(
            self.tool_classes,
            new_config,
            self.state,  # state remains the same
            self.logger,
            self.security_available
        )
        
        # Recreate context builder
        max_context_tokens = self._get_max_context_tokens()
        self.context_builder = self.llm_client.create_context_builder(token_limit=max_context_tokens)
        if self.context_builder:
            self.conversation_manager.context_builder = self.context_builder
        
        # Re-ensure system prompt with new LLM client
        self.conversation = self.llm_client.ensure_system_prompt(current_conversation)
        
        # Restore token counts
        self._token_counts = token_counts
        
        # Reset rate limiting state
        self.reset_rate_limiting()
        
        # Log restart
        if self.logger:
            self.logger.log_info("AGENT_RESTART", f"Configuration reloaded, provider: {self.provider}")

    def request_pause(self):
        """Request pause at the next atomic turn boundary."""
        pause_debug(f"request_pause called, setting _pause_requested=True")
        self._pause_requested = True

    def process_query(self, query):
        """Process a user query, appending it to conversation and running the agent.
        Yields events as dicts."""
        pause_debug(f"process_query called with query: '{query[:50]}...'")
        pause_debug(f"Current execution state: {self.state.execution_state}")
        pause_debug(f"Conversation length before adding query: {len(self.conversation)}")
        pause_debug(f"context_builder exists: {self.context_builder is not None}")
        if self.context_builder and hasattr(self.context_builder, 'session'):
            pause_debug(f"context_builder.session: {self.context_builder.session}")
            if self.context_builder.session:
                pause_debug(f"context_builder.session.session_id: {self.context_builder.session.session_id}")
        pause_debug(f"agent.session: {self.session}")
        if self.session:
            pause_debug(f"agent.session.session_id: {self.session.session_id}")
        # Ensure system prompt present
        self.conversation = self.llm_client.ensure_system_prompt(self.conversation)

        # Clear any pending pause request when starting new query
        pause_debug(f"Clearing _pause_requested (was {self._pause_requested})")
        self._pause_requested = False
        
        # Update execution state based on current state
        current_exec_state = self.state.execution_state
        if current_exec_state == ExecutionState.RUNNING:
            # This shouldn't happen, but handle gracefully
            if self.logger:
                self.logger.log_error("EXECUTION_STATE", "process_query called while already RUNNING")
        elif current_exec_state in (ExecutionState.PAUSED, ExecutionState.WAITING_FOR_USER):
            # Resuming from pause or user interaction - go directly to RUNNING
            events = self.state.set_execution_state(ExecutionState.RUNNING)
            for event in events:
                for yielded_event in self._handle_state_event(event):
                    yield yielded_event
        else:
            # Starting from IDLE, STOPPED, FINALIZED, or MAX_TURNS_REACHED
            events = self.state.set_execution_state(ExecutionState.RUNNING)
            for event in events:
                for yielded_event in self._handle_state_event(event):
                    yield yielded_event
        
        # Log agent start if logger exists
        if self.logger:
            config_data = {
                "model": self.config.model,
                "temperature": self.config.temperature,
                "max_turns": self.config.max_turns,
                "max_history_turns": self.config.max_history_turns,
                "max_tokens": self.config.max_tokens,
                "keep_initial_query": self.config.keep_initial_query,
                "keep_system_messages": self.config.keep_system_messages,
            }
            self.logger.log_agent_start(query, config_data)
            # Log initial system resources
            self.logger.log_system_resources()
        # Append user message
        pause_debug(f"Adding user message to conversation: '{query[:50]}...'")
        user_msg = {"role": "user", "content": query}
        self._add_to_conversation(user_msg)
        pause_debug(f"After adding user message, conversation length: {len(self.conversation)}")
        # Estimate tokens for the new user message (including JSON structure) and update current count
        estimated_tokens = self._estimate_tokens(user_msg)
        self.state.current_conversation_tokens += estimated_tokens
        # Emit token update event for real-time tracking
        yield self._create_token_update_event()
        
        # Increment display turn counter for this query
        self._display_turn = getattr(self, '_display_turn', 0) + 1
        
        # Emit user query event for GUI display
        debug_log(f"query='{query[:50]}...', _display_turn={self._display_turn}", level="WARNING", component="Agent.user_query")
        event_dict = {
            "type": "user_query",
            "content": query,
            "turn": self._display_turn  # Unique turn number for this query
        }
        self._add_conversation_data_to_event(event_dict)
        # Ensure event has all fields GUI expects
        if "timestamp" not in event_dict:
            event_dict["timestamp"] = event_dict.get("created_at", time.time())
        yield event_dict

        prev_conversation_len = len(self.conversation)
        last_input_tokens = 0
        last_output_tokens = 0
        
        for turn in range(self.config.max_turns):
            # Log turn start
            turn_start_time = time.time()
            if self.logger:
                self.logger.log_turn_start(turn)
                # Log system resources every 5 turns to monitor performance
                if turn % 5 == 0:
                    self.logger.log_system_resources()
            
            # Decrement critical countdowns
            countdown_events = self.state.decrement_critical_countdown()
            for event in countdown_events:
                for yielded_event in self._handle_state_event(event):
                    yield yielded_event
            
            # Check stop signal
            if self.stop_check and self.stop_check():
                # Update execution state: transition through PAUSING intermediate state
                events = self.state.set_execution_state(ExecutionState.PAUSING)
                for event in events:
                    for yielded_event in self._handle_state_event(event):
                        yield yielded_event
                # Then transition to PAUSED
                events = self.state.set_execution_state(ExecutionState.PAUSED)
                for event in events:
                    for yielded_event in self._handle_state_event(event):
                        yield yielded_event

                if self.logger:
                    self.logger.log_stop_signal()
                    self.logger.log_system_resources()
                    self.logger.log_agent_end("stopped", "Stop signal received")
                    self.logger.close()
                stopped_event = {
                    "type": "stopped",
                    "turn": self._display_turn,
                    "context_length": self.state.current_conversation_tokens,
                    "usage": {"input": last_input_tokens, "output": last_output_tokens,
                              "total_input": self.total_input_tokens, "total_output": self.total_output_tokens}
                }
                self._add_conversation_data_to_event(stopped_event)
                yield stopped_event
                return            
            # Turn monitoring warning
            turn_events = self.state.update_turn_state(turn)
            for event in turn_events:
                if event["type"] == "turn_warning":
                    # Add warning message to conversation as system message
                    warning_msg = {"role": "system", "content": event.get("message", event.get("warning", ""))}
                    self._add_to_conversation(warning_msg)
                    # Update token count for warning message
                    warning_tokens = self._estimate_tokens(warning_msg)
                    self.state.current_conversation_tokens += warning_tokens
                    # Emit token update event for real-time tracking
                    yield self._create_token_update_event()
                # Yield event with usage info
                event_dict = {
                    "type": event["type"],
                    "message": event.get("message", event.get("warning", "")),
                    "turn_count": event.get("turn_count", turn),
                    "turn": self._display_turn,  # Add turn for GUI grouping
                    "context_length": self.state.current_conversation_tokens,
                    "usage": {
                        "input": last_input_tokens,
                        "output": last_output_tokens,
                        "total_input": self.total_input_tokens,
                        "total_output": self.total_output_tokens,
                    }
                }
                self._add_conversation_data_to_event(event_dict)
                yield event_dict

            # Update conversation token estimate from scratch to prevent drift
            self._update_conversation_token_estimate()
            # Emit token update event for real-time tracking after token estimate update
            yield self._create_token_update_event()
            # Token monitoring warning
            token_events = self.state.update_token_state(self.state.current_conversation_tokens)
            for event in token_events:
                if event["type"] == "token_warning":
                    # Add warning message to conversation as system message
                    warning_msg = {"role": "system", "content": event.get("message", event.get("warning", ""))}
                    self._add_to_conversation(warning_msg)
                    # Update token count for warning message
                    warning_tokens = self._estimate_tokens(warning_msg)
                    self.state.current_conversation_tokens += warning_tokens
                    # Emit token update event for real-time tracking
                    yield self._create_token_update_event()
                # Yield event with usage info
                event_dict = {
                    "type": event["type"],
                    "message": event.get("message", event.get("warning", "")),
                    "token_count": event.get("token_count", self.state.current_conversation_tokens),
                    "turn": self._display_turn,  # Add turn for GUI grouping
                    "context_length": self.state.current_conversation_tokens,
                    "usage": {
                        "input": last_input_tokens,
                        "output": last_output_tokens,
                        "total_input": self.total_input_tokens,
                        "total_output": self.total_output_tokens,
                    }
                }
                self._add_conversation_data_to_event(event_dict)
                yield event_dict
            
            # Ensure any assistant message with tool_calls has reasoning_content field
            for msg in self.conversation:
                if msg.get("role") == "assistant" and "tool_calls" in msg:
                    if msg.get("reasoning_content") is None:
                        msg["reasoning_content"] = ""
            
            # Log conversation state before building context
            if self.logger and hasattr(self.logger, 'py_logger'):
                system_msgs = [msg for msg in self.conversation if msg.get("role") == "system"]
                self.logger.py_logger.info(f"[CONVERSATION] Total messages: {len(self.conversation)}, system messages: {len(system_msgs)}")
            
            # Build the context for the LLM using the configured builder
            # Calculate maximum tokens available for context (input)
            max_context_tokens = self._get_max_context_tokens()
            if self.logger and hasattr(self.logger, 'py_logger'):
                self.logger.py_logger.info(f"[CONTEXT] Max context tokens: {max_context_tokens}, model: {self.config.model}")
            
            # Debug context monitoring
            self.debug_context.debug_context("before_build", context_builder=self.context_builder)
            
            if hasattr(self, 'context_builder') and self.context_builder is not None:
                messages = self.context_builder.build(
                    self.conversation,
                    max_tokens=max_context_tokens
                )
            else:
                messages = self.conversation
            
            # Debug context monitoring: show runtime context
            self.debug_context.debug_context("after_build", messages=messages, context_builder=self.context_builder)
            
            # Debug: print messages being sent to LLM
            pause_debug(f"Messages being sent to LLM ({len(messages)}):")
            for i, msg in enumerate(messages):
                role = msg.get('role', 'unknown')
                content_preview = str(msg.get('content', ''))[:100]
                pause_debug(f"  [{i}] {role}: {content_preview}...")
            
            # Final safety cleanup: remove any orphaned tool messages that might have slipped through
            original_len = len(messages)
            messages = ContextBuilder._cleanup_orphaned_tool_messages(messages)
            if original_len != len(messages):
                logger.warning(f'[DEBUG_CONTEXT] Agent: cleaned {original_len - len(messages)} orphaned tool messages from final context')
            
            if self.logger and hasattr(self.logger, 'py_logger'):
                # Estimate token count for messages
                # TODO: Use token_counter
                import tiktoken
                try:
                    encoder = tiktoken.get_encoding("cl100k_base")
                except Exception:
                    encoder = None
                total_tokens = sum(self.token_counter.estimate_tokens(msg) for msg in messages)
                self.logger.py_logger.info(f"[CONTEXT] Built context: {len(messages)} messages, ~{total_tokens} tokens")
            
            # Log LLM request
            if self.logger:
                self.logger.log_llm_request(messages, self.tool_definitions)
            
            # Apply rate limiting delay if active
            if self.rate_limit_active:
                delay = min(self.rate_limit_delay, self.rate_limit_max_wait)
                if delay > 0:
                    if self.logger and hasattr(self.logger, 'py_logger'):
                        self.logger.py_logger.info(f"[RATE_LIMIT] Applying rate limit delay: {delay}s between turns")

                    time.sleep(delay)
            
            # Format tools
            tools = self.llm_client.format_tools(self.tool_definitions)

            # Estimate request tokens and check against model context window
            request_tokens = self.token_counter.estimate_request_tokens(messages, tools)
            # Get model context window (approximate)
            model_context_window = self.token_counter.get_model_context_window()
            critical_threshold = int(model_context_window * 0.95)  # 95% of context window
            warning_threshold = int(model_context_window * 0.85)  # 85% of context window
            
            if request_tokens > model_context_window:
                # Cannot proceed - request exceeds model context window
                error = f"[SYSTEM] Request token count ({request_tokens}) exceeds model context window ({model_context_window}). Cannot make API call. Please use SummarizeTool to reduce context size."
                error_msg = {"role": "system", "content": error}
                self._add_to_conversation(error_msg)
                error_tokens = self._estimate_tokens(error_msg)
                self.state.current_conversation_tokens += error_tokens
                # Emit token update event for real-time tracking
                yield self._create_token_update_event()
                # Yield error event
                event_dict = {
                    "type": "token_warning",
                    "message": error,
                    "token_count": request_tokens,
                    "old_state": "low",
                    "new_state": "critical",
                    "state": "critical",
                    "request_tokens": request_tokens,
                    "model_context_window": model_context_window
                }
                self._add_conversation_data_to_event(event_dict)
                yield event_dict
                # Still attempt API call but it will likely fail
                if self.logger:
                    self.logger.log_token_warning(
                        "low", "critical", request_tokens,
                        f"Request tokens {request_tokens} exceed model context window {model_context_window}"
                    )
            elif request_tokens > critical_threshold:
                # Critical warning - near context limit
                warning = f"[SYSTEM] Request token count ({request_tokens}) is near model context window limit ({model_context_window}). Please use SummarizeTool immediately to reduce context size."
                warning_msg = {"role": "system", "content": warning}
                self._add_to_conversation(warning_msg)
                warning_tokens = self._estimate_tokens(warning_msg)
                self.state.current_conversation_tokens += warning_tokens
                # Emit token update event for real-time tracking
                yield self._create_token_update_event()
                # Yield token warning event
                event_dict = {
                    "type": "token_warning",
                    "message": warning,
                    "token_count": request_tokens,
                    "old_state": "low",
                    "new_state": "critical",
                    "state": "critical",
                    "request_tokens": request_tokens,
                    "model_context_window": model_context_window
                }
                self._add_conversation_data_to_event(event_dict)
                yield event_dict
                if self.logger:
                    self.logger.log_token_warning(
                        "low", "critical", request_tokens,
                        f"Request tokens {request_tokens} near model context window {model_context_window}"
                    )
            elif request_tokens > warning_threshold:
                # Warning - approaching context limit
                warning = f"[SYSTEM] Request token count ({request_tokens}) is approaching model context window ({model_context_window}). Consider using SummarizeTool soon."
                warning_msg = {"role": "system", "content": warning}
                self._add_to_conversation(warning_msg)
                warning_tokens = self._estimate_tokens(warning_msg)
                self.state.current_conversation_tokens += warning_tokens
                # Emit token update event for real-time tracking
                yield self._create_token_update_event()
                # Yield token warning event
                event_dict = {
                    "type": "token_warning",
                    "message": warning,
                    "token_count": request_tokens,
                    "old_state": "low",
                    "new_state": "warning",
                    "state": "warning",
                    "request_tokens": request_tokens,
                    "model_context_window": model_context_window
                }
                self._add_conversation_data_to_event(event_dict)
                yield event_dict
                if self.logger:
                    self.logger.log_token_warning(
                        "low", "warning", request_tokens,
                        f"Request tokens {request_tokens} approaching model context window {model_context_window}"
                    )
            
            try:
                # Build chat kwargs using runtime parameters (overrides config)
                chat_kwargs = {
                    "temperature": self.runtime_params.temperature,
                }
                if self.runtime_params.max_tokens is not None:
                    chat_kwargs["max_tokens"] = self.runtime_params.max_tokens
                if self.runtime_params.top_p is not None:
                    chat_kwargs["top_p"] = self.runtime_params.top_p
                
                # Measure LLM latency
                llm_start_time = time.time()
                response = self.llm_client.chat_completion(
                    messages=messages,
                    tools=tools if tools else None,
                    **chat_kwargs
                )
                llm_duration_ms = (time.time() - llm_start_time) * 1000
                
                # Log LLM latency
                if self.logger:
                    self.logger.log_latency("llm_call", llm_duration_ms, {
                        "turn": turn,
                        "request_tokens": request_tokens,
                        "model": self.config.model,
                        "has_tools": bool(tools)
                    })
                
                # Update input/output token totals
                input_tokens = response.usage.get('prompt_tokens', 0) if response.usage else 0
                output_tokens = response.usage.get('completion_tokens', 0) if response.usage else 0
                last_input_tokens = input_tokens
                last_output_tokens = output_tokens
                self.total_input_tokens += input_tokens
                self.total_output_tokens += output_tokens
                
            except RateLimitExceeded as e:
                # Handle rate limit errors with exponential backoff
                # Increment rate limit count and calculate delays
                self.rate_limit_count += 1
                self.rate_limit_active = True
                
                # Increase delay for future turns (exponential backoff)
                if self.rate_limit_count > 1:
                    self.rate_limit_delay = min(
                        self.rate_limit_delay * self.rate_limit_backoff_factor,
                        self.rate_limit_max_wait
                    )
                
                # Initial wait (10 seconds)
                wait_time = self.rate_limit_base_wait
                if self.logger:
                    self.logger.log_error("RATE_LIMIT", f"Rate limit exceeded, waiting {wait_time}s, delay between turns: {self.rate_limit_delay}s")
                
                # Yield rate limit warning event
                event_dict = {
                    "type": "rate_limit_warning",
                    "message": f"Rate limit exceeded. Waiting {wait_time}s before retrying. Delay between turns: {self.rate_limit_delay}s",
                    "wait_time": wait_time,
                    "turn_delay": self.rate_limit_delay,
                    "rate_limit_count": self.rate_limit_count,
                    "turn": self._display_turn,
                }
                self._add_conversation_data_to_event(event_dict)
                yield event_dict
                
                # Wait the initial wait time

                time.sleep(wait_time)
                
                # Continue to next turn with delay between turns
                break
                
            except (ProviderError, LLMError) as e:
                # Handle provider errors (including LLMError for provider-independent errors)
                # Update execution state to STOPPED (error state)
                events = self.state.set_execution_state(ExecutionState.STOPPED)
                for event in events:
                    for yielded_event in self._handle_state_event(event):
                        yield yielded_event
                
                # Determine error type
                error_type = "PROVIDER_ERROR"
                if isinstance(e, LLMError):
                    error_type = e.error_type.upper()  # e.g., "AUTHENTICATION_ERROR"
                
                if self.logger:
                    self.logger.log_error(error_type, str(e))
                    self.logger.log_system_resources()
                    self.logger.log_agent_end("provider_error", f"Provider error: {e}")
                    self.logger.close()
                event_dict = {
                    "type": "error",
                    "error_type": error_type,
                    "message": str(e),
                    "traceback": traceback.format_exc(),
                    "turn": self._display_turn,
                    "context_length": self.state.current_conversation_tokens,
                    "usage": {
                        "input": last_input_tokens,
                        "output": last_output_tokens,
                        "total_input": self.total_input_tokens,
                        "total_output": self.total_output_tokens,
                    }
                }
                self._add_conversation_data_to_event(event_dict)
                yield event_dict
                return
            except Exception as e:
                # Catch any other unexpected exception
                logger.exception(f"[Agent] Unexpected exception in process_query: {e}")

                # Update execution state to STOPPED (error state)
                events = self.state.set_execution_state(ExecutionState.STOPPED)
                for event in events:
                    for yielded_event in self._handle_state_event(event):
                        yield yielded_event
                if self.logger:
                    self.logger.log_error("UNEXPECTED_ERROR", str(e))
                    self.logger.log_system_resources()
                    self.logger.log_agent_end("unexpected_error", f"Unexpected error: {e}")
                    self.logger.close()
                event_dict = {
                    "type": "error",
                    "error_type": "UNEXPECTED_ERROR",
                    "message": str(e),
                    "traceback": traceback.format_exc(),
                    "turn": self._display_turn,
                    "context_length": self.state.current_conversation_tokens,
                    "usage": {
                        "input": last_input_tokens,
                        "output": last_output_tokens,
                        "total_input": self.total_input_tokens,
                        "total_output": self.total_output_tokens,
                    }
                }
                self._add_conversation_data_to_event(event_dict)
                yield event_dict
                return
            
            # Extract response content, reasoning, and tool calls from LLMResponse
            content = response.content or ""
            reasoning = response.reasoning
            tool_calls = response.tool_calls
            
            # Initialize user interaction message variable
            user_interaction_message = None
            
            # Check for pause request before starting turn
            pause_debug(f"Checking pause request before turn: _pause_requested={self._pause_requested}")
            if self._pause_requested:
                pause_debug(f"Pause detected! Transitioning to PAUSING then PAUSED")
                # Update execution state: transition through PAUSING intermediate state
                events = self.state.set_execution_state(ExecutionState.PAUSING)
                for event in events:
                    for yielded_event in self._handle_state_event(event):
                        yield yielded_event
                # Then transition to PAUSED
                events = self.state.set_execution_state(ExecutionState.PAUSED)
                for event in events:
                    for yielded_event in self._handle_state_event(event):
                        yield yielded_event
                # Clear pause request flag
                pause_debug(f"Clearing _pause_requested after pause")
                self._pause_requested = False
                # Yield pause event
                pause_event = {
                    "type": "paused",
                    "turn": self._display_turn,
                    "context_length": self.state.current_conversation_tokens,
                }
                self._add_conversation_data_to_event(pause_event)
                yield pause_event
                # Log turn completion
                turn_duration = time.time() - turn_start_time
                if self.logger:
                    self.logger.log_system_resources()
                    self.logger.log_turn_complete(turn, {
                        "input": last_input_tokens,
                        "output": last_output_tokens,
                        "duration_ms": turn_duration * 1000,
                        "context_tokens": self.state.current_conversation_tokens
                    })
                return
            

            
            # Create turn transaction for atomic buffering of the turn (assistant message + tool results)
            turn_transaction = TurnTransaction(self.session, self.context_builder)
            
            # Add assistant message to transaction (buffered)
            assistant_msg = {"role": "assistant", "content": content}
            if reasoning is not None:
                assistant_msg["reasoning_content"] = reasoning
            elif tool_calls:
                assistant_msg["reasoning_content"] = ""  # Empty string for tool calls without reasoning
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            turn_transaction.add_assistant_message(assistant_msg)
            
            # Update token estimate with assistant message
            for event in self._update_tokens_and_yield():
                yield event            
            # Yield turn event (GUI expects "turn" type)
            # Note: tool_calls are emitted as separate events, so we set tool_calls to empty list
            turn_event = {
                "type": "turn",
                "content": content,
                "assistant_content": content,  # For display in EventDelegate
                "tool_calls": [],  # Tool calls emitted as separate events
                "turn": self._display_turn,  # Use display turn for grouping
                "context_length": self.state.current_conversation_tokens,
                "usage": {
                    "input": last_input_tokens,
                    "output": last_output_tokens,
                    "total_input": self.total_input_tokens,
                    "total_output": self.total_output_tokens
                }
            }
            if reasoning is not None:
                turn_event["reasoning"] = reasoning
            elif tool_calls:
                turn_event["reasoning"] = ""  # Empty string for tool calls without reasoning
            self._add_conversation_data_to_event(turn_event)
            yield turn_event
            
            # Execute tool calls if present
            if tool_calls:
                # Execute tool calls
                executed_tools, final_detected, final_content, user_interaction_message, summary_text, summary_keep_recent_turns = self.tool_executor.execute_tool_calls(
                    tool_calls,
                    add_to_conversation_func=self._add_to_conversation,
                    update_token_func=self._update_tokens_and_yield,
                    agent_id=0,
                    turn_transaction=turn_transaction
                )
                
                # Process tools and determine success/error
                processed_tools = []
                for tool_info in executed_tools:
                    result = tool_info.get("result", "")
                    success = True
                    error = None
                    if isinstance(result, str):
                        if result.startswith("❌") or "TOOL CALL REJECTED" in result or "Error executing tool" in result:
                            success = False
                            error = result
                    
                    processed_tools.append({
                        "name": tool_info.get("name"),
                        "arguments": tool_info.get("arguments"),
                        "result": result,
                        "success": success,
                        "error": error,
                        "turn": self._display_turn  # Use display turn for grouping
                    })
                
                # Yield tool call events (without results)
                for tool in processed_tools:
                    event_dict = {
                        "type": "tool_call",
                        "tool_name": tool["name"],
                        "arguments": tool["arguments"],
                        "success": tool["success"],
                        "error": tool["error"],
                        "turn": tool["turn"]
                    }
                    self._add_conversation_data_to_event(event_dict)
                    yield event_dict
                
                # Yield tool result events
                for tool in processed_tools:
                    event_dict = {
                        "type": "tool_result",
                        "tool_name": tool["name"],
                        "result": tool["result"],
                        "success": tool["success"],
                        "error": tool["error"],
                        "turn": tool["turn"]
                    }
                    self._add_conversation_data_to_event(event_dict)
                    yield event_dict
                
                # Commit buffered tool results
                if turn_transaction:
                    turn_transaction.commit()
                
                # Handle final detection
                if final_detected:
                    # Update execution state to FINALIZED (completed with Final/FinalReport)
                    events = self.state.set_execution_state(ExecutionState.FINALIZED)
                    for event in events:
                        for yielded_event in self._handle_state_event(event):
                            yield yielded_event

                    # Yield final event
                    final_event = {
                        "type": "final",
                        "content": final_content if final_content is not None else content,
                        "turn": self._display_turn,
                        "context_length": self.state.current_conversation_tokens,
                        "usage": {
                            "input": last_input_tokens,
                            "output": last_output_tokens,
                            "total_input": self.total_input_tokens,
                            "total_output": self.total_output_tokens
                        }
                    }
                    if reasoning is not None:
                        final_event["reasoning"] = reasoning
                    elif tool_calls:
                        final_event["reasoning"] = ""  # Empty string for tool calls without reasoning
                    self._add_conversation_data_to_event(final_event)
                    yield final_event

                    # Log turn completion
                    turn_duration = time.time() - turn_start_time
                    if self.logger:
                        self.logger.log_system_resources()
                        self.logger.log_turn_complete(turn, {
                            "input": last_input_tokens,
                            "output": last_output_tokens,
                            "duration_ms": turn_duration * 1000,
                            "context_tokens": self.state.current_conversation_tokens
                        })
                    return
                
                # Handle user interaction request
                if user_interaction_message is not None:
                    # Update execution state to WAITING_FOR_USER (waiting for user input)
                    events = self.state.set_execution_state(ExecutionState.WAITING_FOR_USER)
                    for event in events:
                        for yielded_event in self._handle_state_event(event):
                            yield yielded_event

                    # Log turn completion
                    turn_duration = time.time() - turn_start_time
                    if self.logger:
                        self.logger.log_system_resources()
                        self.logger.log_turn_complete(turn, {
                            "input": last_input_tokens,
                            "output": last_output_tokens,
                            "duration_ms": turn_duration * 1000,
                            "context_tokens": self.state.current_conversation_tokens
                        })
                    return
                
                # Handle summary request
                if summary_text is not None:
                    # Apply summary pruning
                    if DEBUG_PRUNING_AVAILABLE:
                        log_summary_operation(f"Processing summary request: summary length={len(summary_text)}, keep_recent_turns={summary_keep_recent_turns}")
                    self._apply_summary_pruning(summary_text, summary_keep_recent_turns)
                    # Update token estimate after pruning
                    for event in self._update_tokens_and_yield():
                        yield event
            
            # Check pause request
            pause_debug(f"Checking pause request after turn processing: _pause_requested={self._pause_requested}")
            if self._pause_requested:
                pause_debug(f"Pause detected after turn processing! Transitioning to PAUSING then PAUSED")
                # Update execution state: transition through PAUSING intermediate state
                events = self.state.set_execution_state(ExecutionState.PAUSING)
                for event in events:
                    for yielded_event in self._handle_state_event(event):
                        yield yielded_event
                # Then transition to PAUSED
                events = self.state.set_execution_state(ExecutionState.PAUSED)
                for event in events:
                    for yielded_event in self._handle_state_event(event):
                        yield yielded_event
                # Clear pause request flag
                pause_debug(f"Clearing _pause_requested after pause (after turn processing)")
                self._pause_requested = False
                # Yield pause event
                pause_event = {
                    "type": "paused",
                    "turn": self._display_turn,
                    "context_length": self.state.current_conversation_tokens,
                }
                self._add_conversation_data_to_event(pause_event)
                yield pause_event
                # Log turn completion
                turn_duration = time.time() - turn_start_time
                if self.logger:
                    self.logger.log_system_resources()
                    self.logger.log_turn_complete(turn, {
                        "input": last_input_tokens,
                        "output": last_output_tokens,
                        "duration_ms": turn_duration * 1000,
                        "context_tokens": self.state.current_conversation_tokens
                    })
                return
            
            # Check if we should continue (no tool calls, or tool calls didn't result in user interaction/final)
            if not tool_calls:
                # No tool calls means the assistant gave a direct answer - we can stop
                if turn_transaction and turn_transaction.has_assistant_message():
                    turn_transaction.commit()
                # Update execution state: transition through PAUSING intermediate state
                events = self.state.set_execution_state(ExecutionState.PAUSING)
                for event in events:
                    for yielded_event in self._handle_state_event(event):
                        yield yielded_event
                # Then transition to PAUSED
                events = self.state.set_execution_state(ExecutionState.PAUSED)
                for event in events:
                    for yielded_event in self._handle_state_event(event):
                        yield yielded_event
                if self.logger:
                    self.logger.log_system_resources()
                    self.logger.log_agent_end("completed", "Assistant provided direct answer with no tool calls")
                    self.logger.close()

                final_event = {
                    "type": "final",
                    "content": content,
                    "turn": self._display_turn,
                    "context_length": self.state.current_conversation_tokens,
                    "usage": {
                        "input": last_input_tokens,
                        "output": last_output_tokens,
                        "total_input": self.total_input_tokens,
                        "total_output": self.total_output_tokens
                    }
                }
                if reasoning is not None:
                    final_event["reasoning"] = reasoning
                elif tool_calls:
                    final_event["reasoning"] = ""  # Empty string for tool calls without reasoning
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
        debug_log('pruning', f"_apply_summary_pruning called with summary length={len(summary)}, keep_recent_turns={keep_recent_turns}")
        debug_log('session_history', f"self.session exists: {self.session is not None}")
        
        if self.session is None:
            # No session - fallback to old behavior (modify conversation directly)
            logger.warning("[DEBUG_PRUNING] No session available, using fallback pruning")
            debug_log('pruning', "No session available, using fallback pruning")
            self._apply_summary_pruning_fallback(summary, keep_recent_turns)
            # Update token estimate after fallback pruning
            old_token_count = self.state.current_conversation_tokens
            self._update_conversation_token_estimate()
            if DEBUG_PRUNING_AVAILABLE:
                log_pruning_operation("Fallback pruning", old_token_count, self.state.current_conversation_tokens)
            if self.logger and hasattr(self.logger, 'py_logger'):
                self.logger.py_logger.info(
                    f"[PRUNING] Updated token estimate after fallback: {self.state.current_conversation_tokens} tokens (was {old_token_count})"
                )
            return
        
        # Use the full append-only history
        user_history = self.session.user_history
        debug_log('session_history', f"session.user_history length: {len(user_history)}")
        debug_log('session_history', f"session.summary set: {self.session.summary is not None}")
        
        # Find insertion index where summary should be inserted
        # This is the index in user_history where the first kept turn begins
        # We need to scan the history and count turns, ignoring system messages
        insertion_idx = self._find_summary_insertion_index(user_history, keep_recent_turns)
        debug_log('pruning', f"Computed insertion_idx={insertion_idx} for keep_recent_turns={keep_recent_turns}")

        # For logging: compute turns count
        other_messages = [msg for msg in user_history if msg.get("role") != "system"]
        turns = self._group_messages_into_turns(other_messages) if other_messages else []
        kept_turns_count = min(keep_recent_turns, len(turns)) if keep_recent_turns > 0 else 0
        debug_log('pruning', f"Found {len(turns)} turns total, keeping {kept_turns_count} turns")
        
        # Count discarded messages for metadata
        if insertion_idx >= len(user_history):
            discarded_msg_count = len(user_history)
        else:
            # Count non-system messages before insertion_idx
            discarded_msg_count = 0
            for i in range(insertion_idx):
                if user_history[i].get("role") != "system":
                    discarded_msg_count += 1        
        # Create summary system message with metadata
        MAX_SUMMARY_LENGTH = 4000
        truncated_summary = summary
        if len(truncated_summary) > MAX_SUMMARY_LENGTH:
            truncated_summary = truncated_summary[:MAX_SUMMARY_LENGTH] + "... (truncated)"
        
        summary_msg = {
            "role": "system",
            "content": f"Summary of previous conversation: {truncated_summary}",
            "summary": True,
            "pruning_keep_recent_turns": keep_recent_turns,
            "pruning_discarded_msg_count": discarded_msg_count,
            "pruning_insertion_idx": insertion_idx,
        }
        
        # Insert summary message into user_history at the computed position
        # This preserves all messages (append-only) but adds the summary marker
        if insertion_idx >= len(user_history):
            # Append at the end (should not happen with valid indices)
            user_history.append(summary_msg)
            debug_log('message_insertion', f"Appended summary at end (insertion_idx={insertion_idx} >= len={len(user_history)})")
        else:
            user_history.insert(insertion_idx, summary_msg)
            debug_log('message_insertion', f"Inserted summary at index {insertion_idx}")
        
        # Update session.summary field
        self.session.summary = summary_msg
        self.session.updated_at = datetime.now()
        
        # Ensure agent.conversation references the full history (with new summary)
        if self.conversation is not user_history:
            self.conversation = user_history
        
        # Clear HistoryProvider cache since we modified user_history directly
        if hasattr(self, 'context_builder') and self.context_builder is not None and hasattr(self.context_builder, '_cached_context'):
            self.context_builder._cached_context = None
        
        # Log the pruning action
        if self.logger:
            self.logger.log_conversation_prune(
                len(user_history) - 1,  # original length before adding summary
                len(user_history),      # new length with summary
                "summary_pruning_append_only"
            )
        
        # Debug logging
        if DEBUG_PRUNING_AVAILABLE:
            log_summary_operation(f"Added summary to append-only history: kept {kept_turns_count} turns, inserted at index {insertion_idx}")
        if self.logger and hasattr(self.logger, 'py_logger'):
            self.logger.py_logger.info(
                f"[PRUNING] Added summary to append-only history: kept {kept_turns_count} turns, "
                f"inserted summary at index {insertion_idx}, "
                f"history length: {len(user_history)} messages"
            )
        
        # Update token estimate (will use HistoryProvider to compute runtime context tokens)
        old_token_count = self.state.current_conversation_tokens
        self._update_conversation_token_estimate()
        # Emit token update event for real-time tracking after pruning
        # Note: This is called from process_query, so we need to yield
        # but _apply_summary_pruning is not a generator.
        # The caller will yield the event.
        # Instead, we'll rely on process_query to emit after summary
        if DEBUG_PRUNING_AVAILABLE:
            log_pruning_operation("Summary pruning", old_token_count, self.state.current_conversation_tokens, summary_idx=insertion_idx, kept_turns=kept_turns_count)
        if self.logger and hasattr(self.logger, 'py_logger'):
            self.logger.py_logger.info(
                f"[PRUNING] Updated token estimate: {self.state.current_conversation_tokens} tokens (was {old_token_count})"
            )
        
        debug_log('pruning', f"_apply_summary_pruning completed. History length: {len(user_history)} messages")
        debug_log('session_history', f"session.summary exists: {self.session.summary is not None}")

    
    def _apply_summary_pruning_fallback(self, summary: str, keep_recent_turns: int):
        """Fallback pruning for when no session is available (legacy behavior)."""
        # This is a simplified version of the old pruning logic
        # Only used when self.session is None
        logger.warning("[DEBUG_PRUNING] Using fallback pruning (no session)")

        # Separate system messages and other messages
        system_messages = [msg for msg in self.conversation if msg.get("role") == "system"]
        other_messages = [msg for msg in self.conversation if msg.get("role") != "system"]

        if not other_messages:
            return

        turns = self._group_messages_into_turns(other_messages)

        if keep_recent_turns <= 0:
            kept_turns = []
        else:
            kept_turns = turns[-keep_recent_turns:] if keep_recent_turns <= len(turns) else turns

        # Create summary message
        MAX_SUMMARY_LENGTH = 4000
        truncated_summary = summary
        if len(truncated_summary) > MAX_SUMMARY_LENGTH:
            truncated_summary = truncated_summary[:MAX_SUMMARY_LENGTH] + "... (truncated)"

        if kept_turns:
            first_kept_turn_idx = len(turns) - len(kept_turns)
            discarded_msg_count = sum(len(turns[i]) for i in range(first_kept_turn_idx))
        else:
            discarded_msg_count = len(other_messages)
        insertion_idx = discarded_msg_count + len(system_messages)

        summary_msg = {
            "role": "system",
            "content": f"Summary of previous conversation: {truncated_summary}",
            "summary": True,
            "pruning_keep_recent_turns": keep_recent_turns,
            "pruning_discarded_msg_count": discarded_msg_count,
            "pruning_insertion_idx": insertion_idx,
        }

        # Clean system messages: keep only first (main prompt) and new summary
        cleaned_system_messages = []
        if system_messages:
            cleaned_system_messages.append(system_messages[0])

        # Flatten kept turns
        pruned_other = []
        for turn in kept_turns:
            pruned_other.extend(turn)

        # Build pruned conversation
        new_conversation = cleaned_system_messages + [summary_msg] + pruned_other
        self.conversation = new_conversation

        logger.debug(f"[DEBUG_PRUNING] Fallback pruning: new conversation length {len(self.conversation)}")

    
    def _find_summary_insertion_index(self, user_history: List[Dict[str, Any]], keep_recent_turns: int) -> int:
        """Find index in user_history where summary should be inserted.
        
        Returns the index of the first message of the first kept turn.
        If keep_recent_turns is 0 or no turns to keep, returns len(user_history).
        """
        if keep_recent_turns <= 0:
            return len(user_history)
        
        # Scan history to count turns and find boundary
        turns = []
        current_turn = []
        turn_start_indices = []  # index in user_history where each turn starts
        
        for i, msg in enumerate(user_history):
            role = msg.get("role")
            
            if role == "system":
                # System messages don't affect turn grouping
                continue
                
            if role == "user":
                # User starts new turn
                if current_turn:
                    turns.append(current_turn)
                current_turn = [msg]
                turn_start_indices.append(i)  # Record start index of this new turn
            elif role == "assistant" and msg.get("tool_calls"):
                # Assistant with tool_calls can start a turn (after pruning)
                # However, if current turn already starts with a user, this assistant belongs to that turn
                if current_turn:
                    if current_turn[0].get("role") == "user":
                        # Continue current turn (user -> assistant with tools)
                        current_turn.append(msg)
                    else:
                        # Start new turn
                        turns.append(current_turn)
                        current_turn = [msg]
                        turn_start_indices.append(i)  # Record start index of this new turn
                else:
                    # Start new turn
                    current_turn = [msg]
                    turn_start_indices.append(i)  # Record start index of this new turn
            else:
                # Other messages (assistant without tools, tool) - add to current turn if we have one
                if current_turn:
                    # For tool messages, validate they follow an assistant with tool_calls
                    if role == 'tool':
                        if current_turn and current_turn[-1].get('role') == 'assistant' and current_turn[-1].get('tool_calls'):
                            current_turn.append(msg)
                        else:
                            # Orphaned tool message - skip it
                            continue
                    else:
                        # assistant without tools - add to current turn
                        current_turn.append(msg)
                else:
                    # Orphaned message without user or assistant-with-tools
                    # Skip for turn counting
                    continue
        
        if current_turn:
            turns.append(current_turn)
        
        # Filter valid turns (starting with user or assistant-with-tools)
        valid_turn_indices = []
        for idx, turn in enumerate(turns):
            if not turn:
                continue
            first_msg = turn[0]
            first_role = first_msg.get("role")
            if first_role == "user" or (first_role == "assistant" and first_msg.get("tool_calls")):
                valid_turn_indices.append(idx)
        
        if not valid_turn_indices:
            return len(user_history)
        
        # Determine which turns to keep
        if keep_recent_turns > len(valid_turn_indices):
            keep_recent_turns = len(valid_turn_indices)
        
        # Index of first kept turn in valid_turn_indices
        first_kept_valid_idx = len(valid_turn_indices) - keep_recent_turns
        # Map back to turns list index
        first_kept_turn_idx = valid_turn_indices[first_kept_valid_idx]
        
        # Get start index of that turn in user_history
        if first_kept_turn_idx < len(turn_start_indices):
            return turn_start_indices[first_kept_turn_idx]
        else:
            # Should not happen, fallback
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
        
        # Debug logging
        import os
        debug = os.environ.get('DEBUG_TURN_GROUPING')
        if debug:
            logger.debug(f"[DEBUG_TURN_GROUPING] Grouping {len(messages)} messages")
            max_to_show = 10
            for i, msg in enumerate(messages[:max_to_show]):
                role = msg.get("role")
                content_preview = str(msg.get("content", ""))[:50]
                has_tool_calls = "tool_calls" in msg and msg["tool_calls"]
                logger.debug(f"  [{i}] {role}: {content_preview}... tool_calls={has_tool_calls}")
            if len(messages) > max_to_show:
                logger.debug(f"  ... and {len(messages) - max_to_show} more messages")
        
        for msg in messages:
            role = msg.get("role")
            
            # Skip system messages (should have been filtered out)
            if role == "system":
                continue
                
            if role == "user":
                # User always starts a new turn
                if current_turn:
                    turns.append(current_turn)
                current_turn = [msg]
            elif role == "assistant" and msg.get("tool_calls"):
                # Assistant with tool_calls can start a turn (after pruning)
                # However, if current turn already starts with a user, this assistant belongs to that turn
                if current_turn:
                    if current_turn[0].get("role") == "user":
                        # Continue current turn (user -> assistant with tools)
                        current_turn.append(msg)
                    else:
                        # Start new turn
                        turns.append(current_turn)
                        current_turn = [msg]
                else:
                    # Start new turn
                    current_turn = [msg]
            else:
                # Other messages (assistant without tools, tool) - add to current turn with validation
                if current_turn:
                    # For tool messages, validate they follow an assistant with tool_calls
                    if role == 'tool':
                        if current_turn and current_turn[-1].get('role') == 'assistant' and current_turn[-1].get('tool_calls'):
                            current_turn.append(msg)
                        else:
                            # Orphaned tool message - skip it
                            if debug:
                                tool_call_id = msg.get('tool_call_id', 'unknown')
                                logger.debug(f"[DEBUG_TURN_GROUPING] Discarding orphaned tool message: {tool_call_id}")
                            continue
                    else:
                        # assistant without tools - add to current turn
                        current_turn.append(msg)
                else:
                    # Orphaned message without user or assistant-with-tools
                    # Discard it
                    if debug:
                        logger.debug(f"[DEBUG_TURN_GROUPING] Discarding orphaned {role} message")
                    continue
        
        if current_turn:
            turns.append(current_turn)
        
        # Filter to keep only valid turns
        valid_turns = []
        for turn in turns:
            if not turn:
                continue
            first_msg = turn[0]
            first_role = first_msg.get("role")
            
            if first_role == "user":
                valid_turns.append(turn)
            elif first_role == "assistant" and first_msg.get("tool_calls"):
                # Turn starts with assistant that made tool calls
                # This is valid (e.g., after pruning cut off the user)
                valid_turns.append(turn)
            elif debug:
                logger.debug(f"[DEBUG_TURN_GROUPING] Discarding turn starting with {first_role}")
        
        if debug:
            logger.debug(f"[DEBUG_TURN_GROUPING] Returned {len(valid_turns)} valid turns")
            max_to_show = 10
            for i, turn in enumerate(valid_turns[:max_to_show]):
                logger.debug(f"  Turn {i}: {[msg.get('role') for msg in turn]}")
            if len(valid_turns) > max_to_show:
                logger.debug(f"  ... and {len(valid_turns) - max_to_show} more turns")
        
        return valid_turns

    def reset(self):
        """Reset agent state."""
        # TODO: Extract from original agent.py
        pass
    
    def update_runtime_params(self, **kwargs):
        """Update runtime parameters."""
        # TODO: Extract from original agent.py
        pass
    
    def submit_next_query(self, query: str):
        """Submit next query to waiting agent."""
        # TODO: Extract from original agent.py
        pass
    
    def request_reset(self):
        """Request agent reset."""
        # TODO: Extract from original agent.py
        pass
    
    def _wait_for_next_query(self):
        """Wait for next query."""
        # TODO: Extract from original agent.py
        pass
    
    @classmethod
    def from_preset(cls, preset_name_or_obj, api_key: str = "", base_url: str = "https://api.deepseek.com", session: Optional['Session'] = None, **overrides):
        """Create an Agent instance from a preset configuration."""
        from agent.config.preset import get_preset_loader
        
        # Resolve preset
        if isinstance(preset_name_or_obj, str):
            loader = get_preset_loader()
            preset = loader.get_preset(preset_name_or_obj)
            if preset is None:
                raise ValueError(f"Preset '{preset_name_or_obj}' not found. Available: {loader.list_presets()}")
        else:
            preset = preset_name_or_obj
        
        # Build tool_classes from preset.tools (list of tool class names)
        tool_classes = []
        preset_tool_names = set(preset.tools or [])
        for tool_cls in SIMPLIFIED_TOOL_CLASSES:
            if tool_cls.__name__ in preset_tool_names:
                tool_classes.append(tool_cls)
        
        # Build AgentConfig fields from preset
        config_data = {
            "api_key": api_key or "",
            "base_url": base_url,
            "model": preset.model,
            "temperature": preset.temperature,
            "tool_classes": tool_classes,
            "enabled_tools": list(preset_tool_names),
            "system_prompt": preset.system_prompt,
            "provider_type": "openai_compatible",
            "max_turns": overrides.get("max_turns", 100),
            "detail": overrides.get("detail", "normal"),
            "workspace_path": overrides.get("workspace_path"),
            "tool_output_token_limit": overrides.get("tool_output_token_limit", 10000),
            "token_monitor_enabled": overrides.get("token_monitor_enabled", True),
            "token_monitor_warning_threshold": overrides.get("token_monitor_warning_threshold", 35000),
            "token_monitor_critical_threshold": overrides.get("token_monitor_critical_threshold", 50000),
            "turn_monitor_enabled": overrides.get("turn_monitor_enabled", True),
            "turn_monitor_warning_threshold": overrides.get("turn_monitor_warning_threshold", 0.8),
            "turn_monitor_critical_threshold": overrides.get("turn_monitor_critical_threshold", 0.95),
            "critical_countdown_turns": overrides.get("critical_countdown_turns", 5),
            "enable_logging": overrides.get("enable_logging", True),
            "log_dir": overrides.get("log_dir", "./logs"),
            "log_level": overrides.get("log_level", "INFO"),
            "enable_file_logging": overrides.get("enable_file_logging", True),
            "enable_console_logging": overrides.get("enable_console_logging", False),
            "jsonl_format": overrides.get("jsonl_format", True),
            "log_categories": overrides.get("log_categories", ["SESSION", "LLM", "TOOLS"]),
            "max_file_size_mb": overrides.get("max_file_size_mb", 10),
            "max_backup_files": overrides.get("max_backup_files", 5),
        }
        
        # Apply any remaining overrides
        config_data.update(overrides)
        
        # Create AgentConfig
        from agent.config import AgentConfig
        config = AgentConfig(**config_data)
        
        # Create Agent instance
        return cls(config, session=session)