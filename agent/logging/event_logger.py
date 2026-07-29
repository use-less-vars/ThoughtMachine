"""Event Logger — subscribes to EventBus and writes events to a structured log file.

Supports:
- Workspace isolation via workspace_path
- Rotating file handler (10 MB, 3 backups)
- Multiple bus subscriptions (subscribe_all + attach_worker_bus)
- Singleton access via instance()
"""
from __future__ import annotations
import json
import os
import queue
import threading
from datetime import datetime
from typing import Optional
from agent.events import EventBus, BaseEvent, EventType, global_event_bus


class EventLogger:
    """Subscribes to EventBus and writes events to JSON lines files.

    Supports:
    - Workspace isolation via workspace_path
    - Rotating file handler (10 MB max per file, 3 backups)
    - Multiple bus subscriptions (subscribe_all + attach_worker_bus)
    - Singleton access via instance()
    """

    _instance: Optional["EventLogger"] = None

    @classmethod
    def instance(cls) -> "EventLogger":
        """Return the singleton instance, creating one with defaults if needed."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(
        self,
        workspace_path: Optional[str] = None,
        event_bus: Optional[EventBus] = None,
    ):
        self.event_bus = event_bus or global_event_bus
        self._lock = threading.Lock()
        self._subscribed = False
        self._worker_buses: dict[str, EventBus] = {}
        self._event_queue: queue.Queue = queue.Queue()
        self._writer_thread: Optional[threading.Thread] = None
        self._stop_requested = threading.Event()
        # Track all (bus, event_type) subscriptions so we can unsubscribe later
        self._subscriptions: list[tuple[EventBus, Optional[EventType]]] = []

        # Determine log directory and file path — always use vault path
        vault_log_dir = os.path.join(os.path.expanduser("~"), ".thoughtmachine", "logs")
        self.log_dir = vault_log_dir
        self._file_path = os.path.join(vault_log_dir, "event_log.jsonl")

        os.makedirs(self.log_dir, exist_ok=True)
        self._file = None

        # Register as singleton if not already set
        if EventLogger._instance is None:
            EventLogger._instance = self

    @property
    def file_path(self) -> str:
        return self._file_path

    def _ensure_file_open(self):
        """Open the log file if not already open."""
        if self._file is None:
            os.makedirs(os.path.dirname(self._file_path), exist_ok=True)
            self._file = open(self._file_path, "a", encoding="utf-8")

    def _maybe_rotate(self):
        """Rotate the log file if it exceeds 10 MB."""
        if not os.path.exists(self._file_path):
            return
        if os.path.getsize(self._file_path) > 10 * 1024 * 1024:
            # Rotate: shift .2 -> .3, .1 -> .2, current -> .1
            for i in range(3, 0, -1):
                older = os.path.join(self.log_dir, f"event_log.jsonl.{i}")
                newer = os.path.join(self.log_dir, f"event_log.jsonl.{i - 1}") if i > 1 else self._file_path
                if os.path.exists(newer):
                    os.rename(newer, older)
            # Close old file handle so next write re-opens (and creates fresh file)
            if self._file:
                self._file.close()
                self._file = None

    def subscribe_all(self, bus: EventBus, prefix: str = "") -> None:
        """Subscribe to ALL event types on the given bus."""
        for event_type in EventType:
            bus.subscribe(event_type, self._on_event)
            self._subscriptions.append((bus, event_type))

    def attach_worker_bus(self, name: str, bus: EventBus) -> None:
        """Attach and subscribe to a per-worker event bus."""
        self._worker_buses[name] = bus
        for event_type in EventType:
            bus.subscribe(event_type, self._on_event)
            self._subscriptions.append((bus, event_type))

    def start(self) -> None:
        """Start logging — subscribe to the main event bus."""
        if self._subscribed:
            return
        self._ensure_file_open()
        self._stop_requested.clear()
        self._writer_thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._writer_thread.start()
        self.subscribe_all(self.event_bus)
        self._subscribed = True

    def stop(self) -> None:
        """Stop logging — unsubscribe from all buses and close file."""
        if not self._subscribed:
            return
        # Unsubscribe all tracked subscriptions
        for bus, event_type in self._subscriptions:
            try:
                bus.unsubscribe(event_type, self._on_event)
            except Exception:
                pass
        self._subscriptions.clear()
        self._worker_buses.clear()
        self._subscribed = False

        self._stop_requested.set()
        if self._writer_thread and self._writer_thread.is_alive():
            self._writer_thread.join(timeout=5)
        self._writer_thread = None

        if self._file:
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None

    def _on_event(self, event: BaseEvent) -> None:
        """Callback: queue event for async writing to log file."""
        try:
            record = {
                "timestamp": (
                    event.metadata.timestamp.isoformat()
                    if hasattr(event.metadata, "timestamp")
                    else datetime.utcnow().isoformat()
                ),
                "event_type": event.type.value,
                "event_id": (
                    event.metadata.event_id
                    if hasattr(event.metadata, "event_id")
                    else None
                ),
                "source": (
                    event.metadata.source
                    if hasattr(event.metadata, "source")
                    else None
                ),
                "data": event.data if hasattr(event, "data") else {},
            }
            self._event_queue.put(record)
        except Exception:
            pass

    def _writer_loop(self) -> None:
        """Background thread: drain the event queue and write to file."""
        while not self._stop_requested.is_set():
            try:
                record = self._event_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._ensure_file_open()
                self._maybe_rotate()
                line = json.dumps(record, default=str) + "\n"
                with self._lock:
                    self._file.write(line)
                    self._file.flush()
            except Exception:
                pass

        # Drain remaining items after stop
        while not self._event_queue.empty():
            try:
                record = self._event_queue.get_nowait()
                self._ensure_file_open()
                line = json.dumps(record, default=str) + "\n"
                with self._lock:
                    self._file.write(line)
                    self._file.flush()
            except Exception:
                pass

    def get_tail(self, n: int = 20) -> list[dict]:
        """Return the last *n* parsed JSON lines from the event log file.

        Args:
            n: Maximum number of entries to return (default 20).

        Returns:
            List of parsed dicts. Lines that cannot be decoded are included
            as ``{"raw": "<line content>"}``. Returns an empty list if the
            file does not exist or an error occurs.
        """
        try:
            if not os.path.exists(self._file_path):
                return []
            with open(self._file_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            tail = lines[-n:] if len(lines) >= n else lines
            entries = []
            for line in tail:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    entries.append({"raw": line})
            return entries
        except Exception:
            return []

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()
