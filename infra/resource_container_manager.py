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
  no network / no socket / no vault / read-only rootfs. Two documented extra
  mounts: (1) for a git linked worktree (``.git`` is a ``gitdir:`` pointer
  file) the MAIN repository is additionally bind-mounted at its original
  host path so the pointer resolves inside the container (see
  ``_resolve_worktree_main_repo``); (2) the project's ``.venv`` is
  bind-mounted READ-ONLY at its original host path so the pre-commit hook
  can run pytest with the workspace's exact dependencies (see
  ``_resolve_venv``).

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


# The resource image Dockerfile is defined in the VAULT (agent-write-blocked)
# at <vault>/docker/resource/Dockerfile; resolve it via the shared resolver
# (thoughtmachine.vault.vault_root, i.e. ~/.thoughtmachine).
from thoughtmachine.vault import vault_root


_LOG = logging.getLogger(__name__)

# Resource image identity. The Dockerfile lives in the vault
# (~/.thoughtmachine/docker/resource/Dockerfile, seeded from
# resources/resource_dockerfile.txt) so the image definition is NOT
# agent-writable — the image can never be built from a tampered file.
RESOURCE_IMAGE_TAG = "tm-resource-git"
RESOURCE_IMAGE_DOCKERFILE_DIR = vault_root() / "docker" / "resource"
RESOURCE_IMAGE_BUILD_CMD = (
    f"docker build -t {RESOURCE_IMAGE_TAG} {RESOURCE_IMAGE_DOCKERFILE_DIR}"
)

# Module-level image-readiness cache + single-flight lock. Only SUCCESS is
# cached (_RESOURCE_IMAGE_READY=True); a failed check/build is retried on the
# next call.
_RESOURCE_IMAGE_READY = False
_RESOURCE_IMAGE_LOCK = threading.Lock()


def _ensure_resource_image() -> bool:
    """Ensure the ``tm-resource-git`` image exists locally, building it if needed.

    Single-flight: concurrent callers serialize on ``_RESOURCE_IMAGE_LOCK``
    (double-checked — the image is re-checked inside the lock before the
    build). Success is cached in ``_RESOURCE_IMAGE_READY``; failures are never
    cached so the next call retries. NEVER raises — failures are logged (with
    the manual ``docker build`` command) and reported as ``False``.

    Returns:
        bool: True when the image is available, False otherwise.
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
    try:
        client.images.get(RESOURCE_IMAGE_TAG)
        _RESOURCE_IMAGE_READY = True
        return True
    except ImageNotFound:
        pass
    except Exception as exc:
        _LOG.warning(
            "Failed to check for resource image %s: %s. Build it manually: %s",
            RESOURCE_IMAGE_TAG,
            exc,
            RESOURCE_IMAGE_BUILD_CMD,
        )
        return False
    # Image missing — build it from the vault-managed Dockerfile, single-flight.
    with _RESOURCE_IMAGE_LOCK:
        if _RESOURCE_IMAGE_READY:
            return True
        try:
            client.images.get(RESOURCE_IMAGE_TAG)
            _RESOURCE_IMAGE_READY = True
            return True
        except ImageNotFound:
            pass
        except Exception as exc:
            _LOG.warning(
                "Failed to re-check for resource image %s: %s. Build it manually: %s",
                RESOURCE_IMAGE_TAG,
                exc,
                RESOURCE_IMAGE_BUILD_CMD,
            )
            return False
        try:
            _LOG.info(
                "Resource image %s missing — building from %s",
                RESOURCE_IMAGE_TAG,
                RESOURCE_IMAGE_DOCKERFILE_DIR,
            )
            _, build_log = client.images.build(
                path=str(RESOURCE_IMAGE_DOCKERFILE_DIR),
                dockerfile="Dockerfile",
                tag=RESOURCE_IMAGE_TAG,
                rm=True,
            )
            try:
                for entry in build_log:
                    _LOG.debug("resource image build: %s", entry)
            except Exception:
                pass  # noisy build-log stream must never fail the build path
            _LOG.info(
                "Resource image %s built successfully from %s",
                RESOURCE_IMAGE_TAG,
                RESOURCE_IMAGE_DOCKERFILE_DIR,
            )
            _RESOURCE_IMAGE_READY = True
            return True
        except Exception as exc:
            _LOG.warning(
                "Failed to build resource image %s from %s: %s. "
                "Build it manually: %s",
                RESOURCE_IMAGE_TAG,
                RESOURCE_IMAGE_DOCKERFILE_DIR,
                exc,
                RESOURCE_IMAGE_BUILD_CMD,
            )
            return False


def is_resource_image_available() -> bool:
    """True when the ``tm-resource-git`` image exists locally.

    Shares the cached success state with ``_ensure_resource_image``; never
    raises — returns False when Docker is unreachable or the image is missing.
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


def _resolve_venv(workspace_path):
    """Find the project's ``.venv`` directory, or None.

    Walks up from ``workspace_path`` looking for a ``.venv`` directory (the
    conventional virtualenv location) and returns the first one found, or
    None if none exists. Mirrors the host-path discovery pattern used for
    the linked-worktree main-repo mount (``_resolve_worktree_main_repo``).
    """
    if not workspace_path:
        return None
    path = os.path.abspath(str(workspace_path))
    while True:
        candidate = os.path.join(path, ".venv")
        if os.path.isdir(candidate):
            return candidate
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent


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
            the vault-managed Dockerfile ``~/.thoughtmachine/docker/resource/Dockerfile``
            (seeded from ``resources/resource_dockerfile.txt``; the vault is
            agent-write-blocked, so the image definition cannot be tampered
            with)::

                docker build -t tm-resource-git ~/.thoughtmachine/docker/resource/
        vault_root: Reserved for future audit/config use; NEVER mounted into
            the container.
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
        # Fixed quotas (mirror the agent-tool defaults; not constructor params
        # by design — the git sandbox is a fixed-shape resource).
        self.mem_limit = "512m"
        self.cpu_quota = 50000
        self.client = docker.from_env()

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
        # the vault-managed Dockerfile) when it is missing.
        if not _ensure_resource_image():
            raise RuntimeError(
                f"Resource image '{RESOURCE_IMAGE_TAG}' is not available "
                f"(auto-build failed or Docker unreachable). "
                f"Build it manually: {RESOURCE_IMAGE_BUILD_CMD}"
            )

        name = self.container_name
        try:
            candidates = self.client.containers.list(
                all=True,
                filters={"label": f"{self.WORKSPACE_LABEL}={self.workspace_id}"},
            )
        except Exception:
            candidates = []
        for container in candidates:
            labels = container.labels or {}
            is_ours = (
                labels.get(self.CONTAINER_NAME_LABEL) == name
                or container.name == name
            )
            if is_ours and labels.get(self.RESOURCE_LABEL) == self.RESOURCE_KIND:
                if container.status != "running":
                    container.start()
                return container.id

        # Workspace bind mount, READ-WRITE: git writes .git + index on the
        # REAL workspace. Documented divergence from the executor's ro/.git-
        # tmpfs scheme — this container's whole purpose is operating on git
        # metadata; isolation comes from it being the ONLY mount (two
        # documented exceptions: the linked-worktree main repo and the
        # read-only .venv below).
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
        # .venv bind mount, READ-ONLY: expose the project's virtualenv at its
        # ORIGINAL host path so the pre-commit hook can run pytest with the
        # workspace's exact dependencies. Read-only keeps the sandbox safe —
        # the container never mutates the venv. Skipped (with a warning) when
        # no .venv exists; the hook then just won't run.
        venv_path = _resolve_venv(self.workspace_path)
        if venv_path:
            mounts.append(
                Mount(
                    target=venv_path,
                    source=venv_path,
                    type="bind",
                    read_only=True,
                )
            )
        else:
            _LOG.warning(
                "No .venv found for workspace %s — pre-commit hook pytest "
                "run unavailable (skipping .venv mount)",
                self.workspace_path,
            )
        # tmpfs (same entries as ContainerManager.start / docker_executor,
        # minus the /workspace/.git shadow — we need the real .git).
        tmpfs = {
            "/tmp": "rw,noexec,nosuid,size=64m",
            "/home/agent": "rw,exec,size=256M,uid=1000,gid=1000",
        }
        try:
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
        return container.id

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
