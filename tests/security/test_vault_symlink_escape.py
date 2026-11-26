"""
Receipt for the vault-symlink-escape claim (branch ``fix/vault-symlink-escape``).

The vault block in ``thoughtmachine.security.validate_path`` tests the LITERAL
path against ``VAULT_ROOT``: it is built with ``os.path.abspath``, which does
NOT resolve symlinks (the code says so itself).  The claim under test is that a
symlink created INSIDE a workspace, pointing at the global credential
compartment (``$VAULT_ROOT/credentials/global/``), slips past that block --- the
literal path starts with the workspace --- and then slips past the workspace
containment check too, so a file tool could read a global credential through it.

These tests materialise the real thing on disk -- a real credential file, a real
directory and a real symlink -- inside the hermetic vault that
``tests/conftest.py`` installs (HOME is redirected at conftest import time, so
``VAULT_ROOT`` is a throwaway temp directory, never the user's real vault).  The
escape is therefore MEASURED, not inferred.

The last test is the other half of the contract: a symlink that stays INSIDE the
workspace must keep working.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import thoughtmachine.security as security_module
from thoughtmachine.security import (
    PathOutsideWorkspaceError,
    VAULT_ROOT,
    validate_path,
)

GLOBAL_CREDENTIAL = '{"anthropic": "sk-ant-CANARY-do-not-read"}'


def _symlink_or_skip(target, link) -> None:
    """Create a symlink, skipping the test where the OS/filesystem refuses."""
    try:
        os.symlink(str(target), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks not supported on this OS/filesystem")


@pytest.fixture()
def global_credentials() -> Path:
    """Materialise ``$VAULT_ROOT/credentials/global/keys.json`` and return the dir.

    ``VAULT_ROOT`` is hermetic under pytest, so this never touches the real
    user vault.
    """
    creds_dir = Path(VAULT_ROOT) / "credentials" / "global"
    creds_dir.mkdir(parents=True, exist_ok=True)
    (creds_dir / "keys.json").write_text(GLOBAL_CREDENTIAL, encoding="utf-8")
    return creds_dir


class TestVaultSymlinkEscape:
    """A link inside the workspace must not reach the vault through the block."""

    def test_symlink_to_global_credentials_dir_is_refused(
        self, tmp_path: Path, global_credentials: Path
    ):
        """Workspace link -> ``credentials/global``, then a file through it."""
        ws = tmp_path / "ws"
        ws.mkdir()
        link = ws / "creds"
        _symlink_or_skip(global_credentials, link)

        target = link / "keys.json"
        # Precondition, on the real filesystem: the link genuinely reaches the
        # credential, so a refusal below is the gate's doing and not a
        # broken-link artifact.
        assert target.is_file()
        assert target.read_text(encoding="utf-8") == GLOBAL_CREDENTIAL

        with pytest.raises(PathOutsideWorkspaceError) as exc_info:
            validate_path(str(target), mode="read", workspace_path=str(ws))
        # The refusal must name the vault layer, not something incidental.
        assert "vault" in str(exc_info.value).lower()

    def test_symlink_to_global_credentials_dir_relative_form_refused(
        self, tmp_path: Path, global_credentials: Path
    ):
        """Same escape, expressed as a workspace-relative path."""
        ws = tmp_path / "ws"
        ws.mkdir()
        _symlink_or_skip(global_credentials, ws / "creds")

        assert (ws / "creds" / "keys.json").is_file()

        with pytest.raises(PathOutsideWorkspaceError) as exc_info:
            validate_path("creds/keys.json", workspace_path=str(ws))
        assert "vault" in str(exc_info.value).lower()

    def test_symlink_to_global_credential_file_is_refused(
        self, tmp_path: Path, global_credentials: Path
    ):
        """A direct file link to the credential is refused too."""
        ws = tmp_path / "ws"
        ws.mkdir()
        link = ws / "innocent.json"
        _symlink_or_skip(global_credentials / "keys.json", link)

        assert link.read_text(encoding="utf-8") == GLOBAL_CREDENTIAL

        with pytest.raises(PathOutsideWorkspaceError) as exc_info:
            validate_path(str(link), mode="read", workspace_path=str(ws))
        assert "vault" in str(exc_info.value).lower()

    def test_symlink_to_vault_root_dir_is_refused(self, tmp_path: Path):
        """A link to the vault root itself (not only a compartment) is refused."""
        ws = tmp_path / "ws"
        ws.mkdir()
        link = ws / "vault"
        _symlink_or_skip(VAULT_ROOT, link)

        with pytest.raises(PathOutsideWorkspaceError) as exc_info:
            validate_path(str(link / "credentials"), mode="read", workspace_path=str(ws))
        assert "vault" in str(exc_info.value).lower()

    def test_escape_refused_even_when_workspace_contains_the_vault(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The hard case: the workspace CONTAINS the vault.

        Here the post-resolution workspace-boundary check cannot help (the
        resolved target really is inside the workspace), so the escape is
        stopped only if the vault itself is re-checked on the RESOLVED path.
        ``VAULT_ROOT`` is repositioned inside ``tmp_path`` for this test; it is
        read as a module global at call time, so this exercises the shipped
        code, not a copy of it.
        """
        ws = tmp_path / "ws"
        creds_dir = ws / ".thoughtmachine" / "credentials" / "global"
        creds_dir.mkdir(parents=True)
        (creds_dir / "keys.json").write_text(GLOBAL_CREDENTIAL, encoding="utf-8")
        monkeypatch.setattr(
            security_module, "VAULT_ROOT", str(ws / ".thoughtmachine")
        )

        link = ws / "creds"
        _symlink_or_skip(creds_dir, link)
        target = link / "keys.json"
        assert target.read_text(encoding="utf-8") == GLOBAL_CREDENTIAL
        # The resolved target is still inside the workspace, so containment
        # alone would allow it.
        assert os.path.relpath(os.path.realpath(target), os.path.realpath(ws)).startswith(
            ".thoughtmachine"
        )

        with pytest.raises(PathOutsideWorkspaceError) as exc_info:
            validate_path(str(target), mode="read", workspace_path=str(ws))
        assert "vault" in str(exc_info.value).lower()


class TestNoWorkspaceBranchStillRechecksVault:
    """H1: the ``workspace_path=None`` return must run the vault re-check too.

    ``validate_path`` returned the resolved path from the NO-WORKSPACE branch
    before the post-resolution vault re-check was reached, so a caller that
    cannot name a workspace root got the vault file back -- the exact link the
    re-check exists to refuse.  The branch is reachable through the tool layer:
    ``ToolBase._validate_path`` passes ``resolved_ws`` straight through, and
    ``_resolve_workspace_root`` returns that value as ``None`` when neither the
    session registry nor the deprecated ``workspace_path`` can name a root
    (``tools/base.py:337-338``, ``:498``, ``:505``).
    """

    def test_no_workspace_symlink_to_vault_dir_is_refused(
        self, tmp_path: Path, global_credentials: Path
    ):
        """Workspace link -> ``credentials/global``, no workspace root given."""
        ws = tmp_path / "ws"
        ws.mkdir()
        _symlink_or_skip(global_credentials, ws / "creds")

        target = ws / "creds" / "keys.json"
        assert target.read_text(encoding="utf-8") == GLOBAL_CREDENTIAL

        with pytest.raises(PathOutsideWorkspaceError) as exc_info:
            validate_path(str(target), mode="read", workspace_path=None)
        assert "vault" in str(exc_info.value).lower()

    def test_no_workspace_symlink_to_vault_file_is_refused(
        self, tmp_path: Path, global_credentials: Path
    ):
        """A direct file link to the credential, with no workspace root."""
        ws = tmp_path / "ws"
        ws.mkdir()
        link = ws / "innocent.json"
        _symlink_or_skip(global_credentials / "keys.json", link)

        assert link.read_text(encoding="utf-8") == GLOBAL_CREDENTIAL

        with pytest.raises(PathOutsideWorkspaceError) as exc_info:
            validate_path(str(link), mode="read", workspace_path=None)
        assert "vault" in str(exc_info.value).lower()

    def test_no_workspace_ordinary_path_is_still_allowed(self, tmp_path: Path):
        """The fix must fail CLOSED, not degenerate into 'refuse everything'."""
        plain = tmp_path / "plain.txt"
        plain.write_text("ordinary", encoding="utf-8")

        allowed = validate_path(str(plain), mode="read", workspace_path=None)

        assert Path(allowed).read_text(encoding="utf-8") == "ordinary"

    def test_tool_layer_with_no_resolvable_workspace_refuses_the_link(
        self, tmp_path: Path, global_credentials: Path
    ):
        """The reachable half: an agent file tool with no resolvable root.

        ``workspace_path=None`` plus a session the registry does not know is
        exactly what ``ToolExecutor`` produces when ``config.workspace_path``
        is unset (``agent/core/tool_executor.py:564``), so this is the tool-level
        receipt for the branch above -- not a synthetic call.
        """
        from tools.file_preview_tool import FilePreviewTool

        ws = tmp_path / "ws"
        ws.mkdir()
        _symlink_or_skip(global_credentials, ws / "creds")

        target = ws / "creds" / "keys.json"
        assert target.read_text(encoding="utf-8") == GLOBAL_CREDENTIAL

        tool = FilePreviewTool(
            filename=str(target),
            workspace_path=None,
            session_id="sess-absent-from-registry",
        )
        assert tool._resolve_workspace_root() is None

        output = tool.execute()

        assert GLOBAL_CREDENTIAL not in output
        assert "vault" in output.lower()


class TestVaultSiblingPrefixIsNotTheVault:
    """H2: a sibling directory that merely SHARES the vault's string prefix.

    The vault comparisons were bare ``str.startswith`` tests, so
    ``<VAULT_ROOT>-evil/credentials/innocent.txt`` was refused as if it were
    vault material.  That is a FALSE POSITIVE, not an escape: the path has no
    ``.thoughtmachine`` component at all.  The workspace boundary next to it
    already resolves with ``os.path.relpath`` and is component-safe.
    """

    def test_sibling_directory_with_shared_vault_prefix_is_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The literal (pre-resolution) vault test must be component-safe."""
        vault = tmp_path / ".thoughtmachine"
        (vault / "credentials").mkdir(parents=True)
        (vault / "credentials" / "keys.json").write_text(
            GLOBAL_CREDENTIAL, encoding="utf-8"
        )
        sibling = tmp_path / ".thoughtmachine-evil"
        (sibling / "credentials").mkdir(parents=True)
        innocent = sibling / "credentials" / "innocent.txt"
        innocent.write_text("not a secret", encoding="utf-8")
        monkeypatch.setattr(security_module, "VAULT_ROOT", str(vault))

        # Precondition: the sibling shares the vault's bare string prefix, but
        # no path component of it is the vault.
        assert str(innocent).startswith(str(vault))
        assert os.path.relpath(innocent, vault).startswith("..")

        allowed = validate_path(str(innocent), mode="read", workspace_path=None)
        assert Path(allowed).read_text(encoding="utf-8") == "not a secret"

        # The genuine vault file is still refused -- the sibling allowance must
        # not have widened the barrier.
        with pytest.raises(PathOutsideWorkspaceError):
            validate_path(
                str(vault / "credentials" / "keys.json"),
                mode="read",
                workspace_path=None,
            )

    def test_symlink_to_sibling_with_shared_vault_prefix_is_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The RESOLVED vault test must be component-safe too."""
        ws = tmp_path / "ws"
        vault = ws / ".thoughtmachine"
        (vault / "credentials").mkdir(parents=True)
        (vault / "credentials" / "keys.json").write_text(
            GLOBAL_CREDENTIAL, encoding="utf-8"
        )
        sibling = ws / ".thoughtmachine-evil"
        (sibling / "credentials").mkdir(parents=True)
        (sibling / "credentials" / "innocent.txt").write_text(
            "not a secret", encoding="utf-8"
        )
        monkeypatch.setattr(security_module, "VAULT_ROOT", str(vault))

        _symlink_or_skip(sibling / "credentials", ws / "creds")
        target = ws / "creds" / "innocent.txt"
        # Precondition: the resolved target is inside the workspace, so the
        # workspace boundary cannot be what refuses it -- only the vault
        # prefix test can.
        assert target.read_text(encoding="utf-8") == "not a secret"
        assert not os.path.relpath(os.path.realpath(target), os.path.realpath(ws)).startswith(
            ".."
        )
        assert str(os.path.realpath(target)).startswith(str(vault))

        allowed = validate_path(str(target), mode="read", workspace_path=str(ws))
        assert Path(allowed).read_text(encoding="utf-8") == "not a secret"

        # A link to the REAL vault is still refused.
        _symlink_or_skip(vault / "credentials", ws / "vaultcreds")
        with pytest.raises(PathOutsideWorkspaceError) as exc_info:
            validate_path(str(ws / "vaultcreds" / "keys.json"), workspace_path=str(ws))
        assert "vault" in str(exc_info.value).lower()


class TestSymlinksInsideWorkspaceStillWork:
    """S4: the vault rule must not degenerate into 'symlinks are refused'."""

    def test_symlink_staying_inside_workspace_is_allowed(self, tmp_path: Path):
        ws = tmp_path / "ws"
        (ws / "src").mkdir(parents=True)
        (ws / "src" / "data.txt").write_text("legitimate", encoding="utf-8")
        _symlink_or_skip(ws / "src", ws / "alias")

        allowed = validate_path(str(ws / "alias" / "data.txt"), workspace_path=str(ws))

        assert Path(allowed).read_text(encoding="utf-8") == "legitimate"

    def test_symlink_to_sibling_file_inside_workspace_is_allowed(self, tmp_path: Path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "real.txt").write_text("legitimate", encoding="utf-8")
        _symlink_or_skip(ws / "real.txt", ws / "alias.txt")

        allowed = validate_path(str(ws / "alias.txt"), workspace_path=str(ws))

        assert Path(allowed).read_text(encoding="utf-8") == "legitimate"
