"""``_workspace_dir`` refuses identifiers that are not a single path segment.

Every vault path for a workspace is derived from
``thoughtmachine.workspace_capabilities._workspace_dir``.  Before this guard,
``_workspace_dir("..")`` produced ``<vault>/workspaces/..`` and
``_workspace_dir("a/b")`` produced a path two levels outside the workspace
namespace, so any caller forwarding an unvalidated id -- a route parameter, a
registry entry, a config value -- could read and write outside
``<vault>/workspaces/``.

The check lives in ``_workspace_dir`` and nowhere else: one fact, one
authority (Inv.6).  Reserved ids are refused here too, so ``global``
(``workspace_registry.RESERVED_WORKSPACE_IDS``) can never acquire a directory
in the workspace namespace.
"""

from __future__ import annotations

import inspect

import pytest

import thoughtmachine.workspace_capabilities as wc
from thoughtmachine.workspace_capabilities import (
    _capabilities_path,
    _user_dir,
    _workspace_dir,
    ensure_workspace_dirs,
    git_shadow_mask_path,
)
from thoughtmachine.workspace_registry import RESERVED_WORKSPACE_IDS


# Each of these, concatenated into ``<vault>/workspaces/<id>``, either escapes
# the workspace namespace or names something the namespace does not own.
INVALID_IDS = [
    "",           # empty: path collapses to <vault>/workspaces
    ".",          # the namespace directory itself
    "..",         # the vault root
    "../escape",  # explicit traversal
    "a/b",        # second segment
    r"a\b",       # Windows separator, second segment on Windows
    "/",          # absolute-lookalike
    chr(92),      # bare backslash
    "..foo",      # substring ".." inside a single segment
    "foo..",
    "global",     # RESERVED_WORKSPACE_IDS
]

VALID_IDS = ["ws-1", "test-ws", "My_Workspace.2", "a", "0"]


@pytest.mark.parametrize("bad", INVALID_IDS)
def test_workspace_dir_refuses_invalid_identifier(bad):
    with pytest.raises(ValueError):
        _workspace_dir(bad)


@pytest.mark.parametrize("bad", INVALID_IDS)
def test_capabilities_path_refuses_invalid_identifier(bad):
    with pytest.raises(ValueError):
        _capabilities_path(bad)


@pytest.mark.parametrize("bad", INVALID_IDS)
def test_git_shadow_mask_path_refuses_invalid_identifier(bad):
    with pytest.raises(ValueError):
        git_shadow_mask_path(bad)


@pytest.mark.parametrize("bad", INVALID_IDS)
def test_ensure_workspace_dirs_refuses_before_touching_disk(bad, tmp_path, monkeypatch):
    monkeypatch.setattr(wc, "_user_dir", lambda: tmp_path)
    with pytest.raises(ValueError):
        ensure_workspace_dirs(bad)
    assert not (tmp_path / "workspaces").exists(), (
        "a rejected identifier must not create anything under <vault>/workspaces"
    )


@pytest.mark.parametrize("bad", sorted(RESERVED_WORKSPACE_IDS))
def test_reserved_ids_are_refused_by_name(bad):
    with pytest.raises(ValueError, match="reserved"):
        _workspace_dir(bad)


@pytest.mark.parametrize("good", VALID_IDS)
def test_valid_identifier_resolves_under_the_workspace_namespace(good):
    assert _workspace_dir(good) == _user_dir() / "workspaces" / good


def test_signature_is_unchanged():
    assert list(inspect.signature(_workspace_dir).parameters) == ["workspace_id"]


def test_credential_name_validation_is_shared_not_duplicated():
    """The rule is one authority, not two copies (Inv.6)."""
    from thoughtmachine.validation import validate_single_path_segment
    from web_ui.backend.global_routes import _validate_credential_name

    assert validate_single_path_segment is not None
    assert _validate_credential_name("") == "credential name must not be empty"
    assert (
        _validate_credential_name("..")
        == "credential name must be a single path segment"
    )
    assert (
        _validate_credential_name("a/b")
        == "credential name must be a single path segment"
    )
    assert (
        _validate_credential_name(chr(92))
        == "credential name must be a single path segment"
    )
    assert _validate_credential_name("ok-name") is None
    assert (
        _validate_credential_name("x" * 129)
        == "credential name must be at most 128 characters"
    )
