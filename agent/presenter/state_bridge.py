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
from agent.config import AgentConfig, load_default_config, load_factory_config, load_config, save_config
from agent.config.loader import _compute_config_diff
from agent.config import loader as config_loader
from agent.config.provider_profile import ProviderManager
from tools import SIMPLIFIED_TOOL_CLASSES
from session.models import Session, RuntimeParams, ObservableList

class StateBridge:
    """Bridge between configuration, session state, and token tracking."""

    def __init__(self, config_path: Optional[str]=None):
        self.config_path = config_path or str(Path.home() / '.thoughtmachine' / 'agent_config.json')
        # Start with factory defaults, not model defaults — ensures the
        # initial state matches exactly what load_factory_config() returns,
        # so that save_config() computes a clean diff against factory.
        default_dict = load_factory_config()
        self.current_config = AgentConfig(**default_dict)
        self.total_input = 0
        self.total_output = 0
        self.context_length = 0
        self.current_session: Optional[Session] = None
        self.current_session_id: Optional[str] = None
        self.session_name: Optional[str] = None
        self._external_file_path: Optional[str] = None
        self._pending_user_history: List[Dict[str, Any]] = []
        if os.path.exists(self.config_path):
            config_dict = load_config(self.config_path)
            self.current_config = AgentConfig(**config_dict)
            log('DEBUG', 'core.config', f'[CONFIG_TRACE] StateBridge loaded from config_path={self.config_path}')
        else:
            log('DEBUG', 'core.config', f'[CONFIG_TRACE] StateBridge using defaults (no config found at {self.config_path})')

    def get_config(self) -> dict:
        """Return current configuration dictionary."""
        return self.current_config.model_dump(exclude={'api_key'}, exclude_none=True)

    def get_agent_config(self) -> AgentConfig:
        """Return current configuration as an AgentConfig instance."""
        return self.current_config

    def update_agent_config(self, agent_config: AgentConfig) -> AgentConfig:
        """Update internal configuration from an AgentConfig instance.

        Args:
            agent_config: AgentConfig instance to update from

        Returns:
            The same AgentConfig instance (already up-to-date)
        """
        self.current_config = agent_config
        return agent_config

    def update_config(self, config_updates: dict) -> dict:
        """Update configuration with partial updates."""
        # Capture caller info
        caller_frame = traceback.extract_stack()[-3]  # 0=this, 1=update_config, 2=our caller
        caller_info = f'{caller_frame.filename}:{caller_frame.lineno} in {caller_frame.name}'
        log('DEBUG', 'core.config', f'[CONFIG_TRACE] state_bridge update_config CALLER={caller_info}')
        log('DEBUG', 'core.config', f'[CONFIG_TRACE] state_bridge update_config before: workspace_path={self.current_config.workspace_path}')
        log('DEBUG', 'core.config', f'[CONFIG_TRACE] state_bridge update_config incoming: workspace_path={config_updates.get("workspace_path", "KEY_MISSING")}')
        # Merge updates into current config
        current_dict = self.current_config.model_dump(exclude={'api_key'}, exclude_none=True)
        current_dict.update(config_updates)
        self.current_config = AgentConfig(**current_dict)
        log('DEBUG', 'core.config', f'[CONFIG_TRACE] state_bridge update_config after: workspace_path={self.current_config.workspace_path}')
        return self.current_config.model_dump(exclude={'api_key'}, exclude_none=True)

    def save_config(self, config: Optional[dict]=None, path: Optional[str]=None, mode: Optional[str] = None) -> bool:
        """Save configuration to file as a minimal diff overlay.

        Only the keys that differ from the factory defaults are persisted to
        the JSON file, keeping it small and allowing new factory defaults to
        flow through on upgrade.

        Before persisting, the ``system_prompt`` value is extracted and
        written to ``~/.thoughtmachine/custom_system_prompt.txt`` (or the file
        is removed if the value is ``None`` / empty, or if the value matches
        the factory-default prompt from ``resources/default_system_prompt.txt``).
        This keeps the JSON config clean and avoids confusion between the two
        storage locations.

        When ``mode='engineer'``, the custom prompt file is **always** removed
        (regardless of the system_prompt value) so that the engineer-mode
        prompt from the resources file is used instead.

        Args:
            config: Optional dictionary to save. If None, saves current config.
            path: Optional path to save to. If None, uses default config path.
            mode: Optional session mode. When ``'engineer'``, the custom prompt
                file is removed to force use of the engineer prompt resource.
        """
        config_to_save = config or self.current_config.model_dump(exclude={'api_key'}, exclude_none=True)
        save_path = path or self.config_path

        # ── Intercept system_prompt ────────────────────────────────────
        system_prompt = config_to_save.pop('system_prompt', None)
        custom_path = Path(config_loader.CUSTOM_SYSTEM_PROMPT_PATH)

        # Engineer mode always uses its own prompt resource — never save custom prompt
        if mode == 'engineer':
            try:
                custom_path.unlink(missing_ok=True)
            except (IOError, OSError) as exc:
                log('WARNING', 'presenter.state_bridge',
                    f'Could not remove {custom_path}: {exc}')
            log('DEBUG', 'presenter.state_bridge',
                f'Engineer mode — removed custom prompt file at {custom_path}')
        elif system_prompt and system_prompt.strip():
            prompt_stripped = system_prompt.strip()
            # If the prompt matches the factory default, treat it as
            # "reset to default" — delete the custom file so the
            # validator falls through to the file-based default.
            factory_default = config_loader.load_default_system_prompt_text()
            if prompt_stripped == factory_default.strip():
                try:
                    custom_path.unlink(missing_ok=True)
                except (IOError, OSError) as exc:
                    log('WARNING', 'presenter.state_bridge',
                        f'Could not remove {custom_path}: {exc}')
                log('DEBUG', 'presenter.state_bridge',
                    f'System prompt matches factory default — removed custom file at {custom_path}')
            else:
                custom_path.parent.mkdir(parents=True, exist_ok=True)
                custom_path.write_text(prompt_stripped + '\n', encoding='utf-8')
                log('DEBUG', 'presenter.state_bridge',
                    f'Wrote system prompt to {custom_path}')
        else:
            # No custom prompt → remove the file so the validator falls
            # through to the factory default.
            try:
                custom_path.unlink(missing_ok=True)
            except (IOError, OSError) as exc:
                log('WARNING', 'presenter.state_bridge',
                    f'Could not remove {custom_path}: {exc}')

        # ── Compute diff vs factory defaults ───────────────────────────
        factory_config = load_factory_config()
        # Normalize factory through AgentConfig so serialized types match
        # (e.g. session_permissions booleans vs permission strings)
        factory_normalized = AgentConfig(**factory_config).model_dump(
            exclude={'api_key'}, exclude_none=True
        )
        overlay = _compute_config_diff(factory_normalized, config_to_save)
        log('DEBUG', 'presenter.state_bridge',
            f'Config overlay has {len(overlay)} key(s) differing from factory')

        return save_config(overlay, save_path)

    def reset_config_to_factory(self) -> dict:
        """Reset configuration to factory defaults.

        Removes ``custom_system_prompt.txt`` if it exists and writes an
        empty overlay file so that all values revert to factory defaults
        on the next load.

        Returns:
            The factory config dict.
        """
        # Remove custom system prompt so validator falls through to file default
        custom_path = Path(config_loader.CUSTOM_SYSTEM_PROMPT_PATH)
        try:
            custom_path.unlink(missing_ok=True)
            log('DEBUG', 'presenter.state_bridge',
                f'Removed custom system prompt {custom_path}')
        except (IOError, OSError) as exc:
            log('WARNING', 'presenter.state_bridge',
                f'Could not remove {custom_path}: {exc}')

        # Load factory defaults and reset instance
        factory_config = load_factory_config()
        self.current_config = AgentConfig(**factory_config)

        # Write empty overlay (clears any previously saved user overrides)
        save_config({}, self.config_path)
        log('INFO', 'presenter.state_bridge',
            'Configuration reset to factory defaults')

        return self.current_config.model_dump(exclude={'api_key'}, exclude_none=True)

    def load_config(self, path: Optional[str]=None) -> dict:
        """Load configuration from file."""
        load_path = path or self.config_path
        config_dict = load_config(load_path)
        self.current_config = AgentConfig(**config_dict)
        return self.current_config.model_dump(exclude={'api_key'}, exclude_none=True)

    def create_agent_config(self, config_dict: Optional[dict]=None, total_input: int=0, total_output: int=0, mode: Optional[str] = None) -> AgentConfig:
        """
        Create AgentConfig instance from configuration dictionary.

        If the config or current config has a ``provider_id``, the matching
        profile is resolved via :class:`~agent.config.provider_profile.ProviderManager`
        to fill in ``provider_type``, ``base_url``, ``api_key``, and ``model``.

        Args:
            config_dict: Optional dictionary to override current config
            total_input: Current total input tokens for initial values
            total_output: Current total output tokens for initial values

        Returns:
            AgentConfig instance ready for use with controller
        """
        if config_dict is not None:
            config = {**self.current_config.model_dump(exclude={'api_key'}, exclude_none=True), **config_dict}
        else:
            config = self.current_config.model_dump(exclude={'api_key'}, exclude_none=True)

        # Resolve provider profile if provider_id is present
        provider_id = config.get('provider_id')
        if provider_id:
            manager = ProviderManager()
            config = manager.resolve_config(config)

        # Apply mode-based tool preset (overrides any tools from saved config)
        if mode:
            from agent.config.presets import get_tools_for_mode
            config['enabled_tools'] = get_tools_for_mode(mode)

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
        direct_mappings = [('model', 'model'), ('provider_type', 'provider_type'), ('provider_config', 'provider_config'), ('temperature', 'temperature'), ('max_turns', 'max_turns'), ('workspace_path', 'workspace_path'), ('detail', 'detail'), ('enabled_tools', 'enabled_tools'), ('turn_monitor_enabled', 'turn_monitor_enabled'), ('system_prompt', 'system_prompt'), ('provider_id', 'provider_id'), ('model_override', 'model_override')]
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
        if mode:
            agent_kwargs['mode'] = mode

        agent_config = AgentConfig(**agent_kwargs)

        return agent_config
    def bind_session(self, session: Session) -> None:
        """Bind a Session object as the source of truth for conversation state."""
        log('DEBUG', 'presenter.state_bridge', f'bind_session START: session_id={session.session_id}, user_history id={id(session.user_history)}, length={len(session.user_history)}, type={type(session.user_history)}, is_ObservableList={isinstance(session.user_history, ObservableList)}')
        self.current_session = session
        self.current_session_id = session.session_id
        self.session_name = session.metadata.get('name')
        log('DEBUG', 'presenter.state_bridge', f'bind_session: pending_history={len(self._pending_user_history)}, session.user_history id={id(session.user_history)}, length={len(session.user_history)}, is_ObservableList={isinstance(session.user_history, ObservableList)}')
        if self._pending_user_history and (not session.user_history):
            log('DEBUG', 'presenter.state_bridge', f'Performing slice assignment: session.user_history[:] = pending_user_history (id={id(session.user_history)}, len={len(self._pending_user_history)})')
            session.user_history[:] = self._pending_user_history
        self._pending_user_history.clear()
        self.total_input = session.total_input_tokens
        self.total_output = session.total_output_tokens
        self.context_length = session.context_length
        # Restore workspace_path from session metadata into the active config
        ws = session.metadata.get('workspace_path')
        if ws:
            current_dict = self.current_config.model_dump(exclude={'api_key'}, exclude_none=True)
            current_dict['workspace_path'] = ws
            self.current_config = AgentConfig(**current_dict)
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
        log('DEBUG', 'pipeline.token_update', f"update_token_totals: input={input_tokens}, output={output_tokens}")

    def update_context_length(self, context_length: int) -> None:
        """Update context length and sync with current session."""
        self.context_length = context_length
        if self.current_session:
            self.current_session.context_length = context_length
        log('DEBUG', 'pipeline.token_update', f"update_context_length: context_length={context_length}")

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

