"""One clock: the TimeBudget contract for every worker timeout hop.

The defect this module removes is *timeout independence*: the same logical
budget was resolved in at least four places with four different numbers, so a
caller asking for 420 s of worker time got 300 s of wait (a silent clamp), the
worker's own agent loop timed out on a third number, and nothing in the
envelope said any of it had happened.

``TimeBudget`` makes the resolution explicit and *reportable*:

  * ``requested``  -- what the caller asked for (``None`` when it said nothing);
  * ``effective``  -- the number every hop must actually use;
  * ``cap``        -- the ceiling that was applied (``DECLARED_CEILING``);
  * ``clamped``    -- True only when ``effective != requested`` because of the
                      cap -- a clamp is never silent, it is reported;
  * ``clamped_by`` -- the layer that owns the cap that clamped it;
  * ``source``     -- which hop resolved this budget (spawn / query / join ...).

Resolution rules
----------------
* ``requested is None``          -> ``default``, not clamped;
* ``requested <= cap``           -> honoured EXACTLY (the requested value is
                                    carried through untouched: no float drift,
                                    no int truncation that changes it);
* ``requested > cap``            -> ``strict=True`` raises
                                    :class:`TimeBudgetExceeded`` (fail at
                                    dispatch), ``strict=False`` clamps to
                                    ``cap`` and says so;
* non-numeric / non-positive     -> treated as absent (``None``).  The check is
  ``isinstance(..., numbers.Real)``, NOT ``try: float(...)``: a ``MagicMock``
  auto-attribute implements ``__float__`` (returning 1.0), so a float-based
  probe would silently accept a mock as a 1-second budget -- see the same note
  in ``tools/workspace/worker_timeout.py``.

``DECLARED_CEILING`` is the ONE declared hard ceiling: 600 s.  It is what the
stack can actually honour end to end -- a worker's own agent loop soft-times-out
at this bound and the caller waits this bound plus the 60 s query grace -- and
it is the value the layers below already ship, so declaring it once cannot
change any working configuration, only expose the ones that were lying:

  * ``agent/config/defaults.py`` ``HARD_TIMEOUT`` (re-exported from here),
  * ``agent/config/defaults.py`` ``WORKER_TIMEOUT_SECONDS`` -- the last hop of
    the worker-timeout resolution chain, so an unconfigured worker already
    lands exactly here,
  * ``tools/workspace/worker_thread.py`` -- dispatch (``_action_spawn``),
    query (``_action_query``) and join/wait_for_job (``_action_join``),
  * ``tools/workspace/worker_timeout.py`` -- ``_worker_query_wait_timeout``
    and ``clamped_wait_for_job_timeout``.

Dependency-free on purpose (stdlib + the dependency-free sibling leaf
``thoughtmachine.timeout_constants``): ``agent/config/defaults.py``,
``agent/core/state.py``, ``tools/docker_code_runner.py`` and
``infra/containers/executor.py`` all import this package without a cycle, so
every layer can read the one ceiling.
"""

from __future__ import annotations

import numbers
from dataclasses import dataclass
from typing import Any, Dict, Optional

from thoughtmachine.timeout_constants import SOFT_BUDGET_FALLBACK_SECONDS

#: The single named hard ceiling (seconds).  See the module docstring: 600 is
#: the bound every layer of the stack can honour end to end.
DECLARED_CEILING = 600

#: Who owns :data:`DECLARED_CEILING`.  Carried into every clamp report and into
#: the ``TimeBudgetExceeded`` message so a caller learns which layer to change
#: instead of guessing why its request was reduced.
CEILING_OWNER = (
    "agent.config.defaults.HARD_TIMEOUT "
    "(thoughtmachine.timebudget.DECLARED_CEILING)"
)

#: Default budget when the caller requests nothing.  Re-exported from the
#: dependency-free leaf so there is exactly one 300 in the tree.
DEFAULT_REQUEST_SECONDS = SOFT_BUDGET_FALLBACK_SECONDS


def _as_requested(value: Any) -> Optional[float]:
    """Return ``value`` as a usable request, or ``None`` when it is not one.

    ``bool`` is excluded (``True`` is not a one-second budget) and so is every
    non-``numbers.Real`` object -- notably mock auto-attributes.
    """
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, numbers.Real):
        return None
    if not value > 0:
        return None
    return value


def _num_text(value: Any) -> str:
    """Render a number for humans without inventing precision."""
    if isinstance(value, numbers.Real) and not isinstance(value, bool):
        if float(value).is_integer():
            return str(int(value))
    return str(value)


class TimeBudgetExceeded(Exception):
    """A requested budget is above :data:`DECLARED_CEILING`.

    Raised at the dispatch hop (``strict=True``) so the request fails loudly
    instead of being silently clamped to something the caller did not ask for.
    Carries the three facts a caller needs: what it asked for, what the ceiling
    is, and which layer owns that ceiling.
    """

    def __init__(
        self,
        requested: Any,
        ceiling: Any = DECLARED_CEILING,
        owner: str = CEILING_OWNER,
        source: str = "unspecified",
    ) -> None:
        self.requested = requested
        self.ceiling = ceiling
        self.owner = owner
        self.source = source
        super().__init__(
            f"requested timeout {_num_text(requested)}s exceeds the declared "
            f"ceiling of {_num_text(ceiling)}s owned by {owner} -- refusing to "
            f"silently clamp (resolved at {source}); ask for at most "
            f"{_num_text(ceiling)}s or raise the ceiling there"
        )


@dataclass(frozen=True)
class TimeBudget:
    """An immutable, fully-reported timeout resolution.  See module docstring."""

    requested: Optional[float]
    effective: Any
    cap: Any
    clamped: bool
    clamped_by: Optional[str]
    source: str

    # ── resolution ────────────────────────────────────────────────────────
    @classmethod
    def resolve(
        cls,
        requested: Any = None,
        default: Any = DEFAULT_REQUEST_SECONDS,
        cap: Any = DECLARED_CEILING,
        strict: bool = False,
        source: str = "unspecified",
        clamped_by: Optional[str] = None,
    ) -> "TimeBudget":
        """Resolve one budget.  ``strict=True`` raises instead of clamping."""
        wanted = _as_requested(requested)
        if wanted is None:
            # Nothing usable was asked for: take the caller's named default and
            # report the absence honestly (``requested`` stays None).
            return cls(
                requested=None,
                effective=default,
                cap=cap,
                clamped=False,
                clamped_by=None,
                source=source,
            )
        if cap is not None and wanted > cap:
            owner = clamped_by or CEILING_OWNER
            if strict:
                raise TimeBudgetExceeded(
                    requested=wanted, ceiling=cap, owner=owner, source=source
                )
            return cls(
                requested=wanted,
                effective=cap,
                cap=cap,
                clamped=True,
                clamped_by=owner,
                source=source,
            )
        # Within the ceiling: carry the requested value through untouched.
        return cls(
            requested=wanted,
            effective=wanted,
            cap=cap,
            clamped=False,
            clamped_by=None,
            source=source,
        )

    # ── reporting ─────────────────────────────────────────────────────────
    def clamp_reason(self) -> Optional[str]:
        """``None`` when nothing was clamped, else the full explanation."""
        if not self.clamped:
            return None
        return (
            f"requested {_num_text(self.requested)}s exceeds the "
            f"{_num_text(self.cap)}s ceiling owned by {self.clamped_by}; "
            f"effective budget is {_num_text(self.effective)}s "
            f"(resolved at {self.source})"
        )

    def as_envelope_fields(self) -> Dict[str, Any]:
        """Flat dict a result envelope spreads to show the ONE clock it got.

        There is exactly ONE spelling per value, and it is the canonical one:
        ``requested_timeout_seconds`` / ``effective_timeout_seconds`` /
        ``timeout_clamped`` / ``timeout_clamped_by`` / ``timeout_source``.

        No aliases are emitted.  Two names for one number is the same disease
        as two clocks for one deadline: it lets a reader pick the spelling that
        happens to agree with it.  Consumers read these five keys, and a
        consumer that wants a shorter local name binds the attribute on the
        :class:`TimeBudget` (``budget.effective`` et al.) instead of asking this
        dict for a second label.
        """
        return {
            "requested_timeout_seconds": self.requested,
            "effective_timeout_seconds": self.effective,
            "timeout_clamped": self.clamped,
            "timeout_clamped_by": self.clamped_by,
            "timeout_source": self.source,
        }
