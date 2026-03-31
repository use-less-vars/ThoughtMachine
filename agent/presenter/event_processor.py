"""
EventProcessor: Handles events from AgentController.

Processes different event types and updates state accordingly.
Designed to be used with GUI integration for signal emission.
"""

import os
from typing import Dict, Any, Optional

from agent.core.state import ExecutionState
from agent import events as ev


class EventProcessor:
    """Processes events from controller and updates state."""
    
    def __init__(self, state_bridge, session_lifecycle, gui_integration=None):
        """
        Initialize event processor.
        
        Args:
            state_bridge: StateBridge instance for state management
            session_lifecycle: SessionLifecycle instance for session operations
            gui_integration: GUI integration for signal emission (optional)
        """
        self.state_bridge = state_bridge
        self.session_lifecycle = session_lifecycle
        self.gui_integration = gui_integration
        
        if os.environ.get('THOUGHTMACHINE_DEBUG'):
            print(f"[EventProcessor] Initialized")
    
    def process_event(self, event: Dict[str, Any]) -> None:
        """
        Process a single event from controller.
        
        Args:
            event: Event dictionary from AgentController
        """
        # Convert to typed event for consistent handling
        typed_event = ev.convert_from_legacy_format(event)
        event_type = typed_event.type.value
        if os.environ.get('THOUGHTMACHINE_DEBUG'):
            print(f"[EventProcessor] Processing event: {event_type}")
        
        # Skip filtering for state/terminal events as they need to be shown regardless
        state_event_types = ["error", "paused", "stopped", "thread_finished", "final", "max_turns", "user_interaction_requested", "rate_limit_warning", "token_warning", "turn_warning"]
        if event_type not in state_event_types:
            event_session_id = event.get("session_id")
            # Ensure both session IDs are strings for comparison to avoid type mismatch issues
            if event_session_id is not None:
                event_session_id = str(event_session_id)
                current_id = self.state_bridge.current_session_id
                if current_id and event_session_id != str(current_id):
                    if os.environ.get('THOUGHTMACHINE_DEBUG'):
                        print(f"[EventProcessor] Ignoring event from old session {event_session_id}")
                    return        
        # Emit event to GUI if integration available
        if self.gui_integration and event_type != "token_update":
            self.gui_integration.emit_event_received(event)
        
        # Process event based on type
        if event_type == "turn":
            self._process_turn_event(event)
        elif event_type == "token_update":
            self._process_token_update_event(event)
        elif event_type == "user_interaction_requested":
            self._process_user_interaction_event(event)
        elif event_type == "paused":
            self._process_paused_event(event)
        elif event_type in ["final", "stopped", "max_turns", "thread_finished"]:
            self._process_terminal_event(event, event_type)
        elif event_type == "error":
            self._process_error_event(event)
        elif event_type == "execution_state_change":
            self._process_execution_state_change_event(event)
        elif event_type == "session_state_change":
            self._process_session_state_change_event(event)
        elif event_type == "token_warning":
            self._process_token_warning_event(event)
        elif event_type == "turn_warning":
            self._process_turn_warning_event(event)
        elif event_type in ["token_critical_countdown_start", "turn_critical_countdown_start",
                           "token_critical_countdown_expired", "turn_critical_countdown_expired"]:
            self._process_critical_countdown_event(event, event_type)
        
        # Emit status update
        if self.gui_integration:
            self.gui_integration.emit_status_message(f"Event: {event_type}")
    
    def _process_turn_event(self, event: Dict[str, Any]) -> None:
        """Process a turn event."""
        # Update token counts
        input_tokens, output_tokens = self._extract_token_counts(event)
        if input_tokens is not None and output_tokens is not None:
            self.state_bridge.update_token_totals(input_tokens, output_tokens)
            if self.gui_integration:
                self.gui_integration.emit_tokens_updated(input_tokens, output_tokens)
        
        # Update context length
        context_length = self._extract_context_length(event)
        if context_length is not None:
            self.state_bridge.update_context_length(context_length)
            if self.gui_integration:
                self.gui_integration.emit_context_updated(context_length)
        
        # Update conversation history
        if "history" in event:
            self._update_user_history(event["history"])
    
    def _process_token_update_event(self, event: Dict[str, Any]) -> None:
        """Process a token update event."""
        input_tokens, output_tokens = self._extract_token_counts(event)
        if input_tokens is not None and output_tokens is not None:
            self.state_bridge.update_token_totals(input_tokens, output_tokens)
            if self.gui_integration:
                self.gui_integration.emit_tokens_updated(input_tokens, output_tokens)
        
        context_length = self._extract_context_length(event)
        if context_length is not None:
            self.state_bridge.update_context_length(context_length)
            if self.gui_integration:
                self.gui_integration.emit_context_updated(context_length)
    
    def _process_user_interaction_event(self, event: Dict[str, Any]) -> None:
        """Process user interaction request event."""
        self.session_lifecycle.state = ExecutionState.WAITING_FOR_USER
        if self.gui_integration:
            self.gui_integration.emit_status_message("Waiting for user input")
        
        # Update conversation history
        if "history" in event:
            self._update_user_history(event["history"])
        
        # Auto-save if needed
        if self.session_lifecycle.has_unsaved_changes():
            # This will be implemented when session lifecycle has auto-save
            pass
    
    def _process_paused_event(self, event: Dict[str, Any]) -> None:
        """Process paused event."""
        self.session_lifecycle.state = ExecutionState.PAUSED
        if self.gui_integration:
            self.gui_integration.emit_status_message("Paused")
        
        # Auto-save if needed
        if self.session_lifecycle.has_unsaved_changes():
            # This will be implemented when session lifecycle has auto-save
            pass
    
    def _process_terminal_event(self, event: Dict[str, Any], event_type: str) -> None:
        """Process terminal event (final, stopped, max_turns, thread_finished)."""
        if event_type == "final":
            self.session_lifecycle.state = ExecutionState.PAUSED
            if self.gui_integration:
                self.gui_integration.emit_status_message("Completed successfully")
            
            # Capture final content
            content = event.get('content')
            reasoning = event.get('reasoning')
            if content and self.state_bridge.current_session:
                self.state_bridge.current_session.final_content = content
                self.state_bridge.current_session.final_reasoning = reasoning
        elif event_type == "max_turns":
            self.session_lifecycle.state = ExecutionState.PAUSED
            if self.gui_integration:
                self.gui_integration.emit_status_message("Max turns reached")
        else:  # "stopped" or "thread_finished"
            # Handle restart logic
            if self.session_lifecycle._restarting:
                # This will be implemented in session lifecycle
                pass
            else:
                self.session_lifecycle.state = ExecutionState.PAUSED
                if self.gui_integration:
                    message = "Paused" if event_type == "stopped" else "Thread finished"
                    self.gui_integration.emit_status_message(message)
        
        # Update conversation history
        if "history" in event:
            self._update_user_history(event["history"])
    
    def _process_error_event(self, event: Dict[str, Any]) -> None:
        """Process error event."""
        self.session_lifecycle.state = ExecutionState.PAUSED
        error_msg = event.get("message", "Unknown error")
        traceback_text = event.get("traceback", "")
        
        if self.gui_integration:
            self.gui_integration.emit_error_occurred(error_msg, traceback_text)
            self.gui_integration.emit_status_message(f"Error: {error_msg}")
        
        # Update conversation history
        if "history" in event:
            self._update_user_history(event["history"])
    
    def _process_execution_state_change_event(self, event: Dict[str, Any]) -> None:
        """Process execution state change event."""
        # Update session lifecycle state
        new_state_str = event.get("new_state")
        if new_state_str:
            # Convert string to ExecutionState enum
            try:
                new_state = ExecutionState(new_state_str)
                self.session_lifecycle.state = new_state
                if self.gui_integration:
                    self.gui_integration.state = new_state
                    self.gui_integration.emit_status_message(f"Execution state changed to: {new_state.value}")
            except ValueError:
                # Invalid state string, log but continue
                if self.gui_integration:
                    self.gui_integration.emit_status_message(f"Invalid execution state: {new_state_str}")
        
        # Update conversation history
        if "history" in event:
            self._update_user_history(event["history"])
    
    def _process_session_state_change_event(self, event: Dict[str, Any]) -> None:
        """Process session state change event."""
        # Update session lifecycle state if needed
        new_state = event.get("new_state")
        if new_state and self.gui_integration:
            self.gui_integration.emit_status_message(f"Session state changed to: {new_state}")
        
        # Update conversation history
        if "history" in event:
            self._update_user_history(event["history"])
    
    def _process_token_warning_event(self, event: Dict[str, Any]) -> None:
        """Process token warning event."""
        warning_message = event.get("warning_message", "Token usage warning")
        token_count = event.get("token_count", 0)
        
        if self.gui_integration:
            self.gui_integration.emit_status_message(f"Token warning: {warning_message}")
            # Also emit to warning system if available
            if hasattr(self.gui_integration, 'emit_warning'):
                self.gui_integration.emit_warning(f"Token usage warning: {token_count} tokens")
        
        # Update conversation history
        if "history" in event:
            self._update_user_history(event["history"])
    
    def _process_turn_warning_event(self, event: Dict[str, Any]) -> None:
        """Process turn warning event."""
        warning_message = event.get("warning_message", "Turn limit warning")
        turn_count = event.get("turn_count", 0)
        
        if self.gui_integration:
            self.gui_integration.emit_status_message(f"Turn warning: {warning_message}")
            # Also emit to warning system if available
            if hasattr(self.gui_integration, 'emit_warning'):
                self.gui_integration.emit_warning(f"Turn limit warning: {turn_count} turns")
        
        # Update conversation history
        if "history" in event:
            self._update_user_history(event["history"])
    
    def _process_critical_countdown_event(self, event: Dict[str, Any], event_type: str) -> None:
        """Process critical countdown event."""
        resource = "token" if "token" in event_type else "turn"
        action = "started" if "start" in event_type else "expired"
        message = event.get("message", f"{resource.upper()} critical countdown {action}")
        
        if self.gui_integration:
            self.gui_integration.emit_status_message(f"Critical countdown: {message}")
            # Also emit to warning system if available
            if hasattr(self.gui_integration, 'emit_warning'):
                self.gui_integration.emit_warning(f"{resource.upper()} critical: {action}")
        
        # Update conversation history
        if "history" in event:
            self._update_user_history(event["history"])
    
    def _extract_token_counts(self, event: Dict[str, Any]) -> tuple[Optional[int], Optional[int]]:
        """Extract token counts from event."""
        input_tokens = None
        output_tokens = None
        
        # First check usage dict
        usage = event.get("usage", {})
        if "total_input_tokens" in usage and "total_output_tokens" in usage:
            input_tokens = usage["total_input_tokens"]
            output_tokens = usage["total_output_tokens"]
        elif "total_input" in usage and "total_output" in usage:
            input_tokens = usage["total_input"]
            output_tokens = usage["total_output"]
        # For backward compatibility, also check top-level
        elif "total_input_tokens" in event and "total_output_tokens" in event:
            input_tokens = event["total_input_tokens"]
            output_tokens = event["total_output_tokens"]
        elif "total_input" in event and "total_output" in event:
            input_tokens = event["total_input"]
            output_tokens = event["total_output"]
        
        return input_tokens, output_tokens
    
    def _extract_context_length(self, event: Dict[str, Any]) -> Optional[int]:
        """Extract context length from event."""
        context_length = None
        if "context_length" in event:
            context_length = event["context_length"]
        elif "usage" in event and "context_length" in event["usage"]:
            context_length = event["usage"]["context_length"]
        elif "usage" in event and "current_conversation_tokens" in event["usage"]:
            context_length = event["usage"]["current_conversation_tokens"]
        return context_length
    
    def _update_user_history(self, event_history: list) -> None:
        """
        Update user_history with current conversation from event.

        Replaces user_history contents in-place with the event's history (which may be pruned).
        This ensures we save exactly what the agent sees and preserves references.
        """
        if event_history:
            # Mutate in-place to preserve existing list references
            session = self.state_bridge.current_session
            if session:
                # If session has user_history, update it directly
                session.user_history[:] = event_history
                # Mark session as dirty
                self.session_lifecycle.mark_dirty()
                # Emit conversation changed signal
                if self.gui_integration:
                    self.gui_integration.emit_conversation_changed()
            else:
                # No session bound, this shouldn't happen but handle gracefully
                if os.environ.get('THOUGHTMACHINE_DEBUG'):
                    print("[EventProcessor] WARNING: No session bound when updating user history")

