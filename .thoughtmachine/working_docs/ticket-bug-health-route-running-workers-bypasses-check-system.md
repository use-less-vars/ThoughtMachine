# Ticket: bug/health-route-running-workers-bypasses-check-system

**Filed:** Gen 44, 2026-10-15, from field agent probe (workspace lively-bear-27).
**Severity:** Medium — cross-workspace disclosure of worker task text, via a route with no caller-scope check and no auth dependency.
**Status:** OPEN.

## Bearing

`GET /api/system/health` does not read running workers through the tool layer. It reimplements the collection by walking the filesystem itself — `web_ui/backend/health_routes.py:26 _collect_workers_from_fs()` — while the tool path is `tools/workspace/check_system.py:356` (`"running_workers": lambda: self._query_running_workers()`) → `:796-816 _query_running_workers()`.

Two consequences, one structural, one concrete:

- **Structural:** the route owns its own scope rules, so it cannot inherit any scoping later applied to the query. Today it has no scope at all.
- **Concrete:** it returns each worker's `current_task` — the stored prompt/task text, verbatim — for **every workspace on the machine**, to any caller that can reach the port.

## Evidence

Route surface:
- `health_routes.py:19` — `router = APIRouter(prefix="/api/system")`.
- `health_routes.py:182` — `@router.get("/health")`; `:183` — `async def system_health(workspace: Optional[str] = Query(None))`.
- Response assembled `:225-232`: keys `running_workers`, `active_sessions`, `recent_event_log_tail` (`{entries, note}`).
- Mounted with no guard: `web_ui/backend/server.py:3037 app.include_router(health_router)` (import at `:158`) — no `dependencies=`, no auth middleware on this router.
- No auth anywhere in `health_routes.py`: the only FastAPI imports are `APIRouter, Query` (`:15`). No `Depends(...)`, no API-key check, no principal/session lookup in the file.

Collector:
- `:23` — `_WORKSPACES_DIR = Path.home() / ".thoughtmachine" / "workspaces"`.
- `:40` — `for ws_dir in _WORKSPACES_DIR.iterdir()`: every workspace directory on the box, unconditionally.
- `:44-52` — display path taken from each workspace's `config.json` `root`.
- Two layouts handled: session-scoped `workers/<session_id>/<name>/status.json` (`:64-88`) and legacy `workers/<name>/status.json` (`:88-107`).

Fields returned per worker (session-scoped `:77-87`, legacy `:96-106`):
`worker_name`, `session_id`, `workspace_path`, `status` (= status.json `runtime_status`), **`current_task`** (`:82` and `:101` — `data.get("current_task")`, returned verbatim: no truncation, no redaction), `last_heartbeat`, `error`, `current_context_tokens`, `max_context_tokens`.

Scope of the filtering:
- The only filter is an optional substring match on `workspace_path` (`:193-198`), applied **only** when the caller passes `?workspace=`.
- With no query parameter — the default, and the documented usage — every workspace's workers are returned, including foreign sessions.
- There is **no** caller-session check, **no** `session_id == caller` comparison, **no** `workspace_id` ownership check, **no** grain/permission check anywhere in `health_routes.py`.

Second channel in the same response:
- `active_sessions`, built by `_collect_sessions_from_store()` — `session_id`, `workspace`, `created_at`, `last_activity`, `mode`; unfiltered unless `?workspace=` (`:200-206`). No session titles, no content, but it is a second unfiltered cross-workspace enumeration and should be fixed in the same pass.

Contrast, and a provenance caveat:
- `tools/workspace/check_system.py:796-816 _query_running_workers()` returns `{"running_workers": [...], "count": n}` with name / session_id / status / alive / current_task / last_heartbeat / error / conversation_length / elapsed_seconds.
- Per commit `254dbfbc` on branch `fix/running-workers-session-scoped`, that query was made session-scoped. **That branch is not the checkout this ticket was filed against** (`fix/global-kb-write-gate`), and in that checkout `_query_running_workers()` still carries no session filter. Do not cite the scoped version's line numbers against this tree, and do not assume the tool path is already safe — confirm which branch lands before calling this fixed.

## Why it matters

- `current_task` is user- and agent-authored text: usually the actual instruction ("migrate tenant X's schema", "review the Acme contract"), not a label. That is content disclosure — one class past the metadata-only disclosure in `ticket-bug-checksystem-vault-status-cross-tenant-leak.md`, which was rated Medium on titles and filenames alone.
- `session_id` + `workspace_path` + `current_task` together are a complete who-is-doing-what map of the machine, keyed to real directory paths.
- Reachability: `server.py:5252-5254` defaults `--host` to `os.environ.get("HOST", "127.0.0.1")`; the module docstring `:25` documents the `0.0.0.0` override. On loopback the exposure is "any local process or user", which on a shared machine is still cross-tenant. The only cross-cutting middleware is permissive CORS — `allow_origins=["*"]` with `allow_credentials=True` (`server.py:1100-1105`) — so a browser page can read the response.
- **Not known:** whether the operator deployment overrides `HOST` to a non-loopback address. The repo default says loopback; that is not the same as saying the deployed service is loopback-only.

Unknowns to state rather than assume: whether another component deletes `status.json` dirs after a worker exits (see staleness below); whether other UI surfaces consume `_collect_workers_from_fs()`; whether the frontend ever populates `?workspace=` in real traffic. This ticket swept `health_routes.py` in full plus its mount point and bind in `server.py` — nothing else.

Staleness, a separate defect in the same collector:
- `_collect_workers_from_fs()` returns every `status.json` it finds. There is no mtime check, no `last_heartbeat` age comparison, and no liveness probe in this file, so a dead worker's last `current_task` and `error` keep being served as if live. Whether the dirs are reaped elsewhere is not established here — do not assume cleanup happens.

## Fix sketch

No patch proposed. Two candidate directions — pick one deliberately, do not do both:

- **(a) Delegate.** `/api/system/health` calls the CheckSystem `running_workers` query instead of walking `_WORKSPACES_DIR`. One source of truth; the route inherits whatever scope the query enforces. Requires the scoped query to be on the branch that actually ships (see provenance caveat) — otherwise this moves the leak rather than closing it.
- **(b) Filter in place.** Keep the fs collector (it is cheap and has no tool-layer dependency, which is the reason it exists) but apply the same caller-session scope the tool path applies, and treat "no caller session" as "own workspace only" rather than "all workspaces".

Either way:
- `current_task` should not be returned verbatim to a caller that does not own the session — omit or truncate on the cross-workspace path.
- Fix `active_sessions` in the same pass; it is the same missing check on a second key.
- Decide staleness explicitly (drop, or flag with an age field) rather than leaving "every file found looks live".

## Verification

1. From workspace A, `GET /api/system/health` with no query parameter returns **only** A's workers.
2. Negative test: workspace B runs a worker whose `current_task` is a unique marker string; the raw response body from A does not contain that string, and B's `session_id` does not appear.
3. `active_sessions` in A's response lists only A's sessions.
4. `?workspace=<foreign-root>` supplied by a non-privileged caller does not widen the response (no escalation via the query parameter) — either it is ignored or it is rejected, but it must not filter *in* foreign rows.
5. `CheckSystem query='running_workers'` and the route return the same worker set for the same caller — proves the single-source rule holds, or documents a deliberate divergence.
6. Staleness: a `status.json` with an old `last_heartbeat` is not reported as live (or is explicitly flagged with its age). Assert whichever the fix chose.

## MUST NOT TOUCH
Same list as the container tickets.
