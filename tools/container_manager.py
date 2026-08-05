"""Per-session Docker container manager for the ThoughtMachine agent.

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

``cleanup_session()`` is a belt-and-braces sweep that stops and removes all
containers carrying a session label (used when a session dies unexpectedly).

Security posture (identical to docker_executor.DockerExecutor)
--------------------------------------------------------------
- network disabled unless the session permissions allow a bridge network
  (decided by the shared ``_compute_container_config_from_permissions`` gate)
- all capabilities dropped, no-new-privileges, read-only root filesystem
- non-root user (1000:1000), tight memory + CPU quotas
- named volume ``tm-workspace-<workspace_id>`` populated ONE-SHOT from the
  host workspace via ``docker_executor.ensure_workspace_volume_populated``;
  the host bind mount is visible only to the short-lived init container

Label scheme
------------
Every container created by this module carries:
- ``thoughtmachine.container_name=<name>``
- ``thoughtmachine.session_id=<session_id>``
Used for label-based reuse lookups and ``cleanup_session()`` sweeps.

No-reload guarantee
-------------------
``docker_executor`` is imported once per process and cached; there is no
``importlib.reload`` anywhere in this module or in docker_code_runner's
execution path anymore.
"""

import hashlib
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime

try:
    import docker
    from docker.errors import APIError, ImageNotFound, NotFound
    DOCKER_AVAILABLE = True
except ImportError:
    DOCKER_AVAILABLE = False
    docker = None
    APIError = Exception
    ImageNotFound = Exception
    NotFound = Exception

from agent.logging import log
from thoughtmachine.audit_logger import audit_event

_audit = lambda event, data: audit_event(event, data)

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
    ):
        if docker is None:
            raise RuntimeError(
                "Docker Python SDK not installed. Install with 'pip install docker'."
            )
        self.workspace_path = os.path.abspath(workspace_path).rstrip("/")
        self.session_id = session_id
        self.session_permissions = session_permissions
        self.image = image
        self.mem_limit = mem_limit
        self.cpu_quota = cpu_quota

        # In-memory registry: container name -> container id (per-manager).
        self._containers = {}

        # Borrow the shared decision/populate helpers from docker_executor —
        # single source of truth, imported once (never reloaded).
        dex = _load_docker_executor()
        self._compute_config = dex._compute_container_config_from_permissions
        self._populate_volume = dex.ensure_workspace_volume_populated
        self._resolve_workspace_id = dex._resolve_workspace_id
        if workspace_id is None:
            workspace_id = self._resolve_workspace_id(self.workspace_path)
        self.workspace_id = str(workspace_id) if workspace_id is not None else None

        self.client = docker.from_env()

    # ── Public API ─────────────────────────────────────────────────────────
    def start(self, image=None, name=None):
        """Ensure a running container exists and return {"id", "name", "status"}.

        Reuse order: in-memory registry -> label lookup -> fresh create.

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
                    _audit("CONTAINER_REUSE_OK",
                           f"source=registry name={name} id={container.id} session={self.session_id}")
                    return {"id": container.id, "name": name, "status": "reused"}
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
                _audit("CONTAINER_REUSE_OK",
                       f"source=label name={name} id={container.id} session={self.session_id}")
                return {"id": container.id, "name": name, "status": "reused"}

        # 3) Fresh create
        volume_name = self._ensure_volume()
        if volume_name:
            self._populate_volume(
                self.client,
                image,
                self.workspace_path,
                volume_name,
                network_mode=network_mode,
                mem_limit=self.mem_limit,
                cpu_quota=self.cpu_quota,
            )

        tmpfs = {
            "/tmp": "rw,noexec,nosuid,size=64m",
            "/home/agent": "rw,exec,size=256M,uid=1000,gid=1000",
        }
        if os.path.isdir(os.path.join(self.workspace_path, ".git")):
            tmpfs["/workspace/.git"] = ""

        volumes = None
        mounts = None
        if volume_name:
            mounts = [
                docker.types.Mount(
                    target="/workspace",
                    source=volume_name,
                    type="volume",
                    read_only=(workspace_mode == "ro"),
                )
            ]
        else:
            volumes = {self.workspace_path: {"bind": "/workspace", "mode": workspace_mode}}

        labels = {
            "thoughtmachine.container_name": name,
            "thoughtmachine.session_id": str(self.session_id) if self.session_id is not None else "",
        }
        _audit("CONTAINER_CREATE",
               f"image={image} network={network_mode} name={name} session={self.session_id}")

        container = self.client.containers.run(
            image=image,
            name=name,
            volumes=volumes,
            mounts=mounts,
            tmpfs=tmpfs,
            network=network_mode,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            read_only=True,
            user="1000:1000",
            detach=True,
            tty=True,
            stdin_open=True,
            command=["tail", "-f", "/dev/null"],
            mem_limit=self.mem_limit,
            cpu_quota=self.cpu_quota,
            labels=labels,
        )
        try:
            container.reload()
        except Exception:
            pass
        self._containers[name] = container.id
        return {"id": container.id, "name": name, "status": "created"}

    def exec(self, container_id, command, timeout=30, workdir="/workspace", environment=None):
        """Run ``command`` in the container; returns {"stdout","stderr","exit_code"}."""
        container = self.client.containers.get(container_id)

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
        return {
            "stdout": _truncate_output(stdout),
            "stderr": _truncate_output(stderr),
            "exit_code": exit_code,
        }

    def stop(self, container_id):
        """Stop the container. Idempotent; NEVER raises."""
        try:
            container = self.client.containers.get(container_id)
        except NotFound:
            return {"status": "missing", "container_id": container_id,
                    "error": "container not found"}
        except Exception as e:
            return {"status": "error", "container_id": container_id, "error": str(e)}
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
            return {"status": "stopped", "container_id": container_id, "name": container.name}
        except Exception as e:
            return {"status": "error", "container_id": container_id, "error": str(e),
                    "name": getattr(container, "name", "")}

    def status(self, container_id):
        """Report container status; NEVER raises."""
        try:
            container = self.client.containers.get(container_id)
        except NotFound:
            return {"status": "missing", "container_id": container_id,
                    "error": "container not found"}
        except Exception as e:
            return {"status": "error", "container_id": container_id, "error": str(e)}
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

        return {
            "container_id": container_id,
            "name": container.name,
            "status": container.status,
            "uptime_seconds": uptime_seconds,
            "memory_usage_bytes": memory_usage_bytes,
        }

    def list_containers(self):
        """List containers carrying this session's label; NEVER raises.

        Queries the daemon for all containers (running or not) whose
        ``thoughtmachine.session_id`` label matches this manager's session id
        (the exact label source ``start()`` applies), so containers from other
        sessions — or unlabeled ones — never appear. Returns a list of dicts
        with EXACTLY: ``container_id``, ``name``, ``image``, ``status``,
        ``uptime_seconds``.
        """
        sid = str(self.session_id) if self.session_id is not None else ""
        try:
            containers = self.client.containers.list(
                all=True,
                filters={"label": f"thoughtmachine.session_id={sid}"},
            )
        except Exception:
            return []

        result = []
        for container in containers:
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
            })
        return result

    def build_image(self, dockerfile_path=None, tag=None):
        """Build a Docker image from the HOST workspace directory.

        Unlike ``docker_executor``'s temp-context build, the build context here
        is the workspace directory itself (``<workspace>/Dockerfile`` by
        default), so the Dockerfile can COPY the local tree directly. The build
        runs synchronously and its output is returned (not just a bool).

        Args:
            dockerfile_path: Path to the Dockerfile, absolute or relative to
                the workspace (default: ``Dockerfile``). Must stay inside the
                workspace.
            tag: Image tag; auto-generated from the workspace path (the same
                ``agent-executor-<hash>`` convention ``docker_executor`` uses)
                when omitted.

        Returns:
            Dict with EXACTLY ``image_tag`` and ``build_log`` (the build log,
            truncated to 100KB with a truncation notice).

        Raises:
            RuntimeError: If the Dockerfile is missing, escapes the workspace,
                or the build fails.
        """
        if not DOCKER_AVAILABLE or self.client is None:
            raise RuntimeError("Docker Python SDK not available")

        ws = self.workspace_path
        dockerfile = dockerfile_path or "Dockerfile"
        resolved = dockerfile if os.path.isabs(dockerfile) else os.path.join(ws, dockerfile)
        if not os.path.exists(resolved):
            raise RuntimeError(
                f"Dockerfile not found at {resolved}. "
                "Place a Dockerfile in the workspace or pass dockerfile_path."
            )
        rel = os.path.relpath(resolved, ws)
        if rel.startswith(".."):
            raise RuntimeError(f"dockerfile_path {dockerfile} escapes the workspace")

        dex = _load_docker_executor()
        if not tag:
            tag = dex._compute_image_tag(ws)

        try:
            _, log_lines = dex._run_image_build(self.client, ws, rel, tag)
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

    # ── Internals ──────────────────────────────────────────────────────────
    def _ensure_volume(self):
        """Return the named workspace volume (created on first use) or None."""
        if not self.workspace_id:
            return None
        volume_name = f"tm-workspace-{self.workspace_id}"
        try:
            self.client.volumes.get(volume_name)
            _audit("VOLUME_ENSURE", f"volume={volume_name} action=reuse")
        except NotFound:
            try:
                self.client.volumes.create(name=volume_name)
                _audit("VOLUME_ENSURE", f"volume={volume_name} action=create")
            except Exception as e:
                log("WARNING", "docker.volume",
                    f"Failed to create volume {volume_name}: {e}")
                return None
        except Exception as e:
            log("WARNING", "docker.volume",
                f"Failed to inspect volume {volume_name}: {e}")
            return None
        return volume_name

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
                        f"thoughtmachine.session_id={str(self.session_id) if self.session_id is not None else ''}",
                    ]
                },
            )
            return containers[0] if containers else None
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


# ── Module-level session helpers ────────────────────────────────────────────
def cleanup_session(session_id, docker_client):
    """Stop + remove every container labelled with ``session_id``.

    Returns {"removed": n}. Never raises.
    """
    # Normalise so a uuid.UUID object matches the str(uuid) label value
    # written by ContainerManager at create time.
    sid = str(session_id) if session_id is not None else ""
    removed = 0
    try:
        containers = docker_client.containers.list(
            all=True, filters={"label": f"thoughtmachine.session_id={sid}"}
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
    _audit("CONTAINER_CLEANUP", f"session={sid} count={removed}")
    return {"removed": removed}


def list_session_containers(session_id, docker_client):
    """List containers labelled with ``session_id``. Never raises."""
    sid = str(session_id) if session_id is not None else ""
    try:
        containers = docker_client.containers.list(
            all=True, filters={"label": f"thoughtmachine.session_id={sid}"}
        )
        return [
            {"id": c.id, "name": c.name, "status": c.status}
            for c in containers
        ]
    except Exception:
        return []
