"""Closure tests for the ``visual`` session-permission grain.

``visual`` is the sixth-turned-ninth canonical session grain
(``banned | ask | view``): it gates the visual/render artifact tool.  This
module pins the whole chain that a new grain must join, because every hop in
it is a *hand-maintained* mirror of ``security.resource_catalog``:

1. the canonical catalog (``RESOURCE_CATALOG``) and the two derived rank
   projections (``GRANT_LEVEL_RANKS`` / ``WORKSPACE_CEILING_LEVELS_RANKS``);
2. the workspace-ceiling vocabulary (``WORKSPACE_CEILING_VOCAB``);
3. the session model + defaults (``SessionPermissions`` / ``SAFE_DEFAULTS``);
4. the effective-permission merge (``get_effective_permissions``);
5. the workspace-ceiling application (``apply_workspace_ceiling``).

Hop 5 is the one that silently no-ops: ``_WORKSPACE_RESOURCE_MAP`` in
``security/security_gate.py`` is hand-written, while ``_WORKSPACE_CEILING_KEYS``
is DERIVED from ``RESOURCE_CATALOG``.  A catalog key missing from the map is
therefore *accepted* as a ceiling name and then skipped, so a workspace ceiling
of ``banned`` on that grain never caps the session grant.  The assertion in
``test_workspace_ceiling_caps_visual_grant`` is what catches that.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from security.resource_catalog import (  # noqa: E402
    GRANT_LEVEL_RANKS,
    RESOURCE_CATALOG,
    WORKSPACE_CEILING_LEVELS_RANKS,
    WORKSPACE_CEILING_VOCAB,
)
from security.security_gate import (  # noqa: E402
    _WORKSPACE_RESOURCE_MAP,
    apply_workspace_ceiling,
    get_effective_permissions,
)
from thoughtmachine.security import (  # noqa: E402
    SAFE_DEFAULTS,
    SessionPermissions,
)
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities  # noqa: E402


# ---------------------------------------------------------------------------
# (a) the visual grain is present in every hand-maintained mirror
# ---------------------------------------------------------------------------


def test_visual_in_canonical_catalog():
    assert RESOURCE_CATALOG["visual"] == ["banned", "ask", "view"]


def test_visual_view_ranks_above_the_ask_tier():
    # ``view`` is NOT the read tier (it ranked there until FINDING 1 was fixed):
    # at the read tier it sits at or below ``_ASK_SILENT_MAX_RANK``, so an
    # ``ask`` grant is silently satisfied by it and the interactive-ask branch
    # is unreachable for the grain.  It must therefore rank strictly ABOVE
    # ``ask`` in BOTH projections, and the two tables must agree (a grant rank
    # and its ceiling rank differ only for the documented ``write_on_feature_branch``
    # / ``outbound`` exceptions), or the ceiling maths flips sign.
    assert GRANT_LEVEL_RANKS["view"] > GRANT_LEVEL_RANKS["ask"]
    assert GRANT_LEVEL_RANKS["view"] == 2.5
    assert WORKSPACE_CEILING_LEVELS_RANKS["view"] == GRANT_LEVEL_RANKS["view"]
    assert WORKSPACE_CEILING_LEVELS_RANKS["read"] == 1.0


def test_visual_in_workspace_ceiling_vocab():
    assert WORKSPACE_CEILING_VOCAB["visual"] == ("banned", "ask", "view")


def test_visual_in_session_model_and_safe_defaults():
    assert set(SAFE_DEFAULTS) == set(RESOURCE_CATALOG)
    assert SAFE_DEFAULTS["visual"] == "banned"
    assert SessionPermissions().visual == "banned"


def test_visual_in_workspace_resource_map():
    assert _WORKSPACE_RESOURCE_MAP["visual"] == "visual"


def test_effective_permissions_expose_visual():
    eff = get_effective_permissions(
        SessionPermissions(visual="view"), WorkspaceCapabilities()
    )
    assert eff["visual"] == "view"


# ---------------------------------------------------------------------------
# (b) the workspace ceiling actually caps the visual grain
# ---------------------------------------------------------------------------


def test_workspace_ceiling_caps_visual_grant():
    """A ``banned`` workspace ceiling must cap a ``view`` session grant.

    This is the regression guard for the ``_WORKSPACE_RESOURCE_MAP`` gap: with
    no ``visual`` entry the resource is silently skipped and the grant survives
    the ceiling (fail-OPEN) -- which is exactly what this assertion denies.
    """
    capped = apply_workspace_ceiling({"visual": "banned"}, {"visual": "view"})
    assert capped["visual"] == "banned"


def test_workspace_ceiling_at_view_allows_view():
    capped = apply_workspace_ceiling({"visual": "view"}, {"visual": "view"})
    assert capped["visual"] == "view"


def test_workspace_ceiling_at_ask_caps_view_to_ask():
    """An ``ask`` workspace ceiling now CAPS a ``view`` session grant to ``ask``.

    ``view`` ranks at 2.5, strictly above ``ask`` (2.0), so the ceiling is the
    more restrictive side and the "lower rank wins" rule in
    ``apply_workspace_ceiling`` rewrites the grant to ``ask``.  Before the rank
    change ``view`` ranked at the read tier (1.0), so this exact call returned
    ``view`` untouched and the ceiling was a no-op above the grant.

    The capped value is prompt-or-deny, never a pass: ``ask`` no longer
    satisfies a ``view`` requirement silently, so the value reaches the ask
    branch -- an interactive prompt when a user exists, an immediate deny when
    none does (worker context).
    """
    capped = apply_workspace_ceiling({"visual": "ask"}, {"visual": "view"})
    assert capped["visual"] == "ask"

    from security.gate_helpers import _value_satisfies
    from security.security_gate import check_required_categories
    from tools.visual_tool import VisualTool

    # The capped value yields the ASK sentinel for a ``view`` requirement --
    # not ``True`` -- so the decision cannot be a silent pass.
    assert _value_satisfies("view", capped["visual"]) == "ASK"
    assert _value_satisfies("view", capped["visual"]) is not True

    allowed, message = check_required_categories(
        VisualTool.required_categories,
        {**capped, "container": True},
        "VisualTool",
        {},
        "d",
        is_worker_context=True,
    )
    assert allowed is False
    assert "ask requires interactive approval" in message


# ---------------------------------------------------------------------------
# (c) FINDING 1: ``ask`` must not be silently satisfied by the ``view`` rank
# ---------------------------------------------------------------------------


def test_ask_is_not_silently_satisfied_by_the_view_rank():
    """Unit pin: ``_value_satisfies("view", "ask")`` is NOT ``True``.

    ``view`` used to rank at the read tier (1), i.e. at or below
    ``_ASK_SILENT_MAX_RANK`` (2 = max(banned, read, ask)), so an ``ask``
    allowed value satisfied a ``view`` requirement SILENTLY: the interactive-ask
    branch in ``security/security_gate.py`` was unreachable for this grain and
    ``visual: ask`` behaved exactly like ``visual: view``.  ``view`` now ranks
    strictly above the silent tier, so the helper returns its ``"ASK"``
    sentinel -- the sentinel, not ``False``: the gate turns ``"ASK"`` into a
    prompt, or into an immediate deny when no interactive user exists.  Pinning
    the rank against ``_ASK_SILENT_MAX_RANK`` (rather than a literal) is what
    stops a future rank change quietly restoring the no-op.
    """
    from security.gate_helpers import _ASK_SILENT_MAX_RANK, _value_satisfies

    assert GRANT_LEVEL_RANKS["view"] > _ASK_SILENT_MAX_RANK
    assert _value_satisfies("view", "ask") == "ASK"
    assert _value_satisfies("view", "ask") is not True

    # The rest of the visual scale is unchanged.
    assert _value_satisfies("view", "view") is True
    assert _value_satisfies("view", "banned") is False
