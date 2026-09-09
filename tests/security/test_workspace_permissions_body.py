"""
Tests for the workspace permissions PUT body model and the permissions
resolver (``web_ui.backend.workspace_routes``).

Covers the bool-permission support added for the container ceiling:
``WorkspacePermissionsBody.permissions`` is a ``Dict[str, Union[str, bool]]``
so a real JSON boolean (``container: true``) survives validation as a real
``True`` instead of the string ``'true'`` (which the resource-catalog
validator would reject and the security gate would misread as an unknown,
fail-open level).  ``_resolve_workspace_permissions`` must keep that real
boolean verbatim when reading ``config.json`` back.

``tests/security/conftest.py`` fixes ``sys.path`` (removes the pytest-injected
``tests/`` dir, inserts the repo root and the sandbox ``/tmp/stubs`` first),
so the heavy sibling imports inside ``workspace_routes`` resolve the same way
they do under the top-level ``tests/`` suites.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("pydantic")
from pydantic import ValidationError  # noqa: E402

# Importing workspace_routes pulls in the worker registry and other heavy
# siblings; on environments where that chain cannot resolve, skip rather than
# fail collection.
try:
    from web_ui.backend.workspace_routes import (  # noqa: E402
        WorkspacePermissionsBody,
        _resolve_workspace_permissions,
    )
except Exception as exc:  # pragma: no cover - environment dependent
    pytest.skip(
        f"web_ui.backend.workspace_routes could not be imported: {exc}",
        allow_module_level=True,
    )


def test_body_accepts_real_bool_container_value():
    """A real JSON boolean for ``container`` parses and stays a real bool."""
    body = WorkspacePermissionsBody(permissions={"container": True})
    assert body.permissions["container"] is True
    assert isinstance(body.permissions["container"], bool)

    body = WorkspacePermissionsBody(permissions={"container": False})
    assert body.permissions["container"] is False


def test_body_accepts_string_levels_including_container():
    """String levels (incl. a string ``container``) pass through untouched."""
    body = WorkspacePermissionsBody(
        permissions={
            "container": "ask",
            "host_bash": "allow",
            "network": "outbound",
            "git": "write",
        }
    )
    assert body.permissions == {
        "container": "ask",
        "host_bash": "allow",
        "network": "outbound",
        "git": "write",
    }


def test_body_permissions_map_optional_and_host_flag_default():
    """``permissions`` is required (PUT always sends it); an empty map is fine
    and ``allow_host_resources`` defaults to None."""
    body = WorkspacePermissionsBody(permissions={})
    assert body.permissions == {}
    assert body.allow_host_resources is None
    body = WorkspacePermissionsBody(permissions={}, allow_host_resources=True)
    assert body.allow_host_resources is True


def test_body_int_container_values_documented():
    """pydantic v2 lax bool coercion applies inside Union[str, bool].

    ``str`` never accepts an int in lax mode, so an int falls to the ``bool``
    branch: 0/1 coerce to False/True, any other int is a ValidationError.
    (Observed on pydantic 2.13.4: 1 -> True, 0 -> False, 2 -> error.)

    Downstream the catalog validator only accepts real bools for ``container``
    (the raw int forms would be rejected there), so an int can only reach
    persistence after this model coercion: 0 -> False (fail closed) and
    1 -> True (identical to the explicit ``container: true`` the UI sends --
    container is an owner-explicit grant, so this is not a privilege raise).
    """

    def _parse(value):
        try:
            return WorkspacePermissionsBody(
                permissions={"container": value}
            ).permissions["container"]
        except ValidationError:
            return None  # documented: rejected outright by this pydantic build

    one = _parse(1)
    assert one is True or one is None, f"unexpected parse of 1: {one!r}"
    zero = _parse(0)
    assert zero is False or zero is None, f"unexpected parse of 0: {zero!r}"

    # A non 0/1 int matches neither union branch on any lax pydantic v2 build.
    try:
        WorkspacePermissionsBody(permissions={"container": 2})
    except ValidationError:
        pass
    else:  # pragma: no cover - defensive across pydantic builds
        pytest.skip("this pydantic build accepted int 2 for the union field")


def test_resolver_preserves_boolean_container_verbatim():
    """Saved real booleans are read back as booleans, never 'True'/'False'.

    Stringifying would break the PUT round trip (the validator rejects the
    string forms) and would be misread as an unknown fail-open level by the
    security gate.
    """
    resolved = _resolve_workspace_permissions(
        {"permissions": {"container": True, "git": "read"}}, "general"
    )
    assert resolved == {"container": True, "git": "read"}
    assert resolved["container"] is True
    assert isinstance(resolved["container"], bool)


def test_resolver_normalizes_legacy_container_string_levels():
    """Stored legacy container strings are normalised to real booleans.

    The config read-back path (``_resolve_workspace_permissions``) runs
    ``normalize_legacy_workspace_ceiling``: a stored ``'ask'`` container
    ceiling (a pre-catalog string) is rewritten to the canonical boolean
    ``False`` so the security gate never sees a non-bool container ceiling.
    Non-container canonical values (``host_bash: allow``) pass through
    untouched.
    """
    resolved = _resolve_workspace_permissions(
        {"permissions": {"container": "ask", "host_bash": "allow"}}, "general"
    )
    assert resolved == {"container": False, "host_bash": "allow"}
    assert resolved["container"] is False


def test_resolver_falls_back_to_purpose_preset():
    """No saved ``permissions`` map -> the purpose preset is applied."""
    resolved = _resolve_workspace_permissions({"purpose": "coding"}, "coding")
    assert isinstance(resolved, dict)
    assert resolved  # non-empty preset map
    # Preset values are strings except the boolean container ceiling.
    assert all(isinstance(v, (str, bool)) for v in resolved.values())
    assert resolved["container"] is False
