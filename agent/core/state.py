from __future__ import annotations
import enum
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
import typing
from agent import events as ev
from agent.logging import log

class TokenState(enum.Enum):
    """Token usage state based on conversation token count."""
    LOW = 'low'
    WARNING = 'warning'
    CRITICAL = 'critical'

class TurnState(enum.Enum):
    """Turn usage state based on turn count."""
    LOW = 'low'
    WARNING = 'warning'

class ExecutionState(enum.Enum):
    """Unified execution state for agent and GUI."""
    RUNNING = 'running'
    PAUSING = 'pausing'
    READY = 'ready'

class SessionState(enum.Enum):
    """Session state of the agent."""
    NEW = 'new'
    CONTINUING = 'continuing'
    RESETTING = 'resetting'

@dataclass
class AgentState:
    """Encapsulates all agent states with transition logic."""
    config: Any
    logger: Optional[Any] = None
    security_config: Optional[Dict[str, Any]] = None
    token_state: TokenState = TokenState.LOW
    turn_state: TurnState = TurnState.LOW
    execution_state: ExecutionState = ExecutionState.READY
    session_state: SessionState = SessionState.NEW
    current_conversation_tokens: int = 0
    current_turn: int = 0
    restrictions_pending: bool = False
    restrictions_active: bool = False
    last_token_warning_state: TokenState = TokenState.LOW
    last_turn_warning_state: TurnState = TurnState.LOW
    last_token_warning: Optional[str] = None
    last_token_warning_count: int = 0
    last_turn_warning: Optional[str] = None
    last_turn_warning_count: int = 0
    _pending_events: List[Dict[str, Any]] = field(default_factory=list)


    def _create_event(self, event_type, data):
        """Create a typed event and convert to legacy dictionary format."""
        event = ev.create_event(event_type, data, source='agent_state')
        legacy_dict = ev.convert_to_legacy_format(event)
        return legacy_dict

    def _format_tokens(self, tokens: int) -> str:
        """Format token count in thousands with 'k' suffix."""
        if tokens >= 1000:
            return f'{tokens // 1000}k'
        return str(tokens)

    def update_token_state(self, total_tokens: int) -> List[Dict[str, Any]]:
        """Update token state based on current token count.
        
        Returns list of events (e.g., warnings) that should be yielded.
        """
        self.current_conversation_tokens = total_tokens
        log('DEBUG', 'core.token_state', f'total_tokens={total_tokens}, warning_threshold={self.config.token_monitor_warning_threshold}, critical_threshold={self.config.token_monitor_critical_threshold}')
        if not self.config.token_monitor_enabled:
            self.token_state = TokenState.LOW
            return []
        if total_tokens < self.config.token_monitor_warning_threshold:
            new_state = TokenState.LOW
        elif total_tokens < self.config.token_monitor_critical_threshold:
            new_state = TokenState.WARNING
        else:
            new_state = TokenState.CRITICAL
        old_state = self.token_state
        self.token_state = new_state
        events = []
        state_order = {TokenState.LOW: 0, TokenState.WARNING: 1, TokenState.CRITICAL: 2}
        if state_order[new_state] > state_order[old_state] and self.last_token_warning_state != new_state and (new_state in (TokenState.WARNING, TokenState.CRITICAL)):
            if new_state == TokenState.WARNING:
                formatted = self._format_tokens(total_tokens)
                critical_formatted = self._format_tokens(self.config.token_monitor_critical_threshold)
                warning = (
                    f'**Token usage warning: Conversation is nearing context window limits** ({formatted} tokens). '
                    f'Critical threshold is at {critical_formatted} tokens. '
                    f'This is not a problem: simply use SummarizeTool to summarize the session and keep a number of recent turns. '
                    f'The summary will free up the context window and you can continue working smoothly.'
                )
            else:
                formatted = self._format_tokens(total_tokens)
                warning = f'Token usage is at the critical threshold ({formatted} tokens). Please summarize to reduce context size or complete work. Only SummarizeTool, Final, and FinalReport will be available.'
            if new_state != TokenState.CRITICAL:
                self.last_token_warning = warning
                self.last_token_warning_count = total_tokens
                self.last_token_warning_state = new_state
            if self.logger:
                self.logger.log_token_warning(old_state.value, new_state.value, total_tokens, warning)
            token_warning_data = {'old_state': old_state.value, 'new_state': new_state.value, 'token_count': total_tokens, 'warning_message': warning, 'state': new_state.value}
            events.append(self._create_event('token_warning', token_warning_data))
        # Immediate restriction activation: no grace turn
        if self.token_state == TokenState.CRITICAL:
            self.restrictions_active = True
            self.restrictions_pending = False
        elif self.token_state != TokenState.CRITICAL and self.turn_state == TurnState.LOW:
            self.restrictions_pending = False
            self.restrictions_active = False

        if new_state == TokenState.LOW:
            self.last_token_warning_state = TokenState.LOW
        return events

    def update_turn_state(self, current_turn: int) -> List[Dict[str, Any]]:
        """Update turn state based on current turn count.

        Uses a fixed warning threshold: when current_turn >= max_turns - 3,
        a single final warning is issued and tool restrictions activate.

        Returns list of events (e.g., warnings) that should be yielded.
        """
        self.current_turn = current_turn
        if not self.config.turn_monitor_enabled:
            self.turn_state = TurnState.LOW
            return []
        max_turns = self.config.max_turns
        warning_turn = max_turns - 3
        if current_turn < warning_turn:
            new_state = TurnState.LOW
        else:
            new_state = TurnState.WARNING
        old_state = self.turn_state
        self.turn_state = new_state
        events = []
        if new_state == TurnState.WARNING and old_state != TurnState.WARNING and self.last_turn_warning_state != TurnState.WARNING:
            self.restrictions_active = True
            self.restrictions_pending = False
            warning = (
                f'**Turn limit warning**: You are running out of turns ({current_turn}/{max_turns}). '
                f'You must wait for the user to re-enable your session. Please provide a final answer now '
                f'so the user can decide if they want to send you further queries or if they are happy '
                f'with the result you have so far. Only Final and FinalReport are available.'
            )
            self.last_turn_warning = warning
            self.last_turn_warning_count = current_turn
            self.last_turn_warning_state = TurnState.WARNING
            if self.logger:
                self.logger.log_turn_warning(old_state.value, new_state.value, current_turn, warning)
            turn_warning_data = {'old_state': old_state.value, 'new_state': new_state.value, 'turn_count': current_turn, 'warning_message': warning, 'state': new_state.value}
            events.append(self._create_event('turn_warning', turn_warning_data))
        if new_state == TurnState.LOW:
            self.last_turn_warning_state = TurnState.LOW
            self.restrictions_active = False
            self.restrictions_pending = False
        return events
    def set_execution_state(self, new_state: ExecutionState) -> List[Dict[str, Any]]:
        """Transition to a new execution state.
        
        Returns list of events (e.g., state change notifications).
        """
        old_state = self.execution_state
        self.execution_state = new_state
        if self.logger:
            self.logger.log_execution_state_change(old_state.value, new_state.value)
        execution_state_data = {'old_state': old_state.value, 'new_state': new_state.value}
        return [self._create_event('execution_state_change', execution_state_data)]

    def set_session_state(self, new_state: SessionState) -> List[Dict[str, Any]]:
        """Transition to a new session state.
        
        Returns list of events (e.g., session notifications).
        """
        old_state = self.session_state
        self.session_state = new_state
        if self.logger:
            self.logger.log_session_state_change(old_state.value, new_state.value)
        session_state_data = {'old_state': old_state.value, 'new_state': new_state.value}
        return [self._create_event('session_state_change', session_state_data)]

    def reset(self) -> List[Dict[str, Any]]:
        """Reset all states to initial values.
        
        Returns list of events for the reset.
        """
        events = []
        self.token_state = TokenState.LOW
        self.current_conversation_tokens = 0
        self.last_token_warning_state = TokenState.LOW
        self.last_token_warning = None
        self.last_token_warning_count = 0
        self.restrictions_pending = False
        self.restrictions_active = False
        self.turn_state = TurnState.LOW
        self.current_turn = 0
        self.last_turn_warning_state = TurnState.LOW
        self.last_turn_warning = None
        self.last_turn_warning_count = 0
        events.extend(self.set_execution_state(ExecutionState.READY))
        events.extend(self.set_session_state(SessionState.NEW))
        return events


    def get_allowed_tools(self) -> List[str]:
        """Get list of allowed tool names based on current states.

        When restrictions_active is True, only Final, FinalReport, and SummarizeTool are allowed.
        """
        if self.restrictions_active:
            return ['Final', 'FinalReport', 'SummarizeTool']
        return []

    def is_tool_allowed(self, tool_name: str) -> bool:
        """Check if a specific tool is allowed in current state."""
        allowed = self.get_allowed_tools()
        if not allowed:
            return True
        return tool_name in allowed