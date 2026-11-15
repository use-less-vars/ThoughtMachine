"""test_permission_hierarchy.py -- deny > ask > allow across every ceiling grain.

The workspace ceiling is a hard cap: the effective grant for a grain is the
more restrictive of the session grant and the workspace ceiling, and the one
ordering that rule implies -- ``banned`` (deny) stricter than ``ask`` stricter
than the grain's most permissive ("allow") level -- is pinned here grain by
grain.

Everything is expressed on the REAL vocabularies
(``security.resource_catalog.WORKSPACE_CEILING_VOCAB`` /
``RESOURCE_CATALOG``) instead of hand-rolled tokens, so a token or grain added
to the catalog without a matching hierarchy decision fails
``test_matrix_covers_exactly_the_real_vocabularies`` rather than being
silently skipped.

Properties under test
---------------------
1. MATRIX -- for every (grain, session level, ceiling level) the effective
   value is the rank-``min`` of the two on
   ``WORKSPACE_CEILING_LEVELS_RANKS``, emitted as the ceiling's OWN token when
   the ceiling lowers the session grant (so a ``write_on_feature_branch``
   ceiling caps ``write`` to branch-restricted write, never unlimited, and an
   ``ask`` ceiling over a session ``read`` leaves it ``read`` -- the documented
   ask inversion).
2. ORDER -- a more permissive ceiling is never stricter in effect, and the
   effective grant never exceeds either the session grant or the ceiling
   (deny > ask > allow, for every grain).
3. END TO END -- ``get_effective_permissions`` (the real resolution entry
   point, non-disk branch) returns exactly the matrix values, so the cap
   reaches consumers and not merely ``apply_workspace_ceiling``.

Two documented deviations from a plain rank-``min`` are pinned explicitly
rather than fudged by the table:

* ``host_bash`` -- an ``ask`` ceiling over an ``allow`` grant emits ``banned``,
  never ``ask``: prompts come only from genuine session-level ``ask`` grants,
  so a ceiling may never fabricate one.
* ``container`` -- boolean grain: ``True`` survives only write-level or
  unlimited ceilings, and the key is always re-emitted as a bool.
"""

import pytest

from security.resource_catalog import (
    RESOURCE_CATALOG,
    WORKSPACE_CEILING_LEVELS_RANKS,
    WORKSPACE_CEILING_VOCAB,
)
from security.security_gate import apply_workspace_ceiling, get_effective_permissions
from thoughtmachine.security import SessionPermissions
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities

_RANKS = WORKSPACE_CEILING_LEVELS_RANKS

#: ``host_bash`` ranks on its OWN scale (``banned < ask < allow``): ``allow`` is
#: not a generic ceiling level, so it is absent from the shared rank table and
#: must be ranked locally (mirroring the gate's dedicated host_bash branch).
_HOST_BASH_RANKS = {"banned": 0.0, "ask": 1.0, "allow": 2.0}


def _rank(grain, level):
    """Rank *level* on *grain*'s own ceiling scale."""
    if grain == "host_bash":
        return _HOST_BASH_RANKS[level]
    return _RANKS[level]

#: Grains whose ceiling/session levels are STRINGS.  ``container`` is the
#: boolean grain and is covered by the dedicated tests below.
_GRAINS = (
    "filesystem",
    "git",
    "network",
    "mcp",
    "host_bash",
    "credential",
    "visual",
    "ci",
)

#: The grain's most permissive ("allow") level.  Derived from the real ceiling
#: vocabulary via the shared rank table, so "allow" means "top of this grain's
#: own scale" -- ``write`` for filesystem/git, ``outbound`` for network,
#: ``full`` for mcp, ``allow`` for host_bash, ``use`` for credential, ``view``
#: for visual, ``read`` for ci.
_MOST_PERMISSIVE = {
    grain: max(WORKSPACE_CEILING_VOCAB[grain], key=lambda lvl, g=grain: _rank(g, lvl))
    for grain in _GRAINS
}

#: ``(grain, session level) -> effective value per ceiling level``, the values
#: aligned to ``WORKSPACE_CEILING_VOCAB[grain]`` order.  This is the hierarchy
#: table: each row is one session grant, each column the ceiling that caps it.
_MATRIX = {
    ("filesystem", "banned"): ("banned", "banned", "banned", "banned"),
    ("filesystem", "ask"): ("banned", "ask", "read", "ask"),
    ("filesystem", "read"): ("banned", "read", "read", "read"),
    ("filesystem", "write"): ("banned", "ask", "read", "write"),
    ("git", "banned"): ("banned", "banned", "banned", "banned", "banned"),
    ("git", "ask"): ("banned", "ask", "read", "ask", "ask"),
    ("git", "read"): ("banned", "read", "read", "read", "read"),
    ("git", "write"): ("banned", "ask", "read", "write_on_feature_branch", "write"),
    ("git", "write_on_feature_branch"): (
        "banned",
        "ask",
        "read",
        "write_on_feature_branch",
        "write_on_feature_branch",
    ),
    ("network", "banned"): ("banned", "banned", "banned", "banned"),
    ("network", "ask"): ("banned", "ask", "ask", "ask"),
    ("network", "write"): ("banned", "ask", "write", "write"),
    ("network", "outbound"): ("banned", "ask", "write", "outbound"),
    ("mcp", "banned"): ("banned", "banned", "banned"),
    ("mcp", "connect"): ("banned", "connect", "connect"),
    ("mcp", "full"): ("banned", "connect", "full"),
    ("host_bash", "banned"): ("banned", "banned", "banned"),
    ("host_bash", "ask"): ("banned", "ask", "ask"),
    # allow + ask ceiling -> banned: a ceiling never fabricates a prompt.
    ("host_bash", "allow"): ("banned", "banned", "allow"),
    ("credential", "banned"): ("banned", "banned", "banned"),
    ("credential", "read"): ("banned", "read", "read"),
    ("credential", "use"): ("banned", "read", "use"),
    ("visual", "banned"): ("banned", "banned", "banned"),
    ("visual", "ask"): ("banned", "ask", "ask"),
    ("visual", "view"): ("banned", "ask", "view"),
    ("ci", "banned"): ("banned", "banned"),
    ("ci", "read"): ("banned", "read"),
}

#: Every real (grain, session, ceiling) triple, cross-producted from the
#: catalog itself -- never from _MATRIX, so the matrix cannot hide a gap.
_TRIPLES = [
    (grain, session, ceiling)
    for grain in _GRAINS
    for session in RESOURCE_CATALOG[grain]
    for ceiling in WORKSPACE_CEILING_VOCAB[grain]
]

_PAIRS = [(grain, session) for grain in _GRAINS for session in RESOURCE_CATALOG[grain]]

#: Grains whose ceiling vocabulary carries an ask tier (Case B applies).
_ASK_GRAINS = tuple(g for g in _GRAINS if "ask" in WORKSPACE_CEILING_VOCAB[g])

#: ``(ceiling, session) -> expected bool`` for the boolean ``container`` grain.
_CONTAINER_CASES = (
    (True, True, True),
    (False, True, False),
    (True, False, False),
    (False, False, False),
    # A write-level string ceiling allows the grant; anything stricter denies.
    ("write", True, True),
    ("read", True, False),
    ("ask", True, False),
    ("banned", True, False),
)


def _expected(grain, session, ceiling):
    """The matrix's expected effective value for one triple."""
    return _MATRIX[(grain, session)][WORKSPACE_CEILING_VOCAB[grain].index(ceiling)]


@pytest.fixture
def host_resources_allowed(monkeypatch):
    """Neutralise the workspace ``allow_host_resources`` operator veto.

    ``get_effective_permissions`` force-denies ``host_bash`` unless the
    workspace config's top-level ``allow_host_resources`` is the JSON boolean
    ``true`` (an independent, absolute policy gate).  That gate is not the
    hierarchy under test here, so it is opened for this module only; its own
    fail-closed behaviour is covered elsewhere.
    """
    monkeypatch.setattr(
        "tools.host_resource_policy.workspace_allows_host_resources",
        lambda workspace_id: True,
    )


# ── Table integrity ───────────────────────────────────────────────────────────


def test_matrix_covers_exactly_the_real_vocabularies():
    """The hierarchy table covers every real (grain, session) row, one entry
    per real ceiling level, and never invents a value outside the grain's own
    vocabulary."""
    assert set(_MATRIX) == set(_PAIRS)
    for (grain, session), row in _MATRIX.items():
        vocab = WORKSPACE_CEILING_VOCAB[grain]
        assert session in RESOURCE_CATALOG[grain]
        assert len(row) == len(vocab), (
            f"{grain}/{session}: {len(row)} effective values for "
            f"{len(vocab)} ceiling levels"
        )
        for ceiling, value in zip(vocab, row):
            assert value in vocab, (
                f"{grain}: ceiling {ceiling!r} over session {session!r} "
                f"produced a non-vocabulary value {value!r}"
            )


# ── 1. The cap matrix ────────────────────────────────────────────────────────


@pytest.mark.parametrize("grain,session,ceiling", _TRIPLES)
def test_ceiling_matrix(grain, session, ceiling):
    """Ceiling × session -> the expected effective level, for every grain."""
    got = apply_workspace_ceiling({grain: ceiling}, {grain: session})[grain]
    assert got == _expected(grain, session, ceiling)


# ── 2. The ordering: deny > ask > allow ──────────────────────────────────────


@pytest.mark.parametrize("grain,session,ceiling", _TRIPLES)
def test_effective_never_exceeds_session_or_ceiling(grain, session, ceiling):
    """A ceiling only ever restricts: the effective grant ranks at or below
    BOTH the session grant and the ceiling -- the core deny > ask > allow
    invariant, asserted for both operands so neither direction can slip."""
    got = apply_workspace_ceiling({grain: ceiling}, {grain: session})[grain]
    assert _rank(grain, got) <= _rank(grain, session), (
        "the ceiling RAISED the session grant"
    )
    assert _rank(grain, got) <= _rank(grain, ceiling), (
        "the effective grant exceeds the ceiling"
    )


@pytest.mark.parametrize("grain,session", _PAIRS)
def test_more_permissive_ceiling_is_never_stricter(grain, session):
    """Walking a grain's ceilings from strictest to most permissive, the
    effective grant is monotonically non-decreasing: deny < ask < allow."""
    ceilings = sorted(
        WORKSPACE_CEILING_VOCAB[grain], key=lambda lvl, g=grain: _rank(g, lvl)
    )
    effective_ranks = [
        _rank(grain, apply_workspace_ceiling({grain: c}, {grain: session})[grain])
        for c in ceilings
    ]
    assert effective_ranks == sorted(effective_ranks), (
        f"{grain} session {session!r}: ceilings {ceilings} produced ranks "
        f"{effective_ranks}, which is not monotone"
    )


# ── Case A: a denied ceiling denies anything ─────────────────────────────────


@pytest.mark.parametrize("grain", _GRAINS)
def test_case_a_denied_ceiling_beats_the_most_permissive_session(grain):
    """Case A -- ceiling ``banned`` over the grain's most permissive session
    grant resolves to ``banned``."""
    got = apply_workspace_ceiling(
        {grain: "banned"}, {grain: _MOST_PERMISSIVE[grain]}
    )[grain]
    assert got == "banned"


# ── Case B: an ask ceiling caps to ask (or stricter) ─────────────────────────


@pytest.mark.parametrize("grain", _ASK_GRAINS)
def test_case_b_ask_ceiling_caps_a_more_permissive_session(grain):
    """Case B -- ceiling ``ask`` over a more permissive session grant resolves
    to ``ask``, i.e. the ceiling level itself.  ``host_bash`` is the one
    documented exception: it caps to ``banned`` because it has no tier between
    ``banned`` and ``ask`` and a ceiling must never fabricate a prompt."""
    got = apply_workspace_ceiling(
        {grain: "ask"}, {grain: _MOST_PERMISSIVE[grain]}
    )[grain]
    assert "ask" in WORKSPACE_CEILING_VOCAB[grain]
    if grain == "host_bash":
        assert got == "banned", "an ask ceiling must never emit a prompt level"
    else:
        assert got == "ask"


# ── The boolean grain ────────────────────────────────────────────────────────


@pytest.mark.parametrize("ceiling,session,expected", _CONTAINER_CASES)
def test_container_boolean_hierarchy(ceiling, session, expected):
    """``container`` ranks on the same hierarchy with booleans: a deny ceiling
    denies, a write-level ceiling allows, anything stricter denies."""
    got = apply_workspace_ceiling({"container": ceiling}, {"container": session})[
        "container"
    ]
    assert got is expected


# ── 3. End to end through get_effective_permissions ──────────────────────────


@pytest.mark.parametrize("grain,session,ceiling", _TRIPLES)
def test_e2e_effective_permissions_honours_the_ceiling(
    grain, session, ceiling, host_resources_allowed
):
    """The resolved profile handed to tools carries exactly the matrix value."""
    eff = get_effective_permissions(
        SessionPermissions(**{grain: session}),
        WorkspaceCapabilities(),
        {grain: ceiling},
    )
    assert eff[grain] == _expected(grain, session, ceiling)


@pytest.mark.parametrize("grain", _GRAINS)
def test_e2e_case_a_denied_ceiling(grain, host_resources_allowed):
    eff = get_effective_permissions(
        SessionPermissions(**{grain: _MOST_PERMISSIVE[grain]}),
        WorkspaceCapabilities(),
        {grain: "banned"},
    )
    assert eff[grain] == "banned"


@pytest.mark.parametrize("grain", _ASK_GRAINS)
def test_e2e_case_b_ask_ceiling(grain, host_resources_allowed):
    eff = get_effective_permissions(
        SessionPermissions(**{grain: _MOST_PERMISSIVE[grain]}),
        WorkspaceCapabilities(),
        {grain: "ask"},
    )
    assert eff[grain] == ("banned" if grain == "host_bash" else "ask")


@pytest.mark.parametrize("ceiling,session,expected", _CONTAINER_CASES)
def test_e2e_container_boolean_ceiling(ceiling, session, expected):
    eff = get_effective_permissions(
        SessionPermissions(container=session),
        WorkspaceCapabilities(),
        {"container": ceiling},
    )
    assert eff["container"] is expected
