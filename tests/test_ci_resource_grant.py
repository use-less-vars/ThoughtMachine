"""RED-first tests for the new ``ci`` session-permission resource (read-only).

These pin that ``ci`` is a canonical grant resource: declared on
``SessionPermissions`` (default ``banned``), present in ``SAFE_DEFAULTS``
(keyed off ``RESOURCE_CATALOG`` so it can never drift), exposed as an
effective-permission key, and enforced by ``check_required_categories`` -- a
``ci:read`` requirement is allowed with a ``ci: read`` grant and denied
otherwise.  The split-on-first-colon semantics and the unknown-category denial
string are pinned too.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from thoughtmachine.security import SessionPermissions, SAFE_DEFAULTS
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities
from security.resource_catalog import RESOURCE_CATALOG
from security.security_gate import (
    check_required_categories,
    get_effective_permissions,
)


def _eff(session=None):
    return get_effective_permissions(
        session if session is not None else SessionPermissions(),
        WorkspaceCapabilities(),
    )


def test_session_permissions_ci_read():
    assert SessionPermissions(ci="read").ci == "read"


def test_session_permissions_ci_default_banned():
    assert SessionPermissions().ci == "banned"


def test_safe_defaults_ci_banned():
    assert SAFE_DEFAULTS["ci"] == "banned"


def test_safe_defaults_keyed_off_catalog():
    assert set(SAFE_DEFAULTS) == set(RESOURCE_CATALOG)


def test_effective_permissions_exposes_ci_read():
    eff = get_effective_permissions(
        SessionPermissions(ci="read"), WorkspaceCapabilities()
    )
    assert eff["ci"] == "read"


def test_effective_permissions_ci_default_banned():
    assert _eff()["ci"] == "banned"


def test_ci_read_requirement_allowed_with_grant():
    eff = get_effective_permissions(
        SessionPermissions(ci="read"), WorkspaceCapabilities()
    )
    ok, msg = check_required_categories(
        ["ci:read"], eff, "DummyCiTool", {}, "read ci", event_bus=None
    )
    assert ok is True and msg == ""


def test_ci_read_requirement_denied_without_grant():
    eff = _eff()  # ci defaults to banned
    ok, msg = check_required_categories(
        ["ci:read"], eff, "DummyCiTool", {}, "read ci", event_bus=None
    )
    assert ok is False
    assert msg == (
        "Permission denied: Tool requires ci:read, but session allows ci:banned"
    )


def test_ci_requirement_splits_on_first_colon_only():
    # "ci:read" -> category "ci", value "read" (matches the grant).
    eff = get_effective_permissions(
        SessionPermissions(ci="read"), WorkspaceCapabilities()
    )
    ok, _ = check_required_categories(
        ["ci:read"], eff, "DummyCiTool", {}, "d", event_bus=None
    )
    assert ok is True
    # "ci: read" -> value " read" (leading space) does NOT match.
    ok, msg = check_required_categories(
        ["ci: read"], eff, "DummyCiTool", {}, "d", event_bus=None
    )
    assert ok is False
    assert msg == (
        "Permission denied: Tool requires ci: read, but session allows ci:read"
    )


def test_unknown_category_denied_with_exact_message():
    eff = _eff()
    ok, msg = check_required_categories(
        ["nope:read"], eff, "DummyCiTool", {}, "d", event_bus=None
    )
    assert ok is False
    assert msg == "Permission denied: Unknown category 'nope' required by tool."
