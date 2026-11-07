# WLM Completion — Architecture Notes

> **Date:** 2026-08-19
> **Scope:** One-document architecture record of the Worker Lifecycle Management (WLM) program delivered on `dev`, built on the Phase 1 safe-foundation baseline commit `d1896a9`.

## 1. WLM Vision

Make background workers first-class, observable, and bounded resources:

- **Observability** — every worker lifecycle transition is published as a structured `WORKER_*` event on the shared event bus, and a `WorkerLifecycleObserver` derives staleness/hung state from the heartbeat stream.
- **Decoupling** — submission is non-blocking: `submit_query` returns immediately with a `job_id`; results arrive asynchronously and are tracked in a job registry.
- **Bounding** — per-worker ceilings on containers, runtime, and context tokens, enforced fail-closed (budget breach terminates the query and emits `worker_error`).
- **Safety** — permissions are composed as a ceiling (min of session and workspace capabilities, fail-closed when unknown); worker containers are ownership-labeled and garbage-collected on a TTL sweep.
- **Transparency** — backend stats routes surface worker liveness (`time_since_last_query`) and container usage (`containers_in_use` / `containers_available`).

## 2. Phase Summaries

| Phase | Scope | Status |
|---|---|---|
| Phase 1 — safe foundation (baseline) | Baseline merge `d1896a9` ("Merge branch `feat/wlm-phase1-safe-foundation` into dev", 2026-08-18). Pre-existing worker event types (`worker_spawned` / `worker_status` / `worker_completed` / `worker_error` / `worker_message`), blocking `Worker` query loop, basic `list/spawn/check/query/stop` actions. | Done (baseline) |
| Phase 2A — lifecycle events + heartbeat | New event types `WORKER_RUNNING`, `WORKER_STOPPING`, `WORKER_HEARTBEAT`, `WORKER_PARTIAL_RESULT`, `WORKER_TIMEOUT` in `agent/events.py`; `tools/workspace/worker_lifecycle.py` observer with heartbeat tracking, staleness, and hung detection. | Done |
| Phase 2B — non-blocking `submit_query` + job registry | `_action_submit_query` returns `{worker_name, job_id, status: "submitted", note}`; `_action_job_status` reads `WorkerJobRegistry` (`tools/workspace/job_registry.py`); new tool actions `submit_query`, `job_status`. | Done |
| Phase 2C — permission ceiling + config hardening | Fail-closed capability gate (`security_gate.py`), write→read downgrade, min-permission composition for network/container/git; `Worker` tool schema (`action`, `worker_name`, `worker_query`, `context`) with `required_categories=[]` (worker execution not gated by the session `execution` permission, 2026-08-03); lazy `Agent` circular-import fix (per program briefing; not separately re-verified). | Done |
| Phase 3 — container GC, budgets, backend stats | TTL sweep + orphan GC for exited workspace containers with server startup hook; hung-worker termination via `WORKER_TIMEOUT`; per-worker runtime/token budgets; backend worker/container stats fields. | Done |

## 3. Lifecycle Event Types

All events share `EventMetadata` (`event_id`, `timestamp`, `source`, optional `session_id`, optional `turn`) and `BaseEvent` (`type` / `metadata` / `data`; `to_dict()` flattens). Per-type classes validate `data` (`validate_data`); most require `worker_name`, and `worker_error` additionally requires `error`. `create_event(event_type, data, source='unknown', session_id=None, turn=None)`.

| EventType member | String value | Required / notable `data` | Phase |
|---|---|---|---|
| `WORKER_SPAWNED` | `worker_spawned` | `worker_name` | 1 |
| `WORKER_STATUS` | `worker_status` | `worker_name`, status | 1 |
| `WORKER_COMPLETED` | `worker_completed` | `worker_name`; marks terminal in observer | 1 |
| `WORKER_ERROR` | `worker_error` | `worker_name` + `error` | 1 |
| `WORKER_MESSAGE` | `worker_message` | `worker_name` | 1 |
| `WORKER_RUNNING` | `worker_running` | `worker_name` | 2A |
| `WORKER_STOPPING` | `worker_stopping` | `worker_name` | 2A |
| `WORKER_HEARTBEAT` | `worker_heartbeat` | `worker_name`; refreshes `_last_heartbeat`, clears stale/hung/terminal flags | 2A |
| `WORKER_PARTIAL_RESULT` | `worker_partial_result` | `worker_name`; mid-flight, turn-level result on success path | 2A |
| `WORKER_TIMEOUT` | `worker_timeout` | `worker_name`, `worker_id`, `reason: "stale_heartbeat"`, `last_heartbeat`, `heartbeat_age_seconds` (rounded 0.1), `session_id`, `source: "worker_lifecycle_observer"` | 2A |

The lifecycle observer subscribes to `WORKER_LIFECYCLE_EVENT_TYPES = (worker_spawned, worker_status, worker_running, worker_heartbeat, worker_stopping, worker_completed, worker_error, worker_timeout, worker_partial_result)` — 9 of the 10 worker events; `worker_message` is not tracked.

## 4. Job Registry Mechanism

`WorkerJobRegistry(event_bus=None)` in `tools/workspace/job_registry.py`:

- Constants: `JOB_REGISTRY_MAX_JOBS=200`, `PREVIEW_CAP=8000`, `PARTIAL_PREVIEW_CAP=2000`; subscribed events `_JOB_EVENT_TYPES = (worker_running, worker_partial_result, worker_completed, worker_timeout, worker_error)`.
- API: `register(job_id, worker_name, session_id=None)` (record starts `status="submitted"`, returns a deep copy), `update(job_id, **fields)` (whitelist: `worker_name` / `session_id` / `status` / `completed_at` / `preview`), `complete(job_id, envelope)` (creates record on demand; `status="completed"`; full envelope stored in `result`; `preview = content[:8000]`), `job(job_id)`, `jobs(worker_name=None, status=None)`.
- Record fields: `job_id`, `worker_name`, `session_id`, `status` (`submitted` / `running` / `partial` / `completed` / `timeout` / `error`), `created_at`, `updated_at`, `completed_at`, `preview`, `result`.
- Mechanics: `RLock`-protected in-memory dict, evicts oldest by `created_at` at capacity, process-wide singleton `_get_worker_job_registry()`, never raises.
- Consumers: `Worker._action_submit_query` (job_id = `uuid.uuid4().hex`, enqueues `(job_id, query, None)` on `thread._input_queue`, then `registry.register(...)`); `Worker._action_job_status` (single job → `{worker_name, job_id, status, created_at, updated_at, completed_at, has_result, preview}`; empty `worker_query` → `{worker_name, jobs, count}`); `WorkerThread` completes records on the success path via `registry.complete(job_id, envelope)` (best-effort).

## 5. Heartbeat and Staleness

- Constants (shared by `worker.py` and `worker_lifecycle.py`): `HEARTBEAT_INTERVAL_S=30`, `HEARTBEAT_STALE_AFTER_S=600` (alias `STALE_AFTER_S`), `WORKER_HUNG_GRACE_S=0` (a stale worker is immediately considered hung).
- `WorkerThread` publishes `WORKER_HEARTBEAT` while idle/paused/busy, throttled to 30s.
- `WorkerLifecycleObserver(event_bus=None, stale_after_s=HEARTBEAT_STALE_AFTER_S, *, stale_callback=None, hung_grace_s=WORKER_HUNG_GRACE_S)`: idempotent `ensure_subscribed()`; `staleness(worker_name)`, `last_heartbeat(worker_name)`, `recent_events(worker_name=None)` (per-worker ring 50, global ring 500); `check_stale_transitions(now=None)` returns the count of newly flagged stale workers.
- Hung emission (at most once per worker, guarded by `_hung_notified`): publishes `WORKER_TIMEOUT` (data as in §3) and invokes `stale_callback(worker, info)`. Synthetic `worker_stale` records carry `source="worker_lifecycle_observer"`. `worker_completed` / `worker_error` mark the worker terminal and stop staleness tracking.

## 6. Permission Ceiling Rules

- **Fail closed:** `security_gate.get_workspace_capabilities` returns `_FAIL_CLOSED_CAPABILITIES` (`filesystem_write=False, allow_docker=False, allow_network=False, git_available=False`) when the stored capabilities load to `None`.
- **Write→read downgrade:** `get_effective_permissions` downgrades a write-grade filesystem permission to read-only before execution.
- **Min-permission composition:** effective network = `min(session.network, workspace.allow_network)`; effective container = `session.container AND workspace.allow_docker`; effective git = `min(session.git, workspace.git_available)`.
- **Defaults:** `WorkspaceCapabilities` permissive defaults (`allow_network=True, allow_docker=True, filesystem_write=True, git_available=True, allowed_workspace_dirs=['.']`), persisted at `<vault>/workspaces/<id>/capabilities.json`; the workspace setup path (`web_ui/backend/workspace_routes.py`) bootstraps with `WorkspaceCapabilities.default()` when absent.
- **Worker exemption:** `Worker.required_categories=[]` — worker execution is **not** gated by the session `execution` permission (2026-08-03); the docker bridge is configured only when the effective network permission is `True` or the permission is write-grade.

## 7. Container Cleanup Policy

`sweep_exited_workspace_containers(registered_workspace_ids=None, max_age_s=3600, dry_run=False)` in `infra/container_manager.py`:

- Predicate: container `status == "exited"` AND idle age (`State.FinishedAt` vs now) ≥ `max_age_s`. Skips: resource containers (`thoughtmachine.resource` label, `tm-res-*` names), containers without the workspace label, non-exited containers, and containers whose `FinishedAt` is missing, unparseable, or in the future (clock-skew safety).
- `registered_workspace_ids=None` → TTL-only sweep (orphan classification disabled); `registered_workspace_ids=[]` → conservative no-op (`registry empty; sweep skipped`); `dry_run=True` counts but never removes.
- Result dict: `removed`, `skipped`, `detail`, `dry_run`, `removed_registered`, `removed_orphan`, `removed_containers` (names).
- **Server startup hook:** `web_ui/backend/server.py` — `_EXITED_CONTAINER_SWEEP_MAX_AGE_S = int(os.environ.get('THOUGHTMACHINE_EXITED_CONTAINER_MAX_AGE_S', '3600'))`; the lifespan startup block calls `_sweep_exited_workspace_containers()`, which passes `WorkspaceRegistry` ids; on registry failure `ids=None` → TTL-only, never an orphan wipe.
- **Ownership label:** `thoughtmachine.worker = "<session_id or 'unknown'>:<worker_name>"` — stamped only on fresh container create; reuse paths never re-label (known gap). Worker teardown only touches containers owned by the exact label value; resource containers are never touched.

## 8. Backend Stats Fields

- `GET /api/workspace/{ws_id}/containers` → `containers_in_use` and `containers_available` (`cap − in_use`, clamped ≥ 0). Cap resolution: session `container_limits.max_containers` → workspace `config.json` (`max_containers`, default 4) → default; clamped to ≥ 1.
- `GET /api/workspace/{ws_id}/workers` → per-worker:
  - `time_since_last_query` — seconds since `last_heartbeat` (`_seconds_since_heartbeat`); `None` when absent/unparseable.
  - `pruned_since_last_query` — `int(ctx.get('pruned_since_last_query') or 0)` read from `context.json` (session-scoped `workers/<name>/context.json` and legacy dirs; max of sources; default 0).
- Websocket `workspace_capabilities` message sends `load_workspace_capabilities(workspace_id)` (or `WorkspaceCapabilities.default()` when `None`) as `capabilities.to_dict()`.

## 9. Test Coverage

| Test file | Lines | Tests |
|---|---|---|
| `tests/test_worker_heartbeat.py` | 146 | 4 |
| `tests/test_worker_lifecycle_events.py` | 161 | 7 |
| `tests/test_worker_jobs.py` | 452 | 9 |
| `tests/test_worker_permission_hardening.py` | 1119 | 63 |
| `tests/test_container_sweep.py` | 193 | 11 |
| `tests/test_worker_budgets.py` | 192 | 7 |
| `tests/test_worker_hung_termination.py` | 263 | 8 |
| `web_ui/backend/tests/test_worker_stats_routes.py` | 326 | 14 |
| `tests/integration/test_server_health.py` | 416 | 13 |
| `tests/docker/test_container_lifecycle.py` | 780 | 19 |

Note: `tests/test_worker_stats_routes.py` at the repo root is absent; stats-routes coverage lives only under `web_ui/backend/tests/`.

## 10. Known Gaps

- **Container cap is per-supervisor, not global:** `WorkerSupervisor._max_container_count` bounds one worker's active containers; `request_container` raises `RuntimeError('Worker container limit reached (N)')` before delegating (fail closed), but there is no global per-session cap across workers.
- **Reused containers never re-labeled:** `thoughtmachine.worker` ownership is stamped only on fresh create; a container adopted via the reuse path may not carry the worker label, so teardown/GC may not attribute it.
- **Job registry is in-memory only:** records live in an `RLock`-protected dict with eviction at `JOB_REGISTRY_MAX_JOBS=200`; jobs and results are lost on process restart (no persistence).
- **Verification caveats:** `DEFAULT_MAX_CONTAINERS` (assumed 4, imported from `infra.container_registry`) was inferred from comments, not read directly; the `WorkerSupervisor` integration wiring call site was not located; permissive caps bootstrap lives in `web_ui/backend/workspace_routes.py` + `WorkspaceCapabilities.default()`, not in `infra/workspace_lifecycle_manager.py` (which contains no capabilities code); the Phase 2C lazy `Agent` circular-import fix is from the program briefing and was not separately re-verified.

## 11. Source-Control Commit Policy

Policy (per program briefing — delivered as one large uncommitted change set on `dev` after `d1896a9`):

- **Split host-side into phase-scoped conventional commits** (never a single mega-commit):
  - `feat(worker): WLM Phase 2A lifecycle events and heartbeat`
  - `feat(worker): WLM Phase 2B non-blocking submit_query and job registry`
  - `feat(worker): WLM Phase 2C permission ceiling and config hardening`
  - `feat(worker): WLM Phase 3 container GC, budgets, and backend stats`
  - `chore(worker): WLM completion remaining integration updates`
- **No in-container git:** commits are hard-blocked inside containers; all commit work happens host-side.
- **Hooks intact:** never `--no-verify`; keep pre-commit hooks enabled.
- **Hygiene:** the trailing blank-line EOF warning on `infra/container_manager.py` is fixed before committing.

Observation: HEAD history already contains commits with exactly these subjects (`3f895f3`, `3a814d6`, `6ba1cd3`, `fa0f1c1`, `980eac4`), consistent with this policy.
