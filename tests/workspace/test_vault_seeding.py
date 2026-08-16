"""Unit tests for ``thoughtmachine.vault.ensure_resource_build_files``.
The vault (``~/.thoughtmachine/docker/resource/``) is the AUTHORITATIVE
source for resource-image builds; the repo ``resources/`` files are only
seeds. These tests redirect the vault root to ``tmp_path`` via the
``THOUGHTMACHINE_VAULT_ROOT`` env var (read at call time by
``vault.vault_root()``) and verify the seeding, legacy-promotion and
never-overwrite contracts — without ever touching the real vault.
"""
import inspect
import os
from pathlib import Path

import pytest

import thoughtmachine.vault as vault
from thoughtmachine import bootstrap

REPO_ROOT = Path(__file__).resolve().parents[2]
RESOURCES = REPO_ROOT / "resources"


@pytest.fixture
def seeded_root(tmp_path, monkeypatch):
    """Redirect the vault root to tmp_path (env is read at call time)."""
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path))
    return tmp_path


def test_vault_root_honors_env_at_call_time(seeded_root, monkeypatch):
    assert vault.vault_root() == seeded_root.resolve()
    monkeypatch.delenv("THOUGHTMACHINE_VAULT_ROOT")
    assert vault.vault_root() == Path.home() / ".thoughtmachine"


def test_first_use_seeds_three_build_files(seeded_root):
    created = vault.ensure_resource_build_files(RESOURCES, REPO_ROOT)
    build_dir = seeded_root / "docker" / "resource"
    expected = {
        "default_runtime.Dockerfile": RESOURCES / "default_dockerfile.txt",
        "git_overlay.Dockerfile": RESOURCES / "git_resource_overlay_dockerfile.txt",
        "requirements.txt": REPO_ROOT / "requirements.txt",
    }
    for name, seed in expected.items():
        dst = build_dir / name
        assert dst.is_file()
        assert dst.read_bytes() == seed.read_bytes()
        assert (dst.stat().st_mode & 0o777) == 0o644
    assert sorted(os.path.basename(p) for p in created) == sorted(expected)
    # every write landed under the redirected root — never the real vault
    assert all(str(Path(p)).startswith(str(seeded_root.resolve())) for p in created)


def test_legacy_dockerfile_promoted_over_seed(seeded_root):
    legacy_dir = seeded_root / "docker" / "resource"
    legacy_dir.mkdir(parents=True)
    legacy = legacy_dir / "Dockerfile"
    legacy.write_bytes(b"# legacy pre-provenance runtime dockerfile\n")
    created = vault.ensure_resource_build_files(RESOURCES, REPO_ROOT)
    runtime_dst = legacy_dir / "default_runtime.Dockerfile"
    assert runtime_dst.read_bytes() == legacy.read_bytes()
    assert (legacy_dir / "git_overlay.Dockerfile").read_bytes() == (
        RESOURCES / "git_resource_overlay_dockerfile.txt"
    ).read_bytes()
    assert (legacy_dir / "requirements.txt").read_bytes() == (
        REPO_ROOT / "requirements.txt"
    ).read_bytes()
    assert len(created) == 3


def test_existing_vault_copies_are_trust_anchors(seeded_root):
    """Default overwrite_existing=False (what bootstrap uses): an existing
    vault copy is never replaced — tampered content is preserved and nothing
    is reported as written."""
    vault.ensure_resource_build_files(RESOURCES, REPO_ROOT)
    tampered = seeded_root / "docker" / "resource" / "git_overlay.Dockerfile"
    tampered.write_bytes(b"# operator-tampered overlay\n")
    created = vault.ensure_resource_build_files(RESOURCES, REPO_ROOT)
    assert created == []
    assert tampered.read_bytes() == b"# operator-tampered overlay\n"


def test_manifest_marks_resource_build_files_never_overwrite(seeded_root):
    """MANIFEST.json marks the three docker/resource/* entries
    never_overwrite:true, so even a factory reset (overwrite_existing=True)
    never clobbers an existing vault copy (bootstrap Step-3 logic)."""
    manifest = bootstrap.get_manifest()
    entries = {entry["dest"]: entry for entry in manifest.get("files", [])}
    for dest in (
        "docker/resource/default_runtime.Dockerfile",
        "docker/resource/git_overlay.Dockerfile",
        "docker/resource/requirements.txt",
    ):
        assert dest in entries, f"missing MANIFEST entry {dest}"
        assert entries[dest].get("never_overwrite") is True


def test_bootstrap_seeds_with_overwrite_existing_false(seeded_root):
    """ensure_user_defaults() hard-codes overwrite_existing=False for the
    resource build files (trust anchors) — the source must not regress."""
    src = inspect.getsource(bootstrap.ensure_user_defaults)
    # scan the real call site (last occurrence; the docstring mentions the
    # function name first) with balanced parens to capture the full arg list
    marker = "ensure_resource_build_files("
    start = src.rindex(marker) + len(marker)
    depth, i, chunks = 1, start, []
    while depth and i < len(src):
        ch = src[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                break
        chunks.append(ch)
        i += 1
    call = "".join(chunks)
    assert "overwrite_existing=False" in call


def test_real_vault_not_touched(seeded_root):
    """Seeding writes ONLY under the redirected root — the real
    ~/.thoughtmachine vault is never read or modified."""
    real_build_dir = Path.home() / ".thoughtmachine" / "docker" / "resource"

    def snapshot():
        if not real_build_dir.exists():
            return None
        return {
            str(p): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in real_build_dir.rglob("*")
            if p.is_file()
        }

    before = snapshot()
    created = vault.ensure_resource_build_files(RESOURCES, REPO_ROOT)
    assert all(str(Path(p)).startswith(str(seeded_root.resolve())) for p in created)
    assert snapshot() == before
