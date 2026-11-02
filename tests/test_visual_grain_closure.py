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


def test_visual_view_ranks_at_the_read_tier():
    # ``view`` is the visual equivalent of ``read``: it must order exactly with
    # ``read`` in BOTH projections, or the ceiling maths flips sign.
    assert GRANT_LEVEL_RANKS["view"] == GRANT_LEVEL_RANKS["read"] == 1
    assert WORKSPACE_CEILING_LEVELS_RANKS["view"] == 1.0
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


def test_workspace_ceiling_at_ask_does_not_cap_view():
    # ``ask`` (rank 2.0) is *more permissive* than ``view`` (rank 1.0): a
    # session ``view`` grant stands unchanged.
    capped = apply_workspace_ceiling({"visual": "ask"}, {"visual": "view"})
    assert capped["visual"] == "view"
