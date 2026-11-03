"""Regression tests for the S7a epoch fence on both WorkerRegistry maps.

These tests exist because worker instance ids restart at 1, so a successor
incarnation re-registers the very ``(session_id, worker_name, instance_id)``
key that a dying predecessor still holds.  Without epoch fencing the
predecessor's ``finally:`` block -- which unwinds *after* the successor has
already registered -- clobbers the successor's registry entry, and the bridge
can no longer find the worker's event bus (its events are silently dropped).

Every registry mutation is therefore tagged with the caller's ``_generation``
epoch: a write is accepted only when it is not older than the newest epoch seen
for that key, the newest epoch SURVIVES the pop, and on a successful pop the
record is RAISED to the caller's epoch -- so a later stale writer still refuses
even after its successor has come and gone.

No real threads and no sleeps are used: the race is reproduced by calling the
two incarnations' registry operations in the order the real race produces.
"""

from __future__ import annotations

import random
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tools.workspace.worker_registry import WorkerRegistry

SESSION = "sess-hazard"
WORKER = "w1"
INSTANCE = 1
KEY = (SESSION, WORKER, INSTANCE)


def _incarnation(generation):
    """Stand-in for a worker incarnation: only ``_generation`` is significant."""
    return SimpleNamespace(_generation=generation, instance_id=INSTANCE)


@pytest.fixture
def registry():
    """The process-wide singleton with all four dicts snapshotted and restored."""
    wr = WorkerRegistry.get_instance()
    with wr._registry_lock:
        old_workers = dict(wr._worker_registry)
        old_worker_epochs = dict(wr._worker_epochs)
        wr._worker_registry.clear()
        wr._worker_epochs.clear()
    with wr._bus_registry_lock:
        old_buses = dict(wr._worker_event_bus_registry)
        old_bus_epochs = dict(wr._bus_epochs)
        wr._worker_event_bus_registry.clear()
        wr._bus_epochs.clear()
    try:
        yield wr
    finally:
        with wr._registry_lock:
            wr._worker_registry.clear()
            wr._worker_registry.update(old_workers)
            wr._worker_epochs.clear()
            wr._worker_epochs.update(old_worker_epochs)
        with wr._bus_registry_lock:
            wr._worker_event_bus_registry.clear()
            wr._worker_event_bus_registry.update(old_buses)
            wr._bus_epochs.clear()
            wr._bus_epochs.update(old_bus_epochs)


# -- Worker-thread map: the CAS lattice --------------------------------------


def test_register_refuses_stale_epoch(registry):
    winner = _incarnation(5)
    loser = _incarnation(3)

    assert registry.register_worker(SESSION, WORKER, winner, INSTANCE, epoch=5) is True
    assert registry.register_worker(SESSION, WORKER, loser, INSTANCE, epoch=3) is False
    assert registry.get_worker(SESSION, WORKER, INSTANCE) is winner
    assert registry._worker_epochs[KEY] == 5


def test_register_equal_epoch_is_idempotent_overwrite(registry):
    first = _incarnation(5)
    second = _incarnation(5)
    assert first is not second

    assert registry.register_worker(SESSION, WORKER, first, INSTANCE, epoch=5) is True
    assert registry.register_worker(SESSION, WORKER, second, INSTANCE, epoch=5) is True
    assert registry.get_worker(SESSION, WORKER, INSTANCE) is second
    assert registry._worker_epochs[KEY] == 5


def test_unregister_refuses_newer_owner(registry):
    owner = _incarnation(5)
    assert registry.register_worker(SESSION, WORKER, owner, INSTANCE, epoch=5) is True

    assert registry.unregister_worker(SESSION, WORKER, INSTANCE, epoch=3) is False
    assert registry.get_worker(SESSION, WORKER, INSTANCE) is owner
    assert registry._worker_epochs[KEY] == 5


def test_unregister_raises_record_on_pop(registry):
    """The monotonicity invariant: a pop raises the record, it never lowers it."""
    assert registry.register_worker(SESSION, WORKER, _incarnation(5), INSTANCE, epoch=5) is True

    # A newer caller pops: the entry goes away, the record is raised to 7.
    assert registry.unregister_worker(SESSION, WORKER, INSTANCE, epoch=7) is True
    assert registry.get_worker(SESSION, WORKER, INSTANCE) is None
    assert registry._worker_epochs[KEY] == 7

    # An epoch between the old record (5) and the new one (7) is STALE: if the
    # pop had merely retained 5, this register would wrongly succeed.
    assert registry.register_worker(SESSION, WORKER, _incarnation(6), INSTANCE, epoch=6) is False
    assert registry.get_worker(SESSION, WORKER, INSTANCE) is None

    # The epoch that popped it may register again.
    assert registry.register_worker(SESSION, WORKER, _incarnation(7), INSTANCE, epoch=7) is True


def test_unregister_without_record_pops_and_records(registry):
    """A never-versioned key still accepts a fenced pop and gains a record."""
    legacy = _incarnation(3)
    assert registry.register_worker(SESSION, WORKER, legacy, INSTANCE) is None
    assert KEY not in registry._worker_epochs

    assert registry.unregister_worker(SESSION, WORKER, INSTANCE, epoch=7) is True
    assert registry.get_worker(SESSION, WORKER, INSTANCE) is None
    assert registry._worker_epochs[KEY] == 7

    assert registry.register_worker(SESSION, WORKER, _incarnation(6), INSTANCE, epoch=6) is False


def test_non_int_epoch_degrades_to_legacy(registry):
    """A duck-typed epoch must fail open on TYPE, never raise."""
    thread = _incarnation(4)

    # A string epoch behaves exactly like epoch=None (legacy, no record kept).
    assert registry.register_worker(SESSION, WORKER, thread, INSTANCE, epoch="abc") is None
    assert KEY not in registry._worker_epochs
    assert registry.get_worker(SESSION, WORKER, INSTANCE) is thread

    # A MagicMock epoch must not raise on the int comparison.
    assert registry.unregister_worker(SESSION, WORKER, INSTANCE, epoch=MagicMock()) is thread
    assert registry.get_worker(SESSION, WORKER, INSTANCE) is None
    assert KEY not in registry._worker_epochs

    # Same on the bus map.
    bus = SimpleNamespace(name="bus")
    assert registry.register_event_bus(SESSION, WORKER, bus, INSTANCE, epoch=MagicMock()) is None
    assert KEY not in registry._bus_epochs
    # The legacy bus pop returns None (not the popped bus) and still pops.
    assert registry.unregister_event_bus(SESSION, WORKER, INSTANCE, epoch="7") is None
    assert registry.get_event_bus(SESSION, WORKER, INSTANCE) is None
    assert KEY not in registry._bus_epochs


def test_legacy_return_shapes_unchanged(registry):
    """epoch=None keeps the pre-S7a return contract on both maps."""
    thread = _incarnation(1)
    assert registry.register_worker(SESSION, WORKER, thread) is None
    assert registry.unregister_worker(SESSION, WORKER, INSTANCE) is thread

    sentinel = object()
    assert registry.unregister_worker(SESSION, "missing", INSTANCE, sentinel) is sentinel

    assert registry.register_event_bus(SESSION, WORKER, "bus") is None
    assert registry.unregister_event_bus(SESSION, WORKER, INSTANCE) is None


# -- EventBus map: an independent CAS domain --------------------------------


def test_bus_map_is_an_independent_cas_domain(registry):
    predecessor_bus = SimpleNamespace(name="bus-4")
    successor_bus = SimpleNamespace(name="bus-5")

    assert registry.register_event_bus(SESSION, WORKER, predecessor_bus, INSTANCE, epoch=5) is True
    assert registry.register_event_bus(SESSION, WORKER, successor_bus, INSTANCE, epoch=3) is False
    assert registry.get_event_bus(SESSION, WORKER, INSTANCE) is predecessor_bus

    assert registry.unregister_event_bus(SESSION, WORKER, INSTANCE, epoch=3) is False
    assert registry.get_event_bus(SESSION, WORKER, INSTANCE) is predecessor_bus

    # The worker-thread map on the SAME key is a separate CAS domain: neither
    # map's writes nor its epoch record leak into the other.
    assert registry.register_worker(SESSION, WORKER, _incarnation(1), INSTANCE, epoch=1) is True
    assert registry._worker_epochs[KEY] == 1
    assert registry._bus_epochs[KEY] == 5
    assert registry.get_event_bus(SESSION, WORKER, INSTANCE) is predecessor_bus

    assert registry.unregister_worker(SESSION, WORKER, INSTANCE, epoch=1) is True
    assert registry.get_worker(SESSION, WORKER, INSTANCE) is None
    assert registry.get_event_bus(SESSION, WORKER, INSTANCE) is predecessor_bus
    assert registry._bus_epochs[KEY] == 5


# -- THE HAZARD: a killed predecessor must not evict its successor ----------


def test_killed_predecessor_teardown_cannot_evict_successor_bus(registry):
    """The production incident, reproduced deterministically."""
    predecessor = SimpleNamespace(
        _generation=4, instance_id=INSTANCE, bus=SimpleNamespace(name="predecessor-bus"),
    )
    successor = SimpleNamespace(
        _generation=5, instance_id=INSTANCE, bus=SimpleNamespace(name="successor-bus"),
    )

    # Instance ids restart at 1, so both incarnations collide on the same key.
    assert registry.register_event_bus(
        SESSION, WORKER, predecessor.bus, INSTANCE, epoch=predecessor._generation,
    ) is True
    assert registry.register_event_bus(
        SESSION, WORKER, successor.bus, INSTANCE, epoch=successor._generation,
    ) is True

    assert registry.get_event_buses_for_session(SESSION) == {WORKER: successor.bus}

    # The killed predecessor's ``finally:`` block is still unwinding.
    assert registry.unregister_event_bus(
        SESSION, WORKER, INSTANCE, epoch=predecessor._generation,
    ) is False

    # REGRESSION ASSERTION: the successor's bus is still discoverable. On the
    # pre-S7a code this returned the predecessor's stale entry (or nothing).
    assert registry.get_event_bus(SESSION, WORKER, INSTANCE) is successor.bus
    assert registry.get_event_buses_for_session(SESSION) == {WORKER: successor.bus}

    # The successor's own teardown does succeed and clears the key.
    assert registry.unregister_event_bus(
        SESSION, WORKER, INSTANCE, epoch=successor._generation,
    ) is True
    assert registry.get_event_bus(SESSION, WORKER, INSTANCE) is None
    assert registry.get_event_buses_for_session(SESSION) == {}


def test_killed_predecessor_teardown_cannot_evict_successor_thread(registry):
    """The same hazard on the worker-thread map."""
    predecessor = _incarnation(4)
    successor = _incarnation(5)

    assert registry.register_worker(
        SESSION, WORKER, predecessor, INSTANCE, epoch=predecessor._generation,
    ) is True
    assert registry.register_worker(
        SESSION, WORKER, successor, INSTANCE, epoch=successor._generation,
    ) is True
    assert registry.get_worker(SESSION, WORKER, INSTANCE) is successor

    # Stale teardown of the killed incarnation.
    assert registry.unregister_worker(
        SESSION, WORKER, INSTANCE, epoch=predecessor._generation,
    ) is False
    assert registry.get_worker(SESSION, WORKER, INSTANCE) is successor

    # The live incarnation can still be found and stopped.
    assert registry.unregister_worker(
        SESSION, WORKER, INSTANCE, epoch=successor._generation,
    ) is True
    assert registry.get_worker(SESSION, WORKER, INSTANCE) is None


# -- Property: the record is monotone ---------------------------------------


def _drive_lattice(epoch_map, register_fenced, unregister_fenced,
                   register_legacy, unregister_legacy):
    """Run 200 pseudo-random ops; the record for KEY may never decrease."""
    rng = random.Random(1234)
    recorded_max = None

    for step in range(200):
        epoch = rng.randrange(0, 9)
        op = rng.randrange(0, 4)
        payload = f"payload-{step}"
        stale = recorded_max is not None and recorded_max > epoch

        if op == 0:
            accepted = register_fenced(epoch, payload)
            if stale:
                assert accepted is False, (
                    f"step {step}: stale register(epoch={epoch}) accepted, stored={recorded_max}"
                )
            else:
                assert accepted is True, (
                    f"step {step}: register(epoch={epoch}) refused, stored={recorded_max}"
                )
                recorded_max = epoch if recorded_max is None else max(recorded_max, epoch)
        elif op == 1:
            accepted = unregister_fenced(epoch)
            if stale:
                assert accepted is False, (
                    f"step {step}: stale unregister(epoch={epoch}) accepted, stored={recorded_max}"
                )
            else:
                assert accepted is True, (
                    f"step {step}: unregister(epoch={epoch}) refused, stored={recorded_max}"
                )
                # The pop RAISES the record to this caller's epoch.
                recorded_max = epoch
        elif op == 2:
            assert register_legacy(payload) is None
        else:
            unregister_legacy()

        assert epoch_map.get(KEY) == recorded_max, (
            f"step {step}: record is {epoch_map.get(KEY)!r}, expected {recorded_max!r} (op={op}, epoch={epoch})"
        )


def test_epoch_record_never_decreases(registry):
    _drive_lattice(
        registry._worker_epochs,
        lambda epoch, payload: registry.register_worker(
            SESSION, WORKER, payload, INSTANCE, epoch=epoch),
        lambda epoch: registry.unregister_worker(SESSION, WORKER, INSTANCE, epoch=epoch),
        lambda payload: registry.register_worker(SESSION, WORKER, payload, INSTANCE),
        lambda: registry.unregister_worker(SESSION, WORKER, INSTANCE),
    )
    _drive_lattice(
        registry._bus_epochs,
        lambda epoch, payload: registry.register_event_bus(
            SESSION, WORKER, payload, INSTANCE, epoch=epoch),
        lambda epoch: registry.unregister_event_bus(SESSION, WORKER, INSTANCE, epoch=epoch),
        lambda payload: registry.register_event_bus(SESSION, WORKER, payload, INSTANCE),
        lambda: registry.unregister_event_bus(SESSION, WORKER, INSTANCE),
    )
