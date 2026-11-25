"""``ensure_git_shadow_mask`` seeds the ``.git`` shadow mask at create time.

``git_shadow_for_workspace`` binds ``<vault>/workspaces/<id>/.git-shadow-mask``
read-only over ``/workspace/.git`` whenever the workspace's ``.git`` is a FILE
(a git worktree / submodule pointer).  The docker daemon silently materialises a
MISSING single-file bind source as a DIRECTORY, which attaches the wrong shape
inside the container, so the helper refuses a missing mask outright.

Nothing on the container-create path used to seed that file: it was only written
by ``ensure_workspace_dirs`` (setup / web routes), so a workspace provisioned
before the mask existed -- and any id that only reaches the create path -- failed
to create a container.  These tests pin the seeding helper itself:

* it seeds a workspace DIRECTORY that ALREADY exists (not just a fresh one),
  and creates a missing parent directory;
* it is idempotent and never truncates an existing mask;
* it always leaves a REGULAR FILE, replacing a directory squatting on the path.
"""

from __future__ import annotations

from pathlib import Path

from thoughtmachine.workspace_capabilities import (
    GIT_SHADOW_MASK_FILENAME,
    ensure_git_shadow_mask,
    ensure_workspace_dirs,
)

_WS_ID = "ws-shadow-seed"


def _mask(tmp_path: Path, workspace_id: str = _WS_ID) -> Path:
    return tmp_path / "vault" / "workspaces" / workspace_id / GIT_SHADOW_MASK_FILENAME


def test_seeds_mask_for_preexisting_workspace_dir(tmp_path, monkeypatch):
    """A workspace directory that ALREADY exists (no mask) is seeded."""
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path / "vault"))

    ws_dir = tmp_path / "vault" / "workspaces" / _WS_ID
    ws_dir.mkdir(parents=True, exist_ok=True)
    assert not _mask(tmp_path).exists()

    path = ensure_git_shadow_mask(_WS_ID)

    assert path == _mask(tmp_path)
    assert path.exists(), f"mask not seeded at {path}"
    assert path.is_file()
    assert not path.is_dir()
    assert path.read_bytes() == b""


def test_seeds_missing_parent_directory(tmp_path, monkeypatch):
    """A workspace id whose directory does not exist yet gets parents created."""
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path / "vault"))

    # The coerced "default" id: the whole ``workspaces/default`` tree is absent.
    path = ensure_git_shadow_mask("default")

    assert path == _mask(tmp_path, "default")
    assert path.is_file()
    assert path.parent.is_dir()


def test_second_call_is_idempotent_and_never_truncates(tmp_path, monkeypatch):
    """An existing mask is left byte-identical (never opened for writing)."""
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path / "vault"))

    mask = _mask(tmp_path)
    mask.parent.mkdir(parents=True, exist_ok=True)
    mask.write_text("operator-marker\n", encoding="utf-8")

    ensure_git_shadow_mask(_WS_ID)
    ensure_git_shadow_mask(_WS_ID)

    assert mask.read_text(encoding="utf-8") == "operator-marker\n"


def test_directory_at_mask_path_becomes_regular_file(tmp_path, monkeypatch):
    """A directory squatting on the path is replaced by the required file."""
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path / "vault"))

    mask = _mask(tmp_path)
    mask.mkdir(parents=True)
    (mask / "materialised-by-daemon").write_text("x", encoding="utf-8")

    path = ensure_git_shadow_mask(_WS_ID)

    assert path.is_file()
    assert not path.is_dir()
    assert path.read_bytes() == b""


def test_ensure_workspace_dirs_seeds_mask_for_preexisting_dir(tmp_path, monkeypatch):
    """``ensure_workspace_dirs`` delegates, so a pre-existing dir is seeded too."""
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path / "vault"))

    (tmp_path / "vault" / "workspaces" / _WS_ID).mkdir(parents=True, exist_ok=True)

    created = ensure_workspace_dirs(_WS_ID)

    mask = _mask(tmp_path)
    assert mask.is_file()
    assert str(mask) in created

    # Idempotent: the second call reports nothing for an already-seeded mask.
    assert str(mask) not in ensure_workspace_dirs(_WS_ID)
    assert mask.is_file()


def test_both_create_paths_seed_the_mask():
    """Both container-create sites seed through the one helper, not the raw path."""
    repo_root = Path(__file__).resolve().parent.parent
    for rel in ("infra/containers/executor.py", "infra/container_manager.py"):
        src = (repo_root / rel).read_text(encoding="utf-8")
        assert "ensure_git_shadow_mask(self.workspace_id)" in src, (
            f"{rel} must seed the mask on the create path"
        )
