"""Per-workspace Docker container manager for the ThoughtMachine agent.

``ContainerManager`` is a thin, security-hardened wrapper around the Docker
SDK that owns the containers for ONE session. It replaces the old
per-execute ``importlib.reload(docker_executor)`` pattern used by
``tools.docker_code_runner``: ``docker_executor`` is imported ONCE per
process (cached behind a lock) — never reloaded.

Lifecycle
---------
1. ``start()`` — reuse a container from the in-memory registry or via label
   lookup, or create a fresh one from the session workspace/permissions.
2. ``exec()``  — run one command inside the container with a timeout.
3. ``stop()``  — stop the container (idempotent, never raises).

Containers are scoped to the WORKSPACE, not the session: they survive session
close and are swept by the module-level ``cleanup_workspace()`` when a
workspace is decommissioned.

Security posture (identical to docker_executor.DockerExecutor)
--------------------------------------------------------------
- network disabled unless the session permissions allow a bridge network
  (decided by the shared ``_compute_container_config_from_permissions`` gate)
- all capabilities dropped, no-new-privileges, read-only root filesystem
- non-root user (1000:1000), tight memory + CPU quotas
- bind-mounts the host session workspace at ``/workspace`` (read-only when
  the session lacks write permission); a per-workspace package volume
  (``tm-packages-<workspace_id>``) is mounted at ``/home/agent/.local``
  (with ``PYTHONUSERBASE`` set) — no named *workspace* volumes are used

Label scheme
------------
Every container created by this module carries:
- ``thoughtmachine.container_name=<name>``
- ``thoughtmachine.workspace_id=<workspace_id>``
Used for label-based reuse lookups and ``cleanup_workspace()`` sweeps.

Containers created on behalf of a worker sub-agent (``start(worker_name=...)``)
additionally carry ``thoughtmachine.worker=<worker_name>`` on FRESH creates.
Workers stop/remove their labelled containers at teardown and never touch
resource containers (see tools/workspace/worker.py). Reuse paths never
re-label an existing container: the worker label is only stamped at create.

Sticky notes (vault bulletin board)
-----------------------------------
Container notes are NOT stored in Docker labels (labels are immutable after
create on stock daemons - there is no label-update API). They live in a
per-workspace JSON file, ``<vault_root>/workspaces/<workspace_id>/container_notes.json``,
where the vault root is the ``vault_root`` kwarg, else the
``THOUGHTMACHINE_VAULT_ROOT`` env var, else ``~/.thoughtmachine``. Notes are
shared by every manager/session for the same workspace and survive container
recreation.

No-reload guarantee
-------------------
``docker_executor`` is imported once per process and cached; there is no
``importlib.reload`` anywhere in this module or in docker_code_runner's
execution path anymore.
"""

import hashlib
import json
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import docker
    from docker.errors import APIError, ImageNotFound, NotFound
    from docker.types import Mount
    DOCKER_AVAILABLE = True
except ImportError:
    DOCKER_AVAILABLE = False
    docker = None
    APIError = Exception
    ImageNotFound = Exception
    NotFound = Exception
    Mount = None

from agent.logging import log
from agent.logging.lifecycle import log_container_event
from thoughtmachine.audit_logger import audit_event

_audit = lambda event, data: audit_event(event, data)

try:
    from infra.registry_wiring import get_active_registry, is_registry_active
except ImportError:  # pragma: no cover - defensive
    def get_active_registry(session_config=None):
        return None

    def is_registry_active(session_config=None):
        return False


# ── Output truncation (mirrors DockerCodeRunner._truncate_output) ──────────
EXEC_OUTPUT_LIMIT_BYTES = 100 * 1024
_TRUNCATION_NOTICE = "\n...[output truncated at 100KB]..."

_docker_executor_module = None
_docker_executor_lock = threading.Lock()


def _load_docker_executor():
    """Import (and cache) docker_executor — once per process, never reloaded.

    The module lives at the repo root; it is added to ``sys.path`` when not
    already importable (this mirrors the old inline sys.path hack, minus the
    reload). The module-level MODULE_LOAD audit therefore fires exactly once
    per process instead of once per execute() call.
    """
    global _docker_executor_module
    if _docker_executor_module is not None:
        return _docker_executor_module
    with _docker_executor_lock:
        if _docker_executor_module is None:
            repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if repo_root not in sys.path:
                sys.path.insert(0, repo_root)
            import docker_executor  # noqa: F401 — deliberately NOT reloaded
            _docker_executor_module = docker_executor
        return _docker_executor_module


def _truncate_output(output):
    """Byte-truncate utf-8 output to EXEC_OUTPUT_LIMIT_BYTES + notice."""
    if output is None:
        return output
    try:
        data = output.encode("utf-8", errors="replace")
    except AttributeError:
        return output
    if len(data) <= EXEC_OUTPUT_LIMIT_BYTES:
        return output
    return data[:EXEC_OUTPUT_LIMIT_BYTES].decode("utf-8", errors="replace") + _TRUNCATION_NOTICE


def _split_docker_log_streams(raw):
    """Split docker-py's multiplexed log stream into (stdout_bytes, stderr_bytes).

    When ``stdout=True`` and ``stderr=True`` are requested together, the Docker
    API multiplexes both streams into one byte stream of 8-byte frames:

        byte 0    = stream id (1 = stdout, 2 = stderr)
        bytes 1-3 = unused
        bytes 4-7 = payload length, big-endian
        payload   = that many bytes of log output

    If the data does not look like a valid multiplexed stream (e.g. the
    container was created with ``tty=True``, in which case docker returns raw
    output with no frame headers), the whole payload is treated as stdout.
    """
    stdout_chunks, stderr_chunks = [], []
    offset = 0
    length = len(raw)
    while offset + 8 <= length:
        header = raw[offset:offset + 8]
        stream_id = header[0]
        payload_len = int.from_bytes(header[4:8], "big")
        if stream_id not in (0, 1, 2) or payload_len > length - offset - 8:
            # Malformed frame (or raw tty output with no headers) — keep the
            # remainder as stdout rather than dropping it.
            stdout_chunks.append(raw[offset:])
            break
        payload = raw[offset + 8:offset + 8 + payload_len]
        if stream_id == 2:
            stderr_chunks.append(payload)
        else:
            # stream 1 = stdout, stream 0 = init/stdin output — fold into stdout.
            stdout_chunks.append(payload)
        offset += 8 + payload_len
    if offset == 0:
        return raw, b""
    return b"".join(stdout_chunks), b"".join(stderr_chunks)


def _safe_session_tag(session_id):
    """Return a docker-safe short tag for a session id (container names).

    Strips unsafe characters (keeps [a-zA-Z0-9_.-], max 16 chars); falls back
    to a sha256 prefix when nothing safe remains; 'anon' for None.
    """
    if session_id is None:
        return "anon"
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]", "", str(session_id))[:16]
    if cleaned:
        return cleaned
    return hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:8]


class ContainerManager:
    """Owns the Docker containers for one session (start -> exec -> stop)."""

    def __init__(
        self,
        workspace_path,
        session_id=None,
        workspace_id=None,
        session_permissions=None,
        image="agent-executor",
        mem_limit="1g",
        cpu_quota=100000,
        vault_root=None,
        session_config=None,
    ):
        if docker is None:
            raise RuntimeError(
                "Docker Python SDK not installed. Install with 'pip install docker'."
            )
        self.workspace_path = os.path.abspath(workspace_path).rstrip("/")
        self.session_id = session_id
        self.session_permissions = session_permissions
        # Per-session config (e.g. container_limits.max_containers); falls back to workspace config.
        self._session_config = session_config
        self.image = image
        self.mem_limit = mem_limit
        self.cpu_quota = cpu_quota

        # In-memory registry: container name -> container id (per-manager).
        self._containers = {}

        # Borrow the shared decision helpers from docker_executor —
        # single source of truth, imported once (never reloaded).
        dex = _load_docker_executor()
        self._compute_config = dex._compute_container_config_from_permissions
        self._resolve_workspace_id = dex._resolve_workspace_id
        if workspace_id is None:
            workspace_id = self._resolve_workspace_id(self.workspace_path)
        self.workspace_id = str(workspace_id) if workspace_id is not None else "default"

        # Vault root: per-workspace config + sticky-note bulletin board live
        # under <vault_root>/workspaces/<workspace_id>/.
        self.vault_root = self._resolve_vault_root(vault_root)

        # Phase 2: per-workspace config (max_containers, disk_quota_mb) loaded
        # from <vault_root>/workspaces/<workspace_id>/config.json.
        self.workspace_config = self._load_workspace_config()
        self.max_containers = self.workspace_config.get("max_containers", 4)

        # Phase 4.5: sticky-note bulletin board (per-workspace JSON file, NOT
        # Docker labels - labels are immutable after create on real daemons).
        self.container_notes = self._load_container_notes()

        self.client = docker.from_env()

    def _load_workspace_config(self):
        """Load per-workspace config; returns a dict and NEVER raises.

        Reads ``<vault_root>/workspaces/<workspace_id>/config.json``.
        Missing file -> defaults in memory (nothing written to disk — construction
        performs no I/O). Corrupt file -> defaults in memory (file untouched).
        """
        defaults = {"max_containers": 4, "disk_quota_mb": 4096}
        config_dir = Path(self.vault_root) / "workspaces" / str(self.workspace_id)
        config_path = config_dir / "config.json"
        self.workspace_config_path = config_path
        try:
            if config_path.exists():
                try:
                    with open(config_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        return data
                    log("WARNING", "docker.container_manager",
                        f"Workspace config {config_path} is not a JSON object; using defaults")
                except (ValueError, OSError) as e:
                    log("WARNING", "docker.container_manager",
                        f"Failed to read workspace config {config_path}: {e}; using defaults")
                return dict(defaults)
            return dict(defaults)
        except Exception as e:
            log("WARNING", "docker.container_manager",
                f"Unexpected error loading workspace config {config_path}: {e}")
            return dict(defaults)

    def _save_workspace_config(self):
        """Atomically persist the workspace config; NEVER raises.

        Writes ``self.workspace_config`` to ``self.workspace_config_path``
        (parent directory created on demand).  Failures are logged, never
        raised, mirroring ``_save_container_notes``.
        """
        config_path = getattr(self, "workspace_config_path", None)
        if config_path is None:
            return
        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = config_path.with_suffix(".json.tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(getattr(self, "workspace_config", {}) or {}, f, indent=2)
            os.replace(tmp_path, config_path)
        except (OSError, ValueError) as e:
            log("WARNING", "docker.container_manager",
                f"Failed to write workspace config {config_path}: {e}")
        except Exception as e:
            log("WARNING", "docker.container_manager",
                f"Unexpected error writing workspace config {config_path}: {e}")

    def _get_max_containers(self) -> int:
        """Effective per-workspace container limit.

        Precedence: session config (``container_limits.max_containers``) ->
        workspace config (``self.max_containers``, which the workspace
        config.json or tests may override). Never raises: invalid values fall
        back to the default, and values below 1 clamp to 1.
        """
        default = self.max_containers
        try:
            limits = (self._session_config or {}).get("container_limits", {})
            value = int(limits.get("max_containers", default))
        except (AttributeError, TypeError, ValueError):
            return default
        if value < 1:
            log("WARNING", "docker.container_manager",
                f"Configured max_containers ({value}) is invalid; clamping to 1")
            return 1
        return value

    @staticmethod
    def _resolve_vault_root(vault_root=None):
        """Resolve the vault root directory (bulletin board + config storage).

        Precedence: explicit ``vault_root`` kwarg -> ``THOUGHTMACHINE_VAULT_ROOT``
        env var -> ``~/.thoughtmachine``. Returns an absolute path string.
        """
        if vault_root:
            return os.path.abspath(os.path.expanduser(str(vault_root)))
        env = os.environ.get("THOUGHTMACHINE_VAULT_ROOT")
        if env:
            return os.path.abspath(os.path.expanduser(env))
        return str(Path.home() / ".thoughtmachine")

    # ── Sticky-note bulletin board (per-workspace JSON file) ───────────
    def _notes_path(self):
        """Path of the per-workspace container_notes.json bulletin board."""
        vault_root = getattr(self, "vault_root", None) or str(Path.home() / ".thoughtmachine")
        return Path(vault_root) / "workspaces" / str(self.workspace_id) / "container_notes.json"

    def _load_container_notes(self):
        """Load the sticky-note bulletin board; returns a dict, NEVER raises.

        Reads ``<vault_root>/workspaces/<workspace_id>/container_notes.json``
        (name -> {"note": str}). Missing file -> {}; corrupt file -> WARNING log
        + {}; non-dict values are normalized to {"note": str(value or "")}.
        """
        notes_path = self._notes_path()
        try:
            if not notes_path.exists():
                return {}
            with open(notes_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                log("WARNING", "docker.container_manager",
                    f"Container notes {notes_path} is not a JSON object; ignoring")
                return {}
            normalized = {}
            for key, value in data.items():
                if isinstance(value, dict):
                    normalized[key] = {"note": str(value.get("note") or "")}
                else:
                    normalized[key] = {"note": str(value or "")}
            return normalized
        except (ValueError, OSError) as e:
            log("WARNING", "docker.container_manager",
                f"Failed to read container notes {notes_path}: {e}")
            return {}
        except Exception as e:
            log("WARNING", "docker.container_manager",
                f"Unexpected error loading container notes {notes_path}: {e}")
            return {}

    def _save_container_notes(self):
        """Atomically persist the bulletin board; NEVER raises."""
        notes_path = self._notes_path()
        try:
            notes_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = notes_path.with_suffix(".json.tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(getattr(self, "container_notes", {}) or {}, f, indent=2)
            os.replace(tmp_path, notes_path)
        except (OSError, ValueError) as e:
            log("WARNING", "docker.container_manager",
                f"Failed to write container notes {notes_path}: {e}")
        except Exception as e:
            log("WARNING", "docker.container_manager",
                f"Unexpected error writing container notes {notes_path}: {e}")

    # ── Public API ─────────────────────────────────────────────────────────
    @property
    def _registry(self):
        """Lazily-resolved ContainerRegistry facade (wired per session config)."""
        return get_active_registry(getattr(self, "_session_config", None))

    def _resolve_registry_handle(self, container_id):
        """Map a container id (or name) to the registry's tracked handle.

        Returns None when the container is not tracked by the registry (e.g.
        a legacy container created before the flag was enabled) — callers
        then fall back to the legacy docker path.  The handle carries the
        registry's ``container_type`` bookkeeping ("resource" for hidden
        resource containers), which the stop/remove registry branches use to
        refuse destroying them.
        """
        try:
            handles = self._registry.list_all()
        except Exception:
            return None
        for handle in handles or []:
            if handle.get("id") == container_id or handle.get("name") == container_id:
                return handle
        return None

    def start(self, image=None, name=None, note=None, worker_name=None):
        """Ensure a running container exists and return {"id", "name", "status", "note"}.

        Reuse order: in-memory registry -> label lookup -> fresh create.

        ``worker_name`` (optional) stamps the container with the
        ``thoughtmachine.worker`` ownership label - but only on a FRESH
        create: reuse paths return before the label dict is built, so an
        existing container keeps whatever labels it was created with. Workers
        use this label to reclaim their containers at teardown (see
        tools/workspace/worker.py).

        ``note`` is an optional sticky note: it is written to the per-workspace
        bulletin board (``<vault_root>/workspaces/<workspace_id>/container_notes.json``)
        - not to Docker labels - and returned in the response. On reuse, a new
        note overwrites the bulletin-board entry for the container name.

        Desired isolation (network_mode, workspace_mode) is computed ONCE from
        the session permissions BEFORE any reuse path, so an existing container
        is only reused when its actual network + /workspace mount mode still
        match. A drifted container is recreated with the computed modes instead
        of being silently reused (mirrors docker_executor's integrity check).
        """
        image = image or self.image
        if name is None:
            ws_hash = hashlib.sha256(self.workspace_path.encode()).hexdigest()[:12]
            name = f"agent-exec-{ws_hash}-{_safe_session_tag(self.session_id)}"

        # Hidden resource containers (tm-res-*) are never addressable through
        # the generic container manager — they are owned by the resource
        # container manager and must stay invisible here.
        if str(name).startswith("tm-res-"):
            return {"error": "Resource container access denied"}

        # Phase 3: workspace-scoped reuse + container-limit enforcement BEFORE
        # any create. An existing container with the same name is reused as-is
        # (never counted against the limit); otherwise the running container
        # count for THIS workspace decides whether a new one may be created.
        containers = self.list_containers()
        for entry in containers:
            if entry["name"] == name:
                note_value = note if note is not None else entry.get("note", "")
                try:
                    container = self.client.containers.get(entry["container_id"])
                    self._ensure_running(container)
                except Exception:
                    pass
                if note is not None:
                    self.container_notes[name] = {"note": note}
                    self._save_container_notes()
                _audit("CONTAINER_REUSE_OK",
                       f"source=workspace-label name={name} id={entry['container_id']} "
                       f"session={self.session_id}")
                log_container_event("started", container_id=entry["container_id"],
                                    session_id=self.session_id or "",
                                    data={"image": image, "name": name, "status": "reused"})
                return {**entry, "status": "reused", "id": entry["container_id"],
                        "note": note_value}
        limit = self._get_max_containers()
        # When the registry is active it owns the per-session limit; the
        # legacy workspace-scoped check is skipped so the registry is the
        # single source of truth for container counts.
        if len(containers) >= limit and not is_registry_active(getattr(self, "_session_config", None)):
            return {"error": f"Workspace container limit ({limit}) reached. "
                             f"Stop an unused container first."}

        # ── Desired isolation from session permissions (all paths) ─────────
        network_mode, workspace_mode = self._compute_config(
            self.workspace_path, self.workspace_id, self.session_permissions
        )
        _audit("CONTAINER_CONFIG",
               f"name={name} network={network_mode} workspace={workspace_mode} "
               f"session={self.session_permissions} workspace_id={self.workspace_id}")
        # Explicit-grant guard: never silently fail-closed on a session that
        # explicitly granted write access — surface it loudly instead.
        sp = self.session_permissions or {}
        if sp.get("network") == "write" and network_mode != "bridge":
            log("WARNING", "docker.container_manager",
                f"Session grants network=write but gate returned "
                f"network_mode={network_mode} (workspace_id={self.workspace_id}) "
                f"— fail-closed; workspace capabilities restrict this session "
                f"or the security gate errored (see docker.security_gate).")
        if sp.get("filesystem") in ("write", "full") and workspace_mode != "rw":
            log("WARNING", "docker.container_manager",
                f"Session grants filesystem={sp.get('filesystem')} but gate "
                f"returned workspace_mode={workspace_mode} "
                f"(workspace_id={self.workspace_id}) "
                f"— fail-closed; workspace capabilities restrict this session "
                f"or the security gate errored (see docker.security_gate).")

        # 1) Registry hit
        container_id = self._containers.get(name)
        if container_id:
            container = self._reuse_container(container_id)
            if container is not None:
                if self._config_matches(container, network_mode, workspace_mode):
                    if note is not None:
                        self.container_notes[name] = {"note": note}
                        self._save_container_notes()
                    note_value = note if note is not None else (
                        (getattr(self, "container_notes", {}) or {}).get(name) or {}
                    ).get("note", "")
                    _audit("CONTAINER_REUSE_OK",
                           f"source=registry name={name} id={container.id} session={self.session_id}")
                    log_container_event("started", container_id=container.id,
                                        session_id=self.session_id or "",
                                        data={"image": image, "name": name, "status": "reused"})
                    return {"id": container.id, "name": name, "status": "reused",
                            "note": note_value}
                log("WARNING", "docker.container_manager",
                    f"Registry container {container.id[:12]} config drifted "
                    f"(network={network_mode} workspace={workspace_mode}) — recreating")
                _audit("CONTAINER_RECREATE_MISMATCH",
                       f"name={name} id={container.id} source=registry "
                       f"network={network_mode} workspace={workspace_mode}")
                self._remove_container(container)
            self._containers.pop(name, None)  # stale entry

        # 2) Label lookup (survives manager restarts)
        container = self._find_by_labels(name)
        if container is not None:
            if not self._config_matches(container, network_mode, workspace_mode):
                log("WARNING", "docker.container_manager",
                    f"Labeled container {container.id[:12]} config drifted "
                    f"(network={network_mode} workspace={workspace_mode}) — recreating")
                _audit("CONTAINER_RECREATE_MISMATCH",
                       f"name={name} id={container.id} source=label "
                       f"network={network_mode} workspace={workspace_mode}")
                self._remove_container(container)
                container = None
            else:
                self._ensure_running(container)
                self._containers[name] = container.id
                if note is not None:
                    self.container_notes[name] = {"note": note}
                    self._save_container_notes()
                note_value = note if note is not None else (
                    (getattr(self, "container_notes", {}) or {}).get(name) or {}
                ).get("note", "")
                _audit("CONTAINER_REUSE_OK",
                       f"source=label name={name} id={container.id} session={self.session_id}")
                log_container_event("started", container_id=container.id,
                                    session_id=self.session_id or "",
                                    data={"image": image, "name": name, "status": "reused"})
                return {"id": container.id, "name": name, "status": "reused",
                        "note": note_value}

        # 3) Fresh create
        tmpfs = {
            "/tmp": "rw,noexec,nosuid,size=64m",
            "/home/agent": "rw,exec,size=256M,uid=1000,gid=1000",
        }
        if os.path.isdir(os.path.join(self.workspace_path, ".git")):
            tmpfs["/workspace/.git"] = ""

        volumes = None
        mounts = [
            Mount(
                target="/workspace", source=self.workspace_path, type="bind",
                read_only=(workspace_mode != "rw"),
            ),
            # Per-workspace package cache volume (mirrors docker_executor).
            Mount(
                target="/home/agent/.local",
                source=f"tm-packages-{self.workspace_id}",
                type="volume",
            ),
        ]

        labels = {
            "thoughtmachine.container_name": name,
            "thoughtmachine.workspace_id": self.workspace_id,
        }
        if worker_name:
            # Ownership label: lets a worker reclaim the containers it created
            # at teardown (presence-based; the value is informational).
            labels["thoughtmachine.worker"] = worker_name
        _audit("CONTAINER_CREATE",
               f"image={image} network={network_mode} name={name} session={self.session_id}")

        # Phase 3 facade: with the registry active, the fresh create (and the
        # per-session limit) is delegated to the registry's single hardened
        # creation path.  The registry generates the docker name; the facade
        # keeps its own ``name`` as the label ``thoughtmachine.container_name``
        # so label-based reuse still works on later start() calls.
        if is_registry_active(getattr(self, "_session_config", None)):
            registry = self._registry
            try:
                handle = registry.request_container(
                    self.session_id or "unknown",
                    self.session_id or "default",
                    self.session_permissions or {},
                    image=image,
                    mem_limit=self.mem_limit,
                    cpu_quota=self.cpu_quota,
                    oom_score_adj=1000,
                    labels=labels,
                    environment={"PYTHONUSERBASE": "/home/agent/.local"},
                    mounts=[{
                        "source": self.workspace_path,
                        "target": "/workspace",
                        "mode": "ro" if workspace_mode != "rw" else "rw",
                    }],
                    volumes=[f"tm-packages-{self.workspace_id}:/home/agent/.local"],
                    tmpfs=tmpfs,
                    name=name,
                )
            except RuntimeError as exc:
                if "Container limit reached" in str(exc):
                    return {"error": f"Workspace container limit reached: {exc}"}
                raise
            container_id = handle["id"]
            container_name = handle["name"]
            self._containers[name] = container_id
            if note is not None:
                self.container_notes[name] = {"note": note}
                self._save_container_notes()
            _audit("CONTAINER_CREATE",
                   f"source=registry image={image} name={container_name} "
                   f"session={self.session_id} workspace_id={self.workspace_id}")
            log_container_event("started", container_id=container_id,
                                session_id=self.session_id or "",
                                data={"image": image, "name": name, "status": "created"})
            return {"id": container_id, "name": name, "status": "created",
                    "note": note or ""}

        container = self.client.containers.run(
            image=image,
            name=name,
            volumes=volumes,
            mounts=mounts,
            tmpfs=tmpfs,
            network=network_mode,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            oom_score_adj=1000,  # user containers are the first OOM-kill victims
            read_only=True,
            user="1000:1000",
            detach=True,
            tty=True,
            stdin_open=True,
            command=["tail", "-f", "/dev/null"],
            mem_limit=self.mem_limit,
            cpu_quota=self.cpu_quota,
            environment={"PYTHONUSERBASE": "/home/agent/.local"},
            labels=labels,
        )
        try:
            container.reload()
        except Exception:
            pass
        self._containers[name] = container.id
        if note is not None:
            self.container_notes[name] = {"note": note}
            self._save_container_notes()
        log_container_event("started", container_id=container.id,
                            session_id=self.session_id or "",
                            data={"image": image, "name": name, "status": "created"})
        return {"id": container.id, "name": name, "status": "created",
                "note": note or ""}

    def exec(self, container_id, command, timeout=30, workdir="/workspace", environment=None):
        """Run ``command`` in the container; returns {"stdout","stderr","exit_code"}."""
        container = self.client.containers.get(container_id)
        if self._is_resource_container(container):
            raise PermissionError("Resource container access denied")

        # Phase 2: disk quota guard for the persistent package cache.
        quota_mb = (getattr(self, "workspace_config", None) or {}).get("disk_quota_mb", 4096)
        if quota_mb and quota_mb > 0 and self._exceeds_disk_quota(container, quota_mb):
            return {
                "stdout": "",
                "stderr": f"Package volume exceeds disk quota ({quota_mb} MB). Please clean up unused packages.",
                "exit_code": 1,
            }

        # Ensure the requested working directory exists (writable by agent)
        if workdir != "/workspace":
            container.exec_run(
                ["sh", "-c", f"mkdir -p {workdir} && chown agent:agent {workdir}"],
                workdir="/workspace",
            )

        exec_kwargs = {
            "cmd": ["/bin/sh", "-c", command],
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
            self._drop_container(container_id)
            raise TimeoutError(f"Command timed out after {timeout} seconds")

        try:
            exit_code, output, error = result_queue.get_nowait()
        except queue.Empty:
            raise RuntimeError("Execution thread finished but no result")
        if error is not None:
            raise error

        stdout = output[0].decode(errors="replace") if output and output[0] else ""
        stderr = output[1].decode(errors="replace") if output and output[1] else ""
        # Phase 6: persistent usage log (best-effort; never affects the result).
        self._append_usage_log(container_id, command)
        return {
            "stdout": _truncate_output(stdout),
            "stderr": _truncate_output(stderr),
            "exit_code": exit_code,
        }

    def _exceeds_disk_quota(self, container, quota_mb):
        """True if /home/agent/.local usage (KB) exceeds quota_mb MB.

        Best-effort: any error (missing dir, non-running container, exec
        failure) returns False so the user command is never blocked.
        """
        try:
            container.reload()
            if container.status != "running":
                return False
            exit_code, output = container.exec_run(
                cmd=["/bin/sh", "-c", "du -s /home/agent/.local 2>/dev/null || echo 0"]
            )
            if exit_code != 0:
                return False
            text = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output)
            kb = int(float(text.strip().split()[0]))
            return kb > quota_mb * 1024
        except Exception:
            return False

    def stop(self, container_id):
        """Stop the container. Idempotent; NEVER raises."""
        if is_registry_active(getattr(self, "_session_config", None)):
            handle = self._resolve_registry_handle(container_id)
            if handle is not None:
                # Resource containers are tracked by the registry too (their
                # factory registers them with container_type="resource");
                # refuse to destroy them here just like the legacy path does.
                if handle.get("container_type") == "resource":
                    return {"status": "error", "container_id": container_id,
                            "error": "Resource container access denied"}
                name = handle.get("name")
                try:
                    self._registry.destroy_container(name)
                except Exception as e:
                    return {"status": "error", "container_id": container_id,
                            "error": str(e)}
                self._drop_container(container_id)
                log_container_event("stopped", container_id=container_id,
                                    session_id=self.session_id or "")
                return {"status": "stopped", "container_id": container_id,
                        "name": name}
        try:
            container = self.client.containers.get(container_id)
        except NotFound:
            return {"status": "missing", "container_id": container_id,
                    "error": "container not found"}
        except Exception as e:
            return {"status": "error", "container_id": container_id, "error": str(e)}
        if self._is_resource_container(container):
            return {"status": "error", "container_id": container_id,
                    "error": "Resource container access denied"}
        try:
            container.reload()
        except Exception:
            pass
        try:
            if container.status == "running":
                _audit("CONTAINER_STOP",
                       f"container={container_id} session={self.session_id}")
                container.stop(timeout=5)
                container.reload()
                if container.status == "running":
                    container.kill()
            self._drop_container(container_id)
            log_container_event("stopped", container_id=container_id,
                                session_id=self.session_id or "")
            return {"status": "stopped", "container_id": container_id, "name": container.name}
        except Exception as e:
            return {"status": "error", "container_id": container_id, "error": str(e),
                    "name": getattr(container, "name", "")}

    def remove(self, container_id):
        """Remove the container. Idempotent; NEVER raises.

        Stops the container first (best-effort, via :meth:`stop`) then removes
        it with ``force=True``. Returns one of:
            {"status": "removed", "container_id": ...}
            {"status": "error", "container_id": ..., "error": ...}
        """
        if is_registry_active(getattr(self, "_session_config", None)):
            handle = self._resolve_registry_handle(container_id)
            if handle is not None:
                # Resource containers are tracked by the registry too (their
                # factory registers them with container_type="resource");
                # refuse to destroy them here just like the legacy path does.
                if handle.get("container_type") == "resource":
                    return {"status": "error", "container_id": container_id,
                            "error": "Resource container access denied"}
                name = handle.get("name")
                try:
                    self._registry.destroy_container(name)
                except Exception as e:
                    return {"status": "error", "container_id": container_id,
                            "error": str(e)}
                self._drop_container(container_id)
                log_container_event("removed", container_id=container_id,
                                    session_id=self.session_id or "")
                return {"status": "removed", "container_id": container_id,
                        "name": name}
        stopped = self.stop(container_id)
        if stopped.get("status") not in ("stopped", "missing"):
            return stopped
        try:
            container = self.client.containers.get(container_id)
        except NotFound:
            return {"status": "removed", "container_id": container_id}
        except Exception as e:
            return {"status": "error", "container_id": container_id, "error": str(e)}
        try:
            container.remove(force=True)
            self._drop_container(container_id)
            log_container_event("removed", container_id=container_id,
                                session_id=self.session_id or "")
            return {"status": "removed", "container_id": container_id}
        except Exception as e:
            return {"status": "error", "container_id": container_id, "error": str(e)}

    def status(self, container_id):
        """Report container status; NEVER raises."""
        try:
            container = self.client.containers.get(container_id)
        except NotFound:
            return {"status": "missing", "container_id": container_id,
                    "error": "container not found"}
        except Exception as e:
            return {"status": "error", "container_id": container_id, "error": str(e)}
        if self._is_resource_container(container):
            return {"status": "error", "container_id": container_id,
                    "error": "Resource container access denied"}
        try:
            container.reload()
        except Exception:
            pass

        uptime_seconds = None
        started_at = (container.attrs.get("State") or {}).get("StartedAt")
        if started_at:
            try:
                ts = datetime.fromisoformat(started_at.replace("Z", "+00:00")).timestamp()
                uptime_seconds = max(0, int(time.time() - ts))
            except Exception:
                uptime_seconds = None

        memory_usage_bytes = None
        try:
            stats = self.client.stats(container_id, stream=False)
            memory_usage_bytes = (stats.get("memory_stats") or {}).get("usage")
        except Exception:
            memory_usage_bytes = None

        result = {
            "container_id": container_id,
            "name": container.name,
            "status": container.status,
            "uptime_seconds": uptime_seconds,
            "memory_usage_bytes": memory_usage_bytes,
            "note": ((getattr(self, "container_notes", {}) or {}).get(container.name)
                     or {}).get("note", ""),
        }

        # Phase 6: live introspection only for running containers. Every probe
        # is best-effort via _exec_checked(); failures never raise and are
        # reported in introspection_errors (only present when non-empty).
        if container.status == "running":
            introspection_errors = []

            exit_code, stdout = (
                self._exec_checked(container, "pip list --format=json") or (None, "")
            )
            if exit_code == 0:
                try:
                    packages = json.loads(stdout)
                    if isinstance(packages, list):
                        result["installed_packages"] = [
                            {"name": p.get("name"), "version": p.get("version")}
                            for p in packages if isinstance(p, dict)
                        ]
                    else:
                        introspection_errors.append("pip list returned non-list JSON")
                except (ValueError, TypeError):
                    introspection_errors.append("pip list JSON unparseable")
            else:
                introspection_errors.append("pip list failed")

            exit_code, stdout = (
                self._exec_checked(
                    container,
                    "for p in /proc/[0-9]*; do pid=${p#/proc/}; "
                    "cmd=$(tr '\\0' ' ' < \"$p/cmdline\" 2>/dev/null); "
                    "[ -n \"$cmd\" ] && printf '%s\\t%s\\n' \"$pid\" \"$cmd\"; done",
                )
                or (None, "")
            )
            if exit_code == 0:
                processes = []
                for line in stdout.splitlines():
                    pid, sep, cmd = line.partition("\t")
                    if sep and pid.isdigit():
                        processes.append({"pid": int(pid), "command": cmd})
                result["running_processes"] = processes
            else:
                introspection_errors.append("process scan failed")

            exit_code, stdout = (
                self._exec_checked(
                    container, "du -sh /workspace /home/agent/.local 2>/dev/null"
                )
                or (None, "")
            )
            if exit_code == 0:
                disk = self._parse_disk_usage(stdout)
                result["disk_usage"] = disk if disk else stdout.strip()
            else:
                introspection_errors.append("du failed")

            result["recent_commands"] = self.container_history(container_id, tail=20)

            if introspection_errors:
                result["introspection_errors"] = introspection_errors

        return result

    def _exec_checked(self, container, command, timeout=10):
        """Run ``command`` in ``container``; return (exit_code, stdout_str) or None.

        Best-effort introspection helper: NEVER raises and NEVER kills the
        container. Any failure (daemon error, timeout, missing output) yields
        None so callers can degrade gracefully.
        """
        result_queue = queue.Queue()

        def _run():
            try:
                exit_code, output = container.exec_run(
                    cmd=["/bin/sh", "-c", command], demux=True
                )
                result_queue.put((exit_code, output, None))
            except Exception as e:
                result_queue.put((None, None, e))

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            return None
        try:
            exit_code, output, error = result_queue.get_nowait()
        except queue.Empty:
            return None
        if error is not None:
            return None
        stdout = output[0].decode(errors="replace") if output and output[0] else ""
        return exit_code, stdout

    def _append_usage_log(self, container_id, command):
        """Append one line to the container's persistent usage log; NEVER raises.

        Line format: ``<utc iso ts> | <session id or anon> | <normalised command>``
        appended to ``/home/agent/.local/usage.log`` (the persistent package
        volume, so it survives container recreation). Best-effort: any failure
        only logs a WARNING and never affects the caller's result. No rotation.
        """
        try:
            container = self.client.containers.get(container_id)
            ts = datetime.now(timezone.utc).isoformat()
            session = str(self.session_id) if self.session_id is not None else "anon"
            line = f"{ts} | {session} | {' '.join(command.split())}"
            escaped = line.replace("'", "'\\''")
            self._exec_checked(
                container,
                f"printf '%s\\n' '{escaped}' >> /home/agent/.local/usage.log",
            )
        except Exception as e:
            log("WARNING", "docker.container_manager", f"usage log append failed: {e}")

    def container_history(self, container_id, tail=50):
        """Return the last ``tail`` usage-log lines for a container; NEVER raises.

        Reads ``/home/agent/.local/usage.log`` via a best-effort exec. Missing,
        non-running containers and any exec failure yield [].
        """
        try:
            container = self.client.containers.get(container_id)
            container.reload()
            if container.status != "running":
                return []
            result = self._exec_checked(
                container,
                f"tail -n {int(tail)} /home/agent/.local/usage.log 2>/dev/null",
            )
            if result is None:
                return []
            exit_code, stdout = result
            if exit_code != 0:
                return []
            return stdout.splitlines()
        except Exception:
            return []

    def container_summary(self, container_id):
        """Compact introspection for list-style responses; {} unless running.

        Runs only two best-effort execs (pip list + du) so callers can decorate
        container entries cheaply. NEVER raises.
        """
        try:
            container = self.client.containers.get(container_id)
            container.reload()
            if container.status != "running":
                return {}
            summary = {}
            exit_code, stdout = self._exec_checked(container, "pip list --format=json")
            if exit_code == 0:
                try:
                    packages = json.loads(stdout)
                    if isinstance(packages, list):
                        summary["packages_count"] = len(packages)
                except (ValueError, TypeError):
                    pass
            exit_code, stdout = self._exec_checked(
                container, "du -sh /workspace /home/agent/.local 2>/dev/null"
            )
            if exit_code == 0:
                disk = self._parse_disk_usage(stdout)
                if disk:
                    summary["disk_usage"] = disk
            return summary
        except Exception:
            return {}

    @staticmethod
    def _parse_disk_usage(text):
        """Parse ``du -sh`` output into {"workspace": size, "packages": size}."""
        try:
            usage = {}
            for line in text.splitlines():
                parts = line.split("\t")
                if len(parts) == 2:
                    size, path = parts
                    if path == "/workspace":
                        usage["workspace"] = size
                    elif path == "/home/agent/.local":
                        usage["packages"] = size
            return usage
        except Exception:
            return {}

    def list_containers(self):
        """List containers carrying this workspace's label; NEVER raises.

        Queries the daemon for all containers (running or not) whose
        ``thoughtmachine.workspace_id`` label matches this manager's workspace
        id (the exact label source ``start()`` applies), so containers from
        other workspaces — or unlabeled ones — never appear. ``note`` comes
        from the per-workspace bulletin board (container_notes.json), not from
        Docker labels. Returns a list of dicts with EXACTLY: ``container_id``,
        ``name``, ``image``, ``status``, ``uptime_seconds``, ``workspace_id``,
        ``note``.
        """
        try:
            containers = self.client.containers.list(
                all=True,
                filters={"label": f"thoughtmachine.workspace_id={self.workspace_id}"},
            )
        except Exception:
            return []

        result = []
        for container in containers:
            # Skip hidden resource containers (e.g. the git sandbox from
            # infra/resource_container_manager.py, label thoughtmachine.resource):
            # they carry the workspace_id label so cleanup_workspace sweeps them,
            # but must stay invisible to agent-facing listings.
            if (container.labels or {}).get("thoughtmachine.resource"):
                continue
            # uptime: now - StartedAt (mirrors status()); None when missing/unparseable
            uptime_seconds = None
            started_at = (container.attrs.get("State") or {}).get("StartedAt")
            if started_at:
                try:
                    ts = datetime.fromisoformat(started_at.replace("Z", "+00:00")).timestamp()
                    uptime_seconds = max(0, int(time.time() - ts))
                except Exception:
                    uptime_seconds = None

            # image: first tag when available; None when image/tags missing
            image = None
            try:
                image_obj = getattr(container, "image", None)
                tags = getattr(image_obj, "tags", None) or []
                image = tags[0] if tags else None
            except Exception:
                image = None

            result.append({
                "container_id": container.id,
                "name": container.name,
                "image": image,
                "status": container.status,
                "uptime_seconds": uptime_seconds,
                "workspace_id": (container.labels.get("thoughtmachine.workspace_id")
                                 or self.workspace_id),
                "note": ((getattr(self, "container_notes", {}) or {}).get(container.name)
                         or {}).get("note", ""),
            })
        return result

    def build_image(self, tag=None):
        """Build a Docker image from ONLY the vault-managed Dockerfile.

        Vault-gated: always builds from the vault-managed ``<workspace>/Dockerfile``
        (resolved from ``<vault_root>/workspaces/<workspace_id>/Dockerfile``,
        falling back to ``<workspace_path>/Dockerfile`` — no ``dockerfile_path``
        override). The build context contains the Dockerfile plus
        ``requirements.txt`` when one is present (vault workspace dir first,
        then workspace root): both are copied into a temporary build directory,
        so the rest of the workspace tree is NOT part of the build context and
        ``COPY .`` cannot read workspace files. The build runs synchronously
        and its output is returned (not just a bool).

        Args:
            tag: Image tag; auto-generated from the workspace path (the same
                ``agent-executor-<hash>`` convention ``docker_executor`` uses)
                when omitted.

        Returns:
            Dict with EXACTLY ``image_tag`` and ``build_log`` (the build log,
            truncated to 100KB with a truncation notice).

        Raises:
            RuntimeError: If the vault Dockerfile is missing or the build fails.
        """
        if not DOCKER_AVAILABLE or self.client is None:
            raise RuntimeError("Docker Python SDK not available")

        ws = self.workspace_path
        # Vault-gated resolution: prefer the vault-managed Dockerfile
        # (<vault_root>/workspaces/<workspace_id>/Dockerfile), falling back to
        # the workspace-path Dockerfile for legacy workspaces.
        vault_dockerfile = (
            Path(self.vault_root) / "workspaces" / str(self.workspace_id) / "Dockerfile"
        )
        if not vault_dockerfile.exists():
            legacy = Path(ws) / "Dockerfile"
            if legacy.exists():
                vault_dockerfile = legacy
            else:
                raise RuntimeError(
                    f"Vault Dockerfile not found at {vault_dockerfile}. "
                    "The vault-managed <workspace>/Dockerfile must exist before building."
                )

        dex = _load_docker_executor()
        if not tag:
            tag = dex._compute_image_tag(ws)

        # Build-drift gate: reuse an existing image whose
        # thoughtmachine.build_hash label still matches the current build
        # sources — the vault-managed workspace Dockerfile (build context) plus
        # the executor build sources resolved via docker_executor; otherwise
        # rebuild with the fresh hash recorded as the label.
        try:
            build_hash = dex.compute_executor_build_hash()
        except OSError as e:
            raise RuntimeError(
                f"Cannot read executor build sources ({e}); the vault-managed "
                "workspace Dockerfile and the executor build sources "
                "(requirements.txt + default Dockerfile, via docker_executor) "
                "must exist."
            ) from e
        try:
            existing = self.client.images.get(tag)
        except ImageNotFound:
            existing = None
        if existing is not None and (
            (getattr(existing, "labels", None) or {}).get(dex.EXECUTOR_BUILD_HASH_LABEL)
            == build_hash
        ):
            log("INFO", "docker.container_manager",
                f"Image {tag} already matches build sources \u2014 skipping build")
            return {"image_tag": tag, "build_log": ""}
        if existing is not None:
            log("INFO", "docker.container_manager",
                f"Image {tag} build sources drifted (label mismatch) \u2014 rebuilding")

        try:
            # SECURITY: the build context contains ONLY the vault Dockerfile
            # (plus requirements.txt when present — needed by the image defs
            # that `COPY requirements.txt` before pip install). It is copied
            # into a temporary build directory so the workspace tree is never
            # part of the build context (no COPY . exfiltration).
            with tempfile.TemporaryDirectory(prefix="tm_build_") as tmpdir:
                shutil.copy2(str(vault_dockerfile), os.path.join(tmpdir, "Dockerfile"))
                req_vault = (
                    Path(self.vault_root) / "workspaces" / str(self.workspace_id) / "requirements.txt"
                )
                req_ws = Path(ws) / "requirements.txt"
                req_src = req_vault if req_vault.exists() else (req_ws if req_ws.exists() else None)
                if req_src is not None:
                    shutil.copy2(str(req_src), os.path.join(tmpdir, "requirements.txt"))
                staged = set(os.listdir(tmpdir))
                if not staged <= {"Dockerfile", "requirements.txt"}:
                    raise RuntimeError(
                        f"Unexpected files staged in build context: {sorted(staged)}"
                    )
                _, log_lines = dex._run_image_build(
                    self.client, tmpdir, "Dockerfile", tag,
                    labels={dex.EXECUTOR_BUILD_HASH_LABEL: build_hash},
                )
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"Docker build failed: {e}") from e

        build_log = "\n".join(log_lines)
        if len(build_log) > EXEC_OUTPUT_LIMIT_BYTES:
            build_log = build_log[:EXEC_OUTPUT_LIMIT_BYTES] + _TRUNCATION_NOTICE
        return {"image_tag": tag, "build_log": build_log}

    def get_logs(self, container_id, tail=100, since=None):
        """Fetch the stdout/stderr logs of a container.

        Args:
            container_id: Container ID or name.
            tail: Number of log lines to fetch from the end (default 100).
            since: Optional timestamp/duration (e.g. ``'10m'``, RFC3339, or a
                Unix timestamp) passed through to Docker unmodified — only log
                entries emitted after this time are returned.

        Returns:
            Dict with EXACTLY ``stdout`` and ``stderr`` — each a utf-8 string,
            individually truncated to 100KB with a truncation notice.

        Raises:
            RuntimeError: If the container does not exist, the daemon cannot be
                reached, or log retrieval fails.
        """
        if not DOCKER_AVAILABLE or self.client is None:
            raise RuntimeError("Docker Python SDK not available")

        try:
            container = self.client.containers.get(container_id)
        except NotFound:
            raise RuntimeError(f"Container {container_id} not found") from None
        except Exception as e:
            raise RuntimeError(
                f"Failed to access container {container_id}: {e}"
            ) from e

        if self._is_resource_container(container):
            raise RuntimeError("Resource container access denied")

        try:
            raw = container.logs(
                stdout=True, stderr=True, tail=tail, since=since
            )
        except Exception as e:
            raise RuntimeError(
                f"Failed to fetch logs for container {container_id}: {e}"
            ) from e

        if not isinstance(raw, bytes):
            raw = str(raw).encode("utf-8", errors="replace")

        stdout_bytes, stderr_bytes = _split_docker_log_streams(raw)
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        if len(stdout) > EXEC_OUTPUT_LIMIT_BYTES:
            stdout = stdout[:EXEC_OUTPUT_LIMIT_BYTES] + _TRUNCATION_NOTICE
        if len(stderr) > EXEC_OUTPUT_LIMIT_BYTES:
            stderr = stderr[:EXEC_OUTPUT_LIMIT_BYTES] + _TRUNCATION_NOTICE

        log(
            "DEBUG",
            "docker.container_manager",
            f"get_logs container={container_id} tail={tail} since={since} "
            f"stdout={len(stdout_bytes)}B stderr={len(stderr_bytes)}B",
        )
        return {"stdout": stdout, "stderr": stderr}

    @staticmethod
    def _is_resource_container(obj):
        """True when ``obj`` is a hidden resource container (tm-res-*).

        Hidden resource containers (managed exclusively by the resource
        container manager) must never be addressable through the generic
        container manager. They are recognized by their
        ``thoughtmachine.resource`` label, their ``tm-res-`` name prefix, or
        their ``tm-resource-git`` image. Any probe failure is treated as
        False (a non-resource container).
        """
        try:
            labels = getattr(obj, "labels", None) or {}
            label_val = labels.get("thoughtmachine.resource")
            if isinstance(label_val, str) and label_val:
                return True
        except Exception:
            pass
        try:
            name = str(getattr(obj, "name", "") or "")
            if name.startswith("tm-res-"):
                return True
        except Exception:
            pass
        try:
            image = getattr(obj, "image", None)
            if image is None:
                return False
            if isinstance(image, str):
                return image == "tm-resource-git"
            tags = getattr(image, "tags", None) or []
            return any(
                tag == "tm-resource-git" or tag.startswith("tm-resource-git:")
                for tag in tags
            )
        except Exception:
            return False

    # ── Internals ──────────────────────────────────────────────────────────

    def _reuse_container(self, container_id):
        """Return a running container for ``container_id`` or None."""
        if not container_id:
            return None
        try:
            container = self.client.containers.get(container_id)
            self._ensure_running(container)
            return container
        except Exception:
            return None

    def _find_by_labels(self, name):
        """Find a container by its thoughtmachine labels (or None)."""
        try:
            containers = self.client.containers.list(
                all=True,
                filters={
                    "label": [
                        f"thoughtmachine.container_name={name}",
                        f"thoughtmachine.workspace_id={self.workspace_id}",
                    ]
                },
            )
            if not containers:
                return None
            first = containers[0]
            if self._is_resource_container(first):
                return None
            return first
        except Exception:
            return None

    def _ensure_running(self, container):
        try:
            container.reload()
        except Exception:
            pass
        try:
            if container.status != "running":
                container.start()
                container.reload()
        except Exception:
            pass

    def set_note(self, container_id, note):
        """Set the sticky note in the per-workspace bulletin board; NEVER raises.

        Writes ``<vault_root>/workspaces/<workspace_id>/container_notes.json``
        (name -> {"note": str}) — the same file ``start()`` reads on reuse, so
        the note survives manager/session restarts and is visible to every
        manager of the workspace. Docker labels are never touched (they are
        immutable after create on stock daemons).

        Returns {"success": True, "note": note} on success; on failure an error
        dict following the existing convention:
        {"success": False, "container_id": ..., "error": "container not found"}
        or {"success": False, "container_id": ..., "error": str(e)}.
        """
        try:
            container = self.client.containers.get(container_id)
        except NotFound:
            return {"success": False, "container_id": container_id,
                    "error": "container not found"}
        except Exception as e:
            return {"success": False, "container_id": container_id, "error": str(e)}
        try:
            container.reload()
        except Exception:
            pass
        self.container_notes[container.name] = {"note": note}
        self._save_container_notes()
        return {"success": True, "note": note}

    def _drop_container(self, container_id):
        """Remove all registry entries pointing at ``container_id``."""
        for key in [k for k, v in self._containers.items() if v == container_id]:
            self._containers.pop(key, None)

    @staticmethod
    def _normalize_mount_mode(mode):
        """Map a Docker mount Mode string to canonical 'rw'/'ro'.

        The daemon may report 'rw', 'ro', 'r', '' (rw default) or — on
        SELinux hosts — 'z'/'Z'/'rw,z'/'ro,z' depending on driver and
        labelling. Only the read-only bit matters for isolation comparison.
        """
        parts = [p for p in (mode or "").split(",") if p]
        if "ro" in parts or "r" in parts or any(p.endswith("ro") for p in parts):
            return "ro"
        return "rw"

    @staticmethod
    def _normalize_network_mode(network_mode):
        """Map a Docker HostConfig.NetworkMode to a canonical value.

        containerd-integrated Docker reports the default bridge network as
        'default' instead of 'bridge'; both are the same isolation level.
        """
        if network_mode == "default":
            return "bridge"
        return network_mode

    def _config_matches(self, container, network_mode, workspace_mode):
        """True if the container's actual network + /workspace mount match
        the desired isolation (mirrors docker_executor's integrity check).

        The /workspace mount is compared via the authoritative RW boolean:
        docker reports Mode strings like 'z' for named volume mounts on
        some hosts, so Mode cannot distinguish ro from rw. NetworkMode is
        normalized ('default' -> 'bridge' for containerd integration).
        """
        try:
            attrs = container.attrs
        except Exception:
            return False
        actual_network = (attrs.get("HostConfig") or {}).get("NetworkMode")
        workspace_rw = None
        for m in attrs.get("Mounts") or []:
            if m.get("Destination") == "/workspace":
                workspace_rw = m.get("RW")
                break
        if workspace_rw is None:
            # /workspace mount missing -> genuine drift; force recreation.
            return False
        expected_rw = workspace_mode == "rw"
        return (
            self._normalize_network_mode(actual_network) == network_mode
            and bool(workspace_rw) == expected_rw
        )

    def _remove_container(self, container):
        """Stop and remove a container; best-effort, NEVER raises."""
        try:
            container.stop(timeout=5)
        except Exception:
            pass
        try:
            container.remove()
        except Exception:
            pass


# ── Module-level workspace helpers ──────────────────────────────────────────
def cleanup_workspace(workspace_id, docker_client):
    """Stop + remove every container labelled with ``workspace_id``.

    Returns {"removed": n}. Never raises.
    """
    wid = str(workspace_id) if workspace_id is not None else "default"
    removed = 0
    try:
        containers = docker_client.containers.list(
            all=True, filters={"label": f"thoughtmachine.workspace_id={wid}"}
        )
    except Exception:
        containers = []
    for container in containers:
        try:
            container.stop(timeout=5)
        except Exception:
            pass
        try:
            # force=True: a stuck/still-running container must not leave
            # the counter at 0 (plain remove() raises on running containers).
            container.remove(force=True)
            removed += 1
        except Exception:
            pass
    _audit("CONTAINER_CLEANUP", f"workspace={wid} count={removed}")
    return {"removed": removed}
