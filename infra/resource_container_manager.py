"""Hidden git resource container manager (defense-in-depth isolation).

``ResourceContainerManager`` owns ONE long-lived, workspace-scoped container
per workspace that runs git operations against the real workspace directory.
It is intentionally INVISIBLE to the agent-facing container tooling.

Security model
--------------
- Hidden from agent tools: the container carries
  ``thoughtmachine.resource=git`` (in addition to the standard
  ``thoughtmachine.workspace_id`` and ``thoughtmachine.container_name``
  labels). ``ContainerManager.list_containers()`` filters by the workspace
  label only, so the resource container WOULD surface — the defense-in-depth
  exclusion diff (see audit report, section A) skips any container whose
  labels contain ``thoughtmachine.resource``. Its name prefix ``tm-res-`` is
  also not matched by the web_ui startup scan (which only matches
  ``agent-exec-``) or by ``docker_executor.verify_container_integrity``.
- Workspace-tied lifecycle (accepted tradeoff): the container carries
  ``thoughtmachine.workspace_id=<ws_id>``, so the module-level
  ``cleanup_workspace()`` WILL stop and remove it when the workspace is
  decommissioned. This is intentional: a git sandbox must never outlive its
  workspace.
- No package volume: unlike ``ContainerManager`` there is no
  ``tm-packages-<ws_id>`` volume and no ``PYTHONUSERBASE`` — the git sandbox
  needs no persistent Python packages, and an extra writable volume would
  widen the attack surface.
- No vault mount: ``~/.thoughtmachine`` (vault, secrets, credentials) is
  NEVER mounted. The hook-isolation test (tests/security/
  test_git_container_sandbox.py) proves a malicious post-commit hook cannot
  read ``/vault/secrets.txt``.
- No docker socket: ``/var/run/docker.sock`` is never mounted, so a
  compromised hook cannot reach the daemon (verified by the sandbox test).
- Network: disabled by default (``network_mode='none'``). The caller may
  resolve a graded mode via ``security_gate.get_expected_container_config``;
  anything other than an explicit grant must stay ``'none'``.
- Hardening: identical to ``ContainerManager.start`` /
  ``docker_executor.DockerExecutor._ensure_container`` — ``cap_drop=["ALL"]``,
  ``security_opt=["no-new-privileges:true"]``, read-only rootfs,
  non-root user ``1000:1000``, mem/cpu quotas, tmpfs for ``/tmp`` and
  ``/home/agent``.
- Workspace mount is READ-WRITE: git writes ``.git`` metadata and updates the
  index, so ``read_only=False``. This intentionally DIVERGES from the
  executor's tmpfs-shadowing of ``/workspace/.git``: the resource container's
  purpose is to operate on the REAL git metadata. Blast radius is bounded by
  no network / no socket / no vault / read-only rootfs. One documented extra
  mount: for a git linked worktree (``.git`` is a ``gitdir:`` pointer file)
  the MAIN repository is additionally bind-mounted at its original host path
  so the pointer resolves inside the container (see
  ``_resolve_worktree_main_repo``).

Exec semantics
--------------
``exec()`` runs the command as a raw argv list via ``container.exec_run``
(``cmd=...``, NO ``/bin/sh -c`` wrapper) with a thread+queue timeout guard
mirroring ``ContainerManager.exec``: on timeout the container is killed,
removed, and ``TimeoutError`` is raised. ``NotFound``/``APIError`` from the
daemon are converted to clear structured results or raised as ``RuntimeError``
with actionable messages (see per-method docstrings).
"""

import hashlib
import logging
import os
import queue
import shutil
import tempfile
import threading

try:
    import docker
    from docker.errors import APIError, ImageNotFound, NotFound
    from docker.types import Mount
    DOCKER_AVAILABLE = True
except ImportError:  # pragma: no cover - environment without docker SDK
    DOCKER_AVAILABLE = False
    docker = None
    APIError = Exception
    ImageNotFound = Exception
    NotFound = Exception
    Mount = None


_LOG = logging.getLogger(__name__)

# Resource image identity. The image is auto-built from THIS repo's unified
# runtime Dockerfile — <repo>/resources/default_dockerfile.txt plus the
# pinned <repo>/requirements.txt (the trusted code base; agent workspaces
# are separate directories, so the image definition cannot be tampered with
# from a workspace). The SAME file is the single source for both the
# executor image and the tm-resource-git resource image; the vault copy
# (~/.thoughtmachine/docker/resource/Dockerfile, seeded from
# resources/default_dockerfile.txt) is kept only as the manual-build
# fallback. Every auto-built image carries a thoughtmachine.build_hash label
# (sha256 of the exact bytes built) so a stale image — built from older
# sources — is detected and rebuilt on drift.
RESOURCE_IMAGE_TAG = "tm-resource-git"
RESOURCE_IMAGE_BUILD_CMD = (
    f"docker build -t {RESOURCE_IMAGE_TAG} -f resources/default_dockerfile.txt ."
)

# Known hidden resources. Every entry runs inside the same hardened
# ``RESOURCE_IMAGE_TAG`` image, so the build-hash drift check applies to all.
RESOURCE_REGISTRY = {
    "git": {"kind": "git"},
}

# Build-hash label + repo-sourced build inputs. The repo root is derived from
# this file's location (infra/ -> repo root).
RESOURCE_BUILD_HASH_LABEL = "thoughtmachine.build_hash"
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_REQUIREMENTS = os.path.join(_REPO_ROOT, "requirements.txt")
REPO_RUNTIME_DOCKERFILE = os.path.join(
    _REPO_ROOT, "resources", "default_dockerfile.txt"
)

# Phase 3: optional ContainerRegistry delegation behind the session-config
# `use_container_registry` flag. Defensive import — the registry must never
# break the legacy path when it is unavailable.
try:
    from infra.registry_wiring import get_active_registry, is_registry_active
except Exception:  # pragma: no cover - registry not wired
    get_active_registry = None
    is_registry_active = lambda session_config: False  # noqa: E731

# Module-level image-readiness cache + single-flight lock. Only SUCCESS is
# cached (_RESOURCE_IMAGE_READY=True); a failed check/build is retried on the
# next call.
_RESOURCE_IMAGE_READY = False
_RESOURCE_IMAGE_LOCK = threading.Lock()


def _hash_resource_bytes(requirements_bytes, dockerfile_bytes) -> str:
    """Deterministic sha256 (hex) over the exact bytes that get built."""
    digest = hashlib.sha256()
    digest.update(requirements_bytes)
    digest.update(b"\n")
    digest.update(dockerfile_bytes)
    return digest.hexdigest()


def compute_resource_build_hash(requirements_path, dockerfile_path) -> str:
    """sha256 (hex) over ``requirements bytes + b'\\n' + dockerfile bytes``.

    Deterministic and pure (no docker): used both to tag auto-built images
    (``thoughtmachine.build_hash`` label) and to detect drift between an
    existing local image and the current repo sources.

    Args:
        requirements_path: Path to the repo ``requirements.txt``.
        dockerfile_path: Path to the runtime Dockerfile source
            (``resources/default_dockerfile.txt``).

    Returns:
        str: 64-char hex digest.
    """
    with open(requirements_path, "rb") as fh:
        requirements_bytes = fh.read()
    with open(dockerfile_path, "rb") as fh:
        dockerfile_bytes = fh.read()
    return _hash_resource_bytes(requirements_bytes, dockerfile_bytes)


def _prepare_resource_build_context():
    """Stage the repo build sources into a fresh temp directory.

    Copies ``<repo>/requirements.txt`` -> ``<tmp>/requirements.txt`` and
    ``<repo>/resources/default_dockerfile.txt`` -> ``<tmp>/Dockerfile`` so the
    docker build context contains ONLY the pinned sources (never the whole
    repo). The build hash is computed from the same bytes that are copied.

    Returns:
        (context_dir: str, build_hash: str) on success, or None when the
        repo sources are missing/unreadable (already logged).
    """
    try:
        with open(REPO_REQUIREMENTS, "rb") as fh:
            requirements_bytes = fh.read()
        with open(REPO_RUNTIME_DOCKERFILE, "rb") as fh:
            dockerfile_bytes = fh.read()
    except OSError as exc:
        _LOG.warning(
            "Cannot read resource image build sources (%s, %s): %s",
            REPO_REQUIREMENTS,
            REPO_RUNTIME_DOCKERFILE,
            exc,
        )
        return None
    build_hash = _hash_resource_bytes(requirements_bytes, dockerfile_bytes)
    context_dir = tempfile.mkdtemp(prefix="tm-resource-build-")
    try:
        with open(os.path.join(context_dir, "requirements.txt"), "wb") as fh:
            fh.write(requirements_bytes)
        with open(os.path.join(context_dir, "Dockerfile"), "wb") as fh:
            fh.write(dockerfile_bytes)
    except OSError as exc:
        shutil.rmtree(context_dir, ignore_errors=True)
        _LOG.warning(
            "Failed to stage resource image build context: %s",
            exc,
        )
        return None
    return context_dir, build_hash


def _check_resource_image(client, build_hash) -> bool:
    """True when the local image exists AND its build-hash label matches.

    A missing image, or an image with a missing/mismatched
    ``thoughtmachine.build_hash`` label (drift), returns False so the caller
    rebuilds. Real daemon errors propagate (callers log and bail).

    Args:
        client: docker client with ``images.get``.
        build_hash: expected hash of the current repo build sources.

    Returns:
        bool
    """
    try:
        image = client.images.get(RESOURCE_IMAGE_TAG)
    except ImageNotFound:
        return False
    labels = getattr(image, "labels", None) or {}
    return labels.get(RESOURCE_BUILD_HASH_LABEL) == build_hash


def _ensure_resource_image() -> bool:
    """Ensure ``tm-resource-git`` exists and matches the repo build sources.

    The image is auto-built from THIS repo's pinned sources (``requirements.txt``
    + ``resources/default_dockerfile.txt``, staged into a temp build context)
    and tagged with a ``thoughtmachine.build_hash`` label (sha256 of the exact
    bytes built). Drift detection: an existing image whose label is missing or
    does not match the current repo sources is rebuilt, so a stale image is
    never silently reused.

    Single-flight: concurrent callers serialize on ``_RESOURCE_IMAGE_LOCK``
    (double-checked — the image is re-checked inside the lock before the
    build). Success is cached in ``_RESOURCE_IMAGE_READY``; failures are never
    cached so the next call retries. NEVER raises — failures are logged (with
    the manual ``docker build`` command) and reported as ``False``.

    Returns:
        bool: True when a matching image is available, False otherwise.
    """
    global _RESOURCE_IMAGE_READY
    if _RESOURCE_IMAGE_READY:
        return True
    if docker is None:
        _LOG.warning(
            "Docker SDK unavailable — cannot ensure resource image %s. "
            "Build it manually: %s",
            RESOURCE_IMAGE_TAG,
            RESOURCE_IMAGE_BUILD_CMD,
        )
        return False
    try:
        client = docker.from_env()
    except Exception as exc:
        _LOG.warning(
            "Cannot reach the Docker daemon to ensure resource image %s: %s. "
            "Build it manually: %s",
            RESOURCE_IMAGE_TAG,
            exc,
            RESOURCE_IMAGE_BUILD_CMD,
        )
        return False
    prepared = _prepare_resource_build_context()
    if prepared is None:
        return False
    context_dir, build_hash = prepared
    try:
        # Fast existence + drift check (no lock): a matching image is ready.
        try:
            if _check_resource_image(client, build_hash):
                _RESOURCE_IMAGE_READY = True
                return True
        except Exception as exc:
            _LOG.warning(
                "Failed to check for resource image %s: %s. Build it manually: %s",
                RESOURCE_IMAGE_TAG,
                exc,
                RESOURCE_IMAGE_BUILD_CMD,
            )
            return False
        # Image missing or stale — build from the repo sources, single-flight.
        with _RESOURCE_IMAGE_LOCK:
            if _RESOURCE_IMAGE_READY:
                return True
            try:
                if _check_resource_image(client, build_hash):
                    _RESOURCE_IMAGE_READY = True
                    return True
            except Exception as exc:
                _LOG.warning(
                    "Failed to re-check for resource image %s: %s. "
                    "Build it manually: %s",
                    RESOURCE_IMAGE_TAG,
                    exc,
                    RESOURCE_IMAGE_BUILD_CMD,
                )
                return False
            try:
                _LOG.info(
                    "Resource image %s missing or stale (expected build hash %s) "
                    "— building from repo sources",
                    RESOURCE_IMAGE_TAG,
                    build_hash,
                )
                _, build_log = client.images.build(
                    path=context_dir,
                    dockerfile="Dockerfile",
                    tag=RESOURCE_IMAGE_TAG,
                    rm=True,
                    labels={RESOURCE_BUILD_HASH_LABEL: build_hash},
                )
                try:
                    for entry in build_log:
                        _LOG.debug("resource image build: %s", entry)
                except Exception:
                    pass  # noisy build-log stream must never fail the build path
                _LOG.info(
                    "Resource image %s built successfully from repo sources "
                    "(build hash %s)",
                    RESOURCE_IMAGE_TAG,
                    build_hash,
                )
                _RESOURCE_IMAGE_READY = True
                return True
            except Exception as exc:
                _LOG.warning(
                    "Failed to build resource image %s from repo sources: %s. "
                    "Build it manually: %s",
                    RESOURCE_IMAGE_TAG,
                    exc,
                    RESOURCE_IMAGE_BUILD_CMD,
                )
                return False
    finally:
        # The staged context is consumed by the build (or unneeded on the
        # fast paths); always clean it up.
        shutil.rmtree(context_dir, ignore_errors=True)


def is_resource_image_available() -> bool:
    """True when the ``tm-resource-git`` image exists locally.

    Existence-only by design: this is a cheap check for tooling and must not
    trigger a build. Content correctness (build-hash drift vs the repo
    sources) is ``_ensure_resource_image``'s job — call that when the image
    must actually be used. Shares the cached success state with
    ``_ensure_resource_image``; never raises — returns False when Docker is
    unreachable or the image is missing.
    """
    if _RESOURCE_IMAGE_READY:
        return True
    if docker is None:
        return False
    try:
        docker.from_env().images.get(RESOURCE_IMAGE_TAG)
        return True
    except Exception:
        return False


def _resolve_worktree_main_repo(workspace_path, vault_root=None):
    """Resolve the MAIN repository of a git linked worktree, or None.

    A linked worktree's ``.git`` is a FILE containing a ``gitdir: <path>``
    pointer into the main repository (``<main>/.git/worktrees/<name>``).
    The resource container bind-mounts only the workspace at ``/workspace``,
    so git inside the container cannot resolve the host-only pointer and
    fails with "Not a git repository". This helper validates the pointer
    and returns the main repository root when an extra bind mount is
    warranted, so ``ensure_container()`` can mount it at its original host
    path (making the pointer resolve inside the container).

    Returns None for: regular repositories (``.git`` is a directory),
    missing/malformed ``.git`` files, submodule-style gitdirs (under
    ``.git/modules/...``), missing targets, main repos that are not real
    git directories, filesystem roots, and pointers into the vault or into
    the workspace itself (already covered by the ``/workspace`` bind).

    Args:
        workspace_path: Host path of the workspace (str or os.PathLike).
        vault_root: Vault root that must NEVER be mounted; defaults to
            ``~/.thoughtmachine``.

    Returns:
        str or None: absolute host path of the main repository, or None.
    """
    if vault_root is None:
        vault_root = os.path.join(os.path.expanduser("~"), ".thoughtmachine")

    dot_git = os.path.join(str(workspace_path), ".git")
    if not os.path.isfile(dot_git):
        return None
    try:
        with open(dot_git, "r", encoding="utf-8") as fh:
            content = fh.read()
    except OSError:
        return None

    lines = content.splitlines()
    if not lines:
        return None
    first = lines[0].strip()
    if not first.startswith("gitdir:"):
        return None
    target = first[len("gitdir:"):].strip()
    if not target:
        return None

    target = os.path.expanduser(target)
    if not os.path.isabs(target):
        # git resolves relative gitdir pointers against the .git file's dir.
        target = os.path.join(str(workspace_path), target)
    gitdir = os.path.realpath(target)

    # Linked-worktree gitdir shape: <main>/.git/worktrees/<name>. Anything
    # else (plain repo, submodule modules/<name>, relocated repo) is not a
    # linked worktree and gets no extra mount.
    if os.path.basename(os.path.dirname(gitdir)) != "worktrees":
        return None
    if not os.path.isdir(gitdir):
        return None

    # <main>/.git/worktrees/<name> -- three parents up is the main repo.
    main_root = os.path.dirname(os.path.dirname(os.path.dirname(gitdir)))
    main_dot_git = os.path.join(main_root, ".git")
    if not os.path.isdir(main_dot_git):
        return None
    # Must actually look like a git dir (HEAD / objects / refs present).
    if not any(
        os.path.exists(os.path.join(main_dot_git, part))
        for part in ("HEAD", "objects", "refs")
    ):
        return None

    mr = os.path.realpath(main_root)
    ws = os.path.realpath(str(workspace_path))
    # Already covered by the /workspace bind — never double-mount.
    if mr == ws or _path_is_within(mr, ws):
        return None
    # Filesystem root — never mount that.
    if os.path.dirname(mr) == mr:
        return None
    # The vault (secrets, credentials) is NEVER mounted.
    if _path_is_within(mr, vault_root):
        return None
    return mr


def _path_is_within(path, parent):
    """True when ``path`` equals or is nested inside ``parent`` (abs paths)."""
    p = os.path.realpath(str(path))
    base = os.path.realpath(str(parent))
    if not base:
        return False
    try:
        return os.path.commonpath([p, base]) == base
    except ValueError:  # pragma: no cover - different drives (windows)
        return False


class _ResourceContainerHandle:
    """Minimal container handle for the registry create path.

    The registry facade returns ``{"id": ...}`` dicts; this shim gives them
    the same ``.id`` surface as docker container objects so callers are
    uniform. ``__slots__`` keeps it lightweight.
    """

    __slots__ = ("id",)

    def __init__(self, container_id):
        self.id = container_id


class ResourceContainerManager:
    """Owns the hidden git resource container for ONE workspace.

    Args:
        workspace_id: The workspace id (str) the container is scoped to.
            Carried in the ``thoughtmachine.workspace_id`` label so
            ``cleanup_workspace()`` sweeps it on decommission.
        workspace_path: Host path bind-mounted (rw) at ``/workspace``.
        network_mode: Docker network mode for the container ('none' default).
            The caller should resolve this from
            ``security_gate.get_expected_container_config``; anything other
            than an explicit grant must stay 'none'.
        image: Image to run. Default 'tm-resource-git' — auto-built from
            the repo's pinned sources (``<repo>/requirements.txt`` +
            ``resources/default_dockerfile.txt``; the trusted code base, not
            the agent workspace, so the image definition cannot be tampered
            with). Every auto-built image carries a ``thoughtmachine.build_hash``
            label so a stale image is rebuilt on drift. Manual fallback::

                docker build -t tm-resource-git -f resources/default_dockerfile.txt .
        vault_root: Reserved for future audit/config use; NEVER mounted into
            the container.
        session_config: Optional session config dict; the registry feature
            flag ``use_container_registry`` is read from it. When set and
            the registry is active, ``ensure_container`` delegates the fresh
            create to ``ContainerRegistry.create_resource_container``
            (design doc docs/container_registry_design.md §6).
        session_id: Optional session id the resource container is registered
            under (registry bookkeeping only).
        session_permissions: Optional session/workspace permissions dict.
            When supplied, ``ensure_resource`` consults
            ``security_gate.get_expected_container_config`` and reports the
            resource as 'unavailable' when the effective container grant is
            False (hard deny). When None, the caller gates container usage
            itself (mirror of ``network_mode`` gating).
    """

    # Resource marker label: any value of ``thoughtmachine.resource`` marks a
    # container as hidden infrastructure (excluded from agent-facing listing).
    RESOURCE_LABEL = "thoughtmachine.resource"
    RESOURCE_KIND = "git"
    CONTAINER_NAME_LABEL = "thoughtmachine.container_name"
    WORKSPACE_LABEL = "thoughtmachine.workspace_id"

    def __init__(
        self,
        workspace_id,
        workspace_path,
        network_mode="none",
        image=RESOURCE_IMAGE_TAG,
        vault_root=None,
        session_config=None,
        session_id=None,
        session_permissions=None,
    ):
        if docker is None:
            raise RuntimeError(
                "Docker Python SDK not installed. Install with 'pip install docker'."
            )
        self.workspace_id = str(workspace_id)
        self.workspace_path = os.path.abspath(str(workspace_path)).rstrip("/")
        self.network_mode = network_mode or "none"
        self.image = image
        self.vault_root = vault_root
        self.session_config = session_config
        self.session_id = session_id
        self.session_permissions = session_permissions or {}
        # Fixed quotas (mirror the agent-tool defaults; not constructor params
        # by design — the git sandbox is a fixed-shape resource).
        self.mem_limit = "512m"
        self.cpu_quota = 50000
        self.client = docker.from_env()

    # -------------------------------------------------- registry facade
    @property
    def _registry_active(self) -> bool:
        """True when the registry feature flag is on AND usable.

        Falls back to the legacy create path when the registry is disabled
        or has no usable docker client.
        """
        try:
            return bool(is_registry_active(self.session_config))
        except Exception:
            return False

    @property
    def _registry(self):
        """Lazily-resolved registry facade for this manager's session config."""
        if get_active_registry is None:
            return None
        return get_active_registry(self.session_config)

    # ------------------------------------------------------------------ names
    @property
    def container_name(self):
        """Deterministic name: ``tm-res-<sha256(workspace_path)[:12]>-git``.

        ``tm-res-`` deliberately avoids the ``agent-exec-`` prefix that the
        web_ui startup scan and ``verify_container_integrity`` match, so the
        resource container is never swept or integrity-checked by the legacy
        executor paths.
        """
        ws_hash = hashlib.sha256(self.workspace_path.encode("utf-8")).hexdigest()[:12]
        return f"tm-res-{ws_hash}-git"

    def _labels(self, name=None):
        """Labels applied to the container at create time."""
        return {
            self.WORKSPACE_LABEL: self.workspace_id,
            self.RESOURCE_LABEL: self.RESOURCE_KIND,
            self.CONTAINER_NAME_LABEL: name or self.container_name,
        }

    # ------------------------------------------------------------ lifecycle
    def ensure_container(self):
        """Ensure the git resource container exists and is running.

        Reuse: lists containers by the ``thoughtmachine.workspace_id`` label
        and reuses one whose ``thoughtmachine.container_name`` label (or
        docker name) matches ours; a stopped match is (re)started. Otherwise
        creates a fresh container with the full hardening set.

        Raises:
            RuntimeError: if the docker SDK is unavailable, the image is
                missing (with build instructions), or creation fails.

        Returns:
            str: the container id (full id).
        """
        # The image is required even for the reuse path; auto-build it (from
        # the repo's unified runtime Dockerfile) when it is missing.
        if not _ensure_resource_image():
            raise RuntimeError(
                f"Resource image '{RESOURCE_IMAGE_TAG}' is not available "
                f"(auto-build failed or Docker unreachable). "
                f"Build it manually: {RESOURCE_IMAGE_BUILD_CMD}"
            )

        name = self.container_name
        container = self._find_resource_container(name)
        if container is not None and (
            (container.labels or {}).get(self.RESOURCE_LABEL)
            == self.RESOURCE_KIND
        ):
            if container.status != "running":
                container.start()
            return container.id

        return self._create_resource_container(name).id

    def _find_resource_container(self, name=None):
        """Find the container matching ``name`` (label or docker name).

        Lists containers by the ``thoughtmachine.workspace_id`` label and
        returns the FIRST one whose ``thoughtmachine.container_name`` label
        (or docker name) equals ``name`` (defaults to ``self.container_name``).
        NO kind filter — callers decide: ``ensure_container`` reuses only
        ``RESOURCE_KIND`` matches; ``ensure_resource`` treats a wrong-kind
        container as stale (removes and recreates it).

        Returns:
            object or None: the container, or None when absent.
        """
        name = name or self.container_name
        try:
            candidates = self.client.containers.list(
                all=True,
                filters={"label": f"{self.WORKSPACE_LABEL}={self.workspace_id}"},
            )
        except Exception:
            return None
        for container in candidates:
            labels = container.labels or {}
            if (
                labels.get(self.CONTAINER_NAME_LABEL) == name
                or container.name == name
            ):
                return container
        return None

    def _create_resource_container(self, name=None):
        """Create a fresh hardened resource container (no reuse logic).

        Builds the workspace bind mount (rw) plus the optional
        linked-worktree main-repo mount, then creates
        the container via the registry facade (when active) or
        ``client.containers.run`` with the full hardening set.

        Raises:
            RuntimeError: when creation fails (with the manual build command
                for actionable image-missing diagnostics).

        Returns:
            object: the created container (``.id`` usable) — a
                ``_ResourceContainerHandle`` on the registry path, or the
                docker container object on the legacy path.
        """
        name = name or self.container_name
        # Workspace bind mount, READ-WRITE: git writes .git + index on the
        # REAL workspace. Documented divergence from the executor's ro/.git-
        # tmpfs scheme — this container's whole purpose is operating on git
        # metadata; isolation comes from it being the ONLY mount (one
        # documented exception: the linked-worktree main repo).
        mounts = [
            Mount(
                target="/workspace",
                source=self.workspace_path,
                type="bind",
                read_only=False,
            )
        ]
        # Linked-worktree fix: when /workspace/.git is a FILE (a "gitdir: ..."
        # pointer), git inside the container cannot resolve the host-only
        # pointer and reports "Not a git repository". Bind the MAIN repository
        # at its ORIGINAL host path (rw: worktree commits write objects/refs
        # to the main repo's common git dir). This is the only extra mount
        # ever added — _resolve_worktree_main_repo() refuses pointers into
        # the vault, filesystem roots, and the workspace itself.
        main_repo = _resolve_worktree_main_repo(
            self.workspace_path, self.vault_root
        )
        if main_repo:
            mounts.append(
                Mount(
                    target=main_repo,
                    source=main_repo,
                    type="bind",
                    read_only=False,
                )
            )
        # tmpfs (same entries as ContainerManager.start / docker_executor,
        # minus the /workspace/.git shadow — we need the real .git).
        tmpfs = {
            "/tmp": "rw,noexec,nosuid,size=64m",
            "/home/agent": "rw,exec,size=256M,uid=1000,gid=1000",
        }
        try:
            if self._registry_active:
                # Phase 3: the registry facade owns the hardened create
                # (design doc §6.2). The /workspace bind is always added by
                # the registry from workspace_path (rw); the linked-worktree
                # main-repo mount computed above is passed as an extra. The
                # registry returns the same shape of handle; its
                # create failure is wrapped identically below.
                handle = self._registry.create_resource_container(
                    session_id=self.session_id or "resource",
                    workspace_id=self.workspace_id,
                    network_mode=self.network_mode,
                    workspace_path=self.workspace_path,
                    name=name,
                    mounts=[
                        {
                            "source": m["source"],
                            "target": m["target"],
                            "mode": "ro" if m["read_only"] else "rw",
                        }
                        for m in mounts[1:]
                    ],
                )
                return _ResourceContainerHandle(handle["id"])
            container = self.client.containers.run(
                image=self.image,
                name=name,
                mounts=mounts,
                tmpfs=tmpfs,
                network_mode=self.network_mode,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                oom_score_adj=500,  # resource (git) containers get a moderate OOM score
                read_only=True,
                user="1000:1000",  # must match the agent user in the Dockerfile
                detach=True,
                tty=True,
                stdin_open=True,
                command=["tail", "-f", "/dev/null"],
                mem_limit=self.mem_limit,
                cpu_quota=self.cpu_quota,
                labels=self._labels(name),
            )
        except Exception as e:
            # Wrap image-missing with actionable build instructions.
            raise RuntimeError(
                f"Failed to create git resource container: {e}. "
                f"Build the image first: {RESOURCE_IMAGE_BUILD_CMD}"
            ) from e
        return container

    @staticmethod
    def _container_image_id(container):
        """The image id a container was created from, or None.

        Tries ``container.image.id`` first (docker SDK), then falls back to
        ``container.attrs['Image']`` (the raw daemon field). Never raises.
        """
        try:
            image = getattr(container, "image", None)
            image_id = getattr(image, "id", None)
            if image_id:
                return image_id
        except Exception:
            pass
        try:
            attrs = container.attrs or {}
            return attrs.get("Image") or None
        except Exception:
            return None

    def _current_image_id(self):
        """The id of the local ``tm-resource-git`` image, or None.

        Used for drift detection: a container created from an older image
        build (different image id) is stale and gets recreated. Never raises
        — failures are logged and reported as None (stale check skipped).
        """
        try:
            image = self.client.images.get(RESOURCE_IMAGE_TAG)
            image_id = getattr(image, "id", None)
            return image_id or None
        except Exception as exc:
            _LOG.warning("Failed to resolve current resource image id: %s", exc)
            return None

    def _container_policy_denied(self):
        """Reason string when session/workspace policy denies containers.

        Opt-in: only consulted when ``session_permissions`` was supplied to
        the constructor (the caller gates container usage itself, mirroring
        ``network_mode`` gating). Uses the shared security gate via a LAZY
        import — a module-level import would create a circular import through
        the tool registry.

        Returns:
            str or None: the denial reason, or None when containers are
                allowed or the gate is unavailable.
        """
        return _container_policy_denied(self.session_permissions)

    @staticmethod
    def _resource_result(
        mode, container_id=None, status=None, image=None, detail=""
    ):
        """Structured ``ensure_resource`` result dict."""
        return {
            "mode": mode,
            "container_id": container_id,
            "status": status,
            "image": image,
            "detail": detail,
        }

    def ensure_resource(self, name):
        """Ensure the named hidden resource is available (state machine).

        Modes returned:
            'containerized': the resource container is running
                (``container_id`` set, ``status`` 'running', ``image``
                ``RESOURCE_IMAGE_TAG``).
            'host_fallback': the container could not be ensured (image
                unavailable, Docker unreachable, or create/start/remove
                failed) — the caller should fall back to a host-side
                operation.
            'unavailable': the resource name is unknown, or container
                resources are denied by session/workspace policy.

        Never raises: every failure is reported as a structured result.

        Returns:
            dict: ``{"mode", "container_id", "status", "image", "detail"}``.
        """
        entry = RESOURCE_REGISTRY.get(name)
        if entry is None:
            return self._resource_result(
                "unavailable", detail=f"unknown resource '{name}'"
            )

        denied = self._container_policy_denied()
        if denied:
            return self._resource_result(
                "unavailable",
                detail=f"container resources disabled/denied: {denied}",
            )

        if not _ensure_resource_image():
            return self._resource_result(
                "host_fallback",
                detail=(
                    "resource image unavailable (auto-build failed or Docker "
                    f"unreachable); manual build: {RESOURCE_IMAGE_BUILD_CMD}"
                ),
            )

        kind = (entry.get("kind") or self.RESOURCE_KIND).lower()
        container = self._find_resource_container()
        if container is None:
            try:
                created = self._create_resource_container()
            except Exception as exc:
                return self._resource_result(
                    "host_fallback",
                    detail=f"failed to create resource container: {exc}",
                )
            return self._resource_result(
                "containerized",
                container_id=created.id,
                status="running",
                image=RESOURCE_IMAGE_TAG,
            )

        labels = container.labels or {}
        current_image_id = self._current_image_id()
        container_image_id = self._container_image_id(container)
        stale = bool(current_image_id) and bool(container_image_id) and (
            current_image_id != container_image_id
        )
        if (
            labels.get(self.WORKSPACE_LABEL) != self.workspace_id
            or labels.get(self.RESOURCE_LABEL) != kind
        ):
            stale = True

        if stale:
            try:
                container.remove(force=True)
            except Exception as exc:
                return self._resource_result(
                    "host_fallback",
                    detail=f"failed to remove stale resource container: {exc}",
                )
            try:
                created = self._create_resource_container()
            except Exception as exc:
                return self._resource_result(
                    "host_fallback",
                    detail=f"failed to recreate resource container: {exc}",
                )
            return self._resource_result(
                "containerized",
                container_id=created.id,
                status="running",
                image=RESOURCE_IMAGE_TAG,
            )

        if container.status != "running":
            try:
                container.start()
            except Exception as exc:
                return self._resource_result(
                    "host_fallback",
                    detail=f"failed to start resource container: {exc}",
                )
        return self._resource_result(
            "containerized",
            container_id=container.id,
            status="running",
            image=RESOURCE_IMAGE_TAG,
        )

    def _container_id(self):
        """Find the current container id by labels; None when absent.

        Raises nothing — callers decide how to report a missing container.
        """
        try:
            candidates = self.client.containers.list(
                all=True,
                filters={"label": f"{self.WORKSPACE_LABEL}={self.workspace_id}"},
            )
        except Exception:
            return None
        name = self.container_name
        for container in candidates:
            labels = container.labels or {}
            if (
                labels.get(self.CONTAINER_NAME_LABEL) == name
                and labels.get(self.RESOURCE_LABEL) == self.RESOURCE_KIND
            ):
                return container.id
        return None

    def exec(self, cmd, workdir="/workspace", environment=None, timeout=30):
        """Run ``cmd`` (raw argv list) in the resource container.

        NO ``/bin/sh -c`` wrapper — the list is passed to
        ``container.exec_run(cmd=list(...))`` directly, so a hook/command
        cannot smuggle shell metacharacters through an extra shell layer.

        Timeout guard mirrors ``ContainerManager.exec`` (thread + queue):
        on timeout the container is killed and removed, then ``TimeoutError``
        is raised.

        Raises:
            RuntimeError: if the container does not exist (call
                ``ensure_container()`` first) or the exec thread died without
                a result.
            TimeoutError: if the command exceeds ``timeout`` seconds.
            NotFound/APIError: propagated from the daemon for other failures.

        Returns:
            dict: ``{"exit_code": int, "stdout": str, "stderr": str}``
        """
        container_id = self._container_id()
        if container_id is None:
            raise RuntimeError(
                "Git resource container not found; call ensure_container() first."
            )
        container = self.client.containers.get(container_id)

        exec_kwargs = {
            "cmd": list(cmd),
            "demux": True,
            "workdir": workdir,
        }
        if environment:
            exec_kwargs["environment"] = environment

        result_queue = queue.Queue()

        def _run():
            try:
                exit_code, output = container.exec_run(**exec_kwargs)
                result_queue.put((exit_code, output, None))
            except Exception as e:
                result_queue.put((None, None, e))

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        thread.join(timeout)

        if thread.is_alive():
            try:
                container.kill()
            except Exception:
                pass
            self.remove()
            raise TimeoutError(
                f"Git resource command timed out after {timeout} seconds"
            )

        try:
            exit_code, output, error = result_queue.get_nowait()
        except queue.Empty:
            raise RuntimeError("Execution thread finished but no result")
        if error is not None:
            raise error

        stdout = output[0].decode(errors="replace") if output and output[0] else ""
        stderr = output[1].decode(errors="replace") if output and output[1] else ""
        return {"exit_code": exit_code, "stdout": stdout, "stderr": stderr}

    def stop(self):
        """Stop the container (idempotent). Never raises.

        Returns:
            dict: ``{"status": "stopped"|"missing"|"error", "container_id": ...,
            "error": ...}`` — "missing" when no resource container exists.
        """
        container_id = self._container_id()
        if container_id is None:
            return {"status": "missing", "container_id": None,
                    "error": "resource container not found"}
        try:
            container = self.client.containers.get(container_id)
            container.stop(timeout=5)
            return {"status": "stopped", "container_id": container_id}
        except NotFound:
            return {"status": "missing", "container_id": container_id}
        except Exception as e:
            return {"status": "error", "container_id": container_id, "error": str(e)}

    def remove(self):
        """Stop (timeout=5) then remove (force=True). Idempotent; never raises.

        Returns:
            dict: ``{"status": "removed"|"missing"|"error", "container_id": ...,
            "error": ...}`` — "missing" (treated as already removed) when the
            container no longer exists.
        """
        container_id = self._container_id()
        if container_id is None:
            return {"status": "removed", "container_id": None,
                    "error": "resource container not found (already removed)"}
        try:
            container = self.client.containers.get(container_id)
        except NotFound:
            return {"status": "removed", "container_id": container_id}
        except Exception as e:
            return {"status": "error", "container_id": container_id, "error": str(e)}
        try:
            container.stop(timeout=5)
        except Exception:
            pass  # already stopped / already gone — removal below is authoritative
        try:
            container.remove(force=True)
            return {"status": "removed", "container_id": container_id}
        except NotFound:
            return {"status": "removed", "container_id": container_id}
        except Exception as e:
            return {"status": "error", "container_id": container_id, "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════
#  Module-level orchestration: workspace resource lifecycle
# ══════════════════════════════════════════════════════════════════════════
#
# Read-only status probe + workspace teardown / startup sweep for web_ui
# orchestration (server lifespan + unregister paths). These operate on the
# docker client directly (no ResourceContainerManager instance) and NEVER
# build or create containers — building/creating stays exclusive to
# ``_ensure_resource_image`` / ``ResourceContainerManager``.


def _container_policy_denied(session_permissions=None):
    """Reason string when session/workspace policy denies containers.

    Module-level twin of ``ResourceContainerManager._container_policy_denied``
    (the instance method delegates here) so orchestration helpers like
    ``resource_status`` can consult the same policy without a manager
    instance.

    Opt-in: only consulted when ``session_permissions`` is supplied (the
    caller gates container usage itself, mirroring ``network_mode`` gating).
    Uses the shared security gate via a LAZY import — a module-level import
    would create a circular import through the tool registry.

    Returns:
        str or None: the denial reason, or None when containers are allowed
            or the gate is unavailable.
    """
    if not session_permissions:
        return None
    try:
        from security.security_gate import get_expected_container_config
    except Exception as exc:
        _LOG.warning(
            "Security gate unavailable; skipping container policy check: %s",
            exc,
        )
        return None
    try:
        effective = get_expected_container_config(session_permissions)
    except Exception as exc:
        _LOG.warning("Security gate container policy check failed: %s", exc)
        return None
    if (effective.get("effective") or {}).get("container") is False:
        return (
            "session/workspace policy denies container usage "
            "(effective container=False)"
        )
    return None


def _join_detail(detail, part):
    """Join a detail string with a new part ('; ' separated), or the part alone."""
    return f"{detail}; {part}" if detail else part


def resource_status(name, workspace_id=None, session_permissions=None):
    """Read-only status of a named hidden resource for a workspace.

    Purely a state probe: NEVER builds the image, NEVER creates, starts or
    removes containers. Returns the same result shape as
    ``ResourceContainerManager.ensure_resource`` so callers can branch on
    ``mode`` uniformly — 'containerized' here means "will be containerized on
    the next ensure_resource call" (possibly after an auto-build /
    auto-provision / restart), 'host_fallback' means Docker itself is
    unavailable, 'unavailable' means the name is unknown or policy-denied.

    Args:
        name: Resource name from ``RESOURCE_REGISTRY`` (e.g. 'git').
        workspace_id: Workspace id whose resource container to inspect.
            Required for the container-state part of the probe; when omitted
            the container state is reported as unknown (no lookup attempted).
        session_permissions: Optional session/workspace permissions dict.
            When supplied, a hard container deny reports 'unavailable'
            (mirrors ``ResourceContainerManager.ensure_resource``).

    Returns:
        dict: ``{"mode", "container_id", "status", "image", "detail"}``.
            Never raises.
    """
    try:
        entry = RESOURCE_REGISTRY.get(name)
        if entry is None:
            return ResourceContainerManager._resource_result(
                "unavailable", detail=f"unknown resource '{name}'"
            )
        denied = _container_policy_denied(session_permissions)
        if denied:
            return ResourceContainerManager._resource_result(
                "unavailable",
                detail=f"container resources disabled/denied: {denied}",
            )
        if docker is None:
            return ResourceContainerManager._resource_result(
                "host_fallback",
                detail="docker unavailable: docker SDK not installed",
            )
        try:
            client = docker.from_env()
        except Exception as exc:
            return ResourceContainerManager._resource_result(
                "host_fallback", detail=f"docker unavailable: {exc}"
            )
        if client is None:
            return ResourceContainerManager._resource_result(
                "host_fallback",
                detail="docker unavailable: from_env() returned None",
            )
        build_hash = compute_resource_build_hash(
            REPO_REQUIREMENTS, REPO_RUNTIME_DOCKERFILE
        )
        try:
            image = client.images.get(RESOURCE_IMAGE_TAG)
        except ImageNotFound:
            return ResourceContainerManager._resource_result(
                "containerized",
                image=RESOURCE_IMAGE_TAG,
                detail="image missing/stale — will auto-build on first use",
            )
        except Exception as exc:
            return ResourceContainerManager._resource_result(
                "host_fallback", detail=f"docker unavailable: {exc}"
            )
        labels = getattr(image, "labels", None) or {}
        if labels.get(RESOURCE_BUILD_HASH_LABEL) != build_hash:
            return ResourceContainerManager._resource_result(
                "containerized",
                image=RESOURCE_IMAGE_TAG,
                detail="image missing/stale — will auto-build on first use",
            )
        if not workspace_id:
            return ResourceContainerManager._resource_result(
                "containerized",
                image=RESOURCE_IMAGE_TAG,
                detail="container state unknown (no workspace_id)",
            )
        kind = (entry.get("kind") or ResourceContainerManager.RESOURCE_KIND).lower()
        try:
            candidates = client.containers.list(
                all=True,
                filters={
                    "label": [
                        f"{ResourceContainerManager.WORKSPACE_LABEL}={workspace_id}",
                        ResourceContainerManager.RESOURCE_LABEL,
                    ]
                },
            )
        except Exception as exc:
            return ResourceContainerManager._resource_result(
                "host_fallback", detail=f"docker unavailable: {exc}"
            )
        container = None
        for candidate in candidates:
            candidate_kind = (candidate.labels or {}).get(
                ResourceContainerManager.RESOURCE_LABEL
            )
            if (candidate_kind or "").lower() == kind:
                container = candidate
                break
        if container is None:
            return ResourceContainerManager._resource_result(
                "containerized",
                image=RESOURCE_IMAGE_TAG,
                detail="container missing — will auto-provision on first use",
            )
        if container.status != "running":
            return ResourceContainerManager._resource_result(
                "containerized",
                container_id=container.id,
                status=container.status,
                image=RESOURCE_IMAGE_TAG,
                detail="container stopped — will restart on first use",
            )
        return ResourceContainerManager._resource_result(
            "containerized",
            container_id=container.id,
            status="running",
            image=RESOURCE_IMAGE_TAG,
        )
    except Exception as exc:
        return ResourceContainerManager._resource_result(
            "host_fallback", detail=f"resource status check failed: {exc}"
        )


def cleanup_workspace_resources(workspace_id):
    """Remove ALL hidden resource containers of a workspace, then the image.

    Workspace teardown: force-removes every container carrying both the
    ``thoughtmachine.resource`` marker and the workspace's
    ``thoughtmachine.workspace_id`` label, then removes the
    ``tm-resource-git`` image when NO remaining resource container (across
    ALL workspaces) still references it. The module-level image-readiness
    cache is invalidated so the next ``ensure_resource`` re-checks.

    Never raises — per-container and per-image failures are collected into
    ``detail`` and the counts returned.

    Returns:
        dict: ``{"removed_containers": int, "removed_image": bool,
        "detail": str}``.
    """
    global _RESOURCE_IMAGE_READY
    # The image may be removed below — never trust the cached readiness.
    _RESOURCE_IMAGE_READY = False
    workspace_id = str(workspace_id)
    removed_containers = 0
    removed_image = False
    detail = ""
    if docker is None:
        return {
            "removed_containers": 0,
            "removed_image": False,
            "detail": "docker SDK not installed",
        }
    try:
        client = docker.from_env()
    except Exception as exc:
        return {
            "removed_containers": 0,
            "removed_image": False,
            "detail": f"docker unavailable: {exc}",
        }
    try:
        containers = client.containers.list(
            all=True,
            filters={
                "label": [
                    ResourceContainerManager.RESOURCE_LABEL,
                    f"{ResourceContainerManager.WORKSPACE_LABEL}={workspace_id}",
                ]
            },
        )
        for container in containers:
            try:
                container.remove(force=True)
                removed_containers += 1
            except Exception as exc:
                detail = _join_detail(
                    detail,
                    f"failed to remove container "
                    f"{getattr(container, 'id', '?')}: {exc}",
                )
        try:
            image = client.images.get(RESOURCE_IMAGE_TAG)
        except ImageNotFound:
            image = None
        except Exception as exc:
            image = None
            detail = _join_detail(detail, f"failed to inspect resource image: {exc}")
        if image is not None:
            try:
                remaining = client.containers.list(
                    all=True,
                    filters={"label": ResourceContainerManager.RESOURCE_LABEL},
                )
            except Exception as exc:
                remaining = []
                detail = _join_detail(
                    detail, f"failed to list remaining resource containers: {exc}"
                )
            referenced = False
            for container in remaining:
                if ResourceContainerManager._container_image_id(container) == image.id:
                    referenced = True
                    break
            if not referenced:
                try:
                    client.images.remove(RESOURCE_IMAGE_TAG, force=True)
                    removed_image = True
                except Exception as exc:
                    detail = _join_detail(
                        detail, f"failed to remove resource image: {exc}"
                    )
    except Exception as exc:
        detail = _join_detail(detail, f"cleanup failed: {exc}")
    return {
        "removed_containers": removed_containers,
        "removed_image": removed_image,
        "detail": detail,
    }


def sweep_stale_resource_containers(registered_workspace_ids):
    """Sweep resource containers whose workspace is no longer registered.

    Startup/orphan sweep: force-removes every ``thoughtmachine.resource``
    container whose ``thoughtmachine.workspace_id`` label is missing or not
    in ``registered_workspace_ids`` (i.e. the workspace was unregistered or
    the label is corrupt). Containers of registered workspaces are NEVER
    touched — even stopped ones (their workspace may just be idle).

    Never raises — per-container failures are collected into ``detail``.

    Args:
        registered_workspace_ids: iterable of workspace ids considered
            in-use (their resource containers must be kept).

    Returns:
        dict: ``{"removed": int, "skipped_in_use": int, "detail": str}``.
    """
    registered = {str(ws) for ws in (registered_workspace_ids or [])}
    removed = 0
    skipped = 0
    detail = ""
    if docker is None:
        return {"removed": 0, "skipped_in_use": 0, "detail": "docker SDK not installed"}
    try:
        client = docker.from_env()
    except Exception as exc:
        return {
            "removed": 0,
            "skipped_in_use": 0,
            "detail": f"docker unavailable: {exc}",
        }
    try:
        containers = client.containers.list(
            all=True,
            filters={"label": ResourceContainerManager.RESOURCE_LABEL},
        )
    except Exception as exc:
        return {
            "removed": 0,
            "skipped_in_use": 0,
            "detail": f"failed to list resource containers: {exc}",
        }
    for container in containers:
        ws_id = (container.labels or {}).get(
            ResourceContainerManager.WORKSPACE_LABEL
        )
        if ws_id is None or str(ws_id) not in registered:
            try:
                container.remove(force=True)
                removed += 1
            except Exception as exc:
                detail = _join_detail(
                    detail,
                    f"failed to remove {getattr(container, 'id', '?')}: {exc}",
                )
        else:
            skipped += 1
    return {"removed": removed, "skipped_in_use": skipped, "detail": detail}


def provision_workspace_resource(workspace_id, workspace_path, session_permissions=None):
    """Provision the hidden git resource container for a workspace (best-effort).

    Thin lifecycle wrapper used by registration call-sites (the resolve-path
    endpoint, setup_workspace, ...): resolves the graded network mode via
    ``security_gate.get_expected_container_config`` (anything other than an
    explicit ``bridge`` grant stays ``'none'``), builds a
    :class:`ResourceContainerManager` for the workspace and calls
    ``ensure_resource('git')``.

    NEVER raises: any failure (docker SDK absent, daemon unreachable, policy
    deny, ...) is logged via ``_LOG`` and reported as a structured
    ``{"mode": "unavailable", "detail": ...}`` result so callers can keep
    serving without the resource.

    Args:
        workspace_id: The workspace id the resource container is scoped to.
        workspace_path: Host path of the workspace root.
        session_permissions: Optional session/workspace permissions dict used
            to resolve the network mode; ``None`` yields ``'none'``.

    Returns:
        dict: ``ensure_resource('git')`` result (normally with ``mode``
        ``'containerized'``), or ``{"mode": "unavailable", "detail": str}``
        on any failure.
    """
    network_mode = "none"
    try:
        from security.security_gate import get_expected_container_config

        config = get_expected_container_config(session_permissions or {})
        resolved = config.get("network_mode")
        if resolved in ("bridge", "none"):
            network_mode = resolved
    except Exception as exc:
        _LOG.warning(
            "provision_workspace_resource: could not resolve network mode for "
            "workspace %s (defaulting to 'none'): %s",
            workspace_id,
            exc,
        )
    try:
        manager = ResourceContainerManager(
            workspace_id,
            workspace_path,
            network_mode=network_mode,
            image=RESOURCE_IMAGE_TAG,
            vault_root=os.path.join(os.path.expanduser("~"), ".thoughtmachine"),
            session_config={},
            session_id=None,
            session_permissions=session_permissions,
        )
        return manager.ensure_resource("git")
    except Exception as exc:
        _LOG.warning(
            "provision_workspace_resource: failed to provision git resource "
            "for workspace %s: %s",
            workspace_id,
            exc,
        )
        return {"mode": "unavailable", "detail": str(exc)}


def prune_unreferenced_resource_images():
    """Prune the shared resource image once no resource container remains.

    Lists every container carrying the ``thoughtmachine.resource`` label.  When
    NONE remain anywhere (i.e. every workspace was decommissioned or swept),
    the shared ``RESOURCE_IMAGE_TAG`` image is removed if it exists and the
    module-level ``_RESOURCE_IMAGE_READY`` cache is reset so the image is
    re-built on demand.  When resource containers still exist the image is
    deliberately KEPT (it is shared, not per-workspace).

    NEVER raises — failures are collected into ``detail``.

    Returns:
        dict: ``{"removed_images": list, "remaining_containers": int,
        "detail": str}``.  When the docker SDK is unavailable or the daemon
        cannot be reached, ``{"removed_images": [], "remaining_containers": 0,
        "detail": "docker unavailable"}``.
    """
    global _RESOURCE_IMAGE_READY
    removed_images = []
    detail = ""
    if docker is None:
        return {
            "removed_images": [],
            "remaining_containers": 0,
            "detail": "docker unavailable",
        }
    try:
        client = docker.from_env()
    except Exception as exc:
        return {
            "removed_images": [],
            "remaining_containers": 0,
            "detail": "docker unavailable",
        }
    try:
        containers = client.containers.list(
            all=True,
            filters={"label": ResourceContainerManager.RESOURCE_LABEL},
        )
    except Exception as exc:
        return {
            "removed_images": [],
            "remaining_containers": 0,
            "detail": f"failed to list resource containers: {exc}",
        }
    remaining = len(containers)
    if remaining == 0:
        try:
            client.images.get(RESOURCE_IMAGE_TAG)
            try:
                client.images.remove(RESOURCE_IMAGE_TAG, force=True)
                removed_images.append(RESOURCE_IMAGE_TAG)
                _RESOURCE_IMAGE_READY = False
            except Exception as exc:
                detail = _join_detail(
                    detail, f"failed to remove resource image: {exc}"
                )
        except Exception as exc:
            # Image missing (or probe failed) — nothing to remove.
            detail = _join_detail(detail, f"resource image not present: {exc}")
    else:
        detail = _join_detail(
            detail, f"{remaining} resource container(s) still in use — image kept"
        )
    return {
        "removed_images": removed_images,
        "remaining_containers": remaining,
        "detail": detail,
    }

