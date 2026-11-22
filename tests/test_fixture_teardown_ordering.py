"""Guard the teardown ordering of the autouse fixtures in ``tests/conftest.py``.

The census/reaper fixture in ``tests/conftest.py`` is declared
``@pytest.fixture(autouse=True)``, so it participates in the fixture graph of
EVERY test in this directory -- not just the ones that opted into the census
assertions.  That makes every fixture it depends on a *global* ordering
hazard: when an autouse fixture requests a function-scoped fixture such as
``monkeypatch``, pytest moves that fixture's SETUP earlier (it must exist
before the fixture that uses it) and, because finalizers run in reverse setup
order, pushes its UNDO later -- past the teardown of every autouse fixture
that was set up after it.

That is exactly how ``tests/test_worker_runtime_seam.py`` started reporting
teardown errors: its module-level ``_handle_registry_teardown`` fixture
re-reads ``worker_runtime._RUNTIME_HANDLES`` after ``yield``, and the census
fixture's ``monkeypatch`` dependency made the undo of
``monkeypatch.setattr(worker_runtime, "_RUNTIME_HANDLES", <garbage>)`` run
*after* that re-read, so the fixture tore down while looking at the garbage.

This module asserts the ORDERING property only -- no product code, no census
internals: an autouse fixture that tears down must never observe a value that
a still-active ``monkeypatch`` from the test left behind.
"""

from __future__ import annotations

import pytest

# A module global dict guarded the same way the seam module guards its handle
# registry: snapshot before ``yield``, re-read after it.
_STATE = {"registry": {"sess": "handle"}}


@pytest.fixture(autouse=True)
def _state_teardown():
    """Snapshot the module global, then require it to be untouched at teardown."""
    snapshot = _STATE["registry"]
    yield
    assert _STATE["registry"] is snapshot, (
        "teardown of an autouse fixture observed %r instead of the dict this "
        "fixture snapshotted at setup: a still-active `monkeypatch` from the "
        "test had not been undone yet, i.e. an autouse fixture in "
        "tests/conftest.py reordered monkeypatch's teardown.  Attributes read "
        "in a teardown (`.keys()` here, `_RUNTIME_HANDLES.keys()` in "
        "tests/test_worker_runtime_seam.py) then raise AttributeError."
        % (_STATE["registry"],)
    )
    # The reason the identity above matters: teardowns call methods on it.
    assert set(_STATE["registry"].keys()) == set(snapshot.keys())


def test_monkeypatch_replaced_global_is_undone_before_autouse_teardown(
    monkeypatch,
) -> None:
    """Replace the guarded global with garbage for the duration of the test."""
    monkeypatch.setitem(_STATE, "registry", None)
    assert _STATE["registry"] is None
