"""
StateBridge: Configuration management, session binding, and token tracking.

- Configuration loading/saving/updating
- AgentConfig creation from configuration dictionaries
- Session binding and external file path management
- Token total tracking (input, output, context)
"""
import os
import json
import traceback
from datetime import datetime
from typing import Optional, Dict, Any, List
from pathlib import Path
from agent.logging import log
from agent.config import AgentConfig, load_default_config, load_config, save_config, update_config
from tools import SIMPLIFIED_TOOL_CLASSES
from session.models import Session, SessionConfig, RuntimeParams, ObservableList

class StateBridge:
    """Bridge between configuration, session state, and token tracking."""

    def __init__(self, config_path: str='agent_config.json'):
        self.config_path = config_path
        self.user_config_path = str(Path.home() / '.thoughtmachine' / 'config.json')
        self._config = load_default_config()
        self.total_input = 0
        self.total_output = 0
        self.context_length = 0
        self.current_session: Optional[Session] = None
        self.current_session_id: Optional[str] = None
        self.session_name: Optional[str] = None
        self._external_file_path: Optional[str] = None
        self._pending_user_history: List[Dict[str, Any]] = []
        if os.path.exists(self.user_config_path):
            self._config = load_config(self.user_config_path)
        else:
            self._config = load_config(self.config_path)

    def get_config(self) -> dict:
        """Return current configuration dictionary."""
        return self._config.copy()

    def update_config(self, config_updates: dict) -> dict:
        """Update configuration with partial updates."""
        # Capture caller info
        caller_frame = traceback.extract_stack()[-3]  # 0=this, 1=update_config, 2=our caller
        caller_info = f'{caller_frame.filename}:{caller_frame.lineno} in {caller_frame.name}'
        log('DEBUG', 'core.config', f'[CONFIG_TRACE] state_bridge update_config CALLER={caller_info}')
        log('DEBUG', 'core.config', f'[CONFIG_TRACE] state_bridge update_config before: workspace_path={self._config.get("workspace_path", "NOT_IN_DICT")}')
        log('DEBUG', 'core.config', f'[CONFIG_TRACE] state_bridge update_config incoming: workspace_path={config_updates.get("workspace_path", "KEY_MISSING")}')
        self._config = update_config(self._config, config_updates)
        log('DEBUG', 'core.config', f'[CONFIG_TRACE] state_bridge update_config after: workspace_path={self._config.get("workspace_path", "NOT_IN_DICT")}')
        return self._config

    def save_config(self, config: Optional[dict]=None, path: Optional[str]=None) -> bool:
        """Save configuration to file."""
        config_to_save = config or self._config
        save_path = path or self.config_path
        return save_config(config_to_save, save_path)

    def save_user_config(self, config: Optional[dict]=None) -> bool:
        """Save configuration to user config file."""
        config_to_save = config or self._config
        return save_config(config_to_save, self.user_config_path)

    def load_config(self, path: Optional[str]=None) -> dict:
        """Load configuration from file."""
        load_path = path or self.config_path
        self._config = load_config(load_path)
        return self._config

    def load_user_config(self) -> dict:
        """Load configuration from user config file."""
        self._config = load_config(self.user_config_path)
        return self._config

    def create_agent_config(self, config_dict: Optional[dict]=None, total_input: int=0, total_output: int=0) -> AgentConfig:
        """
        Create AgentConfig instance from configuration dictionary.
        
        Args:
            config_dict: Optional dictionary to override current config
            total_input: Current total input tokens for initial values
            total_output: Current total output tokens for initial values
            
        Returns:
            AgentConfig instance ready for use with controller
        """
        if config_dict is not None:
            config = {**self._config, **config_dict}
        else:
            config = self._config
        api_key = config.get('api_key') or os.getenv('OPENAI_API_KEY') or os.getenv('DEEPSEEK_API_KEY')
        if not api_key:
            raise ValueError('Neither OPENAI_API_KEY nor DEEPSEEK_API_KEY environment variables are set, and no api_key in config. Please set one of them or add api_key to config.')
        enabled_tools = config.get('enabled_tools', [])
        tool_classes = []
        for tool_cls in SIMPLIFIED_TOOL_CLASSES:
            if tool_cls.__name__ in enabled_tools:
                tool_classes.append(tool_cls)
        agent_kwargs = {}
        agent_kwargs['api_key'] = api_key
        direct_mappings = [('model', 'model'), ('provider_type', 'provider_type'), ('provider_config', 'provider_config'), ('temperature', 'temperature'), ('max_turns', 'max_turns'), ('workspace_path', 'workspace_path'), ('detail', 'detail'), ('token_monitor_enabled', 'token_monitor_enabled'), ('enabled_tools', 'enabled_tools'), ('turn_monitor_enabled', 'turn_monitor_enabled'), ('turn_monitor_warning_threshold', 'turn_monitor_warning_threshold'), ('turn_monitor_critical_threshold', 'turn_monitor_critical_threshold'), ('max_history_turns', 'max_history_turns'), ('keep_initial_query', 'keep_initial_query'), ('keep_system_messages', 'keep_system_messages'), ('system_prompt', 'system_prompt')]
        for config_key, agent_key in direct_mappings:
            if config_key in config:
                agent_kwargs[agent_key] = config[config_key]
        if 'tool_output_token_limit' in config:
            agent_kwargs['tool_output_token_limit'] = config['tool_output_token_limit']
        elif 'tool_output_limit' in config:
            agent_kwargs['tool_output_token_limit'] = config['tool_output_limit']
        if 'token_monitor_warning_threshold' in config:
            agent_kwargs['token_monitor_warning_threshold'] = config['token_monitor_warning_threshold']
        elif 'warning_threshold' in config:
            agent_kwargs['token_monitor_warning_threshold'] = config['warning_threshold'] * 1000
        if 'token_monitor_critical_threshold' in config:
            agent_kwargs['token_monitor_critical_threshold'] = config['token_monitor_critical_threshold']
        elif 'critical_threshold' in config:
            agent_kwargs['token_monitor_critical_threshold'] = config['critical_threshold'] * 1000
        base_url = config.get('base_url')
        if base_url:
            agent_kwargs['base_url'] = base_url
        agent_kwargs['tool_classes'] = tool_classes
        agent_config = AgentConfig(**agent_kwargs)
        agent_config.initial_input_tokens = total_input
        agent_config.initial_output_tokens = total_output
        return agent_config

    def bind_session(self, session: Session) -> None:
        """Bind a Session object as the source of truth for conversation state."""
        if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
            log('DEBUG', 'presenter.state_bridge', f'bind_session START: session_id={session.session_id}, user_history id={id(session.user_history)}, length={len(session.user_history)}, type={type(session.user_history)}, is_ObservableList={isinstance(session.user_history, ObservableList)}')
        self.current_session = session
        self.current_session_id = session.session_id
        self.session_name = session.metadata.get('name')
        if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
            log('DEBUG', 'presenter.state_bridge', f'bind_session: pending_history={len(self._pending_user_history)}, session.user_history id={id(session.user_history)}, length={len(session.user_history)}, is_ObservableList={isinstance(session.user_history, ObservableList)}')
        if self._pending_user_history and (not session.user_history):
            if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                log('DEBUG', 'presenter.state_bridge', f'Performing slice assignment: session.user_history[:] = pending_user_history (id={id(session.user_history)}, len={len(self._pending_user_history)})')
            session.user_history[:] = self._pending_user_history
        self._pending_user_history.clear()
        self.total_input = session.total_input_tokens
        self.total_output = session.total_output_tokens
        self.context_length = session.context_length
        # Restore workspace_path from session metadata into the active config
        ws = session.metadata.get('workspace_path')
        if ws:
            self._config['workspace_path'] = ws
        external_file_path = session.metadata.get('external_file_path')
        if external_file_path:
            self._external_file_path = os.path.abspath(external_file_path)
        else:
            self._external_file_path = None

    def update_external_file_path(self, filepath: Optional[str]) -> None:
        """Update external file path in session metadata."""
        self._external_file_path = filepath
        if self.current_session:
            if filepath:
                self.current_session.metadata['external_file_path'] = filepath
            else:
                self.current_session.metadata.pop('external_file_path', None)

    def update_token_totals(self, input_tokens: int, output_tokens: int) -> None:
        """Update token totals and sync with current session."""
        self.total_input = input_tokens
        self.total_output = output_tokens
        if self.current_session:
            self.current_session.total_input_tokens = input_tokens
            self.current_session.total_output_tokens = output_tokens

    def update_context_length(self, context_length: int) -> None:
        """Update context length and sync with current session."""
        self.context_length = context_length
        if self.current_session:
            self.current_session.context_length = context_length

    @property
    def user_history(self) -> List[Dict[str, Any]]:
        """User conversation history from current session."""
        if self.current_session:
            return self.current_session.user_history
        return self._pending_user_history

    @user_history.setter
    def user_history(self, history: List[Dict[str, Any]]) -> None:
        """Set user conversation history."""
        if self.current_session:
            self.current_session.user_history[:] = history
        else:
            self._pending_user_history[:] = history

    def build_session_config(self, agent_config: AgentConfig) -> SessionConfig:
        """Build SessionConfig from an AgentConfig instance."""
        runtime_params = RuntimeParams(temperature=agent_config.temperature, max_tokens=agent_config.max_tokens if hasattr(agent_config, 'max_tokens') else None, top_p=agent_config.top_p if hasattr(agent_config, 'top_p') else None)
        session_config = SessionConfig(model=agent_config.model, system_prompt=agent_config.system_prompt, toolset=[cls.__name__ for cls in agent_config.tool_classes], safety_settings=None, initial_params=runtime_params)
        return session_config