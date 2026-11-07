"""Tests for intent-snapshot extraction (design §1.1 / §4 step 4)."""

from __future__ import annotations

import pytest

from thoughtmachine.container_record import HARDENING_KEYS, snapshot_from_attrs
from thoughtmachine.container_record.models import INTENT_SNAPSHOT_KEYS
from thoughtmachine.container_record.snapshot import hardening_from_host_config


def _payload(**overrides):
    """A docker-inspect-shaped payload; ``overrides`` patch top-level keys."""
    payload = {
        "Id": "d1",
        "Config": {"Labels": {}, "Image": "python:3.12"},
        "HostConfig": {
            "NetworkMode": "none",
            "Memory": 268435456,
            "CpuQuota": 50000,
            "OomScoreAdj": None,
        },
        "State": {"Status": "running"},
        "Image": "sha256:abc",
    }
    payload.update(overrides)
    return payload


def test_all_intent_keys_always_present_and_ordered():
    snap = snapshot_from_attrs({})
    assert list(snap) == list(INTENT_SNAPSHOT_KEYS)
    assert set(snap) == set(INTENT_SNAPSHOT_KEYS)


def test_empty_input_yields_empty_strings_and_empty_hardening():
    snap = snapshot_from_attrs({})
    assert snap["hardening"] == {}
    for key in INTENT_SNAPSHOT_KEYS:
        if key != "hardening":
            assert snap[key] == ""


def test_coercions_match_legacy_behaviour():
    snap = snapshot_from_attrs(_payload())
    assert snap["network_mode"] == "none"  # str
    assert snap["mem_limit"] == "268435456"  # str
    assert snap["cpu_quota"] == 50000  # raw, not stringified
    assert snap["oom_score_adj"] == ""  # None -> empty
    assert snap["image_ref"] == "python:3.12"
    assert snap["image_hash"] == "sha256:abc"


def test_missing_pieces_stay_empty():
    snap = snapshot_from_attrs(_payload(HostConfig={}, Config={}, Image=None))
    assert snap["network_mode"] == ""
    assert snap["mem_limit"] == ""
    assert snap["cpu_quota"] == ""
    assert snap["oom_score_adj"] == ""
    assert snap["image_ref"] == ""
    assert snap["image_hash"] == ""
    assert snap["workspace_mode"] == ""
    assert snap["hardening"] == {}


def test_cpu_quota_falsy_zero_becomes_empty():
    snap = snapshot_from_attrs(_payload(HostConfig={"CpuQuota": 0}))
    assert snap["cpu_quota"] == ""


def test_cpu_quota_nonzero_is_preserved_raw():
    snap = snapshot_from_attrs(_payload(HostConfig={"CpuQuota": 12345}))
    assert snap["cpu_quota"] == 12345


def test_oom_score_adj_is_preserved_when_not_none():
    snap = snapshot_from_attrs(_payload(HostConfig={"OomScoreAdj": -500}))
    assert snap["oom_score_adj"] == -500


def test_oom_score_adj_zero_is_kept():
    # ``0`` is not ``None`` and so is preserved (asymmetric with cpu_quota).
    snap = snapshot_from_attrs(_payload(HostConfig={"OomScoreAdj": 0}))
    assert snap["oom_score_adj"] == 0


def test_image_ref_and_hash_are_independent():
    payload = _payload(
        Config={"Labels": {}, "Image": "python:3.12"}, Image="sha256:deadbeef"
    )
    snap = snapshot_from_attrs(payload)
    assert snap["image_ref"] == "python:3.12"
    assert snap["image_hash"] == "sha256:deadbeef"


def test_image_hash_is_not_sliced_or_hashed():
    snap = snapshot_from_attrs(_payload(Image="sha256:abc"))
    assert snap["image_hash"] == "sha256:abc"


@pytest.mark.parametrize("rw,expected", [(True, "rw"), (False, "ro")])
def test_workspace_mode_derived_from_workspace_mount(rw, expected):
    payload = _payload(Mounts=[{"Destination": "/workspace", "RW": rw}])
    assert snapshot_from_attrs(payload)["workspace_mode"] == expected


def test_workspace_mode_empty_without_mount():
    assert snapshot_from_attrs(_payload())["workspace_mode"] == ""


def test_workspace_mode_ignores_other_destinations():
    payload = _payload(Mounts=[{"Destination": "/data", "RW": True}])
    assert snapshot_from_attrs(payload)["workspace_mode"] == ""


def test_workspace_mode_non_bool_rw_is_empty():
    payload = _payload(Mounts=[{"Destination": "/workspace", "RW": "true"}])
    assert snapshot_from_attrs(payload)["workspace_mode"] == ""


def test_workspace_mode_missing_rw_is_empty():
    payload = _payload(Mounts=[{"Destination": "/workspace"}])
    assert snapshot_from_attrs(payload)["workspace_mode"] == ""


def test_hardening_keys_constant():
    assert HARDENING_KEYS == (
        "cap_drop",
        "cap_add",
        "security_opt",
        "readonly_rootfs",
    )


def test_hardening_keeps_only_truthy_managed_keys():
    host = {
        "CapDrop": ["ALL"],
        "CapAdd": [],
        "SecurityOpt": ["no-new-privileges"],
        "ReadonlyRootfs": True,
    }
    snap = snapshot_from_attrs(_payload(HostConfig=host))
    assert snap["hardening"] == {
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges"],
        "readonly_rootfs": True,
    }


def test_hardening_from_host_config_ignores_unknown_keys():
    host = {"CapDrop": ["ALL"], "NotManaged": "x", "ReadonlyRootfs": False}
    assert hardening_from_host_config(host) == {"cap_drop": ["ALL"]}


def test_hardening_from_host_config_non_mapping():
    assert hardening_from_host_config(None) == {}
    assert hardening_from_host_config("nope") == {}


@pytest.mark.parametrize("bad", [None, [], "string", 42, object()])
def test_snapshot_never_raises_on_non_mapping(bad):
    snap = snapshot_from_attrs(bad)
    assert set(snap) == set(INTENT_SNAPSHOT_KEYS)
    assert snap["hardening"] == {}


def test_snapshot_tolerates_malformed_submappings():
    payload = {
        "HostConfig": "not-a-dict",
        "Config": 123,
        "Mounts": "not-a-list",
        "Image": None,
    }
    snap = snapshot_from_attrs(payload)
    assert snap["network_mode"] == ""
    assert snap["image_ref"] == ""
    assert snap["image_hash"] == ""
    assert snap["workspace_mode"] == ""
    assert snap["hardening"] == {}


def test_snapshot_tolerates_bad_mount_entries():
    payload = _payload(
        Mounts=[None, "x", 5, {"Destination": "/workspace", "RW": True}]
    )
    assert snapshot_from_attrs(payload)["workspace_mode"] == "rw"


def test_snapshot_performs_no_io(monkeypatch):
    """Extraction touches no filesystem, vault or environment state."""
    import builtins
    import io
    import os
    import pathlib

    def _boom(*args, **kwargs):
        raise AssertionError(
            "snapshot_from_attrs performed IO via %r / %r" % (args, kwargs)
        )

    monkeypatch.setattr(builtins, "open", _boom)
    monkeypatch.setattr(io, "open", _boom)
    monkeypatch.setattr(os, "getenv", _boom)
    monkeypatch.setattr(pathlib.Path, "open", _boom, raising=False)
    # Patch the environ mapping's read hooks without swapping os.environ itself.
    environ_type = type(os.environ)
    monkeypatch.setattr(environ_type, "__getitem__", _boom)
    monkeypatch.setattr(environ_type, "get", _boom)

    payload = _payload(Mounts=[{"Destination": "/workspace", "RW": True}])
    snap = snapshot_from_attrs(payload)
    assert snap["workspace_mode"] == "rw"
