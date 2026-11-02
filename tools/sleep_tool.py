# tools/sleep_tool.py
"""Long-blocking, promptly-interruptible ``sleep`` tool (P0 proof).

The point of this tool is not the sleeping: it is that a tool can block for
minutes and still return within one slice (~0.1 s) of a cancellation signal.
Tool execution is destined to be lifted into a per-session worker process, and
that lift is only safe if a running tool can be interrupted promptly.

Cancellation plumbing that exists today
--------------------------------------
* ``agent/core/tool_executor.py`` consults ``AgentConfig.stop_check`` only
  BETWEEN tool calls (a stopped worker skips the remaining pending calls), so
  a single in-flight ``execute()`` is never interrupted.
* ``agent/config/models.py`` *strips* ``stop_check`` from serialisation, so it
  does not reach tools inside ``agent_config`` either.

Therefore the cancellation token is a per-worker ``threading.Event`` owned by
``WorkerThread`` (``_turn_cancel``) and bound to the *executing thread* here by
:func:`bind_turn_cancel` through a ``threading.local`` holder.  A tool running
on worker A's thread therefore sees only worker A's token: cancelling worker A
can never abort a sleep belonging to worker B in the same process.

Scope, stated plainly: the binding is THREAD-scoped.  A tool that hands its
blocking work to a thread of its own does not inherit the binding and must bind
that thread itself.
"""

from __future__ import annotations

import json
import math
import threading
import time
from typing import ClassVar, List, Literal, Optional

from pydantic import Field

from .base import ToolBase

# Hard cap. Over-cap requests are REFUSED, never silently clamped: a clamped
# sleep makes the caller believe it waited longer than it actually did.
MAX_SECONDS: float = 300.0

# Wait granularity. The loop never blocks longer than one slice without
# re-checking the interrupt, so cancellation latency is bounded by it.
SLICE_SECONDS: float = 0.1

# The primitives live in ``tools/cancellation.py``; imported and RE-EXPORTED
# so ``tools.sleep_tool.<name>`` keeps working for existing callers.
from .cancellation import (  # noqa: F401  (re-exported public API)
    _active_event,
    _tls,
    bind_turn_cancel,
    cancel_reason,
    interrupt,
    is_interrupted,
    reset,
    unbind,
)


class Sleep(ToolBase):
    """Block for ``seconds``, interruptible within one slice."""

    required_categories: ClassVar[List[str]] = []
    name: ClassVar[str] = "sleep"
    tool: Literal["sleep"] = "sleep"

    seconds: float = Field(
        description=(
            "Seconds to block. Must be > 0; hard cap "
            f"{int(MAX_SECONDS)}s (over-cap requests are refused, not clamped)."
        )
    )

    reason: Optional[str] = Field(
        default=None,
        description="Why the agent is sleeping. Echoed into the transcript.",
    )

    def _envelope(
        self,
        ok: bool,
        interrupted: bool,
        elapsed: float,
        requested: float,
        reason: Optional[str],
        error: Optional[str] = None,
    ) -> str:
        payload = {
            "tool": "sleep",
            "ok": ok,
            "interrupted": interrupted,
            "requested_seconds": requested,
            "elapsed_seconds": round(elapsed, 3),
            "reason": reason,
            "requested_reason": self.reason,
        }
        if error is not None:
            payload["error"] = error
        return self._truncate_output(json.dumps(payload))

    def _external_stop(self) -> bool:
        """True when an injected ``agent_config['stop_check']`` callable says stop.

        Defensive only: ``stop_check`` is stripped from ``AgentConfig``
        serialisation (``agent/config/models.py``), so in the current wiring this
        is normally absent.  It costs nothing and is the natural seam if the
        runtime ever starts handing the probe to tools.
        """
        cfg = self.agent_config or {}
        probe = cfg.get("stop_check") if isinstance(cfg, dict) else None
        if not callable(probe):
            return False
        try:
            return bool(probe())
        except Exception:
            return False

    def execute(self) -> str:
        started = time.monotonic()

        if isinstance(self.seconds, bool):
            return self._envelope(
                False, False, time.monotonic() - started, 0.0, None,
                error=f"seconds must be a number, got bool {self.seconds!r}",
            )
        try:
            requested = float(self.seconds)
        except (TypeError, ValueError):
            return self._envelope(
                False, False, time.monotonic() - started, 0.0, None,
                error=f"seconds must be a number, got {self.seconds!r}",
            )
        if not math.isfinite(requested):
            return self._envelope(
                False, False, time.monotonic() - started, 0.0, None,
                error=f"seconds must be finite, got {self.seconds!r}",
            )
        if requested <= 0:
            return self._envelope(
                False, False, time.monotonic() - started, requested, None,
                error=f"seconds must be > 0, got {requested:g}",
            )
        if requested > MAX_SECONDS:
            return self._envelope(
                False, False, time.monotonic() - started, requested, None,
                error=(
                    f"seconds={requested:g} exceeds the {int(MAX_SECONDS)}s cap "
                    "for the sleep tool; issue a shorter sleep instead"
                ),
            )

        # Resolve the cancellation token ONCE, for THIS thread only.
        #
        # Bound (worker path): the token is the owning worker's ``_turn_cancel``,
        # cleared at the start of every query and set by ``detach()``/``kill()``.
        # It must NOT be cleared here -- a cancel that arrived between dispatch
        # and ``execute()`` would be lost.
        #
        # Unbound (direct/test use): the thread's own token, which keeps the
        # legacy self-healing behaviour (a leftover interrupt must not abort an
        # unrelated later call).
        bound = getattr(_tls, "event", None)
        if bound is None:
            reset()
            cancel_event = _active_event()
        else:
            cancel_event = bound

        deadline = started + requested
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # Event.wait() -- not time.sleep() -- so a cancel wakes the slice
            # immediately instead of finishing it.
            if cancel_event.wait(min(SLICE_SECONDS, remaining)):
                return self._envelope(
                    False, True, time.monotonic() - started, requested,
                    getattr(_tls, "reason", None) or "turn_cancelled",
                )
            if self._external_stop():
                return self._envelope(
                    False, True, time.monotonic() - started, requested,
                    "stop_check",
                )

        return self._envelope(
            True, False, time.monotonic() - started, requested, self.reason,
        )
