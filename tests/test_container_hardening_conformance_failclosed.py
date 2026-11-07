"""RED-first pin: unreadable docker attrs must NOT read as fully conformant.

``infra/container_create.py::_hardening_conformance`` classifies a container as
fully conformant by returning an EMPTY failing-axis list (``[]``).  When the
container's ``attrs`` cannot be read at all the predicate historically swallowed
the read error and returned that same ``[]`` -- so "we could not verify the
hardening" was indistinguishable from "the hardening is fully verified".

That is the fail-open hole this module pins shut: the unreadable case must
return a DISTINCT, non-empty verdict (the sentinel ``"attrs_unreadable"``) so a
caller can tell "cannot verify" apart from "verified", while a genuinely
hardened container still returns ``[]`` and a real axis failure still returns
that axis name.

Pure-predicate tests -- no docker daemon, no manager, no vault.
"""

from __future__ import annotations

import infra.container_create as cc


class _RaisingAttrs:
    """A container whose ``attrs`` property RAISES on every access."""

    @property
    def attrs(self):
        raise RuntimeError("attrs unavailable")


class _FakeContainer:
    """A container whose ``attrs`` is a plain dict."""

    def __init__(self, attrs):
        self.attrs = attrs


def _hardened_attrs(user):
    """attrs for a container that is FULLY conformant on all four axes."""
    return {
        "HostConfig": {
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "ReadonlyRootfs": True,
        },
        "Config": {"User": user},
    }


def test_unreadable_attrs_is_not_reported_as_fully_conformant():
    """The failing-axis list must NOT be empty when attrs cannot be read."""
    failures = cc._hardening_conformance(
        _RaisingAttrs(), cc._expected_hardening_recipe()
    )
    # Before the fix this was ``[]`` == "fully conformant" -- the fail-open bug.
    assert failures != []
    assert failures == ["attrs_unreadable"]


def test_unreadable_verdict_is_a_distinct_sentinel():
    """The unreadable verdict must be the disjoint sentinel, not a real axis."""
    failures = cc._hardening_conformance(
        _RaisingAttrs(), cc._expected_hardening_recipe()
    )
    real_axes = {"cap_drop", "security_opt", "read_only", "user"}
    assert set(failures).isdisjoint(real_axes)


def test_conformant_container_still_reports_empty():
    """A genuinely hardened container is STILL the empty (fully conformant) list."""
    recipe = cc._expected_hardening_recipe()
    good = _FakeContainer(_hardened_attrs(recipe.user))
    assert cc._hardening_conformance(good, recipe) == []


def test_real_axis_failure_is_reported_not_the_sentinel():
    """A present-but-wrong cap_drop is REAL drift, never the unreadable sentinel."""
    recipe = cc._expected_hardening_recipe()
    weak = _FakeContainer({
        "HostConfig": {"CapDrop": ["NET_RAW"]},
        "Config": {"User": recipe.user},
    })
    failures = cc._hardening_conformance(weak, recipe)
    assert failures == ["cap_drop"]
    assert "attrs_unreadable" not in failures
