"""
SessionLifecycle: Session start/stop/pause/save/load operations.

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
from agent.logging import log
from agent.controller import AgentController
from agent.core.state import ExecutionState
from session.models import Session
from session.store import FileSystemSessionStore
from session.context_builder import SummaryBuilder
from .state_bridge import StateBridge

class SessionLifecycle:
    """Manages session lifecycle operations."""

    def __init__(self, state_bridge: StateBridge, controller: AgentController):
        self.state_bridge = state_bridge
        self.controller = controller
        self.session_store = FileSystemSessionStore()
        self.context_builder = SummaryBuilder()
        self._state = ExecutionState.READY
        self._restarting = False

        self._session_callback: Optional[Callable] = None
        self._conversation_callback: Optional[Callable] = None
        self._cached_config = None
        self._cached_preset_name = None
        log('DEBUG', 'presenter.lifecycle', f'Initialized')

    def _register_session_callbacks(self, session):
        """Register callbacks on a session for change tracking."""
        log('DEBUG', 'presenter.lifecycle', f'Registering callbacks for session {session.session_id}')
        log('DEBUG', 'presenter.lifecycle', f'Registering callbacks: user_history id={id(session.user_history)}')

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
            log('DEBUG', 'presenter.lifecycle', f'state changed: {old_state} -> {new_state}')
            if self._session_callback:
                try:
                    self._session_callback(old_state, new_state)
                except Exception as e:
                    log('DEBUG', 'presenter.lifecycle', f'Error in state callback: {e}')

    def mark_clean(self) -> None:
        """Mark session as clean (no unsaved changes). Dummy method after removing dirty tracking."""
        log('DEBUG', 'presenter.lifecycle', f'mark_clean called (dummy method - dirty tracking removed)')

    def has_unsaved_changes(self) -> bool:
        """Check if current session has unsaved changes.
        
        Returns:
            Always returns False (dirty tracking removed).
        """
        return False

    def start_session(self, query: str, config: Optional[dict]=None, preset_name: str=None):
        """
        Start a new agent session.

        Args:
            query: User query string
            config: Optional configuration overrides
            preset_name: Optional preset name to use instead of config
        """
        log('DEBUG', 'presenter.lifecycle', f'start_session called, state={self.state}, current_session exists={self.state_bridge.current_session is not None}')
        if self.state != ExecutionState.READY:
            log('DEBUG', 'presenter.lifecycle', f'Cannot start session in state {self.state}')
            return
        if self.state_bridge.current_session_id is None:
            self.state_bridge.session_name = None
        try:
            if preset_name is not None:
                from agent import Agent
                overrides = config if config is not None else {}

                temp_agent = Agent.from_preset(preset_name, session=None, **overrides)
                agent_config = temp_agent.config
                self._cached_config = agent_config
                self._cached_preset_name = preset_name
            else:
                agent_config = self.state_bridge.create_agent_config(config, total_input=self.state_bridge.total_input, total_output=self.state_bridge.total_output)
                self._cached_config = agent_config
                self._cached_preset_name = None
            if self.state_bridge.current_session is None:
                ws_path = self.state_bridge.current_config.workspace_path
                metadata = {}
                if ws_path:
                    metadata['workspace_path'] = ws_path
                metadata['agent_config'] = agent_config.model_dump()
                new_session = Session(session_id=str(uuid.uuid4()), user_history=[], metadata=metadata)
                new_session.ensure_name()
                self.state_bridge.bind_session(new_session)
                self._register_session_callbacks(new_session)
            if preset_name is not None:
                self.controller.start(query, session=self.state_bridge.current_session, preset_name=preset_name, **overrides)
            else:
                self.controller.start(query, config=agent_config, session=self.state_bridge.current_session)
            self.state = ExecutionState.RUNNING
        except Exception as e:
            self.state = ExecutionState.READY
            log('DEBUG', 'presenter.lifecycle', f'Error starting session: {e}')
            raise

    def new_session(self, name: str=None):
        """Start a brand new session.
        
        Args:
            name: Optional name for the new session. If None, session will be unnamed.
        """
        log('DEBUG', 'presenter.lifecycle', f'Auto-saving current session before starting new session')
        self.auto_save_current_session()
        if self.controller.is_running:
            self.controller.stop()
        agent_config = self.state_bridge.create_agent_config()
        ws_path = self.state_bridge.current_config.workspace_path
        metadata = {}
        if name:
            metadata['name'] = name
        if ws_path:
            metadata['workspace_path'] = ws_path
        metadata['agent_config'] = agent_config.model_dump()
        session = Session(session_id=str(uuid.uuid4()), user_history=[], metadata=metadata)
        session.ensure_name()
        self.state_bridge.bind_session(session)
        self._register_session_callbacks(session)
        self.state_bridge.update_external_file_path(None)
        self.state = ExecutionState.READY
        log('DEBUG', 'presenter.lifecycle', f"Started new session{(' named ' + name if name else '')}")

    def continue_session(self, query: str):
        """
        Continue an existing session with a new query.

        Args:
            query: User query string
        """
        log('DEBUG', 'presenter.lifecycle', f'continue_session called in state {self.state}')
        try:
            self.controller.continue_session(query)
            self.state = ExecutionState.RUNNING
            log('DEBUG', 'presenter.lifecycle', f'continue_session successful, state set to RUNNING')
        except Exception as e:
            log('DEBUG', 'presenter.lifecycle', f'Error in continue_session: {e}')
            if os.environ.get('PAUSE_DEBUG'):
                log('WARNING', 'presenter.pause_flow', f'SessionLifecycle.continue_session: controller rejected query: {e}')
            raise

    def pause_session(self):
        """Request pause of current session."""
        if self.state == ExecutionState.RUNNING:
            self.controller.request_pause()
            self.state = ExecutionState.PAUSING
        else:
            log('DEBUG', 'presenter.lifecycle', f'Cannot pause in state {self.state}')

    def _finalize_restart(self):
        """Common restart cleanup: reset controller and state."""
        if hasattr(self.controller, 'agent') and self.controller.agent is not None:
            agent = self.controller.agent
            if hasattr(agent, 'reset_rate_limiting'):
                try:
                    agent.reset_rate_limiting()
                    log('DEBUG', 'presenter.lifecycle', f'Reset rate limiting on agent before restart')
                except Exception as e:
                    log('DEBUG', 'presenter.lifecycle', f'Failed to reset rate limiting: {e}')
        self.controller.reset()
        self.state = ExecutionState.READY
        self._restarting = False


    def restart_session(self, query: str=None):
        """
        Restart a fresh session with current configuration.
        Does NOT automatically start a new session. After restart, state is IDLE.
        """
        try:
            self._cached_config = self.state_bridge.create_agent_config()
        except Exception as e:
            log('DEBUG', 'presenter.lifecycle', f'Error creating agent config for restart: {e}')
            raise
        if self.state_bridge.current_session_id:
            try:
                pass
            except Exception as e:
                log('DEBUG', 'presenter.lifecycle', f'Auto-save before restart failed: {e}')
        if self.state == ExecutionState.READY:
            self._finalize_restart()
            return
        if self._restarting:
            return
        self._restarting = True
        self.controller.stop()
        if not self.controller.is_running:
            self._finalize_restart()
            return

    def save_session(self) -> bool:
        """Save current session to the session store.

        Returns:
            True if saved successfully, False otherwise
        """
        log('DEBUG', 'presenter.lifecycle', f'save_session called, current_session_id={self.state_bridge.current_session_id}')
        try:
            session = self._build_session_from_current_state()
            if session is None:
                log('DEBUG', 'presenter.lifecycle', f'No session to save')
                return False
            if not self.state_bridge.current_session_id:
                self.state_bridge.current_session_id = str(session.session_id)
            else:
                session.session_id = str(self.state_bridge.current_session_id)
            self.state_bridge.current_session = session
            session.ensure_name()
            log('WARNING', 'presenter.lifecycle', f'About to save session {session.session_id} to store')
            self.session_store.save_session(session)
            self.state_bridge.session_name = session.metadata.get('name')
            self.session_store.set_current_session_id(self.state_bridge.current_session_id)
            log('DEBUG', 'presenter.lifecycle', f'Session saved to store: {self.session_store.get_session_path(session.session_id)}')
            if self.state_bridge._external_file_path:
                try:
                    pass
                except Exception as e:
                    log('DEBUG', 'presenter.lifecycle', f'Failed to export to external file: {e}')
            return True
        except Exception as e:
            log('ERROR', 'presenter.lifecycle', f'Error saving session: {e}')
            return False

    def load_session(self, filepath: str, target_session: Optional[Session]=None) -> bool:
        """Load a session from a JSON file.

        Args:
            filepath: Path to the session file
            target_session: Optional existing session to update in-place. If None,
                creates a new session object.

        Returns:
            True if loaded successfully, False otherwise
        """
        log('DEBUG', 'presenter.lifecycle', f'Auto-saving current session before loading new session')
        self.auto_save_current_session()
        try:
            filepath = os.path.abspath(filepath)
            with open(filepath, 'r') as f:
                session_dict = json.load(f)
            version = session_dict.get('version', 0)
            if version != 1:
                log('DEBUG', 'presenter.lifecycle', f'Warning: Session version {version} is not current (1). Attempting to load anyway.')
            if target_session is not None:
                target_session.update_from_persistable_dict(session_dict)
                session = target_session
                session.ensure_name()
            else:
                session = Session.from_persistable_dict(session_dict)
                session.ensure_name()
                self._register_session_callbacks(session)
            self.state_bridge.bind_session(session)
            self.state_bridge.update_external_file_path(filepath)
            if not self.state_bridge.session_name:
                self.state_bridge.session_name = os.path.basename(filepath)
            log('DEBUG', 'presenter.lifecycle', f'Session loaded from {filepath}: {len(session.user_history)} messages')
            return True
        except Exception as e:
            import traceback
            log('DEBUG', 'presenter.lifecycle', f'Error loading session: {e}\n{traceback.format_exc()}')
            return False

    def load_session_by_id(self, session_id: str, target_session: Optional[Session]=None) -> bool:
        """Load a session by ID from the session store.

        Args:
            session_id: ID of session to load
            target_session: Optional existing session to update in-place. If None,
                creates a new session object.
        """
        log('DEBUG', 'presenter.lifecycle', f'Loading session {session_id} from store')
        loaded_session = self.session_store.load_session(session_id)
        if loaded_session is None:
            log('DEBUG', 'presenter.lifecycle', f'Session {session_id} not found')
            return False
        if target_session is not None:
            data = loaded_session.to_persistable_dict()
            target_session.update_from_persistable_dict(data)
            session = target_session
            session.ensure_name()
        else:
            session = loaded_session
            session.ensure_name()
            self._register_session_callbacks(session)
        self.state_bridge.current_session = session
        self.state_bridge.current_session_id = str(session.session_id)
        self.state_bridge.bind_session(session)
        external_file_path = session.metadata.get('external_file_path')
        if external_file_path:
            external_file_path = os.path.abspath(external_file_path)
            self.state_bridge.update_external_file_path(external_file_path)
        if not self.state_bridge.session_name:
            if isinstance(session.created_at, datetime):
                self.state_bridge.session_name = f'Session {session.created_at:%Y-%m-%d %H:%M}'
            else:
                self.state_bridge.session_name = 'Untitled Session'
        log('DEBUG', 'presenter.lifecycle', f'Session loaded from store: {session_id} ({self.state_bridge.session_name})')
        return True

    def export_session(self, filepath: str, set_as_external: bool=False) -> bool:
        """Export current session to a specified file path (for backup/transfer).

        Args:
            filepath: Path to export the session JSON file
            set_as_external: If True, set this file as the external file path
                for future auto-saves. Default False.

        Returns:
            True if exported successfully, False otherwise
        """
        try:
            filepath = os.path.abspath(filepath)
            session = self._build_session_from_current_state()
            if session is None:
                log('DEBUG', 'presenter.lifecycle', f'No session to export')
                return False
            if not session.session_id:
                session.session_id = str(uuid.uuid4())
            session_dict = session.to_persistable_dict()
            if isinstance(session_dict.get('created_at'), datetime):
                session_dict['created_at'] = session_dict['created_at'].isoformat()
            if isinstance(session_dict.get('updated_at'), datetime):
                session_dict['updated_at'] = session_dict['updated_at'].isoformat()
            os.makedirs(os.path.dirname(filepath) if os.path.dirname(filepath) else '.', exist_ok=True)
            with open(filepath, 'w') as f:
                json.dump(session_dict, f, indent=2)
            log('DEBUG', 'presenter.lifecycle', f'Session exported to {filepath}')
            if set_as_external:
                self.state_bridge.update_external_file_path(filepath)
            return True
        except Exception as e:
            import traceback
            log('DEBUG', 'presenter.lifecycle', f'Error exporting session: {e}\n{traceback.format_exc()}')
            return False

    def list_sessions(self) -> List[Dict[str, Any]]:
        """List available sessions from the session store.

        Returns:
            List of session metadata dictionaries
        """
        try:
            sessions = self.session_store.list_sessions()
            log('DEBUG', 'presenter.lifecycle', f'Listed {len(sessions)} sessions')
            return sessions
        except Exception as e:
            log('DEBUG', 'presenter.lifecycle', f'Error listing sessions: {e}')
            return []

    def delete_session(self, session_id: str) -> bool:
        """Delete a session from the store.

        Args:
            session_id: ID of the session to delete
        """
        try:
            success = self.session_store.delete_session(session_id)
            if success:
                log('DEBUG', 'presenter.lifecycle', f'Deleted session {session_id}')
            else:
                log('DEBUG', 'presenter.lifecycle', f'Session {session_id} not found for deletion')
            return success
        except Exception as e:
            log('DEBUG', 'presenter.lifecycle', f'Error deleting session: {e}')
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
                log('DEBUG', 'presenter.lifecycle', f'Session {session_id} not found for rename')
                return False
            session.metadata['name'] = new_name
            self.session_store.save_session(session)
            if self.state_bridge.current_session_id == session_id:
                self.state_bridge.session_name = new_name
            log('DEBUG', 'presenter.lifecycle', f'Renamed session {session_id} to {new_name}')
            return True
        except Exception as e:
            log('DEBUG', 'presenter.lifecycle', f'Error renaming session: {e}')
            return False

    def _build_session_from_current_state(self):
        log('DEBUG', 'core.config', f'[CONFIG_TRACE] _build_session_from_current_state: workspace_path={self.state_bridge.current_config.workspace_path or "NOT_IN_DICT"}')
        """Construct a Session object from current presenter state."""
        log('DEBUG', 'presenter.lifecycle', f'_build_session_from_current_state: user_history length={(len(self.state_bridge.user_history) if self.state_bridge.user_history else 0)}, current_session_id={self.state_bridge.current_session_id}, current_session exists={self.state_bridge.current_session is not None}')
        conversation = None
        if self.state_bridge.user_history is not None:
            conversation = self.state_bridge.user_history
        else:
            conversation = self.controller.get_conversation() if hasattr(self.controller, 'get_conversation') else None
            if conversation is None:
                conversation = None
        if conversation is None:
            if self.state_bridge.current_session is not None:
                conversation = []
            else:
                log('DEBUG', 'presenter.lifecycle', f'_build_session_from_current_state: No conversation found and no current session')
                return None
        try:
            agent_config = self.state_bridge.create_agent_config()
        except Exception as e:
            log('DEBUG', 'presenter.lifecycle', f'Error creating agent config: {e}')
            return None
        # Preserve existing metadata from current session (excluding stale agent_config)
        metadata = {}
        if self.state_bridge.current_session is not None and self.state_bridge.current_session.metadata:
            for k, v in self.state_bridge.current_session.metadata.items():
                if k != 'agent_config':
                    metadata[k] = v
        if self.state_bridge.session_name:
            metadata['name'] = self.state_bridge.session_name
        metadata['agent_config'] = agent_config.model_dump()
        session = Session(session_id=self.state_bridge.current_session_id or str(uuid.uuid4()), user_history=conversation, metadata=metadata)
        session.ensure_name()
        if self.state_bridge.total_input > 0:
            session.total_input_tokens = self.state_bridge.total_input
        if self.state_bridge.total_output > 0:
            session.total_output_tokens = self.state_bridge.total_output
        if self.state_bridge.context_length > 0:
            session.context_length = self.state_bridge.context_length
        if self.state_bridge._external_file_path:
            session.metadata['external_file_path'] = self.state_bridge._external_file_path
        # Store workspace_path in session metadata for persistence
        workspace_path = self.state_bridge.current_config.workspace_path
        session.metadata['workspace_path'] = workspace_path  # always set, even if None
        return session

    def auto_save_current_session(self) -> bool:
        """Auto-save current session.
        
        Returns:
            True if saved successfully, False on error.
        """
        log('DEBUG', 'presenter.lifecycle', f'auto_save_current_session called, current_session_id={self.state_bridge.current_session_id}, current_session exists={self.state_bridge.current_session is not None}')
        log('DEBUG', 'presenter.lifecycle', f'Attempting auto-save (event-driven)')
        try:
            success = self.save_session()
            if success:
                log('DEBUG', 'presenter.lifecycle', f'Auto-saved session successfully')
                if self.state_bridge._external_file_path:
                    try:
                        self.export_session(self.state_bridge._external_file_path, set_as_external=False)
                    except Exception as e:
                        log('DEBUG', 'presenter.lifecycle', f'Failed to export to external file: {e}')
                return True
            else:
                log('DEBUG', 'presenter.lifecycle', f'Auto-save failed')
                return False
        except Exception as e:
            log('DEBUG', 'presenter.lifecycle', f'Error in auto-save: {e}')
            return False