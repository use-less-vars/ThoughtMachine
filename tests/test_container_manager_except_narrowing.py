"""Tests pinning the NARROWING of two swallowing ``except`` clauses.

FINAL SCOPE (2 of the 3 candidate sites are narrowed):

  * Site 1c -- module-level ``_container_lifecycle_class``: the broad
    ``except Exception`` around ``find_by_docker_label(record_id)`` is narrowed
    to ``except ContainerRecordError`` (the record store's own error family is
    the single expected failure mode).
  * Site 3 -- ``ContainerManager._record_id_for_name``: the broad
    ``except Exception`` around ``self.client.containers.get(name)`` is narrowed
    to ``except NotFound`` (``docker.errors.NotFound``).

Site 2 -- ``ContainerManager._record_id_for`` -- is DELIBERATELY LEFT BROAD
(``except Exception``) and is therefore NOT narrowed:

  ``_record_id_for`` is a best-effort identity probe whose contract is
  "never raises; any lookup failure yields None" (see its docstring).  Its
  ``container.attrs`` input is duck-typed, and for a real docker SDK
  ``Container`` ``.attrs`` is a plain dict, so the broad clause is the intended
  implementation of that fail-soft contract -- it is also PINNED GREEN by
  ``tests/test_container_start_drift.py::test_unreadable_attrs_reuse_without_drift``
  (section "(g) fail-safe: unreadable attrs -> reuse as today, never crash").
  Narrowing it broke that existing pin, so the robust clause is retained and
  only the TOLERANT-path behaviour is asserted here.

Each narrowed clause's contract is two-sided:

  * the expected failure mode is still TOLERATED (benign fallback), and
  * an UNEXPECTED error now PROPAGATES instead of being silently swallowed.

No Docker daemon, network or ``sleep`` is involved: managers are built with
``ContainerManager.__new__`` and collaborators are monkeypatched.

ADDED PINS (hardening-conformance verdict consumed by ``_start_drift_decision``):
``_hardening_conformance`` reports a container whose attrs cannot be READ as the
DISTINCT sentinel ``["attrs_unreadable"]`` (never ``[]``);
``_start_drift_decision`` maps that sentinel EXPLICITLY to the zero-failure
REUSE outcome, while a container with REAL failing hardening axes still routes
to ``"recreate"``.  These extend the file's focus (swallowing-``except``
contracts) to the swallow in ``_hardening_conformance`` that this change fixed.
"""

from types import SimpleNamespace

import pytest

import infra.container_create as cc
import infra.container_manager as cm_mod
from infra.container_manager import ContainerManager
from thoughtmachine.container_record import (
    ContainerRecordError,
    LIFECYCLE_PERSISTENT,
    RECORD_LABEL_KEY,
)


def _manager():
    """A ``ContainerManager`` without running its (docker-touching) ctor."""
    mgr = ContainerManager.__new__(ContainerManager)
    mgr.workspace_id = "ws-narrow"
    mgr.vault_root = "/tmp/tm-narrow"
    mgr.session_id = "sess-narrow"
    mgr.workspace_path = "/tmp/tm-narrow-ws"
    mgr.session_permissions = None
    mgr.image = "agent-executor"
    mgr._containers = {}
    return mgr


# ---------------------------------------------------------------------------
# Site 2 -- ContainerManager._record_id_for  (NOT narrowed -- left broad)
#   A malformed ``attrs`` must still degrade to None rather than raise.
# ---------------------------------------------------------------------------


def test_record_id_for_malformed_attrs_is_tolerated():
    """A malformed ``Config`` (duck-typed ``.get``) degrades to None, not raise."""
    mgr = _manager()
    # ``Config`` is a str -> ``("not-a-dict" or {}).get(...)`` raises AttributeError,
    # which the deliberately-broad clause swallows (fail-soft "never raises").
    ctr = SimpleNamespace(labels=None, attrs={"Config": "not-a-dict"})
    assert mgr._record_id_for(ctr) is None


# ---------------------------------------------------------------------------
# Site 3 -- ContainerManager._record_id_for_name
#   broad ``except Exception`` around ``self.client.containers.get(name)``
#   -> narrow to ``except NotFound`` (docker.errors.NotFound)
# ---------------------------------------------------------------------------


class _NoSuchRecord(Exception):
    """Stand-in for ``docker.errors.NotFound`` (monkeypatched onto the module)."""


def _client_whose_get(behaviour):
    """A fake docker client whose ``containers.get`` runs *behaviour(name)*."""
    return SimpleNamespace(containers=SimpleNamespace(get=behaviour))


def test_record_id_for_name_notfound_is_tolerated(monkeypatch):
    """The expected 'container name absent' error is swallowed -> None."""
    monkeypatch.setattr(cm_mod, "NotFound", _NoSuchRecord)

    def _get(name):
        raise _NoSuchRecord(name)

    mgr = _manager()
    mgr.client = _client_whose_get(_get)
    assert mgr._record_id_for_name("missing-ctr") is None


def test_record_id_for_name_unexpected_error_propagates():
    """An unexpected daemon error must NOT be swallowed."""
    def _get(name):
        raise RuntimeError("daemon exploded")

    mgr = _manager()
    mgr.client = _client_whose_get(_get)
    with pytest.raises(RuntimeError):
        mgr._record_id_for_name("ctr-x")


def test_record_id_for_name_success_returns_label():
    """A resolvable name still yields its recorded id (paired happy path)."""
    def _get(name):
        return SimpleNamespace(labels={RECORD_LABEL_KEY: "rec-9"})

    mgr = _manager()
    mgr.client = _client_whose_get(_get)
    assert mgr._record_id_for_name("ctr") == "rec-9"


# ---------------------------------------------------------------------------
# Site 1c -- module-level _container_lifecycle_class
#   broad ``except Exception`` around ``find_by_docker_label(record_id)``
#   -> narrow to ``except ContainerRecordError``
# ---------------------------------------------------------------------------


def _ctr_with_record_label(rid="rec-x"):
    """A live container stand-in that carries a record label (non-resource)."""
    return SimpleNamespace(name="ctr", labels={RECORD_LABEL_KEY: rid}, image=None)


def test_lifecycle_class_tolerates_container_record_error(monkeypatch):
    """The record store's own error family degrades to PERSISTENT, not raise."""
    def _boom(label, vault_root=None):
        raise ContainerRecordError("corrupt record")

    monkeypatch.setattr(cm_mod, "find_by_docker_label", _boom)
    assert cm_mod._container_lifecycle_class(_ctr_with_record_label()) == LIFECYCLE_PERSISTENT


def test_lifecycle_class_unexpected_lookup_error_propagates(monkeypatch):
    """An unexpected error from the record lookup must NOT be swallowed."""
    def _boom(label, vault_root=None):
        raise RuntimeError("record lookup exploded")

    monkeypatch.setattr(cm_mod, "find_by_docker_label", _boom)
    with pytest.raises(RuntimeError):
        cm_mod._container_lifecycle_class(_ctr_with_record_label())


# ---------------------------------------------------------------------------
# Hardening-conformance verdicts consumed by _start_drift_decision
#   ``_hardening_conformance`` returns the DISTINCT sentinel
#   ``["attrs_unreadable"]`` when a container's attrs cannot be read at all;
#   ``_start_drift_decision`` maps that sentinel EXPLICITLY to the
#   zero-failure/reuse outcome (fail-soft), while REAL failing hardening axes
#   still route to ``"recreate"``.
# ---------------------------------------------------------------------------


class _RaisingAttrsContainer:
    """A container stand-in whose ``attrs`` property always raises."""

    id = "c" * 16
    name = "agent-x"

    @property
    def attrs(self):
        raise RuntimeError("attrs unavailable")


# Isolation on both axes MATCHES want=("none", "ro"); only the hardening axes
# are wrong, so a recreate can only be driven by the hardening verdict.
_REAL_HARDENING_DRIFT_ATTRS = {
    "State": {"Status": "running"},
    "HostConfig": {
        "NetworkMode": "none",
        "CapDrop": [],
        "SecurityOpt": [],
        "ReadonlyRootfs": False,
    },
    "Config": {"User": ""},
    "Mounts": [{"Destination": "/workspace", "RW": False}],
}


def test_hardening_conformance_unreadable_attrs_is_distinct_sentinel():
    """(a) An unreadable-attrs container yields the sentinel, NEVER ``[]``."""
    failures = cc._hardening_conformance(
        _RaisingAttrsContainer(), cc._expected_hardening_recipe()
    )
    assert failures == ["attrs_unreadable"]
    assert failures != []


def test_start_drift_decision_unverifiable_hardening_maps_to_reuse():
    """(b) The sentinel maps to the zero-failure/reuse outcome, not recreate."""
    mgr = _manager()
    decision, drift = mgr._start_drift_decision(
        _RaisingAttrsContainer(), "none", "ro", "record"
    )
    assert decision == "ok"
    assert drift is None


def test_start_drift_decision_real_hardening_drift_still_recreates():
    """A REAL weak-hardening container must still be recreated, not reused."""
    mgr = _manager()
    ctr = SimpleNamespace(
        id="c" * 16, name="agent-x", attrs=_REAL_HARDENING_DRIFT_ATTRS
    )
    decision, drift = mgr._start_drift_decision(ctr, "none", "ro", "record")
    assert decision == "recreate"
    assert set(drift["hardening"]["failed"]) == {
        "cap_drop", "security_opt", "read_only", "user"
    }
