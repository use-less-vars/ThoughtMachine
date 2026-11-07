"""Composition test: resize -> recreate -> re-observe -> record -> drift.

The ``mem_limit`` spelling question spans two modules that each own one half of
the story and must agree about it:

* ``infra.container_manager._reobserve_effective_intent`` compares the record's
  authored ``mem_limit`` (``'1g'``, what the resources route wrote) with the
  live inspect value (``str(HostConfig.Memory)`` -> ``'1073741824'``);
* ``thoughtmachine.container_record.drift`` compares the RECORDED intent with
  the live container on every drift scan.

Both use the same parser (``snapshot.mem_limit_bytes``) so that "the same limit
spelled differently" is never drift, while a genuine difference still is.  The
unit tests in ``test_effective_intent_reobservation.py`` and
``test_container_record_drift.py`` pin each half; this file pins the JOIN: the
container that a resize just re-created is re-observed and then scanned by the
real drift detector, and the scan must be quiet -- and must NOT stay quiet when
the live limit really differs.

Storage is faked (an in-memory record), the drift detector, the config
resolution and the parser are the real ones.  No Docker daemon, no vault.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

import infra.container_manager as cm
from thoughtmachine.container_record import drift

WS = "ws-1"
RECORD_ID = "rec-1"
#: The container that the resize re-created (a NEW id: recreate mints one).
CONTAINER_ID = "cid-2"
GIB = 1024 ** 3


# ── Fakes / helpers ───────────────────────────────────────────────────────


def _attrs(memory, network_mode="none", restart="unless-stopped"):
    """A minimal Docker inspect payload (the shape ``snapshot_from_attrs`` reads)."""
    return {
        "HostConfig": {
            "NetworkMode": network_mode,
            "Memory": memory,
            "CpuQuota": 100000,
            "RestartPolicy": {"Name": restart},
        },
        "Mounts": [{"Destination": "/workspace", "RW": True}],
        "Config": {"Image": "agent-executor:test"},
    }


class _LiveContainer:
    """Duck-typed container: only the read-only surface drift touches."""

    def __init__(self, attrs, cid=CONTAINER_ID):
        self.id = cid
        self.attrs = attrs
        self.labels = {}


class _LiveClient:
    """Duck-typed Docker client exposing ``list(all=True)``."""

    def __init__(self, *containers):
        self._containers = list(containers)

    def list(self, all=False):  # noqa: A002 - mirrors the docker SDK signature
        return list(self._containers)


class _RecordStore:
    """In-memory stand-in for ``load_record`` / ``update_record``."""

    def __init__(self, intent_snapshot):
        self.record = SimpleNamespace(
            id=RECORD_ID,
            docker_id=CONTAINER_ID,
            lifecycle_class="ephemeral",
            restart_policy="unless-stopped",
            intent_snapshot=dict(intent_snapshot),
        )
        self.updates = []

    def load_record(self, workspace_id, record_id):
        return self.record if record_id == RECORD_ID else None

    def update_record(self, workspace_id, record_id, **kwargs):
        self.updates.append(kwargs)
        if "intent_snapshot" in kwargs:
            self.record.intent_snapshot = dict(kwargs["intent_snapshot"])


def _reobserve(store, container):
    """Run the real re-observation against the faked storage."""
    with patch.object(cm, "load_record", store.load_record), \
         patch.object(cm, "update_record", store.update_record), \
         patch.object(cm, "log"):
        return cm._reobserve_effective_intent(WS, RECORD_ID, container)


def _runtime_findings(record, container):
    """Run the real drift detector; return only its runtime findings."""
    findings = drift.detect_record_drift(
        record, _LiveClient(container), permissions=None, capabilities=None
    )
    return [f for f in findings if f.drift_class == drift.CLASS_RUNTIME]


# ── The composition ───────────────────────────────────────────────────────


def test_resize_recreate_reobserve_then_drift_is_quiet_for_the_same_limit():
    """``'1g'`` authored by the resize vs ``'1073741824'`` live is not drift.

    Before normalisation this path produced a permanent, unfixable runtime
    finding on every scan of a container whose limit was authored with a unit
    suffix -- the record kept ``'1g'`` and the daemon kept reporting bytes.
    """
    store = _RecordStore({"network_mode": "none", "mem_limit": "1g"})
    container = _LiveContainer(_attrs(memory=GIB))

    merged = _reobserve(store, container)

    # (1) re-observation keeps the authored spelling and writes nothing.
    assert merged["mem_limit"] == "1g"
    assert store.updates == []
    assert store.record.intent_snapshot["mem_limit"] == "1g"
    # (2) the drift scan over that same record and container reports no
    #     runtime mismatch -- the two owners agree about the same limit.
    assert _runtime_findings(store.record, container) == []


def test_reobservation_repairs_a_recreate_that_did_not_apply_the_resize():
    """A resize that silently did NOT take effect is corrected, then quiet.

    The authored ``'512m'`` did not reach the daemon (the container still
    reports 1 GiB).  Re-observation records what actually came up, so the drift
    scan of the repaired record is clean instead of reporting the operator's
    intent as a permanent lie.
    """
    store = _RecordStore({"network_mode": "none", "mem_limit": "512m"})
    container = _LiveContainer(_attrs(memory=GIB))

    merged = _reobserve(store, container)

    assert merged["mem_limit"] == str(GIB)
    assert store.updates and store.updates[0]["intent_snapshot"]["mem_limit"] == str(GIB)
    assert store.record.intent_snapshot["mem_limit"] == str(GIB)
    assert _runtime_findings(store.record, container) == []


def test_drift_still_reports_a_limit_changed_behind_the_record_back():
    """The regression guard: normalisation must not blanket-suppress mem_limit.

    After the re-observation above the record says 1 GiB; somebody then narrows
    the live container with ``docker update``.  That IS drift, and the finding
    still carries the RAW expected/actual values.
    """
    store = _RecordStore({"network_mode": "none", "mem_limit": "1g"})
    recreated = _LiveContainer(_attrs(memory=GIB))
    _reobserve(store, recreated)

    narrowed = _LiveContainer(_attrs(memory=512 * 1024 ** 2))

    findings = _runtime_findings(store.record, narrowed)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.event_type == drift.EVENT_RUNTIME_MISMATCH
    assert finding.expected == "1g"
    assert finding.actual == str(512 * 1024 ** 2)


@pytest.mark.parametrize("authored", ["1g", "1024m", str(GIB), "1073741824b"])
def test_every_spelling_of_the_same_limit_survives_the_round_trip(authored):
    """Whatever spelling the operator authored, one limit => zero findings."""
    store = _RecordStore({"network_mode": "none", "mem_limit": authored})
    container = _LiveContainer(_attrs(memory=GIB))

    _reobserve(store, container)

    assert store.record.intent_snapshot["mem_limit"] == authored
    assert _runtime_findings(store.record, container) == []
