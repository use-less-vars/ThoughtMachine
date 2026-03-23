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
import uuid


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
    # agent_context is not persisted; it's derived from user_history on load.
    # But we may keep a cached version during runtime.
    agent_context: List[Dict[str, Any]] = field(default_factory=list, compare=False, repr=False)
    containers: List[ContainerMetadata] = field(default_factory=list)
    preset_name: Optional[str] = field(default=None, compare=False)
    version: int = 1  # Session format version
    final_content: Optional[str] = None  # Content of the Final tool's result, if any
    final_reasoning: Optional[str] = None  # Reasoning that preceded the final answer

    metadata: Dict[str, Any] = field(default_factory=dict)  # name, tags, notes, etc.

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
            'user_history': self.user_history,
            'containers': [c.to_dict() for c in self.containers],
            'preset_name': self.preset_name,
            'metadata': self.metadata,
            'version': self.version,
            'final_content': self.final_content,
            'final_reasoning': self.final_reasoning,
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
        )
        # agent_context will be built later by ContextBuilder
        return session
