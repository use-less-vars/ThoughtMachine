# agent_state.py - State machine for ThoughtMachine agent
from __future__ import annotations
import enum
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field


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
    """Execution state of the agent."""
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    WAITING_FOR_USER = "waiting_for_user"
    FINALIZED = "finalized"
    MAX_TURNS_REACHED = "max_turns_reached"


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
    
    # State fields
    token_state: TokenState = TokenState.LOW
    turn_state: TurnState = TurnState.LOW
    execution_state: ExecutionState = ExecutionState.IDLE
    session_state: SessionState = SessionState.NEW
    
    # Resource tracking
    current_conversation_tokens: int = 0
    current_turn: int = 0
    
    # Warning tracking
    last_token_warning_state: TokenState = TokenState.LOW
    last_turn_warning_state: TurnState = TurnState.LOW
    last_token_warning: Optional[str] = None
    last_token_warning_count: int = 0
    last_turn_warning: Optional[str] = None
    last_turn_warning_count: int = 0
    

    
    # Internal event storage
    _pending_events: List[Dict[str, Any]] = field(default_factory=list)
    
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
                warning = f"[SYSTEM] Token usage warning: Conversation is nearing context window limits. Please consider pruning soon when you are at a good point."
            else:  # CRITICAL
                formatted = self._format_tokens(total_tokens)
                warning = f"Conversation is at a critical context window limit. You MUST prune now to avoid system crash. Leave all your work and summarize now (and I mean NOW!). Otherwise all your work will be lost."
            
            # Store warning
            self.last_token_warning = warning
            self.last_token_warning_count = total_tokens
            self.last_token_warning_state = new_state
            
            # Log if logger available
            if self.logger:
                self.logger.log_token_warning(old_state.value, new_state.value, total_tokens, warning)
            
            # Create event
            events.append({
                "type": "token_warning",
                "message": warning,
                "token_count": total_tokens,
                "state": new_state.value
            })
        
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
                warning = f"[SYSTEM] Turn limit warning: Agent is nearing maximum turn limit ({current_turn}/{max_turns} turns). Please consider wrapping up soon."
            else:  # CRITICAL
                warning = f"Turn limit critical: Agent is at critical turn limit ({current_turn}/{max_turns} turns). You MUST finish NOW or risk being cut off. Leave all your work and summarize now (and I mean NOW!). Otherwise all your work will be lost!"
            
            # Store warning
            self.last_turn_warning = warning
            self.last_turn_warning_count = current_turn
            self.last_turn_warning_state = new_state
            
            # Log if logger available
            if self.logger:
                self.logger.log_turn_warning(old_state.value, new_state.value, current_turn, warning)
            
            # Create event
            events.append({
                "type": "turn_warning",
                "message": warning,
                "turn_count": current_turn,
                "state": new_state.value
            })
        
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
        
        # Generate event for GUI
        return [{
            "type": "execution_state_change",
            "old_state": old_state.value,
            "new_state": new_state.value
        }]
    
    def set_session_state(self, new_state: SessionState) -> List[Dict[str, Any]]:
        """Transition to a new session state.
        
        Returns list of events (e.g., session notifications).
        """
        old_state = self.session_state
        self.session_state = new_state
        
        # Log state transition
        if self.logger:
            self.logger.log_session_state_change(old_state.value, new_state.value)
        
        # Generate event for GUI
        return [{
            "type": "session_state_change",
            "old_state": old_state.value,
            "new_state": new_state.value
        }]
    
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
        
        # Reset turn state
        self.turn_state = TurnState.LOW
        self.current_turn = 0
        self.last_turn_warning_state = TurnState.LOW
        self.last_turn_warning = None
        self.last_turn_warning_count = 0
        
        # Set execution state to IDLE (or keep current?)
        events.extend(self.set_execution_state(ExecutionState.IDLE))
        
        # Set session state to NEW
        events.extend(self.set_session_state(SessionState.NEW))
        
        return events
    
    def get_allowed_tools(self) -> List[str]:
        """Get list of allowed tool names based on current states.
        
        This implements tool restriction logic (optional).
        """
        # Default: all tools allowed
        allowed = []
        
        # If token state is CRITICAL, only allow SummarizeTool
        if self.token_state == TokenState.CRITICAL:
            allowed = ["SummarizeTool"]
        # If turn state is CRITICAL, only allow Final or FinalReport tools
        elif self.turn_state == TurnState.CRITICAL:
            allowed = ["Final", "FinalReport"]
        
        return allowed
    
    def is_tool_allowed(self, tool_name: str) -> bool:
        """Check if a specific tool is allowed in current state."""
        allowed = self.get_allowed_tools()
        if not allowed:  # Empty list means all tools allowed
            return True
        return tool_name in allowed