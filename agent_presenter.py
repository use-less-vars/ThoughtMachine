# agent_presenter.py
"""
Presenter/ViewModel layer for Agent GUI.

Decouples business logic from UI components and provides a clean interface
between AgentGUI (view) and AgentController (model).
"""

import os
import json
import uuid
from datetime import datetime
import traceback
from typing import Optional, List, Dict, Any
from enum import Enum, auto
from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot
from agent_controller import AgentController
from agent_core import AgentConfig
from tools import SIMPLIFIED_TOOL_CLASSES
from agent_state import ExecutionState
from session.models import Session, SessionConfig, RuntimeParams
from session.store import FileSystemSessionStore
from session.context_builder import ContextBuilder, LastNBuilder




class AgentPresenter(QObject):
    """
    Handles business logic for agent control, event processing, and state management.
    
    Signals:
        state_changed(state: ExecutionState): Emitted when agent state changes
        event_received(event: dict): Emitted when a new event arrives from controller
        tokens_updated(total_input: int, total_output: int): Emitted when token counts update
        status_message(message: str): Emitted for status updates
        context_updated(context_length: int): Emitted when context token count updates
        error_occurred(error: str, traceback: str): Emitted for errors
        config_changed(config: dict): Emitted when configuration changes
    """
    
    # Signals
    state_changed = pyqtSignal(ExecutionState)
    event_received = pyqtSignal(dict)
    tokens_updated = pyqtSignal(int, int)
    context_updated = pyqtSignal(int)
    status_message = pyqtSignal(str)
    error_occurred = pyqtSignal(str, str)
    config_changed = pyqtSignal(dict)
    
    def __init__(self, config_path: Optional[str] = None):
        super().__init__()
        self.controller = AgentController()
        self.config_path = config_path or "agent_config.json"
        self._state = ExecutionState.IDLE
        
        # Token tracking
        self.total_input = 0
        self.total_output = 0
        self.context_length = 0
        
        # Configuration
        self._config = self._load_default_config()
        self._cached_config = None
        self._restarting = False
        self._next_session_id = 1
        self.current_session_id = None
        # Session management
        self.session_store = FileSystemSessionStore()
        self.context_builder = LastNBuilder(keep_last_messages=100000, keep_system_prompt=True)  # Keep effectively unlimited messages to preserve full session history during loading
        self.user_history: List[Dict[str, Any]] = []
        self.current_session: Optional[Session] = None
        self.session_name: Optional[str] = None  # Optional user-provided name
        self.final_content: Optional[str] = None
        self.final_reasoning: Optional[str] = None
        self._initial_conversation: Optional[List[Dict[str, Any]]] = None  # For loading sessions

        # Event processing via signals
        print(f"[Presenter] Connecting controller event_occurred to _process_event")
        self.controller.event_occurred.connect(self._process_event)
        print(f"[Presenter] Connection made")
        
        # Load saved configuration if available
        self._load_config()
        
    @property
    def state(self) -> ExecutionState:
        """Current agent state."""
        return self._state
    
    @state.setter
    def state(self, new_state: ExecutionState):
        """Update state and emit signal."""
        print(f"[Presenter] state setter: {self._state} -> {new_state}")
        if self._state != new_state:
            self._state = new_state
            self.state_changed.emit(new_state)
            print(f"[Presenter] state changed signal emitted")
    
    def _load_default_config(self) -> dict:
        """Return default configuration dictionary."""
        """Return default configuration dictionary."""
        return {
            "temperature": 0.2,
            "max_turns": 100,
            "token_monitor_enabled": True,
            "warning_threshold": 35,  # in thousands
            "critical_threshold": 50,  # in thousands
            "workspace_path": None,
            "tool_output_limit": 10000,
            "model": "deepseek-reasoner",
            "detail": "normal",
            "enabled_tools": [cls.__name__ for cls in SIMPLIFIED_TOOL_CLASSES],
            "api_key": "",
            "base_url": "https://api.deepseek.com",
            "provider_type": "openai_compatible",
            "provider_config": {}
        }
    
    def _load_config(self):
        """Load configuration from file."""
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, 'r') as f:
                    saved_config = json.load(f)
                # Merge with defaults (preserve saved values)
                for key, value in saved_config.items():
                    if key in self._config:
                        self._config[key] = value
                print(f"[Presenter] Loaded config from {self.config_path}")
        except Exception as e:
            print(f"[Presenter] Error loading config: {e}")
    
    def save_config(self, config: Optional[dict] = None):
        """Save configuration to file."""
        try:
            config_to_save = config or self._config
            with open(self.config_path, 'w') as f:
                json.dump(config_to_save, f, indent=2)
            print(f"[Presenter] Saved config to {self.config_path}")
        except Exception as e:
            print(f"[Presenter] Error saving config: {e}")
    
    def get_config(self) -> dict:
        """Return current configuration dictionary."""
        return self._config.copy()
    
    def update_config(self, config_updates: dict):
        """Update configuration with partial updates."""
        self._config.update(config_updates)
        self.config_changed.emit(self._config.copy())
    
    def create_agent_config(self, config_dict: Optional[dict] = None) -> AgentConfig:
        """
        Create AgentConfig instance from configuration dictionary.

        Args:
            config_dict: Optional dictionary to override current config

        Returns:
            AgentConfig instance ready for use with controller
        """
        # Merge config_dict with current config if provided
        if config_dict is not None:
            config = {**self._config, **config_dict}
        else:
            config = self._config

        # Get API key from config or environment (try OPENAI_API_KEY then DEEPSEEK_API_KEY)
        api_key = config.get("api_key") or os.getenv("OPENAI_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise ValueError("Neither OPENAI_API_KEY nor DEEPSEEK_API_KEY environment variables are set, and no api_key in config. Please set one of them or add api_key to config.")

        # Create tools list from enabled tool names
        enabled_tools = config.get("enabled_tools", [])
        tool_classes = []
        for cls in SIMPLIFIED_TOOL_CLASSES:
            if cls.__name__ in enabled_tools:
                tool_classes.append(cls)

        # Build agent_kwargs with proper field mapping
        agent_kwargs = {}
        
        # Always include the API key (from config or environment)
        agent_kwargs["api_key"] = api_key

        # Direct mappings for other fields
        direct_mappings = [
            ("model", "model"),
            ("provider_type", "provider_type"),
            ("provider_config", "provider_config"),
            ("temperature", "temperature"),
            ("max_turns", "max_turns"),
            ("workspace_path", "workspace_path"),
            ("detail", "detail"),
            ("token_monitor_enabled", "token_monitor_enabled"),
            ("enabled_tools", "enabled_tools"),
            ("turn_monitor_enabled", "turn_monitor_enabled"),
            ("turn_monitor_warning_threshold", "turn_monitor_warning_threshold"),
            ("turn_monitor_critical_threshold", "turn_monitor_critical_threshold"),
            ("max_history_turns", "max_history_turns"),
            ("keep_initial_query", "keep_initial_query"),
            ("keep_system_messages", "keep_system_messages"),
            ("system_prompt", "system_prompt"),
        ]

        for config_key, agent_key in direct_mappings:
            if config_key in config:
                agent_kwargs[agent_key] = config[config_key]

        # Field renaming for tool output limit (backward compatibility)
        if "tool_output_token_limit" in config:
            agent_kwargs["tool_output_token_limit"] = config["tool_output_token_limit"]
        elif "tool_output_limit" in config:
            agent_kwargs["tool_output_token_limit"] = config["tool_output_limit"]

        # Handle token monitor thresholds with backward compatibility
        # Prefer actual token values if present, otherwise convert from thousands
        if "token_monitor_warning_threshold" in config:
            agent_kwargs["token_monitor_warning_threshold"] = config["token_monitor_warning_threshold"]
        elif "warning_threshold" in config:
            agent_kwargs["token_monitor_warning_threshold"] = config["warning_threshold"] * 1000

        if "token_monitor_critical_threshold" in config:
            agent_kwargs["token_monitor_critical_threshold"] = config["token_monitor_critical_threshold"]
        elif "critical_threshold" in config:
            agent_kwargs["token_monitor_critical_threshold"] = config["critical_threshold"] * 1000

        # Conditional base_url
        base_url = config.get("base_url")
        if base_url:
            agent_kwargs["base_url"] = base_url

        # Add tool_classes (created from enabled_tools)
        agent_kwargs["tool_classes"] = tool_classes

        # Create AgentConfig instance (AgentConfig will use defaults for missing fields)
        agent_config = AgentConfig(**agent_kwargs)

        return agent_config

    def _load_default_system_prompt(self) -> str:
        """Load the default system prompt from agent_core.py default.
        """
        # Create a minimal AgentConfig with defaults to get the default system_prompt
        try:
            default_config = AgentConfig(api_key="dummy")
            return default_config.system_prompt
        except Exception as e:
            print(f"[Presenter] Error loading default system prompt: {e}")
            return "You are a helpful assistant."

    def _build_session_config(self, agent_config: AgentConfig) -> SessionConfig:
        """Build SessionConfig from an AgentConfig instance."""
        from session.models import RuntimeParams, SessionConfig
        
        # Extract runtime params (use temperature from agent config)
        runtime_params = RuntimeParams(
            temperature=agent_config.temperature,
            max_tokens=agent_config.max_tokens if hasattr(agent_config, 'max_tokens') else None,
            top_p=agent_config.top_p if hasattr(agent_config, 'top_p') else None
        )
        
        # Build SessionConfig
        session_config = SessionConfig(
            model=agent_config.model,
            system_prompt=agent_config.system_prompt,
            toolset=[cls.__name__ for cls in agent_config.tool_classes],
            safety_settings=None,
            initial_params=runtime_params
        )
        return session_config

    def _extract_user_history(self, conversation: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Extract user/assistant messages from conversation, excluding system.
        """
        user_history = []
        for msg in conversation:
            role = msg.get("role", "")
            if role in ["user", "assistant"]:
                # Copy only essential fields to keep session size manageable
                user_msg = {
                    "role": role,
                    "content": msg.get("content", "")
                }
                # Preserve tool_calls and tool_call_id if present
                if "tool_calls" in msg:
                    user_msg["tool_calls"] = msg["tool_calls"]
                if "tool_call_id" in msg:
                    user_msg["tool_call_id"] = msg["tool_call_id"]
                user_history.append(user_msg)
        return user_history
    
    def _update_user_history(self, event_history: List[Dict[str, Any]]):
        """
        Update user_history with current conversation from event.
        
        Replaces user_history with the event's history (which may be pruned).
        This ensures we save exactly what the agent sees.
        """
        if event_history:
            self.user_history = event_history.copy()

    def start_session(self, query: str, config: Optional[dict] = None, preset_name: str = None):
        """
        Start a new agent session.

        Args:
            query: User query string
            config: Optional configuration overrides
            preset_name: Optional preset name to use instead of config
        """
        if self.state != ExecutionState.IDLE:
            print(f"[Presenter] Cannot start session in state {self.state}")
            return

        # Clear session name for a fresh session
        self.session_name = None

        try:
            # Determine if using preset_name or config dict
            if preset_name is not None:
                # preset_name takes precedence; config may be None or provide overrides
                agent_config = None  # not used
                # Cache preset_name for restart (maybe store a derived config? For restart, we can create a minimal config)
                # For simplicity, we'll store a placeholder; restart_session uses _cached_config, which expects AgentConfig
                # We'll create a dummy AgentConfig from the preset for caching
                from agent import Agent
                temp_agent = Agent.from_preset(preset_name, session_id=None)
                self._cached_config = temp_agent.config
                self._cached_preset_name = preset_name
                # Apply any overrides to the temporary config if needed? Not necessary for start, but restart will use preset
            else:
                # Create agent config from dict
                agent_config = self.create_agent_config(config)
                self._cached_config = agent_config
                self._cached_preset_name = None

            # Generate unique session ID
            session_id = self._next_session_id
            self._next_session_id += 1
            self.current_session_id = session_id
            # Clear user history unless resuming a loaded session
            if self._initial_conversation is None:
                self.user_history = []

            # Check if we have an initial conversation to resume from
            initial_conversation = None
            if self._initial_conversation is not None:
                initial_conversation = self._initial_conversation
                # For a resumed session, we may need to prepend the current query if it's not empty
                # However, the controller expects the query to be the first message if initial_conversation is None
                # If initial_conversation is provided, the query will be enqueued separately? Actually controller.start
                # enqueues the query regardless. For resumed sessions, the query is typically empty or we want to
                # continue the conversation. Let's handle this: if initial_conversation exists, we likely want to
                # ignore the query or treat it as an additional user message. For now, pass initial_conversation and
                # let the controller handle it - it will start with that context and then process the query.

            # Start controller with session ID and optional initial conversation
            self.controller.start(query, agent_config, session_id, initial_conversation, preset_name=preset_name, **({} if config is None else config))            
            self.state = ExecutionState.RUNNING
            self.status_message.emit("Session started")
            
            # Clear the initial conversation flag after using it
            self._initial_conversation = None
            

            
        except Exception as e:
            self.state = ExecutionState.STOPPED
            self.error_occurred.emit(f"Failed to start session: {str(e)}", "")
            print(f"[Presenter] Error starting session: {e}")
    
    def can_restart(self) -> bool:
        """Check if restart is possible (has cached configuration)."""
        return self._cached_config is not None

    def _finalize_restart(self):
        """Common restart cleanup: reset controller, counters, and state."""
        self.controller.reset()
        self.total_input = 0
        self.total_output = 0
        self.context_length = 0
        self.state = ExecutionState.IDLE
        self._restarting = False
        self.current_session_id = None
        self._initial_conversation = None  # Clear loaded session
        self.user_history = []
        self.session_name = None  # Clear session name for fresh start
        self.status_message.emit("Ready for new session")

    def restart_session(self, query: str = None):
        """
        Restart a fresh session with current configuration.
        Does NOT automatically start a new session. After restart, state is IDLE.
        """
        # Refresh config from current GUI
        try:
            self._cached_config = self.create_agent_config()
        except Exception as e:
            self.error_occurred.emit(f"Cannot create config for restart: {str(e)}", "")
            return

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

        # Otherwise, wait for terminal event; state = STOPPING
        self.state = ExecutionState.STOPPING
    def continue_session(self, query: str):
        """
        Continue an existing session with a new query.
        
        Args:
            query: User query string
        """
        if self.state not in [ExecutionState.PAUSED, ExecutionState.WAITING_FOR_USER]:
            print(f"[Presenter] Cannot continue session in state {self.state}")
            return
        
        try:
            self.controller.continue_session(query)
            self.state = ExecutionState.RUNNING
            self.status_message.emit("Session continued")
        except Exception as e:
            self.error_occurred.emit(f"Failed to continue session: {str(e)}", "")
    
    def pause_session(self):
        """Request pause of current session."""
        if self.state == ExecutionState.RUNNING:
            self.controller.request_pause()
            self.state = ExecutionState.PAUSING
        else:
            print(f"[Presenter] Cannot pause in state {self.state}")
    
    def stop_session(self):
        """Stop current session."""
        self.controller.stop()
        self.state = ExecutionState.STOPPING

    # ----- Session Management -----

    def save_session(self) -> bool:
        """Save current session to the session store.

        Returns:
            True if saved successfully, False otherwise
        """
        try:
            # Build session from current state
            session = self._build_session_from_current_state()
            if session is None:
                print(f"[Presenter] No session to save")
                return False

            # Ensure we have a session_id (new session if None)
            if not self.current_session_id:
                self.current_session_id = session.session_id
            else:
                # Preserve the existing session_id
                session.session_id = self.current_session_id

            # Set current_session reference
            self.current_session = session

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
            self.session_name = session.metadata.get('name')

            print(f"[Presenter] Session saved to store: {self.session_store.get_session_path(session.session_id)}")
            return True
        except Exception as e:
            print(f"[Presenter] Error saving session: {e}")
            traceback.print_exc()
            return False

    def export_session(self, filepath: str) -> bool:
        """Export current session to a specified file path (for backup/transfer).

        Args:
            filepath: Path to export the session JSON file

        Returns:
            True if exported successfully, False otherwise
        """
        try:
            session = self._build_session_from_current_state()
            if session is None:
                print(f"[Presenter] No session to export")
                return False

            # Use the session's ID if available, otherwise generate a temporary one for export
            if not session.session_id:
                session.session_id = str(uuid.uuid4())

            # Serialize to JSON (version already included by to_persistable_dict)
            session_dict = session.to_persistable_dict()
            # Ensure datetime objects are serialized (they are already isoformat in to_persistable_dict)
            # But to be safe, convert any datetime that might not be converted
            if isinstance(session_dict.get('created_at'), datetime):
                session_dict['created_at'] = session_dict['created_at'].isoformat()
            if isinstance(session_dict.get('updated_at'), datetime):
                session_dict['updated_at'] = session_dict['updated_at'].isoformat()

            # Ensure directory exists
            os.makedirs(os.path.dirname(filepath) if os.path.dirname(filepath) else '.', exist_ok=True)

            with open(filepath, 'w') as f:
                json.dump(session_dict, f, indent=2)

            print(f"[Presenter] Session exported to {filepath}")
            return True
        except Exception as e:
            print(f"[Presenter] Error exporting session: {e}")
            traceback.print_exc()
            return False

    def load_session(self, filepath: str) -> bool:
        """Load a session from a JSON file.

        Args:
            filepath: Path to the session file

        Returns:
            True if loaded successfully, False otherwise
        """
        try:
            with open(filepath, 'r') as f:
                session_dict = json.load(f)

            # Check session version
            version = session_dict.get('version', 0)
            if version != 1:
                print(f"[Presenter] Warning: Session version {version} is not current (1). Attempting to load anyway.")

            # Reconstruct Session object
            session = Session.from_persistable_dict(session_dict)

            # Set as current session
            self.current_session = session
            self.session_name = os.path.basename(filepath)
            # Store full history in user_history
            self.user_history = session.user_history.copy()
            # Use context builder to create pruned initial conversation for agent
            self._initial_conversation = self.context_builder.build(session.user_history)

            print(f"[Presenter] Session loaded from {filepath}: {len(session.user_history)} messages")
            return True
        except Exception as e:
            print(f"[Presenter] Error loading session: {e}")
            traceback.print_exc()
            return False

    def list_sessions(self) -> List[Dict[str, Any]]:
        """List available sessions from the session store.

        Returns:
            List of session metadata dictionaries
        """
        try:
            # The store returns list of dicts directly
            sessions = self.session_store.list_sessions()
            # Each session dict already contains: session_id, name, created_at, updated_at, preview
            # Transform to the format expected by the GUI
            result = []
            for sess in sessions:
                result.append({
                    'id': sess.get('session_id'),
                    'name': sess.get('name', 'Untitled Session'),
                    'created_at': sess.get('created_at'),
                    'updated_at': sess.get('updated_at'),
                    'preview': sess.get('preview', '')
                })
            return result
        except Exception as e:
            print(f"[Presenter] Error listing sessions: {e}")
            return []

    def delete_session(self, session_id: str) -> bool:
        """Delete a session from the store.

        Args:
            session_id: ID of the session to delete

        Returns:
            True if deleted, False if not found or error
        """
        try:
            success = self.session_store.delete_session(session_id)
            if success:
                print(f"[Presenter] Deleted session {session_id}")
            else:
                print(f"[Presenter] Session {session_id} not found")
            return success
        except Exception as e:
            print(f"[Presenter] Error deleting session: {e}")
            return False

    def rename_session(self, session_id: str, new_name: str) -> bool:
        """Rename a session's metadata name.

        Args:
            session_id: ID of the session to rename
            new_name: New name for the session

        Returns:
            True if renamed successfully, False otherwise
        """
        try:
            session = self.session_store.load_session(session_id)
            if session is None:
                return False
            session.metadata['name'] = new_name
            session.updated_at = datetime.now()
            self.session_store.save_session(session)
            # If the renamed session is currently loaded, update its metadata in memory
            if self.current_session and self.current_session.session_id == session_id:
                self.current_session.metadata['name'] = new_name
            return True
        except Exception as e:
            print(f"[Presenter] Error renaming session {session_id}: {e}")
            return False

    def _build_session_from_current_state(self) -> Optional[Session]:
        """Construct a Session object from current presenter state.
        """
        # Get the full conversation from the current session
        conversation = None
        if self.user_history:
            conversation = self.user_history
        else:
            conversation = self.controller.get_conversation()
            if conversation is None:
                conversation = self._initial_conversation
                if conversation is None:
                    return None

        if not conversation:
            return None

        # Build session config from current agent config
        try:
            agent_config = self.create_agent_config()
            session_config = self._build_session_config(agent_config)
        except Exception as e:
            print(f"[Presenter] Error building session config: {e}")
            return None

        # Preserve full conversation including system, user, assistant, and tool messages
        # Normalize tool calls to flattened format for consistency
        def normalize_tool_call(msg):
            if msg.get('role') == 'assistant' and 'tool_calls' in msg:
                normalized = []
                for tc in msg['tool_calls']:
                    if 'name' in tc:
                        # Already flattened; keep as is
                        normalized.append(tc)
                    else:
                        # Convert from OpenAI format (function.name, function.arguments)
                        function = tc.get('function', {})
                        normalized.append({
                            'name': function.get('name', 'Unknown'),
                            'arguments': function.get('arguments', {}),
                            'result': tc.get('result', '')
                        })
                msg['tool_calls'] = normalized
            return msg

        user_history = [normalize_tool_call(m.copy()) for m in conversation]

        # Capture runtime parameters from agent if available
        runtime_params = RuntimeParams()
        try:
            agent = self.controller.agent
            if agent and hasattr(agent, 'runtime_params'):
                rp = agent.runtime_params
                runtime_params = RuntimeParams(
                    temperature=rp.temperature,
                    max_tokens=rp.max_tokens,
                    top_p=rp.top_p
                )
        except Exception:
            pass

        # Create session object
        now = datetime.now()
        session = Session(
            session_id=str(self.current_session_id) if self.current_session_id else str(uuid.uuid4()),
            created_at=now,
            updated_at=now,
            config=session_config,
            runtime_params=runtime_params,
            user_history=user_history,
            version=1
        )

        # Preserve metadata and other fields from current_session if available
        if self.current_session:
            session.metadata = self.current_session.metadata.copy()
            session.preset_name = self.current_session.preset_name
            session.containers = self.current_session.containers.copy()
            # Preserve original created_at for continuity
            session.created_at = self.current_session.created_at
            # Preserve final content and reasoning
            session.final_content = self.current_session.final_content
            session.final_reasoning = self.current_session.final_reasoning
        else:
            # Use captured final content if available (e.g., final event before first save)
            session.final_content = self.final_content
            session.final_reasoning = self.final_reasoning

        return session

    def _process_event(self, event: dict):
        """
        Process a single event from controller.
        
        Args:
            event: Event dictionary from AgentController
        """
        event_type = event.get("type")
        print(f"[Presenter] Processing event: {event_type}")
        
        # Skip filtering for state/terminal events as they need to be shown regardless
        state_event_types = ["error", "paused", "stopped", "thread_finished", "final", "max_turns", "user_interaction_requested"]
        if event_type not in state_event_types:
            event_session_id = event.get("session_id")
            if event_session_id is not None and event_session_id != self.current_session_id:
                print(f"[Presenter] Ignoring event from old session {event_session_id}, current is {self.current_session_id}")
                return
        
        # Emit raw event for UI to handle display
        print(f"[Presenter] Emitting event_received: {event_type}")
        self.event_received.emit(event)
        
        # Update state based on event type
        if event_type == "turn":
            # Update token counts if available
            # Support both naming conventions: total_input_tokens/total_output_tokens and total_input/total_output
            # Token counts are typically inside event["usage"] dict
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
            
            if input_tokens is not None and output_tokens is not None:
                self.total_input = input_tokens
                self.total_output = output_tokens
                self.tokens_updated.emit(self.total_input, self.total_output)
            
            # Update context length if available (either directly or in usage dict)
            context_length = None
            if "context_length" in event:
                context_length = event["context_length"]
            elif "usage" in event and "context_length" in event["usage"]:
                context_length = event["usage"]["context_length"]
            elif "usage" in event and "current_conversation_tokens" in event["usage"]:
                context_length = event["usage"]["current_conversation_tokens"]
            
            if context_length is not None:
                self.context_length = context_length
                self.context_updated.emit(self.context_length)
            # Update user_history with full conversation
            if "history" in event:
                self._update_user_history(event["history"])
            
        elif event_type == "user_interaction_requested":
            self.state = ExecutionState.WAITING_FOR_USER
            self.status_message.emit("Waiting for user input")
            # Update user_history with full conversation
            if "history" in event:
                self._update_user_history(event["history"])
            
        elif event_type == "paused":
            print(f"[Presenter] Handling paused event")
            self.state = ExecutionState.PAUSED
            self.status_message.emit("Paused")
            
        elif event_type in ["final", "stopped", "max_turns", "thread_finished"]:
            print(f"[Presenter] Handling terminal event: {event_type}")
            if event_type == "final":
                self.state = ExecutionState.FINALIZED
                self.status_message.emit("Completed successfully")

                # Capture final content and reasoning
                self.final_content = event.get('content')
                self.final_reasoning = event.get('reasoning')
                if self.current_session:
                    self.current_session.final_content = self.final_content
                    self.current_session.final_reasoning = self.final_reasoning
            elif event_type == "max_turns":
                self.state = ExecutionState.MAX_TURNS_REACHED
                self.status_message.emit("Max turns reached")
            else:  # "stopped" or "thread_finished"
                if self._restarting:
                    self._finalize_restart()
                else:
                    self.state = ExecutionState.STOPPED
                    if event_type == "stopped":
                        self.status_message.emit("Stopped")
                    else:
                        self.status_message.emit("Thread finished")
            # Update user_history with full conversation if present
            if "history" in event:
                self._update_user_history(event["history"])
            
        elif event_type == "error":
            self.state = ExecutionState.STOPPED
            error_msg = event.get("message", "Unknown error")
            traceback = event.get("traceback", "")
            self.error_occurred.emit(error_msg, traceback)
            self.status_message.emit(f"Error: {error_msg}")
            # Update user_history with full conversation if present
            if "history" in event:
                self._update_user_history(event["history"])
        
        # Emit status update for all event types
        self.status_message.emit(f"Event: {event_type}")
    
    def cleanup(self):
        """Clean up resources."""
        if self.controller.is_running:
            self.controller.stop()
        self.state = ExecutionState.IDLE