"""
EventForwarder — Pure callback registry and message routing for WebAgentBridge.

Maintains a multi-key callback registry so that multiple WebSocket connections
can receive the same event stream.  < 100 lines of pure message routing —
no event mapping logic.

Architecture
────────────
    bridge._map_and_emit()  ──(type, data)──▶  forwarder.broadcast(type, data)
                                                    │
                                          callback₁  callback₂  callback₃ …
                                                    │
                                              WebSocket.send()
"""

from __future__ import annotations

import traceback
from typing import Any, Callable, Dict


# Global registry of active tab bridges — used by broadcast_rename and
# broadcast_logging_config to reach across all open tabs holding the same session.
_active_tab_bridges: set = set()


class EventForwarder:
    """
    Manages event callbacks and provides broadcast helpers.

    * ``_callbacks`` — ``Dict[int, Callable]`` keyed by WebSocket id.
    * ``register_websocket(key, callback)`` — register a callback by ws id.
    * ``unregister_websocket(key)`` — remove a callback by ws id.
    * ``send_personal(ws_key, event_type, data)`` — send to one callback.
    * ``broadcast(session_id, event_type, data)`` — send to all callbacks.
    * ``broadcast_rename`` / ``broadcast_logging_config`` — static cross-tab broadcasts.
    """

    def __init__(
        self,
        owner: Any,
        event_callback: Callable[[Dict[str, Any]], None] | None = None,
    ) -> None:
        self._owner = owner
        self._callbacks: Dict[int, Callable] = {}
        if event_callback is not None:
            self._callbacks[id(event_callback)] = event_callback

    # ── Callback management ──────────────────────────────────────────────────

    def register_websocket(
        self,
        key: int | None,
        callback: Callable[[Dict[str, Any]], None],
    ) -> None:
        """Register an event callback by WebSocket id.  If *key* is None, replaces all callbacks."""
        if key is None:
            self._callbacks.clear()
            self._callbacks[id(callback)] = callback
        else:
            self._callbacks[key] = callback

    def unregister_websocket(self, key: int) -> None:
        """Remove a previously registered callback by its WebSocket id."""
        self._callbacks.pop(key, None)

    # ── Event emission ───────────────────────────────────────────────────────

    def send_personal(
        self,
        ws_key: int,
        event_type: str,
        data: Dict[str, Any],
    ) -> None:
        """Send an event to a single registered callback by WebSocket key."""
        cb = self._callbacks.get(ws_key)
        if cb is not None:
            try:
                cb({"type": event_type, **data})
            except Exception:
                traceback.print_exc()

    def broadcast(
        self,
        _session_id: str,
        event_type: str,
        data: Dict[str, Any],
    ) -> None:
        """Broadcast an event to every registered callback."""
        for cb in list(self._callbacks.values()):
            if cb is not None:
                try:
                    cb({"type": event_type, **data})
                except Exception:
                    traceback.print_exc()

    # ── Cross‑tab broadcasts (static) ────────────────────────────────────────

    @staticmethod
    def broadcast_rename(session_id: str, new_name: str) -> None:
        """Update in-memory session name on every bridge that has this session loaded."""
        for b in list(_active_tab_bridges):
            try:
                if b._loaded_session and b._loaded_session.session_id == session_id:
                    b._loaded_session.metadata["name"] = new_name
                if b._session and b._session.session_id == session_id:
                    b._session.metadata["name"] = new_name
            except Exception:
                pass

    @staticmethod
    def broadcast_logging_config(config: Dict[str, Any]) -> None:
        """Emit a logging_config_changed event to every active tab bridge."""
        for b in list(_active_tab_bridges):
            try:
                b._forwarder.broadcast("", "logging_config_changed", {"config": config})
            except Exception:
                pass
