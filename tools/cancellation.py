"""Per-THREAD turn-cancellation primitives (no process-global state).

``WorkerThread`` binds its own ``_turn_cancel`` event for the duration of a
turn, so nothing here is shared between workers or between threads: a cancel
raised by one worker can never reach another worker's tools.

Consumers import these names from ``tools.sleep_tool`` (which re-exports
them) or from here directly.
"""

from __future__ import annotations

import threading
from typing import Optional

# Per-THREAD cancellation holder.
_tls = threading.local()


def bind_turn_cancel(event: threading.Event) -> None:
    """Bind *event* as the cancellation token of the CURRENT thread."""
    _tls.event = event
    _tls.reason = None


def unbind() -> None:
    """Drop the current thread's binding (owning worker calls this at turn end)."""
    _tls.event = None
    _tls.reason = None


def _fallback_event() -> threading.Event:
    """Per-thread token used when no turn event is bound (tests, direct use)."""
    ev = getattr(_tls, "fallback", None)
    if ev is None:
        ev = threading.Event()
        _tls.fallback = ev
    return ev


def _active_event() -> threading.Event:
    """The current thread's cancellation token (bound turn event or fallback)."""
    ev = getattr(_tls, "event", None)
    return ev if ev is not None else _fallback_event()


def interrupt(reason: str = "interrupted") -> None:
    """Cancel the CURRENT thread's in-flight tool work (never crosses threads)."""
    _tls.reason = reason or "interrupted"
    _active_event().set()


def reset() -> None:
    """Clear the current thread's cancellation state (tests / direct use)."""
    _tls.reason = None
    _active_event().clear()


def is_interrupted() -> bool:
    """True while the CURRENT thread's cancellation token is set."""
    return _active_event().is_set()


def cancel_reason() -> Optional[str]:
    """Reason recorded by the last interrupt on this thread (else ``None``)."""
    return getattr(_tls, "reason", None)
