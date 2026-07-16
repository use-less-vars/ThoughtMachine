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
import hashlib
import json
import os
import queue
import time
import traceback
import uuid
from datetime import datetime
from typing import Optional, List, Dict, Any, TYPE_CHECKING, Generator
from agent.logging import log
from agent.logging_helpers import dump_messages
from pydantic import ValidationError
from llm_providers.exceptions import ProviderError, RateLimitExceeded
from tools import SIMPLIFIED_TOOL_CLASSES
from tools.utils import model_to_openai_tool
from tools.summarize_tool import SummarizeTool
from agent.core.message import Message
from fast_json_repair import loads as repair_loads
from session.context_builder import ContextBuilder
from agent.core.state import AgentState, ExecutionState, SessionState, TimeState
from agent import events as ev
from agent.events import global_event_bus
from .token_counter import TokenCounter
from .llm_client import LLMClient, LLMError
from .conversation_manager import ConversationManager
from .tool_executor import ToolExecutor
from .turn_transaction import TurnTransaction
from .debug_context import DebugContext
from .message_utils import group_messages_into_turns, group_messages_into_turns_with_indices
if TYPE_CHECKING:
    from agent.config import AgentConfig
    from session.models import Session

from .debug_context import PAUSE_DEBUG, pause_debug

class Agent:
    """Modular agent coordinating specialized components."""
    SAFETY_MARGIN = 1000
    DEFAULT_RESPONSE_TOKENS = 4096

    def __init__(self, config: AgentConfig, session=None, session_id: str=None, event_bus=None):
        """
        Initialize modular agent.
        
        Args:
            config: Agent configuration.
            session: Optional session object.
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
            # Register the logger with unified.py so _get_logger() returns it
            try:
                from agent.logging.unified import set_logger as unified_set_logger
                unified_set_logger(self.logger)
            except ImportError:
                pass
            try:
                from thoughtmachine.security import set_logger as security_set_logger
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
            self._conversation = []
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
        self._pending_warnings = []  # Buffer for token warnings, flushed after turn commit
        self._pending_warning_events = []  # Buffer for token warning events, yielded after turn commit
        self.tool_classes = config.get_filtered_tool_classes()
        # Register MCP tools in background (non-blocking, no startup delay)
        # MCP tools become available later once background registration completes.
        try:
            from tools.mcp_manager import register_mcp_tools
            register_mcp_tools()
        except Exception as e:
            log('WARNING', 'core.agent', f"Failed to start MCP background registration: {e}")
        self.tool_definitions = [model_to_openai_tool(cls) for cls in self.tool_classes]
        self.tool_executor = ToolExecutor(self.tool_classes, config, None, self.logger, self.security_available, agent=self, event_bus=event_bus or global_event_bus, is_worker_context=config.worker_mode)
        self.provider = self.llm_client.provider
        from session.models import RuntimeParams
        self.runtime_params = RuntimeParams(temperature=config.temperature, top_p=None)
        self._token_encoder = None
        if session is not None:
            self._token_counts = {'input': session.total_input_tokens, 'output': session.total_output_tokens}
        else:
            self._token_counts = {'input': 0, 'output': 0}
        self.conversation = self.llm_client.ensure_system_prompt(self.conversation)
        self.context_builder = self.llm_client.create_context_builder()
        log('DEBUG', 'core.context_builder', f'Agent init: session is None={session is None}, cb is None={self.context_builder is None}')
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
        # Mailbox pattern: pending config update to be applied at next process_query()
        self._pending_config: Optional[AgentConfig] = None
        self._last_config_error: Optional[str] = None

    def request_config_update(self, new_config: AgentConfig):
        """
        Request a configuration update via the mailbox pattern.
        
        The update is queued in _pending_config and will be applied at the
        start of the next process_query() call. This allows safe, atomic
        config changes at turn boundaries.
        
        Args:
            new_config: New AgentConfig to apply.
        """
        self._pending_config = new_config
        log('DEBUG', 'core.agent', f'Pending config update queued: provider={new_config.provider_type}, model={new_config.model}')

    def _apply_pending_config(self) -> bool:
        """Apply pending configuration update if one exists.

        Called at the start of process_query(). Determines whether
        the change can be hot-swapped (simple parameter changes like
        temperature, top_p, enabled_tools) or requires
        a full agent restart.

        Preserves _pending_config on failure so the change can be
        retried on the next process_query() call.

        Returns:
            True if config was successfully applied (or no pending config),
            False if restart failed or no API key available.
        """
        if self._pending_config is None:
            return True

        new_config = self._pending_config
        old_config = self.config  # Capture pre-application config for diff

        # ── Early return if config is semantically unchanged ──────────────
        # Prevents spurious "config updated" notifications on every query
        # when the frontend re-sends an identical config.
        if self._configs_are_identical(old_config, new_config):
            log('DEBUG', 'core.agent',
                f'Skipping no-op config update: pending config is identical '
                f'to current config (provider={new_config.provider_type}, '
                f'model={new_config.model})')
            self._pending_config = None
            return True

        if self._can_hot_swap(new_config):
            self._hot_swap(new_config)
            self._pending_config = None
            self._notify_config_change(old_config, new_config)
            return True
        else:

            # Validate that the new config has an API key available before attempting restart
            if not self._has_api_key(new_config):
                log('WARNING', 'core.agent', f'Cannot restart with provider {new_config.provider_type}: '
                    f'no API key available. Set {new_config.provider_type.upper()}_API_KEY '
                    f'environment variable or provide api_key in config.')
                log('WARNING', 'core.config',
                    'Pending config PRESERVED for retry on next turn '
                    f'(no API key for provider={new_config.provider_type})')
                self._last_config_error = (
                    f'No API key available for provider "{new_config.provider_type}". '
                    f'Set {new_config.provider_type.upper()}_API_KEY environment variable '
                    f'or provide an api_key in the configuration.')
                return False
            success = self._restart_with_config(new_config)
            if success:
                self._pending_config = None
                self._notify_config_change(old_config, new_config)
            return success

    def _configs_are_identical(self, config_a: 'AgentConfig', config_b: 'AgentConfig') -> bool:
        """Compare two AgentConfig instances field-by-field for semantic equality.

        Excludes fields that are sensitive (api_key) or non-comparable (stop_check).
        Treats None and empty string as equivalent for Optional[str] fields.

        This prevents spurious "config updated" notifications when the
        frontend re-sends a config that is semantically identical.
        """
        # Fields to exclude from comparison
        excluded_fields = {'api_key', 'stop_check'}

        for field_name in type(config_a).model_fields:
            if field_name in excluded_fields:
                continue

            old_val = getattr(config_a, field_name)
            new_val = getattr(config_b, field_name)

            # Normalize None vs "" for string fields to avoid trivial mismatches
            if isinstance(old_val, str) and old_val == '':
                old_val = None
            if isinstance(new_val, str) and new_val == '':
                new_val = None

            if old_val != new_val:
                log('DEBUG', 'core.agent',
                    f'Config field "{field_name}" differs: '
                    f'old={type(old_val).__name__}({repr(old_val)[:60]}) '
                    f'!= new={type(new_val).__name__}({repr(new_val)[:60]})')
                return False

        return True

    def _notify_config_change(self, old_config, new_config) -> None:
        """Add a system notification describing the config changes that were applied.

        Called after a successful hot-swap or restart so the LLM and the user
        are explicitly aware of what changed.

        Uses programmatic field comparison across all AgentConfig model fields,
        excluding sensitive fields (api_key) and non-comparable fields (stop_check).
        """
        changes = []
        # Fields to exclude from diff notification
        sensitive_fields = {'api_key', 'stop_check'}

        import json as _json2
        field_log = []

        for field_name in type(new_config).model_fields:
            if field_name in sensitive_fields:
                continue

            old_val = getattr(old_config, field_name)
            new_val = getattr(new_config, field_name)
            changed = old_val != new_val

            field_log.append(f'{field_name}: changed={changed} old={_json2.dumps(old_val, default=str) if not callable(old_val) else "<callable>"} new={_json2.dumps(new_val, default=str) if not callable(new_val) else "<callable>"}')

            if changed:
                # Use friendlier display names for certain fields
                if field_name == 'enabled_tools':
                    changes.append("tools updated")
                elif field_name == 'system_prompt':
                    changes.append("system_prompt updated")
                elif field_name == 'provider_type':
                    changes.append(f"provider={new_val}")
                elif field_name == 'workspace_path':
                    changes.append(f"workspace={new_val}")
                else:
                    changes.append(f"{field_name}={new_val}")


        if changes:
            msg = "[SYSTEM NOTIFICATION] Configuration updated: " + ", ".join(changes) + "."
            log('DEBUG', 'core.config', f'NOTIFICATION CREATED: {msg}')
            self._add_to_conversation(Message(role='user', content=msg, is_system_notification=True))

    def _can_hot_swap(self, new_config: AgentConfig) -> bool:
        """Check if a config change can be applied via hot-swap.
        
        Hot-swap is safe when only simple runtime parameters change
        (temperature, top_p, enabled_tools). Changes to
        model, provider, system_prompt, tool_classes, api_key, base_url
        etc. require a full restart.
        """
        if new_config.provider_type != self.config.provider_type:
            return False
        if new_config.model != self.config.model:
            return False
        if new_config.api_key != self.config.api_key:
            return False
        if new_config.base_url != self.config.base_url:
            return False
        if new_config.system_prompt != self.config.system_prompt:
            return False

        # Workspace path changes require a full restart
        if new_config.workspace_path != self.config.workspace_path:
            return False
        return True

    def _hot_swap(self, new_config: AgentConfig):
        """Apply a lightweight config update without restarting.
        
        Updates only runtime parameters (temperature, top_p)
        and tool definitions. Does NOT re-initialise the LLM provider.
        """
        changed = []
        
        # Update runtime params
        if new_config.temperature != self.config.temperature:
            self.runtime_params.temperature = new_config.temperature
            changed.append(f'temperature={new_config.temperature}')
        # Update config reference
        old_config = self.config
        self.config = new_config
        # Also update downstream config references so they pick up
        # workspace_path, token thresholds, and other HOT_SWAPPABLE fields
        self.state.config = new_config
        self.tool_executor.config = new_config

        # Rebuild tools if enabled_tools changed
        if new_config.enabled_tools != old_config.enabled_tools:
            self.tool_classes = new_config.get_filtered_tool_classes()
            self.tool_definitions = [model_to_openai_tool(cls) for cls in self.tool_classes]
            # Preserve event bus from old executor before closing
            event_bus = self.tool_executor._event_bus if self.tool_executor else None
            self.tool_executor.close()
            self.tool_executor = ToolExecutor(
                self.tool_classes, new_config, None, self.logger,
                self.security_available, agent=self,
                event_bus=event_bus or global_event_bus,
                is_worker_context=new_config.worker_mode,
            )
            self.tool_executor.state = self.state
            changed.append(f'enabled_tools={", ".join(new_config.enabled_tools)}')
        
        if changed:
            log('DEBUG', 'core.agent', f'Config hot-swapped: {", ".join(changed)}')
            if self.logger:
                self.logger.log_system_event(f'Config hot-swapped: {", ".join(changed)}')

    def _restart_with_config(self, new_config: AgentConfig) -> bool:
        """Perform a full agent restart with new configuration.

        Closes old LLM client and tool executor, then re-initialises
        everything from the new config while preserving conversation history.

        Returns:
            True if restart succeeded, False otherwise.
        """
        success = self.restart(new_config)
        if not success:
            error_detail = self._last_config_error or 'Unknown error during restart'
            log('ERROR', 'core.agent', f'Agent restart with provider={new_config.provider_type} failed: {error_detail}')
        return success

    @staticmethod
    def _has_api_key(config: AgentConfig) -> bool:
        """Check if the config has an API key available (either directly or via env var).

        Args:
            config: The AgentConfig to check.

        Returns:
            True if an API key is available, False otherwise.
        """
        if config.api_key:
            return True
        # Check provider-specific environment variable
        env_var = f"{config.provider_type.upper()}_API_KEY"
        if os.getenv(env_var):
            return True
        # For openai/openai_compatible, also check OPENAI_API_KEY
        if config.provider_type in ("openai", "openai_compatible"):
            if os.getenv("OPENAI_API_KEY"):
                return True
        return False

    def restart(self, new_config: AgentConfig) -> bool:
        """
        Restart the agent with a new configuration while preserving conversation history.

        Closes old LLM client and tool executor, then re-initialises
        everything from the new config. Preserves conversation history,
        logger, token counts, and system event logger.

        Args:
            new_config: New AgentConfig to apply.

        Returns:
            True on success, False on error.
        """
        log('DEBUG', 'core.agent', f'restart: new_config provider={new_config.provider_type}, model={new_config.model}')
        # Save old references BEFORE closing, so we can restore on failure
        old_logger = self.logger
        old_system_event_logger = getattr(self, 'system_event_logger', None)
        old_config = getattr(self, 'config', None)
        old_llm_client = getattr(self, 'llm_client', None)
        old_tool_executor = getattr(self, 'tool_executor', None)
        # Capture event bus from old executor before closing
        old_event_bus = old_tool_executor._event_bus if old_tool_executor else None

        try:
            # Preserve conversation and token state BEFORE closing
            current_conversation = self.conversation.copy()
            token_counts = self._token_counts.copy()

            # Close old resources
            if old_llm_client is not None:
                old_llm_client.close()
            if old_tool_executor is not None:
                old_tool_executor.close()

            # Reset execution state
            self.state.set_execution_state(ExecutionState.READY)
            self._next_query_queue = queue.Queue()
            self._paused = False
            self._pause_requested = False
            self._should_reset = False

            # Update config reference
            self.config = new_config
            # Also update downstream config references (state picks up token thresholds etc.)
            self.state.config = new_config
            self.token_counter = TokenCounter(new_config)

            # Re-initialise LLM client and provider from new_config
            self.llm_client = LLMClient(new_config, self._session, old_logger)
            self.provider = self.llm_client.provider

            # Rebuild tool_classes and tool_definitions
            self.tool_classes = new_config.get_filtered_tool_classes()
            self.tool_definitions = [model_to_openai_tool(cls) for cls in self.tool_classes]
            self.tool_executor = ToolExecutor(self.tool_classes, new_config, self.state, old_logger, self.security_available, agent=self, event_bus=old_event_bus or global_event_bus, is_worker_context=new_config.worker_mode)

            # Rebuild context builder
            self.context_builder = self.llm_client.create_context_builder()
            if self.context_builder:
                self.conversation_manager.context_builder = self.context_builder

            # Update runtime params
            from session.models import RuntimeParams
            self.runtime_params = RuntimeParams(
                temperature=new_config.temperature,
                top_p=None
            )

            # Restore conversation with updated system prompt
            self.conversation = self.llm_client.ensure_system_prompt(current_conversation)
            self._token_counts = token_counts

            # Restore logger and system event logger
            self.logger = old_logger
            if old_system_event_logger:
                self.system_event_logger = old_system_event_logger

            # Reset rate limiting
            self.reset_rate_limiting()

            log('DEBUG', 'core.agent', f'Agent restarted with provider={new_config.provider_type}, model={new_config.model}')
            if self.logger:
                self.logger.log_info('AGENT_RESTART', f'Configuration reloaded, provider: {self.provider}')
            return True
        except Exception as e:
            log('ERROR', 'core.agent', f'Failed to restart agent: {e}')
            self._last_config_error = str(e)
            # Restore old LLM client so agent isn't left in a broken state
            if old_config is not None and old_logger is not None:
                try:
                    self.llm_client = LLMClient(old_config, self._session, old_logger)
                    self.provider = self.llm_client.provider
                except Exception as restore_error:
                    log('CRITICAL', 'core.agent', f'Failed to restore old LLM client after restart failure: {restore_error}')
            else:
                self.llm_client = None
                self.provider = None
            # Restore config to previous value
            self.config = old_config
            return False

    @property
    def session(self):
        """Return the session object."""
        return self._session

    @session.setter
    def session(self, value):
        self._session = value
        if self.llm_client:
            self.llm_client.session = value
        if hasattr(self, 'context_builder') and self.context_builder is not None and hasattr(self.context_builder, 'session'):
            self.context_builder.session = value
        elif value is not None and hasattr(self, 'llm_client') and (self.llm_client is not None):
            self.context_builder = self.llm_client.create_context_builder()
            log('DEBUG', 'core.context_builder', f'Session setter: cb created, is None={self.context_builder is None}')
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
            log('DEBUG', 'core.agent', f'conversation setter: replacing session.user_history with {len(value)} messages')
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
            if self.logger:
                pass
            self._add_conversation_data_to_event(event)
            yield event
        elif event.get('type') == 'turn_warning':
            if self.logger:
                log('DEBUG', 'core.token', f'Turn warning event: {event.get("warning_message", "")}')
            self._add_conversation_data_to_event(event)
            yield event
        elif event.get('type') == 'execution_state_change':
            old_state = event.get('old_state')
            new_state = event.get('new_state')
            if self.logger:
                log('DEBUG', 'core.agent', f'Execution state change: {old_state} -> {new_state}')
            self._add_conversation_data_to_event(event)
            yield event
        elif event.get('type') == 'session_state_change':
            old_state = event.get('old_state')
            new_state = event.get('new_state')
            if self.logger:
                log('DEBUG', 'core.agent', f'Session state change: {old_state} -> {new_state}')
            self._add_conversation_data_to_event(event)
            yield event
        elif event.get('type') == 'token_recovery':
            if self.logger:
                log('DEBUG', 'core.token', f'Token recovery event: {event.get("recovery_message", "")}')
            self._add_conversation_data_to_event(event)
            yield event
        elif event.get('type') == 'context_cleared':
            if self.logger:
                log('DEBUG', 'core.token', 'Context cleared event after summarization')
            self._add_conversation_data_to_event(event)
            yield event
            old_state = event.get('old_state')
            new_state = event.get('new_state')
            if self.logger:
                log('DEBUG', 'core.agent', f'Session state change: {old_state} -> {new_state}')
            self._add_conversation_data_to_event(event)
            yield event
    def _update_conversation_token_estimate(self):
        """Update current_conversation_tokens by estimating tokens for runtime context."""
        if self.session is not None:
            needs_update = False
            if not hasattr(self, 'context_builder') or self.context_builder is None:
                needs_update = True
            if needs_update:
                if hasattr(self, 'llm_client') and self.llm_client is not None:
                    self.llm_client.session = self.session
                    self.context_builder = self.llm_client.create_context_builder()
                    if self.context_builder and hasattr(self, 'conversation_manager') and (self.conversation_manager is not None):
                        self.conversation_manager.context_builder = self.context_builder
        if not hasattr(self, 'context_builder') or self.context_builder is None:
            runtime_context = self.conversation
        elif hasattr(self.context_builder, 'get_context_for_llm'):
            runtime_context = self.context_builder.get_context_for_llm()
        else:
            runtime_context = self.context_builder.build(self.conversation)
        original_len = len(runtime_context)
        runtime_context = ContextBuilder._cleanup_orphaned_tool_messages(runtime_context)
        estimated_tokens = 0
        for msg in runtime_context:
            estimated_tokens += self.token_counter.estimate_tokens(msg)
        self.state.current_conversation_tokens = estimated_tokens
        cleaned = original_len - len(runtime_context)
        cb_status = hasattr(self, 'context_builder') and self.context_builder is not None
        token_limit = getattr(getattr(self, 'context_builder', None), 'token_limit', None) if cb_status else None
        log('DEBUG', 'core.token', f'Token estimate: {estimated_tokens} tokens ({len(runtime_context)}/{len(self.conversation)} msgs, cleaned={cleaned}, cb={cb_status}, limit={token_limit})')

    def _add_to_conversation(self, message):
        """Add a message via conversation_manager (ensures cache invalidation)."""

        updated = self.conversation_manager.add_message(message, self.conversation)
        self.conversation = updated
        # Validation: flag consistency check
        role = message.get('role', '')
        content = message.get('content', '')
        is_sys_notif = message.get('is_system_notification', False)
        content_preview = content[:100]
        if content.startswith('[SYSTEM NOTIFICATION]'):
            if is_sys_notif is not True:
                log('WARNING', 'core.validation', 'Flag inconsistency detected', {
                    'reason': 'missing is_system_notification flag',
                    'role': role,
                    'content_preview': content_preview,
                })
        else:
            if is_sys_notif is True:
                log('WARNING', 'core.validation', 'Flag inconsistency detected', {
                    'reason': 'unexpected is_system_notification flag',
                    'role': role,
                    'content_preview': content_preview,
                })
        if hasattr(self, 'context_builder') and self.context_builder is not None:
            if hasattr(self.context_builder, '_cached_context'):
                self.context_builder._cached_context = None

    def _estimate_tokens(self, message):
        """Estimate tokens for a message."""
        return self.token_counter.estimate_tokens(message)

    def _update_tokens_after_tool(self, tool_tokens=None):
        """Update token count after a tool result and inject any warnings.

        This is a regular function (not a generator) so it executes immediately.
        Unlike the old generator version, it consumes warning events from
        update_token_state() and injects them as [SYSTEM NOTIFICATION] messages.

        Args:
            tool_tokens: Optional estimated token count for tool result to add.
        """
        if tool_tokens is not None:
            self.state.current_conversation_tokens += tool_tokens
            log('DEBUG', 'core.token', f'Tool result: +{tool_tokens} tokens, total={self.state.current_conversation_tokens}')

        # Process any warnings or state changes from the token update
        for event in self.state.update_token_state(self.state.current_conversation_tokens):
            if event['type'] == 'token_warning':
                # Buffer the warning instead of injecting immediately — it will be flushed
                # after turn_transaction.commit() so it lands in correct chronological order.
                warning_msg = Message(role='user', content=f'[SYSTEM NOTIFICATION] {event.get("warning_message", event.get("message", event.get("warning", "")))}', is_system_notification=True, metadata={'warning_id': str(uuid.uuid4())})
                self._pending_warnings.append(warning_msg)
                # Also buffer the raw event for yielding to event stream subscribers
                self._add_conversation_data_to_event(event)
                self._pending_warning_events.append(event)
                log('DEBUG', 'core.token', f"warning buffered: state={self.state.token_state.value if hasattr(self.state, 'token_state') else 'N/A'}, count={len(self._pending_warnings)}")
                log('DEBUG', '**pipeline.warning**', f"Warning buffered in _update_tokens_after_tool: state={self.state.token_state.value if hasattr(self.state, 'token_state') else 'N/A'}, count={len(self._pending_warnings)}")
                warning_tokens = self._estimate_tokens(warning_msg)
                self.state.current_conversation_tokens += warning_tokens
            elif event['type'] == 'token_recovery':
                # Buffer the raw event for yielding to event stream subscribers
                self._add_conversation_data_to_event(event)
                self._pending_warning_events.append(event)
                log('DEBUG', '**pipeline.warning**', f"Recovery buffered in _update_tokens_after_tool: token_state={self.state.token_state.value if hasattr(self.state, 'token_state') else 'N/A'}")
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

    def request_pause(self):
        """Request pause at the next atomic turn boundary."""
        pause_debug(f'request_pause called, setting _pause_requested=True')
        self._pause_requested = True

    def process_query(self, query):
        """Process a user query, appending it to conversation and running the agent.
        Yields events as dicts."""
        self._emergency_retries = 0
        try:
            log('DEBUG', 'core.agent', f'process_query: query="{query[:80]}..." turn_display={self._display_turn}')

            # Add query to conversation FIRST so it's never lost, even on config failure
            user_msg = {'role': 'user', 'content': query}
            self._add_to_conversation(user_msg)
            estimated_tokens = self._estimate_tokens(user_msg)
            self.state.current_conversation_tokens += estimated_tokens
            log('DEBUG', '**pipeline.token_update**', f"Token update after user query: tokens={self.state.current_conversation_tokens}, input={self.total_input_tokens}, output={self.total_output_tokens}")
            yield self._create_token_update_event()
            self._display_turn = getattr(self, '_display_turn', 0) + 1
            log('DEBUG', 'core.agent', f"User query added: turn={self._display_turn}")
    
            # Apply any pending configuration update BEFORE yielding user_query,
            # so the [SYSTEM NOTIFICATION] is already in user_history when the
            # bridge syncs the conversation for the frontend.
            config_applied = self._apply_pending_config()
    
            if not config_applied:
                error_detail = self._last_config_error or 'Unknown error'
                log('ERROR', 'core.agent', f'Configuration update failed: {error_detail}')
    
                # Add a visible system notification to the conversation
                error_message = (
                    '[SYSTEM NOTIFICATION] Configuration change failed: '
                    f'{error_detail}. '
                    'The previous configuration remains active. '
                    'Please fix the settings and retry.'
                )
                notif_msg = Message(role='user', content=error_message, is_system_notification=True)
                self._add_to_conversation(notif_msg)
    
                event_dict = {
                    'type': 'error',
                    'error_type': 'invalid_config',
                    'stop_reason': 'error',
                    'message': error_detail,
                    'turn': self._display_turn,
                }
                self._add_conversation_data_to_event(event_dict)
                yield event_dict
                return
    
            # Now yield user_query — the bridge syncs the conversation, and any
            # config-change notification from _apply_pending_config is already visible.
            event_dict = {'type': 'user_query', 'content': query, 'turn': self._display_turn}
            self._add_conversation_data_to_event(event_dict)
            if 'timestamp' not in event_dict:
                event_dict['timestamp'] = event_dict.get('created_at', time.time())
            yield event_dict
    
            pause_debug(f"process_query called with query: '{query[:50]}...'")
            pause_debug(f'Current execution state: {self.state.execution_state}')
            pause_debug(f'Conversation length after adding query: {len(self.conversation)}')
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
                log('WARNING', 'core.agent', f'process_query called while already in RUNNING state')
                if self.logger:
                    self.logger.log_error('EXECUTION_STATE', 'process_query called while already RUNNING')
            elif current_exec_state == ExecutionState.READY:
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
                config_data = {'model': self.config.model, 'temperature': self.config.temperature, 'max_turns': self.config.max_turns}
                self.logger.log_agent_start(query, config_data)
                self.logger.log_system_resources()
            prev_conversation_len = len(self.conversation)
            last_input_tokens = 0
            last_output_tokens = 0
            # Initialize time monitoring at start of agent execution
            self.state.time_start = time.time()
            for turn in range(self.config.max_turns):
                turn_start_time = time.time()
                log('DEBUG', 'core.agent', f'process_query: starting turn {turn}/{self.config.max_turns}, conversation length={len(self.conversation)}')

                if self.logger:
                    self.logger.log_turn_start(turn)
                    if turn % 5 == 0:
                        self.logger.log_system_resources()
                log('DEBUG', 'core.pause', 'PAUSE CHECKPOINT [1] turn_start')
                if self.stop_check and self.stop_check():
                    log('DEBUG', 'core.pause', 'PAUSE CHECKPOINT [1] turn_start: DETECTED')
                    events = self.state.set_execution_state(ExecutionState.PAUSING)
                    for event in events:
                        for yielded_event in self._handle_state_event(event):
                            yield yielded_event
                    if self.logger:
                        self.logger.log_stop_signal()
                        self.logger.log_system_resources()
                        self.logger.log_agent_end('stopped', 'Stop signal received')
                        self.logger.close()
                    stopped_event = {'type': 'stopped', 'stop_reason': 'stopped', 'turn': self._display_turn, 'context_length': self.state.current_conversation_tokens, 'usage': {'input': last_input_tokens, 'output': last_output_tokens, 'total_input': self.total_input_tokens, 'total_output': self.total_output_tokens}}
                    self._add_conversation_data_to_event(stopped_event)
                    yield stopped_event
                    return
                # Time monitoring
                if self.state.time_start is not None:
                    elapsed = time.time() - self.state.time_start
                    time_events = self.state.update_time_state(elapsed)
                    for event in time_events:
                        if event['type'] == 'time_warning':
                            warning_msg = Message(
                                role='user',
                                content='[SYSTEM NOTIFICATION] ' + event.get('message', event.get('warning_message', '')),
                                is_system_notification=True
                            )
                            self._add_to_conversation(warning_msg)
                            warning_tokens = self._estimate_tokens(warning_msg)
                            self.state.current_conversation_tokens += warning_tokens
                            log('DEBUG', '**pipeline.token_update**', f"Token update after time_warning: tokens={self.state.current_conversation_tokens}")
                            yield self._create_token_update_event()
                            # If time is critical, log it but don't stop — soft restriction
                            if self.state.time_state == TimeState.CRITICAL:
                                log('WARNING', 'core.agent', f'Time critical: soft restriction applied after {elapsed:.1f}s')
                                if self.logger:
                                    self.logger.log_agent_end('timeout', f'Agent execution timed out after {elapsed:.1f}s')
                        event_dict = {'type': event['type'], 'message': event.get('message', event.get('warning_message', '')), 'elapsed_seconds': event.get('elapsed_seconds', elapsed), 'turn': self._display_turn, 'context_length': self.state.current_conversation_tokens, 'usage': {'input': last_input_tokens, 'output': last_output_tokens, 'total_input': self.total_input_tokens, 'total_output': self.total_output_tokens}}
                        self._add_conversation_data_to_event(event_dict)
                        yield event_dict
    
                turn_events = self.state.update_turn_state(turn)
                for event in turn_events:
                    if event['type'] == 'turn_warning':
                        warning_msg = Message(role='user', content='[SYSTEM NOTIFICATION] ' + event.get('warning_message', event.get('message', event.get('warning', ''))), is_system_notification=True)
                        self._add_to_conversation(warning_msg)
                        warning_tokens = self._estimate_tokens(warning_msg)
                        self.state.current_conversation_tokens += warning_tokens
                        log('DEBUG', '**pipeline.token_update**', f"Token update after turn_warning: tokens={self.state.current_conversation_tokens}")
                        yield self._create_token_update_event()
                    event_dict = {'type': event['type'], 'message': event.get('warning_message', event.get('message', event.get('warning', ''))), 'turn_count': event.get('turn_count', turn), 'turn': self._display_turn, 'context_length': self.state.current_conversation_tokens, 'usage': {'input': last_input_tokens, 'output': last_output_tokens, 'total_input': self.total_input_tokens, 'total_output': self.total_output_tokens}}
                    self._add_conversation_data_to_event(event_dict)
                    yield event_dict
                # Don't recalculate full estimate here - truth-based current_conversation_tokens
                # is maintained by LLM prompt_tokens + estimated additions for new content
                log('DEBUG', '**pipeline.token_update**', f"Token update after turn state: tokens={self.state.current_conversation_tokens}, turn={self._display_turn}")
                yield self._create_token_update_event()
                token_events = self.state.update_token_state(self.state.current_conversation_tokens)
                for event in token_events:
                    if event['type'] == 'token_warning':
                        warning_msg = Message(role='user', content='[SYSTEM NOTIFICATION] ' + event.get('warning_message', event.get('message', event.get('warning', ''))), is_system_notification=True, metadata={'warning_id': str(uuid.uuid4())})
                        self._add_to_conversation(warning_msg)
                        warning_tokens = self._estimate_tokens(warning_msg)
                        self.state.current_conversation_tokens += warning_tokens
                        log('DEBUG', '**pipeline.token_update**', f"Token update after token_warning: tokens={self.state.current_conversation_tokens}")
                        yield self._create_token_update_event()
                    # Yield the original event (not reconstructed) to preserve all fields
                    self._add_conversation_data_to_event(event)
                    yield event
                for msg in self.conversation:
                    if msg.get('role') == 'assistant' and 'tool_calls' in msg:
                        if msg.get('reasoning_content') is None:
                            msg['reasoning_content'] = ''
                if self.logger and hasattr(self.logger, 'py_logger'):
                    system_msgs = [msg for msg in self.conversation if msg.get('role') == 'system']
                    log('INFO', 'core.agent', f'[CONVERSATION] Total messages: {len(self.conversation)}, system messages: {len(system_msgs)}')
                self.debug_context.debug_context('before_build', context_builder=self.context_builder)
                if hasattr(self, 'context_builder') and self.context_builder is not None:
                    messages = self.context_builder.build(self.conversation)
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
                    log('DEBUG', 'core.agent', f'[DEBUG_CONTEXT] Agent: cleaned {original_len - len(messages)} orphaned tool messages from final context')
                if self.logger and hasattr(self.logger, 'py_logger'):
                    import tiktoken
                    try:
                        encoder = tiktoken.get_encoding('cl100k_base')
                    except Exception:
                        encoder = None
                    total_tokens = sum((self.token_counter.estimate_tokens(msg) for msg in messages))
                    log('INFO', 'core.agent', f'[CONTEXT] Built context: {len(messages)} messages, ~{total_tokens} tokens')
                if self.logger:
                    self.logger.log_llm_request(messages, self.tool_definitions)
                if self.rate_limit_active:
                    delay = min(self.rate_limit_delay, self.rate_limit_max_wait)
                    if delay > 0:
                        if self.logger and hasattr(self.logger, 'py_logger'):
                            log('INFO', 'core.agent', f'[RATE_LIMIT] Applying rate limit delay: {delay}s between turns')
                        time.sleep(delay)
                tools = self.llm_client.format_tools(self.tool_definitions)
                # EMERGENCY TRACE: dump conversation and messages before LLM call
                log('DEBUG', 'core.emergency', f'====== EMERGENCY TRACE: self.conversation ({len(self.conversation)} msgs) ======')
                for i, msg in enumerate(self.conversation):
                    role = msg.get('role', 'unknown')
                    content = msg.get('content', '')
                    preview = content[:80].replace('\n', '\\n')
                    log('DEBUG', 'core.emergency', f'  CONV[{i}] role={role}: {preview}')
                log('DEBUG', 'core.emergency', f'====== EMERGENCY TRACE: messages from build() ({len(messages)} msgs) ======')
                for i, msg in enumerate(messages):
                    role = msg.get('role', 'unknown')
                    content = msg.get('content', '')
                    preview = content[:80].replace('\n', '\\n')
                    log('DEBUG', 'core.emergency', f'  MSG[{i}] role={role}: {preview}')
                try:
                    chat_kwargs = {'temperature': self.runtime_params.temperature}
                    if self.runtime_params.top_p is not None:
                        chat_kwargs['top_p'] = self.runtime_params.top_p
                    # ── Agent messages diagnostic ──────────────────────────────────
                    log('DEBUG', 'core.agent', f"[AGENT MESSAGES BEFORE API] turn={turn} len(messages)={len(messages)} "
                        f"roles={[m.get('role') for m in messages[-5:]]} "
                        f"last_user={'present' if any(m['role']=='user' for m in messages[-2:]) else 'MISSING'}")
                    log('DEBUG', 'core.pause', 'LLM CALL START')
                    llm_start_time = time.time()
                    response = self.llm_client.chat_completion(messages=messages, tools=tools if tools else None, **chat_kwargs)
                    llm_duration_ms = (time.time() - llm_start_time) * 1000
                    log('DEBUG', 'core.pause', f'LLM CALL END (duration_ms={llm_duration_ms:.0f})')
                    if self.logger:
                        self.logger.log_latency('llm_call', llm_duration_ms, {'turn': turn, 'model': self.config.model, 'has_tools': bool(tools)})
                    input_tokens = response.usage.get('prompt_tokens', 0) if response.usage else 0
                    output_tokens = response.usage.get('completion_tokens', 0) if response.usage else 0
                    # Use LLM-reported prompt_tokens as ground truth for conversation token count
                    previous_tokens = self.state.current_conversation_tokens
                    self.state.current_conversation_tokens = input_tokens
                    log('DEBUG', 'core.token', f'LLM prompt_tokens={input_tokens}, overwriting current_tokens (was {previous_tokens})')
                    # Token drift detection: compare pre-call estimate vs LLM-reported
                    if previous_tokens is not None and previous_tokens > 0 and input_tokens > 0:
                        drift = abs(input_tokens - previous_tokens)
                        drift_pct = (drift / input_tokens) * 100
                        if drift_pct > 5:
                            log('WARNING', 'core.token', 'Token estimate drift detected', {
                                'estimated': previous_tokens,
                                'reported': input_tokens,
                                'drift_pct': round(drift_pct, 2),
                            })
                        else:
                            log('DEBUG', 'core.token', 'Token estimate within tolerance', {
                                'estimated': previous_tokens,
                                'reported': input_tokens,
                                'drift_pct': round(drift_pct, 2),
                            })
                    elif input_tokens > 0:
                        log('INFO', 'core.token', 'LLM prompt_tokens received (no prior estimate)', {
                            'reported': input_tokens,
                        })
                    last_input_tokens = input_tokens
                    last_output_tokens = output_tokens
                    self.total_input_tokens += input_tokens
                    self.total_output_tokens += output_tokens
                except LLMError as e:
                    if e.error_type == 'token_limit_exceeded':
                        if self._emergency_retries >= 2:
                            # All retries exhausted – clear emergency mode and stop
                            self.context_builder.emergency_mode = False
                            self._emergency_retries = 0
                            log('ERROR', 'core.agent', 'Emergency recovery failed after retries',
                                {'error': str(e)})
                            yield {
                                'type': 'error',
                                'error': f'Context too large. Please summarise manually and retry. ({str(e)})',
                                'stop_reason': 'context_full'
                            }
                            return
                        # Activate emergency mode and retry the whole turn
                        self._emergency_retries += 1
                        self.context_builder.emergency_mode = True
                        log('WARNING', 'core.agent', 'Token limit exceeded – emergency retry',
                            {'attempt': self._emergency_retries})
                        continue  # jumps back to the top of the turn loop, which rebuilds messages
                    # For any other LLMError, yield a detailed error event (same pattern as line 1002)
                    error_type = e.error_type.upper()
                    if self.logger:
                        self.logger.log_error(error_type, str(e))
                        self.logger.log_system_resources()
                        self.logger.log_agent_end('provider_error', f'Provider error: {e}')
                        self.logger.close()
                    if self.session is not None:
                        self._add_to_conversation(Message(role='user', content=f'[SYSTEM NOTIFICATION] Error: {error_type}: {e}', is_system_notification=True))
                    event_dict = {'type': 'error', 'error_type': error_type, 'message': str(e), 'stop_reason': 'error', 'traceback': traceback.format_exc(), 'turn': self._display_turn, 'context_length': self.state.current_conversation_tokens, 'usage': {'input': last_input_tokens, 'output': last_output_tokens, 'total_input': self.total_input_tokens, 'total_output': self.total_output_tokens}}
                    self._add_conversation_data_to_event(event_dict)
                    yield event_dict
                    return
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
                    if self.session is not None:
                        self._add_to_conversation(Message(role='user', content=f'[SYSTEM NOTIFICATION] Error: RATE_LIMIT_EXCEEDED: rate limit exceeded after {self.rate_limit_count} attempts', is_system_notification=True))
                    stop_reason_event = {'type': 'stop_reason', 'stop_reason': 'rate_limit', 'message': f'Rate limit exceeded after {self.rate_limit_count} attempts', 'turn': self._display_turn, 'context_length': self.state.current_conversation_tokens, 'usage': {'input': last_input_tokens, 'output': last_output_tokens, 'total_input': self.total_input_tokens, 'total_output': self.total_output_tokens}}
                    self._add_conversation_data_to_event(stop_reason_event)
                    yield stop_reason_event
                    return
                except (ProviderError, LLMError) as e:
                    error_type = 'PROVIDER_ERROR'
                    if isinstance(e, LLMError):
                        error_type = e.error_type.upper()
                    if self.logger:
                        self.logger.log_error(error_type, str(e))
                        self.logger.log_system_resources()
                        self.logger.log_agent_end('provider_error', f'Provider error: {e}')
                        self.logger.close()
                    if self.session is not None:
                        self._add_to_conversation(Message(role='user', content=f'[SYSTEM NOTIFICATION] Error: {error_type}: {e}', is_system_notification=True))
                    event_dict = {'type': 'error', 'error_type': error_type, 'message': str(e), 'stop_reason': 'error', 'traceback': traceback.format_exc(), 'turn': self._display_turn, 'context_length': self.state.current_conversation_tokens, 'usage': {'input': last_input_tokens, 'output': last_output_tokens, 'total_input': self.total_input_tokens, 'total_output': self.total_output_tokens}}
                    self._add_conversation_data_to_event(event_dict)
                    yield event_dict
                    return
                except Exception as e:
                    log('ERROR', 'core.agent', f'[Agent] Unexpected exception in process_query: {e}')
                    if self.logger:
                        self.logger.log_error('UNEXPECTED_ERROR', str(e))
                        self.logger.log_system_resources()
                        self.logger.log_agent_end('unexpected_error', f'Unexpected error: {e}')
                        self.logger.close()
                    if self.session is not None:
                        self._add_to_conversation(Message(role='user', content=f'[SYSTEM NOTIFICATION] Error: UNEXPECTED_ERROR: {e}', is_system_notification=True))
                    event_dict = {'type': 'error', 'error_type': 'UNEXPECTED_ERROR', 'message': str(e), 'stop_reason': 'error', 'traceback': traceback.format_exc(), 'turn': self._display_turn, 'context_length': self.state.current_conversation_tokens, 'usage': {'input': last_input_tokens, 'output': last_output_tokens, 'total_input': self.total_input_tokens, 'total_output': self.total_output_tokens}}
                    self._add_conversation_data_to_event(event_dict)
                    yield event_dict
                    return
                content = response.content or ''
                reasoning = response.reasoning
                tool_calls = response.tool_calls
                # Log raw tool call arguments at the closest point to the LLM response
                if tool_calls:
                    log('DEBUG', 'core.agent', 'RAW TOOL CALL ARGUMENTS from LLM response',
                        {'tool_calls': tool_calls}, truncate_hint=None)
                else:
                    log('DEBUG', 'core.agent', 'NO TOOL CALLS in LLM response', truncate_hint=None)
                user_interaction_message = None
                pause_debug(f'Checking pause request before turn: _pause_requested={self._pause_requested}')
                log('DEBUG', 'core.pause', f'PAUSE CHECKPOINT [2] after_llm: _pause_requested={self._pause_requested}, has_tool_calls={bool(tool_calls)}')
                if self._pause_requested:
                    if tool_calls:
                        log('DEBUG', 'core.pause', 'PAUSE CHECKPOINT [2] after_llm: DEFERRED (tool_calls present)')
                        # Defer pause: tools need to execute first.
                        # _pause_requested stays True -> checkpoint 2 (line ~998) will catch it
                        # after tools have run and turn_transaction has been committed.
                        pause_debug(f'Pause requested but tool_calls present, deferring to after tool execution')
                    else:
                        log('DEBUG', 'core.pause', 'PAUSE CHECKPOINT [2] after_llm: DETECTED')
                        pause_debug(f'Pause detected! Transitioning to PAUSING')
                        # Save grace turn: commit LLM response to user_history BEFORE yielding pause
                        assistant_msg = {'role': 'assistant', 'content': content}
                        if reasoning is not None:
                            assistant_msg['reasoning_content'] = reasoning
                        elif tool_calls:
                            assistant_msg['reasoning_content'] = ''
                        # Don't include tool_calls — they weren't executed
                        grace_tx = TurnTransaction(self.session, self.context_builder, conversation=None if self._session is not None else self._conversation)
                        grace_tx.add_assistant_message(assistant_msg)
                        grace_tx.commit()
                        events = self.state.set_execution_state(ExecutionState.PAUSING)
                        for event in events:
                            for yielded_event in self._handle_state_event(event):
                                yield yielded_event
                        pause_debug(f'Clearing _pause_requested after pause')
                        self._pause_requested = False
                        pause_event = {'type': 'paused', 'stop_reason': 'paused', 'turn': self._display_turn, 'context_length': self.state.current_conversation_tokens}
                        self._add_conversation_data_to_event(pause_event)
                        yield pause_event
                        turn_duration = time.time() - turn_start_time
                        if self.logger:
                            self.logger.log_system_resources()
                            self.logger.log_turn_complete(turn, {'input': last_input_tokens, 'output': last_output_tokens, 'duration_ms': turn_duration * 1000, 'context_tokens': self.state.current_conversation_tokens})
                        return
                turn_transaction = TurnTransaction(self.session, self.context_builder, conversation=None if self._session is not None else self._conversation)
                assistant_msg = {'role': 'assistant', 'content': content}
                if reasoning is not None:
                    assistant_msg['reasoning_content'] = reasoning
                elif tool_calls:
                    assistant_msg['reasoning_content'] = ''
                if tool_calls:
                    assistant_msg['tool_calls'] = tool_calls
                turn_transaction.add_assistant_message(assistant_msg)
                log('DEBUG', 'core.agent', f'Added assistant message to turn_transaction: has_tool_calls={bool(tool_calls)}, content_len={len(content)}')
                # Commit assistant message to user_history BEFORE yielding ANY event
                # (token_update, turn, tool_call, tool_result) so a pause between yields
                # doesn't lose the assistant's tool_calls data from user_history.
                if turn_transaction and turn_transaction.has_assistant_message():
                    turn_transaction.commit_assistant_only()
                log('DEBUG', '**pipeline.token_update**', f"Token update after assistant commit: tokens={self.state.current_conversation_tokens}")
                yield self._create_token_update_event()
                turn_event = {'type': 'turn', 'content': content, 'assistant_content': content, 'tool_calls': [], 'turn': self._display_turn, 'context_length': self.state.current_conversation_tokens, 'usage': {'input': last_input_tokens, 'output': last_output_tokens, 'total_input': self.total_input_tokens, 'total_output': self.total_output_tokens}}
                if reasoning is not None:
                    turn_event['reasoning'] = reasoning
                elif tool_calls:
                    turn_event['reasoning'] = ''
                self._add_conversation_data_to_event(turn_event)
                yield turn_event
                if tool_calls:
                    executed_tools, final_detected, respond_result, summary_text, summary_keep_recent_turns = self.tool_executor.execute_tool_calls(tool_calls, add_to_conversation_func=self._add_to_conversation, agent_id=0, session_id=self.session_id, turn_transaction=turn_transaction)
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
                    # Commit ALL tool results to user_history BEFORE yielding any events
                    # (tool_call, tool_result) so a pause between yields doesn't lose data.
                    if turn_transaction:
                        turn_transaction.commit()
                    for tool in processed_tools:
                        event_dict = {'type': 'tool_call', 'tool_name': tool['name'], 'arguments': tool['arguments'], 'success': tool['success'], 'error': tool['error'], 'turn': tool['turn']}
                        self._add_conversation_data_to_event(event_dict)
                        yield event_dict
                    for tool in processed_tools:
                        event_dict = {'type': 'tool_result', 'tool_name': tool['name'], 'result': tool['result'], 'success': tool['success'], 'error': tool['error'], 'turn': tool['turn']}
                        self._add_conversation_data_to_event(event_dict)
                        yield event_dict
                    # Flush any buffered token warnings after the turn is committed
                    # so they land chronologically after tool results in user_history
                    log('DEBUG', '**pipeline.warning**', f"Flushing {len(self._pending_warnings)} buffered warnings after tool execution")
                    for warning in self._pending_warnings:
                        self._add_to_conversation(warning)
                    # Yield token_warning events to event stream subscribers (worker, EventLogger, etc.)
                    for warning_event in self._pending_warning_events:
                        yield warning_event
                    self._pending_warnings.clear()
                    self._pending_warning_events.clear()
                    log('DEBUG', 'core.agent', f"[TOOL LOOP] conversation length now {len(self.conversation)}, last 3 roles: {[m.get('role') for m in self.conversation[-3:]]}")
                    if final_detected:
                        # Unified respond event — carries response_type so consumers
                        # can distinguish 'answer' (no reply needed) from 'question' (waiting for user).
                        respond_event = {
                            'type': 'agent_responded',
                            'response_type': respond_result['response_type'] if respond_result else 'answer',
                            'content': respond_result['content'] if respond_result else content,
                            'turn': self._display_turn,
                            'context_length': self.state.current_conversation_tokens,
                            'usage': {'input': last_input_tokens, 'output': last_output_tokens, 'total_input': self.total_input_tokens, 'total_output': self.total_output_tokens}
                        }
                        if reasoning is not None:
                            respond_event['reasoning'] = reasoning
                        elif tool_calls:
                            respond_event['reasoning'] = ''
                        self._add_conversation_data_to_event(respond_event)
                        yield respond_event
                        turn_duration = time.time() - turn_start_time
                        if self.logger:
                            self.logger.log_system_resources()
                            self.logger.log_turn_complete(turn, {'input': last_input_tokens, 'output': last_output_tokens, 'duration_ms': turn_duration * 1000, 'context_tokens': self.state.current_conversation_tokens})
                        return
                    if summary_text is not None:
                        log('DEBUG', 'core.summary', f'Processing summary request: summary length={len(summary_text)}, keep_recent_turns={summary_keep_recent_turns}')
                        recovery_events = self._apply_summary_pruning(summary_text, summary_keep_recent_turns)
                        log('DEBUG', '**pipeline.token_update**', f"Token update after summary pruning: tokens={self.state.current_conversation_tokens}")
                        yield self._create_token_update_event()
                        # Yield the original recovery events (token_recovery) unchanged,
                        # followed by a context_summarized event so the frontend can show
                        # the meaningful notification text.  The context_summarized event
                        # replaces the old context_cleared event (which was redundant
                        # since context_summarized carries a richer message).
                        for recovery_event in (recovery_events or []):
                            self._add_conversation_data_to_event(recovery_event)
                            yield recovery_event
                            # Yield a context_summarized event so the frontend can show
                            # the actual system notification text in the worker output panel.
                            # Context_summarized is the canonical event; the old
                            # context_cleared event was removed to avoid triple-notification
                            # storms (token_recovery + context_cleared + context_summarized
                            # for a single summary action).
                            summarized_event = dict(recovery_event)
                            summarized_event['type'] = 'context_summarized'
                            summarized_event['message'] = 'Context has been summarized. You now have a fresh context window and full access to tools.'
                            yield summarized_event

                        # Continue the turn loop — summary frees context, agent keeps working
                        turn_duration = time.time() - turn_start_time
                        if self.logger:
                            self.logger.log_system_resources()
                            self.logger.log_turn_complete(turn, {'input': last_input_tokens, 'output': last_output_tokens, 'duration_ms': turn_duration * 1000, 'context_tokens': self.state.current_conversation_tokens})
                        # Skip the 'if not tool_calls' check since we had tools (SummarizeTool)
                        continue
                pause_debug(f'Checking pause request after turn processing: _pause_requested={self._pause_requested}')
                log('DEBUG', 'core.pause', f'PAUSE CHECKPOINT [3] after_turn: _pause_requested={self._pause_requested}')
                if self._pause_requested:
                    log('DEBUG', 'core.pause', 'PAUSE CHECKPOINT [3] after_turn: DETECTED')
                    pause_debug(f'Pause detected after turn processing! Transitioning to PAUSING')
                    events = self.state.set_execution_state(ExecutionState.PAUSING)
                    for event in events:
                        for yielded_event in self._handle_state_event(event):
                            yield yielded_event
                    pause_debug(f'Clearing _pause_requested after pause (after turn processing)')
                    self._pause_requested = False
                    pause_event = {'type': 'paused', 'stop_reason': 'paused', 'turn': self._display_turn, 'context_length': self.state.current_conversation_tokens}
                    self._add_conversation_data_to_event(pause_event)
                    yield pause_event
                    turn_duration = time.time() - turn_start_time
                    if self.logger:
                        self.logger.log_system_resources()
                        self.logger.log_turn_complete(turn, {'input': last_input_tokens, 'output': last_output_tokens, 'duration_ms': turn_duration * 1000, 'context_tokens': self.state.current_conversation_tokens})
                    return
                if not tool_calls:
                    log('DEBUG', 'core.agent', f'No tool calls in turn {turn}, committing directly and yielding final')
                    if turn_transaction and turn_transaction.has_assistant_message():
                        turn_transaction.commit()
                    # Flush any buffered token warnings (unlikely here, but be safe)
                    log('DEBUG', '**pipeline.warning**', f"Flushing {len(self._pending_warnings)} buffered warnings in non-tool branch")
                    for warning in self._pending_warnings:
                        self._add_to_conversation(warning)
                    # Yield token_warning events to event stream subscribers
                    for warning_event in self._pending_warning_events:
                        yield warning_event
                    self._pending_warnings.clear()
                    self._pending_warning_events.clear()
                    if self.logger:
                        self.logger.log_system_resources()
                        self.logger.log_agent_end('completed', 'Assistant provided direct answer with no tool calls')
                        self.logger.close()
                    final_event = {'type': 'agent_responded', 'response_type': 'answer', 'content': content, 'turn': self._display_turn, 'context_length': self.state.current_conversation_tokens, 'usage': {'input': last_input_tokens, 'output': last_output_tokens, 'total_input': self.total_input_tokens, 'total_output': self.total_output_tokens}}
                    if reasoning is not None:
                        final_event['reasoning'] = reasoning
                    elif tool_calls:
                        final_event['reasoning'] = ''
                    self._add_conversation_data_to_event(final_event)
                    yield final_event
                    return
    

            # Max turns reached - loop exhausted naturally
            log('WARNING', 'core.agent', f'Max turns ({self.config.max_turns}) reached - loop exhausted')
            if self.logger:
                self.logger.log_agent_end('max_turns_reached', f'Max turns ({self.config.max_turns}) reached')
                self.logger.close()
            max_turns_event = {
                'type': 'stop_reason',
                'stop_reason': 'max_turns_reached',
                'turns': self.config.max_turns,
                'turn': self._display_turn,
                'context_length': self.state.current_conversation_tokens,
                'usage': {'input': last_input_tokens, 'output': last_output_tokens, 'total_input': self.total_input_tokens, 'total_output': self.total_output_tokens}
            }
            self._add_conversation_data_to_event(max_turns_event)
            yield max_turns_event
            return
        finally:
            self.state.execution_state = ExecutionState.READY
            log('DEBUG', 'core.agent', 'process_query finished, reset execution_state to READY')
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
            log('WARNING', 'core.pruning', 'No session available, using fallback pruning')
            self._apply_summary_pruning_fallback(summary, keep_recent_turns)
            old_token_count = self.state.current_conversation_tokens
            self._update_conversation_token_estimate()
            # Immediately re-evaluate token state to clear restrictions if below critical
            self.state.update_token_state(self.state.current_conversation_tokens)
            log('DEBUG', 'core.pruning', f'Fallback pruning token change: {old_token_count} -> {self.state.current_conversation_tokens}')
            if self.logger and hasattr(self.logger, 'py_logger'):
                log('INFO', 'core.agent', f'[PRUNING] Updated token estimate after fallback: {self.state.current_conversation_tokens} tokens (was {old_token_count})')
            return
        user_history = self.session.user_history
        log('DEBUG', 'core.session_history', f'session.user_history length: {len(user_history)}')
        log('DEBUG', 'core.session_history', f'session.summary set: {self.session.summary is not None}')
        insertion_idx = self._find_summary_insertion_index(user_history, keep_recent_turns)
        log('DEBUG', 'core.summary', f"insertion_idx={insertion_idx}, keep_recent_turns={keep_recent_turns}")
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
        MAX_SUMMARY_LENGTH = 20000
        truncated_summary = summary
        if len(truncated_summary) > MAX_SUMMARY_LENGTH:
            truncated_summary = truncated_summary[:MAX_SUMMARY_LENGTH] + '... [truncated]'
        # Insert summary message into history
        summary_msg = {'role': 'system', 'content': f'Summary of previous conversation: {truncated_summary}', 'summary': True, 'pruning_keep_recent_turns': keep_recent_turns, 'pruning_discarded_msg_count': discarded_msg_count, 'pruning_insertion_idx': insertion_idx}
        if insertion_idx >= len(user_history):
            user_history.append(summary_msg)
            log('DEBUG', 'core.message_insertion', f'Appended summary at end (insertion_idx={insertion_idx} >= len={len(user_history)})')
        else:
            user_history.insert(insertion_idx, summary_msg)
            log('DEBUG', 'core.message_insertion', f'Inserted summary at index {insertion_idx}')
        context_cleared_msg = Message(role='user', content='[SYSTEM NOTIFICATION] Context has been summarized. You now have a fresh context window and full access to tools.', is_system_notification=True)
        # Append unwarning after the tool result (at the end of user_history)
        user_history.append(context_cleared_msg)
        log('DEBUG', 'core.message_insertion', f'Appended context cleared message at end (history length: {len(user_history)})')
        self.session.summary = summary_msg
        self.session.updated_at = datetime.now()
        if self.conversation is not user_history:
            self.conversation = user_history
        log('DEBUG', 'core.context_builder', f"_apply_summary_pruning: clearing context_builder cache, exists={hasattr(self, 'context_builder')}, is None={(self.context_builder if hasattr(self, 'context_builder') else 'no attr')}, has _cached_context={(hasattr(self.context_builder, '_cached_context') if hasattr(self, 'context_builder') and self.context_builder is not None else False)}")
        # Update token estimate AFTER summary insertion
        old_token_count = self.state.current_conversation_tokens
        self._update_conversation_token_estimate()
        # Immediately re-evaluate token state to clear restrictions if below critical
        token_events = self.state.update_token_state(self.state.current_conversation_tokens)
        log('INFO', 'core.token', f"post-summary: tokens={self.state.current_conversation_tokens}, token_state={self.state.token_state.value if hasattr(self.state, 'token_state') else 'N/A'}, turn_state={self.state.turn_state.value if hasattr(self.state, 'turn_state') else 'N/A'}, restrictions_active={self.state.restrictions_active}")
        log('DEBUG', 'core.pruning', f'Summary pruning token change: {old_token_count} -> {self.state.current_conversation_tokens}, summary_idx={insertion_idx}, kept_turns={kept_turns_count}')
        if self.logger and hasattr(self.logger, 'py_logger'):
            log('INFO', 'core.agent', f'[PRUNING] Updated token estimate: {self.state.current_conversation_tokens} tokens (was {old_token_count})')
        log('DEBUG', 'core.pruning', f'_apply_summary_pruning completed. History length: {len(user_history)} messages')
        log('DEBUG', 'core.session_history', f'session.summary exists: {self.session.summary is not None}')
        # Reset emergency mode after a successful summary
        self.context_builder.emergency_mode = False
        self._emergency_retries = 0
        return [ev for ev in token_events if ev['type'] == 'token_recovery']


    def _apply_summary_pruning_fallback(self, summary: str, keep_recent_turns: int):
        """Fallback pruning for when no session is available (legacy behavior).

        This mirrors the main _apply_summary_pruning path: it mutates the existing
        conversation list via insert/append rather than replacing it entirely.
        """
        log('WARNING', 'core.pruning', '[DEBUG_PRUNING] Using fallback pruning (no session)')
        if not self.conversation:
            return
        # Use _find_summary_insertion_index (which uses the shared turn-grouping utility)
        # to find the correct insertion point
        insertion_idx = self._find_summary_insertion_index(self.conversation, keep_recent_turns)
        MAX_SUMMARY_LENGTH = 20000
        truncated_summary = summary
        if len(truncated_summary) > MAX_SUMMARY_LENGTH:
            truncated_summary = truncated_summary[:MAX_SUMMARY_LENGTH] + '... (truncated)'
        summary_msg = {'role': 'system', 'content': f'Summary of previous conversation: {truncated_summary}', 'summary': True, 'pruning_keep_recent_turns': keep_recent_turns, 'pruning_insertion_idx': insertion_idx}
        context_cleared_msg = Message(role='user', content='[SYSTEM NOTIFICATION] Context has been summarized. You now have a fresh context window and full access to tools.', is_system_notification=True)
        # Insert summary at the computed position (mutating the existing list)
        self.conversation.insert(insertion_idx, summary_msg)
        log('DEBUG', 'core.message_insertion', f'Fallback: inserted summary at index {insertion_idx}')
        # Append context-cleared notification at the end
        self.conversation.append(context_cleared_msg)
        log('DEBUG', 'core.message_insertion', f'Fallback: appended context cleared message (history length: {len(self.conversation)})')
        # Invalidate context_builder cache to pick up the new summary
        if hasattr(self, 'context_builder') and self.context_builder is not None and hasattr(self.context_builder, '_cached_context'):
            self.context_builder._cached_context = None
            log('DEBUG', 'core.context_builder', 'Fallback pruning: cleared _cached_context')
        log('DEBUG', 'core.pruning', f'[DEBUG_PRUNING] Fallback pruning: conversation length {len(self.conversation)}')

    def _find_summary_insertion_index(self, user_history: List[Dict[str, Any]], keep_recent_turns: int) -> int:
        """Find index in user_history where summary should be inserted.

        Uses the shared group_messages_into_turns_with_indices utility which
        supports multi-tool-call scenarios (checking ANY message in the turn
        for assistant with tool_calls, not just the last message).

        Returns the index of the first message of the first kept turn.
        If keep_recent_turns is 0 or no turns to keep, returns len(user_history).
        """
        if keep_recent_turns <= 0:
            return len(user_history)
        turns, turn_start_indices = group_messages_into_turns_with_indices(user_history)
        if not turns:
            return len(user_history)
        if keep_recent_turns > len(turns):
            keep_recent_turns = len(turns)
        first_kept_idx = len(turns) - keep_recent_turns
        return turn_start_indices[first_kept_idx]

    def _group_messages_into_turns(self, messages: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        """Group non-system messages into turns.

        Delegates to the shared group_messages_into_turns utility which provides
        a single source of truth for turn-grouping logic. The shared version
        uses the more robust multi-tool-call approach (checking ANY message in
        the turn for assistant with tool_calls, not just the last message).
        """
        return group_messages_into_turns(messages)

    def reset(self):
        """Reset agent state."""
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
        config_data = {'api_key': api_key or '', 'base_url': base_url, 'model': preset.model, 'temperature': preset.temperature, 'enabled_tools': list(preset_tool_names), 'system_prompt': preset.system_prompt, 'provider_type': 'openai_compatible', 'max_turns': overrides.get('max_turns', 100), 'detail': overrides.get('detail', 'normal'), 'workspace_path': overrides.get('workspace_path'), 'tool_output_token_limit': overrides.get('tool_output_token_limit', 10000), 'token_monitor_warning_threshold': overrides.get('token_monitor_warning_threshold', 65000), 'token_monitor_critical_threshold': overrides.get('token_monitor_critical_threshold', 80000), 'turn_monitor_enabled': overrides.get('turn_monitor_enabled', True), 'enable_logging': overrides.get('enable_logging', True), 'log_dir': overrides.get('log_dir', './logs'), 'log_level': overrides.get('log_level', 'INFO'), 'enable_file_logging': overrides.get('enable_file_logging', True), 'jsonl_format': overrides.get('jsonl_format', True), 'log_categories': overrides.get('log_categories', ['SESSION', 'LLM', 'TOOLS']), 'max_file_size_mb': overrides.get('max_file_size_mb', 10)}
        config_data.update(overrides)
        from agent.config import AgentConfig
        config = AgentConfig(**config_data)
        return cls(config, session=session)