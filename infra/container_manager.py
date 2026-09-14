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
  (decided by the shared ``security.security_gate.resolve_container_config`` gate)
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
additionally carry ``thoughtmachine.worker=<worker owner identity>`` on
FRESH creates. The identity format (``<session_id or 'unknown'>:<worker_name>``)
is decided by tools/workspace/worker.py and stamped VERBATIM. Workers stop/
remove their labelled containers at teardown by comparing the label value
EXACTLY to their own identity (stale/mismatched values are ignored) and
never touch resource containers (see tools/workspace/worker.py). Reuse
paths never re-label an existing container: the worker label is only
stamped at create.

Sticky notes (a container RECORD field)
---------------------------------------
A container's sticky note lives on its container RECORD -- the ``notes`` field
of ``<vault_root>/workspaces/<workspace_id>/containers/<record_id>.json`` --
keyed by the record id carried in the ``thoughtmachine.container_id`` Docker
label. The record store owns the note, so it survives container recreation and
is shared by every manager/session for the same workspace. Notes are NOT
written to Docker labels (labels are immutable after create on stock daemons -
there is no label-update API). A legacy per-workspace ``container_notes.json``
sidecar (``<vault_root>/workspaces/<workspace_id>/container_notes.json``) is
treated as READ-ONLY history: its entries are adopted onto records once (see
``_migrate_legacy_notes_once``) and never written again.

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
from collections import OrderedDict
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


from infra.container_env import merge_container_identity_env
from thoughtmachine.container_record import (
    ContainerRecordError,
    LIFECYCLE_EPHEMERAL,
    LIFECYCLE_PERSISTENT,
    LIFECYCLE_RESOURCE,
    RECORD_LABEL_KEY,
    RESOURCE_LABEL,
    RESOURCE_NAME_PREFIX,
    RecordLocked,
    RecordNotFound,
    UnknownLifecycleClass,
    delete_record,
    docker_restart_policy,
    find_by_docker_label,
    is_resource_like,
    list_records,
    load_record,
    normalise_restart_policy,
    policy_for,
    update_record,
)
from thoughtmachine.container_record import drift
from thoughtmachine.container_record import storage
from thoughtmachine.container_record.hook import record_creation

# Admission control (phase 2): the legacy (registry-inactive) fresh create is a
# terminal container-create site, so it consults the pure admission gate before
# touching the daemon (a Deny returns an error dict; a Transform narrows the
# network_mode).  When the registry is active this code is unreachable -- the
# registry already applies admission on its own create path.
from security.admission_gate import (
    AdmissionRequest,
    ClientProbes,
    ContainerSpec,
    Deny,
    REASON_UNKNOWN_LIFECYCLE_CLASS,
    Transform,
    admit,
)

# ── Output truncation (mirrors DockerCodeRunner._truncate_output) ──────────
from agent.config.defaults import (
    CONTAINER_NAME_LABEL,
    CONTAINER_TYPE_FREE_USE,
    CONTAINER_TYPE_LABEL,
    CONTAINER_TYPE_RESOURCE,
    DEFAULT_IMAGE,
    DEFAULT_MAX_CONTAINERS,
    EXEC_OUTPUT_LIMIT_BYTES,
)
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


# ── Exec-path drift admission ─────────────────────────────────────────────────────────
# ``exec()`` refuses to run a command when the LIVE container's network/workspace
# isolation is MORE PERMISSIVE than the session policy desires (a command must
# never run under weaker isolation than the gate asked for).  A container that
# differs but is NOT more permissive — or whose isolation cannot be read — is
# WARNING-logged and allowed through, preserving today's behaviour.
#
# The drift EVENT + WARNING + audit fire ONCE per distinct signature.  Because a
# fresh ``ContainerManager`` is built for every tool call, the "already reported"
# memo lives at MODULE scope so deduplication actually holds across calls.  The
# DECISION, by contrast, is applied on every call (a drifted container keeps
# being denied).
_EXEC_DRIFT_MEMO_MAX = 256
_EXEC_DRIFT_SEEN = OrderedDict()  # (container_id, signature) -> True, oldest evicted
_EXEC_DRIFT_LOCK = threading.Lock()
_EXEC_DRIFT_EVENT = "drift.exec_on_drifted_container"
_EXEC_DRIFT_ACTOR = "infra.container_manager.exec"
_EXEC_DRIFT_AUDIT = "CONTAINER_EXEC_DRIFT"
_EXEC_DRIFT_EXIT_CODE = 126  # distinct from -2 (timeout) and -1 (generic error)

# Start-path drift admission (mirrors the exec-path memo above): ``start()`` no
# longer MUTATES a drifted container (no remove/recreate).  A container whose
# live isolation differs from the resolved policy but is NOT more permissive is
# REUSED with the drift attached to the response; a container that IS more
# permissive is REFUSED (an error response carrying the drift detail) WITHOUT
# touching it.  Same module-scope memo discipline as exec: the drift EVENT +
# WARNING + audit fire ONCE per distinct signature, while the DECISION is
# applied on every call.
_START_DRIFT_SEEN = OrderedDict()  # (container_id, signature) -> True, oldest evicted
_START_DRIFT_LOCK = threading.Lock()
_START_DRIFT_EVENT = "drift.start_on_drifted_container"
_START_DRIFT_ACTOR = "infra.container_manager.start"
_START_DRIFT_AUDIT = "CONTAINER_START_DRIFT"

# Restart-policy drift (third admission axis on the start path).  A reused
# container whose live Docker restart policy is MORE permissive than the
# resolved lifecycle policy is REFUSED; a less-permissive difference is a
# WARNING-only drift.  Its own event/audit codes so the signal is greppable.
_START_DRIFT_RESTART_EVENT = "drift.restart_policy_mismatch"
_START_DRIFT_RESTART_ACTOR = "infra.container_manager.start"
_START_DRIFT_RESTART_AUDIT = "CONTAINER_RESTART_DRIFT"

# Sticky-note migration/warning memos (module scope so dedup holds across the
# fresh ContainerManager instances built for every tool call). ``_NOTES_WARNED``
# dedupes the once-per-condition WARNINGs (a legacy sidecar note served, a write
# refused for lack of a record); ``_NOTES_MIGRATED`` records the workspaces whose
# legacy container_notes.json has already been adopted onto records (so the
# one-shot migration is idempotent).
_NOTES_MEMO_LOCK = threading.Lock()
_NOTES_WARNED = set()
_NOTES_MIGRATED = set()

# Container-name index memos (module scope, same dedup discipline as the notes
# memos above). ``_NAME_INDEX`` maps ``(workspace_id, name) -> record id`` and is
# built from ``list_records`` ONCE per workspace (``_NAME_INDEX_BUILT``) so it is
# never a boot cost; ``_NAME_INDEX_COLLISIONS`` records ``(workspace_id, name)``
# keys claimed by two-or-more records (ambiguous identity -> start refuses);
# ``_NAME_MIGRATED`` records the workspaces whose v1/v2 records have already
# been name-backfilled (one-shot, idempotent).
_NAME_INDEX_LOCK = threading.Lock()
_NAME_INDEX = {}
_NAME_INDEX_COLLISIONS = set()
_NAME_INDEX_BUILT = set()
_NAME_MIGRATED = set()


def _name_index_forget(record_id):
    """Drop every ``(workspace, name)`` index entry pointing at *record_id*.

    Module-scope so callers without a ``ContainerManager`` instance (the
    orphan-record sweeper) can keep the in-memory name index consistent after
    deleting a record.  Never raises.
    """
    if not record_id:
        return
    with _NAME_INDEX_LOCK:
        for key in [k for k, v in _NAME_INDEX.items() if v == record_id]:
            _NAME_INDEX.pop(key, None)

# Isolation ranks: higher == more permissive.  Network "default" is normalized
# to "bridge" upstream (see ContainerManager._normalize_network_mode); any other
# PRESENT network mode (host / container:<id> / ...) is treated as the most
# permissive.  For the workspace bind: ro(0) < rw(1) < absent(2) — an absent
# /workspace bind is MORE permissive because writes then land on the container's
# own layer instead of the (read-only) host bind.
_EXEC_NETWORK_RANK = {"none": 0, "bridge": 1}
_EXEC_WORKSPACE_RANK = {"ro": 0, "rw": 1, "absent": 2}

# Restart-policy ranks: higher == more permissive (keeps the container alive
# across failures/daemon restarts more aggressively).  "no"(0) is the most
# restrictive; anything unknown is treated as the most permissive (3).
_RESTART_POLICY_RANK = {"no": 0, "on-failure": 1, "unless-stopped": 2, "always": 3}


def _exec_network_rank(network_mode):
    """Isolation rank for a network mode (None -> None, unknown -> 2)."""
    if network_mode is None:
        return None
    normalized = ContainerManager._normalize_network_mode(network_mode)
    return _EXEC_NETWORK_RANK.get(normalized, 2)


def _exec_workspace_rank(workspace_mode):
    """Isolation rank for a workspace mode (None -> None, unknown -> 2)."""
    if workspace_mode is None:
        return None
    return _EXEC_WORKSPACE_RANK.get(workspace_mode, 2)


def _exec_live_isolation(container):
    """Read (live_network_mode, live_workspace_mode) from a container's attrs.

    Returns ``(None, None)`` when attrs is unreadable or not a dict.  A key that
    is STRUCTURALLY ABSENT (no ``HostConfig`` dict, no ``Mounts`` list) yields
    ``None`` for that axis — "cannot determine", i.e. no observable drift, so
    pre-existing callers whose fakes omit these keys are unaffected.  A present
    ``Mounts`` list without a ``/workspace`` destination yields ``"absent"``.
    """
    try:
        attrs = getattr(container, "attrs", None)
    except Exception:
        return None, None
    if not isinstance(attrs, dict):
        return None, None

    live_net = None
    host_config = attrs.get("HostConfig")
    if isinstance(host_config, dict):
        raw_net = host_config.get("NetworkMode")
        if raw_net is not None:
            live_net = ContainerManager._normalize_network_mode(raw_net)

    live_ws = None
    mounts = attrs.get("Mounts")
    if isinstance(mounts, list):
        live_ws = "absent"
        for mount in mounts:
            if isinstance(mount, dict) and mount.get("Destination") == "/workspace":
                live_ws = "rw" if mount.get("RW") else "ro"
                break
    return live_net, live_ws


def _exec_drift_decision(live_net, live_ws, want_net, want_ws):
    """Classify live isolation vs desired policy; returns (decision, reason).

    * ``("run", None)``  — nothing determinable.
    * ``("deny", "<axis>_more_permissive")`` — live is MORE permissive on at
      least one axis (network checked first, then workspace).
    * ``("warn", "config_differs_not_more_permissive")`` — diff the other way.

    A ``None``/unknown WANT on an axis means "no opinion" on that axis: it can
    never prove live is MORE permissive, so it never forces a deny.  The
    comparison is therefore total — it never relies on ``int > None`` raising.
    """
    net_known = live_net is not None
    ws_known = live_ws is not None
    if not net_known and not ws_known:
        return "run", None

    # A ``None``/unknown WANT rank (or an unknown WANT mode) is "no opinion":
    # that axis cannot make live MORE permissive, so it never forces a deny.
    want_net_rank = _exec_network_rank(want_net)
    want_ws_rank = _exec_workspace_rank(want_ws)
    net_more = (
        net_known and want_net_rank is not None
        and _exec_network_rank(live_net) > want_net_rank
    )
    ws_more = (
        ws_known and want_ws_rank is not None
        and _exec_workspace_rank(live_ws) > want_ws_rank
    )
    if net_more or ws_more:
        reason = "network_more_permissive" if net_more else "workspace_more_permissive"
        return "deny", reason
    return "warn", "config_differs_not_more_permissive"


def _restart_policy_rank(restart_policy):
    """Permissiveness rank for a restart policy (None -> None, unknown -> 3)."""
    if restart_policy is None:
        return None
    return _RESTART_POLICY_RANK.get(str(restart_policy).strip(), 3)


def _live_restart_policy(container):
    """Read the container's live Docker restart policy name from its attrs.

    Returns ``None`` when it cannot be determined: unreadable/absent attrs, no
    ``HostConfig`` dict, or a STRUCTURALLY ABSENT ``RestartPolicy`` key.  The
    absent-key case is deliberate: fakes that predate this axis omit the key,
    which must read as "no observable drift" rather than a mismatch.
    """
    try:
        attrs = getattr(container, "attrs", None)
    except Exception:
        return None
    if not isinstance(attrs, dict):
        return None
    host_config = attrs.get("HostConfig")
    if not isinstance(host_config, dict):
        return None
    if "RestartPolicy" not in host_config:
        return None
    policy = host_config.get("RestartPolicy")
    if policy is None:
        return "no"
    if isinstance(policy, dict):
        return normalise_restart_policy(policy.get("Name"))
    return normalise_restart_policy(policy)


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


def _load_capabilities(workspace_id):
    """Best-effort load of a workspace's capabilities for admission.

    Runtime import so a test that monkeypatches
    ``security.security_gate.get_workspace_capabilities`` is honoured.  Any
    failure returns ``None``; admission then fails closed on the
    ``capabilities_required`` code (mirrors the fail-closed network resolver).
    """
    try:
        from security.security_gate import get_workspace_capabilities

        return get_workspace_capabilities(workspace_id)
    except Exception:  # noqa: BLE001 - admission fails closed on None
        return None


class ContainerManager:
    """Owns the Docker containers for one session (start -> exec -> stop)."""

    def __init__(
        self,
        workspace_path,
        session_id=None,
        workspace_id=None,
        session_permissions=None,
        image=DEFAULT_IMAGE,
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
        self.max_containers = self.workspace_config.get(
            "max_containers", DEFAULT_MAX_CONTAINERS
        )

        self.client = docker.from_env()

    def _load_workspace_config(self):
        """Load per-workspace config; returns a dict and NEVER raises.

        Reads ``<vault_root>/workspaces/<workspace_id>/config.json``.
        Missing file -> defaults in memory (nothing written to disk — construction
        performs no I/O). Corrupt file -> defaults in memory (file untouched).
        """
        defaults = {"max_containers": DEFAULT_MAX_CONTAINERS, "disk_quota_mb": 4096}
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
        raised.
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

    def _counts_toward_limit(self, entry) -> bool:
        """Whether a container entry occupies a limit slot.

        Terminal states (exited/dead/removing) free a slot so a stuck or
        crashed container never blocks a fresh create. Unknown/new statuses
        count toward the limit (fail-safe: default to occupying a slot).
        """
        status = str((entry or {}).get("status", "")).lower()
        return status not in ("exited", "dead", "removing")

    def _active_containers(self, entries):
        """Filter container entries down to those that occupy a limit slot."""
        return [e for e in (entries or []) if self._counts_toward_limit(e)]

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

    def _warn_note_once(self, key, message):
        """Log *message* at WARNING at most once per *key* (module-scoped).

        Because a fresh ``ContainerManager`` is built for every tool call, the
        "already warned" memo lives at MODULE scope (``_NOTES_WARNED``) so the
        dedup actually holds across calls.
        """
        with _NOTES_MEMO_LOCK:
            if key in _NOTES_WARNED:
                return
            _NOTES_WARNED.add(key)
        log("WARNING", "docker.container_manager", message)

    def _record_id_from_labels(self, labels):
        """Return the record id carried by *labels*, or None.

        The record id is the value of the ``RECORD_LABEL_KEY``
        (``thoughtmachine.container_id``) label; an absent or empty value yields
        None. Never raises.
        """
        try:
            value = (labels or {}).get(RECORD_LABEL_KEY)
        except Exception:
            return None
        if not value:
            return None
        return str(value)

    def _record_id_for(self, container):
        """Return the record id for a docker *container* object, or None.

        Reads the container's labels (``container.labels`` when available, else
        ``container.attrs["Config"]["Labels"]``). Never raises: any lookup
        failure yields None.
        """
        if container is None:
            return None
        labels = getattr(container, "labels", None)
        if not labels:
            try:
                attrs = getattr(container, "attrs", None) or {}
                labels = (attrs.get("Config") or {}).get("Labels")
            except Exception:
                labels = None
        return self._record_id_from_labels(labels)

    def _record_id_for_name(self, name):
        """Resolve a record id for a container *name* via the docker client.

        Returns None when the name is falsy, the lookup fails, or the container
        carries no record label. Never raises.
        """
        if not name:
            return None
        try:
            container = self.client.containers.get(name)
        except Exception:
            return None
        return self._record_id_for(container)

    def _read_note(self, container_or_name):
        """Return the sticky note for a container; returns '' and NEVER raises.

        A docker container OBJECT is preferred (no extra daemon round-trip):
        the note is read from the container RECORD, located via the
        ``thoughtmachine.container_id`` label. When no record id can be
        resolved, a READ-ONLY look at the legacy ``container_notes.json``
        sidecar is used as a fallback (a WARNING is logged once).
        """
        if isinstance(container_or_name, str):
            name = container_or_name
            record_id = self._record_id_for_name(name)
        else:
            container = container_or_name
            name = getattr(container, "name", None)
            record_id = self._record_id_for(container)
        if record_id:
            try:
                # Records live in the DEFAULT vault (SSOT); never thread a
                # manager vault_root into the record store.
                record = load_record(self.workspace_id, record_id)
            except Exception:
                return ""
            if record is None:
                return ""
            return str(getattr(record, "notes", "") or "")
        # No record: read-only legacy sidecar fallback (never written).
        if name:
            try:
                entry = (self._load_container_notes() or {}).get(name) or {}
                note = str(entry.get("note") or "")
            except Exception:
                note = ""
            if note:
                self._warn_note_once(
                    ("notes.legacy_sidecar_read", self.workspace_id, name),
                    f"Container note for {name!r} served from the legacy "
                    f"container_notes.json sidecar (no container record); "
                    f"recreate the container to adopt the note onto its record.")
                return note
        return ""

    def _write_note(self, record_id, note):
        """Persist *note* as the container RECORD's ``notes`` field; NEVER raises.

        The note is record-owned, so it survives container recreation. FAIL
        CLOSED: with no record id the write is REFUSED (a WARNING is logged) and
        NOTHING is persisted -- the legacy sidecar is never written. A real
        error from the record store is logged, not raised (a ``RuntimeError``
        such as a safety barrier is NOT swallowed).
        """
        if not record_id:
            self._warn_note_once(
                ("notes.no_record_write_refused", self.workspace_id),
                f"Refusing to persist a container note for workspace "
                f"{self.workspace_id!r}: no container record id (the container "
                f"carries no {RECORD_LABEL_KEY} label).")
            return
        try:
            # Records live in the DEFAULT vault (SSOT); never thread a
            # manager vault_root into the record store.
            update_record(self.workspace_id, record_id, notes=note)
        except (ContainerRecordError, RecordLocked, RecordNotFound,
                OSError, ValueError) as e:
            log("WARNING", "docker.container_manager",
                f"Failed to write note for record {record_id} "
                f"(workspace {self.workspace_id}): {e}")

    def _migrate_legacy_notes_once(self):
        """Adopt legacy ``container_notes.json`` notes onto records (once).

        For every sidecar entry whose container resolves to a record with an
        EMPTY ``notes`` field, copy the sidecar note onto the record; a record
        that already has a note WINS (never overwritten). Adopted/rederived
        names are dropped from the sidecar, which is rewritten atomically (or
        removed once empty). Idempotent: a second run is a fixed point, and a
        workspace with no sidecar returns immediately.
        """
        if self.workspace_id in _NOTES_MIGRATED:
            return
        with _NOTES_MEMO_LOCK:
            if self.workspace_id in _NOTES_MIGRATED:
                return
            _NOTES_MIGRATED.add(self.workspace_id)
        try:
            legacy = self._load_container_notes()
        except Exception:
            legacy = {}
        if not legacy:
            return
        remaining = dict(legacy)
        changed = False
        for name, entry in legacy.items():
            note = str((entry or {}).get("note") or "")
            record_id = self._record_id_for_name(name)
            if not record_id:
                continue
            try:
                # Records live in the DEFAULT vault (SSOT); never thread a
                # manager vault_root into the record store.
                record = load_record(self.workspace_id, record_id)
            except Exception:
                continue
            if record is None:
                continue
            existing = str(getattr(record, "notes", "") or "")
            if existing == "" and note != "":
                try:
                    # Records live in the DEFAULT vault (SSOT); never thread a
                    # manager vault_root into the record store.
                    update_record(self.workspace_id, record_id, notes=note)
                except Exception as e:
                    log("WARNING", "docker.container_manager",
                        f"Failed to adopt legacy note for record {record_id}: {e}")
                    continue
            # The record now owns the note (adopted, or already present).
            remaining.pop(name, None)
            changed = True
        if changed:
            self._rewrite_legacy_notes(remaining)

    def _rewrite_legacy_notes(self, remaining):
        """Rewrite (or remove) the legacy sidecar after migration; NEVER raises."""
        notes_path = self._notes_path()
        try:
            if remaining:
                notes_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = notes_path.with_suffix(".json.tmp")
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(remaining, f, indent=2)
                os.replace(tmp_path, notes_path)
            else:
                try:
                    os.unlink(notes_path)
                except FileNotFoundError:
                    pass
        except OSError as e:
            log("WARNING", "docker.container_manager",
                f"Failed to rewrite legacy container notes {notes_path}: {e}")

    # ── Container-name index (record-first identity, schema v3) ─────────
    def _ensure_name_index(self):
        """Build the workspace-scoped ``(name) -> record id`` index ONCE.

        Mirrors the notes memo discipline (module-scoped so dedup holds across
        the fresh ``ContainerManager`` built per tool call).  Building from
        ``list_records`` keeps it off the boot path (first start/set_note in a
        workspace triggers it).  Never raises.
        """
        if self.workspace_id in _NAME_INDEX_BUILT:
            return
        with _NAME_INDEX_LOCK:
            if self.workspace_id in _NAME_INDEX_BUILT:
                return
            _NAME_INDEX_BUILT.add(self.workspace_id)
        try:
            records = list_records(self.workspace_id)
        except Exception:
            records = []
        for record in records or []:
            self._name_index_add(record)

    def _name_index_add(self, record):
        """Record *record*'s name in the (workspace, name) index.

        The record-first create paths hold a record id (not a record object),
        so they call :meth:`_name_index_register` directly; this thin adapter
        keeps the ``list_records``/migration callers unchanged.
        """
        self._name_index_register(
            getattr(record, "name", ""), getattr(record, "id", None))

    def _name_index_register(self, name, record_id):
        """Index ``(workspace, name) -> record_id`` (in-memory).

        A second record id claiming the same (workspace, name) marks the key as
        COLLIDED (ambiguous identity) rather than silently overwriting.  A
        falsy name or record id is ignored (an unbound identity is never
        indexed).
        """
        name = str(name or "")
        if not name or not record_id:
            return
        key = (self.workspace_id, name)
        with _NAME_INDEX_LOCK:
            existing = _NAME_INDEX.get(key)
            if existing is None:
                _NAME_INDEX[key] = record_id
            elif existing != record_id:
                _NAME_INDEX_COLLISIONS.add(key)

    def _name_index_forget(self, record_id):
        """Drop every index entry pointing at *record_id* (module helper)."""
        _name_index_forget(record_id)

    def _record_for_name(self, name):
        """Return the record id indexed for *name* in this workspace, or None.

        Returns None when the name is falsy, unindexed, or AMBIGUOUS (a
        collision recorded for the same (workspace, name)).
        """
        if not name:
            return None
        key = (self.workspace_id, name)
        with _NAME_INDEX_LOCK:
            if key in _NAME_INDEX_COLLISIONS:
                return None
            return _NAME_INDEX.get(key)

    def _name_collision(self, name):
        """Return True when *name* is ambiguous (>=2 records) in this ws."""
        if not name:
            return False
        with _NAME_INDEX_LOCK:
            return (self.workspace_id, name) in _NAME_INDEX_COLLISIONS

    def _migrate_records_v3_once(self):
        """Backfill the ``name`` identity field onto v1/v2 records (once).

        Sources each record's name from the LIVE container's
        ``thoughtmachine.container_name`` label (record WINS: a record that
        already carries a name is never overwritten). A record whose container
        cannot be located / carries no name label is LEFT UNSET (no synthesis,
        per the migration constraint). One-shot per workspace; idempotent.
        """
        if self.workspace_id in _NAME_MIGRATED:
            return
        with _NAME_INDEX_LOCK:
            if self.workspace_id in _NAME_MIGRATED:
                return
            _NAME_MIGRATED.add(self.workspace_id)
        try:
            records = list_records(self.workspace_id)
        except Exception:
            records = []
        labels_by_docker_id = {}
        try:
            for entry in self.list_containers():
                cid = str(entry.get("container_id") or "")
                cname = (entry.get("labels") or {}).get(CONTAINER_NAME_LABEL)
                if cid and cname:
                    labels_by_docker_id[cid] = str(cname)
        except Exception:
            labels_by_docker_id = {}
        for record in records or []:
            if str(getattr(record, "name", "") or ""):
                self._name_index_add(record)  # record wins; just (re)index it
                continue
            cname = labels_by_docker_id.get(
                str(getattr(record, "docker_id", "") or ""))
            if not cname:
                continue  # leave UNSET (no synthesis)
            try:
                updated = update_record(self.workspace_id, record.id, name=cname)
            except Exception:
                continue
            self._name_index_add(updated)

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

    def start(self, image=None, name=None, note=None, worker_name=None, *,
              lifecycle_class: str = LIFECYCLE_PERSISTENT):
        """Ensure a running container exists and return {"id", "name", "status", "note"}.

        Reuse order: in-memory registry -> label lookup -> fresh create.

        ``lifecycle_class`` records the lifecycle of a FRESH-created container's
        record; the default (``persistent``) is non-destructive, so only callers
        that KNOW the container is ephemeral pass ``ephemeral`` explicitly.

        ``worker_name`` (optional) stamps the container with the
        ``thoughtmachine.worker`` ownership label - but only on a FRESH
        create: reuse paths return before the label dict is built, so an
        existing container keeps whatever labels it was created with. Workers
        use this label to reclaim their containers at teardown (see
        tools/workspace/worker.py).

        ``note`` is an optional sticky note: it is persisted as the ``notes``
        field of the container's RECORD (located via the
        ``thoughtmachine.container_id`` Docker label) - never to Docker labels -
        and returned in the response. On reuse, a new note overwrites the
        record's note.

        Desired isolation (network_mode, workspace_mode) is computed ONCE from
        the session permissions BEFORE any reuse path.  A drifted container is
        NEVER mutated here: when its live isolation differs from the resolved
        policy but is not MORE permissive it is REUSED with a ``drift`` detail
        attached to the response; when it is MORE permissive than the resolved
        policy the container is REFUSED (``{"error": ..., "drift": ...}``) so
        the caller can act, instead of being silently reused or removed.
        """
        # Adopt any legacy container_notes.json entries onto records before we
        # read/write notes below (idempotent; a no-op once per workspace).
        self._migrate_legacy_notes_once()

        # Capture the caller-supplied image BEFORE defaulting, so image-reuse
        # honesty can tell an explicit request from the manager default.
        explicit_image = image
        image = image or self.image
        if name is None:
            ws_hash = hashlib.sha256(self.workspace_path.encode()).hexdigest()[:12]
            name = f"agent-exec-{ws_hash}-{_safe_session_tag(self.session_id)}"

        # Hidden resource containers (tm-res-*) are never addressable through
        # the generic container manager — they are owned by the resource
        # container manager and must stay invisible here.
        if str(name).startswith(RESOURCE_NAME_PREFIX):
            return {"error": "Resource container access denied"}

        # Phase 3: workspace-scoped reuse + container-limit enforcement BEFORE
        # any create. An existing container with the same name is reused as-is
        # (never counted against the limit); otherwise the active (non-terminal)
        # container count for THIS workspace decides whether a new one may be
        # created.
        # Desired isolation (all paths). Computed here so the workspace-label
        # reuse path can honour a newly-granted network/workspace mode instead
        # of silently reusing a drifted container.
        want_network, want_workspace = self._compute_config(
            self.workspace_path,
            self.workspace_id,
            self.session_permissions,
            lifecycle_class,
        )

        # ── Record-first identity ladder (schema v3) ─────────────────────────
        # The container RECORD is the source of truth for identity: a name
        # resolves to a record via the workspace-scoped index, and the record's
        # ``docker_id`` names the live container.  The in-memory
        # ``self._containers`` dict is a read-through CACHE only.  The old
        # ``_find_by_labels`` lookup stays available but is deliberately NOT on
        # the identity path here.
        self._ensure_name_index()
        self._migrate_records_v3_once()

        # (d) Ambiguous name: >=2 records share (workspace, name) -> REFUSE.
        if self._name_collision(name):
            self._warn_note_once(
                ("name.collision", self.workspace_id, name),
                f"Refusing start({name!r}) in workspace {self.workspace_id!r}: "
                f"multiple container records share this name (ambiguous "
                f"identity).")
            _audit("CONTAINER_NAME_COLLISION",
                   f"name={name} workspace_id={self.workspace_id}")
            return {"error": (f"Container name {name!r} is ambiguous in this "
                              f"workspace: multiple records share it."),
                    "code": "container_name_collision"}

        record_id = self._record_for_name(name)
        if record_id:
            record = None
            try:
                record = load_record(self.workspace_id, record_id)
            except Exception:
                record = None
            docker_id = str(getattr(record, "docker_id", "") or "") if record else ""
            if not docker_id:
                # (b) Record exists but names no container yet -> REFUSE.
                self._warn_note_once(
                    ("name.record_without_container", self.workspace_id, name),
                    f"Refusing start({name!r}): its record {record_id} carries no "
                    f"docker_id (no bound container).")
                _audit("CONTAINER_START_NO_CONTAINER_RECORD",
                       f"name={name} record_id={record_id} "
                       f"workspace_id={self.workspace_id}")
                return {"error": (f"Container {name!r} has a record with no bound "
                                  f"container; refusing to start."),
                        "code": "container_record_unbound"}
            # (a) Record + docker_id: reuse via the record's docker id.
            container = self._reuse_container(docker_id)
            if container is None:
                self._emit_stale_docker_id_drift_once(
                    name, record_id, docker_id, "record")
                return {"error": (f"Container {name!r} record {record_id} names "
                                  f"docker_id {docker_id!r} but no such container "
                                  f"exists."),
                        "code": "container_record_container_missing"}
            self._containers[name] = container.id  # warm the read-through cache
            if (explicit_image is not None
                    and not self._image_matches(container, explicit_image)):
                actual = self._image_ref(container)
                msg = (f"Container `{name}` exists with image {actual}; cannot reuse "
                       f"with image {explicit_image}. Remove it first or use a different name.")
                log("WARNING", "docker.container_manager", msg)
                _audit("CONTAINER_REUSE_IMAGE_MISMATCH",
                       f"name={name} id={container.id} actual={actual} "
                       f"requested={explicit_image} source=record")
                return {"error": msg}
            _action, _payload = self._start_drift_decision(
                container, want_network, want_workspace, "record",
                lifecycle_class=lifecycle_class,
            )
            if _action == "deny":
                return _payload
            if note is not None:
                self._write_note(self._record_id_for(container), note)
            note_value = (note if note is not None else self._read_note(container))
            _audit("CONTAINER_REUSE_OK",
                   f"source=record name={name} id={container.id} session={self.session_id}")
            log_container_event("started", container_id=container.id,
                                session_id=self.session_id or "",
                                data={"image": self._image_ref(container),
                                      "name": name, "status": "reused"})
            _reuse_resp = {"id": container.id, "name": name, "status": "reused",
                           "note": note_value}
            if _payload is not None:
                _reuse_resp["drift"] = _payload
            return _reuse_resp

        # (c) Cached name with NO record -> drift: REFUSE (no-container-record).
        if name in self._containers:
            container_id = self._containers.get(name)
            self._warn_note_once(
                ("name.cache_without_record", self.workspace_id, name),
                f"Refusing start({name!r}): cached container {container_id!r} has "
                f"no container record (drift: cache/record divergence).")
            _audit("CONTAINER_START_NO_CONTAINER_RECORD",
                   f"name={name} container_id={container_id} "
                   f"workspace_id={self.workspace_id} source=cache")
            return {"error": (f"Container {name!r} has no container record; "
                              f"refusing to start (drift)."),
                    "code": "container_no_record",
                    "drift": {"reason": "cached container without a record"}}

        # No record for *name*: fall through to the legacy reuse/create paths
        # below (a fresh create records the name via record_creation).
        containers = self.list_containers()
        for entry in containers:
            if entry["name"] == name:
                note_value = note if note is not None else entry.get("note", "")
                container = None
                try:
                    container = self.client.containers.get(entry["container_id"])
                except Exception:
                    container = None
                # Image honesty: an explicitly-requested image that differs from
                # the existing container's image cannot be honoured by reuse (the
                # image is fixed at create). Surface it instead of silently
                # returning a container running the wrong image. Do NOT remove
                # the container on mismatch.
                if (container is not None and explicit_image is not None
                        and not self._image_matches(container, explicit_image)):
                    actual = self._image_ref(container)
                    msg = (f"Container `{name}` exists with image {actual}; cannot reuse "
                           f"with image {explicit_image}. Remove it first or use a different name.")
                    log("WARNING", "docker.container_manager", msg)
                    _audit("CONTAINER_REUSE_IMAGE_MISMATCH",
                           f"name={name} id={container.id} actual={actual} "
                           f"requested={explicit_image} source=workspace-label")
                    return {"error": msg}
                # A drifted container (network or /workspace mount no longer
                # matches the session permissions) is never silently reused and
                # never mutated here: a not-more-permissive mismatch is reused
                # with a drift detail attached; a more-permissive one is refused
                # (see _start_drift_decision; mirrors the registry/label checks
                # below).
                _start_drift = None
                if container is not None:
                    _action, _payload = self._start_drift_decision(
                        container, want_network, want_workspace, "workspace-label",
                        lifecycle_class=lifecycle_class,
                    )
                    if _action == "deny":
                        return _payload
                    if _action == "reuse":
                        _start_drift = _payload
                    try:
                        self._ensure_running(container)
                    except Exception:
                        pass
                if note is not None:
                    _rid = (self._record_id_for(container)
                            if container is not None
                            else self._record_id_for_name(name))
                    self._write_note(_rid, note)
                _audit("CONTAINER_REUSE_OK",
                       f"source=workspace-label name={name} id={entry['container_id']} "
                       f"session={self.session_id}")
                log_container_event("started", container_id=entry["container_id"],
                                    session_id=self.session_id or "",
                                    data={"image": self._image_ref(container),
                                          "name": name, "status": "reused"})
                _reuse_resp = {**entry, "status": "reused", "id": entry["container_id"],
                               "note": note_value}
                if _start_drift is not None:
                    _reuse_resp["drift"] = _start_drift
                return _reuse_resp
        limit = self._get_max_containers()
        # When the registry is active it owns the per-session limit; the
        # legacy workspace-scoped check is skipped so the registry is the
        # single source of truth for container counts.
        active_containers = self._active_containers(containers)
        if len(active_containers) >= limit and not is_registry_active(getattr(self, "_session_config", None)):
            log("WARNING", "docker.container_manager",
                f"Workspace container limit reached: active={len(active_containers)} "
                f"exited={len(containers) - len(active_containers)} limit={limit} "
                f"workspace_id={self.workspace_id}")
            return {"error": f"Workspace container limit ({limit}) reached "
                             f"({len(active_containers)} active container(s)). "
                             f"Stop or remove a running container to free a slot."}

        # ── Desired isolation from session permissions (all paths) ─────────
        network_mode, workspace_mode = self._compute_config(
            self.workspace_path,
            self.workspace_id,
            self.session_permissions,
            lifecycle_class,
        )
        _audit("CONTAINER_CONFIG",
               f"name={name} network={network_mode} workspace={workspace_mode} "
               f"session={self.session_permissions} workspace_id={self.workspace_id}")
        # Explicit-grant guard: never silently fail-closed on a session that
        # explicitly granted write access — surface it loudly instead.
        sp = self.session_permissions or {}
        if sp.get("network") in ("write", "outbound") and network_mode != "bridge":
            log("WARNING", "docker.container_manager",
                f"Session grants network={sp.get('network')} but gate returned "
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

        # 2) Label lookup (survives manager restarts)
        container = self._find_by_labels(name)
        if container is not None:
            # Image honesty (see workspace-label path).
            if (explicit_image is not None
                    and not self._image_matches(container, explicit_image)):
                actual = self._image_ref(container)
                msg = (f"Container `{name}` exists with image {actual}; cannot reuse "
                       f"with image {explicit_image}. Remove it first or use a different name.")
                log("WARNING", "docker.container_manager", msg)
                _audit("CONTAINER_REUSE_IMAGE_MISMATCH",
                       f"name={name} id={container.id} actual={actual} "
                       f"requested={explicit_image} source=label")
                return {"error": msg}
            _action, _payload = self._start_drift_decision(
                container, network_mode, workspace_mode, "label",
                lifecycle_class=lifecycle_class,
            )
            if _action == "deny":
                return _payload
            self._ensure_running(container)
            self._containers[name] = container.id
            if note is not None:
                self._write_note(self._record_id_for(container), note)
            note_value = (note if note is not None
                          else self._read_note(container))
            _audit("CONTAINER_REUSE_OK",
                   f"source=label name={name} id={container.id} session={self.session_id}")
            log_container_event("started", container_id=container.id,
                                session_id=self.session_id or "",
                                data={"image": self._image_ref(container),
                                      "name": name, "status": "reused"})
            _reuse_resp = {"id": container.id, "name": name, "status": "reused",
                           "note": note_value}
            if _payload is not None:
                _reuse_resp["drift"] = _payload
            return _reuse_resp

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
            CONTAINER_NAME_LABEL: name,
            "thoughtmachine.workspace_id": self.workspace_id,
            CONTAINER_TYPE_LABEL: CONTAINER_TYPE_FREE_USE,
        }
        if worker_name:
            # Ownership label: lets a worker reclaim the containers it created
            # at teardown. The value is the worker's owner identity
            # ("<session_id or 'unknown'>:<worker_name>") stamped VERBATIM;
            # teardown compares the label value EXACTLY (mismatched/stale
            # values are ignored).
            labels["thoughtmachine.worker"] = worker_name
            # Self-heal: a crashed/hung worker session may have left stale
            # containers behind (same owner identity, created/exited/dead).
            # Remove them BEFORE the fresh create so a name collision can
            # never block a worker respawn. Best-effort: a failure here must
            # not block the spawn.
            try:
                cleanup_stale_worker_containers(self.client, worker_name)
            except Exception as exc:
                log("WARNING", "docker.container_manager",
                    f"Stale worker container cleanup failed for {worker_name}: {exc}")
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
                    workspace_id=self.workspace_id,
                    mem_limit=self.mem_limit,
                    cpu_quota=self.cpu_quota,
                    oom_score_adj=1000,
                    labels=labels,
                    environment=merge_container_identity_env(
                        {"PYTHONUSERBASE": "/home/agent/.local"},
                        session_id=self.session_id,
                        workspace_id=self.workspace_id,
                    ),
                    mounts=[{
                        "source": self.workspace_path,
                        "target": "/workspace",
                        "mode": "ro" if workspace_mode != "rw" else "rw",
                    }],
                    volumes=[f"tm-packages-{self.workspace_id}:/home/agent/.local"],
                    tmpfs=tmpfs,
                    lifecycle_class=lifecycle_class,
                    name=name,
                )
            except RuntimeError as exc:
                if "Container limit reached" in str(exc):
                    return {"error": f"Workspace container limit reached: {exc}"}
                raise
            container_id = handle["id"]
            container_name = handle["name"]
            # Record-first identity: the registry minted the record on its own
            # create path; index it here so a subsequent start(name=...) reuses
            # it.  Resolve the record id from the live container label.
            self._name_index_register(
                name, self._record_id_for_name(container_name))
            self._containers[name] = container_id
            if note is not None:
                self._write_note(self._record_id_for_name(container_id), note)
            _audit("CONTAINER_CREATE",
                   f"source=registry image={image} name={container_name} "
                   f"session={self.session_id} workspace_id={self.workspace_id}")
            log_container_event("started", container_id=container_id,
                                session_id=self.session_id or "",
                                data={"image": image, "name": name, "status": "created"})
            return {"id": container_id, "name": name, "status": "created",
                    "note": note or ""}

        # Admission control (phase 2): the legacy (registry-inactive) create is a
        # terminal container-create site, so gate it through the pure admission
        # gate before touching the daemon.  Fail closed: a ``Deny`` returns an
        # error dict; a ``Transform`` may only narrow the network mode.
        # (When the registry is active this code is unreachable - the registry
        # already applied admission on its own create path.)
        _admission_spec = ContainerSpec(
            container_type="user",
            lifecycle_class=lifecycle_class,
            workspace_id=self.workspace_id,
            session_id=self.session_id,
            # The image is operator-configured (``self.image``, defaulting to
            # ``DEFAULT_IMAGE``), so gating it against the user-image allowlist is
            # a tautology; image enforcement lives on the registry path/upstream.
            image=None,
            name=name,
            mem_limit=self.mem_limit,
            cpu_quota=self.cpu_quota,
            oom_score_adj=1000,
            network_mode=network_mode,
            read_only=True,
        )
        _admission_request = AdmissionRequest(
            spec=_admission_spec,
            permissions=self.session_permissions or {},
            capabilities=_load_capabilities(self.workspace_id),
            session_config=getattr(self, "_session_config", None),
        )
        _decision = admit(_admission_request, probes=ClientProbes(self.client))
        if isinstance(_decision, Deny):
            return {"error": _decision.message, "code": _decision.code}
        if isinstance(_decision, Transform):
            network_mode = _decision.spec.network_mode

        with record_creation(
            workspace_id=self.workspace_id,
            lifecycle_class=lifecycle_class,
            labels=labels,
            name=name,
        ) as record:
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
                environment=merge_container_identity_env(
                    {"PYTHONUSERBASE": "/home/agent/.local"},
                    session_id=self.session_id,
                    workspace_id=self.workspace_id,
                ),
                restart_policy=docker_restart_policy(lifecycle_class),
                labels=labels,
            )
            record.attach(container)
            # Record-first identity: index the freshly minted record so a
            # subsequent start(name=...) reuses it instead of re-creating.
            self._name_index_register(name, getattr(record, "id", ""))
        try:
            container.reload()
        except Exception:
            pass
        self._containers[name] = container.id
        if note is not None:
            self._write_note(self._record_id_for(container), note)
        log_container_event("started", container_id=container.id,
                            session_id=self.session_id or "",
                            data={"image": image, "name": name, "status": "created"})
        return {"id": container.id, "name": name, "status": "created",
                "note": note or ""}

    def exec(self, container_id, command, timeout=30, workdir="/workspace", environment=None):
        """Run ``command`` in the container; returns {"stdout","stderr","exit_code"}."""
        container = self.client.containers.get(container_id)
        _denial = self._agent_access_denial(self.class_of(container))
        if _denial is not None:
            raise PermissionError(_denial)

        # ── Exec-path drift admission (fail-safe) ─────────────────────────────────
        # Deny when the LIVE container is more permissive than a RESOLVED session
        # policy; warn (and continue) when it differs but is not more
        # permissive.  An UNRESOLVABLE policy proves nothing, so it (like any
        # other classifier error) degrades to "run as today".
        _drift_action, _drift_payload = self._check_exec_drift(container, container_id)
        if _drift_action == "deny":
            return _drift_payload
        _exec_drift = _drift_payload if _drift_action == "warn" else None

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
        merged_env = merge_container_identity_env(
            environment,
            session_id=self.session_id,
            workspace_id=self.workspace_id,
        )
        if merged_env:
            exec_kwargs["environment"] = merged_env

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
        result = {
            "stdout": _truncate_output(stdout),
            "stderr": _truncate_output(stderr),
            "exit_code": exit_code,
        }
        if _exec_drift is not None:
            result["drift"] = _exec_drift
        return result

    def _check_exec_drift(self, container, container_id):
        """Classify the live container's isolation vs the session policy.

        Returns one of:
          ("run", None)           — no observable drift; run as today.
          ("warn", drift_dict)    — drift seen; run, attaching drift_dict.
          ("deny", response_dict) — live is MORE permissive; return the dict
                                    WITHOUT running the command.

        The session policy is resolved with ``strict=True``: when the policy
        SSOT is unavailable, an unresolvable policy PROVES NOTHING about the
        live container, so we have no drift opinion and run as today (a deny
        requires a genuinely RESOLVED policy proving live is more permissive).
        Any OTHER unexpected exception likewise degrades to ("run", None) as a
        last-resort so a classifier bug can never block legitimate exec.
        """
        try:
            try:
                want_net, want_ws = self._compute_config(
                    getattr(self, "workspace_path", None),
                    getattr(self, "workspace_id", None),
                    getattr(self, "session_permissions", None),
                    LIFECYCLE_PERSISTENT,
                    strict=True,
                )
            except Exception as exc:
                # The policy SSOT could NOT be resolved at all.  This proves
                # nothing about the live container, so we form no drift opinion
                # and MUST NOT deny (deny requires a RESOLVED policy).
                try:
                    log("WARNING", "docker.container_manager",
                        "session policy could not be resolved "
                        f"({exc!r}); proceeding without exec drift opinion")
                except Exception:
                    pass
                return "run", None

            if self._config_matches(container, want_net, want_ws):
                return "run", None

            live_net, live_ws = _exec_live_isolation(container)
            decision, reason = _exec_drift_decision(
                live_net, live_ws, want_net, want_ws
            )
            if decision == "run":
                return "run", None

            drift = {
                "drifted": True,
                "decision": decision,
                "reason": reason,
                "network_mode": live_net,
                "workspace_mode": live_ws,
            }
            signature = hashlib.sha256(
                f"{want_net}|{want_ws}|{live_net}|{live_ws}|{decision}".encode()
            ).hexdigest()
            self._emit_exec_drift_once(
                container_id, container, signature, decision, reason,
                want_net, want_ws, live_net, live_ws,
            )

            if decision == "deny":
                message = (
                    "Container isolation is MORE PERMISSIVE than the session "
                    f"policy ({reason}); refusing to run the command. "
                    f"expected network={want_net} workspace={want_ws}; "
                    f"live network={live_net} workspace={live_ws}. "
                    "Recreate the container to restore the desired isolation."
                )
                return "deny", {
                    "stdout": "",
                    "stderr": f"{message}\nreason={reason}",
                    "exit_code": _EXEC_DRIFT_EXIT_CODE,
                    "drift": drift,
                }
            return "warn", drift
        except Exception as exc:  # fail-safe: never block exec on classifier error
            try:
                log("WARNING", "docker.container_manager",
                    f"exec drift check failed ({exc!r}); proceeding")
            except Exception:
                pass
            return "run", None

    def _emit_exec_drift_once(self, container_id, container, signature, decision,
                              reason, want_net, want_ws, live_net, live_ws):
        """Emit drift EVENT + WARNING + audit ONCE per (container_id, signature).

        Best-effort: every sub-step is individually guarded so a logging/record
        failure never affects the exec decision.  Repeat calls with the same
        signature skip emission entirely (the decision is still applied by the
        caller).
        """
        key = (container_id, signature)
        with _EXEC_DRIFT_LOCK:
            if key in _EXEC_DRIFT_SEEN:
                return
            _EXEC_DRIFT_SEEN[key] = True
            while len(_EXEC_DRIFT_SEEN) > _EXEC_DRIFT_MEMO_MAX:
                _EXEC_DRIFT_SEEN.popitem(last=False)

        summary = (
            f"container_id={container_id} decision={decision} reason={reason} "
            f"expected(network={want_net},workspace={want_ws}) "
            f"actual(network={live_net},workspace={live_ws})"
        )
        try:
            log("WARNING", "docker.container_manager",
                f"exec on drifted container: {summary}")
        except Exception:
            pass

        try:
            _audit(_EXEC_DRIFT_AUDIT, summary)
        except Exception:
            pass

        try:
            labels = getattr(container, "labels", None) or {}
            record_id = labels.get(RECORD_LABEL_KEY)
            if record_id is not None and getattr(self, "workspace_id", None):
                from thoughtmachine.container_record import append_event
                append_event(
                    self.workspace_id,
                    str(record_id),
                    _EXEC_DRIFT_EVENT,
                    _EXEC_DRIFT_ACTOR,
                    decision=decision,
                    reason=reason,
                    expected={"network_mode": want_net, "workspace_mode": want_ws},
                    actual={"network_mode": live_net, "workspace_mode": live_ws},
                    detected_at=datetime.now(timezone.utc).isoformat(),
                )
        except Exception:
            pass

    def _expected_restart_policy(self, container, lifecycle_class):
        """Resolve the restart policy a container SHOULD carry.

        Prefers the persisted record's ``restart_policy`` (authoritative for a
        container that already has a record), falling back to the lifecycle
        class policy.  ``None`` when neither is determinable (e.g. an unknown
        class) --- which reads as "no opinion" on this axis.
        """
        record_policy = None
        try:
            labels = getattr(container, "labels", None) or {}
            record_id = labels.get(RECORD_LABEL_KEY)
            if record_id:
                record = find_by_docker_label(record_id)
                record_policy = getattr(record, "restart_policy", None)
        except Exception:
            record_policy = None
        if record_policy:
            return normalise_restart_policy(record_policy)
        try:
            return policy_for(lifecycle_class).restart_policy
        except UnknownLifecycleClass:
            return None

    def _restart_drift_axis(self, container, lifecycle_class):
        """Classify restart-policy drift for a reused container.

        Returns ``None`` (no drift / not determinable) or a tuple
        ``(decision, reason, detail)`` where decision is ``"deny"`` (live is
        MORE permissive) or ``"warn"`` (differs, not more permissive).
        """
        expected = self._expected_restart_policy(container, lifecycle_class)
        actual = _live_restart_policy(container)
        if expected is None or actual is None:
            return None
        if actual == expected:
            return None
        detail = {"expected": expected, "actual": actual}
        if _restart_policy_rank(actual) > _restart_policy_rank(expected):
            return "deny", "restart_policy_more_permissive", detail
        return "warn", "restart_policy_differs_not_more_permissive", detail

    def _emit_restart_drift_once(self, container_id, container, source, detail,
                                 decision):
        """Emit restart-policy drift EVENT + WARNING + audit ONCE per signature.

        Same module-scope memo/lock discipline as :meth:`_emit_start_drift_once`
        (separate signature namespace via the "restart" tag).  Best-effort:
        every sub-step is individually guarded.
        """
        expected = detail.get("expected")
        actual = detail.get("actual")
        signature = hashlib.sha256(
            f"{source}|restart|{expected}|{actual}".encode()
        ).hexdigest()
        key = (container_id, signature)
        with _START_DRIFT_LOCK:
            if key in _START_DRIFT_SEEN:
                return
            _START_DRIFT_SEEN[key] = True
            while len(_START_DRIFT_SEEN) > _EXEC_DRIFT_MEMO_MAX:
                _START_DRIFT_SEEN.popitem(last=False)

        summary = (
            f"container_id={container_id} source={source} decision={decision} "
            f"expected_restart_policy={expected} actual_restart_policy={actual}"
        )
        try:
            log("WARNING", "docker.container_manager",
                f"start on restart-policy-drifted container: {summary}")
        except Exception:
            pass

        try:
            _audit(_START_DRIFT_RESTART_AUDIT, summary)
        except Exception:
            pass

        try:
            labels = getattr(container, "labels", None) or {}
            record_id = labels.get(RECORD_LABEL_KEY)
            if record_id is not None and getattr(self, "workspace_id", None):
                from thoughtmachine.container_record import append_event
                append_event(
                    self.workspace_id,
                    str(record_id),
                    _START_DRIFT_RESTART_EVENT,
                    _START_DRIFT_RESTART_ACTOR,
                    source=source,
                    decision=decision,
                    expected={"restart_policy": expected},
                    actual={"restart_policy": actual},
                    detected_at=datetime.now(timezone.utc).isoformat(),
                )
        except Exception:
            pass

    def _emit_stale_docker_id_drift_once(self, name, record_id, stale_docker_id,
                                         source):
        """Emit stale-docker-id drift EVENT + WARNING + audit ONCE per signature.

        Fired on the start path when a record names a ``docker_id`` that has no
        live container.  Same module-scope memo/lock discipline as
        :meth:`_emit_restart_drift_once` (separate signature namespace via the
        ``stale-docker-id`` tag).  Best-effort: every sub-step is individually
        guarded.  The record's ``docker_id`` is NEVER rewritten here.
        """
        docker_id = ""  # the container the record names is ABSENT
        signature = hashlib.sha256(
            f"{source}|{stale_docker_id}|{record_id}|{docker_id}".encode()
        ).hexdigest()
        key = (stale_docker_id, signature)
        with _START_DRIFT_LOCK:
            if key in _START_DRIFT_SEEN:
                return
            _START_DRIFT_SEEN[key] = True
            while len(_START_DRIFT_SEEN) > _EXEC_DRIFT_MEMO_MAX:
                _START_DRIFT_SEEN.popitem(last=False)

        summary = (
            f"name={name} record_id={record_id} source={source} "
            f"expected_docker_id={stale_docker_id} actual_docker_id=None"
        )
        try:
            log("WARNING", "docker.container_manager",
                f"start on stale-docker-id record: {summary}")
        except Exception:
            pass

        try:
            _audit("CONTAINER_START_STALE_DOCKER_ID", summary)
        except Exception:
            pass

        try:
            if getattr(self, "workspace_id", None):
                from thoughtmachine.container_record import append_event
                append_event(
                    self.workspace_id,
                    str(record_id),
                    drift.EVENT_CONTAINER_ABSENT,
                    _START_DRIFT_ACTOR,
                    source=source,
                    expected={"docker_id": stale_docker_id},
                    actual={"docker_id": None},
                    detected_at=datetime.now(timezone.utc).isoformat(),
                )
        except Exception:
            pass

    def _start_drift_decision(self, container, want_net, want_ws, source,
                              lifecycle_class=None):
        """Classify a reused container's isolation vs the resolved start policy.

        Start-path twin of :meth:`_check_exec_drift`, but for the REUSE decision
        (there is no command to run).  Two axes are evaluated: container
        isolation (network + /workspace mount) and --- when ``lifecycle_class``
        is supplied --- the Docker restart policy.  Returns one of:

          ("ok", None)             — no observable drift; reuse as today.
          ("reuse", drift_dict)    — drift seen but NOT more permissive; reuse
                                     the container, attaching drift_dict.
          ("deny", response_dict)  — live is MORE permissive on either axis;
                                     REFUSE (return an error response carrying
                                     the drift) WITHOUT running, mutating or
                                     handing back the container.

        The container is NEVER removed or recreated here: start() no longer
        MUTATES on drift.  A caller that must replace a more-permissive drifted
        container (e.g. the ephemeral runner) acts on the refusal itself.
        """
        config_ok = self._config_matches(container, want_net, want_ws)
        restart_axis = None
        if lifecycle_class is not None:
            restart_axis = self._restart_drift_axis(container, lifecycle_class)
        if config_ok and restart_axis is None:
            return "ok", None

        live_net, live_ws = _exec_live_isolation(container)
        isolation_decision = None
        reason = None
        if not config_ok:
            _decision, _reason = _exec_drift_decision(
                live_net, live_ws, want_net, want_ws
            )
            if _decision != "run":
                isolation_decision, reason = _decision, _reason

        restart_decision = None
        restart_reason = None
        restart_detail = None
        if restart_axis is not None:
            restart_decision, restart_reason, restart_detail = restart_axis

        if isolation_decision is None and restart_decision is None:
            return "ok", None

        decision = (
            "deny" if "deny" in (isolation_decision, restart_decision) else "warn"
        )
        if reason is None:
            reason = restart_reason

        drift = {
            "drifted": True,
            "decision": decision,
            "reason": reason,
            "network_mode": live_net,
            "workspace_mode": live_ws,
            "source": source,
        }
        if restart_detail is not None:
            drift["restart_policy"] = restart_detail
        container_id = getattr(container, "id", None)
        if isolation_decision is not None:
            signature = hashlib.sha256(
                f"{source}|{want_net}|{want_ws}|{live_net}|{live_ws}|"
                f"{isolation_decision}".encode()
            ).hexdigest()
            self._emit_start_drift_once(
                container_id, container, signature, isolation_decision, reason,
                want_net, want_ws, live_net, live_ws, source,
            )
        if restart_detail is not None:
            self._emit_restart_drift_once(
                container_id, container, source, restart_detail,
                restart_decision,
            )

        if decision == "deny":
            # Mirror the exec deny payload EXACTLY: the drifted container id
            # rides INSIDE ``drift`` (never a top-level ``container_id``, which a
            # lifecycle consumer would mistake for a successfully-tracked
            # container).
            drift["container_id"] = container_id
            if restart_decision == "deny" and isolation_decision == "deny":
                message = (
                    "Container isolation and restart policy are MORE PERMISSIVE "
                    f"than policy (isolation={reason}; "
                    f"restart={restart_reason}: expected "
                    f"{restart_detail['expected']} got "
                    f"{restart_detail['actual']}); refusing to reuse the "
                    "container."
                )
            elif restart_decision == "deny":
                message = (
                    "Container restart policy is MORE PERMISSIVE than the "
                    f"lifecycle policy ({restart_reason}: expected "
                    f"{restart_detail['expected']} got "
                    f"{restart_detail['actual']}); refusing to reuse the "
                    "container. Recreate the container to restore the desired "
                    "restart policy."
                )
            else:
                message = (
                    "Container isolation is MORE PERMISSIVE than the session "
                    f"policy ({reason}); refusing to reuse the container. "
                    f"expected network={want_net} workspace={want_ws}; "
                    f"live network={live_net} workspace={live_ws}. "
                    "Recreate the container to restore the desired isolation."
                )
            return "deny", {"error": message, "drift": drift}
        return "reuse", drift

    def _emit_start_drift_once(self, container_id, container, signature, decision,
                               reason, want_net, want_ws, live_net, live_ws, source):
        """Emit drift EVENT + WARNING + audit ONCE per (container_id, signature).

        Start-path twin of :meth:`_emit_exec_drift_once` (its own module-scope
        memo + lock, same dedup/discipline).  Best-effort: every sub-step is
        individually guarded so a logging/record failure never affects the
        start decision.
        """
        key = (container_id, signature)
        with _START_DRIFT_LOCK:
            if key in _START_DRIFT_SEEN:
                return
            _START_DRIFT_SEEN[key] = True
            while len(_START_DRIFT_SEEN) > _EXEC_DRIFT_MEMO_MAX:
                _START_DRIFT_SEEN.popitem(last=False)

        summary = (
            f"container_id={container_id} source={source} decision={decision} "
            f"reason={reason} "
            f"expected(network={want_net},workspace={want_ws}) "
            f"actual(network={live_net},workspace={live_ws})"
        )
        try:
            log("WARNING", "docker.container_manager",
                f"start on drifted container: {summary}")
        except Exception:
            pass

        try:
            _audit(_START_DRIFT_AUDIT, summary)
        except Exception:
            pass

        try:
            labels = getattr(container, "labels", None) or {}
            record_id = labels.get(RECORD_LABEL_KEY)
            if record_id is not None and getattr(self, "workspace_id", None):
                from thoughtmachine.container_record import append_event
                append_event(
                    self.workspace_id,
                    str(record_id),
                    _START_DRIFT_EVENT,
                    _START_DRIFT_ACTOR,
                    source=source,
                    decision=decision,
                    reason=reason,
                    expected={"network_mode": want_net, "workspace_mode": want_ws},
                    actual={"network_mode": live_net, "workspace_mode": live_ws},
                    detected_at=datetime.now(timezone.utc).isoformat(),
                )
        except Exception:
            pass

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
                _denial = self._agent_access_denial(self.class_of_handle(handle))
                if _denial is not None:
                    return {"status": "error", "container_id": container_id,
                            "error": _denial}
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
        _denial = self._agent_access_denial(self.class_of(container))
        if _denial is not None:
            return {"status": "error", "container_id": container_id,
                    "error": _denial}
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
                _denial = self._agent_access_denial(self.class_of_handle(handle))
                if _denial is not None:
                    return {"status": "error", "container_id": container_id,
                            "error": _denial}
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
        _denial = self._agent_access_denial(self.class_of(container))
        if _denial is not None:
            return {"status": "error", "container_id": container_id,
                    "error": _denial}
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
            "note": self._read_note(container),
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
        other workspaces — or unlabeled ones — never appear. ``note`` is the
        container's RECORD sticky note (a legacy container_notes.json sidecar is
        consulted read-only as a fallback when the container has no record).
        Returns a list of dicts with EXACTLY: ``container_id``,
        ``name``, ``image``, ``status``, ``uptime_seconds``, ``workspace_id``,
        ``note``, ``labels``.
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
            if self._agent_access_denial(self.class_of(container)) is not None:
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
                "labels": dict(container.labels or {}),
                "note": self._read_note(container),
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

        _denial = self._agent_access_denial(self.class_of(container))
        if _denial is not None:
            raise RuntimeError(_denial)

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

    # ── Lifecycle class (single resource/record classifier) ───────────────

    def class_of(self, container) -> str:
        """Lifecycle class of a live ``container`` object.

        Resource containers are recognised by the shared live probe; every
        other container is resolved through its container-record (keyed by the
        ``RECORD_LABEL_KEY`` label) and falls back to ``LIFECYCLE_PERSISTENT``
        when no record is attached or resolvable.

        The record lookup calls :func:`find_by_docker_label` with the DEFAULT
        vault resolution (``vault_root=None``), mirroring the writer
        (``thoughtmachine.container_record.hook.record_creation``), which also
        writes with the default vault.  Do NOT thread a manager/session
        ``vault_root`` in here without changing the writer too: a divergent
        vault makes the lookup silently miss, so the class degrades to
        ``LIFECYCLE_PERSISTENT`` and the fail-closed denial for the
        ``service``/unknown classes is LOST.
        """
        return _container_lifecycle_class(container)

    def class_of_handle(self, handle) -> str:
        """Lifecycle class of a registry ``handle`` dict.

        The registry records ``container_type`` for every tracked container, so
        a ``"resource"`` handle is classified without touching the daemon.  Any
        other handle is bridged to :meth:`class_of` via the live container (the
        handle's ``id``/``name``), and finally by the ``tm-res-`` name prefix.
        """
        if not isinstance(handle, dict):
            return LIFECYCLE_PERSISTENT
        if handle.get("container_type") == CONTAINER_TYPE_RESOURCE:
            return LIFECYCLE_RESOURCE
        container = None
        container_id = handle.get("id") or handle.get("name")
        if container_id:
            try:
                container = self.client.containers.get(container_id)
            except Exception:
                container = None
        if container is not None:
            return self.class_of(container)
        name = handle.get("name") or handle.get("id") or ""
        if isinstance(name, str) and name.lstrip("/").startswith(RESOURCE_NAME_PREFIX):
            return LIFECYCLE_RESOURCE
        return LIFECYCLE_PERSISTENT

    def policy_of(self, container):
        """Lifecycle policy for a live ``container`` object."""
        return policy_for(self.class_of(container))

    def policy_of_handle(self, handle):
        """Lifecycle policy for a registry ``handle`` dict."""
        return policy_for(self.class_of_handle(handle))

    @staticmethod
    def _agent_access_denial(lifecycle_class):
        """Denial reason for an agent-facing ACCESS site, or None when allowed.

        Fail-closed: an unknown lifecycle class (no policy) is denied with the
        ``REASON_UNKNOWN_LIFECYCLE_CLASS`` code; a class whose policy is not
        ``agent_reachable`` is denied with the standard resource message.
        """
        try:
            policy = policy_for(lifecycle_class)
        except UnknownLifecycleClass:
            return REASON_UNKNOWN_LIFECYCLE_CLASS
        if not policy.agent_reachable:
            return "Resource container access denied"
        return None

    @staticmethod
    def _is_resource_container(obj):
        """True when ``obj`` is a hidden resource container (tm-res-*).

        Thin delegation to the shared, pure probe
        :func:`thoughtmachine.container_record.is_resource_like` — the single
        source of truth for resource-container identity (``thoughtmachine.resource``
        label, ``tm-res-`` name prefix, or ``tm-resource-git`` image).  Any probe
        failure is treated as False (a non-resource container).
        """
        try:
            labels = getattr(obj, "labels", None)
            name = getattr(obj, "name", None)
            image = getattr(obj, "image", None)
            if image is None or isinstance(image, str):
                image_tags = image
            else:
                image_tags = getattr(image, "tags", None)
            return is_resource_like(labels, name=name, image_tags=image_tags)
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
            if self._agent_access_denial(self.class_of(first)) is not None:
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
        """Set the container's sticky note on its RECORD; NEVER raises.

        Persists the note as the ``notes`` field of the container's RECORD
        (located via the ``thoughtmachine.container_id`` Docker label), so it
        survives manager/session restarts and container recreation and is
        visible to every manager of the workspace. Fail-closed: a container
        with no record id is REFUSED and nothing is written. Docker labels are
        never touched (they are immutable after create on stock daemons).

        Returns {"success": True, "note": note} on success; on failure an error
        dict following the existing convention:
        {"success": False, "container_id": ..., "error": "container not found"}
        or {"success": False, "container_id": ..., "error": "no container record"}
        or {"success": False, "container_id": ..., "error": str(e)}.
        """
        # Adopt any legacy container_notes.json entries onto records first
        # (idempotent; a no-op once per workspace).
        self._migrate_legacy_notes_once()
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
        record_id = self._record_id_for(container)
        if not record_id:
            self._warn_note_once(
                ("notes.set_note_no_record", self.workspace_id),
                f"Refusing set_note for container {container_id!r}: the container "
                f"carries no {RECORD_LABEL_KEY} label (no container record).")
            return {"success": False, "container_id": container_id,
                    "error": "no container record"}
        self._write_note(record_id, note)
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

    def _image_ref(self, container):
        """Best-effort human-readable image reference for ``container``.

        Prefers the first tag (``container.image.tags[0]``), then the image
        reference recorded in ``attrs['Config']['Image']``, then the image's
        short id, else ``"<unknown>"``. Returns ``""`` on ANY exception (and
        for ``container is None``) so callers can treat "unknown" distinctly.
        """
        try:
            if container is None:
                return ""
            img = getattr(container, "image", None)
            tags = getattr(img, "tags", None) or []
            if tags:
                return tags[0]
            cfg_image = (
                (getattr(container, "attrs", None) or {}).get("Config", {}) or {}
            ).get("Image", "")
            if cfg_image:
                return cfg_image
            short_id = getattr(img, "short_id", None)
            if short_id:
                return short_id
            return "<unknown>"
        except Exception:
            return ""

    @staticmethod
    def _normalize_image_ref(ref):
        """Normalize an image reference for comparison.

        Strips whitespace and appends ``':latest'`` when the repository part
        carries no explicit tag (so ``nginx`` == ``nginx:latest``). A ':' inside
        the registry host:port segment is NOT a tag.
        """
        if not ref:
            return ""
        ref = str(ref).strip()
        last_slash = ref.rfind("/")
        if ":" not in ref[last_slash + 1:]:
            ref = f"{ref}:latest"
        return ref

    def _image_matches(self, container, image):
        """True if ``container`` runs an image matching the requested ``image``.

        Conservative: returns True when the container's image cannot be
        determined (no tags/Config.Image), so an unknown image never blocks a
        legitimate reuse. Any exception is treated as a match (fail-open on
        comparison, fail-closed on the safety guard only when we KNOW the
        images differ).
        """
        try:
            wanted = self._normalize_image_ref(image)
            if not wanted:
                return True
            candidates = []
            ref = self._image_ref(container)
            if ref and ref != "<unknown>":
                candidates.append(ref)
            try:
                cfg_image = (
                    (getattr(container, "attrs", None) or {}).get("Config", {}) or {}
                ).get("Image")
                if cfg_image:
                    candidates.append(cfg_image)
            except Exception:
                pass
            if not candidates:
                return True
            return any(
                self._normalize_image_ref(c) == wanted for c in candidates
            )
        except Exception:
            return True

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

    def _compute_config(
        self,
        workspace_path,
        workspace_id,
        session_permissions,
        lifecycle_class=LIFECYCLE_PERSISTENT,
        *,
        strict=False,
    ):
        """Desired (network_mode, workspace_mode) from the security-gate SSOT.

        Routes through ``security.security_gate.resolve_container_config`` -
        the canonical, pure container-config resolver - merging the session
        permissions with the workspace's (fail-closed) capabilities. Fail-closed
        to ``("none", "ro")`` only if the SSOT is unavailable, raises, or
        reports a ``ContainerConfigError``.

        ``strict`` (keyword-only, default ``False``): when ``True`` the method
        does NOT fail-closed to the ``("none", "ro")`` sentinel; instead it
        PROPAGATES the resolution failure (re-raises / raises ``RuntimeError``)
        so the caller can tell "the policy genuinely resolved to none/ro" apart
        from "the policy could not be resolved at all".  Only the exec-path
        drift admission passes ``strict=True``; every other caller keeps the
        historical fail-closed behaviour byte-for-byte.
        """
        try:
            from security.security_gate import (
                ContainerConfig,
                get_workspace_capabilities,
                resolve_container_config,
            )

            capabilities = get_workspace_capabilities(workspace_id)
            cfg = resolve_container_config(
                session_permissions or {}, capabilities, lifecycle_class
            )
        except Exception:
            if strict:
                raise
            return "none", "ro"
        if not isinstance(cfg, ContainerConfig):
            if strict:
                raise RuntimeError(
                    "resolve_container_config did not return a ContainerConfig "
                    f"(got {type(cfg).__name__}); cannot resolve session policy"
                )
            return "none", "ro"
        return cfg.network_mode, cfg.workspace_mode

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


# ── Stale worker-container cleanup (crashed/hung worker sessions) ────────────
_WORKER_LABEL = "thoughtmachine.worker"
_REMOVABLE_WORKER_STATES = {"created", "exited", "dead"}


def cleanup_stale_worker_containers(docker_client, owner_identity):
    """Remove containers a crashed/hung worker session left behind.

    Worker containers carry ``thoughtmachine.worker=<owner identity>`` (see
    ``ContainerManager.start(worker_name=...)``). When a worker dies without
    teardown, its container lingers in ``created``/``exited``/``dead`` state
    and can block a later respawn of the same worker (docker name/label
    collisions).

    Only containers whose label value EXACTLY matches ``owner_identity`` and
    whose state is one of ``{'created', 'exited', 'dead'}`` are removed:
    running workers, paused/restarting containers and resource containers
    (``thoughtmachine.resource`` label) are never touched. The label filter
    runs server-side; the per-container checks are defensive for fake/missing
    attributes (all access via ``getattr``).

    Returns the list of removed container ids. Never raises: failures are
    logged and skipped.
    """
    removed = []
    try:
        matches = docker_client.containers.list(
            all=True,
            filters={"label": {_WORKER_LABEL: owner_identity}},
        )
    except Exception as exc:
        log("WARNING", "docker.container_manager",
            f"Failed to list stale worker containers for {owner_identity}: {exc}")
        return removed
    for container in matches or []:
        try:
            labels = getattr(container, "labels", None) or {}
            if labels.get(_WORKER_LABEL) != owner_identity:
                # Not ours (defensive: the label filter runs server-side, but
                # fake/mislabeled containers must never be touched).
                continue
            if _gc_should_skip(container):
                # Resource / lifecycle-owning containers (e.g. the git sandbox from
                # infra/resource_container_manager) are owned elsewhere — never here.
                continue
            status = getattr(container, "status", None)
            if status is None:
                attrs = getattr(container, "attrs", None) or {}
                status = (attrs.get("State") or {}).get("Status")
            if status not in _REMOVABLE_WORKER_STATES:
                continue
            container_id = getattr(container, "id", None)
            container.remove(force=True)
            removed.append(container_id)
            short = str(container_id)[:12]
            log("INFO", "docker.container_manager",
                f"Removed stale worker container {short} ({status}) for {owner_identity}")
        except Exception as exc:
            log("WARNING", "docker.container_manager",
                f"Failed to remove stale worker container "
                f"{getattr(container, 'id', getattr(container, 'name', '?'))}: {exc}")
    return removed


# ── Idle/TTL + orphan sweep for EXITED workspace containers ─────────────────
_WORKSPACE_LABEL = "thoughtmachine.workspace_id"
_RESOURCE_LABEL = RESOURCE_LABEL
_SWEEP_SKIP_DETAIL_CAP = 8  # keep startup log lines bounded


def _container_lifecycle_class(container) -> str:
    """Best-effort lifecycle class of a live ``container`` object.

    Module-level twin of :meth:`ContainerManager.class_of`, for the sweeps that
    run without a manager instance: resource containers are recognised by the
    shared live probe; every other container is resolved through its
    container-record (``RECORD_LABEL_KEY`` label) and falls back to
    ``LIFECYCLE_PERSISTENT`` when no record is attached or resolvable.

    The record lookup uses the DEFAULT vault resolution
    (``find_by_docker_label(record_id)`` with ``vault_root=None``), matching the
    writer ``record_creation``; a divergent vault makes the lookup silently miss
    and the class degrade to ``LIFECYCLE_PERSISTENT`` (losing the fail-closed
    denial for the ``service``/unknown classes).  See :meth:`class_of`.
    """
    try:
        if ContainerManager._is_resource_container(container):
            return LIFECYCLE_RESOURCE
    except Exception:
        pass
    record_id = None
    try:
        labels = getattr(container, "labels", None)
        if isinstance(labels, dict):
            record_id = labels.get(RECORD_LABEL_KEY)
    except Exception:
        record_id = None
    if record_id:
        try:
            record = find_by_docker_label(record_id)
        except Exception:
            record = None
        if record is not None:
            cls = getattr(record, "lifecycle_class", "") or ""
            if cls:
                return cls
    return LIFECYCLE_PERSISTENT


def _gc_should_skip(container) -> bool:
    """True when a destructive workspace GC must NOT touch ``container``.

    Containers whose lifecycle class owns its own lifecycle (resource/service)
    are skipped, as is any container with an unknown/absent class
    (fail-closed).
    """
    try:
        policy = policy_for(_container_lifecycle_class(container))
    except UnknownLifecycleClass:
        return True
    return policy.own_lifecycle


def _container_name(container):
    """Best-effort human-readable name for a container object."""
    for attr in ("name", "id"):
        try:
            value = getattr(container, attr, None)
        except Exception:
            value = None
        if value:
            return value
    return repr(container)


def sweep_exited_workspace_containers(registered_workspace_ids=None,
                                      max_age_s=86400, dry_run=False):
    """Sweep EXITED ``thoughtmachine.workspace_id``-labelled containers that
    have been idle for at least ``max_age_s`` seconds.

    Two branches share the same predicate (``status == 'exited'`` AND idle
    age ``>= max_age_s``):
    - registered workspaces -> idle/TTL branch (``removed_registered``)
    - unregistered workspaces -> orphan branch (``removed_orphan``)
    Resource containers (``thoughtmachine.resource`` label) are ALWAYS
    skipped, as are running/created containers and any container whose
    ``State.FinishedAt`` is missing, unparseable or in the future (clock
    skew safety).

    ``registered_workspace_ids=None`` -> TTL-only sweep: every workspace
    container is treated as registered (orphan classification disabled).
    ``registered_workspace_ids=[]``  -> conservative NO-OP: with an empty
    registry every container would look like an orphan, so nothing is
    removed ("safe when the registry is empty").

    ``dry_run=True`` counts would-be removals but never calls ``remove()``.
    Never raises — a missing/broken docker daemon soft-fails into a result.
    Returns::

        {"removed": int, "skipped": int, "detail": str, "dry_run": bool,
         "removed_registered": int, "removed_orphan": int,
         "removed_containers": [container name or id, ...]}
    """
    result = {
        "removed": 0,
        "skipped": 0,
        "detail": "",
        "dry_run": bool(dry_run),
        "removed_registered": 0,
        "removed_orphan": 0,
        "removed_containers": [],
    }

    if registered_workspace_ids is not None and len(registered_workspace_ids) == 0:
        # Empty registry -> nothing can be classified as "registered"; wiping
        # every exited container on startup would be a data-loss surprise.
        result["detail"] = "registry empty; sweep skipped"
        return result

    if not DOCKER_AVAILABLE:
        result["detail"] = "docker SDK not installed"
        return result

    try:
        client = docker.from_env()
        containers = client.containers.list(all=True, filters={"label": _WORKSPACE_LABEL})
    except Exception as exc:
        result["detail"] = f"docker unavailable: {exc}"
        return result

    registered = (
        None
        if registered_workspace_ids is None
        else {str(ws) for ws in registered_workspace_ids}
    )

    skip_counts = {}
    now = time.time()

    def _note_skip(category):
        skip_counts[category] = skip_counts.get(category, 0) + 1
        result["skipped"] += 1

    for container in containers:
        name = _container_name(container)

        try:
            labels = container.labels or {}
        except Exception:
            labels = {}
        if _gc_should_skip(container):
            # Resource / lifecycle-owning containers (hidden git images etc.) are
            # owned by infra/resource_container_manager — never touched here.
            _note_skip("resource")
            continue

        wid = labels.get(_WORKSPACE_LABEL)
        if not wid:
            _note_skip("no_workspace_label")
            continue

        try:
            status = container.status
        except Exception:
            status = None
        if status != "exited":
            _note_skip(f"status={status!r}")
            continue

        try:
            finished_at = ((container.attrs or {}).get("State") or {}).get("FinishedAt") or ""
            ts = datetime.fromisoformat(finished_at.replace("Z", "+00:00")).timestamp()
        except Exception:
            _note_skip("FinishedAt unparseable")
            continue
        if ts > now:
            _note_skip("FinishedAt in future")
            continue
        if now - ts < max_age_s:
            _note_skip("too young")
            continue

        is_registered = registered is None or wid in registered

        if dry_run:
            result["removed"] += 1
            if is_registered:
                result["removed_registered"] += 1
            else:
                result["removed_orphan"] += 1
            result["removed_containers"].append(name)
            continue

        try:
            container.remove(force=True)
        except Exception as exc:
            _note_skip(f"remove failed: {exc}")
            continue
        result["removed"] += 1
        if is_registered:
            result["removed_registered"] += 1
        else:
            result["removed_orphan"] += 1
        result["removed_containers"].append(name)

    if skip_counts:
        parts = ", ".join(f"{cat}: {cnt}" for cat, cnt in sorted(skip_counts.items()))
        if len(parts) > _SWEEP_SKIP_DETAIL_CAP * 40:
            parts = parts[:_SWEEP_SKIP_DETAIL_CAP * 40] + "…"
        result["detail"] = f"skipped ({parts})"

    _audit("CONTAINER_SWEEP",
           f"removed={result['removed']} skipped={result['skipped']} "
           f"dry_run={dry_run}")
    return result


def sweep_orphan_container_records(*, registered_workspace_ids=None,
                                   default_max_age_s=86400, dry_run=False,
                                   docker_client=None) -> dict:
    """Sweep orphaned container RECORDS whose bound container is gone.

    A record is reaped when ALL of the following hold:
    - its lifecycle class policy does NOT own its lifecycle (resource /
      service containers manage themselves and are exempt);
    - its ``docker_id`` is empty or names no LIVE container;
    - its age (from ``updated_at`` else ``created_at``) is past its retention
      window (``retention_days`` when set, else ``default_max_age_s``).

    The record's workspace registration is NOT a reap condition: a record
    whose bound container is gone is reaped whether or not its workspace is
    registered.

    ``registered_workspace_ids=None`` or ``[]`` -> conservative NO-OP: an
    absent registry is treated as an unconfigured caller, so nothing is removed.

    ``dry_run=True`` counts would-be removals but never calls ``delete_record``.
    Never raises: a missing/broken docker daemon soft-fails into a result.
    Returns::

        {"removed": int, "skipped": int, "detail": str, "dry_run": bool,
         "removed_records": [record id, ...], "removed_orphan": int}
    """
    result = {
        "removed": 0,
        "skipped": 0,
        "detail": "",
        "dry_run": bool(dry_run),
        "removed_records": [],
        "removed_orphan": 0,
    }

    if registered_workspace_ids is None or len(registered_workspace_ids) == 0:
        # Empty/absent registry -> every workspace would classify as orphan;
        # wiping every record on a bad registry read is a data-loss surprise.
        result["detail"] = "registry empty; record GC skipped"
        return result

    if docker_client is None:
        if not DOCKER_AVAILABLE:
            result["detail"] = "docker SDK not installed"
            return result
        try:
            docker_client = docker.from_env()
        except Exception as exc:
            result["detail"] = f"docker unavailable: {exc}"
            return result

    try:
        live_ids = set()
        for container in docker_client.containers.list(all=True):
            cid = getattr(container, "id", None)
            if cid:
                live_ids.add(str(cid))
    except Exception as exc:
        result["detail"] = f"docker unavailable: {exc}"
        return result

    skip_counts = {}
    now = time.time()

    def _note_skip(category):
        skip_counts[category] = skip_counts.get(category, 0) + 1
        result["skipped"] += 1

    try:
        workspace_ids = storage.iter_workspace_ids()
    except Exception:
        workspace_ids = []

    for ws in workspace_ids:
        try:
            records = list_records(ws)
        except Exception:
            records = []
        for record in records or []:
            try:
                policy = policy_for(record.lifecycle_class)
            except UnknownLifecycleClass:
                _note_skip("unknown_class")
                continue
            if policy.own_lifecycle:
                _note_skip("lifecycle_own")
                continue

            docker_id = str(getattr(record, "docker_id", "") or "")
            if docker_id and docker_id in live_ids:
                _note_skip("container_live")
                continue

            timestamp_text = (
                getattr(record, "updated_at", "")
                or getattr(record, "created_at", "")
                or ""
            )
            try:
                ts = datetime.fromisoformat(
                    str(timestamp_text).replace("Z", "+00:00")).timestamp()
            except Exception:
                ts = None
            if ts is None:
                _note_skip("no_timestamp")
                continue
            if ts > now:
                _note_skip("timestamp_in_future")
                continue

            retention = getattr(record, "retention_days", None)
            if isinstance(retention, int) and not isinstance(retention, bool) \
                    and retention > 0:
                max_age_s = retention * 86400
            else:
                max_age_s = default_max_age_s
            if now - ts < max_age_s:
                _note_skip("too young")
                continue

            if dry_run:
                result["removed"] += 1
                result["removed_orphan"] += 1
                result["removed_records"].append(record.id)
                continue

            try:
                delete_record(ws, record.id)
            except Exception as exc:
                _note_skip(f"delete failed: {exc}")
                continue
            _name_index_forget(record.id)
            result["removed"] += 1
            result["removed_orphan"] += 1
            result["removed_records"].append(record.id)
            try:
                log("WARNING", "docker.container_manager",
                    f"reaped orphan container record: workspace_id={ws} "
                    f"record_id={record.id} docker_id={docker_id!r}")
            except Exception:
                pass
            try:
                _audit("RECORD_REAP",
                       f"workspace_id={ws} record_id={record.id} "
                       f"docker_id={docker_id!r} dry_run={dry_run}")
            except Exception:
                pass

    if skip_counts:
        parts = ", ".join(f"{cat}: {cnt}" for cat, cnt in sorted(skip_counts.items()))
        if len(parts) > _SWEEP_SKIP_DETAIL_CAP * 40:
            parts = parts[:_SWEEP_SKIP_DETAIL_CAP * 40] + "\u2026"
        result["detail"] = f"skipped ({parts})"

    return result
