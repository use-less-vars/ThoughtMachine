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
