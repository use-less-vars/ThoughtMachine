# agent_state.py - State machine for ThoughtMachine agent
from __future__ import annotations
import enum
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
import typing
from agent import events as ev
from agent.logging.debug_log import debug_log


class TokenState(enum.Enum):
    """Token usage state based on conversation token count."""
    LOW = "low"
    WARNING = "warning"
    CRITICAL = "critical"


class TurnState(enum.Enum):
    """Turn usage state based on turn count."""
    LOW = "low"
    WARNING = "warning"
    CRITICAL = "critical"


class ExecutionState(enum.Enum):
    """Unified execution state for agent and GUI."""
    IDLE = "idle"                     # Initial state, ready for input
    RUNNING = "running"              # Actively processing an atomic turn
    PAUSING = "pausing"              # Pause requested, finishing current turn
    PAUSED = "paused"                # Paused after turn completion, ready for input
    WAITING_FOR_USER = "waiting_for_user"  # Waiting for user interaction (RequestUserInteraction)
    FINALIZED = "finalized"          # Completed with Final/FinalReport, ready for input
    STOPPED = "stopped"              # Stopped (error)
    MAX_TURNS_REACHED = "max_turns_reached"  # Max turns reached, ready for input
    
    # States that accept new queries: IDLE, PAUSED, WAITING_FOR_USER, FINALIZED, MAX_TURNS_REACHED
    # Terminal error state: STOPPED
    # Active states: RUNNING, PAUSING


class SessionState(enum.Enum):
    """Session state of the agent."""
    NEW = "new"
    CONTINUING = "continuing"
    RESETTING = "resetting"


@dataclass
class AgentState:
    """Encapsulates all agent states with transition logic."""

    # Configuration and dependencies
    config: Any  # AgentConfig
    logger: Optional[Any] = None
    security_config: Optional[Dict[str, Any]] = None

    # State fields
    token_state: TokenState = TokenState.LOW
    turn_state: TurnState = TurnState.LOW
    execution_state: ExecutionState = ExecutionState.IDLE
    session_state: SessionState = SessionState.NEW

    # Resource tracking
    current_conversation_tokens: int = 0
    current_turn: int = 0
    
    # CRITICAL countdown tracking
    token_critical_countdown: int = 0
    turn_critical_countdown: int = 0
    CRITICAL_COUNTDOWN_TURNS: int = 5  # Configurable number of turns after CRITICAL before restrictions

    # Warning tracking
    last_token_warning_state: TokenState = TokenState.LOW
    last_turn_warning_state: TurnState = TurnState.LOW
    last_token_warning: Optional[str] = None
    last_token_warning_count: int = 0
    last_turn_warning: Optional[str] = None
    last_turn_warning_count: int = 0    

    
    # Internal event storage
    _pending_events: List[Dict[str, Any]] = field(default_factory=list)
    
    def __post_init__(self):
        # Initialize countdown turns from config if available
        if hasattr(self.config, 'critical_countdown_turns'):
            self.CRITICAL_COUNTDOWN_TURNS = self.config.critical_countdown_turns
    
    def _create_event(self, event_type, data):
        """Create a typed event and convert to legacy dictionary format."""
        # Create typed event
        event = ev.create_event(event_type, data, source="agent_state")
        # Convert to legacy dict format
        legacy_dict = ev.convert_to_legacy_format(event)
        return legacy_dict

    def _format_tokens(self, tokens: int) -> str:
        """Format token count in thousands with 'k' suffix."""
        if tokens >= 1000:
            return f"{tokens // 1000}k"
        return str(tokens)
    
    def update_token_state(self, total_tokens: int) -> List[Dict[str, Any]]:
        """Update token state based on current token count.
        
        Returns list of events (e.g., warnings) that should be yielded.
        """
        self.current_conversation_tokens = total_tokens
        debug_log(f"total_tokens={total_tokens}, warning_threshold={self.config.token_monitor_warning_threshold}, critical_threshold={self.config.token_monitor_critical_threshold}", level="DEBUG", component="TokenState")
        
        if not self.config.token_monitor_enabled:
            self.token_state = TokenState.LOW
            return []
        
        # Determine new state
        if total_tokens < self.config.token_monitor_warning_threshold:
            new_state = TokenState.LOW
        elif total_tokens < self.config.token_monitor_critical_threshold:
            new_state = TokenState.WARNING
        else:
            new_state = TokenState.CRITICAL
        
        old_state = self.token_state
        self.token_state = new_state
        
        events = []
        
        # Check if we need to warn (only on upward transitions to a NEW warning state)
        state_order = {TokenState.LOW: 0, TokenState.WARNING: 1, TokenState.CRITICAL: 2}
        
        if (state_order[new_state] > state_order[old_state] and
            self.last_token_warning_state != new_state and
            new_state in (TokenState.WARNING, TokenState.CRITICAL)):

            # Create warning message
            if new_state == TokenState.WARNING:
                formatted = self._format_tokens(total_tokens)
                warning = (
                    f"**Token usage warning: Conversation is nearing context window limits** ({formatted} tokens). "
                    f"Consider pruning soon. If tokens reach CRITICAL threshold, you will have {self.CRITICAL_COUNTDOWN_TURNS} turns before tool restrictions apply."
                )
            else:  # CRITICAL
                # Start the countdown for gradual restrictions
                countdown_events = self.start_critical_countdown("token")
                events.extend(countdown_events)
                # Use gentler message
                formatted = self._format_tokens(total_tokens)
                warning = (
                    f"Token usage is at the critical threshold ({formatted} tokens). "
                    f"You have {self.CRITICAL_COUNTDOWN_TURNS} turns to work normally before tool restrictions apply. "
                    f"Please consider summarizing to reduce context size. After summarizing, you may continue working. "
                    f"After the countdown ends, only SummarizeTool, Final, and FinalReport will be available."
                )

            # Store warning (for CRITICAL, updated by start_critical_countdown)
            if new_state != TokenState.CRITICAL:
                self.last_token_warning = warning
                self.last_token_warning_count = total_tokens
                self.last_token_warning_state = new_state            
            # Log if logger available
            if self.logger:
                self.logger.log_token_warning(old_state.value, new_state.value, total_tokens, warning)
            
            # Create typed event
            token_warning_data = {
                "old_state": old_state.value,
                "new_state": new_state.value,
                "token_count": total_tokens,
                "warning_message": warning,
                "state": new_state.value
            }
            events.append(self._create_event("token_warning", token_warning_data))
        
        # Reset last warning state if we drop below warning threshold
        if new_state == TokenState.LOW:
            self.last_token_warning_state = TokenState.LOW
        
        return events
    
    def update_turn_state(self, current_turn: int) -> List[Dict[str, Any]]:
        """Update turn state based on current turn count.
        
        Returns list of events (e.g., warnings) that should be yielded.
        """
        self.current_turn = current_turn
        
        if not self.config.turn_monitor_enabled:
            self.turn_state = TurnState.LOW
            return []
        
        # Determine new state
        max_turns = self.config.max_turns
        warning_threshold = int(max_turns * self.config.turn_monitor_warning_threshold)
        critical_threshold = int(max_turns * self.config.turn_monitor_critical_threshold)
        
        if current_turn < warning_threshold:
            new_state = TurnState.LOW
        elif current_turn < critical_threshold:
            new_state = TurnState.WARNING
        else:
            new_state = TurnState.CRITICAL
        
        old_state = self.turn_state
        self.turn_state = new_state
        
        events = []
        
        # Check if we need to warn (only on upward transitions to a NEW warning state)
        state_order = {TurnState.LOW: 0, TurnState.WARNING: 1, TurnState.CRITICAL: 2}
        
        if (state_order[new_state] > state_order[old_state] and
            self.last_turn_warning_state != new_state and
            new_state in (TurnState.WARNING, TurnState.CRITICAL)):

            # Create warning message
            if new_state == TurnState.WARNING:
                warning = f"**Turn limit warning**: Agent is nearing maximum turn limit ({current_turn}/{max_turns} turns). Please consider wrapping up soon. If turns reach CRITICAL threshold, you will have {self.CRITICAL_COUNTDOWN_TURNS} turns before tool restrictions apply."
            else:  # CRITICAL
                # Start the countdown for gradual restrictions
                countdown_events = self.start_critical_countdown("turn")
                events.extend(countdown_events)
                # Use gentler message
                warning = (
                    f"Turn usage is at the critical threshold ({current_turn}/{max_turns} turns). "
                    f"You have {self.CRITICAL_COUNTDOWN_TURNS} turns to work normally before tool restrictions apply. "
                    f"Please consider completing your work or summarizing. After summarizing, you may continue working. "
                    f"After the countdown ends, only SummarizeTool, Final, and FinalReport will be available."
                )

            # Store warning (for CRITICAL, updated by start_critical_countdown)
            if new_state != TurnState.CRITICAL:
                self.last_turn_warning = warning
                self.last_turn_warning_count = current_turn
                self.last_turn_warning_state = new_state            
            # Log if logger available
            if self.logger:
                self.logger.log_turn_warning(old_state.value, new_state.value, current_turn, warning)
            
            # Create typed event
            turn_warning_data = {
                "old_state": old_state.value,
                "new_state": new_state.value,
                "turn_count": current_turn,
                "warning_message": warning,
                "state": new_state.value
            }
            events.append(self._create_event("turn_warning", turn_warning_data))
        
        # Reset last warning state if we drop below warning threshold
        if new_state == TurnState.LOW:
            self.last_turn_warning_state = TurnState.LOW
        
        return events
    
    def set_execution_state(self, new_state: ExecutionState) -> List[Dict[str, Any]]:
        """Transition to a new execution state.
        
        Returns list of events (e.g., state change notifications).
        """
        old_state = self.execution_state
        self.execution_state = new_state
        
        # Log state transition
        if self.logger:
            self.logger.log_execution_state_change(old_state.value, new_state.value)
        
        # Generate typed event for GUI
        execution_state_data = {
            "old_state": old_state.value,
            "new_state": new_state.value
        }
        return [self._create_event("execution_state_change", execution_state_data)]
    
    def set_session_state(self, new_state: SessionState) -> List[Dict[str, Any]]:
        """Transition to a new session state.
        
        Returns list of events (e.g., session notifications).
        """
        old_state = self.session_state
        self.session_state = new_state
        
        # Log state transition
        if self.logger:
            self.logger.log_session_state_change(old_state.value, new_state.value)
        
        # Generate typed event for GUI
        session_state_data = {
            "old_state": old_state.value,
            "new_state": new_state.value
        }
        return [self._create_event("session_state_change", session_state_data)]
    
    def reset(self) -> List[Dict[str, Any]]:
        """Reset all states to initial values.
        
        Returns list of events for the reset.
        """
        events = []
        
        # Reset token state
        self.token_state = TokenState.LOW
        self.current_conversation_tokens = 0
        self.last_token_warning_state = TokenState.LOW
        self.last_token_warning = None
        self.last_token_warning_count = 0
        # Reset token countdown
        self.token_critical_countdown = 0
        
        # Reset turn state
        self.turn_state = TurnState.LOW
        self.current_turn = 0
        self.last_turn_warning_state = TurnState.LOW
        self.last_turn_warning = None
        self.last_turn_warning_count = 0
        # Reset turn countdown
        self.turn_critical_countdown = 0
        
        # Set execution state to IDLE (or keep current?)
        events.extend(self.set_execution_state(ExecutionState.IDLE))
        
        # Set session state to NEW
        events.extend(self.set_session_state(SessionState.NEW))
        
        return events

    def start_critical_countdown(self, resource: str) -> List[Dict[str, Any]]:
        """Start the critical countdown for a resource (token or turn).

        Returns a list of events containing the countdown start notification.
        """
        countdown = self.CRITICAL_COUNTDOWN_TURNS
        if resource == "token":
            self.token_critical_countdown = countdown
        elif resource == "turn":
            self.turn_critical_countdown = countdown
        else:
            raise ValueError(f"Unknown resource: {resource}")

        warning = (
            f"{resource.upper()} usage is CRITICAL. "
            f"You have {countdown} turns to work normally before tool restrictions apply. "
            f"Use SummarizeTool to reduce context or Final/FinalReport to complete work. After summarizing, you may continue working. "
            f"After countdown: only summary/final tools allowed."
        )

        # Update the last warning to show countdown
        if resource == "token":
            self.last_token_warning = warning
        else:
            self.last_turn_warning = warning

        # Create typed event
        countdown_data = {
            "resource": resource,
            "countdown": countdown,
            "message": warning,
            "state": "critical_countdown"
        }
        event_type = "token_critical_countdown_start" if resource == "token" else "turn_critical_countdown_start"
        return [self._create_event(event_type, countdown_data)]

    def decrement_critical_countdown(self) -> List[Dict[str, Any]]:
        """Decrement critical countdowns and return any expiration events.

        Call this at the start of each turn to manage countdown timers.
        """
        events = []

        # Decrement token countdown if active
        if self.token_critical_countdown > 0:
            self.token_critical_countdown -= 1
            if self.token_critical_countdown == 0:
                # Create typed event
                token_critical_data = {
                    "message": "Token countdown expired. Tool restrictions now active: only SummarizeTool, Final, FinalReport allowed.",
                    "resource": "token"
                }
                events.append(self._create_event("token_critical_countdown_expired", token_critical_data))

        # Decrement turn countdown if active
        if self.turn_critical_countdown > 0:
            self.turn_critical_countdown -= 1
            if self.turn_critical_countdown == 0:
                # Create typed event
                turn_critical_data = {
                    "message": "Turn countdown expired. Tool restrictions now active: only SummarizeTool, Final, FinalReport allowed.",
                    "resource": "turn"
                }
                events.append(self._create_event("turn_critical_countdown_expired", turn_critical_data))

        return events
    
    def get_allowed_tools(self) -> List[str]:
        """Get list of allowed tool names based on current states.

        This implements tool restriction logic. Restrictions only apply when
        countdown has expired (countdown == 0). During countdown, all tools allowed.
        """
        # Default: all tools allowed
        allowed = []

        # Check if restrictions are active (countdown expired)
        token_restricted = (self.token_state == TokenState.CRITICAL and
                            self.token_critical_countdown == 0)
        turn_restricted = (self.turn_state == TurnState.CRITICAL and
                           self.turn_critical_countdown == 0)

        # If either resource is restricted, allow only summary/final tools
        if token_restricted or turn_restricted:
            allowed = ["SummarizeTool", "Final", "FinalReport"]

        return allowed

    def is_tool_allowed(self, tool_name: str) -> bool:
        """Check if a specific tool is allowed in current state."""
        allowed = self.get_allowed_tools()
        if not allowed:  # Empty list means all tools allowed
            return True
        return tool_name in allowed