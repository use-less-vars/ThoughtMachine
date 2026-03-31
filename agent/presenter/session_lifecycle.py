"""
SessionLifecycle: Session start/stop/pause/save/load operations.

Handles:
- Session creation, starting, pausing, restarting
- Session loading, saving, exporting
- Session listing, deletion, renaming
- Auto-save and dirty state tracking
"""

import json
import uuid
import os
from datetime import datetime
from typing import Optional, List, Dict, Any, Callable

from agent.controller import AgentController
from agent.core.state import ExecutionState
from session.models import Session, SessionConfig
from session.store import FileSystemSessionStore
from session.context_builder import LastNBuilder

from .state_bridge import StateBridge


class SessionLifecycle:
    """Manages session lifecycle operations."""
    
    def __init__(self, state_bridge: StateBridge, controller: AgentController):
        self.state_bridge = state_bridge
        self.controller = controller
        
        # Session store and context builder
        self.session_store = FileSystemSessionStore()
        self.context_builder = LastNBuilder(
            keep_last_messages=100000, 
            keep_system_prompt=True
        )
        
        # State tracking
        self._state = ExecutionState.IDLE
        self._restarting = False
        self._dirty = False  # Tracks unsaved changes
        self._initial_conversation: Optional[List[Dict[str, Any]]] = None
        self._session_callback: Optional[Callable] = None
        
        # Cached configuration for restart
        self._cached_config = None
        self._cached_preset_name = None
        
        if os.environ.get('THOUGHTMACHINE_DEBUG'):
            print(f"[SessionLifecycle] Initialized")
    
    # State management
    @property
    def state(self) -> ExecutionState:
        """Current execution state."""
        return self._state
    
    @state.setter
    def state(self, new_state: ExecutionState):
        """Update execution state."""
        if self._state != new_state:
            old_state = self._state
            self._state = new_state
            if os.environ.get('THOUGHTMACHINE_DEBUG'):
                print(f"[SessionLifecycle] state changed: {old_state} -> {new_state}")
            # Notify callback if set
            if self._session_callback:
                try:
                    self._session_callback(old_state, new_state)
                except Exception as e:
                    if os.environ.get('THOUGHTMACHINE_DEBUG'):
                        print(f"[SessionLifecycle] Error in state callback: {e}")
    
    # Session operations
    def start_session(self, query: str, config: Optional[dict] = None, preset_name: str = None):
        """
        Start a new agent session.

        Args:
            query: User query string
            config: Optional configuration overrides
            preset_name: Optional preset name to use instead of config
        """
        if self.state != ExecutionState.IDLE:
            if os.environ.get('THOUGHTMACHINE_DEBUG'):
                print(f"[SessionLifecycle] Cannot start session in state {self.state}")
            return

        # Clear session name only for a brand new session (no existing session ID)
        if self.state_bridge.current_session_id is None:
            self.state_bridge.session_name = None

        try:
            # Resolve configuration and cache for restart
            if preset_name is not None:
                from agent import Agent
                overrides = config if config is not None else {}
                # Ensure agent carries over current session's token totals as initial values
                overrides['initial_input_tokens'] = self.state_bridge.total_input
                overrides['initial_output_tokens'] = self.state_bridge.total_output
                temp_agent = Agent.from_preset(preset_name, session=None, **overrides)
                agent_config = temp_agent.config
                self._cached_config = agent_config
                self._cached_preset_name = preset_name
            else:
                agent_config = self.state_bridge.create_agent_config(
                    config,
                    total_input=self.state_bridge.total_input,
                    total_output=self.state_bridge.total_output
                )
                self._cached_config = agent_config
                self._cached_preset_name = None

            # Ensure we have a bound session (should be bound via new_session or load_session)
            if self.state_bridge.current_session is None:
                session_config = self.state_bridge.build_session_config(agent_config)
                new_session = Session(
                    session_id=str(uuid.uuid4()),
                    config=session_config,
                    user_history=[],
                    metadata={}
                )
                self.state_bridge.bind_session(new_session)

            # Clear any initial conversation flag
            self._initial_conversation = None

            # Start the agent with the bound session
            if preset_name is not None:
                self.controller.start(
                    query,
                    session=self.state_bridge.current_session,
                    preset_name=preset_name,
                    **overrides
                )
            else:
                self.controller.start(
                    query,
                    config=agent_config,
                    session=self.state_bridge.current_session
                )
            self.state = ExecutionState.RUNNING
            # Status message will be emitted through event processing

        except Exception as e:
            self.state = ExecutionState.PAUSED
            # Error will be emitted through event processing
            if os.environ.get('THOUGHTMACHINE_DEBUG'):
                print(f"[SessionLifecycle] Error starting session: {e}")

    def new_session(self, name: str = None, auto_save_current: bool = True):
        """Start a brand new session.
        
        Args:
            name: Optional name for the new session. If None, session will be unnamed.
            auto_save_current: If True, auto-save current session before clearing.
        """
        # Auto-save current session if requested and has unsaved changes
        if auto_save_current and self.has_unsaved_changes():
            if os.environ.get('THOUGHTMACHINE_DEBUG'):
                print("[SessionLifecycle] Auto-saving current session before starting new session")
            # Auto-save will be implemented when save_session is added
            pass
        
        # If agent is running, stop it first (best effort)
        if self.controller.is_running:
            self.controller.stop()
        # Create a fresh Session with a generated UUID and default config
        agent_config = self.state_bridge.create_agent_config()
        session_config = self.state_bridge.build_session_config(agent_config)
        session = Session(
            session_id=str(uuid.uuid4()),
            config=session_config,
            user_history=[],
            metadata={'name': name} if name else {}
        )
        self.state_bridge.bind_session(session)
        self.state_bridge.update_external_file_path(None)
        # Clear final content
        if self.state_bridge.current_session:
            self.state_bridge.current_session.final_content = None
            self.state_bridge.current_session.final_reasoning = None
        # Ensure state is IDLE
        self.state = ExecutionState.IDLE
        # Status message will be emitted through event processing
        if os.environ.get('THOUGHTMACHINE_DEBUG'):
            print(f"[SessionLifecycle] Started new session{' named ' + name if name else ''}")

    def continue_session(self, query: str):
        """
        Continue an existing session with a new query.

        Args:
            query: User query string
        """
        # States that accept new queries: IDLE, PAUSED, WAITING_FOR_USER, FINALIZED, MAX_TURNS_REACHED
        # But for continue_session (existing session), IDLE might mean session was never started
        # Actually, let controller handle whether it can accept queries
        # The controller checks is_running and other internal state
        if os.environ.get('THOUGHTMACHINE_DEBUG'):
            print(f"[SessionLifecycle] continue_session called in state {self.state}")
        # We'll let controller decide; just pass query through
        try:
            self.controller.continue_session(query)
            self.state = ExecutionState.RUNNING
            if os.environ.get('THOUGHTMACHINE_DEBUG'):
                print(f"[SessionLifecycle] continue_session successful, state set to RUNNING")
            # Status message will be emitted through event processing
        except Exception as e:
            # Error will be emitted through event processing
            if os.environ.get('THOUGHTMACHINE_DEBUG'):
                print(f"[SessionLifecycle] Error in continue_session: {e}")
            if os.environ.get('PAUSE_DEBUG'):
                print(f"[PAUSE_FLOW] SessionLifecycle.continue_session: controller rejected query: {e}")
            # Don't set state to RUNNING since controller failed to accept query
            # Re-raise the exception so presenter can handle it
            raise

    def pause_session(self):
        """Request pause of current session."""
        if self.state == ExecutionState.RUNNING:
            self.controller.request_pause()
            self.state = ExecutionState.PAUSING
        else:
            if os.environ.get('THOUGHTMACHINE_DEBUG'):
                print(f"[SessionLifecycle] Cannot pause in state {self.state}")

    def _finalize_restart(self):
        """Common restart cleanup: reset controller and state."""
        # Reset rate limiting on current agent if it exists
        if hasattr(self.controller, 'agent') and self.controller.agent is not None:
            agent = self.controller.agent
            if hasattr(agent, 'reset_rate_limiting'):
                try:
                    agent.reset_rate_limiting()
                    if os.environ.get('THOUGHTMACHINE_DEBUG'):
                        print(f"[SessionLifecycle] Reset rate limiting on agent before restart")
                except Exception as e:
                    if os.environ.get('THOUGHTMACHINE_DEBUG'):
                        print(f"[SessionLifecycle] Failed to reset rate limiting: {e}")
        self.controller.reset()
        self.state = ExecutionState.IDLE
        self._restarting = False
        # Rebuild initial conversation from user_history for continuation
        if self.state_bridge.current_session and self.state_bridge.current_session.user_history:
            self._initial_conversation = self.context_builder.build(
                self.state_bridge.current_session.user_history
            )
        else:
            self._initial_conversation = None
        # Status message will be emitted through event processing

    def restart_session(self, query: str = None):
        """
        Restart a fresh session with current configuration.
        Does NOT automatically start a new session. After restart, state is IDLE.
        """
        # Refresh config from current GUI
        try:
            self._cached_config = self.state_bridge.create_agent_config()
        except Exception as e:
            # Error will be emitted through event processing
            return

        # Auto-save current session if exists (silent)
        if self.state_bridge.current_session_id:
            try:
                # Save will be implemented when save_session is added
                pass
            except Exception as e:
                if os.environ.get('THOUGHTMACHINE_DEBUG'):
                    print(f"[SessionLifecycle] Auto-save before restart failed: {e}")
                # Continue anyway

        # If already IDLE, finalize immediately
        if self.state == ExecutionState.IDLE:
            self._finalize_restart()
            return

        # Avoid re-entrancy
        if self._restarting:
            return
        self._restarting = True

        # Request stop
        self.controller.stop()

        # If controller already stopped (thread dead), finalize now
        if not self.controller.is_running:
            self._finalize_restart()
            return

        # Otherwise, wait for terminal event; state = PAUSING

    def save_session(self) -> bool:
        """Save current session to the session store.

        Returns:
            True if saved successfully, False otherwise
        """
        try:
            # Build session from current state
            session = self._build_session_from_current_state()
            if session is None:
                if os.environ.get('THOUGHTMACHINE_DEBUG'):
                    print(f"[SessionLifecycle] No session to save")
                return False

            # Ensure we have a session_id (new session if None)
            if not self.state_bridge.current_session_id:
                self.state_bridge.current_session_id = str(session.session_id)
            else:
                # Preserve the existing session_id
                session.session_id = str(self.state_bridge.current_session_id)

            # Set current_session reference
            self.state_bridge.current_session = session

            # Ensure session has a name for listing
            if not session.metadata.get('name'):
                created = session.created_at
                if isinstance(created, datetime):
                    session.metadata['name'] = f"Session {created:%Y-%m-%d %H:%M}"
                else:
                    session.metadata['name'] = "Untitled Session"

            # Save via session store (writes to store directory)
            self.session_store.save_session(session)

            # Update session name from metadata
            self.state_bridge.session_name = session.metadata.get('name')

            # Update current session marker to point to this session
            self.session_store.set_current_session_id(self.state_bridge.current_session_id)
            # Mark as clean after successful save
            self.mark_clean()

            if os.environ.get('THOUGHTMACHINE_DEBUG'):
                print(f"[SessionLifecycle] Session saved to store: {self.session_store.get_session_path(session.session_id)}")
            # Also export to external file if set
            if self.state_bridge._external_file_path:
                try:
                    # Export will be implemented when export_session is added
                    pass
                except Exception as e:
                    if os.environ.get('THOUGHTMACHINE_DEBUG'):
                        print(f"[SessionLifecycle] Failed to export to external file: {e}")
            return True
        except Exception as e:
            if os.environ.get('THOUGHTMACHINE_DEBUG'):
                print(f"[SessionLifecycle] Error saving session: {e}")
            return False

    def has_unsaved_changes(self) -> bool:
        """Check if current session has unsaved changes."""
        return self._dirty
    
    def mark_clean(self) -> None:
        """Mark session as clean (no unsaved changes)."""
        self._dirty = False
    
    def mark_dirty(self) -> None:
        """Mark session as dirty (has unsaved changes)."""
        self._dirty = True

    def load_session(self, filepath: str, auto_save: bool = True) -> bool:
        """Load a session from a JSON file.

        Args:
            filepath: Path to the session file
            auto_save: If True, auto-save current session before loading

        Returns:
            True if loaded successfully, False otherwise
        """
        # Auto-save current session before loading new one
        if auto_save and self.has_unsaved_changes():
            if os.environ.get('THOUGHTMACHINE_DEBUG'):
                print("[SessionLifecycle] Auto-saving current session before loading new session")
            # Auto-save will be implemented when auto_save_current_session is added
            pass

        try:
            # Convert to absolute path for consistency
            filepath = os.path.abspath(filepath)
            with open(filepath, 'r') as f:
                session_dict = json.load(f)

            # Check session version
            version = session_dict.get('version', 0)
            if version != 1:
                if os.environ.get('THOUGHTMACHINE_DEBUG'):
                    print(f"[SessionLifecycle] Warning: Session version {version} is not current (1). Attempting to load anyway.")

            # Reconstruct Session object
            session = Session.from_persistable_dict(session_dict)

            self.state_bridge.bind_session(session)
            self.state_bridge.update_external_file_path(filepath)

            # If session name was not set by binding (i.e., metadata lacks name), use fallback
            if not self.state_bridge.session_name:
                self.state_bridge.session_name = os.path.basename(filepath)

            if os.environ.get('THOUGHTMACHINE_DEBUG'):
                print(f"[SessionLifecycle] Session loaded from {filepath}: {len(session.user_history)} messages")
            return True
        except Exception as e:
            if os.environ.get('THOUGHTMACHINE_DEBUG'):
                print(f"[SessionLifecycle] Error loading session: {e}")
            import traceback
            traceback.print_exc()
            return False

    def load_session_by_id(self, session_id: str) -> bool:
        """Load a session by ID from the session store."""
        if os.environ.get('THOUGHTMACHINE_DEBUG'):
            print(f"[SessionLifecycle] Loading session {session_id} from store")
        session = self.session_store.load_session(session_id)
        if session is None:
            if os.environ.get('THOUGHTMACHINE_DEBUG'):
                print(f"[SessionLifecycle] Session {session_id} not found")
            return False

        # Set as current session
        self.state_bridge.current_session = session
        self.state_bridge.current_session_id = str(session.session_id)
        self.state_bridge.bind_session(session)
        # Restore external file path from metadata if present
        external_file_path = session.metadata.get('external_file_path')
        if external_file_path:
            # Convert to absolute path for consistency
            external_file_path = os.path.abspath(external_file_path)
            self.state_bridge.update_external_file_path(external_file_path)
        # If session name was not set by binding, format a fallback
        if not self.state_bridge.session_name:
            if isinstance(session.created_at, datetime):
                self.state_bridge.session_name = f"Session {session.created_at:%Y-%m-%d %H:%M}"
            else:
                self.state_bridge.session_name = "Untitled Session"
        if os.environ.get('THOUGHTMACHINE_DEBUG'):
            print(f"[SessionLifecycle] Session loaded from store: {session_id} ({self.state_bridge.session_name})")
        return True

    def export_session(self, filepath: str, set_as_external: bool = False) -> bool:
        """Export current session to a specified file path (for backup/transfer).

        Args:
            filepath: Path to export the session JSON file
            set_as_external: If True, set this file as the external file path
                for future auto-saves. Default False.

        Returns:
            True if exported successfully, False otherwise
        """
        try:
            # Convert to absolute path for consistency
            filepath = os.path.abspath(filepath)
            session = self._build_session_from_current_state()
            if session is None:
                if os.environ.get('THOUGHTMACHINE_DEBUG'):
                    print(f"[SessionLifecycle] No session to export")
                return False

            # Use the session's ID if available, otherwise generate a temporary one for export
            if not session.session_id:
                session.session_id = str(uuid.uuid4())

            # Serialize to JSON (version already included by to_persistable_dict)
            session_dict = session.to_persistable_dict()
            # Ensure datetime objects are serialized
            if isinstance(session_dict.get('created_at'), datetime):
                session_dict['created_at'] = session_dict['created_at'].isoformat()
            if isinstance(session_dict.get('updated_at'), datetime):
                session_dict['updated_at'] = session_dict['updated_at'].isoformat()

            # Ensure directory exists
            os.makedirs(os.path.dirname(filepath) if os.path.dirname(filepath) else '.', exist_ok=True)

            with open(filepath, 'w') as f:
                json.dump(session_dict, f, indent=2)

            if os.environ.get('THOUGHTMACHINE_DEBUG'):
                print(f"[SessionLifecycle] Session exported to {filepath}")
            if set_as_external:
                self.state_bridge.update_external_file_path(filepath)
            # Note: we do NOT clear dirty flag because export does not affect session store
            return True
        except Exception as e:
            if os.environ.get('THOUGHTMACHINE_DEBUG'):
                print(f"[SessionLifecycle] Error exporting session: {e}")
            import traceback
            traceback.print_exc()
            return False

    def list_sessions(self) -> List[Dict[str, Any]]:
        """List available sessions from the session store.

        Returns:
            List of session metadata dictionaries
        """
        try:
            sessions = self.session_store.list_sessions()
            if os.environ.get('THOUGHTMACHINE_DEBUG'):
                print(f"[SessionLifecycle] Listed {len(sessions)} sessions")
            return sessions
        except Exception as e:
            if os.environ.get('THOUGHTMACHINE_DEBUG'):
                print(f"[SessionLifecycle] Error listing sessions: {e}")
            return []

    def delete_session(self, session_id: str) -> bool:
        """Delete a session from the store.

        Args:
            session_id: ID of the session to delete
        """
        try:
            success = self.session_store.delete_session(session_id)
            if success:
                if os.environ.get('THOUGHTMACHINE_DEBUG'):
                    print(f"[SessionLifecycle] Deleted session {session_id}")
            else:
                if os.environ.get('THOUGHTMACHINE_DEBUG'):
                    print(f"[SessionLifecycle] Session {session_id} not found for deletion")
            return success
        except Exception as e:
            if os.environ.get('THOUGHTMACHINE_DEBUG'):
                print(f"[SessionLifecycle] Error deleting session: {e}")
            return False

    def rename_session(self, session_id: str, new_name: str) -> bool:
        """Rename a session's metadata name.

        Args:
            session_id: ID of the session to rename
            new_name: New name for the session
        """
        try:
            session = self.session_store.load_session(session_id)
            if session is None:
                if os.environ.get('THOUGHTMACHINE_DEBUG'):
                    print(f"[SessionLifecycle] Session {session_id} not found for rename")
                return False

            # Update name in metadata
            session.metadata['name'] = new_name
            # Save back to store
            self.session_store.save_session(session)
            # If this is the current session, update our session_name
            if self.state_bridge.current_session_id == session_id:
                self.state_bridge.session_name = new_name

            if os.environ.get('THOUGHTMACHINE_DEBUG'):
                print(f"[SessionLifecycle] Renamed session {session_id} to {new_name}")
            return True
        except Exception as e:
            if os.environ.get('THOUGHTMACHINE_DEBUG'):
                print(f"[SessionLifecycle] Error renaming session: {e}")
            return False

    def _build_session_from_current_state(self):
        """Construct a Session object from current presenter state."""
        # Get the full conversation from the current session
        conversation = None
        if self.state_bridge.user_history:
            conversation = self.state_bridge.user_history
        else:
            # Try to get conversation from controller
            conversation = self.controller.get_conversation() if hasattr(self.controller, 'get_conversation') else None
            if conversation is None:
                conversation = self._initial_conversation
                if conversation is None:
                    return None

        if not conversation:
            return None

        # Build session config from current agent config
        try:
            agent_config = self.state_bridge.create_agent_config()
            session_config = self.state_bridge.build_session_config(agent_config)
        except Exception as e:
            if os.environ.get('THOUGHTMACHINE_DEBUG'):
                print(f"[SessionLifecycle] Error building session config: {e}")
            return None

        # Create session with current state
        session = Session(
            session_id=self.state_bridge.current_session_id or str(uuid.uuid4()),
            config=session_config,
            user_history=conversation,
            metadata={'name': self.state_bridge.session_name} if self.state_bridge.session_name else {}
        )
        
        # Copy token totals and context length if available
        if self.state_bridge.total_input > 0:
            session.total_input_tokens = self.state_bridge.total_input
        if self.state_bridge.total_output > 0:
            session.total_output_tokens = self.state_bridge.total_output
        if self.state_bridge.context_length > 0:
            session.context_length = self.state_bridge.context_length
        
        # Copy final content if available
        if self.state_bridge.current_session:
            session.final_content = self.state_bridge.current_session.final_content
            session.final_reasoning = self.state_bridge.current_session.final_reasoning
        
        # Add external file path to metadata if set
        if self.state_bridge._external_file_path:
            session.metadata['external_file_path'] = self.state_bridge._external_file_path
        
        return session

    def auto_save_current_session(self, default_name: str = None) -> bool:
        """Auto-save current session with default name if not already saved.

        Args:
            default_name: Default name to use if session has no name.
                          If None, generates timestamp-based name.

        Returns:
            True if saved or no need to save, False on error.
        """
        if not self.has_unsaved_changes():
            if os.environ.get('THOUGHTMACHINE_DEBUG'):
                print("[SessionLifecycle] No unsaved changes, skipping auto-save")
            return True

        try:
            success = self.save_session()
            if success:
                if os.environ.get('THOUGHTMACHINE_DEBUG'):
                    print("[SessionLifecycle] Auto-saved session successfully")
                # Also export to external file if set
                if self.state_bridge._external_file_path:
                    try:
                        self.export_session(self.state_bridge._external_file_path, set_as_external=False)
                    except Exception as e:
                        if os.environ.get('THOUGHTMACHINE_DEBUG'):
                            print(f"[SessionLifecycle] Failed to export to external file: {e}")
                return True
            else:
                if os.environ.get('THOUGHTMACHINE_DEBUG'):
                    print("[SessionLifecycle] Auto-save failed")
                return False
        except Exception as e:
            if os.environ.get('THOUGHTMACHINE_DEBUG'):
                print(f"[SessionLifecycle] Error in auto-save: {e}")
            return False

