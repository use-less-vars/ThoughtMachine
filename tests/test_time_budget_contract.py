"""RED contract tests for a single, explicit TimeBudget on the dispatch path.

FEASIBILITY GATE -> **BRANCH A: the worker's own soft budget IS settable per
instance from the dispatch path.**  Evidence (all quoted from the tree as it
stands today):

  * ``tools/workspace/worker_thread.py:1369``
        ``timeout_seconds: Optional[int] = None,``            <- ctor arg
  * ``tools/workspace/worker_thread.py:1444-1448``
        ``self._timeout_seconds: int = (timeout_seconds if timeout_seconds
        is not None else _def_timeout)``                      <- per-instance
  * ``tools/workspace/worker_thread.py:2985``
        ``worker_cfg["timeout_seconds"] = self._timeout_seconds``
    ``tools/workspace/worker_thread.py:3036``
        ``return AgentConfig(**worker_cfg)``                  <- the worker's
        OWN agent-loop soft budget is built from that per-instance value
  * ``tools/workspace/worker_thread.py:2993-2994``
        ``worker_cfg["time_warning_threshold"] = max(5, int(_timeout_for_warning * 0.8))``
  * ``agent/core/state.py:188``
        ``timeout = getattr(self.config, 'timeout_seconds', self.timeout_seconds)``
    -- the value the agent loop self-terminates against.
  * ``agent/config/models.py:192``
        ``timeout_seconds: int = Field(default=SOFT_BUDGET_FALLBACK_SECONDS, ...)``
  * dispatch path: ``tools/workspace/worker_thread.py:6217-6221``
        ``effective_timeout = (self.timeout_seconds if self.timeout_seconds
        is not None else definition.get("timeout_seconds"))``
    ``tools/workspace/worker_thread.py:6262``
        ``timeout_seconds=effective_timeout,``                <- into WorkerThread

So a 420 s request is *honourable* end-to-end: the worker agent soft-times-out
at 420 (``worker_thread.py:2985``) and the caller waits 420 + 60 grace
(``agent/config/defaults.py:37`` ``QUERY_WAIT_GRACE_SECONDS = 60``).

The contract these tests encode (BRANCH A only):
  * a request within the declared ceiling is honoured EXACTLY (420 -> 420);
  * only a request above the declared ceiling fails, and it fails AT DISPATCH
    with an explicit error naming both numbers and the ceiling's owner;
  * a permissive resolution that does clamp must REPORT the clamp instead of
    hiding it (today ``tools/workspace/worker_thread.py:7074``
    ``total = min(requested_timeout, 300)`` hides it behind an INFO log).

Module under contract: ``thoughtmachine/timebudget.py`` exporting
``TimeBudget``, ``TimeBudgetExceeded`` and ``DECLARED_CEILING``.
Justification for that path: ``thoughtmachine/`` is the dependency-free leaf
package both layers already import -- ``tools/docker_code_runner.py:12``,
``infra/containers/executor.py:26``, ``agent/core/state.py:8`` and
``agent/config/models.py:13`` all import ``thoughtmachine.timeout_constants``
without a cycle, so a sibling module sits at the same proven seam.  The module
does not exist yet -- that is the behaviour under test, not an import typo.

Run:  python -m pytest tests/test_time_budget_contract.py -q
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:  # the module under contract -- absent today, which is the point
    from thoughtmachine.timebudget import (  # type: ignore[import-not-found]
        DECLARED_CEILING,
        TimeBudget,
        TimeBudgetExceeded,
    )

    _IMPORT_ERROR = None
except Exception as _exc:  # pragma: no cover - exercised until the module lands
    DECLARED_CEILING = None
    TimeBudget = None
    TimeBudgetExceeded = None
    _IMPORT_ERROR = _exc


def _tb():
    """Return the TimeBudget class, failing the *calling test* if absent."""
    if _IMPORT_ERROR is not None:
        pytest.fail(
            "thoughtmachine.timebudget is not importable "
            f"({_IMPORT_ERROR!r}) -- the dispatch path has no TimeBudget "
            "contract object yet",
            pytrace=False,
        )
    return TimeBudget


# ───────────────────────────── 1. module surface ──────────────────────────────


def test_module_exposes_time_budget():
    if _IMPORT_ERROR is not None:
        pytest.fail(
            "thoughtmachine.timebudget must export TimeBudget, "
            f"TimeBudgetExceeded and DECLARED_CEILING; got {_IMPORT_ERROR!r}",
            pytrace=False,
        )
    budget = TimeBudget.resolve(420, default=300, cap=DECLARED_CEILING)
    for field in (
        "requested",
        "effective",
        "cap",
        "clamped",
        "clamped_by",
        "as_envelope_fields",
    ):
        assert hasattr(budget, field), f"TimeBudget is missing {field!r}"
    assert issubclass(TimeBudgetExceeded, Exception)


# ───────────────────────── 2. default, not a magic cap ────────────────────────


def test_unspecified_request_takes_the_default_unchanged():
    budget = _tb().resolve(None, default=60, cap=600)
    assert budget.effective == 60
    assert budget.clamped is False
    assert budget.requested is None


# ─────────────────────── 3. a 420 s request is honoured ───────────────────────


def test_request_within_the_ceiling_is_honoured_exactly():
    budget = _tb().resolve(420, default=300, cap=600)
    # literal on purpose: the number a caller asked for, not a recomputed constant
    assert budget.effective == 420, (
        f"requested 420 within the 600 s ceiling became {budget.effective}"
    )
    assert budget.clamped is False


# ─────────────────── 4. above the ceiling -> fail at dispatch ─────────────────

_CEILING_OWNERS = ("hard_timeout", "timeout_constants", "declared_ceiling")


def test_request_above_the_ceiling_fails_at_dispatch():
    _tb()  # fail for the real reason before pytest inspects the exception class
    with pytest.raises(TimeBudgetExceeded) as excinfo:
        _tb().resolve(900, default=300, cap=600, strict=True)
    message = str(excinfo.value)
    assert "900" in message, f"error must name the request: {message!r}"
    assert "600" in message, f"error must name the ceiling: {message!r}"
    assert any(owner in message.lower() for owner in _CEILING_OWNERS), (
        "error must name the layer that owns the ceiling "
        "(agent/config/defaults.py:131 HARD_TIMEOUT = 600); "
        f"got {message!r}"
    )


# ───────────── 5. permissive resolution REPORTS the clamp, never hides it ─────


def test_permissive_resolution_reports_the_clamp_instead_of_hiding_it():
    budget = _tb().resolve(900, default=300, cap=600, strict=False)
    assert budget.effective == 600
    assert budget.clamped is True
    fields = budget.as_envelope_fields()
    assert set(fields) >= {
        "requested_timeout_seconds",
        "effective_timeout_seconds",
        "timeout_clamped",
        "timeout_clamped_by",
    }
    assert fields["requested_timeout_seconds"] == 900
    assert fields["effective_timeout_seconds"] == 600
    assert fields["timeout_clamped"] is True
    assert fields["timeout_clamped_by"] is not None


# ─────── 6. ACCEPTANCE: the real job-wait helper must not answer 300 ──────────


def test_job_wait_does_not_return_300_for_a_420_request():
    """The engineer's acceptance test, on the helper that ships today.

    ``tools/workspace/worker_timeout.py:83``
        ``def clamped_wait_for_job_timeout(requested_timeout: Any, cap: float = 300.0) -> int:``
    Its default ``cap=300.0`` is the silent clamp.  The declared ceiling for
    the worker loop is 600 (``agent/config/defaults.py:131`` HARD_TIMEOUT),
    so a 420 s request has no legitimate reason to become 300.  Pure
    arithmetic call: no threads, no registry, no sleeping.
    """
    from tools.workspace.worker_timeout import clamped_wait_for_job_timeout

    result = clamped_wait_for_job_timeout(420)
    assert result != 300, (
        "silent clamp: requested 420, got 300 "
        "(tools/workspace/worker_timeout.py:83 defaults cap=300.0; "
        "the declared ceiling is 600 -- agent/config/defaults.py:131 HARD_TIMEOUT)"
    )
    assert result == 420, f"requested 420 was not honoured: got {result}"


# ─────────────────── 7. one clock for the wait AND the envelope ───────────────


def test_one_clock_is_used_by_wait_and_envelope():
    budget = _tb().resolve(420, default=300, cap=600)
    wait_seconds = budget.effective  # what the deadline math would use
    envelope = budget.as_envelope_fields()  # what the tool returns to the agent
    assert envelope["effective_timeout_seconds"] == wait_seconds, (
        "two clocks: the deadline math and the result envelope disagree "
        f"({wait_seconds} vs {envelope['effective_timeout_seconds']})"
    )
    assert wait_seconds == 420
    assert envelope["requested_timeout_seconds"] == 420


# ─────────────── 8. exactly one declared ceiling, one symbol ──────────────────


def test_declared_ceiling_is_a_single_named_constant():
    if _IMPORT_ERROR is not None:
        pytest.fail(
            f"thoughtmachine.timebudget must expose DECLARED_CEILING; got {_IMPORT_ERROR!r}",
            pytrace=False,
        )
    assert DECLARED_CEILING == 600, (
        f"declared ceiling must be the worker-loop bound (600); got {DECLARED_CEILING!r}"
    )
    # Same number the stack already ships: a second, different ceiling is the defect.
    defaults = pytest.importorskip("agent.config.defaults")
    assert DECLARED_CEILING == defaults.HARD_TIMEOUT
