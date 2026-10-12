"""``ensure_workspace_dirs`` seeds a byte-identical ``requirements.txt``.

The workspace image build (``infra.container_manager.build_image``) stages its
build context from ``<vault>/workspaces/<id>/requirements.txt`` first and only
then falls back to ``<workspace_path>/requirements.txt``.  Seeding the
vault-managed copy guarantees the pinned requirements are always available to
the build regardless of the workspace path, so these tests pin the vault copy
to the repository's pinned ``requirements.txt``.
"""

from __future__ import annotations

from pathlib import Path

from thoughtmachine.workspace_capabilities import ensure_workspace_dirs

REPO_ROOT = Path(__file__).resolve().parent.parent
REPO_REQUIREMENTS = REPO_ROOT / "requirements.txt"

_WS_ID = "ws-req-seed"


def _ws_dir(tmp_path: Path) -> Path:
    return tmp_path / "vault" / "workspaces" / _WS_ID


def test_seeds_requirements_byte_identical(tmp_path, monkeypatch):
    """A fresh workspace seeds requirements.txt byte-identical to the repo copy."""
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path / "vault"))

    created = ensure_workspace_dirs(_WS_ID)

    dst = _ws_dir(tmp_path) / "requirements.txt"
    assert dst.exists(), f"requirements.txt not seeded at {dst}"
    assert dst.read_bytes() == REPO_REQUIREMENTS.read_bytes()
    assert str(dst) in created


def test_does_not_overwrite_existing_requirements(tmp_path, monkeypatch):
    """An operator-edited requirements.txt is preserved (idempotent seed)."""
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path / "vault"))

    dst = _ws_dir(tmp_path) / "requirements.txt"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("# operator-pinned marker\n", encoding="utf-8")

    created = ensure_workspace_dirs(_WS_ID)

    assert dst.read_text(encoding="utf-8") == "# operator-pinned marker\n"
    assert str(dst) not in created


def test_idempotent_second_call_does_not_recreate(tmp_path, monkeypatch):
    """A second call leaves the bytes untouched and does not re-list the file."""
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path / "vault"))

    ensure_workspace_dirs(_WS_ID)
    dst = _ws_dir(tmp_path) / "requirements.txt"
    first_bytes = dst.read_bytes()

    created_second = ensure_workspace_dirs(_WS_ID)

    assert dst.read_bytes() == first_bytes == REPO_REQUIREMENTS.read_bytes()
    assert str(dst) not in created_second
