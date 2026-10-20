"""A record's OWN recorded grant must drive drift detection.

The two drift call sites (the container-record routes helper and the boot-time
drift scan) used to resolve policy with ``permissions={}`` -- the
framework-default profile -- instead of the grant the record itself carries.
Every record then resolved to the defaults, so a record created with a wider
grant reported phantom policy drift against its own recorded intent.

This test pins the consequence that matters: a record whose recorded intent
matches what its own grant still resolves to is CLEAN.
"""

from __future__ import annotations

from types import SimpleNamespace

from thoughtmachine.container_record import drift
from thoughtmachine.container_record.snapshot import snapshot_from_attrs
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities

#: A grant wide enough to resolve to "bridge" / "rw" (needs permissive caps).
WIDE_PERMS = {
    "filesystem": "write",
    "network": "write",
    "container": True,
    "git": "write",
    "mcp": "connect",
    "host_bash": "banned",
    "ci": "banned",
}


# -- Fakes / helpers ----------------------------------------------------------


class FakeContainer:
    """Duck-typed Docker container (read-only surface only)."""

    def __init__(self, cid, attrs, labels=None):
        self.id = cid
        self.attrs = attrs
        self.labels = labels or {}


class FakeContainers:
    """Duck-typed Docker client exposing ``list(all=...)``."""

    def __init__(self, items=None):
        self._items = list(items or [])

    def list(self, all=False):
        return list(self._items)


def _record(**overrides):
    """Build a minimal record-like object for the pure classifier."""
    base = {
        "id": "rec-1",
        "docker_id": "",
        "intent_snapshot": {},
        "inferred": False,
        "lifecycle_class": "ephemeral",
        "permissions": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _attrs(*, network="none", rw=True):
    """Build a Docker inspect ``attrs`` payload (shape consumed by snapshot.py)."""
    return {
        "HostConfig": {"NetworkMode": network, "Memory": 536870912, "OomScoreAdj": 0},
        "Config": {"Image": "python:3.12"},
        "Image": "sha256:deadbeef",
        "Mounts": [{"Destination": "/workspace", "RW": rw}],
    }


def _permissive_caps():
    """Fully-permissive caps so the resolved effective dict == the input grants."""
    return WorkspaceCapabilities(
        filesystem_write=True,
        allow_docker=True,
        allow_network=True,
        git_available=True,
    )


# -- The record's own grant is honoured: no phantom drift ----------------------


def test_permission_honoured_record_reports_no_drift():
    """A record recorded wide and still resolving wide is clean.

    Passing the record's OWN recorded grant (rather than the framework default)
    is what makes this quiet: the old ``permissions={}`` call sites resolved
    every record to the framework-default profile, so this record reported
    phantom policy drift even though nothing had changed.
    """
    snap = snapshot_from_attrs(_attrs(network="bridge", rw=True))
    record = _record(
        docker_id="d1",
        intent_snapshot=dict(snap),
        permissions=dict(WIDE_PERMS),
    )

    findings = drift.detect_record_drift(
        record,
        FakeContainers([FakeContainer("d1", _attrs(network="bridge", rw=True))]),
        permissions=drift.record_permissions(record),
        capabilities=_permissive_caps(),
    )

    assert findings == []


# -- The helper the call sites use: every record shape ------------------------


def test_record_permissions_reads_every_record_shape():
    """Objects, serialised dicts and unknown grants all yield the real grant."""
    grants = dict(WIDE_PERMS)

    # A record-like object: its own grant comes back.
    assert drift.record_permissions(_record(permissions=grants)) == grants

    # A serialised record dict: the SAME grant, never a silent ``{}`` -- that
    # silent ``{}`` is exactly the phantom-drift bug this commit fixes.
    assert drift.record_permissions({"permissions": grants}) == grants

    # Pre-v5 record: an unknown grant degrades to the framework-default ceiling.
    assert drift.record_permissions(_record(permissions=None)) == {}

    # Present but not a mapping: degrade to ``{}``, never raise or leak through.
    assert drift.record_permissions(_record(permissions="network=write")) == {}
    assert drift.record_permissions({"permissions": ["network=write"]}) == {}

    # The result is a copy: mutating it must not mutate the record's grant.
    copy = drift.record_permissions(_record(permissions=grants))
    copy["network"] = "banned"
    assert grants["network"] == "write"
