"""Truth table for agent.core.turn_demand.compose_demand.

The whole point of the module is that the six stop authorities cannot disagree,
so the table is asserted exhaustively rather than by example: every row pins
demand, source, disposition AND ``reasons`` (order included, because a receipt
that names the signals in the wrong order is a receipt nobody trusts).

Run:  python3 -m pytest tests/test_turn_demand.py -q
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.core.turn_demand import (  # noqa: E402
    Demand,
    DemandSource,
    Disposition,
    TurnDemand,
    compose_demand,
)


class TestComposeDemandTable:
    """Most restrictive wins, in one fixed precedence order."""

    @pytest.mark.parametrize(
        ('signals', 'demand', 'source', 'disposition', 'reasons'),
        [
            # Nothing live: the turn is simply running, and nobody is asking.
            ({}, Demand.RUN, None, None, ()),
            # A pause on its own stays a pause.
            (
                {'paused': True},
                Demand.PAUSE,
                DemandSource.USER,
                Disposition.RESUMABLE,
                ('paused',),
            ),
            # THE INVERSION THIS MODULE EXISTS TO PREVENT: a pause still set
            # when a stop arrives must NOT soften the stop.
            (
                {'paused': True, 'stop': True},
                Demand.STOP,
                DemandSource.USER,
                Disposition.RESUMABLE,
                ('stop', 'paused'),
            ),
            # Same inversion, timer flavour: a stale pause cannot make a
            # non-resumable timeout look resumable.
            (
                {'paused': True, 'timeout': True},
                Demand.STOP,
                DemandSource.TIMER,
                Disposition.TERMINAL_WITH_RECEIPT,
                ('timeout', 'paused'),
            ),
            # Stop outranks the timer, and stop is a detach: resumable.
            (
                {'stop': True, 'timeout': True},
                Demand.STOP,
                DemandSource.USER,
                Disposition.RESUMABLE,
                ('stop', 'timeout'),
            ),
            (
                {'token_budget': True},
                Demand.STOP,
                DemandSource.BUDGET,
                Disposition.TERMINAL_WITH_REPORT,
                ('token_budget',),
            ),
            (
                {'turn_budget': True},
                Demand.STOP,
                DemandSource.BUDGET,
                Disposition.TERMINAL_WITH_REPORT,
                ('turn_budget',),
            ),
            # Both budgets: one label, both reasons, token named first.
            (
                {'token_budget': True, 'turn_budget': True},
                Demand.STOP,
                DemandSource.BUDGET,
                Disposition.TERMINAL_WITH_REPORT,
                ('token_budget', 'turn_budget'),
            ),
            # The timer outranks a budget stop, and says so with a receipt.
            (
                {'timeout': True, 'token_budget': True},
                Demand.STOP,
                DemandSource.TIMER,
                Disposition.TERMINAL_WITH_RECEIPT,
                ('timeout', 'token_budget'),
            ),
            (
                {'kill': True},
                Demand.STOP,
                DemandSource.OPERATOR,
                Disposition.DESTROYED,
                ('kill',),
            ),
            (
                {'stop': True, 'kill': True},
                Demand.STOP,
                DemandSource.OPERATOR,
                Disposition.DESTROYED,
                ('kill', 'stop'),
            ),
            # Everything at once: only kill's disposition survives, and the
            # receipt names all six in precedence order.
            (
                {
                    'kill': True,
                    'stop': True,
                    'timeout': True,
                    'token_budget': True,
                    'turn_budget': True,
                    'paused': True,
                },
                Demand.STOP,
                DemandSource.OPERATOR,
                Disposition.DESTROYED,
                (
                    'kill',
                    'stop',
                    'timeout',
                    'token_budget',
                    'turn_budget',
                    'paused',
                ),
            ),
        ],
    )
    def test_row(self, signals, demand, source, disposition, reasons):
        result = compose_demand(**signals)
        assert isinstance(result, TurnDemand)
        assert result.demand is demand
        assert result.source is source
        assert result.disposition is disposition
        assert result.reasons == reasons


class TestComposeDemandDetail:
    """The human sentence: named for the winner, present in every row."""

    def test_running_detail(self):
        assert compose_demand().detail == 'running'

    def test_pause_detail_says_resumable(self):
        assert 'resumable' in compose_demand(paused=True).detail

    def test_stop_detail_does_not_speak_in_terminal_vocabulary(self):
        # stop == detach: the worker survives and is re-queryable, so the
        # sentence must not read as though the work were gone.
        detail = compose_demand(stop=True).detail
        assert 'detached' in detail
        assert 'destroyed' not in detail

    def test_timeout_detail_requires_evidence(self):
        assert 'receipt' in compose_demand(timeout=True).detail

    def test_budget_detail_names_the_winning_budget(self):
        assert 'token budget' in compose_demand(token_budget=True).detail
        assert 'turn budget' in compose_demand(turn_budget=True).detail
        # Both live: the token budget is the name that gets used.
        both = compose_demand(token_budget=True, turn_budget=True)
        assert 'token budget' in both.detail

    def test_kill_detail_says_destroyed(self):
        assert 'destroyed' in compose_demand(kill=True).detail

    def test_reasons_is_a_tuple_of_every_live_signal(self):
        result = compose_demand(stop=True, paused=True)
        assert isinstance(result.reasons, tuple)
        assert result.reasons == ('stop', 'paused')

    def test_run_carries_no_reasons_and_no_source(self):
        result = compose_demand()
        assert result.reasons == ()
        assert result.source is None
        assert result.disposition is None
