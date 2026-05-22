"""
Lightweight dict subclass with a derived *is_system_notification* property.

Eliminates the need to manually set ``is_system_notification: True`` at 15+
injection sites. The property is inferred from the message content and role,
so turn-boundary bugs caused by a missing flag are impossible.

Usage::

    msg = Message(role='user', content='[SYSTEM NOTIFICATION] ...')
    assert msg.is_system_notification is True   # inferred from content
    assert msg['is_system_notification'] is True  # bracket access works
    assert msg.get('is_system_notification') is True  # dict get works
    msg['is_system_notification'] = False  # logs warning, does nothing

Because ``Message`` is a ``dict`` subclass, it is JSON-serializable and can be
used anywhere a plain message dict is expected.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

SYSTEM_NOTIFICATION_PREFIX: str = '[SYSTEM NOTIFICATION]'


def _is_notification_role_and_content(role: Any, content: Any) -> bool:
    """Return True if *role* is ``'user'`` and *content* is a string starting with the prefix."""
    return role == 'user' and isinstance(content, str) and content.startswith(SYSTEM_NOTIFICATION_PREFIX)


class Message(dict):
    """Dict subclass that derives *is_system_notification* from ``role`` + ``content``.

    The flag is **never** stored as a real key.  It is computed on the fly:
    ``is_system_notification is True`` iff ``role == 'user'`` and ``content``
    starts with ``[SYSTEM NOTIFICATION]``.

    All normal dict operations work (``get``, ``__getitem__``, ``items``, ...).
    Setting ``msg['is_system_notification'] = ...`` logs a warning and is ignored.
    """

    # ── derived property ────────────────────────────────────────────────────

    @property
    def is_system_notification(self) -> bool:
        """Inferred from ``role == 'user'`` and ``content`` starting with ``[SYSTEM NOTIFICATION]``."""
        return _is_notification_role_and_content(
            self.get('role'),
            self.get('content'),
        )

    @is_system_notification.setter
    def is_system_notification(self, value: Any) -> None:
        """Read-only -- logs a warning and discards the value.

        Catches legacy code that still tries to set the flag manually.
        """
        if value is True:
            logger.warning(
                'Ignored attempt to set is_system_notification=True on a Message object. '
                'The flag is now derived from content/role.'
            )
        # Never actually store the key.

    # ── Override dict methods so ``is_system_notification`` is never stored ──

    def __getitem__(self, key: str) -> Any:
        if key == 'is_system_notification':
            return self.is_system_notification
        return super().__getitem__(key)

    def __setitem__(self, key: str, value: Any) -> None:
        if key == 'is_system_notification':
            self.is_system_notification = value  # uses the property setter
            return
        super().__setitem__(key, value)

    def __contains__(self, key: object) -> bool:
        if isinstance(key, str) and key == 'is_system_notification':
            return True
        return super().__contains__(key)

    def get(self, key: str, default: Any = None) -> Any:
        if key == 'is_system_notification':
            return self.is_system_notification
        return super().get(key, default)

    def pop(self, key: str, *args: Any) -> Any:
        if key == 'is_system_notification':
            raise KeyError('is_system_notification is a read-only derived property and cannot be popped')
        return super().pop(key, *args)

    def __delitem__(self, key: str) -> None:
        if key == 'is_system_notification':
            raise KeyError('is_system_notification is a read-only derived property and cannot be deleted')
        super().__delitem__(key)

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
        """Return a plain ``dict`` (not a ``Message``) with all real keys plus
        the derived ``is_system_notification`` value."""
        result = dict(self)
        result['is_system_notification'] = self.is_system_notification
        return result
