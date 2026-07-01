"""
Regression tests for security defaults.

Verifies:
- SessionPermissions(network) defaults to ``"banned"``.
- ``get_default_security_config()`` returns ``"deny"`` as the default policy.
- Explicit overrides for both fields still work.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from thoughtmachine.security import (
    SessionPermissions,
    get_default_security_config,
)


class TestNetworkDefault:
    """SessionPermissions.network must default to ``'banned'``."""

    def test_default_network_is_banned(self):
        """SessionPermissions() with no args has network='banned'."""
        sp = SessionPermissions()
        assert sp.network == "banned", (
            f"Expected network='banned', got {sp.network!r}"
        )

    def test_explicit_network_write_still_works(self):
        """SessionPermissions(network='write') overrides the default."""
        sp = SessionPermissions(network="write")
        assert sp.network == "write", (
            f"Expected network='write', got {sp.network!r}"
        )

    def test_explicit_network_ask_still_works(self):
        """SessionPermissions(network='ask') overrides the default."""
        sp = SessionPermissions(network="ask")
        assert sp.network == "ask", (
            f"Expected network='ask', got {sp.network!r}"
        )


class TestDefaultPolicy:
    """get_default_security_config() must have default_policy='deny'."""

    def test_default_policy_is_deny(self):
        """get_default_security_config() contains default_policy='deny'."""
        config = get_default_security_config()
        policy = config["session_policy"]["default_policy"]
        assert policy == "deny", (
            f"Expected default_policy='deny', got {policy!r}"
        )

    def test_explicit_policy_allow_still_works_via_merge(self):
        """Merging an explicit 'allow' overrides the default."""
        from thoughtmachine.security import merge_security_config

        user_config: Dict[str, Any] = {
            "session_policy": {"default_policy": "allow"},
        }
        merged = merge_security_config(user_config)
        assert merged["session_policy"]["default_policy"] == "allow", (
            "Explicit 'allow' should survive the merge"
        )

    def test_explicit_policy_deny_still_works(self):
        """Explicit 'deny' also survives the merge."""
        from thoughtmachine.security import merge_security_config

        user_config: Dict[str, Any] = {
            "session_policy": {"default_policy": "deny"},
        }
        merged = merge_security_config(user_config)
        assert merged["session_policy"]["default_policy"] == "deny"
