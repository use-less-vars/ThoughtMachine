"""
Lightweight dict subclass.

``is_system_notification`` is a regular dict key set explicitly at injection
sites.  No content-based derivation — a user typing ``[SYSTEM NOTIFICATION]``
in their message will **not** be treated as a system notification.

Because ``Message`` is a ``dict`` subclass, it is JSON-serializable and can be
used anywhere a plain message dict is expected.
"""

import logging

logger = logging.getLogger(__name__)

SYSTEM_NOTIFICATION_PREFIX: str = '[SYSTEM NOTIFICATION]'


class Message(dict):
    """Dict subclass that stores ``is_system_notification`` as a regular dict key.

    No content-based derivation — the flag must be set explicitly.
    """

    def __repr__(self) -> str:
        inner = ', '.join(f'{k!r}: {v!r}' for k, v in super().items())
        return f'Message({{{inner}}})'

    def copy(self) -> 'Message':
        return Message(super().copy())

    @classmethod
    def from_dict(cls, d: dict) -> 'Message':
        """Create a Message from an existing dict (shallow copy)."""
        return cls(d)

    def to_dict(self) -> dict:
        """Return a plain ``dict`` (not a ``Message``) with all keys."""
        return dict(self)
