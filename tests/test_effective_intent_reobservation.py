"""Unit tests for post-create effective-intent re-observation.

``intent_snapshot`` is FILL-ONCE, which left one blind spot: a container that is
re-created (or restarted) keeps the authored snapshot even when what actually
came up differs from it.  ``_reobserve_effective_intent`` repairs exactly that,
value-by-value, right after a container has been created for / attached to a
record.  These tests pin its three rules:

1. agreement keeps the record's value -- including ``mem_limit``, where the
   authored ``'1g'`` and the live ``'1073741824'`` are the same limit and the
   authored spelling survives (the drift report must not gain a false positive);
2. a genuine disagreement takes the OBSERVED value;
3. keys the payload does not report are left alone, and a key the record never
   carried is never ADDED (fill-once still owns the key SET).

All storage access is mocked: no vault, no Docker daemon.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

import infra.container_manager as cm


def _attrs(network_mode="none", memory=None, cpu_quota=None, mount_rw=True,
           restart="unless-stopped"):
    """A minimal Docker inspect payload (the shape ``snapshot_from_attrs`` reads)."""
    return {
        "HostConfig": {
            "NetworkMode": network_mode,
            "Memory": memory,
            "CpuQuota": cpu_quota,
            "RestartPolicy": {"Name": restart},
        },
        "Mounts": [{"Destination": "/workspace", "RW": mount_rw}],
        "Config": {"Image": "agent-executor:test"},
    }


def _record(snapshot):
    return SimpleNamespace(intent_snapshot=snapshot)


def _run(snapshot, attrs, record_id="rec-1"):
    """Drive the helper against mocked storage; return (merged, update_mock)."""
    with patch.object(cm, "load_record", return_value=_record(snapshot)), \
         patch.object(cm, "update_record") as update, \
         patch.object(cm, "log"):
        merged = cm._reobserve_effective_intent(
            "ws-1", record_id, SimpleNamespace(attrs=attrs)
        )
    return merged, update


class TestMemBytes:
    """``_mem_bytes`` compares two SPELLINGS of the same limit."""

    @pytest.mark.parametrize("value,expected", [
        ("1g", 1024 ** 3),
        ("1gb", 1024 ** 3),
        ("512m", 512 * 1024 ** 2),
        ("512MB", 512 * 1024 ** 2),
        ("1024", 1024),
        ("1024b", 1024),
        (1073741824, 1073741824),
        ("1073741824", 1073741824),
    ])
    def test_parses_known_spellings(self, value, expected):
        assert cm._mem_bytes(value) == expected

    @pytest.mark.parametrize("value", [
        None, "", "   ", "junk", "1q", "-1g", -1, True, False, 1.5, [], {}
    ])
    def test_returns_none_for_unusable_values(self, value):
        assert cm._mem_bytes(value) is None


class TestReobservation:
    def test_same_limit_different_spelling_keeps_authored_value(self):
        """'1g' vs live '1073741824' is NOT drift: no write, spelling preserved."""
        merged, update = _run(
            {"mem_limit": "1g", "network_mode": "none"},
            _attrs(memory=1073741824),
        )
        assert merged["mem_limit"] == "1g"
        assert merged["network_mode"] == "none"
        update.assert_not_called()

    def test_genuine_disagreement_takes_observed_value(self):
        merged, update = _run(
            {"network_mode": "bridge", "mem_limit": "1g"},
            _attrs(network_mode="none", memory=1073741824),
        )
        assert merged["network_mode"] == "none"
        assert merged["mem_limit"] == "1g"
        update.assert_called_once()
        args, kwargs = update.call_args
        assert args[0] == "ws-1" and args[1] == "rec-1"
        assert kwargs["intent_snapshot"]["network_mode"] == "none"

    def test_different_limit_is_drift(self):
        merged, update = _run(
            {"mem_limit": "512m"},
            _attrs(memory=1024 ** 3),
        )
        assert merged["mem_limit"] == str(1024 ** 3)
        update.assert_called_once()

    def test_missing_key_is_never_added(self):
        """Fill-once owns the key SET: an absent key stays absent."""
        merged, update = _run(
            {"network_mode": "none"},
            _attrs(network_mode="none", memory=1073741824, cpu_quota=100000),
        )
        assert "mem_limit" not in merged
        assert "cpu_quota" not in merged
        update.assert_not_called()

    def test_unreported_values_are_left_alone(self):
        """An empty live value (not visible in the payload) never overwrites."""
        merged, update = _run(
            {"mem_limit": "1g", "cpu_quota": 100000},
            _attrs(memory=None, cpu_quota=None),
        )
        assert merged["mem_limit"] == "1g"
        assert merged["cpu_quota"] == 100000
        update.assert_not_called()

    def test_missing_record_is_a_noop(self):
        with patch.object(cm, "load_record", return_value=None), \
             patch.object(cm, "update_record") as update, \
             patch.object(cm, "log"):
            merged = cm._reobserve_effective_intent(
                "ws-1", "rec-1", SimpleNamespace(attrs=_attrs())
            )
        assert merged is None
        update.assert_not_called()

    def test_blank_ids_are_a_noop(self):
        with patch.object(cm, "load_record") as load, \
             patch.object(cm, "log"):
            assert cm._reobserve_effective_intent("", "rec-1", None) is None
            assert cm._reobserve_effective_intent("ws-1", "", None) is None
        load.assert_not_called()

    def test_storage_fault_is_swallowed_and_logged(self):
        """A container create must never fail on this best-effort repair."""
        with patch.object(cm, "load_record", side_effect=RuntimeError("vault down")), \
             patch.object(cm, "log") as log_mock:
            merged = cm._reobserve_effective_intent(
                "ws-1", "rec-1", SimpleNamespace(attrs=_attrs())
            )
        assert merged is None
        assert log_mock.called
        assert "effective-intent re-observation skipped" in log_mock.call_args[0][2]
