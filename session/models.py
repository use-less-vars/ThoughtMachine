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


class ObservableList(list):
    """A list that notifies a callback when mutated."""
    def __init__(self, iterable=(), callback=None):
        super().__init__(iterable)
        self.callback = callback

    def _notify(self):
        if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
            import sys, traceback
            sys.stderr.write(f'[ObservableList] _notify called, callback={self.callback}\n')
            # Don't print stack trace - too verbose
            # traceback.print_stack(limit=5, file=sys.stderr)
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
    # Future: frequency_penalty, presence_penalty, etc.

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
    toolset: List[str] = field(default_factory=list)  # List of tool class names
    safety_settings: Optional[Dict[str, Any]] = None
    initial_params: RuntimeParams = field(default_factory=RuntimeParams)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        result = {
            'model': self.model,
            'system_prompt': self.system_prompt,
            'toolset': self.toolset,
            'safety_settings': self.safety_settings,
            'initial_params': self.initial_params.to_dict(),
        }
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SessionConfig':
        """Create from dictionary."""
        # Handle nested initial_params
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
    # Add other fields as needed (e.g., hostname, environment, ports)

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
    config: SessionConfig = field(default_factory=lambda: SessionConfig(
        model="deepseek-reasoner",
        system_prompt="You are a helpful assistant.",
        toolset=[]
    ))
    runtime_params: RuntimeParams = field(default_factory=RuntimeParams)
    user_history: List[Dict[str, Any]] = field(default_factory=list)
    # Token usage tracking
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    context_length: int = 0
    # agent_context is not persisted; it's derived from user_history on load.
    # But we may keep a cached version during runtime.
    agent_context: List[Dict[str, Any]] = field(default_factory=list, compare=False, repr=False)
    containers: List[ContainerMetadata] = field(default_factory=list)
    preset_name: Optional[str] = field(default=None, compare=False)
    version: int = 1  # Session format version
    final_content: Optional[str] = None  # Content of the Final tool's result, if any
    final_reasoning: Optional[str] = None  # Reasoning that preceded the final answer
    summary: Optional[Dict[str, Any]] = field(default=None, compare=False, repr=False)  # Summary system message (from pruning)

    # Runtime reference to the active Agent instance (not persisted)
    agent_instance: Optional[Any] = field(default=None, compare=False, repr=False)

    metadata: Dict[str, Any] = field(default_factory=dict)  # name, tags, notes, etc.
    _conversation_changed_callbacks: List[Any] = field(default_factory=list, compare=False, repr=False)
    _conversation_version: int = field(default=0, compare=False, repr=False)  # Increments on each history change
    conversation_hash: str = field(default="", compare=False, repr=False)  # Hash of current conversation content

    def __post_init__(self):
        # Ensure context_length reflects token counts if not already set
        if self.context_length == 0:
            self.context_length = self.total_input_tokens + self.total_output_tokens
        # Wrap user_history with observable list
        self._wrap_user_history()
        # Ensure session has a name
        self.ensure_name()
        # Compute initial conversation hash
        try:
            conv_str = self._normalize_conversation_for_hash(self.user_history)
            self.conversation_hash = hashlib.md5(conv_str.encode()).hexdigest()[:8]
        except Exception:
            self.conversation_hash = ""

    def _wrap_user_history(self):
        """Wrap user_history with ObservableList if not already wrapped."""
        if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
            import sys
            sys.stderr.write(f'[Session] _wrap_user_history called, session_id={self.session_id}, is_ObservableList={isinstance(self.user_history, ObservableList)}\n')
        if not isinstance(self.user_history, ObservableList):
            self.user_history = ObservableList(self.user_history, callback=self._on_conversation_changed)
        else:
            # Ensure callback is set
            self.user_history.callback = self._on_conversation_changed

    def ensure_name(self):
        """Ensure session has a name for display/persistence.
        
        If metadata doesn't have a 'name' key, generates a default name
        based on creation timestamp.
        """
        name = self.metadata.get('name', '')
        if not name or not str(name).strip():
            from datetime import datetime
            # Use created_at if available, otherwise current time
            if isinstance(self.created_at, datetime):
                timestamp = self.created_at
            else:
                timestamp = datetime.now()
            self.metadata['name'] = f"Session {timestamp:%Y-%m-%d %H:%M}"
    @staticmethod
    def _normalize_conversation_for_hash(conversation: List[Dict[str, Any]]) -> str:
        """Create normalized JSON representation for consistent hashing.
        
        Strips transient fields and ensures consistent ordering for stable hashing.
        """
        from .utils import normalize_conversation_for_hash as normalize
        return normalize(conversation)
    def _on_conversation_changed(self):
        """Called when user_history is mutated."""
        if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
            import sys
            sys.stderr.write(f'[Session] _on_conversation_changed called, session_id={self.session_id}, callbacks={len(self._conversation_changed_callbacks)}\n')
        self.updated_at = datetime.now()
        self._conversation_version += 1
        # Update conversation hash
        try:
            conv_str = self._normalize_conversation_for_hash(self.user_history)
            self.conversation_hash = hashlib.md5(conv_str.encode()).hexdigest()[:8]
        except Exception:
            self.conversation_hash = ""
        for callback in self._conversation_changed_callbacks:
            try:
                callback()
            except Exception as e:
                # Log but don't break
                import traceback
                traceback.print_exc()

    def connect_conversation_changed(self, callback):
        """Register a callback to be invoked when user_history changes."""
        self._conversation_changed_callbacks.append(callback)

    def disconnect_conversation_changed(self, callback):
        """Remove a previously registered callback."""
        if callback in self._conversation_changed_callbacks:
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
                raise ValueError(f"Unknown runtime parameter: {key}")
        self.updated_at = datetime.now()

    def to_persistable_dict(self) -> Dict[str, Any]:
        """
        Convert session to a dictionary suitable for JSON serialization.
        Excludes non-persistable fields like agent_context (derived) and objects.
        """
        data = {
            'session_id': self.session_id,
            'created_at': self.created_at.isoformat(),
            'updated_at': datetime.now().isoformat(),
            'config': self.config.to_dict(),
            'runtime_params': self.runtime_params.to_dict(),
            'user_history': list(self.user_history),  # Convert ObservableList to plain list
            'containers': [c.to_dict() for c in self.containers],
            'preset_name': self.preset_name,
            'metadata': self.metadata,
            'version': self.version,
            'final_content': self.final_content,
            'final_reasoning': self.final_reasoning,
            'summary': self.summary,
            'total_input_tokens': self.total_input_tokens,
            'total_output_tokens': self.total_output_tokens,
            'context_length': self.context_length,
        }
        return data

    @classmethod
    def from_persistable_dict(cls, data: Dict[str, Any]) -> 'Session':
        """Reconstruct a Session from a persisted dictionary."""
        # Parse timestamps
        created_at = datetime.fromisoformat(data.get('created_at', '')) if data.get('created_at') else datetime.now()
        updated_at = datetime.fromisoformat(data.get('updated_at', '')) if data.get('updated_at') else datetime.now()

        # Build nested objects
        config_data = data.get('config', {})
        config = SessionConfig.from_dict(config_data) if config_data else SessionConfig()

        runtime_params_data = data.get('runtime_params', {})
        runtime_params = RuntimeParams.from_dict(runtime_params_data) if runtime_params_data else RuntimeParams()

        user_history = data.get('user_history', [])
        containers_data = data.get('containers', [])
        containers = [ContainerMetadata.from_dict(c) for c in containers_data]

        metadata = data.get('metadata', {})
        version = data.get('version', 1)
        final_content = data.get('final_content')
        final_reasoning = data.get('final_reasoning')

        session = cls(
            session_id=str(data.get('session_id', str(uuid.uuid4()))),
            created_at=created_at,
            updated_at=updated_at,
            config=config,
            runtime_params=runtime_params,
            user_history=user_history,
            containers=containers,
            preset_name=data.get('preset_name'),
            metadata=metadata,
            version=version,
            final_content=final_content,
            final_reasoning=final_reasoning,
            summary=data.get('summary'),
            total_input_tokens=data.get('total_input_tokens', 0),
            total_output_tokens=data.get('total_output_tokens', 0),
            context_length=data.get('context_length', 0),
        )
        # agent_context will be built later by ContextBuilder
        return session

    def add_message(self, role: str, content: str, **kwargs) -> None:
        """
        Add a message to the session's user_history.
        Automatically updates the updated_at timestamp.
        """
        message = {"role": role, "content": content, **kwargs}
        self.user_history.append(message)
        self.updated_at = datetime.now()

    def create_agent(self, config):
        """
        Create an Agent instance associated with this session.
        The agent will use this session's user_history as its conversation source.
        """
        # Import here to avoid circular dependencies
        from agent import Agent
        # Create the agent with this session and the provided config.
        # Agent now accepts a session parameter and uses session.user_history directly.
        agent = Agent(config, session=self)
        self.agent_instance = agent
        return agent

    def get_history_subset(self, max_tokens: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Build a context subset from the user_history using the configured ContextBuilder.
        Returns a list of messages suitable for sending to the LLM.
        """
        from session.context_builder import LastNBuilder
        # For now, use LastNBuilder with effectively unlimited keep_last_messages
        # to preserve full session history during processing (pruning only by max_tokens)
        builder = LastNBuilder(keep_last_messages=100000, keep_system_prompt=True)
        return builder.build(self.user_history, max_tokens=max_tokens)
