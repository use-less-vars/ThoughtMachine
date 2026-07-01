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
import json
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
        worker_name: Optional[str] = None,
        turn_count: int = 0,
    ) -> None:
        self.session_id: str = session_id or f"worker-{uuid.uuid4().hex[:12]}"
        self.worker_name: str = worker_name or f"worker-{uuid.uuid4().hex[:12]}"
        self.user_history: List[Dict[str, Any]] = user_history or []
        self.total_input_tokens: int = total_input_tokens
        self.total_output_tokens: int = total_output_tokens
        self.turn_count: int = turn_count

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

    # ── Token estimation ────────────────────────────────────────────

    def estimated_context_tokens(self) -> int:
        """
        Estimate the total token count of the current conversation using
        ``tiktoken`` with the ``cl100k_base`` encoding (OpenAI-compatible).

        Falls back to a rough character-based estimate (4 chars \u2248 1 token)
        if ``tiktoken`` is not available.

        Returns:
            Estimated token count (int).
        """
        import json
        try:
            import tiktoken
            encoder = tiktoken.get_encoding('cl100k_base')
            total = 0
            for msg in self.user_history:
                total += len(encoder.encode(json.dumps(msg)))
            return total
        except Exception:
            # Fallback: rough character-based estimate
            return sum(len(json.dumps(msg)) // 4 for msg in self.user_history)

    # ── Compaction after summarization ─────────────────

    def compact_after_summary(self) -> bool:
        """Remove old conversation messages that have been summarized.

        After ``SummarizeTool`` is used, the Agent inserts a summary message
        (with ``'summary': True``) into ``user_history`` followed by a
        context-cleared notification, but does *not* remove the old messages
        that were summarized.  This method prunes them.

        The compaction keeps:
          - Leading system prompt messages (role='system' before any
            non-system message)
          - The latest summary message (the one with ``'summary': True``)
          - All messages after the latest summary

        All other messages (old conversation turns before the summary)
        are removed.

        Returns:
            bool: ``True`` if compaction was performed, ``False`` if no
            summary was found (nothing to compact).
        """
        # Find the last summary message
        last_summary_idx = -1
        for i in range(len(self.user_history) - 1, -1, -1):
            msg = self.user_history[i]
            if isinstance(msg, dict) and msg.get("summary") is True:
                last_summary_idx = i
                break

        if last_summary_idx == -1:
            return False  # No summary found — nothing to compact

        # Collect leading system prompts (before the first non-system message).
        # Summary messages are NOT included here — they're handled separately.
        leading_system_msgs = []
        for msg in self.user_history:
            if msg.get("role") == "system" and not msg.get("summary"):
                leading_system_msgs.append(msg)
            else:
                break

        # Build new history: leading system prompts + summary + everything after
        new_history = list(leading_system_msgs)
        new_history.append(self.user_history[last_summary_idx])
        new_history.extend(self.user_history[last_summary_idx + 1:])

        self.user_history = new_history
        self._on_conversation_changed()
        return True

    # ── Persistence ──────────────────────────────────────────────────

    def to_persistable_dict(self) -> Dict[str, Any]:
        """Serialize this WorkerContext to a dict for JSON persistence."""
        return {
            "session_id": self.session_id,
            "worker_name": self.worker_name,
            "turn_count": self.turn_count,
            "conversation": self.user_history,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
        }

    @classmethod
    def from_persistable_dict(cls, data: Dict[str, Any]) -> "WorkerContext":
        """Deserialize a WorkerContext from a dict (as stored in context.json)."""
        return cls(
            session_id=data.get("session_id"),
            worker_name=data.get("worker_name"),
            user_history=data.get("conversation", []),
            total_input_tokens=data.get("total_input_tokens", 0),
            total_output_tokens=data.get("total_output_tokens", 0),
            turn_count=data.get("turn_count", 0),
        )

    def __repr__(self) -> str:
        return (
            f"WorkerContext(session_id={self.session_id!r}, "
            f"worker_name={self.worker_name!r}, "
            f"messages={len(self.user_history)}, "
            f"input_tokens={self.total_input_tokens}, "
            f"output_tokens={self.total_output_tokens}, "
            f"turns={self.turn_count})"
        )
