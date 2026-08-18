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

# Resource image identity. The tm-resource-git resource image is layered on
# top of the workspace runtime image (tm-workspace-runtime:latest) via the
# git resource overlay. Both are auto-built from the VAULT's docker/resource/
# directory — ~/.thoughtmachine/docker/resource/ — which bootstrap seeds from
# the repo's pinned sources (ensure_resource_build_files, MANIFEST.json) and
# which is agent-write-blocked, so the image definitions cannot be tampered
# with from a workspace. The vault is the SINGLE authoritative source: the
# runtime image from default_runtime.Dockerfile + requirements.txt, and the
# overlay from git_overlay.Dockerfile on top of the freshly-ensured runtime
# image. Every auto-built image carries a thoughtmachine.build_hash label
# (sha256 of the exact bytes built) so a stale image — built from older
# sources — is detected and rebuilt on drift.
RUNTIME_IMAGE_TAG = "tm-workspace-runtime:latest"
RESOURCE_IMAGE_TAG = "tm-resource-git"

# Known hidden resources. Every entry runs inside the same hardened
# ``RESOURCE_IMAGE_TAG`` image, so the build-hash drift check applies to all.
RESOURCE_REGISTRY = {
    "git": {"kind": "git"},
}

# Global resource images are lifecycle-protected shared infrastructure:
# conservative cleanup/prune NEVER removes them — only an explicit global
# lifecycle operation may remove or rebuild such an image.
GLOBAL_RESOURCE_IMAGES = frozenset({RESOURCE_IMAGE_TAG})


def is_global_resource_image(image_tag):
    """True when ``image_tag`` is a protected global resource image.

    Global resource images (``GLOBAL_RESOURCE_IMAGES``) are shared
    infrastructure: ``cleanup_workspace_resources`` and
    ``prune_unreferenced_resource_images`` remove resource containers only
    and deliberately KEEP these images, because a later workspace would
    otherwise have to rebuild (or run without) the shared resource image.
    Only an explicit global lifecycle operation may remove/rebuild them.
    """
    return image_tag in GLOBAL_RESOURCE_IMAGES

# Build-hash label + build inputs. The repo root is derived from this file's
# location (infra/ -> repo root); the repo-side files below are SEEDS only —
# the vault's docker/resource/ copy is the single authoritative build source
# (tests monkeypatch the module-level VAULT_* constants to a tmp vault).
RESOURCE_BUILD_HASH_LABEL = "thoughtmachine.build_hash"
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_REQUIREMENTS = os.path.join(_REPO_ROOT, "requirements.txt")
REPO_RUNTIME_DOCKERFILE = os.path.join(
    _REPO_ROOT, "resources", "default_dockerfile.txt"
)
GIT_OVERLAY_DOCKERFILE = os.path.join(
    _REPO_ROOT, "resources", "git_resource_overlay_dockerfile.txt"
)


def _resolve_vault_root() -> str:
    """Return the vault root: THOUGHTMACHINE_VAULT_ROOT env or ~/.thoughtmachine.

    Mirrors ``thoughtmachine.vault.vault_root()`` without importing that
    module, so this module stays importable in stripped-down environments.
    """
    override = os.environ.get("THOUGHTMACHINE_VAULT_ROOT")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.expanduser("~"), ".thoughtmachine")


# VAULT-authoritative build inputs. The vault's docker/resource/ directory is
# the SINGLE source for resource-image builds: bootstrap seeds it
# (ensure_resource_build_files, MANIFEST.json) and the agent cannot write it.
VAULT_RESOURCE_DIR = os.path.join(_resolve_vault_root(), "docker", "resource")
VAULT_REQUIREMENTS = os.path.join(VAULT_RESOURCE_DIR, "requirements.txt")
VAULT_RUNTIME_DOCKERFILE = os.path.join(
    VAULT_RESOURCE_DIR, "default_runtime.Dockerfile"
)
VAULT_OVERLAY_DOCKERFILE = os.path.join(
    VAULT_RESOURCE_DIR, "git_overlay.Dockerfile"
)

# Manual build fallback (two steps): first the runtime base image, then the
# git resource overlay on top of it — both from the VAULT build directory
# (the context must contain requirements.txt for the runtime stage's COPY).
GIT_OVERLAY_BUILD_CMD = (
    f"docker build -t {RUNTIME_IMAGE_TAG} -f {VAULT_RUNTIME_DOCKERFILE} "
    f"{VAULT_RESOURCE_DIR}\n"
    f"docker build -t {RESOURCE_IMAGE_TAG} "
    f"-f {VAULT_OVERLAY_DOCKERFILE} "
    f"--build-arg BASE_IMAGE={RUNTIME_IMAGE_TAG} {VAULT_RESOURCE_DIR}"
)
RESOURCE_IMAGE_BUILD_CMD = GIT_OVERLAY_BUILD_CMD

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

    Deterministic and pure (no docker): this is the build hash of the
    WORKSPACE RUNTIME image (``tm-workspace-runtime:latest``), used both to
    tag it with the ``thoughtmachine.build_hash`` label and to detect drift
    between an existing local image and the current vault build sources.

    Args:
        requirements_path: Path to the pinned ``requirements.txt``
            (vault copy).
        dockerfile_path: Path to the runtime Dockerfile source
            (``default_runtime.Dockerfile``).

    Returns:
        str: 64-char hex digest.
    """
    with open(requirements_path, "rb") as fh:
        requirements_bytes = fh.read()
    with open(dockerfile_path, "rb") as fh:
        dockerfile_bytes = fh.read()
    return _hash_resource_bytes(requirements_bytes, dockerfile_bytes)


def _hash_overlay_bytes(
    requirements_bytes,
    runtime_dockerfile_bytes,
    overlay_dockerfile_bytes,
    runtime_image_id_bytes,
) -> str:
    """Deterministic sha256 (hex) over the exact bytes the overlay is built from."""
    digest = hashlib.sha256()
    digest.update(requirements_bytes)
    digest.update(b"\n")
    digest.update(runtime_dockerfile_bytes)
    digest.update(b"\n")
    digest.update(overlay_dockerfile_bytes)
    digest.update(b"\n")
    digest.update(runtime_image_id_bytes)
    return digest.hexdigest()


def compute_git_overlay_build_hash(
    requirements_path, runtime_dockerfile_path, overlay_dockerfile_path, runtime_image_id
) -> str:
    """sha256 (hex) over requirements + runtime dockerfile + overlay dockerfile
    + runtime image id bytes.

    Deterministic and pure (no docker): the build hash of the git resource
    overlay image (``tm-resource-git``). It covers BOTH the runtime base
    (``requirements.txt`` + ``default_runtime.Dockerfile``) AND the
    overlay dockerfile AND the exact id of the runtime image the overlay is
    built on, so any change to the base sources — or a rebuilt runtime image
    — forces the overlay to rebuild.

    Args:
        requirements_path: Path to the pinned ``requirements.txt`` (vault copy).
        runtime_dockerfile_path: Path to the runtime Dockerfile source
            (``default_runtime.Dockerfile``).
        overlay_dockerfile_path: Path to the git overlay Dockerfile source
            (``git_overlay.Dockerfile``).
        runtime_image_id: Full image id (e.g. ``sha256:...``) of the
            freshly-ensured ``tm-workspace-runtime:latest`` image.

    Returns:
        str: 64-char hex digest.
    """
    with open(requirements_path, "rb") as fh:
        requirements_bytes = fh.read()
    with open(runtime_dockerfile_path, "rb") as fh:
        runtime_dockerfile_bytes = fh.read()
    with open(overlay_dockerfile_path, "rb") as fh:
        overlay_dockerfile_bytes = fh.read()
    return _hash_overlay_bytes(
        requirements_bytes,
        runtime_dockerfile_bytes,
        overlay_dockerfile_bytes,
        runtime_image_id.encode("utf-8"),
    )


def _prepare_resource_build_context():
    """Stage the RUNTIME image build sources into a fresh temp directory.

    Copies ``<vault>/docker/resource/requirements.txt`` -> ``<tmp>/requirements.txt``
    and ``<vault>/docker/resource/default_runtime.Dockerfile`` ->
    ``<tmp>/Dockerfile`` so the docker build context contains ONLY the pinned
    sources (never the whole vault or repo). The runtime build hash is
    computed from the same bytes that are copied. (The git overlay is staged
    separately by ``_prepare_git_overlay_build_context``.)

    Returns:
        (context_dir: str, build_hash: str) on success, or None when the
        vault sources are missing/unreadable (already logged).
    """
    try:
        with open(VAULT_REQUIREMENTS, "rb") as fh:
            requirements_bytes = fh.read()
        with open(VAULT_RUNTIME_DOCKERFILE, "rb") as fh:
            dockerfile_bytes = fh.read()
    except OSError as exc:
        _LOG.warning(
            "Cannot read resource image build sources (%s, %s): %s. "
            "Seed the vault by running 'python -m thoughtmachine.bootstrap' "
            "(or ensure_user_defaults()).",
            VAULT_REQUIREMENTS,
            VAULT_RUNTIME_DOCKERFILE,
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


def _prepare_git_overlay_build_context():
    """Stage ONLY the git overlay Dockerfile into a fresh temp directory.

    The overlay build context contains a single ``Dockerfile`` (a copy of
    ``<vault>/docker/resource/git_overlay.Dockerfile``); the runtime base
    image is passed via ``--build-arg BASE_IMAGE=...`` at build time, so no
    other files are needed.

    Returns:
        str: context dir on success, or None when the overlay dockerfile is
        missing/unreadable (already logged).
    """
    try:
        with open(VAULT_OVERLAY_DOCKERFILE, "rb") as fh:
            overlay_bytes = fh.read()
    except OSError as exc:
        _LOG.warning(
            "Cannot read git overlay dockerfile %s: %s. "
            "Seed the vault by running 'python -m thoughtmachine.bootstrap' "
            "(or ensure_user_defaults()).",
            VAULT_OVERLAY_DOCKERFILE,
            exc,
        )
        return None
    context_dir = tempfile.mkdtemp(prefix="tm-resource-build-")
    try:
        with open(os.path.join(context_dir, "Dockerfile"), "wb") as fh:
            fh.write(overlay_bytes)
    except OSError as exc:
        shutil.rmtree(context_dir, ignore_errors=True)
        _LOG.warning(
            "Failed to stage git overlay build context: %s",
            exc,
        )
        return None
    return context_dir


def _check_resource_image(client, build_hash, image_tag=RESOURCE_IMAGE_TAG) -> bool:
    """True when the local image ``image_tag`` exists AND its hash label matches.

    A missing image, or an image with a missing/mismatched
    ``thoughtmachine.build_hash`` label (drift), returns False so the caller
    rebuilds. Real daemon errors propagate (callers log and bail).

    Args:
        client: docker client with ``images.get``.
        build_hash: expected hash of the current vault build sources.
        image_tag: image tag to check (runtime base or git resource overlay).

    Returns:
        bool
    """
    try:
        image = client.images.get(image_tag)
    except ImageNotFound:
        return False
    labels = getattr(image, "labels", None) or {}
    return labels.get(RESOURCE_BUILD_HASH_LABEL) == build_hash


def _runtime_and_overlay_ready(client, runtime_hash) -> bool:
    """Read-only fast check that BOTH resource images are present and current.

    The git resource overlay's expected build hash depends on the id of the
    runtime image it is built on, so the runtime image is inspected first and
    the overlay hash is derived from its (full) image id.

    Returns:
        bool: True when both images exist with matching build-hash labels.
            Real daemon errors propagate (callers log and bail).
    """
    if not _check_resource_image(client, runtime_hash, RUNTIME_IMAGE_TAG):
        return False
    runtime_image_id = client.images.get(RUNTIME_IMAGE_TAG).id
    overlay_hash = compute_git_overlay_build_hash(
        VAULT_REQUIREMENTS,
        VAULT_RUNTIME_DOCKERFILE,
        VAULT_OVERLAY_DOCKERFILE,
        runtime_image_id,
    )
    return _check_resource_image(client, overlay_hash, RESOURCE_IMAGE_TAG)


def report_vault_seed_divergence() -> list[str]:
    """Report whether the vault's resource build files differ from the repo seeds.

    The vault (``~/.thoughtmachine/docker/resource/``) is AUTHORITATIVE for
    resource-image builds; the repo ``resources/`` files are only seeds. This
    helper compares the vault copies against the repo seeds and returns a list
    of divergence labels (empty when they match). It only REPORTS — it never
    copies or rebuilds, so a stale vault is left intact and images are built
    from whatever the vault currently contains. Refreshing the vault is the
    operator's job: ``python -m thoughtmachine.bootstrap`` (or
    ``ensure_user_defaults()``) — note the vault files are ``never_overwrite``
    trust anchors, so a refresh requires removing them first.

    Returns:
        list[str]: labels of diverged/missing vault files (e.g.
            ``["requirements.txt"]``), or ``[]`` when the vault matches the
            repo seeds. Missing vault files are reported as ``"<name> (missing)"``.
    """
    pairs = (
        ("requirements.txt", VAULT_REQUIREMENTS, REPO_REQUIREMENTS),
        (
            "default_runtime.Dockerfile",
            VAULT_RUNTIME_DOCKERFILE,
            REPO_RUNTIME_DOCKERFILE,
        ),
        ("git_overlay.Dockerfile", VAULT_OVERLAY_DOCKERFILE, GIT_OVERLAY_DOCKERFILE),
    )
    diverged = []
    for label, vault_path, repo_path in pairs:
        try:
            with open(vault_path, "rb") as fh:
                vault_bytes = fh.read()
        except OSError:
            diverged.append(f"{label} (missing)")
            continue
        try:
            with open(repo_path, "rb") as fh:
                repo_bytes = fh.read()
        except OSError:
            continue
        if vault_bytes != repo_bytes:
            diverged.append(label)
    if diverged:
        _LOG.warning(
            "vault-vs-repo-seed divergence: %s; vault is authoritative — "
            "NOT rebuilding from repo sources; to refresh the vault copy run "
            "'python -m thoughtmachine.bootstrap' (or ensure_user_defaults())",
            ", ".join(diverged),
        )
    return diverged


def _ensure_resource_image() -> bool:
    """Ensure the git resource overlay image exists and matches vault sources.

    Two-stage architecture:
      Stage 1 — ``tm-workspace-runtime:latest``: the dependency-only runtime
        base image, built from the VAULT's pinned sources
        (``~/.thoughtmachine/docker/resource/requirements.txt`` +
        ``default_runtime.Dockerfile``, staged into a temp build context) and
        tagged with a ``thoughtmachine.build_hash`` label (sha256 of the exact
        bytes built).
      Stage 2 — ``tm-resource-git``: the git resource overlay, built from
        ``~/.thoughtmachine/docker/resource/git_overlay.Dockerfile`` on top of
        the freshly-ensured runtime image (``--build-arg BASE_IMAGE=...``).
        Its build-hash label covers requirements + runtime dockerfile + overlay
        dockerfile + the runtime image id, so a rebuilt runtime image forces
        the overlay to rebuild.

    The vault is the SINGLE authoritative source: builds NEVER read the repo
    directly. A vault-vs-repo seed divergence is reported (see
    ``report_vault_seed_divergence``) but never acted upon — a stale vault
    simply means the images are built from the vault's current bytes.

    Drift detection is STRICT: the overlay is ready only when its
    ``thoughtmachine.build_hash`` label equals the freshly-computed overlay
    hash. A legacy unified ``tm-resource-git`` (labeled with only the runtime
    hash) is treated as stale and rebuilt from the overlay.

    Single-flight: concurrent callers serialize on ``_RESOURCE_IMAGE_LOCK``
    (double-checked — both images are re-checked inside the lock before any
    build). Success is cached in ``_RESOURCE_IMAGE_READY``; failures are never
    cached so the next call retries. NEVER raises — failures are logged (with
    the manual ``docker build`` commands) and reported as ``False``.

    Returns:
        bool: True when both images are available and current, False otherwise.
    """
    global _RESOURCE_IMAGE_READY
    if _RESOURCE_IMAGE_READY:
        return True
    if docker is None:
        _LOG.warning(
            "Docker SDK unavailable — cannot ensure resource images %s / %s. "
            "Build them manually:\n%s",
            RUNTIME_IMAGE_TAG,
            RESOURCE_IMAGE_TAG,
            RESOURCE_IMAGE_BUILD_CMD,
        )
        return False
    try:
        client = docker.from_env()
    except Exception as exc:
        _LOG.warning(
            "Cannot reach the Docker daemon to ensure resource images %s / %s: %s. "
            "Build them manually:\n%s",
            RUNTIME_IMAGE_TAG,
            RESOURCE_IMAGE_TAG,
            exc,
            RESOURCE_IMAGE_BUILD_CMD,
        )
        return False
    runtime_prepared = _prepare_resource_build_context()
    if runtime_prepared is None:
        return False
    runtime_context_dir, runtime_hash = runtime_prepared
    try:
        # Fast existence + drift check (no lock): both images ready.
        try:
            if _runtime_and_overlay_ready(client, runtime_hash):
                _RESOURCE_IMAGE_READY = True
                return True
        except Exception as exc:
            _LOG.warning(
                "Failed to check for resource images %s / %s: %s. "
                "Build them manually:\n%s",
                RUNTIME_IMAGE_TAG,
                RESOURCE_IMAGE_TAG,
                exc,
                RESOURCE_IMAGE_BUILD_CMD,
            )
            return False
        # Something missing or stale — build from the vault sources,
        # single-flight. Report (but never act on) any vault-vs-repo seed
        # divergence first: the vault is authoritative.
        report_vault_seed_divergence()
        with _RESOURCE_IMAGE_LOCK:
            if _RESOURCE_IMAGE_READY:
                return True
            try:
                if _runtime_and_overlay_ready(client, runtime_hash):
                    _RESOURCE_IMAGE_READY = True
                    return True
            except Exception as exc:
                _LOG.warning(
                    "Failed to re-check for resource images %s / %s: %s. "
                    "Build them manually:\n%s",
                    RUNTIME_IMAGE_TAG,
                    RESOURCE_IMAGE_TAG,
                    exc,
                    RESOURCE_IMAGE_BUILD_CMD,
                )
                return False
            try:
                # Stage 1 — the workspace runtime base image.
                if not _check_resource_image(client, runtime_hash, RUNTIME_IMAGE_TAG):
                    _LOG.info(
                        "Runtime image %s missing or stale (expected build hash %s) "
                        "— building from vault sources",
                        RUNTIME_IMAGE_TAG,
                        runtime_hash,
                    )
                    _, runtime_build_log = client.images.build(
                        path=runtime_context_dir,
                        dockerfile="Dockerfile",
                        tag=RUNTIME_IMAGE_TAG,
                        rm=True,
                        labels={RESOURCE_BUILD_HASH_LABEL: runtime_hash},
                    )
                    try:
                        for entry in runtime_build_log:
                            _LOG.debug("runtime image build: %s", entry)
                    except Exception:
                        pass  # noisy build-log stream must never fail the build path
                runtime_image_id = client.images.get(RUNTIME_IMAGE_TAG).id
                overlay_hash = compute_git_overlay_build_hash(
                    VAULT_REQUIREMENTS,
                    VAULT_RUNTIME_DOCKERFILE,
                    VAULT_OVERLAY_DOCKERFILE,
                    runtime_image_id,
                )
                overlay_context_dir = _prepare_git_overlay_build_context()
                if overlay_context_dir is None:
                    return False
                try:
                    # Stage 2 — the git resource overlay on top of the runtime.
                    if not _check_resource_image(client, overlay_hash, RESOURCE_IMAGE_TAG):
                        _LOG.info(
                            "Resource image %s missing or stale "
                            "(expected build hash %s) — building overlay on %s",
                            RESOURCE_IMAGE_TAG,
                            overlay_hash,
                            RUNTIME_IMAGE_TAG,
                        )
                        _, overlay_build_log = client.images.build(
                            path=overlay_context_dir,
                            dockerfile="Dockerfile",
                            tag=RESOURCE_IMAGE_TAG,
                            rm=True,
                            buildargs={"BASE_IMAGE": RUNTIME_IMAGE_TAG},
                            labels={RESOURCE_BUILD_HASH_LABEL: overlay_hash},
                        )
                        try:
                            for entry in overlay_build_log:
                                _LOG.debug("resource overlay build: %s", entry)
                        except Exception:
                            pass  # noisy build-log stream must never fail the build path
                    _LOG.info(
                        "Resource image %s ready (overlay of %s, build hash %s)",
                        RESOURCE_IMAGE_TAG,
                        RUNTIME_IMAGE_TAG,
                        overlay_hash,
                    )
                    _RESOURCE_IMAGE_READY = True
                    return True
                finally:
                    shutil.rmtree(overlay_context_dir, ignore_errors=True)
            except Exception as exc:
                _LOG.warning(
                    "Failed to build resource image %s from vault sources: %s. "
                    "Build them manually:\n%s",
                    RESOURCE_IMAGE_TAG,
                    exc,
                    RESOURCE_IMAGE_BUILD_CMD,
                )
                return False
    finally:
        # The staged context is consumed by the build (or unneeded on the
        # fast paths); always clean it up.
        shutil.rmtree(runtime_context_dir, ignore_errors=True)


def is_resource_image_available() -> bool:
    """True when the ``tm-resource-git`` image exists locally.

    Existence-only by design: this is a cheap check for tooling and must not
    trigger a build. Content correctness (build-hash drift vs the current vault
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
        vault_root = _resolve_vault_root()

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
        image: Image to run. Default 'tm-resource-git' — the git resource
            overlay image, auto-built in two stages from the VAULT's pinned
            sources (``~/.thoughtmachine/docker/resource/``, seeded by
            bootstrap and agent-write-blocked, so the image definition cannot
            be tampered with from a workspace): first the workspace runtime
            base ``tm-workspace-runtime:latest`` from ``requirements.txt`` +
            ``default_runtime.Dockerfile``, then the overlay from
            ``git_overlay.Dockerfile`` via
            ``--build-arg BASE_IMAGE=tm-workspace-runtime:latest``. Every
            auto-built image carries a ``thoughtmachine.build_hash`` label so
            a stale image is rebuilt on drift. Manual fallback::

                docker build -t tm-workspace-runtime:latest -f ~/.thoughtmachine/docker/resource/default_runtime.Dockerfile ~/.thoughtmachine/docker/resource
                docker build -t tm-resource-git -f ~/.thoughtmachine/docker/resource/git_overlay.Dockerfile --build-arg BASE_IMAGE=tm-workspace-runtime:latest ~/.thoughtmachine/docker/resource
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
        # the vault's docker/resource sources) when it is missing.
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
        mode,
        container_id=None,
        status=None,
        image=None,
        detail="",
        failure_reason=None,
        fallback_used=False,
    ):
        """Structured ``ensure_resource`` result dict.

        ``failure_reason`` classifies WHY the resource could not be
        containerized (one of ``"unknown_resource"``, ``"policy_denied"``,
        ``"build_failed"``, ``"container_create_failed"``, ``"fallback_used"``
        or ``None`` when containerized). ``fallback_used`` is True whenever
        the caller must degrade to a host-side operation.
        """
        return {
            "mode": mode,
            "container_id": container_id,
            "status": status,
            "image": image,
            "detail": detail,
            "failure_reason": failure_reason,
            "fallback_used": fallback_used,
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
            dict: ``{"mode", "container_id", "status", "image", "detail",
            "failure_reason", "fallback_used"}``. ``failure_reason``
            classifies why containerization failed (``"unknown_resource"``,
            ``"policy_denied"``, ``"build_failed"``,
            ``"container_create_failed"``, ``"fallback_used"``, or ``None``
            when containerized) and ``fallback_used`` is True whenever the
            caller must degrade to a host-side operation. Unknown resources
            and policy denials are fail-closed (no fallback).
        """
        entry = RESOURCE_REGISTRY.get(name)
        if entry is None:
            return self._resource_result(
                "unavailable",
                detail=f"unknown resource '{name}'",
                failure_reason="unknown_resource",
            )

        denied = self._container_policy_denied()
        if denied:
            return self._resource_result(
                "unavailable",
                detail=f"container resources disabled/denied: {denied}",
                failure_reason="policy_denied",
            )

        if not _ensure_resource_image():
            return self._resource_result(
                "host_fallback",
                detail=(
                    "resource image unavailable (auto-build failed or Docker "
                    f"unreachable); manual build: {RESOURCE_IMAGE_BUILD_CMD}"
                ),
                failure_reason="build_failed",
                fallback_used=True,
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
                    failure_reason="container_create_failed",
                    fallback_used=True,
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
                    failure_reason="fallback_used",
                    fallback_used=True,
                )
            try:
                created = self._create_resource_container()
            except Exception as exc:
                return self._resource_result(
                    "host_fallback",
                    detail=f"failed to recreate resource container: {exc}",
                    failure_reason="fallback_used",
                    fallback_used=True,
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
                    failure_reason="fallback_used",
                    fallback_used=True,
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
        runtime_hash = compute_resource_build_hash(
            VAULT_REQUIREMENTS, VAULT_RUNTIME_DOCKERFILE
        )
        # Probe 1 — the workspace runtime base image.
        try:
            runtime_image = client.images.get(RUNTIME_IMAGE_TAG)
        except ImageNotFound:
            return ResourceContainerManager._resource_result(
                "containerized",
                image=RESOURCE_IMAGE_TAG,
                detail="runtime image missing/stale — will auto-build on first use",
            )
        except Exception as exc:
            return ResourceContainerManager._resource_result(
                "host_fallback", detail=f"docker unavailable: {exc}"
            )
        runtime_labels = getattr(runtime_image, "labels", None) or {}
        if runtime_labels.get(RESOURCE_BUILD_HASH_LABEL) != runtime_hash:
            return ResourceContainerManager._resource_result(
                "containerized",
                image=RESOURCE_IMAGE_TAG,
                detail="runtime image missing/stale — will auto-build on first use",
            )
        # Probe 2 — the git resource overlay built on the runtime image.
        # STRICT: ready only when the overlay's label equals the freshly
        # computed overlay hash (a legacy unified tm-resource-git labeled with
        # only the runtime hash is treated as stale).
        try:
            overlay_hash = compute_git_overlay_build_hash(
                VAULT_REQUIREMENTS,
                VAULT_RUNTIME_DOCKERFILE,
                VAULT_OVERLAY_DOCKERFILE,
                getattr(runtime_image, "id", ""),
            )
        except OSError as exc:
            _LOG.warning("Cannot read git overlay build sources: %s", exc)
            return ResourceContainerManager._resource_result(
                "host_fallback", detail=f"resource status check failed: {exc}"
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
        if labels.get(RESOURCE_BUILD_HASH_LABEL) != overlay_hash:
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
    """Remove ALL hidden resource containers of a workspace; keep the image.

    Workspace teardown: force-removes every container carrying both the
    ``thoughtmachine.resource`` marker and the workspace's
    ``thoughtmachine.workspace_id`` label. The shared ``tm-resource-git``
    image is NEVER removed here: it is a protected global resource image
    (``GLOBAL_RESOURCE_IMAGES``), so cleanup removes containers only — only
    an explicit global lifecycle operation may remove or rebuild it. The
    module-level image-readiness cache is invalidated so the next
    ``ensure_resource`` re-checks.

    Never raises — per-container failures are collected into ``detail`` and
    the counts returned.

    Returns:
        dict: ``{"removed_containers": int, "removed_image": bool,
        "detail": str}``.
    """
    global _RESOURCE_IMAGE_READY
    # Containers were removed below; the shared image is kept (protected),
    # but never trust the cached readiness after a teardown pass.
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
                if is_global_resource_image(RESOURCE_IMAGE_TAG):
                    detail = _join_detail(
                        detail,
                        f"global resource image {RESOURCE_IMAGE_TAG} is "
                        "protected — kept",
                    )
                else:
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
    the shared ``RESOURCE_IMAGE_TAG`` image is KEPT: it is a protected global
    resource image (``GLOBAL_RESOURCE_IMAGES``), so this conservative prune
    NEVER removes it — only an explicit global lifecycle operation may remove
    or rebuild it.  (A non-protected image would be removed if present and the
    ``_RESOURCE_IMAGE_READY`` cache reset so it is re-built on demand.)  When
    resource containers still exist the image is deliberately KEPT (it is
    shared, not per-workspace).

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
        if is_global_resource_image(RESOURCE_IMAGE_TAG):
            detail = _join_detail(
                detail,
                f"global resource image {RESOURCE_IMAGE_TAG} is protected — kept",
            )
        else:
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
