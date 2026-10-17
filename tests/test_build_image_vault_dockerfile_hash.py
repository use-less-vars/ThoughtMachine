"""The build hash must cover the VAULT Dockerfile that is actually built.

``ContainerManager.build_image`` gates the build on the
``thoughtmachine.build_hash`` image label: a matching label returns early with
``build_log == ""`` and no build.  The hash used to be computed from the REPO
sources only (``compute_executor_build_hash()``), while the build context is the
vault ``<vault>/workspaces/<workspace_id>/Dockerfile`` (+ ``requirements.txt``).
Editing the workspace Dockerfile therefore changed a real build input without
changing the label, so the gate matched and the rebuild was silently skipped.

The hash now folds in the vault-supplied inputs (``extra_paths``); the default
(no ``extra_paths``) stays byte-identical so every other caller is unaffected.

Style follows ``tests/test_build_image_requires_vault_requirements.py``: the
manager is built via ``ContainerManager.__new__`` and ``_load_docker_executor``
is patched with a dex double that runs the REAL hash function, so the drift gate
behaves exactly as in production.
"""

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from infra.container_manager import ContainerManager, ImageNotFound
from infra.containers import executor as executor_mod


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class _Computed(tuple):
    """Minimal stand-in for the resolved config (``(net, ws)`` + ``.effective``)."""

    def __new__(cls, network_mode, workspace_mode, effective=None):
        obj = super().__new__(cls, (network_mode, workspace_mode))
        obj.effective = effective if effective is not None else {"container": True}
        return obj


def _manager(tmp_path, workspace_id="ws-1"):
    """A hermetic manager whose vault workspace holds Dockerfile + requirements."""
    vault_root = tmp_path / "vault"
    vault_ws = vault_root / "workspaces" / workspace_id
    vault_ws.mkdir(parents=True)
    (vault_ws / "Dockerfile").write_text("FROM python:3.12-slim\n")
    (vault_ws / "requirements.txt").write_text("vault-pin==1.0\n")

    ws = tmp_path / "ws"
    ws.mkdir()

    manager = ContainerManager.__new__(ContainerManager)  # skip docker.from_env()
    manager.client = MagicMock()
    manager.client.images.get.side_effect = ImageNotFound("absent")
    manager.workspace_path = str(ws)
    manager.vault_root = str(vault_root)
    manager.workspace_id = workspace_id
    manager.session_id = "sess-1"
    manager.session_permissions = {"container": True}
    # Grant the session container access (build_image's gate is fail-closed).
    manager._compute_config = lambda *a, **k: _Computed("none", "ro")
    return manager, vault_ws / "Dockerfile"


def _mock_dex(captured):
    """Docker-executor double using the REAL build-hash function + label key."""
    dex = MagicMock()
    dex._compute_image_tag.side_effect = lambda ws: "agent-executor-test"
    dex._resolve_container_config_via_gate.return_value = ("none", "ro")
    dex.compute_executor_build_hash = executor_mod.compute_executor_build_hash
    dex.EXECUTOR_BUILD_HASH_LABEL = executor_mod.EXECUTOR_BUILD_HASH_LABEL

    def _capture_build(client, build_path, dockerfile, tag, **kw):
        labels = dict(kw.get("labels") or {})
        captured.setdefault("hashes", []).append(
            labels.get(executor_mod.EXECUTOR_BUILD_HASH_LABEL)
        )
        return "sha256:abc", ["Step 1"]

    dex._run_image_build.side_effect = _capture_build
    return dex


def _run_build(manager, captured):
    with patch("infra.container_manager.DOCKER_AVAILABLE", True), \
            patch("infra.container_manager._load_docker_executor") as mock_load:
        dex = _mock_dex(captured)
        mock_load.return_value = dex
        result = manager.build_image()
    return result, dex


def _label_image(manager, build_hash):
    """Pretend an image tagged with ``build_hash`` already exists."""
    manager.client.images.get.side_effect = None
    manager.client.images.get.return_value = SimpleNamespace(
        labels={executor_mod.EXECUTOR_BUILD_HASH_LABEL: build_hash}
    )


# ---------------------------------------------------------------------------
# compute_executor_build_hash: default stays byte-identical
# ---------------------------------------------------------------------------


def test_default_hash_is_byte_identical_to_legacy_two_source_hash():
    """Callers that pass no extra inputs keep the historical digest."""
    legacy = hashlib.sha256(
        Path(executor_mod.EXECUTOR_REQUIREMENTS).read_bytes()
        + b"\n"
        + Path(executor_mod.EXECUTOR_RUNTIME_DOCKERFILE).read_bytes()
    ).hexdigest()
    assert executor_mod.compute_executor_build_hash() == legacy
    assert executor_mod.compute_executor_build_hash(extra_paths=None) == legacy
    assert executor_mod.compute_executor_build_hash(extra_paths=[]) == legacy


def test_extra_paths_content_changes_the_hash(tmp_path):
    vault_dockerfile = tmp_path / "Dockerfile"
    vault_dockerfile.write_text("FROM python:3.12-slim\n")

    baseline = executor_mod.compute_executor_build_hash()
    first = executor_mod.compute_executor_build_hash(
        extra_paths=[str(vault_dockerfile)]
    )
    again = executor_mod.compute_executor_build_hash(
        extra_paths=[str(vault_dockerfile)]
    )

    vault_dockerfile.write_text("FROM python:3.14-slim\n")
    edited = executor_mod.compute_executor_build_hash(
        extra_paths=[str(vault_dockerfile)]
    )

    assert first == again
    assert first != baseline  # extras participate in the digest
    assert edited != first     # editing a vault input changes the digest


def test_missing_extra_path_is_folded_not_raised(tmp_path):
    """A missing vault input is a distinct state, never an OSError."""
    missing = tmp_path / "nope" / "Dockerfile"
    present = tmp_path / "Dockerfile"
    present.write_text("FROM python:3.12-slim\n")

    absent_hash = executor_mod.compute_executor_build_hash(
        extra_paths=[str(missing)]
    )
    present_hash = executor_mod.compute_executor_build_hash(
        extra_paths=[str(present)]
    )
    assert absent_hash != present_hash


# ---------------------------------------------------------------------------
# build_image: the drift gate honours the vault Dockerfile
# ---------------------------------------------------------------------------


@patch("infra.container_manager.DOCKER_AVAILABLE", True)
def test_editing_the_vault_dockerfile_causes_a_rebuild(tmp_path):
    """Same vault inputs -> skip; edited Dockerfile -> rebuild (no silent skip)."""
    manager, vault_dockerfile = _manager(tmp_path)
    captured = {}

    # 1) First build: no image yet -> builds and records the label.
    result, dex = _run_build(manager, captured)
    assert captured.get("hashes"), "the build must run the first time"
    first_hash = captured["hashes"][-1]
    assert result["build_log"] != ""

    # 2) Image labelled with that hash + unchanged inputs -> early return.
    _label_image(manager, first_hash)
    result, dex = _run_build(manager, captured)
    assert result == {"image_tag": "agent-executor-test", "build_log": ""}
    assert len(captured["hashes"]) == 1  # no rebuild

    # 3) Edit the VAULT Dockerfile -> the label no longer matches -> rebuild.
    vault_dockerfile.write_text("FROM python:3.12-slim\nRUN echo edited\n")
    result, dex = _run_build(manager, captured)
    assert len(captured["hashes"]) == 2, "a Dockerfile edit must rebuild"
    assert captured["hashes"][-1] != first_hash
    assert result["build_log"] != ""


@patch("infra.container_manager.DOCKER_AVAILABLE", True)
def test_legacy_bare_hash_label_does_not_skip_the_build(tmp_path):
    """Regression pin: a label from the repo-only hash must NOT be good enough."""
    manager, _ = _manager(tmp_path)
    captured = {}

    # The pre-fix gate hashed the repo sources only.
    _label_image(manager, executor_mod.compute_executor_build_hash())

    result, dex = _run_build(manager, captured)
    assert len(captured["hashes"]) == 1, "repo-only label must be treated as drift"
    assert captured["hashes"][0] != executor_mod.compute_executor_build_hash()
    assert result["build_log"] != ""
