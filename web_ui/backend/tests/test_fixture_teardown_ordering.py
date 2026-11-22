"""Guard the teardown ordering of the autouse fixtures in this conftest.

``_webui_thread_bridge_census`` is declared ``@pytest.fixture(autouse=True)``,
so it participates in the fixture graph of EVERY test in this directory.  That
makes every fixture it depends on a *global* ordering hazard: when an autouse
fixture requests a function-scoped fixture such as ``monkeypatch``, pytest
moves that fixture's SETUP earlier (it must exist before the fixture that uses
it) and, because finalizers run in reverse setup order, pushes its UNDO later
-- past the teardown of every autouse fixture set up after it.

That is the defect class that cost us a red CI run in ``tests/``: there, the
census fixture's ``monkeypatch`` dependency made
``monkeypatch.setattr(worker_runtime, "_RUNTIME_HANDLES", <garbage>)`` undo
*after* a module-level autouse fixture had already re-read that global, so the
teardown saw the garbage and raised ``AttributeError``
(``tests/test_worker_runtime_seam.py:39``).  ``tests/conftest.py`` fixes it by
patching and restoring ``WebAgentBridge.__init__`` by hand; this module pins
the same property for this suite's copy of the fixture, so re-introducing the
``monkeypatch`` dependency here goes red instead of latent.

This module asserts the ORDERING property only -- no product code, no census
internals: an autouse fixture that tears down must never observe a value that
a still-active ``monkeypatch`` from the test left behind.
"""

from __future__ import annotations

import pytest

# A module global dict guarded the way a registry global is guarded: snapshot
# before ``yield``, re-read after it.
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
        "web_ui/backend/tests/conftest.py reordered monkeypatch's teardown.  "
        "Attributes read in a teardown (`.keys()` here) then raise "
        "AttributeError." % (_STATE["registry"],)
    )
    # The reason the identity above matters: teardowns call methods on it.
    assert set(_STATE["registry"].keys()) == set(snapshot.keys())


def test_monkeypatch_replaced_global_is_undone_before_autouse_teardown(
    monkeypatch,
) -> None:
    """Replace the guarded global with garbage for the duration of the test."""
    monkeypatch.setitem(_STATE, "registry", None)
    assert _STATE["registry"] is None
