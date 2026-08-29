"""Centralized literal defaults for the agent runtime (Phase A consolidation).

Single source of truth for configuration *literal constants* that were
previously scattered across agent modules. This module imports only
``thoughtmachine.timeout_constants`` (a dependency-free leaf) so any module
can import from it without import-cycle risk.

Consumers use the re-export idiom: each source module does
``from agent.config.defaults import ...`` at module level so existing
importers (including tests) keep seeing the same names on the source
module.

Deliberately NOT here (stays in the source modules): mutable runtime
state, env-derived values, path computations, type aliases, lazy-import
None placeholders, derived aliases (e.g. STALE_AFTER_S,
GLOBAL_RESOURCE_IMAGES, *_BUILD_CMD) and private/underscored constants.
"""

# ── thoughtmachine/timeout_constants.py (re-exported) ────────────────────────
# Unified idle/cleanup + soft-budget timeouts. The canonical definitions live
# in the dependency-free thoughtmachine.timeout_constants leaf; they are
# re-exported here so the consolidated defaults table stays complete.
from thoughtmachine.timeout_constants import (
    IDLE_TIMEOUT_SECONDS,
    SOFT_BUDGET_FALLBACK_SECONDS,
    SHIPPED_SOFT_BUDGET_SECONDS,
)

# ── tools/workspace/worker.py ────────────────────────────────────────────────
# Spawn-queue drain timeout (generous fixed value, NOT tied to agent timeout).
SPAWN_QUEUE_TIMEOUT = 600
# A caller waiting for a worker reply must wait at least
# ``timeout_seconds + QUERY_WAIT_GRACE_SECONDS`` so the worker's own timeout
# envelope arrives before the caller force-stops it.
QUERY_WAIT_GRACE_SECONDS = 60
# Session-level worker spawn cap (safe default).
MAX_WORKERS_PER_SESSION = 3
# Publisher-side heartbeat cadence (worker.py throttles heartbeats to this).
HEARTBEAT_INTERVAL_S = 30
# A worker whose last heartbeat is older than this is considered stale.
HEARTBEAT_STALE_AFTER_S = 600
# Default per-worker container budget (aligns with
# infra.container_registry.DEFAULT_MAX_CONTAINERS).
WORKER_DEFAULT_MAX_CONTAINERS = 4
# Default system prompt for worker sub-agents.
DEFAULT_WORKER_SYSTEM_PROMPT = (
    "You are a capable autonomous sub-agent of ThoughtMachine. "
    "Complete the task given to you thoroughly, using all available tools. "
    "Think, research, write, edit, test, review. "
    "When finished, use the Respond tool to return your final result. "
    "Be concise but complete. "
    "Do not ask the user for clarification — the main agent already understood the request."
)
# Default worker LLM sampling temperature.
WORKER_DEFAULT_TEMPERATURE = 0.7

# ── tools/workspace/worker_lifecycle.py ──────────────────────────────────────
# Lifecycle event types the observer subscribes to (Phase 2A).
WORKER_LIFECYCLE_EVENT_TYPES = (
    "worker_spawned",
    "worker_status",
    "worker_running",
    "worker_heartbeat",
    "worker_stopping",
    "worker_completed",
    "worker_error",
    "worker_timeout",
    "worker_partial_result",
)
# Per-worker ring buffer size and global ring buffer size.
PER_WORKER_RING_SIZE = 50
GLOBAL_RING_SIZE = 500
# Extra time past staleness before a worker is reported as "hung". 0 means a
# stale worker is immediately hung.
WORKER_HUNG_GRACE_S = 0

# ── tools/workspace/job_registry.py ──────────────────────────────────────────
# Maximum number of job records kept (bounded; oldest evicted when full).
JOB_REGISTRY_MAX_JOBS = 200
# Preview cap for completed-job results (full envelope stored separately).
PREVIEW_CAP = 8000
# Preview cap for partial/error/timeout snippets.
PARTIAL_PREVIEW_CAP = 2000
# Statuses that are final: once a job reaches one of these it must never
# transition again (no overwriting a paused/timeout/completed job).
TERMINAL_STATUSES = ("completed", "paused", "timeout", "error", "stopped", "interrupted")

# ── tools/host_bash_tool.py ──────────────────────────────────────────────────
# Execution timeout for the spawned shell (seconds).
HOST_BASH_TIMEOUT = 120
# How long to wait for the operator's approval decision (seconds).
HOST_BASH_APPROVAL_TIMEOUT = 120.0

# ── tools/git_info_tool.py ───────────────────────────────────────────────────
# Clone URL protocol allowlist. ``git clone`` accepts arbitrary transport URLs
# (including ``ext::`` shell executors and ``file://`` local access), so clone
# URLs are restricted to these schemes plus scp-like ``user@host:path`` syntax.
ALLOWED_GIT_PROTOCOLS = ["https://", "http://", "git://", "ssh://"]

# ── infra/workspace_lifecycle_manager.py ─────────────────────────────────────
# Timeouts (seconds).
SOFT_TIMEOUT = 300    # default per-query wait bound
HARD_TIMEOUT = 600    # upper bound for the underlying worker loop
EXEC_KILL_GRACE = 10  # grace period for terminating a docker exec
QUERY_ID_PREFIX = "q_"

# ── infra/container_registry.py ──────────────────────────────────────────────
# The resource git image tag (mirrored from infra/resource_container_manager).
RESOURCE_IMAGE_TAG = "tm-resource-git"
# Resource-container identity labels.
RESOURCE_LABEL = "thoughtmachine.resource"
RESOURCE_KIND = "git"
WORKSPACE_ID_LABEL = "thoughtmachine.workspace_id"
CONTAINER_NAME_LABEL = "thoughtmachine.container_name"
RESOURCE_MEM_LIMIT = "512m"
RESOURCE_CPU_QUOTA = 50000
# Default image used by container_manager.
DEFAULT_IMAGE = "agent-executor"
# Workspace config.json default max_containers (container_manager L228/243).
DEFAULT_MAX_CONTAINERS = 4
# Valid container_type values ("git" is represented as "resource"; mcp/proxy
# are future types reserved by the registry).
CONTAINER_TYPES = ("user", "resource", "mcp", "proxy")
# OOM score defaults by container type (None sentinel in ContainerProfile):
#   user     -> 1000  first OOM-kill victims (container_manager L523)
#   resource ->  500  moderate score (resource_container_manager L536)
DEFAULT_USER_OOM_SCORE_ADJ = 1000
DEFAULT_RESOURCE_OOM_SCORE_ADJ = 500
# Hardening recipe — the union of the three create stacks (design doc §1.1).
HARDENED_CAP_DROP = ["ALL"]
HARDENED_SECURITY_OPT = ["no-new-privileges:true"]
HARDENED_READ_ONLY = True
HARDENED_USER = "1000:1000"
# tmpfs recipe (design doc §1.1; dispatch spelling "256m").
DEFAULT_TMPFS = {
    "/tmp": "rw,noexec,nosuid,size=64m",
    "/home/agent": "rw,exec,size=256m,uid=1000,gid=1000",
}
DEFAULT_COMMAND = ["tail", "-f", "/dev/null"]
DEFAULT_MEM_LIMIT = "1g"
DEFAULT_CPU_QUOTA = 100000
# Graceful-stop timeout used by destroy_container / permission reconciliation.
STOP_TIMEOUT = 10

# ── infra/container_manager.py ───────────────────────────────────────────────
# Output truncation for exec streams (mirrors DockerCodeRunner._truncate_output).
EXEC_OUTPUT_LIMIT_BYTES = 100 * 1024

# ── infra/resource_container_manager.py ──────────────────────────────────────
RUNTIME_IMAGE_TAG = "tm-workspace-runtime:latest"
# Known hidden resources. Every entry runs inside the same hardened
# ``RESOURCE_IMAGE_TAG`` image, so the build-hash drift check applies to all.
RESOURCE_REGISTRY = {
    "git": {"kind": "git", "permission": "git"},
}
# Docker label carrying the image build hash for drift detection.
RESOURCE_BUILD_HASH_LABEL = "thoughtmachine.build_hash"

# ── security/security_gate.py ────────────────────────────────────────────────
# How long check_required_categories waits for the user's approve/deny decision.
PROMPT_TIMEOUT = 120.0

# ── agent/core/agent.py ──────────────────────────────────────────────────────
# Default response-token budget for the Agent class.
DEFAULT_RESPONSE_TOKENS = 4096

# ── agent/core/tool_executor.py ──────────────────────────────────────────────
# Fallback session-permissions profile (seven categories) used when no live
# SessionPermissions model is available on config.
DEFAULT_SESSION_PERMISSIONS = {
    "container": False,
    "network": "banned",
    "filesystem": "read",
    "system": "read",
    "git": "read",
    "mcp": "banned",
    "execution": "banned",
}


