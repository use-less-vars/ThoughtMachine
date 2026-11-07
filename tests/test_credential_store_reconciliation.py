"""W0 — credential store reconciliation (design_credential_resource.md, Decision 2).

Four properties, RED-first against the pre-W0 code:

1. ``CredentialInjector`` resolves through ``vault_root()``, never ``Path.home()``
   — the reader and the writer must name the same vault (Inv.6).
2. The workspace scope wins over the global scope for the same key.
3. The global scope is the fallback when the workspace scope has no such key.
4. ``global`` is a reserved workspace id — registration is refused, reason named.

Plus a sweep: the migrated modules carry no home-derived vault path.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent.credentials import CredentialInjector
from thoughtmachine.vault import vault_root
from thoughtmachine.workspace_registry import (
    RESERVED_WORKSPACE_IDS,
    WorkspaceRegistry,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """A tmp vault root, plus a decoy HOME holding a different value per key.

    ``THOUGHTMACHINE_VAULT_ROOT`` points at the real temp vault; ``HOME`` points
    at a decoy whose ``.thoughtmachine`` holds a ``github_token`` with a
    different value.  A ``Path.home()`` implementation can therefore only ever
    return the decoy — the two answers are distinguishable.
    """
    root = tmp_path / "vault"
    (root / "credentials" / "ws-alpha").mkdir(parents=True)
    (root / "credentials" / "global").mkdir(parents=True)

    decoy = tmp_path / "decoy-home" / ".thoughtmachine" / "credentials" / "ws-alpha"
    decoy.mkdir(parents=True)
    (decoy / "github_token").write_text("decoy-from-Path.home()")

    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(root))
    monkeypatch.setenv("HOME", str(tmp_path / "decoy-home"))
    return root


# ── 1. The injector resolves through vault_root() ────────────────────────────


def test_injector_resolves_through_vault_root(vault):
    """A credential in the THOUGHTMACHINE_VAULT_ROOT vault is what gets read."""
    (vault / "credentials" / "ws-alpha" / "github_token").write_text("ghp_from_vault")

    assert vault_root() == Path(vault).resolve()
    assert CredentialInjector("ws-alpha").resolve("github_token") == "ghp_from_vault"


def test_injector_credentials_dir_is_vault_root_derived(vault):
    """The directory the injector reads is built from vault_root(), not HOME."""
    injector = CredentialInjector("ws-alpha")

    assert injector.credentials_dir == Path(vault).resolve() / "credentials" / "ws-alpha"
    assert injector.global_credentials_dir == Path(vault).resolve() / "credentials" / "global"


# ── 2 and 3. Resolution order: workspace first, then global ──────────────────


def test_workspace_scope_wins_over_global(vault):
    """Same key in both scopes: the workspace binding wins."""
    (vault / "credentials" / "ws-alpha" / "github_token").write_text("workspace-value")
    (vault / "credentials" / "global" / "github_token").write_text("global-value")

    assert CredentialInjector("ws-alpha").resolve("github_token") == "workspace-value"


def test_global_scope_is_the_fallback(vault):
    """Key absent from the workspace scope falls through to the global scope."""
    (vault / "credentials" / "global" / "github_token").write_text("global-value\n")

    assert CredentialInjector("ws-alpha").resolve("github_token") == "global-value"


def test_global_scope_is_shared_across_workspaces(vault):
    """One operator-added global credential serves every workspace (no copies)."""
    (vault / "credentials" / "global" / "github_token").write_text("global-value")
    (vault / "credentials" / "ws-beta").mkdir()

    assert CredentialInjector("ws-alpha").resolve("github_token") == "global-value"
    assert CredentialInjector("ws-beta").resolve("github_token") == "global-value"


def test_missing_key_in_both_scopes_still_errors(vault):
    """Absent everywhere is still a hard failure — no silent empty value."""
    from agent.credentials import CredentialError

    with pytest.raises(CredentialError, match="not found"):
        CredentialInjector("ws-alpha").resolve("no_such_key")


def test_symlink_escape_is_refused_not_fallen_back(vault, tmp_path):
    """A traversal-shaped symlink in the workspace scope fails closed."""
    from agent.credentials import CredentialError

    outside = tmp_path / "outside-secret"
    outside.write_text("leaked")
    (vault / "credentials" / "ws-alpha" / "linked").symlink_to(outside)
    # Same key resolving cleanly in the global scope must NOT mask the attempt.
    (vault / "credentials" / "global" / "linked").write_text("global-value")

    with pytest.raises(CredentialError, match="path traversal"):
        CredentialInjector("ws-alpha").resolve("linked")


# ── 4. `global` is a reserved workspace id ──────────────────────────────────


def test_global_is_reserved_workspace_id(tmp_path):
    """Registering a workspace literally named ``global`` is refused."""
    assert "global" in RESERVED_WORKSPACE_IDS

    registry_path = tmp_path / "workspace_registry.json"
    registry_path.write_text("{}", encoding="utf-8")
    registry = WorkspaceRegistry(registry_path)
    root = tmp_path / "some-project"
    root.mkdir()

    with pytest.raises(ValueError, match="reserved"):
        registry.register_workspace("global", str(root))

    assert registry.get_workspace("global") is None


def test_reserved_refusal_names_the_reason(tmp_path):
    """The refusal says *why*, rather than "already registered"."""
    registry_path = tmp_path / "workspace_registry.json"
    registry_path.write_text("{}", encoding="utf-8")
    registry = WorkspaceRegistry(registry_path)
    root = tmp_path / "some-project"
    root.mkdir()

    with pytest.raises(ValueError) as excinfo:
        registry.register_workspace("global", str(root))

    message = str(excinfo.value)
    assert "credentials/global" in message
    assert "already registered" not in message


# ── Sweep: no home-derived vault path left in the migrated modules ───────────

_MIGRATED_MODULES = (
    "agent/credentials/injector.py",
    "agent/knowledge/global_kb.py",
    "agent/config/service.py",
    "agent/presenter/state_bridge.py",
)

_HOME_DERIVED = re.compile(r"Path\.home\(\)|expanduser\(")


def test_migrated_modules_do_not_hardcode_the_vault_path():
    """Inv.6: one function owns "where is the vault"; these callers use it."""
    offenders = []
    for rel in _MIGRATED_MODULES:
        text = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if ".thoughtmachine" in line and _HOME_DERIVED.search(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")

    assert not offenders, (
        "home-derived vault paths must go through thoughtmachine.vault.vault_root():\n"
        + "\n".join(offenders)
    )
