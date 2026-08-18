"""Git Resource Overlay tests: pure-unit (no Docker) + Docker-gated integration.

Covers the two-stage resource image architecture in
``infra.resource_container_manager``:

- Stage 1 — ``tm-workspace-runtime:latest``: the dependency-only runtime base
  image (``requirements.txt`` + ``default_runtime.Dockerfile`` from the
  VAULT-MANAGED ``~/.thoughtmachine/docker/resource/`` directory, seeded once
  from the repo's pinned sources), labeled with a ``thoughtmachine.build_hash``.
- Stage 2 — ``tm-resource-git``: the git resource overlay
  (``git_overlay.Dockerfile`` in the same vault directory), built on top of
  the freshly-ensured runtime image via ``--build-arg BASE_IMAGE=...``. Its
  build
  hash covers requirements + runtime dockerfile + overlay dockerfile + the
  runtime image id, so any base drift forces the overlay to rebuild.

Unit tests (the first 8) run everywhere with fakes mirroring the docker SDK
surface — no daemon required. The Docker-gated tests (the last 3) build the
real images via ``rcm`` internals and are skipped unless
``TM_RUN_DOCKER_OVERLAY_TESTS=1`` AND a live Docker daemon is reachable;
gating is applied per-test via ``@docker_gated`` (never module-level, so the
unit tests always run).
"""

import os
import shutil
from pathlib import Path

import pytest

import infra.resource_container_manager as rcm


_REPO_ROOT = Path(__file__).resolve().parents[2]

try:
    from docker.errors import ImageNotFound
except Exception:  # pragma: no cover - docker SDK absent
    ImageNotFound = Exception


# ------------------------------------------------------------------ gating


def _overlay_docker_tests_enabled() -> bool:
    """True when the operator opted into Docker-gated overlay tests AND a
    live Docker daemon is reachable."""
    if os.environ.get("TM_RUN_DOCKER_OVERLAY_TESTS") != "1":
        return False
    try:
        import docker

        return bool(docker.from_env().ping())
    except Exception:
        return False


OVERLAY_SKIP_REASON = (
    "Docker-gated overlay test; set TM_RUN_DOCKER_OVERLAY_TESTS=1 and "
    "ensure Docker is available"
)
docker_gated = pytest.mark.skipif(
    not _overlay_docker_tests_enabled(), reason=OVERLAY_SKIP_REASON
)


# ------------------------------------------------------------------ fakes
# Minimal self-contained fakes (mirroring tests/docker/test_resource_lifecycle.py)
# so this suite stays independent: images.get/build/remove, containers.list/run.


class _FakeImage:
    """Image fake with ``.id`` and ``.labels``."""

    def __init__(self, image_id="sha256:img-current", labels=None):
        self.id = image_id
        self.labels = labels or {}


class _FakeImageRef:
    """``container.image`` fake — only ``.id`` is used."""

    def __init__(self, image_id):
        self.id = image_id


class _FakeImages:
    """``client.images`` fake with per-tag presence (runtime vs overlay).

    ``runtime_present`` models ``tm-workspace-runtime:latest`` independently
    from ``present`` (``tm-resource-git``), so a test can express "runtime
    ABSENT, overlay present-but-stale" exactly.
    """

    def __init__(
        self,
        present=True,
        runtime_present=True,
        image_id="sha256:img-current",
        labels=None,
        runtime_labels=None,
        get_error=None,
    ):
        self.present = present
        self.runtime_present = runtime_present
        self.image_id = image_id
        self.labels = labels
        self.runtime_labels = runtime_labels
        self.get_error = get_error
        self.get_calls = []
        self.build_calls = []
        self.remove_calls = []

    def get(self, tag):
        self.get_calls.append(tag)
        if self.get_error is not None:
            raise self.get_error
        if tag == rcm.RUNTIME_IMAGE_TAG:
            if not self.runtime_present:
                raise ImageNotFound(tag)
            return _FakeImage(self.image_id, self.runtime_labels)
        if not self.present:
            raise ImageNotFound(tag)
        return _FakeImage(self.image_id, self.labels)

    def build(self, **kwargs):
        self.build_calls.append(kwargs)
        self.present = True
        self.runtime_present = True
        return None, []

    def remove(self, tag, force=False):
        self.remove_calls.append({"tag": tag, "force": force})
        self.present = False


class _FakeContainer:
    """Container fake returned by ``containers.run`` / listed by ``list``."""

    def __init__(
        self,
        container_id,
        name=None,
        image_id="sha256:img-current",
        status="running",
        labels=None,
        owner=None,
    ):
        self.id = container_id
        self.name = name
        self.image = _FakeImageRef(image_id)
        self.status = status
        self.labels = labels or {}
        self.owner = owner

    def start(self):
        self.status = "running"

    def remove(self, force=False):
        if self.owner is not None and self in self.owner.containers:
            self.owner.containers.remove(self)


class _FakeContainers:
    """``client.containers`` fake with ``list`` and ``run`` recording."""

    def __init__(self, containers=None, run_error=None, list_error=None):
        self.containers = list(containers or [])
        for container in self.containers:
            container.owner = self
        self.run_error = run_error
        self.list_error = list_error
        self.run_calls = []
        self._next_id = 1

    def list(self, all=True, filters=None):
        if self.list_error is not None:
            raise self.list_error
        return list(self.containers)

    def run(self, **kwargs):
        self.run_calls.append(kwargs)
        if self.run_error is not None:
            raise self.run_error
        container = _FakeContainer(
            container_id=f"new-{self._next_id}",
            name=kwargs.get("name"),
            image_id=kwargs.get("image", rcm.RESOURCE_IMAGE_TAG),
            status="running",
            labels=kwargs.get("labels") or {},
            owner=self,
        )
        self._next_id += 1
        self.containers.append(container)
        return container


class _FakeClient:
    def __init__(self, images=None, containers=None):
        self.images = images
        self.containers = containers


class _FakeDockerModule:
    """Stands in for the ``docker`` module: ``from_env()`` -> client."""

    def __init__(
        self,
        images=None,
        containers=None,
        from_env_error=None,
        from_env_returns_none=False,
    ):
        self.images = images
        self.containers = containers
        self.from_env_error = from_env_error
        self.from_env_returns_none = from_env_returns_none
        self.from_env_calls = 0

    def from_env(self):
        self.from_env_calls += 1
        if self.from_env_error is not None:
            raise self.from_env_error
        if self.from_env_returns_none:
            return None
        return _FakeClient(self.images, self.containers)


def _monkeypatch_docker(monkeypatch, docker_mod):
    monkeypatch.setattr(rcm, "docker", docker_mod)
    return docker_mod


# --------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def _reset_image_ready():
    """Isolate the module-level image-readiness cache per test."""
    rcm._RESOURCE_IMAGE_READY = False
    yield
    rcm._RESOURCE_IMAGE_READY = False


@pytest.fixture(autouse=True)
def _vault_resource_files(tmp_path, monkeypatch):
    """Point rcm's VAULT_* build inputs at a tmp vault seeded from the repo
    seeds (resources/ + requirements.txt). Production reads VAULT_* only —
    the repo files are seeds. Identical bytes -> identical build hashes."""
    vault_dir = tmp_path / "docker" / "resource"
    vault_dir.mkdir(parents=True)
    shutil.copy2(
        _REPO_ROOT / "resources" / "default_dockerfile.txt",
        vault_dir / "default_runtime.Dockerfile",
    )
    shutil.copy2(
        _REPO_ROOT / "resources" / "git_resource_overlay_dockerfile.txt",
        vault_dir / "git_overlay.Dockerfile",
    )
    shutil.copy2(_REPO_ROOT / "requirements.txt", vault_dir / "requirements.txt")
    monkeypatch.setattr(rcm, "VAULT_RESOURCE_DIR", str(vault_dir))
    monkeypatch.setattr(rcm, "VAULT_REQUIREMENTS", str(vault_dir / "requirements.txt"))
    monkeypatch.setattr(rcm, "VAULT_RUNTIME_DOCKERFILE", str(vault_dir / "default_runtime.Dockerfile"))
    monkeypatch.setattr(rcm, "VAULT_OVERLAY_DOCKERFILE", str(vault_dir / "git_overlay.Dockerfile"))
    yield vault_dir


@pytest.fixture(scope="module")
def overlay_images():
    """Build BOTH resource images ONCE via rcm internals (real Docker).

    Skipped (with the shared reason) when the Docker-gated suite is not
    enabled, so this fixture is harmless in plain unit runs.
    """
    if not _overlay_docker_tests_enabled():
        pytest.skip(OVERLAY_SKIP_REASON)
    assert rcm._ensure_resource_image(), (
        "failed to build tm-workspace-runtime / tm-resource-git; "
        "check Docker availability and network (apt-get)"
    )


# --------------------------------------------------------------- helpers


def _repo_build_hash():
    return rcm.compute_resource_build_hash(
        rcm.VAULT_REQUIREMENTS, rcm.VAULT_RUNTIME_DOCKERFILE
    )


def _overlay_build_hash(runtime_image_id="sha256:img-current"):
    return rcm.compute_git_overlay_build_hash(
        rcm.VAULT_REQUIREMENTS,
        rcm.VAULT_RUNTIME_DOCKERFILE,
        rcm.VAULT_OVERLAY_DOCKERFILE,
        runtime_image_id,
    )


def _fresh_images(image_id="sha256:img-current"):
    """Both images present and current: overlay labeled with the overlay hash
    (derived from ``image_id``), runtime labeled with the runtime hash."""
    return _FakeImages(
        present=True,
        runtime_present=True,
        image_id=image_id,
        labels={rcm.RESOURCE_BUILD_HASH_LABEL: _overlay_build_hash(image_id)},
        runtime_labels={rcm.RESOURCE_BUILD_HASH_LABEL: _repo_build_hash()},
    )


# ══════════════════════════════════════════════════════════════════════════
#  Unit tests (default run — no Docker required)
# ══════════════════════════════════════════════════════════════════════════


def test_hash_inputs_cover_all_drift_sources(monkeypatch, tmp_path):
    """Every input the overlay is built from feeds its build hash."""
    runtime_hash = rcm.compute_resource_build_hash(
        rcm.VAULT_REQUIREMENTS, rcm.VAULT_RUNTIME_DOCKERFILE
    )
    overlay_hash_a = rcm.compute_git_overlay_build_hash(
        rcm.VAULT_REQUIREMENTS,
        rcm.VAULT_RUNTIME_DOCKERFILE,
        rcm.VAULT_OVERLAY_DOCKERFILE,
        "sha256:aaa",
    )
    # (a) the runtime hash and the overlay hash differ for the same repo files
    assert runtime_hash != overlay_hash_a
    # (b) the overlay hash changes when the runtime image id changes
    overlay_hash_b = rcm.compute_git_overlay_build_hash(
        rcm.VAULT_REQUIREMENTS,
        rcm.VAULT_RUNTIME_DOCKERFILE,
        rcm.VAULT_OVERLAY_DOCKERFILE,
        "sha256:bbb",
    )
    assert overlay_hash_a != overlay_hash_b
    # (c) the overlay hash changes when the overlay dockerfile content changes
    tmp_overlay = tmp_path / "git_resource_overlay_dockerfile.txt"
    tmp_overlay.write_bytes(
        Path(rcm.VAULT_OVERLAY_DOCKERFILE).read_bytes() + b"\n# drift\n"
    )
    monkeypatch.setattr(rcm, "VAULT_OVERLAY_DOCKERFILE", str(tmp_overlay))
    overlay_hash_c = rcm.compute_git_overlay_build_hash(
        rcm.VAULT_REQUIREMENTS,
        rcm.VAULT_RUNTIME_DOCKERFILE,
        rcm.VAULT_OVERLAY_DOCKERFILE,
        "sha256:aaa",
    )
    assert overlay_hash_c != overlay_hash_a
    # (d) the overlay hash changes when the runtime dockerfile content changes
    tmp_runtime = tmp_path / "default_dockerfile.txt"
    tmp_runtime.write_bytes(
        Path(rcm.VAULT_RUNTIME_DOCKERFILE).read_bytes() + b"\n# drift\n"
    )
    monkeypatch.setattr(rcm, "VAULT_RUNTIME_DOCKERFILE", str(tmp_runtime))
    overlay_hash_d = rcm.compute_git_overlay_build_hash(
        rcm.VAULT_REQUIREMENTS,
        rcm.VAULT_RUNTIME_DOCKERFILE,
        rcm.VAULT_OVERLAY_DOCKERFILE,
        "sha256:aaa",
    )
    assert overlay_hash_d != overlay_hash_a


def test_static_runtime_dockerfile_excludes_git_tooling():
    """The runtime base image must NOT carry git/curl tooling — that is the
    overlay's job."""
    lines = Path(rcm.VAULT_RUNTIME_DOCKERFILE).read_text().splitlines()
    # header advertises the git resource overlay layered on top
    assert any("git resource overlay" in line for line in lines[:8])
    # apt-get install token set (RUN block incl. `&&` continuations)
    run_tokens = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("RUN ") or stripped.startswith("&&"):
            run_tokens.extend(stripped.split())
    assert "nodejs" in run_tokens
    assert "npm" in run_tokens
    assert "git" not in run_tokens
    assert "ca-certificates" not in run_tokens
    assert "curl" not in run_tokens


def test_static_overlay_dockerfile_spec():
    """The git overlay dockerfile layers ONLY the OS git tooling on the
    runtime image (no python/node package installs)."""
    text = Path(rcm.VAULT_OVERLAY_DOCKERFILE).read_text()
    assert "ARG BASE_IMAGE" in text
    assert "FROM ${BASE_IMAGE}" in text
    run_line = next(
        line for line in text.splitlines() if line.startswith("RUN ")
    )
    for tool in ("git", "ca-certificates", "curl"):
        assert tool in run_line
    assert "USER agent" in text
    assert "ENV HOME=/home/agent" in text
    assert "WORKDIR /workspace" in text
    assert '"tail", "-f", "/dev/null"' in text  # keep-alive CMD in JSON array form
    assert "pip install" not in text
    assert "requirements.txt" not in text
    assert "nodejs" not in text
    assert "npm" not in text


def test_ensure_resource_returns_required_schema(monkeypatch, tmp_path):
    """With both images ready, ensure_resource('git') returns the exact
    seven-key containerized result dict."""
    ws = tmp_path / "ws"
    ws.mkdir()
    images = _fresh_images()
    containers = _FakeContainers()
    _monkeypatch_docker(
        monkeypatch, _FakeDockerModule(images=images, containers=containers)
    )
    mgr = rcm.ResourceContainerManager(workspace_id="ws-1", workspace_path=str(ws))
    result = mgr.ensure_resource("git")
    assert set(result.keys()) == {
        "mode", "container_id", "status", "image", "detail",
        "failure_reason", "fallback_used",
    }
    assert result["failure_reason"] is None
    assert result["fallback_used"] is False
    assert result["mode"] == "containerized"
    assert result["image"] == rcm.RESOURCE_IMAGE_TAG
    assert result["container_id"] is not None
    assert result["status"] == "running"


def test_host_fallback_when_docker_unavailable(monkeypatch, tmp_path):
    """When docker.from_env() yields no client, ensure_resource degrades to
    host_fallback whose detail carries BOTH image build commands."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _monkeypatch_docker(
        monkeypatch,
        _FakeDockerModule(
            images=None, containers=None, from_env_returns_none=True
        ),
    )
    mgr = rcm.ResourceContainerManager(workspace_id="ws-1", workspace_path=str(ws))
    result = mgr.ensure_resource("git")
    assert result["mode"] == "host_fallback"
    # manual-build instructions mention the runtime base AND the git overlay
    assert rcm.RUNTIME_IMAGE_TAG in result["detail"]
    assert rcm.RESOURCE_IMAGE_TAG in result["detail"]
    assert "docker build" in result["detail"]


def test_runtime_missing_rebuilds_runtime_then_overlay(monkeypatch):
    """Missing runtime base -> stage 1 build (runtime tag) FIRST, then stage 2
    overlay build on top of it; the overlay label matches the freshly computed
    overlay hash for the (fake) runtime image id."""
    images = _FakeImages(
        present=True,  # tm-resource-git exists…
        runtime_present=False,  # …but tm-workspace-runtime:latest is ABSENT
        image_id="sha256:img-current",
        labels={rcm.RESOURCE_BUILD_HASH_LABEL: "legacy-stale-label"},
        runtime_labels={rcm.RESOURCE_BUILD_HASH_LABEL: "legacy-stale-label"},
    )
    _monkeypatch_docker(
        monkeypatch, _FakeDockerModule(images=images, containers=_FakeContainers())
    )
    assert rcm._ensure_resource_image() is True
    assert [call["tag"] for call in images.build_calls] == [
        rcm.RUNTIME_IMAGE_TAG,
        rcm.RESOURCE_IMAGE_TAG,
    ]
    overlay_build = images.build_calls[1]
    assert overlay_build["buildargs"] == {"BASE_IMAGE": rcm.RUNTIME_IMAGE_TAG}
    expected = _overlay_build_hash("sha256:img-current")
    assert overlay_build["labels"][rcm.RESOURCE_BUILD_HASH_LABEL] == expected


def test_stale_overlay_rebuilds_on_hash_mismatch(monkeypatch):
    """Runtime current + overlay labeled with the LEGACY runtime hash ->
    only the overlay is rebuilt (runtime build NOT called)."""
    runtime_hash = _repo_build_hash()
    images = _FakeImages(
        present=True,
        runtime_present=True,
        image_id="sha256:img-current",
        labels={rcm.RESOURCE_BUILD_HASH_LABEL: runtime_hash},  # legacy label
        runtime_labels={rcm.RESOURCE_BUILD_HASH_LABEL: runtime_hash},
    )
    _monkeypatch_docker(
        monkeypatch, _FakeDockerModule(images=images, containers=_FakeContainers())
    )
    assert rcm._ensure_resource_image() is True
    assert len(images.build_calls) == 1
    assert images.build_calls[0]["tag"] == rcm.RESOURCE_IMAGE_TAG
    assert images.build_calls[0]["buildargs"] == {"BASE_IMAGE": rcm.RUNTIME_IMAGE_TAG}
    expected = _overlay_build_hash("sha256:img-current")
    assert images.build_calls[0]["labels"][rcm.RESOURCE_BUILD_HASH_LABEL] == expected


def test_git_info_tool_resolves_overlay_image(monkeypatch, tmp_path):
    """GitInfoTool's real _resolve_resource_execution constructs the manager,
    calls ensure_resource('git'), and routes a containerized result to a raw
    `git` argv exec (no /bin/sh -c wrapper, no --no-verify)."""
    from tools.git_info_tool import GitInfoTool

    class _FakeResourceManager:
        """Stand-in for infra.resource_container_manager.ResourceContainerManager."""

        def __init__(self, **kwargs):
            self.ctor_kwargs = kwargs
            self.ensure_calls = []
            self.exec_calls = []

        def ensure_resource(self, name):
            self.ensure_calls.append(name)
            return {
                "mode": "containerized",
                "container_id": "res-c1",
                "status": "running",
                "image": rcm.RESOURCE_IMAGE_TAG,
                "detail": "",
            }

        def exec(self, cmd, workdir="/workspace", environment=None, timeout=30):
            self.exec_calls.append(
                {
                    "cmd": list(cmd),
                    "workdir": workdir,
                    "environment": environment,
                    "timeout": timeout,
                }
            )
            return {"exit_code": 0, "stdout": "", "stderr": ""}

    # _resolve_resource_execution does `from infra.resource_container_manager
    # import ResourceContainerManager` at call time -> patching the module attr
    # is sufficient.
    monkeypatch.setattr(rcm, "ResourceContainerManager", _FakeResourceManager)

    tool = GitInfoTool(operation="status", message="x")
    object.__setattr__(tool, "_resolved_workspace_path", str(tmp_path))
    object.__setattr__(tool, "_resolved_workspace_id", "test-ws")

    exit_code, stdout, stderr = tool._run_git_raw(tmp_path, ["status", "--short"])

    assert (exit_code, stdout, stderr) == (0, "", "")
    fake_mgr = tool._resource_manager
    assert isinstance(fake_mgr, _FakeResourceManager)
    assert fake_mgr.ensure_calls == ["git"]
    assert len(fake_mgr.exec_calls) == 1
    argv = fake_mgr.exec_calls[0]["cmd"]
    assert argv == ["git", "status", "--short"]
    assert argv[0] == "git"
    # raw argv dispatch: no shell wrapper, no host-only --no-verify hardening
    assert "/bin/sh" not in argv
    assert "-c" not in argv
    assert "--no-verify" not in argv
    # repo root maps to the containerized /workspace
    assert fake_mgr.exec_calls[0]["workdir"] == "/workspace"


# ══════════════════════════════════════════════════════════════════════════
#  Docker-gated tests (skip unless TM_RUN_DOCKER_OVERLAY_TESTS=1 + daemon)
# ══════════════════════════════════════════════════════════════════════════


@docker_gated
def test_built_runtime_image_has_no_git(overlay_images):
    """tm-workspace-runtime:latest must NOT contain git/curl — they are added
    only by the overlay."""
    import docker

    client = docker.from_env()
    container = client.containers.create(
        rcm.RUNTIME_IMAGE_TAG,
        command=["/bin/sh", "-c", "tail -f /dev/null"],
        tty=True,
        stdin_open=True,
    )
    try:
        container.start()
        for cmd, expected in [
            (["/bin/sh", "-c", "which git || echo NO_GIT"], "NO_GIT"),
            (["/bin/sh", "-c", "which curl || echo NO_CURL"], "NO_CURL"),
        ]:
            exit_code, output = container.exec_run(cmd)
            text = (
                output.decode(errors="replace")
                if isinstance(output, bytes)
                else str(output)
            )
            assert expected in text, f"expected {expected!r} in {text!r}"
    finally:
        try:
            container.remove(force=True)
        except Exception:
            pass


@docker_gated
def test_built_overlay_image_has_git_and_is_used(overlay_images, tmp_path):
    """ensure_resource('git') provisions a running tm-resource-git container
    and `git --version` executes inside it."""
    mgr = rcm.ResourceContainerManager(
        workspace_id="overlay-gated-ws-10", workspace_path=str(tmp_path)
    )
    try:
        result = mgr.ensure_resource("git")
        assert result["mode"] == "containerized"
        assert result["image"] == rcm.RESOURCE_IMAGE_TAG
        assert result["status"] == "running"
        out = mgr.exec(["git", "--version"])
        assert out["exit_code"] == 0
        assert "git version" in out["stdout"]
    finally:
        mgr.remove()


@docker_gated
def test_stale_overlay_rebuilds_on_overlay_change(overlay_images, tmp_path, monkeypatch):
    """Editing the overlay dockerfile changes its build hash and forces a
    rebuild; the relabeled tm-resource-git still serves git --version."""
    import docker

    tmp_overlay = tmp_path / "git_resource_overlay_dockerfile.txt"
    tmp_overlay.write_bytes(
        Path(rcm.VAULT_OVERLAY_DOCKERFILE).read_bytes() + b"\n# drift\n"
    )
    monkeypatch.setattr(rcm, "VAULT_OVERLAY_DOCKERFILE", str(tmp_overlay))

    assert rcm._ensure_resource_image() is True

    client = docker.from_env()
    runtime_id = client.images.get(rcm.RUNTIME_IMAGE_TAG).id
    expected = rcm.compute_git_overlay_build_hash(
        rcm.VAULT_REQUIREMENTS,
        rcm.VAULT_RUNTIME_DOCKERFILE,
        str(tmp_overlay),
        runtime_id,
    )
    labels = client.images.get(rcm.RESOURCE_IMAGE_TAG).labels or {}
    assert labels.get(rcm.RESOURCE_BUILD_HASH_LABEL) == expected

    mgr = rcm.ResourceContainerManager(
        workspace_id="overlay-gated-ws-11", workspace_path=str(tmp_path)
    )
    try:
        result = mgr.ensure_resource("git")
        assert result["mode"] == "containerized"
        out = mgr.exec(["git", "--version"])
        assert out["exit_code"] == 0
        assert "git version" in out["stdout"]
    finally:
        mgr.remove()
