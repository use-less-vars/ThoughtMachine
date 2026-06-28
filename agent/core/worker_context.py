"""
worker_context.py — Lightweight Session surrogate for worker (sub-agent) loops.

Provides just the attributes that ``Agent.process_query()`` reads from the
Session object, without the full Session dataclass overhead (file I/O,
ObservableList wrapping, persistence callbacks, etc.).

Usage::

    ctx = WorkerContext(session_id="worker-abc-123")
    agent = Agent(config, session=ctx)
    for event in agent.process_query("review this commit"):
        ...

The WorkerContext exposes these Session-equivalent attributes:

  - session_id (str)
  - user_history (list[dict])
  - total_input_tokens (int)
  - total_output_tokens (int)
  - conversation_version (int) — property
  - conversation_hash (str)
  - summary (dict | None)
  - updated_at (datetime)
  - _get_next_seq() — method returning int
  - _on_conversation_changed() — no-op method
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional


class WorkerContext:
    """Lightweight Session stand-in for sub-agent worker loops.

    Provides exactly the attributes that ``Agent`` reads from a ``Session``
    object during ``process_query()``, with no persistence machinery.
    """

    def __init__(
        self,
        session_id: Optional[str] = None,
        user_history: Optional[List[Dict[str, Any]]] = None,
        total_input_tokens: int = 0,
        total_output_tokens: int = 0,
    ) -> None:
        self.session_id: str = session_id or f"worker-{uuid.uuid4().hex[:12]}"
        self.user_history: List[Dict[str, Any]] = user_history or []
        self.total_input_tokens: int = total_input_tokens
        self.total_output_tokens: int = total_output_tokens

        # Version tracking (mimics Session dataclass fields)
        self.summary: Optional[Dict[str, Any]] = None
        self.updated_at: datetime = datetime.now()
        self._conversation_version: int = 0
        self.conversation_hash: str = self._compute_hash()
        self._next_seq: int = 0

    # ── Session-like attributes ────────────────────────────────────────────

    @property
    def conversation_version(self) -> int:
        """Current conversation version (increments on each change)."""
        return self._conversation_version

    def _get_next_seq(self) -> int:
        """Return the next sequence number and increment."""
        seq = self._next_seq
        self._next_seq += 1
        return seq

    def _on_conversation_changed(self) -> None:
        """Called when user_history is mutated — no-op for worker context.

        In the real Session this increments ``_conversation_version``,
        recomputes the hash, and notifies callbacks.  WorkerContext
        does not need full conversation change tracking, but we still
        stub it to avoid AttributeError when Agent's conversation setter
        calls it (line 545 of agent.py).
        """
        self._conversation_version += 1
        self.conversation_hash = self._compute_hash()
        self.updated_at = datetime.now()

    # ── Internal helpers ──────────────────────────────────────────────────

    def _compute_hash(self) -> str:
        """Stable hash of the current user_history content."""
        import json

        try:
            normalized = json.dumps(self.user_history, sort_keys=True, default=str)
            return hashlib.md5(normalized.encode()).hexdigest()[:8]
        except Exception:
            return ""

    def __repr__(self) -> str:
        return (
            f"WorkerContext(session_id={self.session_id!r}, "
            f"messages={len(self.user_history)}, "
            f"input_tokens={self.total_input_tokens}, "
            f"output_tokens={self.total_output_tokens})"
        )
