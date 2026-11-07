"""``_resolve_workspace_path`` must validate the REGISTRY branch too.

The registry branch of ``_resolve_workspace_path`` used to return
``entry.root_path`` verbatim while only the explicit ``workspace_path`` branch
ran ``_validate_workspace_path``.  That returned value flows into
``_make_container_manager`` -> ``ContainerManager(workspace_path=...)``, i.e.
it becomes a container bind-mount source, so a registry entry rooted at the
vault (or at a path that is not a usable directory) could be mounted into a
container without ever passing the trust-anchor guard.  These tests pin the
fix: the registry fallback now goes through the same validation as the
explicit parameter, so a vault-rooted entry is refused instead of mounted.
"""
import os
from types import SimpleNamespace

import pytest

from thoughtmachine.workspace_registry import WorkspaceRegistryEntry


def _server_module():
    import web_ui.backend.server as server  # lazy: needs sys.path

    return server


def _entry(workspace_id, root_path):
    """Build a registry entry for *workspace_id* rooted at *root_path*."""
    return WorkspaceRegistryEntry(id=workspace_id, root_path=str(root_path))


@pytest.fixture
def mount_source_seam(tmp_path, monkeypatch):
    """Install a fake vault root and a fake registry singleton.

    ``_confine_to_home`` reads the vault through the module-level ``_vault``
    object of ``web_ui.backend.server``, so replacing that one attribute with a
    stub is enough to make the trust anchor hermetic -- no reloads, no real
    ``~/.thoughtmachine``.  ``install(entry)`` then pins what the registry
    lookup returns for any workspace id.
    """
    server = _server_module()

    vault = tmp_path / ".thoughtmachine"
    (vault / "workspaces").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        server, "_vault", SimpleNamespace(vault_root=lambda: str(vault))
    )

    def _install(entry):
        registry = SimpleNamespace(get_workspace=lambda ws_id: entry)
        monkeypatch.setattr(
            server.WorkspaceRegistry, "get_default", staticmethod(lambda: registry)
        )
        return entry

    return SimpleNamespace(server=server, vault=vault, install=_install)


def test_vault_rooted_registry_entry_is_refused(mount_source_seam):
    """Regression test: a vault-rooted entry must never become a mount source."""
    server = mount_source_seam.server
    vault = mount_source_seam.vault
    rooted_in_vault = vault / "workspaces" / "smuggled"
    rooted_in_vault.mkdir(parents=True, exist_ok=True)
    mount_source_seam.install(_entry("ws-vault", rooted_in_vault))

    with pytest.raises(server.WorkspacePathError) as excinfo:
        server._resolve_workspace_path("ws-vault")

    message = str(excinfo.value)
    assert "protected vault directory" in message, (
        "a registry entry rooted inside the vault was returned unvalidated, so "
        "it would have been bind-mounted into a container as the workspace "
        f"root; got message {message!r}"
    )
    assert os.path.realpath(str(vault)) in message, (
        "the refusal must name the vault it refused to mount; "
        f"got message {message!r}"
    )


def test_registry_entry_outside_vault_still_resolves(mount_source_seam):
    """The legitimate case keeps working: a normal dir resolves to its realpath."""
    server = mount_source_seam.server
    legit = mount_source_seam.vault.parent / "coding"
    legit.mkdir(parents=True, exist_ok=True)
    mount_source_seam.install(_entry("ws-legit", legit))

    resolved = server._resolve_workspace_path("ws-legit")

    assert isinstance(resolved, str), (
        "a registered workspace outside the vault must resolve to a path "
        f"string, not {type(resolved).__name__}: callers pass it straight to "
        "ContainerManager(workspace_path=...)"
    )
    assert resolved == os.path.realpath(str(legit)), (
        "the registry branch must return the symlink-resolved path so it "
        f"matches the explicit-branch contract; got {resolved!r}"
    )


def test_missing_registry_entry_returns_none(mount_source_seam):
    """An unknown workspace answers None (-> 404), it must not raise."""
    server = mount_source_seam.server
    mount_source_seam.install(None)

    assert server._resolve_workspace_path("ws-missing") is None, (
        "an unregistered workspace must stay unresolvable (the route maps None "
        "to 404); raising here would turn a missing workspace into a 400/403"
    )


def test_registry_entry_with_empty_root_returns_none(mount_source_seam):
    """A registered entry carrying no root is unresolvable, not forbidden."""
    server = mount_source_seam.server
    mount_source_seam.install(_entry("ws-empty", ""))

    assert server._resolve_workspace_path("ws-empty") is None, (
        "a registry entry with an empty root_path must be unresolvable (None -> "
        "404), not validated into an exception and not returned as ''"
    )
