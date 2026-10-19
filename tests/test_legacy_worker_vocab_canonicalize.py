"""Legacy worker-definition permission vocabulary is canonicalized at LOAD time.

The worker definition's declared ``permission_footprint`` is the cap the
security gate applies on top of the live session grants.  Older definitions
persist ``network`` as a boolean (see
``thoughtmachine.security.SessionPermissions.coerce_legacy_network_permission``,
``True`` -> ``write``, ``False`` -> ``banned``).  The gate stays strict and
never learns that vocabulary, so a legacy ``True`` used to fall into the
fail-closed branch (``worker_val not in valid_levels``) and hard-deny every
network tool.  The definition LOAD boundary (``WorkerThread.__init__``) now
folds the legacy form onto the canonical level.
"""

from unittest.mock import MagicMock

import pytest

from security.security_gate import check_required_categories
from tools.workspace.worker_thread import (
    _canonicalize_worker_footprint,
    _restrictive_merge,
    WorkerThread,
)


def make_worker_thread(definition: dict, session_permissions: dict | None = None) -> WorkerThread:
    """Create a WorkerThread with minimal required params for testing."""
    return WorkerThread(
        name="test-worker",
        definition=definition,
        agent_config={"provider": "test-provider", "model": "test-model"},
        workspace_dir=MagicMock(),
        session_permissions=session_permissions or {},
        project_root="/tmp",
    )


# ── 1. legacy bool network True -> write ────────────────────────────────────
def test_legacy_bool_network_true_folds_to_write():
    """``network: True`` in a definition is loaded as the canonical ``write``."""
    wt = make_worker_thread(
        {"name": "w", "permission_footprint": {"network": True}},
        {"network": "write"},
    )
    assert wt._permission_footprint == {"network": "write"}


# ── 2. legacy bool network False -> banned ──────────────────────────────────
def test_legacy_bool_network_false_folds_to_banned():
    """``network: False`` is loaded as ``banned`` -- i.e. still a hard deny,
    but expressed in canonical vocabulary instead of an unresolvable bool."""
    wt = make_worker_thread(
        {"name": "w", "permission_footprint": {"network": False}},
        {"network": "write"},
    )
    assert wt._permission_footprint == {"network": "banned"}
    # Non-vacuity: the two legacy directions must not collapse onto one value.
    assert {"network": "write"} != wt._permission_footprint


# ── 3. end-to-end: the gate no longer hard-denies the legacy form ────────────
def test_legacy_bool_network_is_not_hard_denied_by_the_gate():
    """Before: raw legacy footprint hard-denies.  After: canonical does not."""
    wt = make_worker_thread(
        {"name": "w", "permission_footprint": {"network": True}},
        {"network": "write"},
    )
    # Non-vacuity control: the gate's own strict membership test still rejects
    # the legacy bool, so the allow below can only come from the load boundary.
    raw_allowed, raw_message = check_required_categories(
        ["network:write"],
        {"network": "write"},
        "WebSearch",
        {},
        "",
        None,
        permission_footprint={"network": True},
    )
    assert raw_allowed is False
    assert "Permission denied" in raw_message

    allowed, message = check_required_categories(
        ["network:write"],
        {"network": "write"},
        "WebSearch",
        {},
        "",
        None,
        permission_footprint=wt._permission_footprint,
    )
    assert (allowed, message) == (True, "")


# ── 4. idempotent for canonical input ───────────────────────────────────────
def test_canonical_footprint_is_loaded_unchanged_and_idempotent():
    """A canonical footprint survives the boundary verbatim, twice."""
    footprint = {"network": "outbound", "filesystem": "read", "container": True}
    session = {"network": "outbound", "filesystem": "write", "container": True}

    wt = make_worker_thread({"name": "w", "permission_footprint": footprint}, session)
    assert wt._permission_footprint == footprint
    assert _canonicalize_worker_footprint(wt._permission_footprint) == footprint
    # The spawn-time merge is unchanged for canonical input (worker stricter
    # filesystem still wins; nothing is invented or dropped).
    assert _restrictive_merge(session, wt._permission_footprint) == {
        "network": "outbound",
        "filesystem": "read",
        "container": True,
    }


# ── 5. unknown category fails LOUDLY at load time, naming key + value ───────
def test_unknown_footprint_category_raises_naming_key_and_value():
    """An unresolvable definition is a load error, never a silent deny."""
    with pytest.raises(ValueError) as excinfo:
        make_worker_thread(
            {"name": "w", "permission_footprint": {"teleport": "whenever"}},
            {"network": "write"},
        )
    message = str(excinfo.value)
    assert "'teleport'" in message
    assert "'whenever'" in message
    assert "unknown category" in message


# ── 6. legacy grains are stripped, not mapped ───────────────────────────────
def test_legacy_grains_are_stripped_from_the_declared_footprint():
    """``git_read``/``git_write``/``system``/``execution`` never reach the gate."""
    wt = make_worker_thread(
        {
            "name": "w",
            "permission_footprint": {
                "network": True,
                "filesystem": "read",
                "git_read": "read",
                "git_write": "write",
                "system": "full",
                "execution": "allow",
            },
        },
        {"network": "write", "filesystem": "write", "git": "write"},
    )
    assert wt._permission_footprint == {"network": "write", "filesystem": "read"}
