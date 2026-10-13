"""PR2-HOIST-v1: the shared ``.git`` shadow helper (``infra.container_create``).

One helper, ``git_shadow_for_workspace``, owns the host ``.git`` shadow for
EVERY layout so that no create path can silently attach the wrong shape.
"""
import inspect
import os

import pytest

from infra.container_create import (
    GIT_SHADOW_TARGET,
    GitShadow,
    MountSpec,
    git_shadow_for_workspace,
)


def test_t1_git_dir_shadows_with_tmpfs(tmp_path):
    (tmp_path / ".git").mkdir()
    shadow = git_shadow_for_workspace(str(tmp_path))
    assert isinstance(shadow, GitShadow)
    assert shadow.tmpfs == {GIT_SHADOW_TARGET: ""}
    assert shadow.mounts == ()


def test_t2_git_file_shadows_with_single_file_read_only_bind(tmp_path):
    (tmp_path / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
    mask = tmp_path / "mask"
    mask.write_text("", encoding="utf-8")
    shadow = git_shadow_for_workspace(str(tmp_path), mask)
    assert shadow.tmpfs == {}
    assert shadow.mounts == (
        MountSpec(
            source=str(mask),
            target=GIT_SHADOW_TARGET,
            type="bind",
            read_only=True,
        ),
    )


def test_t3_git_file_missing_mask_refuses_loudly(tmp_path):
    (tmp_path / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
    missing = tmp_path / "absent-mask"
    with pytest.raises(RuntimeError) as exc:
        git_shadow_for_workspace(str(tmp_path), missing)
    msg = str(exc.value)
    assert GIT_SHADOW_TARGET in msg
    assert str(missing) in msg
    assert "DIRECTORY" in msg.upper()


def test_t3b_git_file_with_no_mask_path_refuses(tmp_path):
    (tmp_path / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
    with pytest.raises(RuntimeError):
        git_shadow_for_workspace(str(tmp_path))


def test_t4_both_create_sites_use_the_shared_helper():
    import infra.container_manager as cm
    import docker_executor as de

    cm_src = inspect.getsource(cm.ContainerManager._fresh_start)
    de_src = inspect.getsource(de.DockerExecutor._ensure_container)
    assert "git_shadow_for_workspace" in cm_src
    assert "git_shadow_for_workspace" in de_src
    # the duplicated inline detection is gone from both sites
    assert 'tmpfs["/workspace/.git"]' not in cm_src
    assert 'tmpfs["/workspace/.git"]' not in de_src
    assert "else:\n            pass" not in de_src


def test_t5_absent_git_is_no_shadow(tmp_path):
    shadow = git_shadow_for_workspace(str(tmp_path))
    assert shadow.tmpfs == {}
    assert shadow.mounts == ()


def test_t5_mask_seeded_idempotently_and_allowed(monkeypatch, tmp_path):
    import thoughtmachine.workspace_capabilities as wc

    monkeypatch.setattr(wc, "_user_dir", lambda: tmp_path)
    mask = wc.git_shadow_mask_path("ws-shadow")
    assert mask.name == wc.GIT_SHADOW_MASK_FILENAME
    assert str(mask).endswith(os.path.join("workspaces", "ws-shadow",
                                           wc.GIT_SHADOW_MASK_FILENAME))
    created = wc.ensure_workspace_dirs("ws-shadow")
    assert mask.exists() and mask.read_text(encoding="utf-8") == ""
    assert str(mask) in created
    # idempotent: a second call does not re-create it
    assert str(mask) not in wc.ensure_workspace_dirs("ws-shadow")
