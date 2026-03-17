# agent_presenter.py
"""
Presenter/ViewModel layer for Agent GUI.

Decouples business logic from UI components and provides a clean interface
between AgentGUI (view) and AgentController (model).
"""

import os
import json
from typing import Optional, List, Dict, Any
from enum import Enum, auto
from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot
from agent_controller import AgentController
from agent_core import AgentConfig
from tools import SIMPLIFIED_TOOL_CLASSES


class AgentState(Enum):
    """Explicit states for the agent's lifecycle."""
    IDLE = auto()           # No active session, ready to start
    RUNNING = auto()        # Agent is processing a query
    PAUSED = auto()         # Agent is paused, can be resumed
    WAITING_FOR_USER = auto()  # Agent needs user input (user_interaction_requested)
    STOPPED = auto()        # Agent has stopped (error, max_turns, etc.)
    FINISHED = auto()       # Agent completed successfully (final)


class AgentPresenter(QObject):
    """
    Handles business logic for agent control, event processing, and state management.
    
    Signals:
        state_changed(state: AgentState): Emitted when agent state changes
        event_received(event: dict): Emitted when a new event arrives from controller
        tokens_updated(total_input: int, total_output: int): Emitted when token counts update
        status_message(message: str): Emitted for status updates
        context_updated(context_length: int): Emitted when context token count updates
        error_occurred(error: str, traceback: str): Emitted for errors
        config_changed(config: dict): Emitted when configuration changes
    """
    
    # Signals
    state_changed = pyqtSignal(AgentState)
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
        self._state = AgentState.IDLE
        
        # Token tracking
        self.total_input = 0
        self.total_output = 0
        self.context_length = 0
        
        # Configuration
        self._config = self._load_default_config()
        self._cached_config = None
        
        # Event processing via signals
        self.controller.event_occurred.connect(self._process_event)
        
        # Load saved configuration if available
        self._load_config()
        
    @property
    def state(self) -> AgentState:
        """Current agent state."""
        return self._state
    
    @state.setter
    def state(self, new_state: AgentState):
        """Update state and emit signal."""
        if self._state != new_state:
            self._state = new_state
            self.state_changed.emit(new_state)
    
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
    
    def start_session(self, query: str, config: Optional[dict] = None):
        """
        Start a new agent session.
        
        Args:
            query: User query string
            config: Optional configuration overrides
        """
        if self.state != AgentState.IDLE:
            print(f"[Presenter] Cannot start session in state {self.state}")
            return
        
        try:
            # Create agent config
            agent_config = self.create_agent_config(config)
            
            # Cache config for restart_session
            self._cached_config = agent_config
            
            # Start controller
            self.controller.start(query, agent_config)
            self.state = AgentState.RUNNING
            self.status_message.emit("Session started")
            

            
        except Exception as e:
            self.state = AgentState.STOPPED
            self.error_occurred.emit(f"Failed to start session: {str(e)}", "")
            print(f"[Presenter] Error starting session: {e}")
    
    def can_restart(self) -> bool:
        """Check if restart is possible (has cached configuration)."""
        return self._cached_config is not None

    def restart_session(self):
        """Restart a fresh session with cached configuration."""
        if not self._cached_config:
            self.error_occurred.emit("No cached configuration for restart", "")
            return
        
        if self.state != AgentState.IDLE:
            self.stop_session()
        
        # Reset controller
        self.controller.reset()
        
        # Update state and status
        self.state = AgentState.IDLE
        self.status_message.emit("Ready for new session")
    
    def continue_session(self, query: str):
        """
        Continue an existing session with a new query.
        
        Args:
            query: User query string
        """
        if self.state not in [AgentState.PAUSED, AgentState.WAITING_FOR_USER]:
            print(f"[Presenter] Cannot continue session in state {self.state}")
            return
        
        try:
            self.controller.continue_session(query)
            self.state = AgentState.RUNNING
            self.status_message.emit("Session continued")
        except Exception as e:
            self.error_occurred.emit(f"Failed to continue session: {str(e)}", "")
    
    def pause_session(self):
        """Request pause of current session."""
        if self.state == AgentState.RUNNING:
            self.controller.request_pause()
            self.status_message.emit("Pause requested")
        else:
            print(f"[Presenter] Cannot pause in state {self.state}")
    
    def stop_session(self):
        """Stop current session."""
        self.controller.stop()
        self.state = AgentState.STOPPED
        self.status_message.emit("Session stopped")
    
    
    def _process_event(self, event: dict):
        """
        Process a single event from controller.
        
        Args:
            event: Event dictionary from AgentController
        """
        event_type = event.get("type")
        print(f"[Presenter] Processing event: {event_type}")
        
        # Emit raw event for UI to handle display
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
            
        elif event_type == "user_interaction_requested":
            self.state = AgentState.WAITING_FOR_USER
            self.status_message.emit("Waiting for user input")
            
        elif event_type == "paused":
            self.state = AgentState.PAUSED
            self.status_message.emit("Paused")
            
        elif event_type in ["final", "stopped", "max_turns", "thread_finished"]:
            
            if event_type == "final":
                self.state = AgentState.FINISHED
                self.status_message.emit("Completed successfully")
            else:
                self.state = AgentState.STOPPED
                if event_type == "stopped":
                    self.status_message.emit("Stopped")
                elif event_type == "max_turns":
                    self.status_message.emit("Max turns reached")
                else:
                    self.status_message.emit("Thread finished")
            
        elif event_type == "error":
            self.state = AgentState.STOPPED
            error_msg = event.get("message", "Unknown error")
            traceback = event.get("traceback", "")
            self.error_occurred.emit(error_msg, traceback)
            self.status_message.emit(f"Error: {error_msg}")
        
        # Emit status update for all event types
        self.status_message.emit(f"Event: {event_type}")
    
    def cleanup(self):
        """Clean up resources."""
        if self.controller.is_running:
            self.controller.stop()
        self.state = AgentState.IDLE