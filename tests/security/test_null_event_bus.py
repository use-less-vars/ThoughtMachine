"""
test_null_event_bus.py — NullEventBus contract tests.

Verifies that:

1. ``NullEventBus.ask()`` returns ``"deny"`` instantly (no blocking).
2. ``check_required_categories`` with ``event_bus=NullEventBus()`` and
   an ``"ask"`` permission level returns denied immediately (no 120s timeout).
3. ``NullEventBus`` as ``event_bus=None`` still works for callers that
   do not need the prompt path (allow/deny-only checks).
"""

from __future__ import annotations

import sys
import time

import pytest

# ── Fix sys.path for Docker sandbox ──────────────────────────────────────
# Pytest inserts ``tests/`` at the front of ``sys.path``, so any bare
# ``import security`` finds ``/workspace/tests/security/`` first and caches
# it in ``sys.modules['security']`` with the wrong `__file__`.
# Fix: remove tests dir from sys.path, and put /tmp/stubs (stub agent
# package) first to avoid loading the heavy dependency chain.
_bad_prefix = "/workspace/tests"
sys.path = [p for p in sys.path if not p.startswith(_bad_prefix)]

_stubs_path = "/tmp/stubs"
if _stubs_path in sys.path:
    sys.path.remove(_stubs_path)
if "/workspace" in sys.path:
    sys.path.remove("/workspace")
sys.path.insert(0, _stubs_path)
sys.path.insert(1, "/workspace")


from agent.events import NullEventBus
from security.security_gate import (
    check_required_categories,
)


# ══════════════════════════════════════════════════════════════════════════
#  NullEventBus unit tests
# ══════════════════════════════════════════════════════════════════════════


class TestNullEventBusUnit:
    """Direct unit tests on NullEventBus itself."""

    def test_ask_returns_deny(self):
        """NullEventBus.ask() returns 'deny'."""
        bus = NullEventBus()
        result = bus.ask(("title", "message"))
        assert result == "deny"

    def test_ask_is_instant(self):
        """NullEventBus.ask() completes in under 0.1 seconds (no blocking)."""
        bus = NullEventBus()
        start = time.perf_counter()
        bus.ask(("some request", "details"))
        elapsed = time.perf_counter() - start
        assert elapsed < 0.1, (
            f"NullEventBus.ask() took {elapsed:.3f}s — should be instant"
        )

    def test_publish_does_not_raise(self):
        """NullEventBus.publish() silently discards without error."""
        bus = NullEventBus()
        bus.publish(None)
        bus.publish("anything")
        bus.publish(42)

    def test_publish_dict_does_not_raise(self):
        """NullEventBus.publish_dict() silently discards without error."""
        bus = NullEventBus()
        bus.publish_dict({})
        bus.publish_dict({"type": "test", "data": "value"})

    def test_subscribe_does_not_raise(self):
        """NullEventBus.subscribe() silently accepts without error."""
        bus = NullEventBus()
        bus.subscribe(None, lambda e: None)
        bus.subscribe()

    def test_unsubscribe_does_not_raise(self):
        """NullEventBus.unsubscribe() silently accepts without error."""
        bus = NullEventBus()
        bus.unsubscribe(None, lambda e: None)


# ══════════════════════════════════════════════════════════════════════════
#  Integration: check_required_categories × NullEventBus
# ══════════════════════════════════════════════════════════════════════════


class TestNullEventBusIntegration:
    """
    Integration tests that pass a real ``NullEventBus`` to the gate.

    The critical behaviour: when the effective permission is ``"ask"``,
    the gate would normally block on ``response_queue.get(timeout=120.0)``
    waiting for a human.  With ``NullEventBus`` (or ``None``) it must
    return ``(False, ...)`` immediately instead.
    """

    def test_ask_with_null_event_bus_returns_denied_instantly(self):
        """
        effective=``{"network": "ask"}`` + ``event_bus=NullEventBus()``
        → denied in under 1 second (no 120s wait).
        """
        eff = {"network": "ask"}
        bus = NullEventBus()

        start = time.perf_counter()
        ok, msg = check_required_categories(
            ["network:true"],
            eff,
            "NetworkTool",
            {},
            "Test tool requiring approval",
            bus,
        )
        elapsed = time.perf_counter() - start

        assert ok is False, "Should be denied with NullEventBus"
        assert elapsed < 1.0, (
            f"NullEventBus prompt path took {elapsed:.3f}s "
            f"— should return instantly without blocking"
        )
        assert "no interactive user available" in msg.lower(), (
            f"Message should explain why: {msg}"
        )

    def test_ask_with_event_bus_none_returns_denied_instantly(self):
        """
        effective=``{"network": "ask"}`` + ``event_bus=None``
        → denied in under 1 second (same early-exit path).
        """
        eff = {"network": "ask"}

        start = time.perf_counter()
        ok, msg = check_required_categories(
            ["network:true"],
            eff,
            "NetworkTool",
            {},
            "Test tool requiring approval",
            None,
        )
        elapsed = time.perf_counter() - start

        assert ok is False, "Should be denied with event_bus=None"
        assert elapsed < 1.0, (
            f"event_bus=None prompt path took {elapsed:.3f}s "
            f"— should return instantly without blocking"
        )
        assert "no interactive user available" in msg.lower(), (
            f"Message should explain why: {msg}"
        )

    def test_ask_multi_category_with_null_bus(self):
        """
        Multiple 'ask' categories with NullEventBus → all denied immediately.
        """
        eff = {"filesystem": "ask", "network": "ask"}
        bus = NullEventBus()

        start = time.perf_counter()
        ok, msg = check_required_categories(
            ["filesystem:write", "network:true"],
            eff,
            "MultiTool",
            {},
            "Multiple asks",
            bus,
        )
        elapsed = time.perf_counter() - start

        assert ok is False
        assert elapsed < 1.0
        # Message should mention all ask categories
        assert "filesystem:write" in msg
        assert "network:true" in msg

    def test_permission_footprint_still_apply_before_null_bus_check(self):
        """
        Worker permissions are applied *before* the NullEventBus early-exit,
        so if permission_footprint makes the required access impossible, the
        gate returns a plain deny (not the NullEventBus message).
        """
        eff = {"network": "write"}  # normally fine
        bus = NullEventBus()

        ok, msg = check_required_categories(
            ["network:write"],
            eff,
            "WriteTool",
            {},
            "",
            bus,
            permission_footprint={"network": "read"},
        )
        # network:write is narrowed to read → denied straight away
        assert ok is False
        assert "denied" in msg.lower()

    def test_existing_callers_still_work(self):
        """
        Callers that pass ``event_bus=None`` and do **not** hit the
        ``"ask"`` path continue to work exactly as before.
        """
        eff = {"filesystem": "write", "network": True}

        # allow
        ok, msg = check_required_categories(
            ["filesystem:write"], eff, "Tool", {}, "", None
        )
        assert ok is True

        # deny
        ok2, msg2 = check_required_categories(
            ["filesystem:read"],
            {"filesystem": "banned"},
            "Tool",
            {},
            "",
            None,
        )
        assert ok2 is False

        # Multiple categories, all allowed
        ok3, _ = check_required_categories(
            ["filesystem:write", "network:true"],
            eff,
            "Tool",
            {},
            "",
            None,
        )
        assert ok3 is True

    def test_null_bus_with_no_ask_requirements(self):
        """
        When effective permissions are sufficient (no 'ask' triggered),
        NullEventBus behaves identically to None — returns (True, "").
        """
        eff = {"filesystem": "write"}
        bus = NullEventBus()

        ok, msg = check_required_categories(
            ["filesystem:write"], eff, "WriteTool", {}, "", bus
        )
        assert ok is True
        assert msg == ""
