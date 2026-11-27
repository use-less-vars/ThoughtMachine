"""The ONE composition point for the authorities that can stop a turn.

Six independent predicates can end or suspend a turn today -- session pause,
stop/detach, the turn timeout, the token budget, the turn budget, and a worker
kill -- and each one has always been read where it is enforced.  Read
separately, they disagree: a live pause could be mistaken for a stop, and a
receipt written for one authority could contradict the report written for
another.  :func:`compose_demand` collapses all six into a single
:class:`TurnDemand` so that whatever announces the outcome speaks with one
voice.

The rule is: **the most restrictive live demand wins, and a stale pause must
never soften a stop.**  The inversion this forbids was a real bug, not a
hypothetical -- a pause flag that was still set when a stop arrived let the
composed outcome read as resumable, which is exactly the vocabulary that
strands work.

This module is pure and wires nothing: no imports from the agent, no state, no
side effects.  It lands first so that the call sites can be moved onto it one
at a time, with behaviour unchanged until each is moved on purpose.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional


class Demand(str, enum.Enum):
    """What is being asked of the turn right now."""

    RUN = 'run'
    PAUSE = 'pause'
    STOP = 'stop'


class DemandSource(str, enum.Enum):
    """Who is asking.  ``None`` on a RUN: nobody is."""

    USER = 'user'
    TIMER = 'timer'
    BUDGET = 'budget'
    OPERATOR = 'operator'


class Disposition(str, enum.Enum):
    """What survives of the turn once the demand is honoured.

    ``RESUMABLE`` covers both a pause and a stop, for the same reason: stop is
    a detach, so the worker outlives it and stays re-queryable.  Only
    ``DESTROYED`` means the work is gone.
    """

    RESUMABLE = 'resumable'
    TERMINAL_WITH_REPORT = 'terminal_with_report'
    TERMINAL_WITH_RECEIPT = 'terminal_with_receipt'
    DESTROYED = 'destroyed'


@dataclass(frozen=True)
class TurnDemand:
    """One composed answer, plus every signal that produced it.

    ``reasons`` carries ALL the signals that were live, in precedence order --
    not only the winning one -- so a single receipt can name everything that
    was true at the moment the turn was ended, instead of the caller of one
    authority reporting its own truth in isolation.
    """

    demand: Demand
    source: Optional[DemandSource]
    disposition: Optional[Disposition]
    detail: str
    reasons: tuple[str, ...]


def compose_demand(
    *,
    kill: bool = False,
    stop: bool = False,
    timeout: bool = False,
    token_budget: bool = False,
    turn_budget: bool = False,
    paused: bool = False,
) -> TurnDemand:
    """Compose the most restrictive live demand; a stale pause never softens a stop.

    Precedence, highest first: kill, stop, timeout, token budget, turn budget,
    pause.  ``reasons`` lists every true input in that same order.
    """
    reasons: list[str] = [
        name
        for name, live in (
            ('kill', kill),
            ('stop', stop),
            ('timeout', timeout),
            ('token_budget', token_budget),
            ('turn_budget', turn_budget),
            ('paused', paused),
        )
        if live
    ]

    if kill:
        return TurnDemand(
            Demand.STOP,
            DemandSource.OPERATOR,
            Disposition.DESTROYED,
            'killed by operator: the work is destroyed',
            tuple(reasons),
        )
    if stop:
        # A stop is a detach: the worker survives and stays re-queryable, so
        # this is resumable and must not be announced as terminal.
        return TurnDemand(
            Demand.STOP,
            DemandSource.USER,
            Disposition.RESUMABLE,
            'stopped by user: detached, re-queryable',
            tuple(reasons),
        )
    if timeout:
        # No user can resume a timer, so the turn must leave evidence instead.
        return TurnDemand(
            Demand.STOP,
            DemandSource.TIMER,
            Disposition.TERMINAL_WITH_RECEIPT,
            'turn timeout: not resumable, receipt required',
            tuple(reasons),
        )
    if token_budget or turn_budget:
        # Both read as a budget stop; the token budget names it when both fire.
        detail = (
            'token budget exhausted: report required'
            if token_budget
            else 'turn budget exhausted: report required'
        )
        return TurnDemand(
            Demand.STOP,
            DemandSource.BUDGET,
            Disposition.TERMINAL_WITH_REPORT,
            detail,
            tuple(reasons),
        )
    if paused:
        return TurnDemand(
            Demand.PAUSE,
            DemandSource.USER,
            Disposition.RESUMABLE,
            'paused by user: resumable',
            tuple(reasons),
        )
    return TurnDemand(Demand.RUN, None, None, 'running', ())
