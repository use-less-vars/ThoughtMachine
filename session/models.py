"""
Session management data models.

Defines the core concepts:
- RuntimeParams: Mutable parameters for LLM generation (temperature, max_tokens, top_p)
- SessionConfig: Immutable session-level configuration (model, system_prompt, toolset, safety_settings)
- ContainerMetadata: Metadata for session-scoped containers (not the live objects)
- Session: The atomic conversation unit containing all state.
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import List, Dict, Any, Optional
import uuid, hashlib, json, os
from thoughtmachine.security import merge_security_config, get_default_security_config
from agent.logging import log

class ObservableList(list):
    """A list that notifies a callback when mutated."""

    def __init__(self, iterable=(), callback=None):
        import sys
        log('DEBUG', 'debug.unknown', f"[ObservableList.__init__] Creating ObservableList id={id(self)}, input type={type(iterable)}, len={(len(iterable) if hasattr(iterable, '__len__') else 'N/A')}, callback={(callback.__qualname__ if callback else None)}")
        try:
            super().__init__(iterable)
            log('DEBUG', 'debug.unknown', f'[ObservableList.__init__] Success, list length={len(self)}, id={id(self)}')
        except Exception as e:
            log('ERROR', 'debug.unknown', f'[ObservableList.__init__] ERROR during initialization: {e}')
            raise
        self.callback = callback

    def _notify(self):
        callback_repr = self.callback.__qualname__ if self.callback else None
        log('DEBUG', 'debug.unknown', f'[ObservableList._notify] called on id={id(self)}, callback={callback_repr}')
        if self.callback:
            self.callback()

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self._notify()

    def __delitem__(self, key):
        super().__delitem__(key)
        self._notify()

    def append(self, item):
        super().append(item)
        self._notify()

    def extend(self, iterable):
        try:
            length = len(iterable) if hasattr(iterable, '__len__') else 'unknown'
        except:
            length = 'unknown'
        log('DEBUG', 'debug.unknown', f'[ObservableList.extend] called on id={id(self)} with iterable length={length}')
        super().extend(iterable)
        self._notify()

    def insert(self, index, item):
        super().insert(index, item)
        self._notify()

    def pop(self, index=-1):
        result = super().pop(index)
        self._notify()
        return result

    def remove(self, item):
        super().remove(item)
        self._notify()

    def clear(self):
        super().clear()
        self._notify()

    def __iadd__(self, other):
        result = super().__iadd__(other)
        self._notify()
        return result

    def __imul__(self, other):
        result = super().__imul__(other)
        self._notify()
        return result

    def sort(self, *, key=None, reverse=False):
        super().sort(key=key, reverse=reverse)
        self._notify()

    def reverse(self):
        super().reverse()
        self._notify()

@dataclass
class RuntimeParams:
    """Mutable runtime parameters that can be adjusted during a session."""
    temperature: float = 0.2
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary, excluding None values."""
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'RuntimeParams':
        """Create from dictionary."""
        return cls(**data)

@dataclass(frozen=True)
class SessionConfig:
    """Immutable session-level configuration. Set at creation and cannot be changed."""
    model: str
    system_prompt: str
    toolset: List[str] = field(default_factory=list)
    safety_settings: Optional[Dict[str, Any]] = None
    initial_params: RuntimeParams = field(default_factory=RuntimeParams)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        result = {'model': self.model, 'system_prompt': self.system_prompt, 'toolset': self.toolset, 'safety_settings': self.safety_settings, 'initial_params': self.initial_params.to_dict()}
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SessionConfig':
        """Create from dictionary."""
        init_params_data = data.pop('initial_params', {})
        init_params = RuntimeParams.from_dict(init_params_data) if init_params_data else RuntimeParams()
        return cls(initial_params=init_params, **data)

@dataclass
class ContainerMetadata:
    """Metadata about a container associated with a session (not the live container itself)."""
    container_id: Optional[str] = None
    image: Optional[str] = None
    workspace_path: Optional[str] = None
    volumes: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ContainerMetadata':
        """Create from dictionary."""
        return cls(**data)

@dataclass
class Session:
    """An atomic conversation unit with all its state."""
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    config: SessionConfig = field(default_factory=lambda: SessionConfig(model='deepseek-reasoner', system_prompt='You are a helpful assistant.', toolset=[]))
    runtime_params: RuntimeParams = field(default_factory=RuntimeParams)
    user_history: List[Dict[str, Any]] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    context_length: int = 0
    agent_context: List[Dict[str, Any]] = field(default_factory=list, compare=False, repr=False)
    containers: List[ContainerMetadata] = field(default_factory=list)
    preset_name: Optional[str] = field(default=None, compare=False)
    version: int = 1
    next_seq: int = 0
    summary: Optional[Dict[str, Any]] = field(default=None, compare=False, repr=False)
    agent_instance: Optional[Any] = field(default=None, compare=False, repr=False)
    metadata: Dict[str, Any] = field(default_factory=dict)
    security_config: Dict[str, Any] = field(default_factory=get_default_security_config)
    _conversation_changed_callbacks: List[Any] = field(default_factory=list, compare=False, repr=False)
    _conversation_version: int = field(default=0, compare=False, repr=False)
    conversation_hash: str = field(default='', compare=False, repr=False)

    def __post_init__(self):
        if self.context_length == 0:
            self.context_length = self.total_input_tokens + self.total_output_tokens
        self._wrap_user_history()
        self.ensure_name()
        try:
            conv_str = self._normalize_conversation_for_hash(self.user_history)
            self.conversation_hash = hashlib.md5(conv_str.encode()).hexdigest()[:8]
        except Exception:
            self.conversation_hash = ''

    def _wrap_user_history(self):
        """Wrap user_history with ObservableList if not already wrapped."""
        if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
            import sys
            log('DEBUG', 'debug.unknown', f"[Session] _wrap_user_history called, session_id={self.session_id}, is_ObservableList={isinstance(self.user_history, ObservableList)}, type={type(self.user_history)}, len={(len(self.user_history) if hasattr(self.user_history, '__len__') else 'N/A')}")
        if not isinstance(self.user_history, ObservableList):
            if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                import sys
                log('DEBUG', 'debug.unknown', f'[Session] _wrap_user_history: Creating ObservableList from current user_history')
            new_list = ObservableList(self.user_history, callback=self._on_conversation_changed)
            if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                import sys
                log('DEBUG', 'debug.unknown', f'[Session] _wrap_user_history: Created ObservableList, id={id(new_list)}, len={len(new_list)}')
            self.user_history = new_list
        else:
            self.user_history.callback = self._on_conversation_changed
            if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                import sys
                log('DEBUG', 'debug.unknown', f'[Session] _wrap_user_history: Already ObservableList, ensuring session callback, id={id(self.user_history)}, len={len(self.user_history)}')
        if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
            import sys
            callback_repr = self.user_history.callback.__qualname__ if self.user_history.callback else None
            log('DEBUG', 'debug.unknown', f'[Session] _wrap_user_history: after, len={len(self.user_history)}, id={id(self.user_history)}, callback={callback_repr}')

    def _get_next_seq(self) -> int:
        """Return the next sequence number and increment the counter."""
        seq = self.next_seq
        self.next_seq += 1
        return seq

    def ensure_name(self):
        """Ensure session has a name for display/persistence.
        
        If metadata doesn't have a 'name' key, generates a default name
        based on creation timestamp.
        """
        name = self.metadata.get('name', '')
        if not name or not str(name).strip():
            from datetime import datetime
            if isinstance(self.created_at, datetime):
                timestamp = self.created_at
            else:
                timestamp = datetime.now()
            self.metadata['name'] = f'Session {timestamp:%Y-%m-%d %H:%M}'

    @staticmethod
    def _normalize_conversation_for_hash(conversation: List[Dict[str, Any]]) -> str:
        """Create normalized JSON representation for consistent hashing.
        
        Strips transient fields and ensures consistent ordering for stable hashing.
        """
        from .utils import normalize_conversation_for_hash as normalize
        return normalize(conversation)

    def _on_conversation_changed(self):
        """Called when user_history is mutated."""
        import os
        log('DEBUG', 'session.session', f'_on_conversation_changed called, session_id={self.session_id}, callbacks={len(self._conversation_changed_callbacks)}')
        log('DEBUG', 'session.session', f'[SESSION] _on_conversation_changed: {len(self._conversation_changed_callbacks)} callbacks')
        for i, cb in enumerate(self._conversation_changed_callbacks):
            cb_repr = cb.__qualname__ if hasattr(cb, '__qualname__') else repr(cb)
            log('DEBUG', 'session.session', f'  Callback {i}: {cb_repr}')
        self.updated_at = datetime.now()
        self._conversation_version += 1
        try:
            conv_str = self._normalize_conversation_for_hash(self.user_history)
            self.conversation_hash = hashlib.md5(conv_str.encode()).hexdigest()[:8]
        except Exception:
            self.conversation_hash = ''
        for callback in self._conversation_changed_callbacks:
            try:
                callback_repr = callback.__qualname__ if hasattr(callback, '__qualname__') else repr(callback)
                log('DEBUG', 'session.session', f'invoking callback {callback_repr}')
                callback()
            except Exception as e:
                import traceback
                log('ERROR', 'session.session', f'callback error: {e}')
                log('ERROR', 'session.session', f'[SESSION] _on_conversation_changed callback error')

    def connect_conversation_changed(self, callback):
        """Register a callback to be invoked when user_history changes."""
        log('DEBUG', 'session.session', f'[SESSION] connect_conversation_changed: adding callback, total {len(self._conversation_changed_callbacks) + 1}')
        self._conversation_changed_callbacks.append(callback)

    def disconnect_conversation_changed(self, callback):
        """Remove a previously registered callback."""
        log('DEBUG', 'session.session', f'[SESSION] disconnect_conversation_changed: looking for callback {callback}, total callbacks {len(self._conversation_changed_callbacks)}')
        if callback in self._conversation_changed_callbacks:
            log('DEBUG', 'session.session', f'[SESSION] disconnect_conversation_changed: removing callback, remaining {len(self._conversation_changed_callbacks) - 1}')
            self._conversation_changed_callbacks.remove(callback)

    @property
    def conversation_version(self) -> int:
        """Get current conversation version (increments on each change)."""
        return self._conversation_version

    def get_conversation_snapshot(self) -> List[Dict[str, Any]]:
        """
        Get immutable snapshot of conversation.
        Returns deep copy to ensure immutability.
        """
        import copy
        return copy.deepcopy(self.user_history)

    def update_runtime_params(self, **kwargs) -> None:
        """Update mutable runtime parameters."""
        for key, value in kwargs.items():
            if hasattr(self.runtime_params, key):
                setattr(self.runtime_params, key, value)
            else:
                raise ValueError(f'Unknown runtime parameter: {key}')
        self.updated_at = datetime.now()

    def to_persistable_dict(self) -> Dict[str, Any]:
        """
        Convert session to a dictionary suitable for JSON serialization.
        Excludes non-persistable fields like agent_context (derived) and objects.
        """
        data = {'session_id': self.session_id, 'created_at': self.created_at.isoformat(), 'updated_at': datetime.now().isoformat(), 'config': self.config.to_dict(), 'runtime_params': self.runtime_params.to_dict(), 'user_history': list(self.user_history), 'containers': [c.to_dict() for c in self.containers], 'preset_name': self.preset_name, 'metadata': self.metadata, 'security_config': self.security_config, 'version': self.version, 'summary': self.summary, 'total_input_tokens': self.total_input_tokens, 'total_output_tokens': self.total_output_tokens, 'context_length': self.context_length}
        return data

    @classmethod
    def from_persistable_dict(cls, data: Dict[str, Any]) -> 'Session':
        """Reconstruct a Session from a persisted dictionary."""
        created_at = datetime.fromisoformat(data.get('created_at', '')) if data.get('created_at') else datetime.now()
        updated_at = datetime.fromisoformat(data.get('updated_at', '')) if data.get('updated_at') else datetime.now()
        config_data = data.get('config', {})
        config = SessionConfig.from_dict(config_data) if config_data else SessionConfig()
        runtime_params_data = data.get('runtime_params', {})
        runtime_params = RuntimeParams.from_dict(runtime_params_data) if runtime_params_data else RuntimeParams()
        user_history = data.get('user_history', [])
        max_seq = 0
        for i, msg in enumerate(user_history):
            if isinstance(msg, dict):
                if 'created_at' not in msg:
                    msg['created_at'] = updated_at.isoformat()
                if 'seq' not in msg:
                    msg['seq'] = i
                else:
                    try:
                        max_seq = max(max_seq, int(msg['seq']))
                    except (ValueError, TypeError):
                        msg['seq'] = i
                        max_seq = max(max_seq, i)
        next_seq_value = max(data.get('next_seq', 0), max_seq + 1)
        containers_data = data.get('containers', [])
        containers = [ContainerMetadata.from_dict(c) for c in containers_data]
        metadata = data.get('metadata', {})
        security_config_data = data.get('security_config', {})
        security_config = merge_security_config(security_config_data)
        version = data.get('version', 1)
        session = cls(session_id=str(data.get('session_id', str(uuid.uuid4()))), created_at=created_at, updated_at=updated_at, config=config, runtime_params=runtime_params, user_history=user_history, containers=containers, preset_name=data.get('preset_name'), metadata=metadata, security_config=security_config, version=version, summary=data.get('summary'), total_input_tokens=data.get('total_input_tokens', 0), total_output_tokens=data.get('total_output_tokens', 0), next_seq=next_seq_value, context_length=data.get('context_length', 0))
        return session

    def update_from_persistable_dict(self, data: Dict[str, Any]) -> None:
        """Update this session's data from a persistable dict (in-place, preserves callbacks)."""
        old_history = self.user_history
        created_at = datetime.fromisoformat(data.get('created_at', '')) if data.get('created_at') else datetime.now()
        updated_at = datetime.fromisoformat(data.get('updated_at', '')) if data.get('updated_at') else datetime.now()
        config_data = data.get('config', {})
        config = SessionConfig.from_dict(config_data) if config_data else SessionConfig()
        runtime_params_data = data.get('runtime_params', {})
        runtime_params = RuntimeParams.from_dict(runtime_params_data) if runtime_params_data else RuntimeParams()
        user_history = data.get('user_history', [])
        max_seq = 0
        for i, msg in enumerate(user_history):
            if isinstance(msg, dict):
                if 'created_at' not in msg:
                    msg['created_at'] = updated_at.isoformat()
                if 'seq' not in msg:
                    msg['seq'] = i
                else:
                    try:
                        max_seq = max(max_seq, int(msg['seq']))
                    except (ValueError, TypeError):
                        msg['seq'] = i
                        max_seq = max(max_seq, i)
        next_seq_value = max(data.get('next_seq', 0), max_seq + 1)
        containers_data = data.get('containers', [])
        containers = [ContainerMetadata.from_dict(c) for c in containers_data]
        metadata = data.get('metadata', {})
        security_config_data = data.get('security_config', {})
        security_config = merge_security_config(security_config_data)
        version = data.get('version', 1)
        self.session_id = str(data.get('session_id', str(uuid.uuid4())))
        self.created_at = created_at
        self.updated_at = updated_at
        self.config = config
        self.runtime_params = runtime_params
        old_history.clear()
        old_history.extend(user_history)
        self.containers = containers
        self.preset_name = data.get('preset_name')
        self.metadata = metadata
        self.security_config = security_config
        self.version = version
        self.summary = data.get('summary')
        self.total_input_tokens = data.get('total_input_tokens', 0)
        self.total_output_tokens = data.get('total_output_tokens', 0)
        self.next_seq = next_seq_value
        self.context_length = data.get('context_length', 0)
        self.agent_context = []

    def add_message(self, role: str, content: str, **kwargs) -> None:
        """
        Add a message to the session's user_history.
        Automatically updates the updated_at timestamp.
        """
        message = {'role': role, 'content': content, **kwargs}
        if 'created_at' not in message:
            message['created_at'] = datetime.now().isoformat()
        if 'seq' not in message:
            message['seq'] = self._get_next_seq()
        self.user_history.append(message)
        self.updated_at = datetime.now()

    def create_agent(self, config):
        """
        Create an Agent instance associated with this session.
        The agent will use this session's user_history as its conversation source.
        """
        from agent import Agent
        agent = Agent(config, session=self)
        self.agent_instance = agent
        return agent