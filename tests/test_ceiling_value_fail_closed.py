"""Pin the VALUE-direction fail policy of ``apply_workspace_ceiling``.

An unknown / mistyped ceiling *level* for a KNOWN resource must DENY that
resource (fail CLOSED) -- a typo in a security ceiling must never silently
mean "no restriction".  An unknown ceiling *resource* keeps the separate
fail-OPEN policy: it is logged and ignored so forward-compatible workspace
maps never break session resolution.

These tests are narrow on purpose: they pin only the value-direction policy
(plus the one legacy spelling alias and the two no-op cases), not the whole
ceiling matrix that ``tests/test_workspace_permissions.py`` covers.
"""

from __future__ import annotations

import logging

from security.security_gate import apply_workspace_ceiling


def test_unknown_level_fails_closed_and_warns(caplog):
    """``filesystem: mega`` denies filesystem and says so at WARNING."""
    with caplog.at_level(logging.WARNING):
        result = apply_workspace_ceiling(
            {"filesystem": "mega"}, {"filesystem": "write"}
        )
    assert result == {"filesystem": "banned"}
    assert any(
        "invalid workspace ceiling level" in r.message
        and "filesystem" in r.message
        for r in caplog.records
    ), [r.message for r in caplog.records]


def test_unknown_level_on_network_denies_instead_of_leaving_grant(caplog):
    """``network: super_allow`` is on no scale (network: banned/ask/write/outbound),
    so the permissive session grant is DENIED rather than left standing."""
    with caplog.at_level(logging.WARNING):
        result = apply_workspace_ceiling(
            {"network": "super_allow"}, {"network": "write"}
        )
    assert result == {"network": "banned"}
    assert "write" not in result.values()


def test_unknown_resource_is_still_fail_open(caplog):
    """An unknown ceiling RESOURCE is logged and ignored -- the session value stands."""
    session = {"filesystem": "write"}
    with caplog.at_level(logging.WARNING):
        result = apply_workspace_ceiling({"not_a_resource": "banned"}, session)
    assert result == {"filesystem": "write"}
    assert result is not session
    assert any(
        "unknown resource 'not_a_resource'" in r.message for r in caplog.records
    ), [r.message for r in caplog.records]


def test_empty_ceiling_map_leaves_session_untouched(caplog):
    """No ceilings at all: a defensive copy of the session, no WARNINGs."""
    session = {"filesystem": "write", "git": "write"}
    with caplog.at_level(logging.WARNING):
        result = apply_workspace_ceiling({}, session)
    assert result == session
    assert result is not session
    assert caplog.records == []


def test_legacy_write_feature_branches_alias_on_git_bans_elsewhere():
    """The retired ``write_feature_branches`` spelling translates where its
    canonical target is legal (git) and fails CLOSED everywhere else."""
    assert apply_workspace_ceiling(
        {"git": "write_feature_branches"}, {"git": "write"}
    ) == {"git": "write_on_feature_branch"}
    assert apply_workspace_ceiling(
        {"filesystem": "write_feature_branches"}, {"filesystem": "write"}
    ) == {"filesystem": "banned"}


def test_stray_full_ceiling_caps_to_write_where_write_is_legal(caplog):
    """A stray ``full`` ceiling translates onto ``write`` where that level is
    legal, so the grant is CAPPED rather than bricked.

    ``full`` is a legal ceiling level only on ``mcp`` (see
    ``WORKSPACE_CEILING_VOCAB``); on git/filesystem/network it is not, so the
    alias guard rewrites it to ``write`` -- the resource keeps its write-tier
    grant instead of being denied outright, and no fail-closed WARNING is
    emitted.  ``network``'s unlimited token is ``outbound``, not ``full``, so
    even there the rewritten ``write`` (rank 3.0) still caps an ``outbound``
    (rank 4.0) grant.
    """
    with caplog.at_level(logging.WARNING):
        assert apply_workspace_ceiling({"git": "full"}, {"git": "write"}) == {
            "git": "write"
        }
        assert apply_workspace_ceiling(
            {"filesystem": "full"}, {"filesystem": "write"}
        ) == {"filesystem": "write"}
        assert apply_workspace_ceiling({"network": "full"}, {"network": "outbound"}) == {
            "network": "write"
        }
    assert caplog.records == [], [r.message for r in caplog.records]


def test_full_ceiling_is_untouched_where_full_is_a_legal_level(caplog):
    """``full`` IS a legal ``mcp`` level, so the alias guard must not fire.

    The guard is two-way: it rewrites an out-of-vocabulary spelling only when
    the canonical target is itself legal for that key.  ``mcp`` keeps ``full``
    (unlimited), so a permissive mcp grant stands and nothing is denied --
    contrast a ``full`` ceiling on a grain with no write tier, which has no
    legal target and therefore still fails CLOSED (the same path the
    ``write_feature_branches`` alias test above exercises on filesystem).
    """
    with caplog.at_level(logging.WARNING):
        assert apply_workspace_ceiling({"mcp": "full"}, {"mcp": "full"}) == {"mcp": "full"}
        assert apply_workspace_ceiling({"mcp": "full"}, {"mcp": "connect"}) == {
            "mcp": "connect"
        }
    assert caplog.records == [], [r.message for r in caplog.records]

