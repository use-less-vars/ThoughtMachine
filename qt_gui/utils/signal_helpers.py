"""Signal connection helpers for PyQt6."""
from typing import Callable, Any
from PyQt6.QtCore import QObject
from agent.logging import log

def connect_signal(signal: Any, slot: Callable) -> None:
    """Connect a Qt signal to a slot with basic error handling.
    
    Args:
        signal: Qt signal to connect.
        slot: Callable to invoke when signal is emitted.
    """
    try:
        signal.connect(slot)
    except Exception as e:
        log('DEBUG', 'debug.unknown', f'[signal_helpers] Error connecting signal: {e}')

def disconnect_all(signal: Any, *slots) -> None:
    """Disconnect multiple slots from a signal.
    
    Args:
        signal: Qt signal.
        *slots: Slot callables to disconnect.
    """
    for slot in slots:
        try:
            signal.disconnect(slot)
        except Exception:
            pass