# Workspace Isolation Matrix

**Status:** design pass — document only. This is **not** a gate, it **blocks nothing**, and it changes no code.
**What it is:** the evidence brief for the red-team pass over workspace isolation. It records, per entry point, which of the six facts the entry point can reach and whether that reach is bounded by the caller's workspace.

> **Reading note.** §1–§3 state the invariant, the two-class split and the escalation trigger this survey was run against. Everything in §4 onward is first-hand: every `path:line` and every test name was read out of this tree in this session, or is footnoted (†) as ticket-sourced.

---

## §0 Purpose and scope

This document exists to answer one question per entry point: **when a caller bound to workspace A invokes this entry point, can the invocation touch a fact that belongs to workspace B?**

- It is **not** an implementation plan. It prescribes no patch; §7 records defects and stops.
- It is **not** a gate. Nothing here passes or fails, and nothing here may be cited to block a merge.
- It does **not** block the Credential work. The credential surface is enumerated only where it is also a workspace-isolation entry point.
- A violation found here is **RECORDED, not fixed**. Any row tagged `RECORDED, NOT FIXED` stays unfixed by construction of this task; fixing it is a separate dispatch.

Scope of the survey is the entry-point surface that a *caller with a workspace identity* can reach:

1. WebSocket method dispatch in `web_ui/backend/server.py` (`websocket_endpoint`, `web_ui/backend/server.py:1231`).
2. HTTP route declarations in `web_ui/backend/` (FastAPI `@app.*` in `server.py` plus the ten `APIRouter` modules wired at `web_ui/backend/server.py:3615-3627`).
3. Agent tool definitions under `tools/`.

---

## §1 Invariant

**Workspace isolation is scoped by construction, never by key naming.** The store refuses on scope mismatch. The dispatch layer passes the socket's workspace. The route layer scopes the connection's workspace. No site trusts a client's claim of `workspace_id`. Every entry point × fact pair names its owner site and its verification test.

---

## §2 Two-class table

| Class | Actor | Status | Enforcement |
|---|---|---|---|
| agent-visible (CheckSystem event_log, running_workers, vault_status IDs, KB write) | the agent calls a tool and gets foreign data | **Security. Real breach today.** | **Security invariant. Violation blocks merge** |
| client-claim (delete_session, load_session, list_sessions, security_response) | a client — today the operator's browser, tomorrow maybe a second one | **Correctness. Data-loss bug, not attacker path.** | **Correctness invariant. Violation files a ticket, doesn't block** |

#### Current state of the four agent-visible claims (this pass, not the framing above)

This is *this document's* verification, not the class definition above. Two of the four may already be fixed.

- **`event_log`** — read side is session-filtered and pinned (`_query_event_log` `tools/workspace/check_system.py:1087`; `tests/test_event_log_scoped.py:82`, `:101`). Write side **does** stamp `session_id` (`agent/logging/event_logger.py:168`) but stamps `workspace_id` from an identical `getattr` (`:169`) on a model with no such field, so it is always `None` (`:165-167`). Session axis fixed, **workspace axis still open**.
- **`running_workers`** — **fixed** in `d45cebb03` (2026-10-21): scope taken from `self.session_id` and the registry filter fails closed (`tools/workspace/check_system.py:805-815`; `tests/test_check_system_running_workers_scoping.py:63`, `:83`).
- **`vault_status` IDs** — **fixed for the agent path**: the globally-built report is post-filtered to the caller's workspace (`_scope_vault_status_report` `tools/workspace/check_system.py:1216`, pinned by `tests/test_check_system_vault_status_scoping.py:61`, `:108`), with a declared global-allowed escape hatch (`_vault_status_global_allowed` `:1201`).
- **KB write** — global-scope writes are refused by class (`tools/knowledge_base.py:341-342`, `_global_write_refusal` `:377`) and the workspace root is resolved through the session→workspace registry (`:265-287`), **but** an unresolvable workspace falls back to `Path.cwd()` (`:289`). That fallback is the remaining hole.

---

## §3 Escalation trigger

The first PR that stores a secret behind the workspace boundary promotes client-claim from correctness to security. That is the event, not the calendar.

---

## §4 Methodology

### (a) How entry points were enumerated

Actual searches run against this tree in this session, with the result count that each produced:

| Lane | Tool call (pattern, target) | Hits |
|---|---|---|
| WS methods | `FileSearchTool` regex `command == "` / `command == '`, `web_ui/backend/server.py` | 27 (`:1334` … `:3544`) |
| HTTP routes, app-level | `FileSearchTool` regex `^@app\.(get\|post\|put\|delete\|route\|websocket)` over `web_ui/backend/` | 0 — **the anchor failed**; re-run without the anchor as `@router\.\|@app\.` and hit 83 |
| HTTP routes, router-level | `FileSearchTool` regex `add_get\|add_post\|add_put\|add_delete\|add_route\|@router\.\|@app\.` over `web_ui/backend/` | 83 (output truncated at 10 000 tokens; tail re-run per-file) |
| Router wiring | `FileSearchTool` regex `include_router\|APIRouter\(` over `web_ui/backend/` | 22 → 11 `APIRouter` prefixes + 11 `include_router` calls |
| Agent tools | `DirectoryTreeTool tools/ *.py` | 49 files (38 top-level + 11 under `tools/workspace/`) |
| CheckSystem owners | `FileSearchTool` literal `def `, `tools/workspace/check_system.py` | 33 `def`s |
| Event-log writer | `FileSearchTool` regex `def _on_event\|...\|workspace_id`, `agent/logging/event_logger.py` | 4 |
| Test names | `FileSearchTool` regex `def test_\w*(workspace_scope\|workspace_isolation\|cross_workspace\|cross_session\|not_scoped\|scope)\w*`, `tests/` | 34 |

Two of those searches are worth recording as method defects rather than wins:

- The `^@app\.` anchor returned **zero** although `@app.get("...")` is present in `server.py` — the anchor form is unreliable in this tool. Routes were instead enumerated with the unanchored form and then de-duplicated by file.
- `^class \w+\. *Tool` and `^class \w+` over `tools/` both returned **zero** hits, although `tools/` describes 38 tool modules. Tool classes are therefore **not** enumerable by class-declaration regex in this tree, which is why lane 5.3 has thin owner coverage (see §6(c)).

Route-count arithmetic used for the coverage table: 25 `@app.*` routes in `server.py` (plus the `@app.websocket("/ws")` counted in 5.1) + 25 in `workspace_routes.py` + 6 in `session_routes.py` + 3 in `config_routes.py` + 3 in `onboarding_routes.py` + 4 in `prompt_routes.py` + 3 in `provider_routes.py` + 2 in `logging_routes.py` + 2 in `vault_repair_routes.py` + 4 in `global_routes.py` + 1 in `health_routes.py` + 3 in `container_record_routes.py` = **81 HTTP routes**.

### (b) The six facts, and what scoping means for each

| Fact | What the fact is | What "scoped" means for it |
|---|---|---|
| **session** | A persisted conversation (metadata + message history) under `~/.thoughtmachine/workspaces/<ws_id>/sessions/`. | Every read, write, list, delete, rename and open/close resolves the session **inside the caller's own workspace directory**, and a client-supplied `session_id` or `workspace_id` is validated against that directory rather than trusted. |
| **event** | The in-process `EventBus` fan-out and its persisted log (`agent/events.py`, `agent/logging/event_logger.py`). | A subscriber receives only events produced by its own session; a publisher cannot address another session's subscriber; the agent-readable log tail is filtered to the caller's session. |
| **worker** | Worker threads/instances registered per workspace and session (`tools/workspace/worker_registry.py`, `worker_thread.py`), plus their liveness records. | Registration key carries the session; spawn/stop/reset/enumerate act only on the caller's own session's workers; a live-worker listing returned to an agent is filtered to the caller. |
| **KB** | The knowledge base(s) the agent reads and writes (`tools/knowledge_base.py`, `docs/KB_POLICY.md`). | A write lands in the caller's workspace scope; a read cannot address another workspace's KB path; the global KB is either read-only to agents or explicitly declared global. |
| **container** | Docker containers, their records, logs, resource edits and lifecycle actions (`infra/container_manager.py`, `infra/container_registry.py`). | Every container operation is keyed by `workspace_id` at the manager boundary, and the record/name index used to resolve a container is workspace-scoped. |
| **vault** | `~/.thoughtmachine/` itself: workspace dirs, session files, logs, prompts, registries, repair status. | An agent-reachable payload may describe the caller's own workspace slice only; global inventory is operator-only; a path handed to the server may not resolve into the vault, and a workspace path may not be re-pointed at another tenant's root. |

### (c) Status vocabulary

- **`scoped by construction`** — the mechanism enforces the scope: the key, path, or bound object cannot address a foreign fact without first failing. Removing the scope requires changing the data structure, not an argument.
- **`scoped by convention`** — the scope holds only because every current caller passes the right argument. The mechanism accepts a foreign or absent scope and does something plausible with it (defaults, falls through, "hint abandoned on miss").
- **`not scoped`** — the fact is reachable without any workspace identity in the request at all. This is the class that produces cross-tenant disclosure.
- **`N/A`** — reserved for a fact that genuinely does not apply to the entry point. **Not used in §5**: a row that would have been `N/A` was dropped instead, per the no-padding rule.
- **`UNVERIFIED`** — the entry point exists and is enumerated, but the adjudication was not completed (budget, or evidence insufficient). Per the task instruction: *stop adjudicating and say so; never guess.*

### (d) Two hard rules

1. **Owner site = a function name plus `path:line`.** A module, a file, a "layer", or a subsystem is **not** an owner. Where the only locatable owner is a layer, the row says `UNOWNED` and names the file, and §6(c) counts it.
2. **Verification = a test name that exists in this tree, or the literal `NONE`.** No test is inferred from a filename, a docstring, or a ticket. **`NONE` is itself a finding**: an entry point whose scoping is unpinned by any test can regress silently, and each `NONE` below is counted in the coverage table and re-listed in §7.

### (e) `UNOWNED`

`UNOWNED` means: the entry point is real and enumerable, but no single function could be identified as the site that decides its scoping. It is a finding about *legibility*, not about safety — an unowned entry point cannot be repaired by anyone who has not first re-derived its control flow.

### Coverage table

| Lane | Exists | Adjudicated | Enumerated, not adjudicated |
|---|---|---|---|
| 5.1 WebSocket methods (`server.py`) | 27 | 27 | 0 |
| 5.2 HTTP routes (`web_ui/backend/`) | 81 | 25 | 56 |
| 5.3 Agent tools (`tools/`) | 38 tool modules | 8 | 30 |
| **Total** | **146** | **60** | **86** |

The 56 un-adjudicated HTTP routes are the global/config/provider/prompt/logging/onboarding/system-health surface plus the workspace-config CRUD routes. They are enumerated (names and lines are in §5.2's stub list) but their adjudication was **stopped for budget**, not because they were judged clean. Per instruction they are recorded as enumerated-and-unexamined rather than guessed. **A red-team pass must not read their absence from the matrix as a pass.**

---

## §5 Matrix

Status vocabulary per §4(c). Verification is a test name from this tree or literal `NONE`. Rows marked † take their evidence from a filed ticket rather than from a first-hand read in this session (footnotes in §5.4).

### 5.1 WebSocket methods

One entry point per `command` string in the dispatch chain of `websocket_endpoint` (`web_ui/backend/server.py:1231`). Where the row's owner is the dispatch function itself, the branch line is given in parentheses; a branch is not a function and is never cited as an owner on its own.

| Entry point | Fact | Scoping status | Owner site | Verification |
|---|---|---|---|---|
| `start_session` (`:1334`) | session | scoped by construction — creates its own bridge and stores it under the connection's bridge, not under a client id | `websocket_endpoint` `web_ui/backend/server.py:1231` (branch `:1334`, bridge created `:1352`, stored `:1703`) | NONE |
| `start_session` (`:1334`) | worker | scoped by convention — worker events are subscribed per bridge, but see §6(a)/(b) for the key and the missing handle | `Bridge._subscribe_to_worker_events` `web_ui/backend/bridge.py:567` | `test_respawn_after_bus_replacement_still_delivers_tool_events` `tests/test_worker_bus_respawn_bridge.py:127` — **partial** (pins that a bridge receives its own session's `WORKER_SPAWNED` on the global bus and forwards that worker's per-worker bus; does not pin the session boundary) |
| `continue_session` (`:1378`) | session | scoped by construction — operates on `bridge`, which is the connection's own object | `websocket_endpoint` `web_ui/backend/server.py:1231` (branch `:1378`) | NONE |
| `pause_session` (`:1461`) | session | scoped by construction — `bridge.pause()` on the connection's own bridge | `websocket_endpoint` `web_ui/backend/server.py:1231` (branch `:1461`) | NONE |
| `resume_session` (`:1466`) | session | scoped by construction — `bridge.resume()` | `websocket_endpoint` `web_ui/backend/server.py:1231` (branch `:1466`) | NONE |
| `stop_session` (`:1471`) | worker | scoped by convention — `bridge.stop()` reaches `_unsubscribe_worker_events()`, which cannot detach what was never handed a handle (§6(b)) | `Bridge.stop` `web_ui/backend/bridge.py:1674` | NONE |
| `get_config` (`:1476`) | session | scoped by construction — reads `bridge._session_config` | `websocket_endpoint` `web_ui/backend/server.py:1231` (branch `:1476`) | NONE |
| `get_conversation` (`:1498`) | session | scoped by construction — `bridge.get_conversation()` | `websocket_endpoint` `web_ui/backend/server.py:1231` (branch `:1498`) | NONE |
| `apply_config` (`:1509`) | session | scoped by construction — merges into the connection's own session config | `websocket_endpoint` `web_ui/backend/server.py:1231` (branch `:1509`) | NONE |
| `set_default_config` (`:1966`) | vault | not scoped — writes the machine-global default config; no workspace identity in the write | `websocket_endpoint` `web_ui/backend/server.py:1231` (branch `:1966`) | NONE |
| `get_providers` (`:2047`) | vault | not scoped — provider profiles are machine-global by design; the reply attaches a session id for routing only | `websocket_endpoint` `web_ui/backend/server.py:1231` (branch `:2047`) | NONE |
| `save_provider` (`:2089`) | vault | not scoped — global credential/provider store | `websocket_endpoint` `web_ui/backend/server.py:1231` (branch `:2089`) | NONE |
| `delete_provider` (`:2147`) | vault | not scoped — global provider store, delete by client-supplied id | `websocket_endpoint` `web_ui/backend/server.py:1231` (branch `:2147`) | NONE |
| `get_available_tools` (`:2197`) | worker | not scoped — returns the static tool catalogue; no per-caller fact is read | `websocket_endpoint` `web_ui/backend/server.py:1231` (branch `:2197`) | NONE |
| `list_sessions` (`:2230`) | session | scoped by convention — the ATTACK-5 comment states the scope is the connection's own `bridge.workspace_id` and never a client value; the code, not a test, is what says so | `websocket_endpoint` `web_ui/backend/server.py:1231` (branch `:2230`) | NONE |
| `save_session` (`:2303`) | session | scoped by construction — saves `bridge.session` | `websocket_endpoint` `web_ui/backend/server.py:1231` (branch `:2303`) | NONE |
| `load_session` (`:2334`) | session | scoped by convention — `session_id` is client-supplied; the bridge is re-pointed and cached per session (`:2463`, `:2472`). Path-shaped store makes the *store* honest, the *pivot* a hint † | `websocket_endpoint` `web_ui/backend/server.py:1231` (branch `:2334`) | `test_caller_scope_refuses_foreign_record` `tests/test_bridge_load_session_workspace_scope.py:80`; `test_load_session_scoped_to_workspace` `tests/test_session_store.py:434` |
| `load_session` (`:2334`) | worker | scoped by convention — the re-pointed bridge can carry worker subscriptions from the previous session; early-out `bridge.py:853` prevents re-bind (§6(a)) | `Bridge._on_worker_spawned` `web_ui/backend/bridge.py:812` | `test_stop_and_respawn_without_completion_leaves_no_dead_bus_bound` `tests/test_worker_bus_respawn_bridge.py:239` — **partial**; `test_respawn_after_bus_replacement_still_delivers_tool_events` `:127` — **partial** (both pin `_on_worker_spawned`'s re-bind after a *bus* swap with the session id held fixed; neither exercises a session pivot) |
| `load_more_messages` (`:2657`) | session | scoped by construction — reads the loaded session through `bridge` | `websocket_endpoint` `web_ui/backend/server.py:1231` (branch `:2657`) | NONE |
| `delete_session` (`:2676`) | session | scoped by convention — takes a client `workspace_id` (`:2678`) alongside the `session_id`; the store can scope, but the command decides what to pass † | `websocket_endpoint` `web_ui/backend/server.py:1231` (branch `:2676`) | `test_delete_scoped_to_workspace_leaves_other_workspace_intact` `tests/test_session_store.py:274` |
| `rename_session` (`:2826`) | session | scoped by convention — client-supplied `session_id`, same shape as delete | `websocket_endpoint` `web_ui/backend/server.py:1231` (branch `:2826`) | NONE |
| `get_open_sessions` (`:2894`) | session | scoped by convention — reads `session_store.get_open_sessions()` with no scope argument at the call site | `websocket_endpoint` `web_ui/backend/server.py:1231` (branch `:2894`) | NONE |
| `close_session` (`:2916`) | session | scoped by convention — ownership gate documented in place (R8); the target is a client-supplied `session_id` | `websocket_endpoint` `web_ui/backend/server.py:1231` (branch `:2916`) | NONE |
| `new_session` (`:3027`) | vault | scoped by construction — the branch carries an explicit guard against re-scoping a bound socket, and the guard is test-pinned | `websocket_endpoint` `web_ui/backend/server.py:1231` (branch `:3027`) | `test_new_session_cannot_rescope_bound_socket` `tests/test_isolation_boundary_e2e.py:792`; `test_new_session_without_workspace_id_cannot_rescope` `tests/test_isolation_boundary_e2e.py:1072` |
| `set_project` (`:3215`) | vault | scoped by convention — the vault confinement of the new path is the deciding control, and it has been bypassed once † | `websocket_endpoint` `web_ui/backend/server.py:1231` (branch `:3215`) | `test_set_project_cannot_rescope_bound_socket` `tests/test_isolation_boundary_e2e.py:1006` |
| `security_response` (`:3442`) | event | scoped by convention — resolves a prompt by client-supplied `request_id`; correlation is the only scope † | `websocket_endpoint` `web_ui/backend/server.py:1231` (branch `:3442`) | NONE |
| `get_workspace_capabilities` (`:3494`) | vault | scoped by convention — takes `workspace_id` from the message, not from the connection | `websocket_endpoint` `web_ui/backend/server.py:1231` (branch `:3494`) | NONE |
| `bootstrap_workspace` (`:3522`) | vault | scoped by convention — takes `workspace_id` from the message | `websocket_endpoint` `web_ui/backend/server.py:1231` (branch `:3522`) | NONE |
| `rebuild_container` (`:3544`) | container | scoped by convention — takes `workspace` (a path) from the message and rebuilds | `websocket_endpoint` `web_ui/backend/server.py:1231` (branch `:3544`) | NONE |

### 5.2 HTTP routes

Adjudicated subset (25 of 81). **Un-adjudicated remainder is listed in §5.2b and counted in §4's coverage table — it is not clean, it is unexamined.**

| Entry point | Fact | Scoping status | Owner site | Verification |
|---|---|---|---|---|
| `GET /api/vault/status` (`web_ui/backend/server.py:3910`) | vault | scoped by convention — the route runs the drift check and returns a report; report scoping is delegated to `_scope_vault_status_report` | `_scope_vault_status_report` `tools/workspace/check_system.py:1216` | `test_default_scope_hides_foreign_workspace` `tests/test_check_system_vault_status_scoping.py:61`; `test_execute_scopes_report_to_caller_workspace` `tests/test_check_system_vault_status_scoping.py:108` |
| `POST /api/workspace/{ws_id}/workers/{name}/stop` (`workspace_routes.py:1261`) | worker | scoped by construction — `ws_id` is a path segment and the instance is resolved under that workspace's directory | `stop_worker` `web_ui/backend/workspace_routes.py:1262` | `test_stop_fallback_is_session_scoped` `tests/test_worker_cap.py:211` |
| `POST /api/workspace/{ws_id}/workers/{name}/pause` (`workspace_routes.py:1304`) | worker | scoped by construction — same path segment + instance resolution | `pause_worker` `web_ui/backend/workspace_routes.py:1305` | NONE |
| `POST /api/workspace/{ws_id}/workers/{name}/resume` (`workspace_routes.py:1416`) | worker | scoped by construction — same path segment + instance resolution | `resume_worker` `web_ui/backend/workspace_routes.py:1417` | NONE |
| `POST /api/workspace/{ws_id}/workers/stop_all` (`workspace_routes.py:1204`) | worker | scoped by construction — enumerates instances from the workspace's own on-disk layout | `stop_all_workers` `web_ui/backend/workspace_routes.py:1205` | NONE |
| `GET /api/workspace/{ws_id}/workers/active` (`workspace_routes.py:613`) | worker | scoped by convention — `ws_id` path segment, but the live-thread lookup is global and then filtered | `get_active_workers` `web_ui/backend/workspace_routes.py:614` | `test_get_active_workers_session_scoped` `tests/test_worker_crud.py:140` |
| `POST /api/session/create` (`session_routes.py:106`) | session | scoped by convention — `workspace_id` is an optional body field, so an omitted value lands somewhere † | `create_session` `web_ui/backend/session_routes.py:107` | NONE |
| `GET /api/session/list` (`session_routes.py:206`) | session | scoped by convention — `workspace_id` is an optional *query filter*; unfiltered means machine-wide | `list_sessions` `web_ui/backend/session_routes.py:207` | NONE |
| `GET /api/session/{session_id}` (`session_routes.py:260`) | session | scoped by convention — lookup by id alone, no workspace argument | `get_session` `web_ui/backend/session_routes.py:261` | NONE |
| `DELETE /api/session/{session_id}` (`session_routes.py:296`) | session | scoped by convention — `workspace_id` is an optional query parameter; the delete works without it † | `delete_session` `web_ui/backend/session_routes.py:297` | `test_delete_scoped_to_workspace_leaves_other_workspace_intact` `tests/test_session_store.py:274` |
| `GET /api/session/{session_id}/permissions` (`session_routes.py:429`) | session | scoped by convention — id-only lookup of a session's grant map | `get_session_permissions` `web_ui/backend/session_routes.py:430` | NONE |
| `PUT /api/session/{session_id}/permissions` (`session_routes.py:477`) | session | scoped by convention — id-only write of a session's raw grants | `put_session_permissions` `web_ui/backend/session_routes.py:478` | NONE |
| `GET /api/workspace/{ws_id}/containers` (`server.py:4661`) | container | scoped by construction — manager is built from the `workspace_id` path segment | `workspace_containers` `web_ui/backend/server.py:4662` | NONE |
| `POST .../{container_name}/start` (`server.py:4763`) | container | scoped by construction — same manager construction | `workspace_container_start` `web_ui/backend/server.py:4764` | NONE |
| `POST .../{container_name}/stop` (`server.py:4794`) | container | scoped by construction | `workspace_container_stop` `web_ui/backend/server.py:4795` | NONE |
| `DELETE .../{container_name}` (`server.py:4836`) | container | scoped by construction | `workspace_container_delete` `web_ui/backend/server.py:4837` | NONE |
| `GET .../{container_name}/logs` (`server.py:5640`) | container | scoped by construction | `workspace_container_logs` `web_ui/backend/server.py:5641` | NONE |
| `POST /api/container-records/{record_id}/kill` (`server.py:5178`) | container | scoped by convention — `workspace_id` is an optional *query* argument to a record-id action | `_record_user_action` `web_ui/backend/server.py:5179` | NONE |
| `POST /api/container-records/{record_id}/restart` (`server.py:5186`) | container | scoped by convention — same optional `workspace_id` | `_record_user_action` `web_ui/backend/server.py:5187` | NONE |
| `POST /api/container-records/{record_id}/recreate` (`server.py:5194`) | container | scoped by convention — same optional `workspace_id` | `_record_user_action` `web_ui/backend/server.py:5195` | NONE |
| `GET /api/container-records` (`container_record_routes.py:130`) | container | scoped by convention — record listing; scope is whatever the manager was constructed with | UNOWNED (file-level: `web_ui/backend/container_record_routes.py`) | NONE |
| `GET /api/system/health` (`health_routes.py:182`) | worker | scoped by convention — the health payload carries worker status; session scoping is a query concern † | UNOWNED (file-level: `web_ui/backend/health_routes.py`) | `test_session_scoped_worker_status` `tests/test_health_endpoint.py:111`; `test_session_scoped_fallback_session_id` `tests/test_health_endpoint.py:137` |
| `GET /health` (`server.py:3892`) | worker | scoped by convention — the route is gated to localhost, which is a transport control, not a workspace control † | `health` `web_ui/backend/server.py:3893` | NONE |
| `GET /api/global/summary` (`global_routes.py:322`) | vault | not scoped — one-shot snapshot of *registered workspaces, open sessions and containers*, i.e. all of them | UNOWNED (file-level: `web_ui/backend/global_routes.py`) | NONE |
| `GET /api/credentials` (`global_routes.py:390`) | vault | not scoped — global credential names; names only, never values | `list_credentials` `web_ui/backend/global_routes.py:391` | NONE |

**5.2b Enumerated, not adjudicated (56 routes).** `server.py`: `/api/browse` `:3705`, `/api/tools` `:3742`, `/api/browse/create` `:3784`, `/api/user-home` `:3825`, `/api/health` `:3904`, `/api/resource-catalog` `:3968`, `/api/permission-defaults` `:3991`, `/api/container/integrity` `:4013`, `/api/container/status` `:4062`, `.../containers/{name}/status` `:4719`, `.../containers/{record_id}/action` `:5237`, `.../resources` `:5432`, `/api/health/containers` `:5698`, `/` `:5817`. `workspace_routes.py`: `/resolve` `:113`, `/list` `:191`, `/templates` `:214`, `/{ws}/dockerfile` GET `:362` PUT `:903`, `/{ws}/domain_allowlist` GET `:376` PUT `:391`, `/{ws}/workers` GET `:430` POST `:801`, `/{ws}/workers/{name}` PUT `:837` DELETE `:877`, `/{ws}/workers/blueprints` `:718`, `.../blueprints/{name}` GET `:725` PATCH `:738`, `/{ws}/mcp_servers` `:919`, `/{ws}/effective_permissions` `:934`, `/{ws}/permissions` GET `:1615` PUT `:1647`, `POST ""` (create workspace) `:1699`, `/{ws}/summary` `:1871`, `/{ws}` `:2006`. `config_routes.py`: `/reset` `:74`, `/mode` POST `:125` GET `:192`. `onboarding_routes.py`: `/status` `:86`, `/complete` `:92`, `/test-connection` `:122`. `prompt_routes.py`: GET `""` `:69`, `/{filename}` GET `:75` POST `:94` DELETE `:118`. `provider_routes.py`: `/providers` GET `:70` POST `:80`, DELETE `/{provider_id}` `:111`. `logging_routes.py`: `/config` GET `:33` PUT `:53`. `vault_repair_routes.py`: `/vault/repair/status` `:40`, `/vault/repair/apply` `:82`. `global_routes.py`: `POST /credentials` `:407`, `DELETE /credentials/{name}` `:424`. `container_record_routes.py`: `/{record_id}` `:165`, `/{record_id}/events` `:192`.

### 5.3 Agent tools

Lane coverage is thin **because the tool classes are not enumerable by declaration** (§4(a)) — a defect of this survey, recorded in §6(c), not a property of the code.

| Entry point | Fact | Scoping status | Owner site | Verification |
|---|---|---|---|---|
| `CheckSystem query="event_log"` | event | scoped by convention — the read is session-filtered and body-redacted, and the persisted record carries `session_id`; because `workspace_id` is never written (`agent/logging/event_logger.py:169`), the read can only ever be session-complete, never workspace-complete | `_query_event_log` `tools/workspace/check_system.py:1087` (helpers `_event_belongs_to_session` `:1029`, `_redact_event_body` `:1057`) | `test_event_log_scoped_to_caller_session` `tests/test_event_log_scoped.py:82`; `test_event_log_fails_closed_without_session_scope` `tests/test_event_log_scoped.py:101` |
| `CheckSystem query="event_log"` (write path) | event | not scoped on the workspace axis — the writer **is** session-attributed: `"session_id": getattr(event.metadata, "session_id", None)` (`agent/logging/event_logger.py:168`). It is **not** workspace-attributed: `"workspace_id"` is the same `getattr` (`:169`) over a model that has no such field, so it is always `None` — the code's own comment says so (`:165-167`) | `_on_event` `agent/logging/event_logger.py:142` (record built `:144-170`) | NONE |
| `CheckSystem query="running_workers"` | worker | scoped by construction — the handler filters the process-global registry on `self.session_id or ""` and fails closed (no session ⇒ no entries). It takes no session *argument* because the scope is the object's own | `_query_running_workers` `tools/workspace/check_system.py:805-815` (scope `:807-809`, filter `:814`) | `test_running_workers_scoped_to_calling_session` `tests/test_check_system_running_workers_scoping.py:63`; `test_running_workers_fail_closed_without_session` `tests/test_check_system_running_workers_scoping.py:83` |
| `CheckSystem query="vault_status"` | vault | scoped by convention — scope applied by a post-filter over a globally-built report, plus a global-allowed escape hatch | `_query_vault_status` `tools/workspace/check_system.py:1150` (post-filter `:1216`, global gate `:1201`) | `test_default_scope_hides_foreign_workspace` `tests/test_check_system_vault_status_scoping.py:61` |
| `CheckSystem query="workers"` / `"worker_detail"` | worker | scoped by convention — both take `ws_id`, derived from the caller | `_query_workers` `tools/workspace/check_system.py:765`; `_query_worker_detail` `:783` | NONE |
| `CheckSystem query="containers"` / `"container_status"` | container | scoped by convention — both take `ws_path` from the caller's context | `_query_containers` `tools/workspace/check_system.py:492`; `_query_container_status` `:472` | NONE |
| `CheckSystem` allowlist gate (all queries) | vault | scoped by convention — every query is gated by a path allowlist loaded from the vault, and the gate fails open on `None` in at least one mode | `_check_path_allowed` `tools/workspace/check_system.py:268` (`_load_allowlist_from_vault` `:285`) | NONE |
| Worker registry key (spawn/stop/reset/sweep) | worker | not scoped — the registry key is asserted to carry **no** workspace scope, by a test that pins that absence | UNOWNED (file-level: `tools/workspace/worker_registry.py`) | `test_registry_key_has_no_workspace_scope` `tests/test_worker_workspace_scoping.py:110` |
| `git_read`/`git_write` (repo boundary) | vault | scoped by convention — the repo root must resolve inside the resolved workspace (`repo_root.relative_to(ws_abs)`, else `ValueError`), **but** the docstring is explicit that when no workspace can be resolved "no restriction is applied" | `_validate_repo_root` `tools/git_info_tool.py:474` (fail-open branch `:481-483`; inherited by `GitWriteTool` `tools/git_write_tool.py:57`) | NONE |
| `host_bash` (cwd) | vault | not scoped — the child is launched as `subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)` with **no `cwd=` argument**, so it inherits the server process's working directory; nothing in the tool derives a cwd from the caller's workspace (the workspace is consulted for *authorisation* only — grain `_effective_grain` `:92`, `allow_host_resources` `:261-270`, audit id `_workspace_id` `:107`) | `HostBashTool.execute` `tools/host_bash_tool.py:227` (launch `:303`) | NONE |
| `knowledge_base` (read/write scope) | KB | scoped by convention — global-scope writes are refused by class, and the workspace root comes from the session→workspace registry, **but** an unresolvable workspace falls back to `Path.cwd()` | `KnowledgeBaseTool.execute` `tools/knowledge_base.py:244` (root resolution `:253-289`, `Path.cwd()` fallback `:289`, global write refusal `:341-342` / `_global_write_refusal` `:377`, containment `_resolve_domain_path` `:405`) | NONE |

### 5.4 Evidence footnotes

† = row's evidence came from a filed ticket in `.thoughtmachine/working_docs/`, not from a first-hand read in this session. The ticket is named so the row can be re-derived; the ticket's own citations are **not** copied here, because this pass could not re-verify them.

| Row(s) | Ticket |
|---|---|
| `load_session` (session, worker) | `ticket-bug-load-session-workspace-pivot.md` |
| `delete_session`; `DELETE /api/session/{id}` | `ticket-bug-session-delete-ignores-workspace-scope.md` |
| `set_project` | `ticket-bug-ws-set-project-bypasses-vault-confinement.md` |
| `security_response` | `ticket-bug-ws-security-response-allows-anonymous-escalation.md` |
| `create_session` (`POST /api/session/create`) | `ticket-bug-create-session-drops-workspace-id.md` |
| `/health`, `/api/system/health` | `ticket-bug-health-route-running-workers-bypasses-check-system.md` |
| `CheckSystem running_workers` | `ticket-bug-checksystem-running-workers-global-leak.md` — **† withdrawn**: the row's premise is stale as of `d45cebb03`, see F15 |
| `CheckSystem vault_status`, `GET /api/vault/status` | `ticket-bug-checksystem-vault-status-cross-tenant-leak.md`, `ticket-bug-checksystem-vault-status-issues-unscoped.md` |
| `CheckSystem event_log` | `ticket-bug-checksystem-event-log-cross-session-leak.md`, `ticket-bug-event-log-global-no-workspace-scope.md` |
| `host_bash` | `ticket-bug-host-bash-cwd-wrong-workspace.md` |
| `knowledge_base` | `ticket-hygiene-global-kb-docs-claim-writable-global-scope.md` |
| WS lane generally | `ticket-bug-ws-commands-unauthenticated.md`, `ticket-bug-ws-handshake-bypasses-cors-origin-check.md` |

---

## §6 Known gaps

### (a) `_worker_bus_subs` is keyed on a bare worker label, not on `(session_id, label)`

`Bridge._worker_instance_label` (`web_ui/backend/bridge.py:159-166`) produces the key used for the subscription map initialised at `bridge.py:483`. The key carries no session component.

- **Simultaneous sessions are disjoint by construction.** Each WebSocket connection owns its own `Bridge`, the handler closures are built per bridge (`_make_handler`, `bridge.py:580-616`) with a session guard at `bridge.py:589`, and the subscription dict is an instance attribute. Two connections live side by side without colliding.
- **A warmed/reused bridge breaks that.** When a bridge is re-pointed at a different session, entries written under the old session stay in the map, because the stale check is on the *bus*, and `_stored_entry_is_stale` returns `False` when `current_bus is None` (`bridge.py:655-666`, `:662-663`). `_worker_bus_is_stale` reconstructs a candidate session id with `entry.get('session_id') or self._session_id or ''` (`bridge.py:683`), which is exactly the fallback that makes an unlabelled entry look current.
- **The early-out closes the door on repair.** `_on_worker_spawned` (`bridge.py:812`) applies its own session guard (`:836-839`) and stale check (`:845-849`), then returns early on a duplicate at `bridge.py:853` — so a stale-but-not-detectably-stale entry is never re-bound to the new session. The resolve-and-subscribe pair sits at `:861-872` and is simply not reached.
- **Consequence recorded, not fixed:** a warmed bridge can hold, and keep firing, handlers bound to a previous session's workers. The map is per bridge, so the blast radius is the connections that share that bridge — which, given the cached-bridge path (`_cached_bridge_for` `web_ui/backend/server.py:1086`, reuse comment `:2454-2457`), is more than one.

`tests/test_worker_bus_respawn_bridge.py` contains exactly three tests, now cited where they bite:

- `test_respawn_after_bus_replacement_still_delivers_tool_events` (`:127`) — old bus bound, registry entry replaced by a **new** bus for the same `(session, worker, instance)` key, `WORKER_SPAWNED` published again, then `tool_call_start` on the new bus must be forwarded and the old bus must go silent. Pins `_on_worker_spawned`'s re-bind on a bus swap.
- `test_missing_worker_bus_retries_boundedly_then_degrades_once` (`:189`) — an unresolvable per-worker bus is retried exactly 5 times, then surfaced **once** as a `worker:system_notification` naming the worker, with no `_worker_bus_subs` entry left behind.
- `test_stop_and_respawn_without_completion_leaves_no_dead_bus_bound` (`:239`) — the entry survives a worker that died without a delivered `WORKER_COMPLETED`, and the respawned instance's events arrive while the vanished bus's do not.

None of the three exercises a **session pivot**: all three hold the session id fixed and change the bus. In particular the `current_bus is None → not stale` branch of `_stored_entry_is_stale` (`bridge.py:662-663`), which is the branch F3 turns on, is not covered by any of them — F3 stays unpinned. The `session_id` fixture docstring (`:45-56`) describes the leaked-listener behaviour in prose; prose is not a test and is cited as none.

### (b) `EventBus.subscribe()` returns `None` for the bridge's call form, so the bridge's subscriptions have no handle

`EventBus.subscribe` is `def subscribe(self, event_type=None, callback=None)` (`agent/events.py:455`). The decorator branch returns the decorator (`:457-462`); the **registration branch has no return at all** (`:463-469`). Called as `subscribe(event_type=..., callback=...)`, it therefore yields `None`.

The bridge registers:
- the security subscription at `bridge.py:562-564`, stored into `_security_subscription` (`bridge.py:463`);
- seven worker subscriptions in `_subscribe_to_worker_events` (`bridge.py:567-640`, subscribes at `:618-638`), stored into `_worker_*_sub` attributes initialised to `None` at `bridge.py:465-471`.

That is **8 subscriptions per bridge whose handle is `None`**. `_unsubscribe_worker_events` (`bridge.py:784-808`) walks the attribute list (`:786-794`) and guards each detach with `if sub is not None:` (`:797`) — so the `unsubscribe(sub)` call at `bridge.py:800` is unreachable for every subscription this bridge creates. `_unsubscribe_security_events` (`bridge.py:772-782`) has the same shape. `stop()` (`bridge.py:1674`) dutifully calls `_unsubscribe_worker_events()` (`:1681`) and detaches nothing.

This is not merely a leak of a list entry: `EventBus.publish` (`agent/events.py:471-485`) copies the callback list and then invokes each callback inside `try/except Exception` logging `core.event_bus`, so a dead handler stays attached, keeps being called, and any exception it raises is swallowed and visible only as log noise. `unsubscribe` itself swallows `ValueError` on both the wildcard remove (`agent/events.py:496-499`) and the typed remove (`:502-505`), so a wrong-handle detach is silent too.

The one place that *does* detach correctly is `_unsubscribe_worker_bus` (`bridge.py:1111`), which passes the callback explicitly at `bridge.py:1137-1141` — the two-argument form works precisely because it does not depend on a return value. That is the proof that the gap is the missing return, not the unsubscribe semantics.

### (c) Entry points I could not owner-site

Recorded as `UNOWNED` rows above; this is the count and the reason for each:

| Entry point | Why no function-level owner |
|---|---|
| `GET /api/container-records` (`container_record_routes.py:130`) | route body delegates to a module-level API; the scoping decision is not in a single named function I could point at |
| `GET /api/system/health` (`health_routes.py:182`) | the payload is assembled across helpers; the worker-status scoping is not localised to one function |
| `GET /api/global/summary` (`global_routes.py:322`) | same — a summary builder spanning registries |
| Worker registry key (spawn/stop/reset/sweep) | the key is constructed at several call sites in `tools/workspace/worker_registry.py`; the defect is the *absence* of a field, which has no owner line |
| `git_read`/`git_write` repo boundary | tool classes are not enumerable by declaration regex in this tree (§4(a)); I did not locate the class |
| `host_bash` cwd | same |
| `knowledge_base` scope | same |

### (d) Survey gaps that belong to this document rather than to the code

- **`tools/` has 38 tool modules and this pass adjudicated 8.** The class-declaration regexes returned zero hits (twice), and I stopped enumerating rather than switching to reading 38 files. Lane 5.3 is therefore **structurally under-covered**, and the coverage table says so.
- **56 of 81 HTTP routes were enumerated and not adjudicated.** They are named with lines in §5.2b so the next pass can pick them up without re-running the enumeration.

---

## §7 Findings

Each row: what is wrong, the one-line blast radius, and the literal tag. **No patches.** Ordered by the fact they violate.

| # | Entry point | Fact | What is wrong | Blast radius | Tag |
|---|---|---|---|---|---|
| F1 | Bridge worker subscriptions (`bridge.py:567-640`) | event, worker | `EventBus.subscribe()` returns `None` for the callback form, so all 8 per-bridge subscriptions (`:562`, `:618-638`) have no handle; `_unsubscribe_worker_events` guards on `if sub is not None` (`:797`) and therefore never detaches (`:800` unreachable) | Every bridge ever created keeps calling its handlers until process exit; a handler bound to a dead session still runs, and its exceptions are swallowed by `publish` | **RECORDED, NOT FIXED** |
| F2 | `_worker_bus_subs` key (`bridge.py:483`, label from `:159-166`) | worker | subscription map is keyed on the bare worker label with no session component | two sessions sharing a warmed bridge collide on the same key; nothing in the key prevents it | **RECORDED, NOT FIXED** |
| F3 | `_on_worker_spawned` (`bridge.py:812`) | worker | duplicate early-out at `:853` runs *after* the stale checks at `:845-849`, and `_stored_entry_is_stale` answers "not stale" when the current bus is `None` (`:662-663`) | a re-pointed bridge never re-binds its worker handlers; the new session gets the old session's subscriber | **RECORDED, NOT FIXED** |
| F4 | `_worker_bus_is_stale` (`bridge.py:683`) | worker | candidate session id is built as `entry.get('session_id') or self._session_id or ''` — an entry with no session id silently adopts the current one | staleness detection cannot distinguish "my entry" from "an unlabelled entry", so the check that exists to catch F2/F3 cannot fire | **RECORDED, NOT FIXED** |
| F5 | `_on_worker_completed` (`bridge.py:1218-1229`), `_on_worker_error` (`bridge.py:1231-1242`) | worker | both unsubscribe unconditionally and carry no session guard of their own, unlike the `_make_handler` closures (`:589`) | a completion/error event from a foreign session detaches handlers that this bridge may still need | **RECORDED, NOT FIXED** |
| F6 | `close_session()` (`bridge.py:2532`) | session | calls `self.stop()` (`:2549`) then clears `self._session_id = None` (`:2579`); because of F1 the stop detaches nothing, and clearing the id removes even the fallback that F4 depends on | a bridge that outlives its session cannot be told from a fresh one | **RECORDED, NOT FIXED** |
| F7 | `EventBus.unsubscribe` (`agent/events.py:492-507`) | event | swallows `ValueError` on both the wildcard (`:496-499`) and typed (`:502-505`) removal paths | a wrong-handle detach is indistinguishable from a successful one; F1's failure mode is invisible by design | **RECORDED, NOT FIXED** |
| F8 | `EventBus.subscribe` (`agent/events.py:455`, registration branch `:463-469`) | event | the registration branch returns nothing while the decorator branch returns the decorator (`:457-462`); two contract shapes behind one name | every caller that stores the return value stores `None` and believes it holds a handle | **RECORDED, NOT FIXED** |
| F9 | `CheckSystem query="event_log"` write path (`event_logger.py:142`) | event | the writer **does** stamp the session: `"session_id": getattr(event.metadata, "session_id", None)` (`agent/logging/event_logger.py:168`). It does **not** stamp the workspace: `"workspace_id"` is the identical `getattr` (`:169`) over a model that has no such field, by the code's own comment (`:165-167`), so the key is always `None` | no read-side filter can ever be workspace-complete; the session filter is real and is the only scope that can work. The gap is a **missing model field**, not a missing line — adding lines cannot close it | **RECORDED, NOT FIXED** |
| F10 | `CheckSystem query="running_workers"` (`check_system.py:805-815`) | worker | **this finding is retracted as filed.** The absence of a session *argument* is not an absence of scope: the handler derives it from `self.session_id or ""` (`:809`, with the intent stated at `:807-808`) and filters the process-global registry with `if not isinstance(key, tuple) or key[0] != session_id: continue` (`:814`) — so it fails closed, and two tests pin both halves. All four lines land in `d45cebb03` (2026-10-21) | none on the session axis: a caller that lost its identity now sees **nothing** rather than everyone else's workers. The still-open axis is not this function — it is F13/F15 | **RETRACTED — stale premise (see F15)** |
| F11 | `GET /api/global/summary` (`global_routes.py:322`) | vault | one-shot snapshot of registered workspaces, open sessions and containers — all of them, by design and by name | anyone who can reach the route sees the whole machine's inventory; contrast with the scoped `/api/vault/status` | **RECORDED, NOT FIXED** |
| F12 | `load_session` WS branch (`server.py:2334`) | session, worker | the session id is client-supplied and the bridge is re-pointed and cached (`:2463`, `:2472`); the guard against a foreign pivot lives in the store/bridge, not in the command, and an unscoped bridge is explicitly allowed to load any workspace (`tests/test_bridge_load_session_workspace_scope.py:115`) | one command changes what a connection *is*, and the only thing standing between it and another workspace is a scope check one layer down | **RECORDED, NOT FIXED** |
| F13 | Worker registry key (`tools/workspace/worker_registry.py`) | worker | a test pins the absence of workspace scope on the registry key (`test_registry_key_has_no_workspace_scope`) — the gap is asserted, not accidental | worker enumeration and sweeping can cross the workspace line by construction; the test protects the behaviour from being fixed silently | **RECORDED, NOT FIXED** |
| F14 | 50 of the 65 matrix rows | — | 50 of the 65 adjudicated entry-point × fact rows carry verification `NONE`; 15 are pinned by a named test (6 in 5.1 — two of those only **partially** — 5 in 5.2, 4 in 5.3) | the scoping that does hold is largely unpinned — a regression would be a code-review miss, not a CI failure | **RECORDED, NOT FIXED** |
| F15 | `ticket-bug-checksystem-running-workers-global-leak.md` vs `test_registry_key_has_no_workspace_scope` (`tests/test_worker_workspace_scoping.py:110`) | worker | **two different axes, conflated into one ticket.** (i) *Session* axis — what an agent can read from `query="running_workers"`: closed in `d45cebb03`, scoped from `self.session_id`, fails closed, pinned by two tests. (ii) *Workspace* axis — the registry key `(session_id, worker_name, instance_id)` has **no** workspace component at all, and a test pins that absence as the expected behaviour. Evidence about (i) says nothing about (ii) | leaving the two merged keeps a fixed defect looking open and an open one looking fixed; the ticket should be closed as stale on (i) and a new one opened on (ii) | **RECORDED, NOT FIXED** |
| F16 | filed ticket asserting the event-log writer "drops `session_id` at write time" | event | **stale as filed.** The writer has stamped `session_id` since `d60ffc92b` (`agent/logging/event_logger.py:168`). What remains is F9's missing `workspace_id` **model field** (`:169`, comment `:165-167`), which no line in the writer can supply | closing the ticket as stale must not close F9 with it — the workspace gap is real, and it is a different object from the session gap | **RECORDED, NOT FIXED** |
