"""Tests for ``tools.capability_registry`` (design doc Section 8.2).

The registry is pure data plus one classmethod, so every test here injects its
own name universe: no global tool map is touched and no spawn machinery runs.
"""

from __future__ import annotations

import pytest

from tools.capability_registry import CapabilityRegistry, CapabilityResolveError


# --------------------------------------------------------------------------- #
# Fake tool classes and an injected name universe
# --------------------------------------------------------------------------- #
class FileEditor:
    pass


class GlobTool:
    pass


class DockerCodeRunner:
    pass


class Worker:
    pass


_LOOKUP = {
    "DockerCodeRunner": DockerCodeRunner,
    "FileEditor": FileEditor,
    "GlobTool": GlobTool,
    "Worker": Worker,
}

_AVAILABLE = tuple(sorted(_LOOKUP))
_BLOCKLIST = ("Worker",)
_IMPLICIT_NAMES = ("DockerCodeRunner", "FileEditor", "GlobTool")


def _resolve(declared, **kwargs):
    """``CapabilityRegistry.resolve`` with the fake universe pre-injected."""
    kwargs.setdefault("available_names", _AVAILABLE)
    kwargs.setdefault("lookup", _LOOKUP)
    kwargs.setdefault("blocklist", _BLOCKLIST)
    return CapabilityRegistry.resolve(declared, **kwargs)


# --------------------------------------------------------------------------- #
# 1. Explicit subset: exactly the declared names are granted.
# --------------------------------------------------------------------------- #
def test_explicit_subset_granted():
    reg = _resolve(["GlobTool", "FileEditor"])

    assert reg.names == ("FileEditor", "GlobTool")  # sorted, not declaration order
    assert len(reg) == 2
    assert "FileEditor" in reg
    assert "DockerCodeRunner" not in reg
    assert reg.declared_empty is False
    assert reg.implicit is False
    assert reg.describe()["source"] == "explicit"


# --------------------------------------------------------------------------- #
# 2. Empty declaration keeps the historical "everything" meaning, but is
#    reported as implicit rather than silently privileged.
# --------------------------------------------------------------------------- #
def test_empty_declaration_is_implicit_everything():
    reg = _resolve([])

    assert reg.names == _IMPLICIT_NAMES
    assert reg.declared_empty is True
    assert reg.implicit is True
    assert reg.describe()["source"] == "implicit"
    assert reg.describe()["size"] == 3


# --------------------------------------------------------------------------- #
# 3. The blocklist is additive to the subset, never replaced by it.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("declared", [["Worker", "GlobTool"], []])
def test_blocklist_always_excluded(declared):
    reg = _resolve(declared)

    assert "Worker" not in reg
    assert "Worker" not in reg.resolved
    assert reg.get("Worker") is None
    assert "Worker" in reg.excluded


# --------------------------------------------------------------------------- #
# 4. A name this build does not ship fails the spawn loudly.
# --------------------------------------------------------------------------- #
def test_missing_tool_raises():
    with pytest.raises(CapabilityResolveError) as excinfo:
        _resolve(["GlobTool", "NoSuchTool"])

    assert excinfo.value.missing_names == ("NoSuchTool",)
    assert "NoSuchTool" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 5. Multiple missing names are reported sorted.
# --------------------------------------------------------------------------- #
def test_multiple_missing_tools_sorted():
    with pytest.raises(CapabilityResolveError) as excinfo:
        _resolve(["ZetaTool", "GlobTool", "AlphaTool"])

    assert excinfo.value.missing_names == ("AlphaTool", "ZetaTool")
    assert "AlphaTool" in str(excinfo.value)
    assert "ZetaTool" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 6. A worker is never wider than its parent.
# --------------------------------------------------------------------------- #
def test_parent_clamp_narrows_to_parent():
    reg = _resolve(
        ["FileEditor", "GlobTool", "DockerCodeRunner"],
        parent_enabled_tools=["FileEditor", "GlobTool"],
    )

    assert reg.names == ("FileEditor", "GlobTool")
    assert "DockerCodeRunner" not in reg
    assert "DockerCodeRunner" not in reg.resolved
    # The parent removed it: neither the template nor the build refused it, but
    # the narrowing step is still NAMED in the audit record (Section 7.2 pt 3).
    assert reg.parent_denied == ("DockerCodeRunner",)
    assert reg.describe()["parent_denied"] == ["DockerCodeRunner"]
    assert "DockerCodeRunner" not in reg.excluded
    assert "DockerCodeRunner" not in reg.unavailable
    assert "DockerCodeRunner" not in reg.permission_denied


# --------------------------------------------------------------------------- #
# 7. Permission veto is soft: the worker is built without the name.
# --------------------------------------------------------------------------- #
def test_permission_veto_is_soft():
    reg = _resolve(
        ["FileEditor", "GlobTool", "DockerCodeRunner"],
        permission_veto=lambda name: name == "DockerCodeRunner",
    )

    assert reg.names == ("FileEditor", "GlobTool")
    assert reg.permission_denied == ("DockerCodeRunner",)
    assert "DockerCodeRunner" not in reg.resolved
    assert reg.get("FileEditor") is FileEditor


# --------------------------------------------------------------------------- #
# 8. Look-up is final: no fallback to a process-wide tool map.
# --------------------------------------------------------------------------- #
def test_get_returns_none_for_ungranted_names():
    reg = _resolve(["FileEditor"])

    assert reg.get("GlobTool") is None
    assert reg.get("not_registered") is None
    assert reg.get("FileEditor") is FileEditor


# --------------------------------------------------------------------------- #
# 9. The resolved set is frozen and there is no mutation API.
# --------------------------------------------------------------------------- #
def test_registry_is_immutable():
    reg = _resolve(["FileEditor"])

    with pytest.raises(TypeError):
        reg.resolved["GlobTool"] = GlobTool

    for bucket in (
        "names",
        "excluded",
        "unavailable",
        "permission_denied",
        "parent_denied",
    ):
        assert isinstance(getattr(reg, bucket), tuple)

    for forbidden in ("add", "remove", "register", "update", "grant", "extend"):
        assert not hasattr(reg, forbidden)


# --------------------------------------------------------------------------- #
# 10. Registries are independent: no shared state, no late widening.
# --------------------------------------------------------------------------- #
def test_registries_are_independent():
    lookup = dict(_LOOKUP)
    narrow = _resolve(["FileEditor"], lookup=lookup)
    wide = _resolve(["FileEditor", "GlobTool"], lookup=lookup)

    assert narrow.names == ("FileEditor",)
    assert wide.names == ("FileEditor", "GlobTool")

    lookup["ExtraTool"] = Worker  # mutating the caller's map afterwards
    assert "ExtraTool" not in narrow
    assert "GlobTool" not in narrow
    assert narrow.get("ExtraTool") is None
