"""
Lightweight dict subclass.

``is_system_notification`` is a regular dict key set explicitly at injection
sites.  No content-based derivation — a user typing ``[SYSTEM NOTIFICATION]``
in their message will **not** be treated as a system notification.

Because ``Message`` is a ``dict`` subclass, it is JSON-serializable and can be
used anywhere a plain message dict is expected.
"""

import logging
import uuid

logger = logging.getLogger(__name__)

SYSTEM_NOTIFICATION_PREFIX: str = '[SYSTEM NOTIFICATION]'

# Second, distinctly-prefixed system family: notices tell the model what just
# happened to its own turn (paused, stopped).  Rendered as
# ``[System Notice: <text>]`` -- note the closing bracket the helper adds.
SYSTEM_NOTICE_PREFIX: str = '[System Notice:'


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


def make_system_notice(text: str, *, kind: str, **metadata) -> Message:
    """Build a ``[System Notice: <text>]`` message (role ``user``).

    ``kind`` names what happened to the turn (``'pause_nudge'``,
    ``'stop_notice'``); any extra keyword becomes an extra metadata key
    alongside the generated ``warning_id``, the ``system_notice`` marker and
    the ``kind``.  Like every system message it carries
    ``is_system_notification=True``, so it stays out of the model-facing
    history pruning and renders as a system bubble.
    """
    return Message(
        role='user',
        content=f'{SYSTEM_NOTICE_PREFIX} {text}]',
        is_system_notification=True,
        metadata={'warning_id': str(uuid.uuid4()), 'system_notice': True, 'kind': kind, **metadata},
    )


def is_system_notice_text(text: str) -> bool:
    """True when ``text`` opens a system message of EITHER family."""
    return text.startswith(SYSTEM_NOTIFICATION_PREFIX) or text.startswith(SYSTEM_NOTICE_PREFIX)
