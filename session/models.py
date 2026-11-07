"""
Session management data models.

Defines the core concepts:
- RuntimeParams: Mutable parameters for LLM generation (temperature, top_p)
- ContainerMetadata: Metadata for session-scoped containers (not the live objects)
- Session: The atomic conversation unit containing all state.

Backward compatibility: Old sessions with 'config' key are migrated to metadata['agent_config'].
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import List, Dict, Any, Optional
import uuid, hashlib, json, os, threading
from typing import Any
from thoughtmachine.security import merge_security_config, get_default_security_config
from agent.logging import log
from agent.core.message import Message

# Sentinel attribute name marking that a Session instance has finished
# __post_init__. Its ABSENCE means "still under construction": a plain-list wrap
# performed during construction is expected and logged at DEBUG, whereas a live
# (post-construction) wrap keeps the original WARNING.
_SESSION_INITIALISED_FLAG = '_tm_session_initialised'

# Addendum schema defaults (brief §4.1, Path A; rulings R2/R3). The addendum is
# a first-class session field -- a sibling of `summary` -- with the shape
# {"entries": [...], "budget_chars": int, "max_entries": int}.
# `budget_chars` is an exact character count (R2: no estimator); `max_entries`
# caps the entry count separately at 64 (R3).
DEFAULT_ADDENDUM_BUDGET_CHARS = 8000
DEFAULT_ADDENDUM_MAX_ENTRIES = 64
# R4: `tool` is NOT a legal source -- C2 forbids the model writing its own
# memory, so no entry in this wave could legitimately carry it.
ADDENDUM_SOURCES = ('operator', 'mailbox')

class ObservableList(list):
    """A list that notifies a callback when mutated."""

    def __init__(self, iterable=(), callback=None):
        try:
            super().__init__(iterable)
        except Exception as e:
            log('ERROR', 'core.history', f'[ObservableList.__init__] ERROR during initialization: {e}')
            raise
        self.callback = callback
        self._lock = threading.Lock()

    def _notify(self):
        with self._lock:
            if self.callback:
                self.callback()

    def __setitem__(self, key, value):
        with self._lock:
            super().__setitem__(key, value)
        self._notify()

    def __delitem__(self, key):
        with self._lock:
            super().__delitem__(key)
        self._notify()

    def append(self, item):
        with self._lock:
            super().append(item)
        self._notify()

    def extend(self, iterable):
        with self._lock:
            super().extend(iterable)
        self._notify()

    def insert(self, index, item):
        with self._lock:
            super().insert(index, item)
        self._notify()

    def pop(self, index=-1):
        with self._lock:
            result = super().pop(index)
        self._notify()
        return result

    def remove(self, item):
        with self._lock:
            super().remove(item)
        self._notify()

    def clear(self):
        with self._lock:
            super().clear()
        self._notify()

    def __iadd__(self, other):
        with self._lock:
            result = super().__iadd__(other)
        self._notify()
        return result

    def __imul__(self, other):
        with self._lock:
            result = super().__imul__(other)
        self._notify()
        return result

    def sort(self, *, key=None, reverse=False):
        with self._lock:
            super().sort(key=key, reverse=reverse)
        self._notify()

    def reverse(self):
        with self._lock:
            super().reverse()
        self._notify()

@dataclass
class RuntimeParams:
    """Mutable runtime parameters that can be adjusted during a session."""
    temperature: float = 0.2
    top_p: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary, excluding None values."""
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'RuntimeParams':
        """Create from dictionary."""
        return cls(**data)



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


def _default_addendum() -> Dict[str, Any]:
    """A fresh, empty addendum in its canonical default state."""
    return {
        'entries': [],
        'budget_chars': DEFAULT_ADDENDUM_BUDGET_CHARS,
        'max_entries': DEFAULT_ADDENDUM_MAX_ENTRIES,
    }


def _normalize_addendum_entry(entry: Any) -> Optional[Dict[str, Any]]:
    """Normalise one addendum entry, or return None when it is not legal.

    A legal entry carries `id` (non-empty str), `source` in ADDENDUM_SOURCES and
    `text` (str); `ts` defaults to '' and `pinned` is coerced to bool. Unknown
    keys are dropped, which is what makes a later version's extra field
    harmless on read.
    """
    if not isinstance(entry, dict):
        return None
    entry_id = entry.get('id')
    source = entry.get('source')
    text = entry.get('text')
    if not isinstance(entry_id, str) or not entry_id:
        return None
    if source not in ADDENDUM_SOURCES:
        return None
    if not isinstance(text, str):
        return None
    ts = entry.get('ts', '')
    return {
        'id': entry_id,
        'ts': ts if isinstance(ts, str) else str(ts),
        'source': source,
        'text': text,
        'pinned': bool(entry.get('pinned', False)),
    }


def normalize_addendum(raw: Any) -> Dict[str, Any]:
    """Coerce a persisted (or partial) addendum into the canonical shape.

    Missing keys are defaulted rather than rejected, so a session file written
    before the field existed loads as an empty addendum. An entry that fails
    validation is DROPPED with a WARNING and never raised: the store's
    load_session treats any exception here as a failed load, and one malformed
    entry must not cost the whole session.
    """
    addendum = _default_addendum()
    if raw is None:
        return addendum
    if not isinstance(raw, dict):
        log('WARNING', 'core.session',
            f'[addendum] ignoring non-dict addendum of type {type(raw).__name__}')
        return addendum

    raw_entries = raw.get('entries', [])
    if isinstance(raw_entries, list):
        for raw_entry in raw_entries:
            entry = _normalize_addendum_entry(raw_entry)
            if entry is None:
                log('WARNING', 'core.session',
                    f'[addendum] dropping invalid entry on load: {raw_entry!r}')
                continue
            addendum['entries'].append(entry)
    elif raw_entries is not None:
        log('WARNING', 'core.session',
            f"[addendum] ignoring non-list 'entries' of type {type(raw_entries).__name__}")

    budget_chars = raw.get('budget_chars', DEFAULT_ADDENDUM_BUDGET_CHARS)
    if isinstance(budget_chars, int) and not isinstance(budget_chars, bool):
        addendum['budget_chars'] = budget_chars
    else:
        log('WARNING', 'core.session',
            f'[addendum] budget_chars={budget_chars!r} is not an int; '
            f'using {DEFAULT_ADDENDUM_BUDGET_CHARS}')

    max_entries = raw.get('max_entries', DEFAULT_ADDENDUM_MAX_ENTRIES)
    if isinstance(max_entries, int) and not isinstance(max_entries, bool):
        addendum['max_entries'] = max_entries
    else:
        log('WARNING', 'core.session',
            f'[addendum] max_entries={max_entries!r} is not an int; '
            f'using {DEFAULT_ADDENDUM_MAX_ENTRIES}')

    return addendum


@dataclass
class Session:
    """An atomic conversation unit with all its state."""
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

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
    addendum: Dict[str, Any] = field(default_factory=_default_addendum)
    agent_instance: Optional[Any] = field(default=None, compare=False, repr=False)
    workspace_id: Optional[str] = field(default=None, compare=False)
    mode: str = "agent"  # "agent", "engineer", or "custom"
    last_active: str = ""  # ISO datetime string, updated on each use
    metadata: Dict[str, Any] = field(default_factory=dict)
    security_config: Dict[str, Any] = field(default_factory=get_default_security_config)
    _conversation_changed_callbacks: List[Any] = field(default_factory=list, compare=False, repr=False)
    _conversation_version: int = field(default=0, compare=False, repr=False)
    conversation_hash: str = field(default='', compare=False, repr=False)

    def __setattr__(self, name: str, value: Any) -> None:
        # Intercept assignment to user_history to ensure it always stays ObservableList.
        if name == 'user_history':
            if not isinstance(value, ObservableList):
                if getattr(self, _SESSION_INITIALISED_FLAG, False):
                    # Live (post-construction) assignment: keep the original WARNING.
                    log('WARNING', 'core.history', f"Plain list assignment to user_history intercepted — auto-wrapped to ObservableList")
                else:
                    # Construction-time wrap is expected: log at DEBUG (original text kept for greps).
                    log('DEBUG', 'core.history', f"Construction-time plain list assignment to user_history intercepted — auto-wrapped to ObservableList")
                # Wrap plain list back into ObservableList, preserving callback.
                new_list = ObservableList(list(value), callback=self._on_conversation_changed)
                object.__setattr__(self, name, new_list)
                # A live wholesale replacement bypasses ObservableList's mutation callback, so announce it now.
                if getattr(self, _SESSION_INITIALISED_FLAG, False):
                    self._on_conversation_changed()
                return
        # Guard workspace_id from being changed once set (immutable after initial assignment).
        if name == 'workspace_id':
            try:
                current = object.__getattribute__(self, 'workspace_id')
            except AttributeError:
                current = None
            if current is not None and value != current:
                raise AttributeError("workspace_id is immutable once set")
        # Default behavior for all other attributes.
        super().__setattr__(name, value)

    def __post_init__(self):
        # context_length stays as provided (0 = unknown);
        # total_input_tokens + total_output_tokens is the cumulative total,
        # NOT the current conversation's context length, so do NOT use it as fallback.
        self._wrap_user_history()
        object.__setattr__(self, _SESSION_INITIALISED_FLAG, True)
        self.ensure_name()
        try:
            conv_str = self._normalize_conversation_for_hash(self.user_history)
            self.conversation_hash = hashlib.md5(conv_str.encode()).hexdigest()[:8]
        except Exception:
            self.conversation_hash = ''

    def _wrap_user_history(self):
        """Wrap user_history with ObservableList if not already wrapped."""
        log('DEBUG', 'core.history', f"wrapping user_history: is_ObservableList={isinstance(self.user_history, ObservableList)}")
        if not isinstance(self.user_history, ObservableList):
            log('DEBUG', 'core.history', f'[Session] _wrap_user_history: Creating ObservableList from current user_history')
            new_list = ObservableList(self.user_history, callback=self._on_conversation_changed)
            log('DEBUG', 'core.history', f'[Session] _wrap_user_history: Created ObservableList, id={id(new_list)}, len={len(new_list)}')
            self.user_history = new_list
        else:
            self.user_history.callback = self._on_conversation_changed
            log('DEBUG', 'core.history', f'[Session] _wrap_user_history: Already ObservableList, ensuring session callback, id={id(self.user_history)}, len={len(self.user_history)}')
        callback_repr = self.user_history.callback.__qualname__ if self.user_history.callback else None
        log('DEBUG', 'core.history', f'[Session] _wrap_user_history: after, len={len(self.user_history)}, id={id(self.user_history)}, callback={callback_repr}')

    def _get_next_seq(self) -> int:
        """Return the next sequence number and increment the counter."""
        seq = self.next_seq
        self.next_seq += 1
        return seq

    def ensure_name(self):
        """Ensure session has a name for display/persistence.
        
        If metadata doesn't have a 'name' key, generates a default name
        based on creation timestamp and session mode.
        """
        name = self.metadata.get('name', '')
        if not name or not str(name).strip():
            from datetime import datetime
            if isinstance(self.created_at, datetime):
                timestamp = self.created_at
            else:
                timestamp = datetime.now()
            mode_prefix = {
                "agent": "Agent Session",
                "engineer": "Engineer Session",
                "custom": "Custom Session",
            }.get(self.mode, "Session")
            self.metadata['name'] = f'{mode_prefix} {timestamp:%Y-%m-%d %H:%M}'

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
        old_version = self._conversation_version
        log('DEBUG', 'core.history', f"conversation version: {old_version} → {old_version + 1}, hash={self.conversation_hash}, callbacks={len(self._conversation_changed_callbacks)}")
        for i, cb in enumerate(self._conversation_changed_callbacks):
            cb_repr = cb.__qualname__ if hasattr(cb, '__qualname__') else repr(cb)
            log('DEBUG', 'core.session', f'  Callback {i}: {cb_repr}')
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
                log('DEBUG', 'core.session', f'invoking callback {callback_repr}')
                callback()
            except Exception as e:
                import traceback
                log('ERROR', 'core.session', f'callback error: {e}')
                log('ERROR', 'core.session', f'[SESSION] _on_conversation_changed callback error')

    def connect_conversation_changed(self, callback):
        """Register a callback to be invoked when user_history changes."""
        log('DEBUG', 'core.session', f'[SESSION] connect_conversation_changed: adding callback, total {len(self._conversation_changed_callbacks) + 1}')
        self._conversation_changed_callbacks.append(callback)

    def disconnect_conversation_changed(self, callback):
        """Remove a previously registered callback."""
        log('DEBUG', 'core.session', f'[SESSION] disconnect_conversation_changed: looking for callback {callback}, total callbacks {len(self._conversation_changed_callbacks)}')
        if callback in self._conversation_changed_callbacks:
            log('DEBUG', 'core.session', f'[SESSION] disconnect_conversation_changed: removing callback, remaining {len(self._conversation_changed_callbacks) - 1}')
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

    def to_dict(self) -> Dict[str, Any]:
        """Alias for to_persistable_dict — convenience for test usage."""
        return self.to_persistable_dict()

    def to_persistable_dict(self) -> Dict[str, Any]:
        """
        Convert session to a dictionary suitable for JSON serialization.
        Excludes non-persistable fields like agent_context (derived) and objects.
        """
        data = {'session_id': self.session_id, 'created_at': self.created_at.isoformat(), 'updated_at': datetime.now().isoformat(), 'user_history': list(self.user_history), 'containers': [c.to_dict() for c in self.containers], 'preset_name': self.preset_name, 'workspace_id': self.workspace_id, 'mode': self.mode, 'last_active': self.last_active, 'metadata': self.metadata, 'security_config': self.security_config, 'version': self.version, 'summary': self.summary, 'addendum': self.addendum, 'total_input_tokens': self.total_input_tokens, 'total_output_tokens': self.total_output_tokens, 'context_length': self.context_length}
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Session':
        """Alias for from_persistable_dict — convenience for test usage."""
        return cls.from_persistable_dict(data)

    @classmethod
    def from_persistable_dict(cls, data: Dict[str, Any]) -> 'Session':
        """Reconstruct a Session from a persisted dictionary."""
        created_at = datetime.fromisoformat(data.get('created_at', '')) if data.get('created_at') else datetime.now()
        updated_at = datetime.fromisoformat(data.get('updated_at', '')) if data.get('updated_at') else datetime.now()
        # Backward compat: migrate old 'config' key to metadata['agent_config']
        config_data = data.pop('config', None)
        metadata = data.get('metadata', {})
        if config_data and 'agent_config' not in metadata:
            if isinstance(config_data, dict):
                metadata['agent_config'] = config_data

        # Derive runtime params from metadata.agent_config (sole source of truth)
        agent_cfg = metadata.get('agent_config', {})
        runtime_params = RuntimeParams(
            temperature=agent_cfg.get('temperature', 0.2),
        )
        user_history = data.get('user_history', [])
        # Ensure all messages are Message objects so is_system_notification
        # is derived from role+content (never from content-substring matching).
        from agent.core.message import Message
        user_history = [Message(m) if not isinstance(m, Message) else m for m in user_history]
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
        security_config_data = data.get('security_config', {})
        security_config = merge_security_config(security_config_data)

        version = data.get('version', 1)
        # Backward compat: mode and last_active (legacy sessions may lack these)
        mode = data.get('mode', 'agent')
        last_active = data.get('last_active', '')
        session = cls(session_id=str(data.get('session_id', str(uuid.uuid4()))), created_at=created_at, updated_at=updated_at, runtime_params=runtime_params, user_history=user_history, containers=containers, preset_name=data.get('preset_name'), workspace_id=data.get('workspace_id'), mode=mode, last_active=last_active, metadata=metadata, security_config=security_config, version=version, summary=data.get('summary'), addendum=normalize_addendum(data.get('addendum')), total_input_tokens=data.get('total_input_tokens', 0), total_output_tokens=data.get('total_output_tokens', 0), next_seq=next_seq_value, context_length=data.get('context_length', 0))
        return session

    def update_from_persistable_dict(self, data: Dict[str, Any]) -> None:
        """Update this session's data from a persistable dict (in-place, preserves callbacks)."""
        old_history = self.user_history
        created_at = datetime.fromisoformat(data.get('created_at', '')) if data.get('created_at') else datetime.now()
        updated_at = datetime.fromisoformat(data.get('updated_at', '')) if data.get('updated_at') else datetime.now()
        # Backward compat: migrate old 'config' key to metadata['agent_config']
        config_data = data.pop('config', None)
        metadata = data.get('metadata', {})
        if config_data and 'agent_config' not in metadata:
            if isinstance(config_data, dict):
                metadata['agent_config'] = config_data
        # Derive runtime params from metadata.agent_config (sole source of truth)
        agent_cfg = metadata.get('agent_config', {})
        runtime_params = RuntimeParams(
            temperature=agent_cfg.get('temperature', 0.2),
        )
        user_history = data.get('user_history', [])
        # Ensure all messages are Message objects so is_system_notification
        # is derived from role+content (never from content-substring matching).
        from agent.core.message import Message
        user_history = [Message(m) if not isinstance(m, Message) else m for m in user_history]
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
        security_config_data = data.get('security_config', {})
        security_config = merge_security_config(security_config_data)

        version = data.get('version', 1)
        self.mode = data.get('mode', self.mode)
        self.last_active = data.get('last_active', self.last_active)
        self.session_id = str(data.get('session_id', str(uuid.uuid4())))
        self.created_at = created_at
        self.updated_at = updated_at
        self.runtime_params = runtime_params
        old_history.clear()
        old_history.extend(user_history)
        self.containers = containers
        self.preset_name = data.get('preset_name')
        self.workspace_id = data.get('workspace_id')
        self.metadata = metadata
        self.security_config = security_config
        self.version = version
        self.summary = data.get('summary')
        self.addendum = normalize_addendum(data.get('addendum'))
        self.total_input_tokens = data.get('total_input_tokens', 0)
        self.total_output_tokens = data.get('total_output_tokens', 0)
        self.next_seq = next_seq_value
        self.context_length = data.get('context_length', 0)
        self.agent_context = []

    def rebind_workspace(self, workspace_id: Optional[str], *, reason: str = "") -> Optional[str]:
        """Move this session to a different workspace, returning the previous one.

        This is the ONLY sanctioned way to move a session between workspaces.
        ``workspace_id`` is immutable once stamped (see ``__setattr__``), which
        protects the store from non-owner writes; but the OWNER of a session
        (the server, when a fresh tab applies a config that switches the active
        workspace) legitimately needs the record to follow the tab.  The
        immutability guard stays in place for every other caller: this method
        is a single audited escape hatch, it logs the move, and it never
        touches the filesystem -- the caller saves and deletes afterwards.
        """
        current = self.workspace_id
        if workspace_id == current:
            return current
        log(
            'INFO',
            'core.session',
            f"Session {self.session_id} workspace rebind: {current!r} -> {workspace_id!r} "
            f"(reason={reason or 'unspecified'})",
        )
        object.__setattr__(self, 'workspace_id', workspace_id)
        return current

    def add_message(self, role: str, content: str, **kwargs) -> None:
        """
        Add a message to the session's user_history.
        Automatically updates the updated_at timestamp.
        """
        message = Message(role=role, content=content, **kwargs)
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