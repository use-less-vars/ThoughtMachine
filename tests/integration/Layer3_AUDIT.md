# Layer-3 Session-Tab Foundation — Audit Evidence

**Date:** 2026-08-15
**Branch:** `feat/sec-rce-upgrade`
**HEAD:** `580d9cb`
**Scope:** Layer-3 slice — session creation / switching / persistence (R2), WebSocket
architecture mapping for multi-tab support (R5), worker event routing & isolation
(R2/R6), session restore semantics (R7), provider/LLM mocking seams (R3), and the new
concurrency integration test (R3).
**Method:** read-only audit of the two Layer-3 audit WorkingDocuments (backend Q1–Q12
`ae2edd9d4010`, frontend 12 sections `f7b57e84fe93`, both recorded on this branch/HEAD)
+ spot greps on source (`server.py`, `bridge.py`, `event_forwarder.py`, `App.jsx`,
`SessionTab.jsx`) + fresh baseline runs (Section 1). No source files were modified by
this audit.

---

## 1. Baseline results (fresh)

Recorded on HEAD `580d9cb` before this audit:

- **pytest:** `1416 passed / 38 skipped / 0 failed` — `1454 collected`, `117.07s`
  (`python3 -m pytest -q`). Note: `pyproject.toml` `testpaths = ['tests']` means the
  runner only collects the root `tests/` tree; `web_ui/backend/tests/` is NOT collected
  by the default command (see §8.4 for the empty stub there).
- **vitest:** `11 files / 306 tests passed`, `44.69s`
  (`npm test --prefix web_ui/frontend -- --maxWorkers=1`).
- `SessionTab.test.jsx`: **25/25 passed**. Its file header previously claimed the suite
  was written from static analysis and *"have NOT yet been executed"* (predicted
  outcomes, SessionTab.test.jsx L1-18); that stale header is being corrected — the suite
  now executes green in this environment.

---

## 2. R2 audit — session creation / switching / storage

### 2.1 Why only one session is usable at a time: it is a UI gap, not a backend limit

**Backend is multi-session capable and has no single-session lock:**

- Single WS endpoint serves ALL sessions; selection is command-based (see §3).
- `_session_bridges: dict session_id → WebAgentBridge` warm cache
  (`web_ui/backend/server.py:379`, entries at L958, L1902, L2030). No global
  single-session lock exists; the only cross-session shared state is the store
  singleton + `_session_store_lock` for cache coherence (`server.py:131-135`), the
  global event bus, and the `_active_tab_bridges` set (`event_forwarder.py:22`).
- Each bridge owns one agent: `"A single bridge instance manages one agent session"`
  (bridge.py module docstring L7), one thread + query queue
  (`bridge.py:1108-1113` `self._thread = threading.Thread(target=self._run_loop,
  daemon=True, name="web-bridge-agent")`, `_query_queue` L222, `_stop_event` /
  `_pause_event` L217-219). Multiple sessions therefore run concurrently as separate
  bridges with separate controllers; `controller.is_busy` is per-controller
  (`bridge.py:884-889`, deferred apply when busy L1993-1997).
- WS handler keeps a per-connection `bridge` variable, rebinding it on
  new_session/load_session/set_project (`server.py:1407-1567`, cached-bridge reuse
  L1409-1410, fresh creation L1455-1457).

**Frontend mounts all tabs but hides them; there is no visible tab strip:**

- `App.jsx` owns `const [tabs, setTabs] = useState([])` (L49-50). All tabs stay
  mounted; hidden ones are `display:none` (`App.jsx:636-640`
  `<div className="tab-wrapper" style={{display: visible ? '' : 'none'}}>`), and
  visibility is `route.view === 'session' && route.id === tab.sessionId`
  (App.jsx:634).
- **No tab strip exists.** `TabBar.jsx` (71 lines) is dead code: zero JSX imports of
  it anywhere in `src`; it is only mentioned in comments (`styles.css:1252`,
  `useStore.js` docstring L6/L13/L69).
- Tab creation: `loadTab` (`App.jsx:316-364`), deduped via `loadedSessionIdsRef`
  (L324-335), `tabId = tab-${Date.now()}-${rand}` (L337).
- Switching UX today: WorkspaceSelector → `navigate('/session/<id>')` → `loadTab`
  (`App.jsx:361`); leaving a session is only possible via the per-tab
  **"← Back to Workspace"** button (`SessionTab.jsx:1108-1116`,
  `navigate(\`/workspace/${encodeURIComponent(backWorkspaceId)}\`)`).
- **Conclusion:** a user who starts session B cannot return to a still-running session
  A because no strip renders the hidden tabs — a UI gap (R4 target), not a backend
  restriction.

### 2.2 Session storage

- `FileSystemSessionStore` (`session/store.py:87`, ABC L60); `save_session` L269 →
  `~/.thoughtmachine/workspaces/<ws_id>/sessions/<name>_<short_id>.json` (L272-274,
  legacy `~/.thoughtmachine/sessions` fallback); `load_session` L350; metadata batch
  L457; `delete_session` L743 ("prevents session resurrection", scans `_meta_` files).
- `session/session_registry.py`: `~/.thoughtmachine/state/session_registry.json`
  (L20-23), thread-safe via `_file_lock` (L43), `rebuild_from_disk` L143.
- Open-session ledger: `open_sessions.json` (`store.py:811-813`), `get_open_sessions`
  L815, idempotent `add_open_session` L850, `remove_open_session` L859.
- On disconnect, open sessions are re-saved: `server.py:2199-2224` —
  explicitly closed sessions discarded, un-cleanly-closed ones re-persisted via
  `bridge.save_open_session()` (L2216-2224).

---

## 3. R5 — WebSocket architecture mapping (preserve; do NOT create per-tab WS)

### 3.1 One backend endpoint

- `@app.websocket("/ws")` (`server.py:523-524`) — the ONLY websocket route in
  `web_ui/backend` (regex scan). The only optional query param is `project` (absolute
  path, L526-528; documented at L2905-2906).
- Frontend sends **no query params** on either socket:
  `App.jsx:42-43` and `SessionTab.jsx:37-38`:
  ```js
  const WS_URL = `ws://${window.location.hostname}:${WS_PORT}/ws`
  ```
  (vite dev proxy: `vite.config.js:11-14`). Session binding is purely command-based
  inside the socket (`new_session` / `load_session`); no `/ws/session/<id>` variant
  exists and `session_id` never appears in the URL.

### 3.2 Event forwarder granularity = per bridge

- Each bridge owns an `EventForwarder` (`bridge.py:228`). Callbacks are registered by
  WS identity: `bridge.set_event_callback(event_callback, key=id(ws))`
  (`server.py:1410`), removed on disconnect (`server.py:2199-2203`;
  `bridge.py:901-913`).
- `broadcast` **ignores** its session_id argument and fans out to every registered
  callback of that forwarder (`event_forwarder.py:84-93`):
  ```python
  def broadcast(self, _session_id, event_type, data):
      """Broadcast an event to every registered callback."""
  ```
  → different sessions = different bridges = isolated registries; cross-session
  leakage via the forwarder is impossible. Same session on N tabs → N callbacks on the
  same bridge → every tab receives every event (no per-tab filter server-side).

### 3.3 Session identity is implicit, not in payloads

- Canonical session event: `{"type": EventType, "created_at": float, "data": Dict}`
  (`session/event_schema.py`); **none of the TypedDicts carry `session_id`** — scoping
  is implicit via the per-bridge forwarder (verified by full read of event_schema.py).

### 3.4 Decision (preserve existing model)

- Keep **one App-level hub WS** (`App.jsx:93-173`; traffic: `list_sessions` on open
  L115-123, `get_open_sessions` L215-220) + **one WS per SessionTab**
  (`SessionTab.jsx:285-427`), ALL hitting the single `/ws` endpoint. Multi-tab same
  session shares one bridge/forwarder. No new per-tab backend WS architecture.
- A shared single hub for ALL session traffic would require backend changes (global
  fanout + session_id in payloads) — **out of scope** for this slice.

---

## 4. Worker event routing & isolation (R2/R6 backend side)

Bridge subscribes to the global event bus on spawn and replays buffered events for
late-arriving sockets:

- `_on_worker_spawned` (`bridge.py:526+`) subscribes per-worker buses BEFORE the
  session guard (race fix); `_discover_existing_workers` for reconnects/new tabs
  (~L415). Per-worker bus handlers flatten `tokens_updated`/`context_updated`
  (X.XK dedup) and map others to `worker:<type>` with worker_name/timestamp/data.
  Heartbeat types are NOT buffered.
- Events are buffered (ring, max 100, `bridge.py:233-235`) **before** the drop gate,
  replayed via `set_event_callback` → `_flush_worker_event_buffer`.
- **Session filter (critical isolation code), repeated at every global-bus handler:**
  ```python
  # web_ui/backend/bridge.py:364 (same guard at L462, L546, L815)
  if data.get('session_id') and data['session_id'] != self._session_id:
      return
  ```
  Events without a session_id (tagless) are broadcast to ALL bridges — confirmed by
  integration test and by the forwarding path `_forward_worker_event` (L815).
- After the filter, delivery is `for cb in list(self._forwarder._callbacks.values()):
  cb(event_dict)` — all callbacks on that bridge (still no per-tab filter).
- Security prompts use the same session_id filter (`_security_prompt_handler`).
- Not subscribed by bridges: `status_message`, `error_occurred`, `config_changed`,
  `conversation_changed` (main-agent only).

---

## 5. Restore semantics (R7 backend side)

- Server doc header claims `open_sessions_list` (`server.py:73`), but the
  implementation emits **`open_sessions`** (`server.py:1727-1747`):
  ```json
  {"type": "open_sessions", "sessions": [{"session_id","name","updated_at","message_count"}]}
  ```
  (metadata batch via `load_sessions_metadata_batch`, `store.py:457`).
- Restore path: `load_session` → fresh `bridge.load_session()` broadcasts
  `conversation_changed` (bridge.py:1682), `session_loaded` (L1694-1710,
  payload incl. `session_id/session_name/message_count/workspace_id/workspace_path/
  is_running/config/tools`), `context_updated` (L1713), `tokens_updated` (L1718).
  "The server no longer sends a separate state_changed after session_loaded"
  (L1700-1703 comment, Fix 2a).
- Failure: `session_loaded` with `"load_error": true` (`server.py:1551-1559`).
- Un-cleanly-closed sessions are re-saved to `open_sessions.json` on disconnect
  (`server.py:2216-2224`).
- Frontend consumption: hub `open_sessions` handler (`App.jsx:257-276`) restores ALL
  open sessions as tabs; preferred tab = localStorage `activeSessionId` if present,
  else first in list (L262-270); `msg.sessions.forEach(s => loadTab(s.session_id,
  preferred))` (L271-273).

---

## 6. Provider/LLM mocking seams (R3 evidence)

- `ProviderFactory` (`llm_providers/factory.py`); the sole creation seam is
  `LLMClient.__init__` → `ProviderFactory.create_provider(...)`
  (`agent/core/llm_client.py:37`).
- `provider_type` is deliberately NOT `Literal` (`agent/config/models.py:74-76`) so
  plugins registered via `ProviderFactory.register_provider()` (e.g. `mock`) pass
  config validation.
- `MockProvider(LLMProvider)` (canned `LLMResponse`, `count_tokens=42`) registered
  via `_register_mock_provider()` → `ProviderFactory.register_provider("mock",
  MockProvider)` (`tests/web_ui/backend/test_ws_mock_provider.py:53,96-99`).
- Fixture pattern: temp HOME + `Path.home()` patch + purge/re-import of
  `web_ui.backend` / provider_profile / bootstrap modules + `TestClient` +
  `client.websocket_connect("/ws")`; `recv_n` / `poll_for_type` helpers.
  Covered: `new_session` → exactly 5 events (`session_loaded, tokens_updated,
  context_updated, config_changed, status_message`); `continue_session` without a
  usable provider → error status_message and the mock is never instantiated;
  config roundtrips; api_key stripped from dumps.
- Other registered test providers: `ScriptedProvider`
  (`tests/test_worker_agent_transplant.py:39`), `EchoToolProvider`
  (`tests/test_worker_loop_spike.py:33`).
- **DUP PATH (see §8.4):** `web_ui/backend/tests/test_ws_mock_provider.py` is a 0-byte
  stub; the real 18.2 KB test lives at `tests/web_ui/backend/`.

---

## 7. R3 test — `tests/integration/test_concurrent_sessions.py`

New integration test proving concurrent-session isolation over the single `/ws`
endpoint (uses the `mock` provider seam of §6 + hermetic-vault fixture from
`tests/conftest.py`):

| Test | Covers |
|---|---|
| `test_concurrent_sessions_respond_independently` | two sessions with distinct `session_id`s; query on A produces A-only traffic; B socket silent; no cross-leak of conversation/session_loaded events |
| `test_worker_events_delivered_to_owning_session_only` | worker events tagged with `session_id` reach the owning socket only; tagless events reach all bridges; both cross-checks asserted |
| `test_closing_one_session_leaves_other_running` | `ws_a.close()` → B still answers a follow-up query; open-session persistence not disturbed |

Result: **3 passed (12.55s)**; reference `tests/web_ui/backend/test_ws_mock_provider.py`
still **7 passed**. Design notes recorded from probes: `new_session` emits exactly 5
events; `bridge._session_id` is set only after a query; worker wire type is
`worker:worker_status` with `data`; `_receive_pool` single-worker hazard means
drain/silence checks must be the last reads on a socket.

---

## 8. Known doc/code mismatches found

1. **`open_sessions_list` vs `open_sessions`** — server.py doc header L73 vs actual
   emit (server.py:1727-1747). Frontend must consume `open_sessions`.
2. **`config-panel-width:${tabId}` orphaned** — keyed by regenerated
   `tab-${Date.now()}-${rand}` (SessionTab.jsx:166/177, App.jsx:337) → stale entries
   accumulate; should be re-keyed by sessionId.
3. **`lastSessionMode` vs `thoughtmachine_last_mode`** — App.jsx:525 reads
   `lastSessionMode` (never written by App); modals write `thoughtmachine_last_mode`
   (SessionCreationModal.jsx:16/144, NewSessionModal.jsx:26/54). Two sibling keys.
4. **`web_ui/backend/tests/test_ws_mock_provider.py` 0-byte stub** vs the real test at
   `tests/web_ui/backend/test_ws_mock_provider.py` (18.2 KB) — pytest's default
   `testpaths=['tests']` runs the real copy; the stub risks confusion.

---

## Appendix — key file:line index

- WS endpoint: `server.py:523-528` · commands docstring `server.py:30-57`
- Bridge cache & dispatch: `server.py:379,1407-1567,1900-1912,1937-2051,2199-2224`
- Bridge: `bridge.py:217-235,364/462/546/815 (session filter),526+ (bus handlers),
  901-913 (callbacks),1103-1115 (start/continue),1591-1718 (load_session),
  1811-1860 (close_session)`
- Forwarder: `event_forwarder.py:22,28,52-66,70-93,95-103,105-112`
- Store: `session/store.py:87,269-274,350,457,743,811-859` · registry
  `session/session_registry.py:20-23,43,85-143` · event schema
  `session/event_schema.py` (canonical `{type, created_at, data}`, no session_id)
- Mock seams: `agent/core/llm_client.py:37`; `agent/config/models.py:74-76`;
  `tests/web_ui/backend/test_ws_mock_provider.py:53,96-99`
- Frontend hub: `App.jsx:42-43,93-173,215-220,223-292,257-276,316-364,368-389,626-713`
- Frontend tab WS: `SessionTab.jsx:37-38,234-270,285-427,432-451,481-970,1108-1116`
- Store: `useStore.js:38-46,54-260` · router: `router.js:10-40` ·
  dead code: `TabBar.jsx` (71 ln), `components/WorkspacePanel.jsx` (stale, 7.9 KB;
  App uses `components/workspace/WorkspacePanel.jsx`)
- Test fixtures: `tests/conftest.py` (hermetic_vault), `tests/integration/test_server_health.py`
  (contract_server), `SessionTab.test.jsx` (MockWebSocket L30-66)


## Backend file:line evidence (verified)

Verified against the working tree during R3 concurrency-isolation test authoring
(`tests/integration/test_concurrent_sessions.py`, 6 tests, all passing).

### web_ui/backend/server.py
- `close_session` command L1749-1768: resolves `session_id` from `msg.get("session_id","")`; calls `bridge.close_session(session_id if session_id else None)`; records the resolved id in `_explicitly_closed_sessions`; removes the cached bridge from `_session_bridges` and calls `cached_bridge.stop()`.
- `new_session` command L1802-1934: saves + stops the previous bridge (L1804-1808); constructs a fresh `WebAgentBridge` (L1816); `workspace_id = msg.get("workspace_id") or None` → `bridge._workspace_id` (L1822-1824); fallback `_resolve_workspace_id(_project_path)` (L1829-1840); auto-registers `WorkspaceRegistry.get_default().register_by_root(str(_project_path))` → entry.id (L1846-1857); `session_id, frontend_config = bridge.create_session(mode=mode)` (L1870); persists `new_session.workspace_id` (L1873); resolves `workspace_path` from the registry → `bridge._workspace_path` + persisted in metadata `agent_config` (L1877-1895); `_session_bridges[session_id] = bridge` (L1902); `session_loaded` payload L1904-1912 carries `workspace_id` (`bridge._workspace_id`), `workspace_path`, `session_id`, `session_name`, `is_running`, `config`; followed by tokens_updated/context_updated/config_changed/status_message (5 events total).
- `websocket_endpoint` (L524+): disconnect handling — `except WebSocketDisconnect` L2189-2191 marks `ws._closed`; `finally` L2196-2224 removes the event callback for `id(ws)`, and unless the sid is in `_explicitly_closed_sessions` calls `bridge.save_open_session()`; then `bridge.unregister()` — the bridge is NOT stopped (kept cached for reconnect).
- No WS command exists for `spawn_worker` / `list_workers` (command docstring L52-56 lists all supported commands; worker visibility is only via `WorkerRegistry` + `worker:*` events).

### web_ui/backend/bridge.py
- `close_session` L1811-1860: session-id resolution L1821-1823; `self.stop()` L1827; `save_session` + `_session_manager.close_session(sid)` L1834-1835; `shutdown_workers(timeout=5.0)` L1841-1845; `_persisted_workers.clear()` L1848; resets `_session`/`_loaded_session`/`_session_id` to None L1851-1853; broadcasts `session_cleared` + `state_changed` IDLE L1854-1858; sets `_cleanly_closed = True`.
- `_on_worker_spawned` L524-600: session filter L546-549 (`if data.get('session_id') and data['session_id'] != self._session_id: return`); duplicate-subscription guard L553; if `worker_name` and `WORKER_BUS_AVAILABLE` and `get_worker_event_bus` → `worker_bus = get_worker_event_bus(session_id, worker_name)` L562; `_subscribe_to_worker_bus(worker_name, worker_bus)` L567; builds `event_dict = {'type': f'worker:{event.type.value}', 'worker_name', 'timestamp', 'data'}` L582-587; buffers L588; forwards to callbacks L595-600.
- `_subscribe_to_worker_bus` L602-765: subscribed types = tool_call, tool_result, worker_message, assistant_message, context_updated, context_cleared, context_summarized, token_recovery, token_warning, turn_warning, time_warning, user_message, system_notification (L614-619); `worker_bus.subscribe(evt_enum, handler_fn)` L746 (EventType enum); subs stored in `self._worker_bus_subs[worker_name]` L754. `_make_bus_handler` L626-729: tokens_updated → flattened `worker:tokens_updated` (L637-644); context_updated → `worker:context_updated` with dedup via `self._last_context_updated` (L645-675); context_summarized special-cased (L676-687); otherwise generic `{'type': f'worker:{original_type}', 'worker_name': data.get('worker_name', worker_name), 'timestamp', 'data': data}` (L688-698); buffers non-heartbeat L704-705; forwards L722-728. NOTE: per-worker bus handlers do NOT re-check session_id — the subscription itself is the scoping.

### tools/workspace/worker_registry.py
- `WorkerRegistry` singleton (`get_instance()`); `_worker_registry: dict[(session_id, worker_name), Any]` + `_registry_lock`; `_worker_event_bus_registry: Dict[(session_id, worker_name), Any]` + `_bus_registry_lock`; atexit `shutdown_workers`.
- `register_worker(session_id, worker_name, thread)` L58-63 (no type validation; key uses `session_id or ""`); `unregister_worker` L71-79; `get_all_workers` L81-84 (snapshot dict); `find_workers_by_name` L86-98; `register_event_bus` L102-106; `unregister_event_bus` L108-112; `get_event_bus` L114-118; `get_event_buses_for_session(session_id)` L120-133 (filters `sid == session_id`).

### tools/workspace/worker.py
- `shutdown_workers` L445-447, `register_worker_event_bus` L449-451, `get_worker_event_bus` L457-459, `get_worker_event_buses_for_session` L462-464 — all delegate to `WorkerRegistry`. The `WorkerRegistry` module is NOT purged by the mock_server fixture (which purges only `web_ui.backend`, `agent.config.provider_profile`, `thoughtmachine.bootstrap`).

### agent/events.py
- `WorkerSpawnedEvent` L277-285 (WORKER_SPAWNED; validator requires `worker_name` in data); `WorkerStatusEvent` L287-297 (requires `worker_name` + `status`); `WorkerCompletedEvent` L299-307; `WorkerMessageEvent` L309-317 (requires `worker_name`).
- `EventBus` L373+: `subscribe(event_type=None, callback=None)` L381-395; `publish(event: BaseEvent)` L397-412 → typed + wildcard callbacks. `global_event_bus` singleton. EventType enum: WORKER_SPAWNED='worker_spawned', WORKER_STATUS='worker_status', TOOL_CALL, TOOL_RESULT, WORKER_MESSAGE, ASSISTANT_MESSAGE, TOKENS_UPDATED, CONTEXT_UPDATED, TOKEN_RECOVERY, CONTEXT_CLEARED, CONTEXT_SUMMARIZED, TURN_WARNING, TIME_WARNING, USER_MESSAGE, SYSTEM_NOTIFICATION.

### agent/session_manager.py
- `create_session` L78-123: `new_session = Session()`; `metadata["source"] = "web_ui"`; `save_session(new_session, workspace_id=new_session.workspace_id)` L110; `add_open_session` L112; returns `(session_id, frontend_config)`.

### Test-implied behavior (asserted by the 6 tests)
- `new_session` without a `workspace_id` in the message → both connections auto-register the SAME project root workspace (same `_project_path`), so both sessions share a `workspace_id` (per-session isolation is via `session_id`, not `workspace_id`).
- The bridge's `_session_id` is set only after a query produces controller events (`create_session` alone does not set it) — worker-event tests must prime each session with one query first.


---

## Frontend file:line evidence (verified)

Verified against the working tree (post-b282130 multi-session upgrade; full frontend
suite green: 15 files / 335 tests, incl. sessionTabsStore 16, SessionTabsIntegration 5,
SessionWorkerIsolation 3, SessionTab 26).

### web_ui/frontend/src/App.jsx
- `loadTab` L377-400: resolves the owning workspace (hint → learned mapping → last known → current, L380); dedups when the tab already exists L387-394; otherwise `st.openTab(ws, { sessionId, title })` L395 and, only for the preferred session, `st.setActiveTab(ws, sessionId)` L397 + `navigate('/session/<id>')` L398.
- Hub `session_loaded` case L333-337: `loadTab(msg.session_id, undefined, msg.workspace_id)` L336 — a new session created via the hub WS opens a tab, using the backend-provided `workspace_id` as the workspace hint.
- `handleNewSessionCreated` L500-516: dedup guard L506, `st.openTab(ws, ...)` L507, `st.setActiveTab(ws, sessionId)` L509, `navigate('/session/<id>')` L515 — a session created inside a tab lands in the strip and becomes active.
- Route-driven activation L615-641: on `#/session/<id>` — hydrate the owning workspace L632, `st.openTab(ws, ...)` if the tab is missing L633-635, then unconditional `st.setActiveTab(ws, route.id)` L636 — the route wins.
- TabBar strip render L711-724: every tab becomes a strip entry (`tabs.map(t => ({ id: t.sessionId, name: t.title }))` L713); click handler `onSelectTab` L715-718 = `setActiveTab(currentWs, id)` + `navigate('/session/<id>')`.
- Active-only mount L726-753: `{activeTab && (<div className="tab-wrapper" key={activeTab.sessionId}>…<SessionTab …/>)}` — ONLY the active session's SessionTab is mounted (its own WS opens via the hubReady effect, SessionTab.jsx L440-458); inactive tabs are strip entries only — no mount, no WS.
- activeTab derivation L106: `tabs.find(t => t.sessionId === activeSessionId) || null`, fed by the per-workspace store selectors L103-105 — activation is eager when a tab is selected, loading is lazy (strip-only until active).

### web_ui/frontend/src/sessionTabsStore.js
- Storage key L18: `const STORAGE_PREFIX = 'tm.sessionTabs.'` → per-workspace key `tm.sessionTabs.<workspaceId>` (read in `loadEntry` L20-30 @ L22; written in `saveEntry` L32-37 @ L34 as `{ v: 1, tabs, activeSessionId }` on every mutation).
- `openTab` L63-72: dedup on existing tab L66; `const activeSessionId = entry.activeSessionId || sessionId` L68 — a workspace's FIRST tab becomes active automatically; persist via `saveEntry` L71.
- `hydrate` L116-123: `const existing = get().byWorkspace[ws]; if (existing) return existing` L118-119 — idempotent restore; otherwise validates and loads `sanitize(loadEntry(ws))` (sanitize L40-51).

### Why only one session was usable before the fix (pre-upgrade, commit 580d9cb)
Old App.jsx tracked a SINGLE active session app-wide: one `tabs` array + one `activeTabId`,
one derived `activeSessionId`, and one GLOBAL localStorage key (`activeSessionId` — not
per-workspace). Verbatim from `git show 580d9cb:web_ui/frontend/src/App.jsx` (removed by
commit b282130; neither 580d9cb nor 7ffd751 touched App.jsx, so these line numbers are
exact for 580d9cb):

```js
// L47  const [tabs, setTabs] = useState([])           // { tabId, sessionId }
// L48  const [activeTabId, setActiveTabId] = useState(null)
// L80  const activeTab = tabs.find((t) => t.tabId === activeTabId)
// L81  const activeSessionId = activeTab?.sessionId
// L86  const startupActiveSessionId = useMemo(() => {
// L87    return localStorage.getItem('activeSessionId')
// L88  }, [])
// L262 const savedSessionId = localStorage.getItem('activeSessionId')  // open_sessions case
```

The app could restore/activate only one session at a time — with two sessions open the
single `activeTabId` could point at only one of them, so the other was unreachable (a UI
gap, not a backend limit; see §2.1). b282130 replaced this with the per-workspace strip +
`tm.sessionTabs.<wsId>` persistence.

