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

class TimeState(enum.Enum):
    """Time-based execution state based on elapsed runtime."""
    LOW = 'low'
    WARNING = 'warning'
    CRITICAL = 'critical'

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
    time_state: TimeState = TimeState.LOW
    execution_state: ExecutionState = ExecutionState.READY
    session_state: SessionState = SessionState.NEW
    current_conversation_tokens: int = 0
    current_turn: int = 0
    restrictions_pending: bool = False
    restrictions_active: bool = False
    last_token_warning_state: TokenState = TokenState.LOW
    last_turn_warning_state: TurnState = TurnState.LOW
    last_time_warning_state: TimeState = TimeState.LOW
    last_token_warning: Optional[str] = None
    last_token_warning_count: int = 0
    last_turn_warning: Optional[str] = None
    last_turn_warning_count: int = 0
    last_time_warning: Optional[str] = None
    last_time_warning_count: int = 0
    timeout_seconds: int = 300
    time_warning_threshold: int = 240
    time_start: Optional[float] = None
    restriction_reason: Optional[str] = None
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
        log('DEBUG', 'core.token', f'total_tokens={total_tokens}, warning_threshold={self.config.token_monitor_warning_threshold}, critical_threshold={self.config.token_monitor_critical_threshold}')
        log('WARNING', 'core.token', f"update_token_state: total_tokens={total_tokens} threshold={self.config.token_monitor_warning_threshold if hasattr(self.config, 'token_monitor_warning_threshold') else 'N/A'}")
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
                    f'The summary will free up the context window and you can continue working smoothly. '
                    f'Tip: For long-running tasks, store intermediate results and subtask status in KnowledgeBase '
                    f'to avoid losing context when summarizing.'
                )
            else:
                formatted = self._format_tokens(total_tokens)
                warning = f'Token usage is at the critical threshold ({formatted} tokens). Please summarize to reduce context size or complete work. Only Respond and SummarizeTool are available.'
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
            self.restriction_reason = 'token'
        elif self.token_state != TokenState.CRITICAL and self.turn_state == TurnState.LOW:
            self.restrictions_pending = False
            self.restrictions_active = False
            self.restriction_reason = None

        if new_state == TokenState.LOW:
            self.last_token_warning_state = TokenState.LOW
        return events

    def update_time_state(self, elapsed_seconds: float) -> List[Dict[str, Any]]:
        """Update time state based on elapsed runtime.

        Compares elapsed runtime against configured timeout thresholds.
        Issues a single warning when elapsed passes the warning threshold
        and activates restrictions when elapsed passes the critical threshold.

        Returns list of events (e.g., warnings) that should be yielded.
        """
        if not hasattr(self.config, 'time_monitor_enabled') or not self.config.time_monitor_enabled:
            self.time_state = TimeState.LOW
            return []

        timeout = getattr(self.config, 'timeout_seconds', self.timeout_seconds)
        warning_at = getattr(self.config, 'time_warning_threshold', self.time_warning_threshold)

        # Defensive: if warning_at >= timeout, skip WARNING and go straight to CRITICAL
        if warning_at >= timeout:
            if elapsed_seconds < timeout:
                new_state = TimeState.LOW
            else:
                new_state = TimeState.CRITICAL
        elif elapsed_seconds < warning_at:
            new_state = TimeState.LOW
        elif elapsed_seconds < timeout:
            new_state = TimeState.WARNING
        else:
            new_state = TimeState.CRITICAL

        old_state = self.time_state
        self.time_state = new_state
        events = []

        state_order = {TimeState.LOW: 0, TimeState.WARNING: 1, TimeState.CRITICAL: 2}
        if state_order[new_state] > state_order[old_state] and self.last_time_warning_state != new_state:
            if new_state == TimeState.WARNING:
                remaining = timeout - elapsed_seconds
                warning = (
                    f'**Time warning**: Agent has been running for '
                    f'{elapsed_seconds:.1f}s ({remaining:.1f}s remaining before timeout). '
                    f'Tool restrictions will be applied when the timeout is reached.'
                )
            else:
                warning = (
                    f'**Time critical**: Agent has exceeded the timeout '
                    f'({timeout}s). Tool restrictions are now active. '
                    f'Only the Respond tool is available. Please finish your work '
                    f'and respond immediately.'
                )
            self.last_time_warning = warning
            self.last_time_warning_count = int(elapsed_seconds)
            self.last_time_warning_state = new_state

            if new_state == TimeState.CRITICAL:
                self.restrictions_active = True
                self.restrictions_pending = False
                self.restriction_reason = 'timeout'

            if self.logger:
                self.logger.log_time_warning(old_state.value, new_state.value, elapsed_seconds, warning)

            time_warning_data = {
                'old_state': old_state.value,
                'new_state': new_state.value,
                'elapsed_seconds': elapsed_seconds,
                'warning_message': warning,
                'state': new_state.value,
            }
            events.append(self._create_event('time_warning', time_warning_data))

        if new_state == TimeState.LOW:
            self.last_time_warning_state = TimeState.LOW
            self.restriction_reason = None

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
        if warning_turn < 0 or current_turn < warning_turn:
            new_state = TurnState.LOW
        else:
            new_state = TurnState.WARNING
        old_state = self.turn_state
        self.turn_state = new_state
        events = []
        if new_state == TurnState.WARNING and old_state != TurnState.WARNING and self.last_turn_warning_state != TurnState.WARNING:
            self.restrictions_active = True
            self.restrictions_pending = False
            self.restriction_reason = 'turn'
            warning = (
                f'**Turn limit warning**: You are running out of turns ({current_turn}/{max_turns}). '
                f'You must wait for the user to re-enable your session. Please provide a final answer now '
                f'so the user can decide if they want to send you further queries or if they are happy '
                f'with the result you have so far. Only Respond is available.'
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
            self.restriction_reason = None
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
        self.restriction_reason = None
        self.turn_state = TurnState.LOW
        self.current_turn = 0
        self.last_turn_warning_state = TurnState.LOW
        self.last_turn_warning = None
        self.last_turn_warning_count = 0
        self.time_state = TimeState.LOW
        self.time_start = None
        self.last_time_warning_state = TimeState.LOW
        self.last_time_warning = None
        self.last_time_warning_count = 0
        events.extend(self.set_execution_state(ExecutionState.READY))
        events.extend(self.set_session_state(SessionState.NEW))
        return events

    def get_allowed_tools(self) -> List[str]:
        """Get list of allowed tool names based on current states.

        When restrictions_active is True, only Respond (and optionally SummarizeTool) are allowed.
        For timeout restrictions, only Respond is available (no SummarizeTool).
        """
        if self.restrictions_active:
            if self.restriction_reason == 'timeout':
                return ['Respond']
            return ['Respond', 'SummarizeTool']
        return []

    def is_tool_allowed(self, tool_name: str) -> bool:
        """Check if a specific tool is allowed in current state."""
        allowed = self.get_allowed_tools()
        if not allowed:
            return True
        return tool_name in allowed