# ThoughtMachine Web UI Map

Scope: `web_ui/frontend/` (React 18 + Vite 5, plain JSX — no TypeScript) and its FastAPI backend (`web_ui/backend/`: `server.py` + `*_routes.py`). Audit method: **static** import-graph, handler, and route/command tracing — **not** runtime execution; every claim carries a `file:line`. Companion inputs: the seven chunk audits under `.tm-fix-scratch/` (`ui_recon.md`, `ui_chunk_a.md`, `ui_chunk_b.md`, `ui_chunk_c.md`, `ui_chunk_d.md`, `ui_chunk_e.md`, `ui_chunk_f1.md`, `ui_chunk_f2.md`, `ui_chunk_g.md`). The full per-view element tables are appended after the marker at the end of this file.

## How to read this document

- **WIRED** — the control's handler issues a real request/command that reaches an existing backend route, WS command handler, or event emitter.
- **DEAD** — rendered but inert: no handler, a stub/no-op handler, or a control whose action reaches nothing.
- **PARTIAL** — the path works but a caveat materially affects the operator (silent drop, silently unpersisted field, transient success signal, or an inconsistent host contract).
- **ORPHANED** — code/element unreachable from the live entry graph (`index.html` → `src/main.jsx` → `src/App.jsx`); also used for a backend capability (command/route) that has **no** sender/caller.
- **UNVERIFIED** — a plausible path exists but a concrete `file:line` on one side could not be confirmed; not asserted as working.

Row format: `element | view/layer | file:line | verdict | reason`. The per-view appendix tables add `element kind`, `handler name`, `what the handler ACTUALLY does`, `backend counterpart`, and `evidence`.

## Executive summary

- **The UI is largely wired.** Aggregate against the live backend: Chunk A 126 elements (109 WIRED / 9 PARTIAL / 8 DEAD); Chunk B 31 (27 WIRED / 0 PARTIAL / 4 DEAD); Chunk C 39 elements + a 13-command / 30-event WS contract (35 WIRED / 2 PARTIAL / 2 ORPHANED); Chunk D 26 verified / 5 PARTIAL / 0 dead; Chunk E 39 (30 WIRED / 1 PARTIAL / 1 ORPHANED / 7 UNVERIFIED).
- **Both operator complaints are real and now diagnosed — and neither is a missing backend handler.** They are frontend wiring + perception problems.
- **(a) "Save as Default does nothing"** — the backend round-trip is functional end to end (`set_default_config` handler at `server.py:1472`, persistence via `save_global_defaults()` `server.py:174-208`, read back live at session create `session_manager.py:149-157`). It *looks* dead for three reasons: only 6 keys are persisted, success is a 2.5 s transient label, and a stale-session guard silently swallows the click.
- **(b) "Landing-page providers interface does not work"** — two independent frontend bugs. The landing `ManageProvidersModal` is mounted with a no-op `sendCommand` (`WorkspaceSelector.jsx:304`), and the provider **list** is always empty because its `providers` prop is fed by `GET /api/global/summary`, whose builder returns no `providers` key (`global_routes.py:234-238`).
- **The provider backend is healthy on both transports.** WS `save_provider` (`server.py:1571-1627`) and `delete_provider` (`server.py:1629-1677`) work and persist; REST `provider_routes.py:70/80/111` mirrors them; both share `~/.thoughtmachine/providers.json`. The identical modal works when hosted by `ConfigPanel.jsx:604-610`.
- **There is a significant orphan problem: 18 of 63 non-test frontend source files are unreachable** (Chunk F1). They form the entire legacy `workspace/` tabbed shell plus a few zero-importer components/`data`/`types` modules.
- **Three dead-code test suites** exercise those orphans (`WorkspaceSessionList.test.jsx`, `WorkspaceIntegration.test.jsx`, and the "Phase 4 tabs" block of `WorkspacePanel.test.jsx`), giving false green coverage.
- **16 unclaimed REST routes and 9 unclaimed WS commands** exist on the backend with no UI caller (Register 4) — this is the operator's list for NEW feature work.
- **2 WS events are emitted and never consumed** (`workspace_capabilities` `server.py:2553`, `workspace_bootstrapped` `server.py:2575`) and **1 frontend handler listens for an event that is never emitted** (`session_cleared` `SessionTab.jsx:974`).
- **The Stop gap is real:** `stop_session` (`server.py:1079`) and `resume_session` (`server.py:1074`) have handlers but no in-scope sender — there is no Stop button at all (`QueryBar.jsx` docstring `7-13` is stale).
- **Stub-callback pattern:** landing `ManageProvidersModal` (`WorkspaceSelector.jsx:304`, `() => {}`) and landing `PromptLibrary` (mounted with no `onSelectPrompt` at `WorkspaceSelector.jsx:250`) are the two cross-cutting silent no-ops.
- **Local-state-only editor pattern:** `LoggingPanel` (controls `L234-320`, persisted only by Apply `L347`) and the workspace permission editors discard edits unless a separate Apply is pressed — no autosave, no `beforeunload` guard.
- **Set-but-never-rendered pattern:** the save-as-default status, and `provider_deleted` being `console.log`'d but not acted on.
- **UI capabilities the backend offers but the UI never surfaces:** `stop_all` (`workspace_routes.py:1055`), worker `/stop :1112`, `/pause :1155`, `/resume :1267`, plus the 16+9 unclaimed routes/commands.
- **Three coexisting API-base conventions** (gateway risk): relative/proxied (preferred, ~13 components), absolute `http://${hostname}:8000` (`App.jsx:1102/1133/1137/1171/1333`, `ConfigPanel.jsx:48`, `PromptLibrary.jsx:3`), and absolute `ws://${hostname}:8000/ws` (`App.jsx:51`, `SessionTab.jsx:38`).
- **Two websockets are live simultaneously:** one hub socket opened by `App` (commands `list_sessions` / `get_open_sessions`) and one socket **per** `SessionTab` (every tab, including inactive/deferred ones, opens its own and sends `load_session`).
- **Biggest themes, ranked:** (1) the two reported defects; (2) stub callbacks and silent-drop guards; (3) the orphan/dead-code pile and its false-green tests; (4) unsurfaced backend capability; (5) the API-base cleanup.
- **Not a defect by design:** display-only cards, empty-state copy, and placeholder tabs (e.g. Container "External containers" card) are recorded inline but not counted as dead rows.

## The two reported defects, fully diagnosed

### Defect (a) — "Save as Default does nothing"

Root cause: **not** a missing handler. The round-trip is wired end to end:

1. Frontend send — `ConfigPanel.jsx:451-462` strips `session_permissions` and calls `sendCommand('set_default_config', {config})`; `SessionTab.jsx:246-277` wraps it as `{command:'set_default_config', config:{...}}`.
2. Backend receive — `server.py:1472` `elif command == "set_default_config":` (L1486 payload, L1487-1489 `translate_frontend_config`, L1512 `save_global_defaults(cfg_dict)`, L1516-1520 emits `{"type":"default_config_saved","status":"ok"}`).
3. Persist — `save_global_defaults()` `server.py:174-208` filters to `GLOBAL_DEFAULT_KEYS` (`config_manager.py:119-126` = `{provider_id, model, base_url, temperature, max_turns, system_prompt}`) and writes `~/.thoughtmachine/user/defaults.json`.
4. Read back — `session_manager.py:149-157` applies the same 6 keys on the next session create (no restart needed); same key space on read and write, so **no key mismatch**.

The perception has **three** causes:

- **Only 6 keys persist.** `mode`, `tools`, `tool_output_token_limit`, token-monitor thresholds, `workspace_path`, and `session_permissions` are **silently dropped** (filter at `server.py:203`; key list `config_manager.py:119-126`). If the operator changed tools/thresholds, the next session silently reverts them — read as "it did nothing."
- **Transient success signal.** Success is a 2.5 s button label (`ConfigPanel.jsx:110-126` "✓ Default saved!" → reverts), easy to miss.
- **Silent click drop.** `sendCommand` gates on `staleSessionRef.current` and, when stale, only `console.warn`s (`SessionTab.jsx:250`) — a genuine "nothing happens" path after a backend restart.

Fix implication (state only, do not implement): make the persistence contract honest and the feedback durable. Either extend `GLOBAL_DEFAULT_KEYS`/`save_global_defaults()` to cover the dropped keys (tools, token limits, thresholds, `workspace_path`, `session_permissions`) or make the UI show exactly which keys are saved-and-ignored; replace the 2.5 s transient label with a persistent confirmation (and surface a toast/status line); and make the stale-session drop at `SessionTab.jsx:250` visible to the user (disable the button or show "session stale — start a new session") instead of only `console.warn`. Touch points: `server.py:174-208` & `:203`, `config_manager.py:119-126`, `ConfigPanel.jsx:110-126`, `SessionTab.jsx:250`.

### Defect (b) — "Landing-page providers interface does not work"

Root cause: **two independent frontend bugs** — the backend is healthy.

1. **No-op sender.** `WorkspaceSelector.jsx:304` mounts `ManageProvidersModal` with `sendCommand={() => {}}`. The modal's Save (`ManageProvidersModal.jsx:66`) and Delete-confirm (`ManageProvidersModal.jsx:81`) call `sendCommand('save_provider'|'delete_provider', …)`, which go nowhere on the landing page.
2. **Empty list.** The landing modal's `providers` prop (`WorkspaceSelector.jsx:78`) is fed by `GET /api/global/summary`; its builder `_build_summary()` (`global_routes.py:234-238`) returns **no** `providers` key, so the modal always lists nothing.

Backend evidence (healthy): WS `save_provider` `server.py:1571-1627` (persists via `ProviderProfile` + `manager.add_profile` + `manager.save`, emits `provider_saved` `:1609` and `providers_list` `:1613`); WS `delete_provider` `server.py:1629-1677` (emits `provider_deleted` `:1659`, `providers_list` `:1663`); REST equivalents `provider_routes.py:70/80/111`; one shared store `~/.thoughtmachine/providers.json`. The same modal works correctly when hosted by `ConfigPanel.jsx:604-610` (real `sendCommand`). Chunk G adjudicates the Chunk-A-vs-Chunk-F2 contradiction: **Chunk A is correct; the provider paths are NOT documented no-ops.**

Fix implication (state only, do not implement): at `WorkspaceSelector.jsx:304` pass a real sender (the hub WS `sendCommand`) so Add/Edit/Delete dispatch; and populate the list — either add a `providers` array to `_build_summary()` (`global_routes.py:234`) or fetch providers for the landing modal separately (REST `GET /api/providers` already exists). Bug #1 is the most likely intended fix; bug #2 is independent and must also be fixed for the list to populate.

## Architecture and wiring primer

- **Stack:** React `^18.3.1` + `react-dom`, built with Vite `^5.4.0` + `@vitejs/plugin-react`; tests via vitest `^3` on jsdom. Plain JSX/JS — **no TypeScript**. `react-router-dom` is declared in `package.json` but **not installed/not used**.
- **Router:** dependency-free **hash** router, `src/router.js` (51 lines) — `parseHash` / `useRoute` / `useNavigate`. Three-layer model: **selector → workspace → session**. Routes: `#/workspaces` → selector; `#/workspace/:id` → workspace; `#/workspace/:wsId/session/:sid` → session; legacy `#/session/:id` → session with `workspaceId:null`.
- **Entry chain:** `index.html` → `src/main.jsx` (18) → `src/App.jsx` (1374). `App.jsx` is the top-level orchestrator (tabs, hub WS, banners, worker-panel state).
- **State stores (zustand):** `store/useStore.js` (285), `store/workspaceStore.js` (657), `src/sessionTabsStore.js` (128; persists tab set to `localStorage` key `tm.sessionTabs.<wsId>`).
- **Two websockets.**
  - **Hub socket** — opened once by `App.jsx:309-338`; sends `list_sessions` (`App.jsx:259/388/396/400/465`) and `get_open_sessions` (`App.jsx:343`); consumes `sessions_list`, `open_sessions`, etc.
  - **Per-session socket** — one per `SessionTab`; `SessionTab.jsx:314` opens `ws://${hostname}:${VITE_BACKEND_PORT||8000}/ws` and on `onopen` sends `load_session {session_id}` or `new_session {mode}` (`SessionTab.jsx:386-418`). Every tab, including inactive/deferred ones, opens its own socket and re-sends `load_session`.
  - Client→server commands the in-scope UI sends include `start_session`, `continue_session`, `pause_session`, `apply_config`, `set_default_config`, `save_provider`, `delete_provider`, `get_providers`, `get_available_tools`, `load_more_messages`, `rename_session`, `delete_session`, `security_response`, `rebuild_container`. Server→client events consumed include `state_changed`, `tokens_updated`, `context_updated`, `conversation_changed`, `session_loaded`, `config_changed`, `providers_list`, `tools_list`, `default_config_saved`, `session_*`, `security_prompt`, `session_stop`, and the generic `worker:*` fan-out.
- **Backend:** FastAPI — `server.py` (3979 lines; HTTP + the `/ws` hub) plus 11 routers (`config_routes`, `provider_routes`, `session_routes`, `workspace_routes`, `global_routes`, `prompt_routes`, `logging_routes`, `onboarding_routes`, `health_routes`, `vault_routes`, `container-records`). ~76 HTTP routes + the single WS endpoint. WS command dispatch is an **if/elif chain on `command`** (not a table).
- **Canonical API base = relative/proxied.** `vite.config.js` proxies `/ws` → `ws://127.0.0.1:8000` and `/api` → `http://127.0.0.1:8000`; the preferred frontend convention is `API_BASE=''` + bare `/api/...` (used by `globalApi.js`, `FolderBrowser`, `workspaceApi.js`, `workspaceStore.js`, `WorkerManagementPanel`, `SessionSidebar`, `OnboardingWizard`, `DomainAllowlistEditor`, `ContainerPanel`, `RecordContainerPanel`, `DockerfileEditor`, `WorkspaceSelector`).
- **Three coexisting conventions (cleanup target):** (A) relative/proxied — preferred; (B) absolute `http://${hostname}:${VITE_BACKEND_PORT||8000}` which **bypasses** the Vite proxy (`App.jsx:50/1102/1133/1137/1171/1333`, `ConfigPanel.jsx:47-48` used at `:182/209/316/369`, `PromptLibrary.jsx:3`); (C) absolute `ws://${hostname}:${WS_PORT||8000}/ws` (`App.jsx:51` → `:251`, `SessionTab.jsx:38` → `:314`). Only a proxy/port mismatch breaks these; no literal hard-coded host exists.
- **WS is the LIVE provider/rename/capabilities transport.** Providers, session rename, and workspace capabilities flow over WS (`save_provider`/`delete_provider`/`get_providers`; `rename_session`; `get_workspace_capabilities`/`bootstrap_workspace`); several REST equivalents exist but are **unclaimed** (see Register 4).

## Register 1 — DEAD and PARTIAL UI elements

All DEAD/PARTIAL rows from chunks A/B/C/D/E, sorted by view, plus the two shared-widget stub-callback findings (rows marked ▣ dedup the same elements as the Chunk-A rows above them).

| Element | View/layer | file:line | Verdict | Reason |
|---|---|---|---|---|
| docker-down banner | Landing / App shell (A) | App.jsx:1248 | DEAD | Display-only banner; **no dismiss control** |
| ⚙ Manage Providers → modal `sendCommand` | Landing (A) | WorkspaceSelector.jsx:304 | DEAD | `sendCommand={() => {}}` no-op — modal Save/Delete dispatch nowhere |
| Delete confirm (provider) | Landing (A) | ManageProvidersModal.jsx:126 (dispatch L81) | DEAD | `sendCommand('delete_provider')` reaches the landing stub |
| Save (add/edit provider) | Landing (A) | ManageProvidersModal.jsx:66 | DEAD | `sendCommand('save_provider')` reaches the landing stub |
| Save (ProviderEditModal) | Landing (A) | ProviderEditModal.jsx:284 | DEAD | `onSave` → ManageProvidersModal `handleSave` → landing stub (dead transitively) |
| GlobalContainers | Landing / global (A) | GlobalContainers.jsx:1-23 | DEAD | Read-only render, no handlers anywhere |
| Edit (resource) | Landing / global (A) | GlobalResources.jsx:55-57 | DEAD | `disabled title="Coming soon"` — inert |
| prompt row | Landing (A) | PromptLibrary.jsx:365 (handler 145-147) | DEAD | `onSelectPrompt?.()` has no consumer — WorkspaceSelector mounts `<PromptLibrary/>` without `onSelectPrompt` |
| proxy-bypass absolute `:8000` URLs | App shell (A) | App.jsx:1102,1133,1137,1171,1333 | PARTIAL | Hardcoded `http://host:8000` bypasses the Vite proxy |
| session row | Landing / global (A) | GlobalSessions.jsx:41 | PARTIAL | `navigate('/session/'+id)` — legacy route omits `workspaceId` |
| level select | Logging panel (A) | LoggingPanel.jsx:234 | PARTIAL | Local state only; needs separate Apply |
| tags input | Logging panel (A) | LoggingPanel.jsx:254 | PARTIAL | Local state only; needs Apply |
| tag checkbox | Logging panel (A) | LoggingPanel.jsx:277 | PARTIAL | Local state only; needs Apply |
| tag level select | Logging panel (A) | LoggingPanel.jsx:284 | PARTIAL | Local state only; needs Apply |
| truncation number | Logging panel (A) | LoggingPanel.jsx:320 | PARTIAL | Local state only; needs Apply |
| hostResources checkbox | Onboarding wizard (A) | OnboardingWizard.jsx:485 | PARTIAL | Sets local state; **never sent/persisted** |
| config API_BASE | Prompt library (A) | PromptLibrary.jsx:3 | PARTIAL | Absolute `http://${hostname}:${port}` bypasses the proxy |
| TTY informational card | Workspace detail (B) | WorkspaceDetailPage.jsx:189-190 | DEAD | Static "No ceiling control." copy; no handler |
| JTAG informational card | Workspace detail (B) | WorkspaceDetailPage.jsx:189-190 | DEAD | Static copy; no handler |
| Session Defaults tab body | Workspace detail (B) | WorkspaceDetailPage.jsx:44,49-56 | DEAD | Pure placeholder ("configurable here soon"), no controls |
| Credentials tab body | Workspace detail (B) | WorkspaceDetailPage.jsx:45,49-56 | DEAD | Pure placeholder ("coming soon"), no controls |
| `provider_deleted` event | Session (C) | server.py:1659 | PARTIAL | Emitted; frontend handler is a deliberate `console.log` no-op (list refresh relies on sibling `providers_list` `:1663`) |
| `default_config_saved` event | Session (C) | server.py:1506/1517/1524 | PARTIAL | SessionTab stores it (`SessionTab.jsx:837-843`) but the visible status is rendered by ConfigPanel (D) |
| Save-as-default key loss | Config panel (D) | server.py:203 / config_manager.py:119-126 | PARTIAL | Only 6 keys persist; mode/tools/token limits/thresholds/workspace_path/session_permissions silently dropped |
| Save-as-default success signal | Config panel (D) | ConfigPanel.jsx:110-126 | PARTIAL | Success is a 2.5 s transient label that reverts |
| Stale-session send drop | Config panel (D) | SessionTab.jsx:250 | PARTIAL | `sendCommand` silently drops the click (`console.warn`) when `staleSessionRef.current` |
| DockerfileEditor section | Config panel / WorkspacePanel (D) | WorkspacePanel.jsx (~L178) | PARTIAL | Section rendered; child internals not read this pass |
| WorkerManagementPanel section | Config panel / WorkspacePanel (D) | WorkspacePanel.jsx (~L192) | PARTIAL | Section rendered; child internals not read this pass |
| Stop (per worker) | Worker UI / SessionSidebar (E) | SessionSidebar.jsx:66-88 (fetch :73) | PARTIAL | POST omits `instance_id`/`session_id` → stops **all** instances of that worker name, inconsistent with E4/F7 |
| ▣ ManageProvidersModal (shared) | Shared widget | ManageProvidersModal.jsx:231; host WorkspaceSelector.jsx:302-306 | DEAD | Landing host passes no-op `sendCommand`; ConfigPanel host (`:605-610`) is real → PARTIAL host contract (same element as landing row above) |
| ▣ PromptLibrary (shared) | Shared widget | PromptLibrary.jsx:424; host WorkspaceSelector.jsx:250 | DEAD | Landing host omits `onSelectPrompt` so prompt-row click is a silent no-op; ConfigPanel host (`:900/921`) is real (same element as landing row above) |

Rows: 8 DEAD + 9 PARTIAL (Chunk A) + 4 DEAD (B) + 2 PARTIAL (C) + 5 PARTIAL (D) + 1 PARTIAL (E) = 29; + 2 shared-widget rows (▣, dedup) = **31 rows**.

## Register 2 — ORPHANED frontend code

18 of 63 non-test frontend source files are unreachable from `index.html` → `main.jsx` → `App.jsx`. Direct orphans carry the marker `// --- ORPHANED — replaced by WorkspaceDetailPage; do not import in new code ---` on line 1.

| # | path | lines | what it is | why unreachable |
|---|---|---|---|---|
| 1 | src/components/workspace/WorkspacePanel.jsx | 303 | legacy tabbed workspace shell | marker present; zero live importers (only test importers) |
| 2 | src/components/workspace/tabs/ContainersTab.jsx | 89 | tab | imported only by (1) |
| 3 | src/components/workspace/tabs/CredentialsTab.jsx | 43 | tab | imported only by (1) |
| 4 | src/components/workspace/tabs/PermissionsTab.jsx | 54 | tab | imported only by (1) |
| 5 | src/components/workspace/tabs/ResourcesTab.jsx | 58 | tab | imported only by (1) |
| 6 | src/components/workspace/tabs/SessionDefaultsTab.jsx | 151 | tab | imported only by (1) |
| 7 | src/components/workspace/tabs/ToolsTab.jsx | 46 | tab | imported only by (1) |
| 8 | src/components/workspace/tabs/WorkersTab.jsx | 81 | tab | imported only by (1) |
| 9 | src/components/workspace/modals/ResourceCatalogModal.jsx | 74 | modal (marker present) | imported only by orphan ResourcesTab (5) |
| 10 | src/components/workspace/workspaceUtils.jsx | 97 | helpers | imported only by (1) and orphan tabs/modals (2–9) |
| 11 | src/components/workspace/modals/ContainerLogsModal.jsx | 10 | modal | imported only by orphan ContainersTab (2) |
| 12 | src/components/workspace/modals/CredentialPickerModal.jsx | 60 | modal | imported only by orphan CredentialsTab (3) |
| 13 | src/components/workspace/modals/WorkerEditorModal.jsx | 136 | modal | imported only by orphan WorkersTab (8) |
| 14 | src/components/SessionCreationModal.jsx | 562 | session-creation modal | zero importers; itself imports FolderBrowser + WorkspaceSessionList |
| 15 | src/components/SessionList.jsx | 256 | session list | zero importers (other grep hits are the substring "SessionListItem" inside `apiContracts.js` strings) |
| 16 | src/components/WorkspaceSessionList.jsx | 369 | session list | imported only by orphan SessionCreationModal (14) |
| 17 | src/data/apiContracts.js | 291 | "PURE DOCUMENTATION" module | zero importers |
| 18 | src/types/workspace.ts | 98 | TS type doc | zero importers (self-comment "Nothing imports this file yet") |

**Trap:** the live `src/components/WorkspacePanel.jsx` (204 L, imported by `ConfigPanel.jsx`) must **not** be confused with orphan (1) `src/components/workspace/WorkspacePanel.jsx` (303 L).

**Dead-code tests (import orphan components):**

| Test | lines | status | covers |
|---|---|---|---|
| src/components/__tests__/WorkspaceSessionList.test.jsx | 248 | PURE DEAD-CODE (imports only orphan WorkspaceSessionList) | empty states, rendering, interactions, deletion |
| src/components/__tests__/WorkspaceIntegration.test.jsx | 307 | DEAD-CODE for the component (imports orphan workspace/WorkspacePanel.jsx + live workspaceStore) | loading/header, tab navigation, edits-reach-store, New Session flow, safety advisory |
| src/components/__tests__/WorkspacePanel.test.jsx | 736 | MIXED/PARTIAL (imports live root WorkspacePanel **and** orphan workspace/WorkspacePanel + all 7 orphan tabs) | root sections live; "Phase 4 tabs" `L482-686` and "tabbed panel" describes exercise DEAD code |

No test exists for `SessionCreationModal.jsx` or `SessionList.jsx`. Related: backend-provided orphan REST routes (only orphan/test callers) — `GET .../containers/{name}/logs` (`server.py:3621`, only `ContainerLogsModal.jsx`) and `GET /api/session/{id}` (`session_routes:245`, only `WorkspaceSessionList.jsx:88`).

## Register 3 — WebSocket mismatches

| Gap | Command / event | Side | file:line | Note |
|---|---|---|---|---|
| No sender | `stop_session` | backend handler | server.py:1079 | No Stop button exists; `QueryBar.jsx:7-13` docstring is stale |
| No sender | `resume_session` | backend handler | server.py:1074 | Never sent (`continue_session` used instead) |
| No sender | `get_config` | backend handler | server.py:1084 | 0 grep hits in FE |
| No sender | `get_conversation` | backend handler | server.py:1102 | 0 grep hits |
| No sender | `save_session` | backend handler | server.py:1734 | 0 grep hits |
| No sender | `close_session` | backend handler | server.py:2154 | Only a comment; never actually sent |
| No sender | `set_project` | backend handler | server.py:2357 | 0 grep hits |
| No sender | `get_workspace_capabilities` | backend handler | server.py:2536 | 0 grep hits |
| No sender | `bootstrap_workspace` | backend handler | server.py:2564 | 0 grep hits |
| Emitted, never consumed | `workspace_capabilities` | backend event | server.py:2553 | No FE `case` (paired with unclaimed `get_workspace_capabilities`) |
| Emitted, never consumed | `workspace_bootstrapped` | backend event | server.py:2575 | No FE `case` (paired with unclaimed `bootstrap_workspace`) |
| Handled, never emitted | `session_cleared` | frontend handler | SessionTab.jsx:974 | Backend grep = never emitted (`server.py:75` docstring likewise dead) |
| Name mismatch (doc only) | `open_sessions_list` vs `open_sessions` | doc vs code | server.py:73 (doc) / server.py:2148 (emit) / App.jsx:402 (listen) | Docstring names an event the code never emits |

Related `PARTIAL` two-way quirks (rows also in Register 1): `provider_deleted` emitted `server.py:1659` but FE handler is a deliberate no-op; `default_config_saved` stored by SessionTab (`SessionTab.jsx:837-843`) but rendered by ConfigPanel. Note that `session_stop` (inbound event, `bridge.py:2587`) is distinct from the never-sent `stop_session` command. Related: `update_config` appears only in a docstring (`server.py:40`) with **no** handler branch and no sender — a MISMATCH.

Rows: **13** (9 unsent commands + 2 unconsumed events + 1 dead handler + 1 doc-name mismatch).

## Register 4 — Unclaimed backend capability

Backend surface with no live UI caller — the operator's list for NEW feature work. All "file" references are under `web_ui/backend/`.

**REST routes — 16 unclaimed** (a UI fix would add a component/handler; each line names what it touches):

| # | Method + path | backend@ | family | a UI fix would touch |
|---|---|---|---|---|
| 1 | POST `/api/config/reset` | config_routes:71 | config | a "Reset config" action |
| 2 | POST `/api/config/mode` | config_routes:112 | config | a mode switch (mode is currently only passed inside `start/continue_session`) |
| 3 | GET `/api/config/mode` | config_routes:179 | config | a config read-back |
| 4 | GET `/api/providers` | provider_routes:70 | provider | landing list — the defect-(b) bug #2 fix path |
| 5 | POST `/api/providers` | provider_routes:80 | provider | provider REST CRUD (WS path used + landing stub) |
| 6 | DELETE `/api/providers/{id}` | provider_routes:111 | provider | provider REST CRUD (WS path used + landing stub) |
| 7 | GET `/api/system/health` | health_routes:182 | health | App hits `/api/health` instead |
| 8 | GET `/api/session/{id}` | session_routes:245 | session | raw detail GET (WS `load_session` used; only orphan `WorkspaceSessionList.jsx:88`) |
| 9 | POST `/api/session/{id}/rename` | session_routes:281 | session | REST rename (WS `rename_session` used) |
| 10 | GET `/api/workspace/{id}/mcp_servers` | workspace_routes:770 | workspace | an MCP-server listing panel (no consumer) |
| 11 | POST `/api/workspace/{id}/workers/stop_all` | workspace_routes:1055 | workers | a "Stop all workers" button |
| 12 | GET `/api/workspace/{id}/permissions` | workspace_routes:1430 | workspace | a GET-permissions read (only tests + PUT path used) |
| 13 | POST `/api/workspace` (create) | workspace_routes:1518 | workspace | UI routes creation via `browse/create`×2 + `workspace/resolve` instead |
| 14 | GET `/api/workspace/{id}` (detail) | workspace_routes:1774 | workspace | UI uses `/summary`/`/list`; bare detail unused |
| 15 | GET `/health` (bare) | server.py:2850 | health | UI uses `/api/health` |
| 16 | GET `/api/workspace/{id}/containers/{name}/status` | server.py:3164 | containers | no live caller |

**WebSocket commands — 9 unclaimed** (accepted by `server.py`, never sent by the UI):

| # | command | backend@ | family | a UI fix would touch |
|---|---|---|---|---|
| 1 | `get_config` | server.py:1084 | session/config | a config-refresh request |
| 2 | `get_conversation` | server.py:1102 | session | a full-conversation fetch |
| 3 | `stop_session` | server.py:1079 | session control | a **Stop** button (see Register 3) |
| 4 | `resume_session` | server.py:1074 | session control | a **Resume** button |
| 5 | `save_session` | server.py:1734 | session | an explicit save flow |
| 6 | `close_session` | server.py:2154 | session | an explicit close flow |
| 7 | `set_project` | server.py:2357 | workspace | a project selector |
| 8 | `get_workspace_capabilities` | server.py:2536 | workspace | a capabilities panel (its event is also unconsumed) |
| 9 | `bootstrap_workspace` | server.py:2564 | workspace | a bootstrap action (its event is also unconsumed) |

Rows: **25** (16 REST + 9 WS).

## Test coverage notes

- **Landing / global shell (A):** covered by `PromptLibrary`, `OnboardingWizard`, `GlobalResources`, `WorkspaceSelector` tests, but **`WorkspaceSelector.test.jsx` stubs `providers: []` and asserts only the heading** — it cannot catch defect (b). **No test exists for `ManageProvidersModal` or `ProviderEditModal`.**
- **Config panel (D):** `ConfigPanel.test.jsx:175-195` **MOCKS `sendCommand`**, so it proves only the frontend wiring (`('set_default_config', {config:any})` and the three button labels); the backend handler and persistence are untested. `ConfigPanelDraft.test.jsx` covers draft state.
- **Session (C):** WS command/event handling is exercised via the real-`App` integration suites; note the **stale-session drop** (`SessionTab.jsx:250`) and `default_config_saved` render path are not asserted end-to-end.
- **Worker UI (E):** strong per-event coverage (`adaptWorkerEvent.test.js` per-event describes; `WorkerOutputPanel.test.jsx` isolation/stale-header/status-dot; `WorkerPanelArea.test.jsx`; `SessionWorkerIsolation.test.jsx`; `workerEventRouting.test.jsx`). Gaps: `WorkerManagementPanel.test.jsx` covers only the "deliverable 1 tray" (`L76`); the create/edit form fields are not field-by-field tested.
- **Backend:** `test_ws_mock_provider.py` is **0 bytes** — no WS provider test. (`__tests__/`)
- **Orphan/dead-code tests:** `WorkspaceSessionList.test.jsx` (pure dead-code), `WorkspaceIntegration.test.jsx` (asserts the orphan tabbed panel), and `WorkspacePanel.test.jsx` (mixed — root live, "Phase 4 tabs" `L482-686` dead) give **false-green** coverage of unreachable UI.

## Recommended fix order

Suggested (not a mandate), highest value first:

1. Fix defect (b) bug #1: real `sendCommand` at `WorkspaceSelector.jsx:304`.
2. Fix defect (b) bug #2: populate the landing list — add `providers` to `global_routes.py:234-238` or fetch `GET /api/providers` (provider_routes.py:70).
3. Make defect (a) honest: broaden `GLOBAL_DEFAULT_KEYS` (`config_manager.py:119-126`, filter `server.py:203`) and/or surface ignored keys; replace the transient label (`ConfigPanel.jsx:110-126`).
4. Surface the silent stale-session drop (`SessionTab.jsx:250`) instead of `console.warn`.
5. Fix the two stub callbacks: landing `ManageProvidersModal` (`WorkspaceSelector.jsx:304`) and landing `PromptLibrary` `onSelectPrompt` (`WorkspaceSelector.jsx:250`).
6. Add the missing Stop/Resume senders (`stop_session` server.py:1079, `resume_session` server.py:1074; retire the stale `QueryBar.jsx:7-13` docstring).
7. Make `SessionSidebar` worker Stop pass `instance_id`/`session_id` (`SessionSidebar.jsx:66-88`) to match `WorkerManagementPanel.jsx:634-641` / `WorkerOutputPanel.jsx:902-907`.
8. Decide the orphans: keep + wire, or delete the 18 files (Register 2) and the 3 dead-code tests; also fix/remove the never-emitted `session_cleared` handler (`SessionTab.jsx:974`).
9. Clean up the API-base conventions: migrate the absolute `http://${hostname}:8000` callers (`App.jsx:1102/1133/1137/1171/1333`, `ConfigPanel.jsx:48`, `PromptLibrary.jsx:3`) and `ws://${hostname}:8000/ws` (`App.jsx:51`, `SessionTab.jsx:38`) to the relative/proxied convention.
10. Surface unclaimed backend capability as new features (Register 4) — start with `stop_all` (`workspace_routes.py:1055`) and the provider REST CRUD (`provider_routes.py:70/80/111`).

<!-- APPENDIX-START -->

---

## Per-view element tables — appendix index

The appendices below contain the verbatim per-view element tables produced during the UI audit. Element-row verdict vocabulary is defined in "How to read this document" above. Appendix Z is background/original recon material, retained for provenance rather than as engineer-facing rows.

- **Appendix A** — Landing page and global shell — `.tm-fix-scratch/ui_chunk_a.md`
- **Appendix B** — Workspace layer — `.tm-fix-scratch/ui_chunk_b.md`
- **Appendix C** — Session layer (incl. WebSocket contract table) — `.tm-fix-scratch/ui_chunk_c.md`
- **Appendix D** — ConfigPanel + root WorkspacePanel + save-as-default round trip — `.tm-fix-scratch/ui_chunk_d.md`
- **Appendix E** — Worker UI (incl. worker:* event matrix) — `.tm-fix-scratch/ui_chunk_e.md`
- **Appendix F1** — Orphan register + shared widgets + API-base audit — `.tm-fix-scratch/ui_chunk_f1.md`
- **Appendix F2** — Inverse backend cross-check — `.tm-fix-scratch/ui_chunk_f2.md`
- **Appendix G** — Provider path verification — `.tm-fix-scratch/ui_chunk_g.md`
- **Appendix Z** — Background / original reconstruction (recon) — `.tm-fix-scratch/ui_recon.md`


---

## Appendix A — Landing page and global shell

_Source: .tm-fix-scratch/ui_chunk_a.md (126 element rows). Verdict vocabulary: see "How to read this document"._

# UI Interaction Audit — CHUNK A

## Scope
Frontend root: `web_ui/frontend/src/`. Every **visible interactive element** (buttons, links,
tabs, toggles, checkboxes, selects, inputs, textareas, modal actions, keyboard handlers and
onChange/useEffect/subscription auto-triggers) in the files below, with what each handler
*actually* does and whether a live backend counterpart exists.

### Files read (line counts)
| File | Lines |
|---|---|
| main.jsx | 18 |
| router.js | 51 |
| App.jsx | 1374 |
| components/TabBar.jsx | 73 |
| components/GlobalSessions.jsx | 54 |
| components/GlobalContainers.jsx | 23 |
| components/GlobalResources.jsx | 62 |
| components/GlobalCredentials.jsx | 158 |
| components/PromptLibrary.jsx | 424 |
| components/FolderBrowser.jsx | 400 |
| components/LoggingPanel.jsx | 376 |
| components/OnboardingWizard.jsx | 540 |
| components/ProviderEditModal.jsx | 302 |
| components/ManageProvidersModal.jsx | 231 |
| components/WorkspaceSelector.jsx | 374 |
| components/RecordContainerPanel.jsx | 730 |
| components/workspace/modals/NewSessionModal.jsx | 204 |
| globalApi.js | 84 |
| store/workspaceStore.js | 657 |
| data/apiContracts.js | 291 |

Out of chunk: `DomainAllowlistEditor.jsx` (305).

### Verdict counts
| Verdict | Count |
|---|---|
| WIRED | 109 |
| PARTIAL | 9 |
| DEAD | 8 |
| ORPHANED | 0 |
| UNVERIFIED | 0 |
| **Total** | **126** |

> `main.jsx`, `router.js`, `GlobalContainers.jsx` and `data/apiContracts.js` contain **no user
> interactive elements** (root mount, hash-parse helpers, read-only display, unused doc module).

---

## Group A1 — App shell / routing / banners (`App.jsx`, `TabBar.jsx`)

| # | Visible label | file:line | Element kind | Handler name | What handler ACTUALLY does | Backend counterpart | VERDICT | Evidence |
|---|---|---|---|---|---|---|---|---|
| 1 | Tab (workspace/session tab) | TabBar.jsx:46 | tab div onClick | `onSelectTab` | Calls `onSelectTab(id)` → App L1261 `setActiveTab` + `navigate` | NONE (client routing) | WIRED | TabBar.jsx:46; App.jsx:1261 |
| 2 | ✕ (close tab) | TabBar.jsx:52-54 | button onClick | `onCloseTab` | Calls `onCloseTab(id)` → App L1265 `handleCloseTab` (closes tab in store) | NONE (sessionTabsStore) | WIRED | TabBar.jsx:52-54; App.jsx:1265; sessionTabsStore.test.js closeTab |
| 3 | + (new tab) | TabBar.jsx:62 | button onClick | `onNewTab` | Calls `onNewTab()` → App L1266 navigates to workspace landing | NONE | WIRED | TabBar.jsx:62; App.jsx:1266 |
| 4 | Logging | TabBar.jsx:67 | button onClick | `onLoggingClick` | App L1267 `setShowLoggingPanel(true)` | NONE (opens panel) | WIRED | TabBar.jsx:67; App.jsx:1267 |
| 5 | ✕ (backend-down banner dismiss) | App.jsx:1241 | button onClick | inline | `setBackendBannerDismissed(true)` (local state only) | NONE | WIRED | App.jsx:1236-1246 |
| 6 | docker banner | App.jsx:1248 | banner (no button) | — | Display-only; **no dismiss control** | NONE | DEAD | App.jsx:1248 |
| 7 | (auto) onboarding status | App.jsx:1167-1185 | useEffect | inline | GET `/api/onboarding/status` → `setOnboardingDone` | GET /api/onboarding/status onboarding_routes.py:86 | WIRED | App.jsx:1167-1185; onboarding_routes.py:86 |
| 8 | (auto) backend health poll | App.jsx:1158-1162 | useEffect setInterval | `checkBackendHealth` | GET `/api/health` then `/api/health/containers` every 10s (L1126-1156) | GET /api/health, /api/health/containers | WIRED | App.jsx:1126-1162 |
| 9 | (auto) hub WS subscription | App.jsx:309-338 | useEffect WS | inline | Opens workspace hub WS, routes messages | WS hub (server.py) | WIRED | App.jsx:309-338 |
| 10 | (auto) get_open_sessions | App.jsx:341-346 | useEffect | inline | On hub connect sends `get_open_sessions` | WS get_open_sessions server.py:2132 | WIRED | App.jsx:341-346; server.py:2132 |
| 11 | (auto) logging config load | App.jsx:1098-1107,1188-1190 | useEffect | `fetchLoggingConfig` | GET `/api/logging/config` | GET /api/logging/config logging_routes.py:33 | WIRED | App.jsx:1098-1107; logging_routes.py:33 |
| 12 | (auto) persist active session | App.jsx:1194-1198 | useEffect | inline | Persist `activeSessionId` (localStorage) | NONE | WIRED | App.jsx:1194-1198 |
| 13 | (proxy bypass) absolute :8000 URLs | App.jsx:1102,1133,1137,1171,1333 | fetch literal | inline | Uses `http://host:8000/...` hardcoded, bypassing Vite proxy | — | PARTIAL | App.jsx:1102,1133,1137,1171,1333 |

---

## Group A2 — WorkspaceSelector landing (`WorkspaceSelector.jsx`, `globalApi.js`)

| # | Visible label | file:line | Element kind | Handler name | What handler ACTUALLY does | Backend counterpart | VERDICT | Evidence |
|---|---|---|---|---|---|---|---|---|
| 14 | workspace card | WorkspaceSelector.jsx:187 | card onClick | inline | `navigate(...)` to workspace | NONE (client routing) | WIRED | WorkspaceSelector.jsx:187 |
| 15 | workspace card (keyboard) | WorkspaceSelector.jsx:188-193 | onKeyDown Enter/Space | inline | Same navigation on Enter/Space | NONE | WIRED | WorkspaceSelector.jsx:188-193 |
| 16 | + New Workspace | WorkspaceSelector.jsx:163 | button onClick | `openCustomModal` | Opens custom-path modal, fetches `/api/user-home` (L86) | GET /api/user-home server.py:2844 | WIRED | WorkspaceSelector.jsx:80,163; server.py:2844 |
| 17 | Refresh | WorkspaceSelector.jsx:166 | button onClick | `loadSummary` | `fetchGlobalSummary()` → GET `/api/global/summary` | GET /api/global/summary global_routes.py:244 | WIRED | WorkspaceSelector.test.jsx:212-213 (Refresh refetch 2x) |
| 18 | (auto) load summary | WorkspaceSelector.jsx:70-72 | useEffect | `loadSummary` | `fetchGlobalSummary()` GET `/api/global/summary` | GET /api/global/summary global_routes.py:244 | WIRED | WorkspaceSelector.test.jsx:149; global_routes.py:244 |
| 19 | + New Session | WorkspaceSelector.jsx:221 | button onClick | `openNewSession` | 1 workspace → open NewSessionModal direct; else chooser | NONE (opens modal) | WIRED | WorkspaceSelector.test.jsx:278-285,295-303 |
| 20 | (chooser) workspace select | WorkspaceSelector.jsx:277 | select onChange | inline | `setChooserWsId(value)` | NONE | WIRED | WorkspaceSelector.jsx:277 |
| 21 | (chooser) Cancel | WorkspaceSelector.jsx:286 | button onClick | inline | Closes chooser | NONE | WIRED | WorkspaceSelector.jsx:286 |
| 22 | (chooser) Create Session | WorkspaceSelector.jsx:289 | button onClick | `confirmChooser` | Opens NewSessionModal for chosen workspace | NONE | WIRED | WorkspaceSelector.jsx:148-153,289 |
| 23 | ⚙ Manage Providers | WorkspaceSelector.jsx:255 | button onClick | inline | `setShowProviders(true)` (opens modal) | NONE (opens modal) | WIRED | WorkspaceSelector.jsx:255 |
| 24 | (ManageProvidersModal wiring) | WorkspaceSelector.jsx:304 | prop `sendCommand` | `() => {}` | **NO-OP STUB** — modal save/delete do nothing here | (should be WS save_provider/delete_provider server.py:1571,1629) | DEAD | WorkspaceSelector.jsx:301-308 |
| 25 | Folder select (custom modal) | WorkspaceSelector.jsx:117 | FolderBrowser onSelect | `handleFolderSelect` | localStorage + resolveWorkspacePath → POST `/api/workspace/resolve` | POST /api/workspace/resolve workspaceStore.js:292 | WIRED | WorkspaceSelector.jsx:117; workspaceStore.js:292 |
| 26 | risk ack checkbox | WorkspaceSelector.jsx:345 | checkbox onChange | inline | `setAcknowledgedRisk(checked)` (local) | NONE | WIRED | WorkspaceSelector.jsx:345 |
| 27 | Proceed (sensitive) | WorkspaceSelector.jsx:352 | button onClick | `handleProceedSensitive` | Proceeds after ack (see L128) | NONE | WIRED | WorkspaceSelector.jsx:128,352 |
| 28 | Cancel (custom modal) | WorkspaceSelector.jsx:362 | button onClick | inline | Closes custom modal | NONE | WIRED | WorkspaceSelector.jsx:362 |

---

## Group A3 — ManageProvidersModal (`ManageProvidersModal.jsx`)

| # | Visible label | file:line | Element kind | Handler name | What handler ACTUALLY does | Backend counterpart | VERDICT | Evidence |
|---|---|---|---|---|---|---|---|---|
| 29 | + Add Provider | ManageProvidersModal.jsx:219 | button onClick | `handleAdd` | Opens ProviderEditModal (add mode) | NONE | WIRED | ManageProvidersModal.jsx:219 |
| 30 | Edit (row) | ManageProvidersModal.jsx:183 | button onClick | `handleEdit` | Opens ProviderEditModal (edit mode) | NONE | WIRED | ManageProvidersModal.jsx:183 |
| 31 | Delete (row) | ManageProvidersModal.jsx:192 | button onClick | `handleDeleteClick` | `setConfirmDeleteId(id)` → confirm overlay | NONE | WIRED | ManageProvidersModal.jsx:192 |
| 32 | Delete confirm | ManageProvidersModal.jsx:126 | button onClick | `handleConfirmDelete` | `sendCommand('delete_provider',{provider_id})` (L81) | WS delete_provider server.py:1629 (via stub → dead) | DEAD | ManageProvidersModal.jsx:79-85; server.py:1629 |
| 33 | Cancel delete | ManageProvidersModal.jsx:119 | button onClick | `handleCancelDelete` | Clears confirm id | NONE | WIRED | ManageProvidersModal.jsx:119 |
| 34 | Save (add/edit provider) | ManageProvidersModal.jsx:66 | onSave prop | `handleSave` | `sendCommand('save_provider',{provider})` (L66) | WS save_provider server.py:1571 (via stub → dead) | DEAD | ManageProvidersModal.jsx:65-69; server.py:1571 |
| 35 | Close / ✕ / backdrop | ManageProvidersModal.jsx:140,145,212 | button/backdrop onClick | `onClose` | Closes modal | NONE | WIRED | ManageProvidersModal.jsx:140,145,212 |

---

## Group A4 — ProviderEditModal (`ProviderEditModal.jsx`)

| # | Visible label | file:line | Element kind | Handler name | What handler ACTUALLY does | Backend counterpart | VERDICT | Evidence |
|---|---|---|---|---|---|---|---|---|
| 36 | id input | ProviderEditModal.jsx:147 | input onChange | inline | Local field edit | NONE | WIRED | ProviderEditModal.jsx:147 |
| 37 | label input | ProviderEditModal.jsx:159 | input onChange | inline | Local | NONE | WIRED | ProviderEditModal.jsx:159 |
| 38 | type select | ProviderEditModal.jsx:170 | select onChange | inline | Local | NONE | WIRED | ProviderEditModal.jsx:170 |
| 39 | baseUrl input | ProviderEditModal.jsx:186 | input onChange | inline | Local | NONE | WIRED | ProviderEditModal.jsx:186 |
| 40 | apiKey input | ProviderEditModal.jsx:199 | input onChange | inline | Local | NONE | WIRED | ProviderEditModal.jsx:199 |
| 41 | Show/Hide apiKey | ProviderEditModal.jsx:204 | button onClick | `setShowApiKey` | Toggles key visibility | NONE | WIRED | ProviderEditModal.jsx:204 |
| 42 | defaultModel input | ProviderEditModal.jsx:239 | input onChange | inline | Local | NONE | WIRED | ProviderEditModal.jsx:239 |
| 43 | models textarea | ProviderEditModal.jsx:251 | textarea onChange | inline | Local | NONE | WIRED | ProviderEditModal.jsx:251 |
| 44 | timeout input | ProviderEditModal.jsx:265 | input onChange | inline | Local | NONE | WIRED | ProviderEditModal.jsx:265 |
| 45 | Save | ProviderEditModal.jsx:284 | button onClick | `handleSubmit` | Builds payload (L110-121) → `onSave` → ManageProvidersModal → **stub** | WS save_provider server.py:1571 (dead transitively) | DEAD | ProviderEditModal.jsx:284; ManageProvidersModal.jsx:66 |
| 46 | Cancel / ✕ / backdrop | ProviderEditModal.jsx:125,129,272 | button/backdrop onClick | `onCancel` | Closes modal | NONE | WIRED | ProviderEditModal.jsx:125,129,272 |
| 47 | (auto) hydrate on open | ProviderEditModal.jsx:69 | useEffect | inline | Populates fields from `provider` prop | NONE | WIRED | ProviderEditModal.jsx:69 |

---

## Group A5 — Global Sessions / Containers / Resources

| # | Visible label | file:line | Element kind | Handler name | What handler ACTUALLY does | Backend counterpart | VERDICT | Evidence |
|---|---|---|---|---|---|---|---|---|
| 48 | session row | GlobalSessions.jsx:41 | button onClick | inline | `navigate('/session/'+id)` — **legacy route, omits workspaceId** | NONE (client routing) | PARTIAL | GlobalSessions.jsx:41 |
| 49 | (GlobalContainers) | GlobalContainers.jsx:1-23 | read-only render | — | No handlers; pure display | NONE | DEAD | GlobalContainers.jsx (whole file) |
| 50 | (auto) load resource catalog | GlobalResources.jsx:12,15 | useEffect | `fetchResourceCatalog` | GET `/api/resource-catalog` | GET /api/resource-catalog server.py:2923 | WIRED | GlobalResources.jsx:12-15; GlobalResources.test.jsx:64-104 |
| 51 | Edit (resource) | GlobalResources.jsx:55-57 | button (disabled) | — | `disabled title="Coming soon"` — inert | NONE | DEAD | GlobalResources.test.jsx:80-82 |

---

## Group A6 — GlobalCredentials (`GlobalCredentials.jsx`)

| # | Visible label | file:line | Element kind | Handler name | What handler ACTUALLY does | Backend counterpart | VERDICT | Evidence |
|---|---|---|---|---|---|---|---|---|
| 52 | (auto) load credentials | GlobalCredentials.jsx:24-26 | useEffect | `fetchCredentials` | GET `/api/credentials` | GET /api/credentials global_routes.py:312 | WIRED | GlobalCredentials.test.jsx:72; global_routes.py:312 |
| 53 | + Add Credential | GlobalCredentials.jsx:106 | button onClick | inline | `setShowAdd(true)` | NONE | WIRED | GlobalCredentials.jsx:106 |
| 54 | name input | GlobalCredentials.jsx:130 | input onChange | inline | Local | NONE | WIRED | GlobalCredentials.jsx:130 |
| 55 | secret input | GlobalCredentials.jsx:140 | input onChange | inline | Local | NONE | WIRED | GlobalCredentials.jsx:140 |
| 56 | Save (submit form) | GlobalCredentials.jsx:148 | form onSubmit | `submitAdd` | Requires name+secret (L39) → `createCredential({name,secret})` POST `/api/credentials` | POST /api/credentials global_routes.py:329 | WIRED | GlobalCredentials.test.jsx:158-199; global_routes.py:329 |
| 57 | Cancel | GlobalCredentials.jsx:145 | button onClick | `closeAdd` | Closes add modal | NONE | WIRED | GlobalCredentials.test.jsx:221-231 |
| 58 | Add-modal overlay / stopProp | GlobalCredentials.jsx:116,121 | overlay onClick | `closeAdd` | Overlay closes; inner stops propagation | NONE | WIRED | GlobalCredentials.jsx:116,121 |
| 59 | Delete (row) | GlobalCredentials.jsx:93 | button onClick | inline | `setDeleteTarget(c.name)` | NONE | WIRED | GlobalCredentials.jsx:93 |
| 60 | Confirm delete | GlobalCredentials.jsx:85 | button onClick | `confirmDelete` | `deleteCredential(name)` DELETE `/api/credentials/{name}` | DELETE /api/credentials/{name} global_routes.py:346 | WIRED | GlobalCredentials.test.jsx:97-121; global_routes.py:346 |
| 61 | Cancel delete | GlobalCredentials.jsx:88 | button onClick | inline | `setDeleteTarget(null)` | NONE | WIRED | GlobalCredentials.jsx:88 |

---

## Group A7 — FolderBrowser (`FolderBrowser.jsx`)

| # | Visible label | file:line | Element kind | Handler name | What handler ACTUALLY does | Backend counterpart | VERDICT | Evidence |
|---|---|---|---|---|---|---|---|---|
| 62 | (auto) initial listing | FolderBrowser.jsx:89-116 | useEffect | inline | GET `/api/user-home` (L92,101) | GET /api/user-home server.py:2844 | WIRED | FolderBrowser.jsx:89-116; server.py:2844 |
| 63 | folder row | FolderBrowser.jsx:378 | span onClick | `navigateTo` | GET `/api/browse?path=` (L123) | GET /api/browse server.py:2724 | WIRED | FolderBrowser.jsx:378,123; server.py:2724 |
| 64 | ＋ New Folder | FolderBrowser.jsx:320 | button onClick | `handleCreateFolder` | `window.prompt` (L153) → POST `/api/browse/create` (L158) | POST /api/browse/create server.py:2803 | WIRED | FolderBrowser.jsx:320,151-158; server.py:2803 |
| 65 | ↑ Parent | FolderBrowser.jsx:347 | button onClick | `navigateUp` | GET `/api/browse?path=` parent | GET /api/browse server.py:2724 | WIRED | FolderBrowser.jsx:347 |
| 66 | breadcrumb | FolderBrowser.jsx:309 | span onClick | `navigateTo` | GET `/api/browse?path=` | GET /api/browse server.py:2724 | WIRED | FolderBrowser.jsx:309 |
| 67 | Select This Folder | FolderBrowser.jsx:391 | button onClick | inline | `onSelect(currentPath)` (callback) | NONE | WIRED | FolderBrowser.jsx:391 |

---

## Group A8 — LoggingPanel (`LoggingPanel.jsx`)

| # | Visible label | file:line | Element kind | Handler name | What handler ACTUALLY does | Backend counterpart | VERDICT | Evidence |
|---|---|---|---|---|---|---|---|---|
| 68 | (auto) init local config | LoggingPanel.jsx:64 | useEffect | inline | Seeds local state from `config` prop | NONE | WIRED | LoggingPanel.jsx:64 |
| 69 | level select | LoggingPanel.jsx:234 | select onChange | `handleLevelChange` | Local state only (needs Apply) | NONE | PARTIAL | LoggingPanel.jsx:234 |
| 70 | tags input | LoggingPanel.jsx:254 | input onChange | `handleTagsInputChange` | Local state only | NONE | PARTIAL | LoggingPanel.jsx:254 |
| 71 | tag checkbox | LoggingPanel.jsx:277 | checkbox onChange | `handleTagToggle` | Local state only | NONE | PARTIAL | LoggingPanel.jsx:277 |
| 72 | tag level select | LoggingPanel.jsx:284 | select onChange | `handleTagLevelChange` | Local state only | NONE | PARTIAL | LoggingPanel.jsx:284 |
| 73 | truncation number | LoggingPanel.jsx:320 | input onChange | `handleTruncationChange` | Local state only | NONE | PARTIAL | LoggingPanel.jsx:320 |
| 74 | Apply Changes | LoggingPanel.jsx:347 | button onClick | `handleSave` | `onSaveConfig(config)` → App PUT `/api/logging/config` | PUT /api/logging/config logging_routes.py:53 | WIRED | LoggingPanel.jsx:347; App.jsx:1330-1342; logging_routes.py:53 |
| 75 | Retry | LoggingPanel.jsx:196 | button onClick | `onRetry` | App `fetchLoggingConfig` GET `/api/logging/config` | GET /api/logging/config logging_routes.py:33 | WIRED | LoggingPanel.jsx:196; App.jsx:1329 |
| 76 | Raw Config toggle | LoggingPanel.jsx:367 | button onClick | `setShowRaw` | Toggles raw view (local) | NONE | WIRED | LoggingPanel.jsx:367 |
| 77 | ✕ close | LoggingPanel.jsx:224 | button onClick | `onClose` | Closes panel | NONE | WIRED | LoggingPanel.jsx:224 |

> Note: fields 69-73 **never auto-save**; edits are lost unless *Apply Changes* is pressed (by design, but a UX trap — flagged PARTIAL because edit-only, not persisted on change).

---

## Group A9 — OnboardingWizard (`OnboardingWizard.jsx`)

| # | Visible label | file:line | Element kind | Handler name | What handler ACTUALLY does | Backend counterpart | VERDICT | Evidence |
|---|---|---|---|---|---|---|---|---|
| 78 | Get Started | OnboardingWizard.jsx:348 | button onClick | inline | `setStep(2)` | NONE | WIRED | OnboardingWizard.test.jsx:250-257 |
| 79 | provider select | OnboardingWizard.jsx:364 | select onChange | `changeProvider` | Local | NONE | WIRED | OnboardingWizard.jsx:364 |
| 80 | baseUrl input | OnboardingWizard.jsx:381 | input onChange | inline | Local | NONE | WIRED | OnboardingWizard.jsx:381 |
| 81 | apiKey input | OnboardingWizard.jsx:394 | input onChange | inline | Local | NONE | WIRED | OnboardingWizard.jsx:394 |
| 82 | Show/Hide key | OnboardingWizard.jsx:401 | button onClick | `setShowKey` | Toggle visibility | NONE | WIRED | OnboardingWizard.jsx:401 |
| 83 | model input | OnboardingWizard.jsx:415 | input onChange | inline | Local | NONE | WIRED | OnboardingWizard.jsx:415 |
| 84 | Test connection | OnboardingWizard.jsx:423 | button onClick | `testConnection` | POST `/api/onboarding/test-connection` (L196) | POST /api/onboarding/test-connection onboarding_routes.py:122 | WIRED | OnboardingWizard.test.jsx:260-269; onboarding_routes.py:122 |
| 85 | Save & Continue | OnboardingWizard.jsx:439 | button onClick | `saveProvider` | `sendCommand('save_provider',{provider})` (L224) | WS save_provider server.py:1571 | WIRED | OnboardingWizard.test.jsx:294-310; server.py:1571 |
| 86 | Skip (step 1) | OnboardingWizard.jsx:347,436 | button onClick | `handleSkip` | POST `/api/onboarding/complete` (L324) + `onFinished` (ignores failure) | POST /api/onboarding/complete onboarding_routes.py:92 | WIRED | OnboardingWizard.test.jsx:276-291 |
| 87 | Back (step 2) | OnboardingWizard.jsx:438 | button onClick | inline | `setStep(1)` | NONE | WIRED | OnboardingWizard.jsx:438 |
| 88 | workspaceName input | OnboardingWizard.jsx:459 | input onChange | inline | Local | NONE | WIRED | OnboardingWizard.jsx:459 |
| 89 | workspaceDescription input | OnboardingWizard.jsx:476 | input onChange | inline | Local | NONE | WIRED | OnboardingWizard.jsx:476 |
| 90 | hostResources checkbox | OnboardingWizard.jsx:485 | checkbox onChange | inline | Sets local state; **never sent/persisted** anywhere | NONE | PARTIAL | OnboardingWizard.jsx:485 |
| 91 | Create Workspace | OnboardingWizard.jsx:503 | button onClick | `createWorkspace` | POST `/api/browse/create` (L258,269) then POST `/api/workspace/resolve` (L280) | server.py:2803 + workspaceStore.js:292 | WIRED | OnboardingWizard.jsx:244-280 |
| 92 | Back / Skip (step 3) | OnboardingWizard.jsx:500,502,527,529 | button onClick | handleSkip / inline | Skip completes; Back steps back | onboarding_routes.py:92 | WIRED | OnboardingWizard.jsx:500-529 |
| 93 | Finish | OnboardingWizard.jsx:530 | button onClick | `handleFinish` | `completeOnboarding()` POST `/api/onboarding/complete` (L299) | POST /api/onboarding/complete onboarding_routes.py:92 | WIRED | OnboardingWizard.jsx:298-307 |

---

## Group A10 — NewSessionModal (`workspace/modals/NewSessionModal.jsx`)

| # | Visible label | file:line | Element kind | Handler name | What handler ACTUALLY does | Backend counterpart | VERDICT | Evidence |
|---|---|---|---|---|---|---|---|---|
| 94 | (auto) fetch sessions | NewSessionModal.jsx:40-51 | useEffect | `fetchSessions` | GET `/api/session/list?workspace_id=` | GET /api/session/list session_routes.py:191 | WIRED | NewSessionModal.jsx:40-51; session_routes.py:191 |
| 95 | name input | NewSessionModal.jsx:137 | input onChange | inline | Local | NONE | WIRED | NewSessionModal.jsx:137 |
| 96 | name input (Enter) | NewSessionModal.jsx:139-141 | onKeyDown Enter | `handleCreate` | Triggers create session | POST /api/session/create session_routes.py:110 | WIRED | NewSessionModal.jsx:139-141 |
| 97 | mode button | NewSessionModal.jsx:152 | button onClick | `setMode` | Local | NONE | WIRED | NewSessionModal.jsx:152 |
| 98 | Create Session | NewSessionModal.jsx:164 | button onClick | `handleCreate` | `createSession(workspace.id,{name,mode})` POST `/api/session/create` | POST /api/session/create workspaceStore.js:534 | WIRED | NewSessionModal.jsx:53-77,164; workspaceStore.js:534 |
| 99 | Cancel | NewSessionModal.jsx:163 | button onClick | `onClose` | Closes modal | NONE | WIRED | NewSessionModal.jsx:163 |
| 100 | row Open Session | NewSessionModal.jsx:125,188 | button onClick | `openSession` | Writes LS `activeSessionId` + `window.location.hash` | NONE (client routing) | WIRED | NewSessionModal.jsx:93-96,188 |
| 101 | row Delete | NewSessionModal.jsx:192 | button onClick | `handleDelete` | `window.confirm` (L80) → `deleteSession` DELETE `/api/session/{id}` | DELETE /api/session/{id} workspaceStore.js:571 | WIRED | NewSessionModal.jsx:79-90,192; workspaceStore.js:571 |
| 102 | Close / overlay / stopProp | NewSessionModal.jsx:109,114,124 | button/overlay onClick | `onClose` | Overlay closes modal; inner stops propagation | NONE | WIRED | NewSessionModal.jsx:109,114,124 |

---

## Group A11 — RecordContainerPanel (`RecordContainerPanel.jsx`)

| # | Visible label | file:line | Element kind | Handler name | What handler ACTUALLY does | Backend counterpart | VERDICT | Evidence |
|---|---|---|---|---|---|---|---|---|
| 103 | (auto) load list | RecordContainerPanel.jsx:371-373 | useEffect | `loadList` | GET `/api/container-records` | GET /api/container-records container_record_routes.py:113 | WIRED | RecordContainerPanel.test.jsx:59-71; container_record_routes.py:113 |
| 104 | (auto) poll list | RecordContainerPanel.jsx:376-381 | useEffect setInterval | `loadList` | Re-GET list every POLL_MS (10s) | GET /api/container-records container_record_routes.py:113 | WIRED | RecordContainerPanel.jsx:376-381 |
| 105 | (auto) select → detail+events | RecordContainerPanel.jsx:389-393 | useEffect | `loadDetail`,`loadEvents` | GET `/{id}` + `/{id}/events`?workspace_id | container_record_routes.py:148,175 | WIRED | RecordContainerPanel.test.jsx:145-148 |
| 106 | Refresh Records | RecordContainerPanel.jsx:477 | button onClick | `handleRefresh` | Reloads list/detail/events | container_record_routes.py:113 | WIRED | RecordContainerPanel.test.jsx:242-253 |
| 107 | record row | RecordContainerPanel.jsx:516 | row onClick | inline | `setSelectedId(key)` | NONE | WIRED | RecordContainerPanel.jsx:516 |
| 108 | Close (detail) | RecordContainerPanel.jsx:562 | button onClick | inline | `setSelectedId(null)` | NONE | WIRED | RecordContainerPanel.jsx:562 |
| 109 | action buttons (kill/restart/recreate) | RecordContainerPanel.jsx:684 | button onClick | `performAction(action)` | POST `/{id}/{action}?workspace_id` body `{actor,reason?}` (L420-430) | server.py:3591/3599/3607 | WIRED | RecordContainerPanel.test.jsx:259-261,300-310,472-481; server.py:3591-3607 |
| 110 | reason input | RecordContainerPanel.jsx:721 | input onChange | `setReason` | Local; included in action body only if set | NONE | WIRED | RecordContainerPanel.jsx:721 |
| 111 | drift toggle | RecordContainerPanel.jsx:262 | toggle onClick | `onToggle(key)` | Delegated callback (local UI toggle) | NONE | WIRED | RecordContainerPanel.jsx:262 |

---

## Group A12 — PromptLibrary (`PromptLibrary.jsx`)

| # | Visible label | file:line | Element kind | Handler name | What handler ACTUALLY does | Backend counterpart | VERDICT | Evidence |
|---|---|---|---|---|---|---|---|---|
| 112 | (auto) load prompts | PromptLibrary.jsx:35 | useEffect | `fetchPrompts` | GET `${API_BASE}/api/prompts` (L24) | GET /api/prompts prompt_routes.py:69 | WIRED | PromptLibrary.jsx:24,35; prompt_routes.py:69 |
| 113 | + New Prompt | PromptLibrary.jsx:341 | button onClick | `startCreating` | Opens create form | NONE | WIRED | PromptLibrary.jsx:341 |
| 114 | create name input | PromptLibrary.jsx:301 | input onChange | inline | Local | NONE | WIRED | PromptLibrary.jsx:301 |
| 115 | create textarea | PromptLibrary.jsx:314 | textarea onChange | inline | Local | NONE | WIRED | PromptLibrary.jsx:314 |
| 116 | Save (create) | PromptLibrary.jsx:321 | button onClick | `handleCreate` | POST `/api/prompts/{name}` body `{content}` (L48) | POST /api/prompts/{filename} prompt_routes.py:94 | WIRED | PromptLibrary.jsx:38-64; prompt_routes.py:94 |
| 117 | Cancel (create) | PromptLibrary.jsx:326 | button onClick | `cancelForm` | Closes form | NONE | WIRED | PromptLibrary.jsx:326 |
| 118 | prompt row | PromptLibrary.jsx:365 | row onClick | `handleSelect` | `onSelectPrompt?.(name)` — **no consumer**: WorkspaceSelector renders `<PromptLibrary/>` without `onSelectPrompt` → no-op | NONE | DEAD | PromptLibrary.jsx:145-147,365; WorkspaceSelector.jsx (no onSelectPrompt prop) |
| 119 | ✏️ edit | PromptLibrary.jsx:381 | button onClick | `startEdit` | GET `/api/prompts/{name}` (L123) → open edit | GET /api/prompts/{filename} prompt_routes.py:75 | WIRED | PromptLibrary.jsx:120-128,381; prompt_routes.py:75 |
| 120 | edit textarea | PromptLibrary.jsx:266 | textarea onChange | inline | Local | NONE | WIRED | PromptLibrary.jsx:266 |
| 121 | Save (edit) | PromptLibrary.jsx:272 | button onClick | `handleSaveEdit` | POST `/api/prompts/{name}` (L77) | POST /api/prompts/{filename} prompt_routes.py:94 | WIRED | PromptLibrary.jsx:67-92,272; prompt_routes.py:94 |
| 122 | Cancel (edit) | PromptLibrary.jsx:277 | button onClick | `cancelForm` | Closes edit | NONE | WIRED | PromptLibrary.jsx:277 |
| 123 | 🗑️ delete | PromptLibrary.jsx:388 | button onClick | inline | `setDeleteTarget(name)` | NONE | WIRED | PromptLibrary.jsx:388 |
| 124 | Delete confirm | PromptLibrary.jsx:413 | button onClick | `handleDelete` | DELETE `/api/prompts/{name}` (L100); 403 → "factory locked" | DELETE /api/prompts/{filename} prompt_routes.py:118 | WIRED | PromptLibrary.jsx:95-116,413; prompt_routes.py:118 |
| 125 | Cancel (delete) | PromptLibrary.jsx:416 | button onClick | inline | Clears delete target | NONE | WIRED | PromptLibrary.jsx:416 |
| 126 | (config) absolute API_BASE | PromptLibrary.jsx:3 | fetch base URL | — | `http://${hostname}:${port}` — **bypasses Vite proxy** | — | PARTIAL | PromptLibrary.jsx:3 |

---

## Headline findings

**DEAD (frontend bug — backend exists):**
- `ManageProvidersModal` Save (row 34) & Delete (row 32) and `ProviderEditModal` Save (row 45):
  `WorkspaceSelector.jsx:304` passes `sendCommand={() => {}}` (no-op stub). Backend **does** expose
  `WS save_provider` (server.py:1571), `WS delete_provider` (server.py:1629) **and** REST
  `/api/providers` (provider_routes.py:70/80/111) — the wiring is the missing piece.
- `GlobalResources` Edit button (row 51): `disabled title="Coming soon"`.
- `PromptLibrary` prompt row click (row 118): `onSelectPrompt` prop never supplied by the host.
- `docker banner` (row 6): no dismiss control.
- `GlobalContainers` (row 49): entire panel read-only, no interactive elements.

**PARTIAL:**
- `LoggingPanel` field edits (rows 69-73): local state only, never auto-save (need Apply).
- `OnboardingWizard` hostResources checkbox (row 90): captured, never sent/persisted.
- `GlobalSessions` row (row 48): navigates to legacy `/session/:id` route missing `workspaceId`.
- Proxy bypass via absolute `:8000` URLs: `App.jsx` (row 13) and `PromptLibrary.jsx` (row 126).

**Notable:** `data/apiContracts.js` (291 lines) is a pure documentation module imported nowhere —
**ORPHANED** as a module, but exposes no interactive element so it is not tabulated as a row.

---

## Appendix B — Workspace layer

_Source: .tm-fix-scratch/ui_chunk_b.md (31 element rows). Verdict vocabulary: see "How to read this document"._

# UI Interaction Audit — CHUNK B (Workspace Detail layer)

## Scope
`web_ui/frontend/src/components/workspace/WorkspaceDetailPage.jsx` (810L) **and everything it
renders**: `PermissionsResourcesTab`, `ContainersTab`, `WorkersTab`, `ToolsTab`, `TabPlaceholder`
(all module-level in the same file), the header/host-toggle/Apply chrome, the two modals, plus the
helper/API files it depends on (`useWorkspaceSummary.js`, `workspaceApi.js`) and the shared child
`VaultHealthBanner.jsx`. Every visible interactive element (buttons, links, tabs, switches, selects,
modal actions, auto-triggering useEffect) is recorded with what its handler *actually* does and
whether a live backend counterpart exists.

**Out of scope for this chunk:** App.jsx / landing (chunk A), ConfigPanel + session layer (C/D),
WorkerManagementPanel (chunk E). `DomainAllowlistEditor.jsx` is **NOT** rendered by this layer — it
is imported only by the (live) root `components/WorkspacePanel.jsx:185`, so domain-allowlist UI does
not belong to chunk B.

### Files read (line counts)
| File | Lines | Role |
|---|---|---|
| components/workspace/WorkspaceDetailPage.jsx | 810 | the layer under audit |
| components/workspace/workspaceApi.js | 76 | fetch layer (summary / permissions PUT / tools) |
| components/workspace/useWorkspaceSummary.js | 45 | summary hook (fetch + refetch) |
| components/VaultHealthBanner.jsx | 107 | shared banner (also rendered by WorkspaceSelector/chunk A) |
| components/workspace/modals/NewSessionModal.jsx | 204 | chunk A — note only (mounted here) |
| components/workspace/WorkspaceDetailPage.test.jsx | 627 | test file (assertions cross-checked) |
| components/__tests__/WorkspaceDetailPagePermissions.test.jsx | 347 | test file (assertions cross-checked) |
| web_ui/backend/workspace_routes.py (relevant ranges) | 1809 | summary + PUT permissions handlers |
| web_ui/backend/server.py (relevant ranges) | 3979 | /api/tools + /api/vault/status |
| router.js / App.jsx (route context) | 51 / 1374 | `#/workspaces`→selector, `#/workspace/:id`→this page |

### Verdict counts
| Verdict | Count |
|---|---|
| WIRED | 27 |
| PARTIAL | 0 |
| DEAD | 4 |
| ORPHANED | 0 |
| UNVERIFIED | 0 |
| **Total** | **31** |

> Display-only elements (no handler, correct-by-design) are noted inline, not counted as rows:
> header counts spans `:658-666`, "Unsaved changes" hint `:682`, applyError/applySuccess `:692-697`,
> Overview cards (`:718-753` Root path / Security posture / Host execution / stats), ContainersTab
> rows (dockerfile path `:305-312`, container rows `:320-339`), WorkersTab rows (templates `:378-393`,
> active workers `:402-418`), ToolsTab tool rows `:495-516`.

---

## Group B1 — Page shell / header / modals (`WorkspaceDetailPage.jsx`)

| # | Visible label | file:line | Element kind | Handler name | What handler ACTUALLY does | Backend counterpart | VERDICT | Evidence/notes |
|---|---|---|---|---|---|---|---|---|
| 1 | ← Back to workspaces | WorkspaceDetailPage.jsx:643 | `<a href="#/workspaces">` | none (hash nav) | sets location.hash → router `parseHash` → `{view:'selector'}` → App renders WorkspaceSelector | NONE (client hash route; App.jsx:1317, router.js:12) | WIRED | router.js:12 maps `/workspaces`→selector; test WorkspaceDetailPage.test.jsx:527 asserts href |
| 2 | ← Back to workspaces (empty state) | WorkspaceDetailPage.jsx:614 | `<a href="#/workspaces">` | none (hash nav) | same as row 1, shown when `!workspaceId` (`:611`) | NONE (client hash route) | WIRED | `:611-618` |
| 3 | + New Session | WorkspaceDetailPage.jsx:650-656 | button onClick | inline `() => setShowNewSession(true)` | opens NewSessionModal (`:797-807`) | POST /api/session/create (chunk A) | WIRED | test :568-575 |
| 4 | Host execution toggle | WorkspaceDetailPage.jsx:670-679 | button role=switch onClick | `handleToggle` (:560-570) | ON→`setHostAllowed(false)` (local); OFF→`setShowWarning(true)` (confirm modal). **No request issued here** — persisted by Apply. | PUT /api/workspace/{id}/permissions workspace_routes.py:1466 (via row 5) | WIRED | tests :244-289 |
| 5 | Apply (header) | WorkspaceDetailPage.jsx:683-690 | button onClick, `disabled={!pendingChanges||saving}` | `handleApply` (:589-609) | builds `{permissions: localPermissions, allow_host_resources?}` (host key ONLY if hostChanged) → `updateWorkspacePermissions` → PUT → `refetch()` → success msg | PUT /api/workspace/{id}/permissions workspace_routes.py:1466 | WIRED | test :275-280 asserts body `{permissions, allow_host_resources:true}` |
| 6 | Tab button ×7 (Overview, Permissions & Resources, Containers, Workers, Session Defaults, Tools, Credentials) | WorkspaceDetailPage.jsx:703-714 | button role=tab onClick | inline `() => setActiveTab(tab)` | `setActiveTab(tab)` (pure client view switch) | NONE (client) | WIRED | TABS list :16-24; test :327,416,449,490 |
| 7 | Retry (summary error) | WorkspaceDetailPage.jsx:624 | button onClick | `refetch` (useWorkspaceSummary.js:15) | `setTick(t=>t+1)` → re-GET summary | GET /api/workspace/{id}/summary workspace_routes.py:1645 | WIRED | test :224-242 |
| 8 | Cancel (host-enable modal) | WorkspaceDetailPage.jsx:782-787 | button onClick | inline `() => setShowWarning(false)` | closes modal, no state change | NONE | WIRED | modal `:776-795` |
| 9 | Confirm (host-enable modal) | WorkspaceDetailPage.jsx:789-791 | button onClick | `confirmHostEnable` (:572-577) | `setHostAllowed(true)`; `setShowWarning(false)` (local pending) | PUT …/permissions workspace_routes.py:1466 (via row 5) | WIRED | test :265-270 |
| 10 | VaultHealthBanner mount | WorkspaceDetailPage.jsx:700 | child component | — | renders shared banner (row 30-31 has its own controls) | GET /api/vault/status server.py:2865 | WIRED | shared w/ WorkspaceSelector (chunk A) |
| 11 | NewSessionModal mount | WorkspaceDetailPage.jsx:797-807 | child component, onClose | inline `() => setShowNewSession(false)` | closes modal | POST /api/session/create (chunk A) | WIRED | chunk A owns internals |

---

## Group B2 — Overview tab (`:718-753`)
No interactive controls. Root path `:722`, Security posture string `:732` (exact strings
`POSTURE_SAFE`/`POSTURE_HOST` at `:556-558`), Host-execution state `:737-741`, stat cards `:743-752`
— all render `summary` fields verbatim (test asserts the exact posture strings + "no-dummy-data").
**0 rows.**

---

## Group B3 — Permissions & Resources tab (`PermissionsResourcesTab`, :136-293)

| # | Visible label | file:line | Element kind | Handler name | What handler ACTUALLY does | Backend counterpart | VERDICT | Evidence/notes |
|---|---|---|---|---|---|---|---|---|
| 12 | Git ceiling select | WorkspaceDetailPage.jsx:195-205 | `<select>` onChange | `onPermissionChange`→`handlePermissionChange` (:579-583) | sets localPermissions[git]=value; clears msgs. Persist on Apply. | PUT /api/workspace/{id}/permissions workspace_routes.py:1466 | WIRED | opts banned/ask/read/write_on_feature_branch/write; test :178-181 |
| 13 | Filesystem ceiling select | WorkspaceDetailPage.jsx:195-205 | `<select>` onChange | same | local update | PUT …/permissions :1466 | WIRED | opts banned/ask/read/write; test :182 |
| 14 | Network ceiling select (fallback card) | WorkspaceDetailPage.jsx:195-205 | `<select>` onChange | same | local update | PUT …/permissions :1466 | WIRED | fallback meta :100-108; opts banned/ask/write/outbound; test :226-231 |
| 15 | MCP ceiling select (fallback card) | WorkspaceDetailPage.jsx:195-205 | `<select>` onChange | same | local update | PUT …/permissions :1466 | WIRED | opts banned/connect/full; test :184,190-192 |
| 16 | Host bash ceiling select | WorkspaceDetailPage.jsx:195-205 | `<select>` onChange | same | local update | PUT …/permissions :1466 | WIRED | opts banned/ask/allow; test :195-198 |
| 17 | Git toggle switch | WorkspaceDetailPage.jsx:207-218 | button role=switch onClick | `onPermissionChange(name, enabled?'banned':firstEnabledLevel)` | banned↔first non-banned level (local) | PUT …/permissions :1466 | WIRED | :210-212, :132-134 |
| 18 | Filesystem toggle switch | WorkspaceDetailPage.jsx:207-218 | button role=switch onClick | same | local update | PUT …/permissions :1466 | WIRED | — |
| 19 | Network toggle switch | WorkspaceDetailPage.jsx:207-218 | button role=switch onClick | same | local update | PUT …/permissions :1466 | WIRED | — |
| 20 | MCP toggle switch | WorkspaceDetailPage.jsx:207-218 | button role=switch onClick | same | local update | PUT …/permissions :1466 | WIRED | — |
| 21 | Host bash toggle switch | WorkspaceDetailPage.jsx:207-218 | button role=switch onClick | same | local update | PUT …/permissions :1466 | WIRED | — |
| 22 | Container toggle (boolean ceiling) | WorkspaceDetailPage.jsx:177-186 | button role=switch onClick | `onPermissionChange(name, !enabled)` | real boolean (true/false), not a string (local) | PUT …/permissions :1466 | WIRED | `containerCeilingEnabled` :128-130; test :234-252 asserts `container:true` |
| 23 | TTY informational card ("No ceiling control.") | WorkspaceDetailPage.jsx:189-190 | card, no control | — | renders static copy only; no handler | NONE (validator rejects tty as a ceiling key) | DEAD | :258-272; test Permission :206-209 |
| 24 | JTAG informational card ("No ceiling control.") | WorkspaceDetailPage.jsx:189-190 | card, no control | — | renders static copy only; no handler | NONE | DEAD | :258-272; test Permission :208-209 |
| 25 | Apply Permissions | WorkspaceDetailPage.jsx:287-289 | button onClick, `disabled={!dirty||saving}` | `onApply`=`handleApply` (:589) | same handler as row 5 → PUT merged permissions → refetch | PUT /api/workspace/{id}/permissions workspace_routes.py:1466 | WIRED | test :228,243,278,318 |

---

## Group B4 — Containers tab (`ContainersTab`, :297-343)
Read-only. Dockerfile path/note `:305-312` and active-container rows `:320-339` render
`summary.dockerfile` / `summary.active_containers` verbatim. **No start/stop controls, no handlers.
0 rows** (informational only).

## Group B5 — Workers tab (`WorkersTab`, :367-423)
Read-only. Worker templates `:378-393` and active workers `:402-418` (`normalizeWorkerTemplates`
:347-358, `formatElapsed` :360-365) render `summary.worker_templates` / `summary.active_workers`.
**No pause/resume/stop controls. 0 rows** — although backend worker lifecycle endpoints exist
(see Surprises).

## Group B6 — Tools tab (`ToolsTab`, :436-520)

| # | Visible label | file:line | Element kind | Handler name | What handler ACTUALLY does | Backend counterpart | VERDICT | Evidence/notes |
|---|---|---|---|---|---|---|---|---|
| 26 | (auto) tool list load | WorkspaceDetailPage.jsx:442-459 | useEffect [tick] | inline | `fetchTools()` on mount/dep → `setTools(data.tools)` | GET /api/tools server.py:2761 | WIRED | read-only list `:486-518`; test :431-462 |
| 27 | Retry (tools error) | WorkspaceDetailPage.jsx:473-479 | button onClick | inline `() => setTick(t=>t+1)` | re-runs the fetch effect | GET /api/tools server.py:2761 | WIRED | test :464-484 |

> Tab note text `:488-490` "Tool availability is controlled globally and is read-only from this
> workspace." is display-only.

## Group B7 — Placeholder tabs (`TabPlaceholder` :49-56, `placeholderMessage` :43-47)

| # | Visible label | file:line | Element kind | Handler name | What handler ACTUALLY does | Backend counterpart | VERDICT | Evidence/notes |
|---|---|---|---|---|---|---|---|---|
| 28 | Session Defaults tab body ("Session defaults will be configurable here soon.") | WorkspaceDetailPage.jsx:44,49-56 | static copy | — | nothing — pure placeholder, no controls | NONE | DEAD | rendered via `TabPlaceholder` `:772`; test :530-536 |
| 29 | Credentials tab body ("Workspace credential attachments coming soon.") | WorkspaceDetailPage.jsx:45,49-56 | static copy | — | nothing — pure placeholder, no controls | NONE | DEAD | test :538-539 |

## Group B8 — Shared child `VaultHealthBanner.jsx` (also rendered by WorkspaceSelector / chunk A)

| # | Visible label | file:line | Element kind | Handler name | What handler ACTUALLY does | Backend counterpart | VERDICT | Evidence/notes |
|---|---|---|---|---|---|---|---|---|
| 30 | Vault issue count / ▸▾ expand toggle | VaultHealthBanner.jsx:75-88 | button onClick (`aria-expanded`) | inline `() => setExpanded(p=>!p)` | expands/collapses the issue `<ul>` (`:89-102`); client only, only shown when `issues.length>0` | NONE (client) | WIRED | `:60-104` |
| 31 | (auto) vault status poll | VaultHealthBanner.jsx:26-40 | useEffect [] + setInterval 30s | inline `check()` | `fetchVaultStatus()` on mount and every 30s → banner state | GET /api/vault/status server.py:2865 | WIRED | shared component; mount at WorkspaceDetailPage.jsx:700 |

---

## KEY DELIVERABLE ANSWERS
- **Permission persistence = WIRED (persists).** Both Apply buttons funnel through `handleApply`
  (:589) → `updateWorkspacePermissions` (workspaceApi.js:54) → **PUT /api/workspace/{id}/permissions**
  (workspace_routes.py:1466 `put_workspace_permissions`), which validates via
  `validate_workspace_permissions` and merges into `config.json` (`:1483-1494`), then a `refetch()`
  reloads the summary. **Caveat:** edits are **only** sent when Apply is pressed (dirty-tracking,
  no autosave).
- **Host toggle** persists through the *same* PUT; `allow_host_resources` is included **only** when
  the toggle changed (`:597`), and the backend updates it only when not `None`
  (workspace_routes.py:1491-1492).
- **Stub tabs = "Session Defaults" and "Credentials"** (`TabPlaceholder`, rows 28-29).
- **Workspace stop/pause/resume controls: NONE in this layer.**

## DEAD / PARTIAL / ORPHANED rows (complete)
- DEAD — TTY informational card — WorkspaceDetailPage.jsx:189-190 — renders "No ceiling control." with **no** handler; validator rejects `tty` as a ceiling key (intentional/ informational, but non-functional).
- DEAD — JTAG informational card — WorkspaceDetailPage.jsx:189-190 — as above.
- DEAD — Session Defaults tab body — WorkspaceDetailPage.jsx:44,49-56 — static placeholder copy, no controls, no backend.
- DEAD — Credentials tab body — WorkspaceDetailPage.jsx:45,49-56 — static placeholder copy, no controls, no backend.
- PARTIAL — none.
- ORPHANED — none in this layer. (Note: `components/workspace/WorkspacePanel.jsx`, `components/workspace/tabs/*` and `modals/ResourceCatalogModal.jsx` carry ORPHANED markers, but they are NOT referenced by the live WorkspaceDetailPage layer; exhaustive orphan analysis is chunk F. The **root** `components/WorkspacePanel.jsx` is LIVE.)

## Surprises / cross-cutting notes
- **Two Apply buttons, one handler.** Header "Apply" (:683) and tab "Apply Permissions" (:287) both
  call `handleApply` and both send the merged permission dict (`:585-589` comment confirms intent).
- **No autosave / no unload guard.** Permission + host edits live in `localPermissions`/`hostAllowed`
  until Apply; navigating away silently discards them.
- **Read-only tabs vs. live backend lifecycle ops.** WorkersTab/ContainersTab expose no controls, yet
  the backend has `POST /{ws}/workers/stop_all` (workspace_routes.py:1055), `POST /{ws}/workers/{name}`
  /stop (:1112) /pause (:1155) /resume (:1267) and container ops — a UI gap, not a broken element.
- **Tools tab is global.** `/api/tools` (server.py:2761) derives `enabled` from GLOBAL defaults
  (e.g. host_bash gating), **not** from this workspace's ceiling — the tab candidly says so
  (`:488-490`, workspaceApi.js:67-74).
- **DomainAllowlistEditor is not here** — only root `WorkspacePanel.jsx:185` renders it (root panel is
  live via ConfigPanel), so domain-allowlist UI is out of chunk B.
- **VaultHealthBanner is shared** with the landing layer (WorkspaceSelector, chunk A) — documented here
  with that caveat.
- The permission PUT always sends the **full** local permissions dict; unknown resource names / bad
  levels are rejected 422 (`workspaceApi.js:13-32` parses `{errors:[…]}` / `{detail:{errors}}`), and
  the draft is preserved on failure (`:603-605`).

---

## Appendix C — Session layer (incl. WebSocket contract table)

_Source: .tm-fix-scratch/ui_chunk_c.md (39 element rows + 13-command / 30-event WebSocket contract table). Verdict vocabulary: see "How to read this document"._

# Chunk C — Session view (`#/workspace/:wsId/session/:sid` + legacy `#/session/:id`)

## Scope

- Routes: `#/workspace/:wsId/session/:sid` (view=session) and legacy `#/session/:id`.
- Root component: `web_ui/frontend/src/components/SessionTab.jsx` + everything it renders
  (EXCLUDING ConfigPanel and worker-management, which are chunks D/E).
- OUT OF SCOPE: ConfigPanel (D), WorkerManagementPanel (E), App.jsx global shell (A).

## Files read (with line counts)

| File | Lines |
|---|---|
| web_ui/frontend/src/components/SessionTab.jsx | 1276 |
| web_ui/frontend/src/components/QueryBar.jsx | 125 |
| web_ui/frontend/src/components/ChatPanel.jsx | 199 |
| web_ui/frontend/src/components/StatusBar.jsx | 42 |
| web_ui/frontend/src/components/SecurityDialog.jsx | 216 |
| web_ui/frontend/src/components/SessionSidebar.jsx | 222 |
| web_ui/frontend/src/components/chat/MessageBubble.jsx | 221 |
| web_ui/frontend/src/components/chat/adaptWorkerEvent.js | 499 |
| web_ui/frontend/src/components/chat/workerEventRouting.js | 45 |

Backend evidence read via grep: `web_ui/backend/server.py` (WS hub + command switch),
`web_ui/backend/bridge.py`, `web_ui/backend/session_manager.py`,
`web_ui/backend/event_forwarder.py`, `web_ui/backend/workspace_routes.py`.

## Verdict counts

| Verdict | Count |
|---|---|
| WIRED | 33 |
| WIRED (REST) | 2 |
| PARTIAL | 2 |
| ORPHANED | 2 |
| DEAD | 0 |
| UNVERIFIED | 0 |
| **Total elements** | **39** |

---

## Rows — SessionTab.jsx (header / chrome)

| # | Visible label/text | file:line | Kind | Handler | What handler ACTUALLY does | Backend counterpart | VERDICT | Notes |
|---|---|---|---|---|---|---|---|---|
| 1 | `{sessionName}` (session name) | SessionTab.jsx:1110 | text | — | displays `store.sessions[storeKey].name` | NONE (display; fed by session_loaded/session_renamed) | WIRED | store-driven |
| 2 | `Rename` | SessionTab.jsx:1111 | button | onClick → setIsRenaming(true) | opens inline rename input + focus | NONE (client) | WIRED | |
| 3 | inline rename `<input>` (`session-header-input`) | SessionTab.jsx:1080 | input | onKeyDown Enter / onBlur | `sendCommand('rename_session',{session_id,new_name})` + optimistic updateSessionName | `rename_session` → server.py:2064 | WIRED | Escape cancels |
| 4 | `← Back to Workspace` | SessionTab.jsx:1122 | button | onClick → navigate() | client route to `/workspace/:id` or `/workspaces` | NONE (router) | WIRED | backWorkspaceId chain |
| 5 | `Details` | SessionTab.jsx:1131 | button | onClick → setSidebarOpen(v=>!v) | toggles SessionSidebar | NONE (client) | WIRED | |
| 6 | `Delete` | SessionTab.jsx:1160 | button | onClick → setShowDeleteConfirm(true) | reveals confirm strip | NONE (client) | WIRED | |
| 7 | `Yes` (delete confirm) | SessionTab.jsx:1142 | button | onClick → sendCommand('delete_session') | `delete_session {session_id}` | `delete_session` → server.py:2033 | WIRED | |
| 8 | `No` (delete confirm) | SessionTab.jsx:1152 | button | onClick → setShowDeleteConfirm(false) | cancels | NONE (client) | WIRED | |
| 9 | `⚠ {sessionError}` banner text | SessionTab.jsx:1177 | text | — | renders store sessionError | NONE (display; fed by `error`/`status_message`) | WIRED | |
| 10 | `Start New Session` (error banner, stale) | SessionTab.jsx:1179 | button | onClick → startNewSession | clears stale; adopts pending session OR `sendCommand('new_session',{mode})` | `new_session` → server.py:2207 | WIRED | only shown when staleSession |
| 11 | `✕` (error banner dismiss) | SessionTab.jsx:1184 | button | onClick → clearSessionError | clears store error | NONE (client) | WIRED | |
| 12 | resize handle (`.resize-handle`) | SessionTab.jsx:1221 | div | onMouseDown → handleResizeStart | drags config-panel width, persists localStorage | NONE (client) | WIRED | |
| 13 | `Click tab to load conversation` | SessionTab.jsx:1067 | text | — | deferred-tab placeholder | NONE (display) | WIRED | |
| 14 | global keydown → focus `.query-input` | SessionTab.jsx:992 | keydown handler | bounce printable keys | focus query textarea | NONE (client) | WIRED | |
| — | (component mount) | SessionTab.jsx:386-418 | side-effect | onopen raw send | `load_session {session_id}` or `new_session {mode}` | `load_session` → server.py:1765 / `new_session` → server.py:2207 | WIRED | one per connection (loadSentRef) |
| — | (component mount) | SessionTab.jsx:422/427 | side-effect | onopen → sendCommand | `get_providers` / `get_available_tools {mode}` when cache empty | server.py:1529 / 1679 | WIRED | |

## Rows — QueryBar.jsx

| # | Visible label/text | file:line | Kind | Handler | What handler ACTUALLY does | Backend counterpart | VERDICT | Notes |
|---|---|---|---|---|---|---|---|---|
| 15 | query `<textarea>` (`.query-input`, placeholder "Enter your query…") | QueryBar.jsx:93-98 | textarea | onKeyDown Enter → handleToggle | always enabled (`disabled={false}`) | NONE | WIRED | |
| 16 | `▶ Run` | QueryBar.jsx:112 | button | onClick → handleToggle → handleRun | `start_session` (no sessionId) / `continue_session` (sessionId) with `{query,config:{...config,mode}}` | server.py:953 / 997 | WIRED | disabled if isConnecting or empty+idle |
| 17 | `⏸ Pause` | QueryBar.jsx:104 | button | onClick → handleToggle | `sendCommand('pause_session',{})` | `pause_session` → server.py:1069 | WIRED | shown when RUNNING |
| 18 | `⏸ Pausing…` | QueryBar.jsx:108 | button | disabled | no-op display | NONE | WIRED | shown when PAUSING |
| 19 | Enter key (submit) | QueryBar.jsx:70-71 | keydown | handleKeyDown → handleToggle | Enter (no shift) when IDLE/WAITING/PAUSED | NONE | WIRED | |
| 20 | **(Stop button — missing)** | QueryBar.jsx docstring:7-13 | missing | — | docstring documents a Stop button + `stop_session`, but NO Stop button exists in code | `stop_session` → server.py:1079 (handler present) | ORPHANED | backend cmd has no in-scope sender |
| 21 | **(Resume button — missing)** | — (no element) | missing | — | no Resume UI in scope | `resume_session` → server.py:1074 (handler present) | ORPHANED | backend cmd has no in-scope sender |

## Rows — StatusBar.jsx

| # | Visible label/text | file:line | Kind | Handler | What handler ACTUALLY does | Backend counterpart | VERDICT | Notes |
|---|---|---|---|---|---|---|---|---|
| 22 | status label (`statusDisplay(status)`) | StatusBar.jsx:~20 | text | — | maps IDLE/RUNNING/PAUSING/PAUSED/WAITING_FOR_USER | fed by `state_changed` → server.py:2193 / bridge.py:2587 `session_stop` | WIRED | display only |
| 23 | `In / Out / Context` token counters | StatusBar.jsx:~28 | text | — | displays tokensIn/Out/contextLength | fed by `tokens_updated` (server.py:1974 etc.) + `context_updated` (server.py:1979 etc.) | WIRED | display only |

## Rows — SecurityDialog.jsx

| # | Visible label/text | file:line | Kind | Handler | What handler ACTUALLY does | Backend counterpart | VERDICT | Notes |
|---|---|---|---|---|---|---|---|---|
| 24 | `Approve` | SecurityDialog.jsx | button | onClick → sendCommand('security_response',{approved:true,remember}) | approves pending capability request | `security_response` → server.py:2502 (`resolve_security_prompt`) | WIRED | |
| 25 | `Deny` | SecurityDialog.jsx | button | onClick → sendCommand('security_response',{approved:false,remember}) | denies request | server.py:2502 | WIRED | |
| 26 | dialog backdrop | SecurityDialog.jsx | div | onClick → deny() | same as Deny | server.py:2502 | WIRED | |
| 27 | `Remember this decision` | SecurityDialog.jsx | checkbox | local setState | includes `remember` in payload | NONE directly (client state → payload) | WIRED | |
| — | (component trigger) | SessionTab.jsx:1227 | side-effect | setSecurityPrompt | opens dialog on `security_prompt` event | `security_prompt` → bridge.py:401 | WIRED | |

## Rows — SessionSidebar.jsx

| # | Visible label/text | file:line | Kind | Handler | What handler ACTUALLY does | Backend counterpart | VERDICT | Notes |
|---|---|---|---|---|---|---|---|---|
| 28 | tool checkbox (per tool) | SessionSidebar.jsx | checkbox | toggleTool → sendCommand('apply_config',{config:{...config,tools:next}}) | optimistic toggle + full-config apply | `apply_config` → server.py:1113 | WIRED | |
| 29 | worker `Stop` (per worker) | SessionSidebar.jsx | button | handleStopWorker → POST | `POST /api/workspace/{ws}/workers/{name}/stop` | workspace_routes.py:1112 | WIRED (REST) | not WS |
| 30 | container `Start`/`Stop` | SessionSidebar.jsx | button | runContainerAction → containerAction | workspaceStore REST container action | (containers REST) | WIRED (REST) | |
| 31 | `✕` close | SessionSidebar.jsx | button | onClick → onClose | closes panel | NONE (client) | WIRED | |

## Rows — ChatPanel.jsx

| # | Visible label/text | file:line | Kind | Handler | What handler ACTUALLY does | Backend counterpart | VERDICT | Notes |
|---|---|---|---|---|---|---|---|---|
| 32 | `← Load older messages` | ChatPanel.jsx | button | onClick → loadMore | SessionTab.loadMore → `sendCommand('load_more_messages',{offset,limit:20})` | `load_more_messages` → server.py:2014 (reply `more_messages` session_manager.py:536) | WIRED | shown only when hasMore && loadMore |
| 33 | `↑` (prev query) | ChatPanel.jsx | button | jumpToPrevQuery | scroll to prev `.message-user` | NONE (client) | WIRED | |
| 34 | `↓` (scroll bottom) | ChatPanel.jsx | button | scrollToBottomFn(true) | scroll client | NONE (client) | WIRED | |
| 35 | `Send a message to start.` | ChatPanel.jsx | text | — | empty-state | NONE (display) | WIRED | |

## Rows — MessageBubble.jsx

| # | Visible label/text | file:line | Kind | Handler | What handler ACTUALLY does | Backend counterpart | VERDICT | Notes |
|---|---|---|---|---|---|---|---|---|
| 36 | `📋` CopyButton (per message / reasoning) | MessageBubble.jsx | button | navigator.clipboard | copies text, ✅ 1.5s | NONE (client) | WIRED | |
| 37 | `Show more`/`Show less` (TruncatableContent) | MessageBubble.jsx | button | local expand | >500 chars toggle | NONE (client) | WIRED | |
| 38 | `Thinking` (details) | MessageBubble.jsx | `<details>` | local toggle | reasoning block | NONE (client) | WIRED | |
| 39 | tool-call `<details>` (ToolCallContent) | MessageBubble.jsx | `<details>` | local toggle | args/result | NONE (client) | WIRED | |

## Pure helpers (no rendered element)

- `chat/adaptWorkerEvent.js` — `adaptWorkerEvent(evt)` maps worker events → MessageBubble
  msg objects (or null to suppress). Handles: user_message / query(null) /
  final_response / worker_message / assistant_message / tool_call / tool_result /
  system_notification (token_warning|turn_warning|time_warning|context_summarized|fallback) /
  context_updated(null) / tokens_updated(null) / token_recovery / started|completed|stopped|paused|resumed /
  error / worker_spawned|worker_status|worker_completed|worker_error|worker_partial_result /
  default("Unknown event: "). Also `isWorkerEventRenderable(evt)`.
- `chat/workerEventRouting.js` — `instanceKeyOf`, `matchesPanel`, `routeEventsToPanels`
  (consumed by worker panels = Chunk E; not rendered by SessionTab).

---

## Two-way WebSocket contract (SessionTab ↔ `/ws`)

WS URL: `ws://${hostname}:${VITE_BACKEND_PORT||8000}/ws` (SessionTab.jsx:31-32). Each SessionTab owns its own socket.

### Client → Server (commands sent by in-scope UI)

| Command | Sent from | Backend handler | Verdict |
|---|---|---|---|
| `load_session` | SessionTab.jsx:390 (onopen raw) | server.py:1765 | WIRED |
| `new_session` | SessionTab.jsx:411 / startNewSession | server.py:2207 | WIRED |
| `get_providers` | SessionTab.jsx:422 | server.py:1529 | WIRED |
| `get_available_tools` | SessionTab.jsx:427 (+ on config_changed mode change) | server.py:1679 | WIRED |
| `load_more_messages` | ChatPanel→loadMore (SessionTab) | server.py:2014 | WIRED |
| `rename_session` | SessionTab.jsx:1088/1100 | server.py:2064 | WIRED |
| `delete_session` | SessionTab.jsx:1146 | server.py:2033 | WIRED |
| `start_session` | QueryBar.jsx:46 | server.py:953 | WIRED |
| `continue_session` | QueryBar.jsx:39/58 | server.py:997 | WIRED |
| `pause_session` | QueryBar.jsx:56 | server.py:1069 | WIRED |
| `apply_config` | SessionSidebar.jsx (toggleTool) | server.py:1113 | WIRED |
| `security_response` | SecurityDialog.jsx | server.py:2502 | WIRED |
| `stop_session` | **none in scope** | server.py:1079 | ORPHANED |
| `resume_session` | **none in scope** | server.py:1074 | ORPHANED |

### Server → Client (events `handleEvent` listens, SessionTab.jsx:491)

| Event | Frontend reaction | Backend emitter | Verdict |
|---|---|---|---|
| `state_changed` | store.receiveStateChanged | server.py:2193 | WIRED |
| `tokens_updated` | store + (source==='worker' → onWorkerEvent) | server.py:1974/2331/2474; worker bridge.py:735 | WIRED |
| `context_updated` | updateContextLength + (worker → onWorkerEvent) | server.py:1979/2336/2479; worker bridge.py:764 | WIRED |
| `conversation_changed` | store + total_count/has_more + compaction scroll | server.py:1109/1809 | WIRED |
| `more_messages` | prepend older + hasMore | session_manager.py:536 | WIRED |
| `config_changed` | store; if mode changed → get_available_tools | server.py:1090/1363/1457… | WIRED |
| `config_queued` | store.receiveConfigQueued | server.py:1446 | WIRED |
| `config_apply_failed` | store.receiveConfigApplyFailed | server.py:1468 | WIRED |
| `rebuild_result` | setContainerRebuildResult | server.py:2589/2594/2596 | WIRED (consumer=ConfigPanel D) |
| `status_message` | append system line to history | server.py (many, e.g. 995/1067) | WIRED |
| `session_loaded` | adopt/register; stale & replacement handling | server.py:1273/1322/1951/2321/2461 | WIRED |
| `providers_list` | store.receiveProvidersList | server.py:1558/1613/1663 | WIRED |
| `provider_saved` | sendCommand('get_providers') | server.py:1609 | WIRED |
| `provider_deleted` | **console.log only (no-op)** | server.py:1659 (+ providers_list 1663) | PARTIAL |
| `tools_list` | store.receiveToolsList | server.py:1701/1708 | WIRED |
| `default_config_saved` | setDefaultConfigSaveStatus | server.py:1506/1517/1524 | PARTIAL (consumed by ConfigPanel D) |
| `session_saved` | onSessionSaved?.() | server.py:1742 | WIRED |
| `session_renamed` | setCurrentSessionId + updateName + onSessionRenamed | server.py:2110 | WIRED |
| `session_closed` | closedRef=true; onClose?.() | server.py:2189 | WIRED |
| `session_deleted` | onClose?.() | server.py:2053 | WIRED |
| `security_prompt` | setSecurityPrompt → SecurityDialog | bridge.py:401 | WIRED |
| `logging_config_changed` | onLoggingConfigChanged(msg.config) | event_forwarder.py:117 | WIRED |
| `error` | store.setSessionError | server.py:1408 (`session_id`?) / 2152 | WIRED |
| `session_stop` | state→IDLE; abnormal → setSessionError | bridge.py:2587 | WIRED |
| `session_cleared` | receiveConversationChanged([]) + clearError | bridge.py:2186 | WIRED |
| `worker:*` (tool_call, tool_result, token_warning, turn_warning, time_warning, assistant_message, worker_spawned, worker_status, worker_completed, worker_error, worker_paused, worker_resumed, system_notification, user_message, worker_message, tokens_updated, context_updated, context_summarized, context_cleared, token_recovery) | → onWorkerEvent(sid,msg) (routed to worker panels) | bridge.py:453/554/674/735/764/778/792 (generic `f'worker:{event.type.value}'`) | WIRED (routing; consumer = Chunk E) |
| default / unknown | console.warn | — | WIRED |

### WebSocket mismatches / noteworthy

1. **`stop_session` + `resume_session`**: backend command handlers exist (server.py:1079 / 1074) but **no in-scope UI sends them**. QueryBar's docstring (lines 7-13) even documents a "Stop" column, yet no Stop button is rendered. → 2 ORPHANED commands.
2. **`session_stop` (event) ≠ `stop_session` (command)**: SessionTab *listens* for the `session_stop` event (emitted by bridge.py:2587 on abnormal agent stop) but never *sends* `stop_session`. Distinct two-way paths.
3. **`provider_deleted`**: emitted by backend (server.py:1659) but frontend handler is a deliberate no-op; list refresh relies on the sibling `providers_list` (server.py:1663). → PARTIAL.
4. **`default_config_saved`**: SessionTab stores it (`setDefaultConfigSaveStatus`) but the visible status is rendered by ConfigPanel (Chunk D). → PARTIAL from C's view.
5. **`worker:context_cleared`** is in the frontend switch but there is no explicit backend emitter string; it reaches the client only via the generic `f'worker:{event.type.value}'` forwarder (bridge.py:453/674).
6. **`tokens_updated`/`context_updated` carry `source:'worker'`** on the worker path (bridge.py:738/766); SessionTab re-routes these to `onWorkerEvent` in addition to updating the header counters.
7. **`sendCommand` is gated by `staleSessionRef`** (SessionTab.jsx:250): when a session is stale, ALL commands (incl. `security_response`, `apply_config`, `load_more_messages`) are blocked until "Start New Session".

## Send / Stop / Pause verdicts

- **Send (Run)**: `start_session` / `continue_session` → WIRED (QueryBar.jsx:39/46 → server.py:953/997).
- **Pause**: `pause_session` → WIRED (QueryBar.jsx:56 → server.py:1069).
- **Stop**: **ORPHANED** — no Stop button anywhere in scope; backend `stop_session` unreachable from this UI. The only stop-like UI actions are worker Stop (REST) and the `session_stop` inbound event.

## Surprises

- QueryBar docstring claims a Stop button and `stop_session`, but the implementation renders only Run/Pause.
- Per-session WS: every SessionTab (even inactive/deferred) opens its own socket and sends its own `load_session`.
- `provider_deleted` is intentionally swallowed; `default_config_saved` is set but rendered elsewhere (D).
- Frontend handles a rich set of worker events only for routing; rendering lives in Chunk E.

---

## Appendix D — ConfigPanel + root WorkspacePanel + save-as-default round trip

_Source: .tm-fix-scratch/ui_chunk_d.md (ConfigPanel + root WorkspacePanel elements + save-as-default round trip). Verdict vocabulary: see "How to read this document"._

# Chunk D — Config Panel / Workspace Panel / Save-As-Default (verification)

## Scope & method
Static verification of the config-related UI surface: `ConfigPanel.jsx`, its
`WorkspacePanel`/`DomainAllowlistEditor`/`ContainerPanel` children, the backend
REST routes they call, and the WS command handlers in `server.py`. Every element
was matched to a concrete file:line on BOTH sides (JSX control → handler →
backend route/command). No repo source was modified.

Files: `web_ui/frontend/src/components/{ConfigPanel,WorkspacePanel,DomainAllowlistEditor,ContainerPanel,SessionTab}.jsx`;
`web_ui/backend/{server.py,config_routes.py,config_manager.py,workspace_routes.py,prompt_routes.py,session_routes.py,session_manager.py}`;
tests `__tests__/ConfigPanel.test.jsx`, `ConfigPanelDraft.test.jsx`.

## Verdict counts
- VERIFIED: 26
- PARTIAL: 5   (save-as-default key loss; transient success signal; stale-send drop; DockerfileEditor & WorkerManagementPanel unread)
- UNVERIFIED: 0
- BROKEN: 0

Legend: VERIFIED = both ends proven; PARTIAL = works but with a caveat that
matters to the operator.

---

## Sub-area: ConfigPanel tabs & controls (web_ui/frontend/src/components/ConfigPanel.jsx, 981 L)
| Element | file:line | Verdict | Notes |
|---|---|---|---|
| "Save as Default" button | ConfigPanel.jsx:451-462 | VERIFIED | onClick → `sendCommand('set_default_config', {config: defaultsPayload})`; label swaps pending/true/error (L461) |
| 8 tab buttons (workspace/permissions/system_prompt/general/model/tools/container/advanced) | ConfigPanel.jsx:467-471 (keys L429, labels L430) | VERIFIED | → `setActiveTab(tab)` |
| Workspace Path read-only display | ConfigPanel.jsx:479-493 | VERIFIED | shows `draft.workspace_path` or "⚠ No workspace" |
| WorkspacePanel mount | ConfigPanel.jsx:496 | VERIFIED | passes workspaceId/sessionId/selectedWorker/isActive/effectivePermissions |
| Temperature (range) | ConfigPanel.jsx:507-512 | VERIFIED | onChange → updateDraft |
| Max Turns (number) | ConfigPanel.jsx:517-522 | VERIFIED | onChange → updateDraft |
| Critical Threshold (number) | ConfigPanel.jsx:529-541 | VERIFIED | sets critical + warning=critical-15000 |
| Provider select | ConfigPanel.jsx:551-562 | VERIFIED | handleProviderChange L303-312 sets provider_id + model=default_model |
| Model select | ConfigPanel.jsx:567-582 | VERIFIED | handleModelChange L314-316; disabled when !selectedProvider |
| "⚙ Manage Providers..." button | ConfigPanel.jsx:587-600 | VERIFIED | → setShowManageProviders(true) |
| ManageProvidersModal | ConfigPanel.jsx:604-611 | VERIFIED | providers/sendCommand/onProviderSaved |
| Tool Output Token Limit (number) | ConfigPanel.jsx:637-655 | VERIFIED | disabled when isModeLocked |
| Per-tool checkboxes | ConfigPanel.jsx:675-688 | VERIFIED | checked from draft.tools; onChange → updateDraft |
| Permissions selects (Git/Filesystem/Container/Network/MCP/Host Bash) | ConfigPanel.jsx:737-829 | VERIFIED | all → handlePermissionChange (tab-local; returns no-op if !sessionPerms) |
| Effective-profile display (read-only) | ConfigPanel.jsx:715-734 | VERIFIED | "Changes take effect on the next tool call." L835-837 |
| Container tab → ContainerPanelContent | ConfigPanel.jsx:842-849 | VERIFIED | sends workspacePath + sendCommand |
| System Prompt locked-mode preview + PromptLibrary (disabled) | ConfigPanel.jsx:874-892, 900 | VERIFIED | pointerEvents:none when locked |
| System Prompt custom textarea + PromptLibrary | ConfigPanel.jsx:909-915, 921 | VERIFIED | handleLoadPromptFromLibrary L246-262 → `GET /api/prompts/{name}` |
| Advanced tab | ConfigPanel.jsx:930-934 | VERIFIED | static "No advanced options at this time." (intentionally no controls) |
| Apply button → handleApply | ConfigPanel.jsx:938-960 / L323-400 | VERIFIED | `PUT /api/session/{id}/permissions` then `sendCommand('apply_config', {config})` |
| Auto: fetch /api/tools → setAllTools | ConfigPanel.jsx:184-197 | VERIFIED | route server.py:2761 |
| Auto: fetch /api/session/{id}/permissions | ConfigPanel.jsx:216-244 | VERIFIED | route session_routes.py (GET/PUT /permissions) |

## Sub-area: WorkspacePanel (web_ui/frontend/src/components/WorkspacePanel.jsx, 204 L)
| Element | file:line | Verdict | Notes |
|---|---|---|---|
| No-workspace guard | WorkspacePanel.jsx | VERIFIED | "No workspace loaded." when !workspaceId |
| DockerfileEditor section | WorkspacePanel.jsx (~L178) | PARTIAL | section rendered; child not read this pass |
| DomainAllowlistEditor section | WorkspacePanel.jsx (~L185) | VERIFIED | see below |
| WorkerManagementPanel section | WorkspacePanel.jsx (~L192) | PARTIAL | rendered; child not read this pass |
| EffectivePermissionsSection (pills) | WorkspacePanel.jsx | VERIFIED | display-only from sessionConfigs or PERMISSION_DEFAULTS |

## Sub-area: DomainAllowlistEditor (relative URLs, no API_BASE)
| Element | file:line | Verdict | Notes |
|---|---|---|---|
| mount GET allowlist | DomainAllowlistEditor.jsx | VERIFIED | `GET /api/workspace/{id}/domain_allowlist` → workspace_routes.py:390 |
| Retry button | DomainAllowlistEditor.jsx | VERIFIED | re-fetch on error |
| ✕ remove per domain | DomainAllowlistEditor.jsx | VERIFIED | local state only |
| Add (input + Enter + button) | DomainAllowlistEditor.jsx | VERIFIED | local push, dedupe |
| Save → PUT | DomainAllowlistEditor.jsx | VERIFIED | `PUT /api/workspace/{id}/domain_allowlist` → workspace_routes.py:405 |

## Sub-area: ContainerPanel (ContainerPanelContent)
| Element | file:line | Verdict | Notes |
|---|---|---|---|
| status poll (5 s) | ContainerPanel.jsx | VERIFIED | `GET /api/container/status` → server.py:2994 |
| integrity poll (5 s) | ContainerPanel.jsx | VERIFIED | `GET /api/container/integrity` → server.py:2946 |
| Rebuild Container button | ContainerPanel.jsx | VERIFIED | `sendCommand('rebuild_container', {workspace})` → server.py:2586 |
| status/integrity/image/caps/build-log display | ContainerPanel.jsx | VERIFIED | display-only |

---

## THE SAVE-AS-DEFAULT ROUND TRIP
WS dispatch in `server.py` is an **if/elif chain on `command`** (NOT a table/dict).
`set_default_config` IS a handled branch — the handler exists.

1. **Frontend send** — ConfigPanel.jsx:451-462 strips `session_permissions` and
   calls `sendCommand('set_default_config', {config})`.
   SessionTab.jsx:246-277 wraps it as `{command:'set_default_config', config:{...}}`.
2. **Backend receive** — server.py:1472 `elif command == "set_default_config":`
   - L1486 `config_dict = msg.get("config")`; L1487-1489 `translate_frontend_config(config_dict)`.
   - L1490-1503 fallback to `bridge.get_config()` when no payload.
   - L1504-1510 no config & no session → emits `default_config_saved` status **error**.
   - L1512 `save_global_defaults(cfg_dict)`.
   - L1516-1520 emits `{"type":"default_config_saved","status":"ok"}` (NO `session_id` field).
   - L1521-1527 exception → status **error**.
3. **Persist** — `save_global_defaults()` server.py:174-208 filters to
   `GLOBAL_DEFAULT_KEYS` (config_manager.py:119-126 =
   `{provider_id, model, base_url, temperature, max_turns, system_prompt}`),
   merges over `load_global_defaults()`, writes `~/.thoughtmachine/user/defaults.json`.
4. **New session reads it** — session_manager.py:149-157:
   `defaults_be = translate_frontend_config(load_global_defaults())`, then
   `setattr(session_config, key, defaults_be[key])` for each GLOBAL_DEFAULT_KEYS.
   Provider/model precedence session_manager.py:680-690. Read live at session create
   (no restart needed). Same key space on both write & read → **no key mismatch**.
5. **Status render** — SessionTab.jsx:837-843 `case 'default_config_saved'` →
   `setDefaultConfigSaveStatus(msg.status)` (guard skipped since no session_id);
   ConfigPanel.jsx:110-126 syncs button to "✓ Default saved!" and auto-clears after 2.5 s.

**Verdict: the round trip is wired end-to-end and functional** — backend handles it,
status is surfaced, and a new session inherits the saved values. BUT it is **PARTIAL**
for the operator because:
- **Only 6 keys persist.** `mode`, `tools`, `tool_output_token_limit`, token-monitor
  thresholds, `workspace_path`, `session_permissions` are **silently dropped**
  (server.py:203 filter). If the operator changed tools/thresholds, "Save as Default"
  looks like it "did nothing" on the next session.
- **Success is a 2.5 s transient label** that then reverts to "Save as Default" —
  easy to read as no-op.
- **`sendCommand` silently drops the click** (`console.warn` only) when
  `staleSessionRef.current` is true (SessionTab.jsx:250) — a genuine "nothing happens"
  path after a backend restart.

Test coverage gap: `ConfigPanel.test.jsx:175-195` mocks `sendCommand`, so it proves
ONLY the frontend wiring (`('set_default_config', {config:any})`, and the three button
labels). The backend handler and persistence are NOT covered by that test.

## PROVIDER / MODEL SELECTION
| Piece | file:line | Verdict |
|---|---|---|
| `get_providers` WS command | server.py:1529 | VERIFIED |
| `save_provider` WS command | server.py:1571 | VERIFIED |
| `delete_provider` WS command | server.py:1629 | VERIFIED |
| Provider select → handleProviderChange | ConfigPanel.jsx:303-312, 551-562 | VERIFIED |
| Model select → handleModelChange | ConfigPanel.jsx:314-316, 567-582 | VERIFIED |
| New-session provider/model precedence | session_manager.py:680-690 | VERIFIED |
| Global-default provider/model applied | session_manager.py:154-157 | VERIFIED |

---

## Runtime verification (recommended to confirm operator symptom)
1. Start backend; open a session; DevTools → Network → WS: click "Save as Default" and
   confirm the frame `{"command":"set_default_config","config":{...}}` is sent.
2. `cat ~/.thoughtmachine/user/defaults.json` → should now contain exactly the 6
   GLOBAL_DEFAULT_KEYS; button shows "✓ Default saved!" for ~2.5 s.
3. Create a NEW session → provider/model/temperature/max_turns/system_prompt match the
   saved values; **tools / thresholds / permissions will NOT** (expected given the filter).
4. To exercise the drop path: restart backend with the tab open, click once — the
   command is swallowed by the stale-session guard (console.warn) and nothing happens.

## Open items (UNVERIFIED, out of this pass)
- DockerfileEditor.jsx and WorkerManagementPanel.jsx child internals not read.
- DomainAllowlistEditor / ContainerPanel use **relative** URLs while ConfigPanel uses an
  **absolute** `http://host:8000` base — harmless unless a dev proxy/port mismatch exists.

---

## Appendix E — Worker UI (incl. worker:* event matrix)

_Source: .tm-fix-scratch/ui_chunk_e.md (39 element rows + worker:* event matrix). Verdict vocabulary: see "How to read this document"._

# CHUNK E — Worker UI control map (frontend controls → handlers → backend)

Repo root: `/home/jojo/PycharmProjects/ThoughtMachine-dev`
Frontend base: `web_ui/frontend/src/`
Method: read every worker-UI source file + its tests; enumerated every visible control
(label/button/input), traced its handler literally, then matched to the backend route
(`web_ui/backend/.../workspace_routes.py`, prefix `/api/workspace`) or WebSocket event
(`web_ui/backend/.../bridge.py`). **No repo source was modified.**

Legend for the "Handler does" column: *literal* description of the code, not intent.

---

## 1. Files read (full)

| File | Lines | Kind |
|---|---|---|
| `web_ui/frontend/src/components/WorkerManagementPanel.jsx` | 1329 | component |
| `web_ui/frontend/src/components/WorkerOutputPanel.jsx` | 943 | component |
| `web_ui/frontend/src/components/WorkerPanelArea.jsx` | 165 | component |
| `web_ui/frontend/src/components/chat/adaptWorkerEvent.js` | 499 | pure fn |
| `web_ui/frontend/src/components/chat/workerEventRouting.js` | 45 | pure fn |
| `web_ui/frontend/src/components/SessionSidebar.jsx` | ~ (Workers section) | component |
| `web_ui/frontend/src/components/WorkspacePanel.jsx` | 191+ | component (hosts WMP) |
| `web_ui/frontend/src/components/workspace/modals/WorkerEditorModal.jsx` | 136 | modal (ORPHANED) |
| `web_ui/frontend/src/App.jsx` | 1374 | orchestrator (panel state) |
| `web_ui/backend/…/workspace_routes.py` | 1809 | backend routes |
| `web_ui/backend/…/bridge.py` | ~810+ | WS event emitter |
| `agent/events.py` | 27.8 KB | EventType enum |

### Test files present (worker-related) — `web_ui/frontend/src/components/__tests__/`

| Test file | Size | Key describes (line) |
|---|---|---|
| `WorkerManagementPanel.test.jsx` | 4.4 KB | `'WorkerManagementPanel — deliverable 1 tray'` **L76** |
| `WorkerManagementPanelActiveInstances.test.jsx` | 11.3 KB | `'…— active instances'` **L102** (loads `/workers/active?session_id=` **L103**) |
| `WorkerOutputPanel.test.jsx` | 10.5 KB | isolation **L121**, stale header **L189**, status dot **L229** |
| `WorkerPanelArea.test.jsx` | 17.7 KB | component **L97**, real-App multi-panel **L432** |
| `SessionWorkerIsolation.test.jsx` | 16.9 KB | isolation R6b **L276**, status dot F5 **L390** |
| `workerEventRouting.test.jsx` | 4.9 KB | `instanceKeyOf` **L17**, `matchesPanel` **L33**, `routeEventsToPanels` **L66** |
| `adaptWorkerEvent.test.js` | 33.8 KB | per-event describes: guard **L33**, user_message **L54**, query **L154**, final_response **L164**, worker_message **L225**, assistant_message **L264**, tool_call **L303**, tool_result **L348**, system_notification **L444**, started **L598**, completed **L612**, stopped **L626**, error **L640**, worker_spawned **L681**, worker_status **L722**, worker_completed **L764**, worker_error **L797** |

---

## 2. Control map — WorkerManagementPanel.jsx (left "Workers" panel)

| # | Visible label/text | file:line | Element kind | Handler name | What handler ACTUALLY does (literal) | Backend counterpart (route / WS + file:line) | VERDICT | Evidence/notes |
|---|---|---|---|---|---|---|---|---|
| E1 | `Workers` (panel title / rows) | WorkerManagementPanel.jsx:512-521 | panel + list | `fetchWorkers` | `fetch('/api/workspace/{ws}/workers/active?session_id=…')`, normalises rows, `setWorkers(rows)`; polled every 5s | `GET /api/workspace/{ws}/workers/active` — workspace_routes.py:626 (`get_active_workers`→`_collect_active_workers`, filters by `?session_id=`) | WIRED | Poll interval registered L613-617 (`clearInterval` cleanup L616) |
| E2 | worker row (name · status dot · instance) | WorkerManagementPanel.jsx L521 `workers` state | list row | — (render) | renders runtime status from active-instance list; dot class from status | same as E1 (`status` field) | WIRED | comment L521 "from GET …/workers/active (runtime instances)" |
| E3 | (empty/loading) "Loading workers…" | WorkerManagementPanel.jsx (render) | text | — | static placeholder while `loading` | NONE | WIRED | covered by WorkspacePanel.test.jsx L298 |
| E4 | `Stop` (per instance) | WorkerManagementPanel.jsx:634-641 | button | `handleStop` | `POST /api/workspace/{ws}/workers/{name}/stop?instance_id={id}`; on !ok throws `body.detail.error` → `stopErrors[key]` | `POST /api/workspace/{ws}/workers/{name}/stop` — workspace_routes.py:1112 (`stop_worker`→`_stop_worker_instance`) | WIRED | instanceQuery built L637; only sent when `instanceId != null` |
| E5 | `Pause` (per instance) | WorkerManagementPanel.jsx:673-680 | button | `handlePause` | `POST /api/workspace/{ws}/workers/{name}/pause?instance_id={id}`; !ok → `stopErrors[key]` | `POST …/workers/{name}/pause` — workspace_routes.py:1155 (`pause_worker`) | WIRED | cmd.json `{action:pause}` + status.json `pausing` + `thread.pause()` |
| E6 | `Resume` (per instance) | WorkerManagementPanel.jsx:701-708 | button | `handleResume` | `POST /api/workspace/{ws}/workers/{name}/resume?instance_id={id}`; !ok → `stopErrors[key]` | `POST …/workers/{name}/resume` — workspace_routes.py:1267 (`resume_worker`) | WIRED | cmd.json `{action:resume}` + `thread.resume()` |
| E7 | `Save`/create worker (form submit) | WorkerManagementPanel.jsx:745-750 | form button | `handleSubmit`(create/update) | `url = isEdit ? /workers/{name} : /workers`, `method = isEdit ? PUT : POST`, `fetch(url,{method,headers,body:JSON.stringify(payload)})` | `POST /{ws}/workers` workspace_routes.py:652 (`create_worker`) / `PUT /{ws}/workers/{name}` :688 (`update_worker`) | WIRED | validate `WorkerDefinition` → `workers.json` atomic write |
| E8 | `Delete` (per worker config) | WorkerManagementPanel.jsx:777-783 | button | `handleDeleteWorker` | `DELETE /api/workspace/{ws}/workers/{name}`; accepts 204 (`res.status !== 204` guard) | `DELETE /{ws}/workers/{name}` — workspace_routes.py:728 (`delete_worker`, 204) | WIRED | — |
| E9 | worker name/template select → open panel | props `onSelectWorker` | row click | (prop) → App `handleSelectWorker` | App.jsx:706-714 — queues `pendingWorkerSelectionRef` if no `activeSessionId`, else `openPanel(...)` | NONE (frontend-only panel open) | WIRED | passed App.jsx:1304 `onSelectWorker={handleSelectWorker}`; WMP consumed at WorkspacePanel.jsx:191 |
| E10 | templates for form | WorkerManagementPanel.jsx:619-625 | effect | `fetchTemplates` | `fetch('/api/workspace/templates')` → `setTemplates(json)`; silent on !ok | `GET /api/workspace/templates` — workspace_routes.py:214 (`get_worker_templates`) | WIRED | reads `~/.thoughtmachine/worker_templates/` then fallback |
| E11 | stop/pause/resume error text | WorkerManagementPanel.jsx (`stopErrors`) | text | — | renders `stopErrors[key]` message | NONE (client error surface) | WIRED | — |

---

## 3. Control map — WorkerOutputPanel.jsx (right output panel)

| # | Visible label/text | file:line | Element kind | Handler name | What handler ACTUALLY does (literal) | Backend counterpart | VERDICT | Evidence/notes |
|---|---|---|---|---|---|---|---|---|
| F1 | `Worker: {instanceLabel||workerName}` | WorkerOutputPanel.jsx:769-771 | header text | — | static render of prop | NONE | WIRED | — |
| F2 | status dot + label (Idle/Running/Paused/Error/Stopped) | WorkerOutputPanel.jsx:14-20 `STATUS_DOT`; render ~800s | badge | — | dot derived from `runtimeStatus` in component state, updated by event loop L246-319 | driven by WS `worker:*` (see §6) | WIRED | tests WorkerOutputPanel.test.jsx L229; SessionWorkerIsolation.test.jsx L390 |
| F3 | `ctx: {current} / {max}` | WorkerOutputPanel.jsx:772-775 | header metric | — | reads `workerInfo.current_context_tokens / max_context_tokens` | WS `worker:context_updated` bridge.py:764 / `worker:tokens_updated` :735 | WIRED | live update path L250-276 |
| F4 | `{current_task}` inline | WorkerOutputPanel.jsx:777-779 | header text | — | from `workerInfo.current_task` (set from `worker_status` L288) | WS `worker:worker_status` | WIRED | — |
| F5 | `⏸ Pause` | WorkerOutputPanel.jsx:834-839 | button | `handlePause` | guards non-owning session (`workerInfo.session_id !== sessionId`→error), then `POST …/workers/{name}/pause?instance_id=` :649; !ok → `setStopError` | `POST …/{name}/pause` workspace_routes.py:1155 | WIRED | `canPause` gate |
| F6 | `▶ Resume` | WorkerOutputPanel.jsx:826-831 | button | `handleResume` | session guard, then `POST …/resume?instance_id=` :676; !ok → `setStopError` | `POST …/{name}/resume` workspace_routes.py:1267 | WIRED | shown when `runtimeStatus==='paused'` L825 |
| F7 | `⏹ Stop` | WorkerOutputPanel.jsx:902-907 | button | `handleStop` | session guard, then `POST …/workers/{name}/stop?instance_id=` :619; !ok → `setStopError` | `POST …/{name}/stop` workspace_routes.py:1112 | WIRED | `canStop = busy||ready` L634 |
| F8 | stop/pause/resume error | WorkerOutputPanel.jsx:899-901 | text | — | renders `stopError` (auto-clears after 3s) | NONE (client) | WIRED | — |
| F9 | message bubbles (output stream) | WorkerOutputPanel.jsx:843-857 | list | `adaptWorkerEvent` (L844) | maps each routed event → msg; `null` suppressed; empty-content filtered L855 | events originate from `worker:*` WS + events.jsonl | WIRED | `isWorkerEventRenderable` also exported/used |
| F10 | "Worker output appears here" (empty state) | WorkerOutputPanel.jsx (empty branch ~L712) | text | — | static "no worker selected" branch `if (!workerName)` | NONE | WIRED | test WorkerOutputPanel.test.jsx L224 |

**Note:** WorkerOutputPanel's Stop/Pause/Resume duplicate WorkerManagementPanel's and **both** send, correctly, a specific `instance_id` — unlike SessionSidebar (see §4 H1).

---

## 4. Control map — SessionSidebar.jsx (session details "Workers" section)

| # | Visible label/text | file:line | Element kind | Handler name | What handler ACTUALLY does (literal) | Backend counterpart | VERDICT | Evidence/notes |
|---|---|---|---|---|---|---|---|---|
| H1 | `Stop` (per worker) | SessionSidebar.jsx:66-88 (fetch :73) | button | `handleStopWorker` | `POST /api/workspace/{ws}/workers/{name}/stop` — **NO `instance_id`, NO `session_id`**; on !ok reads `body.detail.error`→`setStopError` | `POST …/{name}/stop` workspace_routes.py:1112 | **PARTIAL** | Backend accepts optional `instance_id` (query) + `session_id`; sidebar omits both → stops **all instances of that worker name** in the workspace, not the session-scoped one. Inconsistent with E4/F7 which do send `instance_id`. |
| H2 | worker row (name + status dot) | SessionSidebar.jsx (Workers section) | list row | — | read from `useWorkspaceStore.currentWorkspace.workers` (no direct fetch) | populated by workspaceStore | WIRED | tested SessionSidebar.test.jsx L354 |
| H3 | stop error text | SessionSidebar.jsx (`stopError`) | text | — | renders `stopError` | NONE (client) | WIRED | tested SessionSidebar.test.jsx L380 |

---

## 5. Control map — WorkerPanelArea.jsx (multi-panel tab strip) + App.jsx (panel state)

| # | Visible label/text | file:line | Element kind | Handler name | What handler ACTUALLY does (literal) | Backend counterpart | VERDICT | Evidence/notes |
|---|---|---|---|---|---|---|---|---|
| G1 | panel tab (worker label) | WorkerPanelArea.jsx:129-139; tab strip | tab | `onFocus` → App `focusPanel` | App.jsx:701-704 sets focused key for session | NONE | WIRED | test WorkerPanelArea.test.jsx L97 |
| G2 | `◀ ▶` move-left / move-right chevrons | WorkerPanelArea.jsx tab strip (per tab) | button | `onMoveLeft`/`onMoveRight` → App `reorderPanel(dir)` | App.jsx:648-662 reorders panel array by direction | NONE | WIRED | comment L18-21 (drag-reorder intentionally not implemented) |
| G3 | `✕` close tab | WorkerPanelArea.jsx (tab close) | button | `onClose` → App `closePanel` | App.jsx:632-646 removes panel by key | NONE | WIRED | — |
| G4 | (single panel, no tab strip) | WorkerPanelArea.jsx:49-59 | fallback | — | 1 panel → plain `<WorkerOutputPanel>` (no tab strip, no maximize/pin) | NONE | WIRED | comment L14-17 |
| G5 | panel resize (drag) | (App `resizePanel`) | — | `resizePanel` | App.jsx:664-675 clamps size to MIN250/MAX600 | NONE | WIRED | constants App.jsx:54-56 |
| G6 | maximized/pinned toggles | (App `toggleMaximize`/`togglePin`) | — | `toggleMaximize`(L677-687)/`togglePin`(L689-699) | flips `maximized`/`pinned` on the panel object | NONE | **UNVERIFIED** | handlers exist & are passed (App.jsx:1348-1361) but I did **not** locate the concrete visible maximize/pin buttons in WorkerPanelArea (single-panel fallback hides them per comment L15-17). See "controls not fully read" §7. |
| G7 | 0 panels | WorkerPanelArea.jsx (layout) | — | — | returns `null` (no sidebar) | NONE | WIRED | comment L14 |
| G8 | panel persistence/reload | App.jsx:1013-1036 | effect | — | persists `workerPanelsBySession`+focused to `localStorage` under `tm.workerPanels.` | NONE | WIRED | prefix App.jsx:53; migration drops legacy `workerPanelState` L1038-1045 |

---

## 6. `worker:*` EVENT MATRIX (bridge.py emit → frontend consume)

Emitter: `web_ui/backend/…/bridge.py`. Consumer: `WorkerOutputPanel.jsx` event loop (L246-319) +
`adaptWorkerEvent.js` switch + `isWorkerEventRenderable`.

| # | Backend event (`worker:*`) | Emit site (bridge.py) | Frontend consumer (file:line) | VERDICT | Notes |
|---|---|---|---|---|---|
| M1 | `worker:worker_spawned` | L674 (`_on_worker_spawned`); global sub L476-496 | WorkerOutputPanel L278; adaptWorkerEvent `case 'worker_spawned'` L305 (test L681) | WIRED | sets `runtime_status` ready/busy |
| M2 | `worker:worker_status` | generic L453 + L476-496 | WorkerOutputPanel L285-291; adaptWorkerEvent L722 | WIRED | carries `status`,`current_task` |
| M3 | `worker:worker_completed` | L476-496 (`_on_worker_completed`) | WorkerOutputPanel L292-298; adaptWorkerEvent L764 | WIRED | → `runtime_status:'ready'` |
| M4 | `worker:worker_error` | L476-496 (`_on_worker_error`) | WorkerOutputPanel L299-305; adaptWorkerEvent L797 | WIRED | → `runtime_status:'error'` |
| M5 | `worker:worker_message` | L453/L476-496 | WorkerOutputPanel (case worker_message); adaptWorkerEvent L225 | WIRED | `is_final` from response_type |
| M6 | `worker:worker_partial_result` | L476-496 (WORKER_PARTIAL_RESULT sub) | — (no dedicated case seen in WOP/adapt tests) | **UNVERIFIED** | Event exists (EventType L89); not confirmed rendered. |
| M7 | `worker:tokens_updated` | L735 (flattened `input/output`, `source:'worker'`) | WorkerOutputPanel L421-429 (`case 'tokens_updated'`) | WIRED | header-only; not buffered (bridge L808 exempt) |
| M8 | `worker:context_updated` | L764 (`context_length`, dedup `_last_context_updated`) | WorkerOutputPanel L445-451 + L250-264; header ctx L772 | WIRED | header-only; exempt from buffer |
| M9 | `worker:context_summarized` | L778 | WorkerOutputPanel L431-444 | WIRED | mapped to `system_notification` bubble |
| M10 | `worker:context_cleared` | subscribed in per-worker bus (`context_cleared` in subscribed_types L702-802 → else `worker:{type}` L792) | — (no case found) | **UNVERIFIED** | EventType `CONTEXT_CLEARED` exists (events.py L420 map). |
| M11 | `worker:system_notification` | L554 (non-worker token warnings only; worker-sourced skipped to dedupe) | adaptWorkerEvent `system_notification` L444 | WIRED | — |
| M12 | `worker:token_warning` | L476-496 / per-worker bus | adaptWorkerEvent `token_warning type` L446 | WIRED | — |
| M13 | `worker:turn_warning` | per-worker bus (`turn_warning` subscribed) | adaptWorkerEvent `turn_warning type` L487 | WIRED | — |
| M14 | `worker:time_warning` | per-worker bus (`time_warning` subscribed) | adaptWorkerEvent `time_warning type` L510 | WIRED | — |
| M15 | `worker:token_recovery` | per-worker bus (`token_recovery` subscribed) | — (no case seen) | **UNVERIFIED** | — |
| M16 | `worker:worker_paused` | per-worker bus (`worker_paused` subscribed) | WorkerOutputPanel L306-312 | WIRED | → `runtime_status:'paused'` |
| M17 | `worker:worker_resumed` | per-worker bus (`worker_resumed` subscribed) | WorkerOutputPanel L313-319 | WIRED | → `runtime_status:'ready'` |
| M18 | `worker:tool_call` | per-worker bus | WorkerOutputPanel (case tool_call); adaptWorkerEvent L303 | WIRED | — |
| M19 | `worker:tool_result` | per-worker bus | WorkerOutputPanel (case tool_result); adaptWorkerEvent L348 | WIRED | — |
| M20 | `worker:assistant_message` | per-worker bus | WorkerOutputPanel L357-366; adaptWorkerEvent L264 | WIRED | preserves `response_type` |
| M21 | `worker:user_message` | per-worker bus | adaptWorkerEvent L54 (test) | WIRED | shown as query in panel |
| M22 | `worker:security_prompt` | L401 (from SECURITY_PROMPT global bus), L420-423 sub | — (no worker-UI case found) | **UNVERIFIED** | likely handled by main chat, not worker panel |
| M23 | `worker:worker_timeout` | (EventType WORKER_TIMEOUT exists, events.py L90) | — | **UNVERIFIED** | no emit/consume site confirmed |

Also confirmed: `worker:tokens_updated` and `worker:context_updated` are **excluded** from buffering
(bridge.py L808) because they are header-only.
Generic fan-out for global-bus handlers = `f'worker:{event.type.value}'` (bridge.py L453).

---

## 7. Controls not fully read / gaps

1. **Maximize / Pin buttons (G6)** — `toggleMaximize`/`togglePin` handlers exist in App.jsx
   (L677-687 / L689-699) and are passed to `WorkerPanelArea` (App.jsx:1348-1361), but I did
   **not** locate the concrete visible buttons that call them (the single-panel fallback
   explicitly omits them per WorkerPanelArea.jsx L15-17). The multi-panel tab strip was
   read structurally; the per-tab maximize/pin affordances were not line-verified.
2. **WorkerManagementPanel create/edit form fields** — the modal/section inputs (name, system
   prompt, token limits, preset add) were not enumerated field-by-field; only the submit
   fetch (E7) was traced. `WorkerManagementPanel.test.jsx` L76 covers only the "deliverable 1
   tray".
3. **`WorkerEditorModal.jsx`** — read header only; it is imported **solely** by the ORPHANED
   `workspace/tabs/WorkersTab.jsx` (L8/L77/L78). Per its own header (L1-2) WorkersTab was
   replaced by `WorkspaceDetailPage`. → WorkerEditorModal is **ORPHANED** (not reachable in
   the live UI).
4. **`worker_partial_result` / `context_cleared` / `token_recovery` / `worker_timeout` /
   `security_prompt`** (M6, M10, M15, M22, M23) — backend emits/EventTypes exist, but no
   frontend consumer case was confirmed; flagged UNVERIFIED rather than DEAD.
5. **Detail sub-panels inside WorkerManagementPanel** (per-worker expand/accordion, active
   tool chips) were not individually enumerated.
6. `WorkspacePanel.jsx` L116-123 documents that a `WorkerAutoOpenWatcher` was **removed**
   ("in favor of the identical auto-open logic already built into WorkerManagementPanel").

---

## 8. Verdict counts

| Verdict | Count |
|---|---|
| WIRED | 30 |
| PARTIAL | 1 |
| ORPHANED | 1 |
| DEAD | 0 |
| UNVERIFIED | 7 |
| **Total rows** | **39** |

Breakdown by table: §2 (11 rows: 11 WIRED) · §3 (10 rows: 10 WIRED) · §4 (3 rows: 2 WIRED, 1 PARTIAL)
· §5 (8 rows: 6 WIRED, 2 UNVERIFIED) · §6 (23 rows: 18 WIRED, 5 UNVERIFIED) · §7 ORPHANED=1.

### Headline findings
- **H1 (PARTIAL):** `SessionSidebar` worker **Stop** omits `instance_id`/`session_id` →
  stops all instances of the worker name, inconsistent with the panel Stop buttons (E4/F7)
  which correctly pass `instance_id`.
- Every other user-facing worker control (list/stop/pause/resume/create/update/delete, panel
  focus/move/close/resize, output bubbles) maps to a **real** backend handler
  (`workspace_routes.py`) or WS event (`bridge.py`); no stub routes found.
- `WorkerEditorModal` is reachable only via an ORPHANED tab → effectively dead UI.

---

## Appendix F1 — Orphan register + shared widgets + API-base audit

_Source: .tm-fix-scratch/ui_chunk_f1.md (182 lines: orphan register + shared widgets + API-base audit). Verdict vocabulary: see "How to read this document"._

# Frontend UI Audit — Chunk F1

**Verdict vocabulary:** WIRED / DEAD / PARTIAL / ORPHANED / UNVERIFIED
(DEAD = rendered but inert / no handler wired; ORPHANED = code never reachable
from the live entry graph; PARTIAL = partly live, partly dead.)

## Scope
Frontend web UI under `web_ui/frontend/`. Three sub-audits:
1. **Orphan register** — source reachable from the app entry graph vs. dead code.
2. **Shared widgets** — components/modules imported by >1 live importer, checking
   prop-contract consistency across hosts.
3. **API-base audit** — how the frontend addresses the backend (relative vs absolute
   host:port) and whether any addressing breaks the Vite proxy.

## Method
- Entry: `index.html` → `/src/main.jsx` → `src/App.jsx`; import graph walked by BFS
  over all `import` statements (static analysis + `grep`).
- Counts: **63** non-test source files (`.js`/`.jsx`/`.ts`); **45** live
  (reachable from `main.jsx`, non-test); **18** orphans.
- Directory-read of every file listed below; line counts recorded.
- Backend REST routes cross-checked in `web_ui/backend` where relevant.
- No repo source was modified; only this scratch report was written.

## Files read (with line counts)
Live core: `index.html`, `src/main.jsx` (18), `src/App.jsx` (1374),
`src/components/WorkspacePanel.jsx` (204), `src/components/ConfigPanel.jsx`,
`src/components/WorkspaceSelector.jsx`, `src/components/NewSessionModal.jsx` (204),
`src/components/FolderBrowser.jsx` (400), `src/components/ManageProvidersModal.jsx` (231),
`src/components/PromptLibrary.jsx` (424), `src/components/VaultHealthBanner.jsx` (107),
`src/components/MessageBubble.jsx` (221), `src/components/SessionTab.jsx`,
`src/components/chat/ChatPanel.jsx`, `src/components/chat/WorkerOutputPanel.jsx`,
`src/components/workspace/WorkspaceDetailPage.jsx`, `src/components/DomainAllowlistEditor.jsx`,
`src/components/WorkerManagementPanel.jsx`, `src/components/RecordContainerPanel.jsx`,
`src/components/SessionSidebar.jsx`, `src/components/OnboardingWizard.jsx`,
`src/components/DockerfileEditor.jsx`, `src/components/ContainerPanel.jsx`,
`src/components/SecurityDialog.jsx`, `src/components/ProviderEditModal.jsx`,
`src/utils/globalApi.js`, `src/utils/router.js`, `src/store/workspaceStore.js`,
`src/store/useStore.js`, `src/utils/permissionVocab.js`, `src/chat/adaptWorkerEvent.js`,
`src/workspace/workspaceApi.js`, `vite.config.js`.
Orphans: all 18 files in the register below.
Tests: `__tests__/WorkspaceSessionList.test.jsx` (248),
`__tests__/WorkspaceIntegration.test.jsx` (307), `__tests__/WorkspacePanel.test.jsx` (736).

## Per-verdict counts
| Verdict | Count |
|---|---|
| ORPHANED (source files) | 18 |
| WIRED (shared widgets/components verified live) | see §2 |
| DEAD (rendered but inert buttons) | 3 findings |
| PARTIAL (hosts inconsistent) | 3 findings |
| UNVERIFIED | 0 |

---

## §1 ORPHAN REGISTER (18 files)

Marker `// --- ORPHANED — replaced by WorkspaceDetailPage; do not import in new code ---`
present on line 1 of each *direct* orphan (1–9 below).

### Tagged orphans (marker present, zero live importers)
| # | File | Lines | Role | Live importers | Notes |
|---|---|---|---|---|---|
| 1 | `src/components/workspace/WorkspacePanel.jsx` | 303 | tabbed workspace shell | none | test importers: WorkspaceIntegration.test.jsx, WorkspacePanel.test.jsx |
| 2 | `.../workspace/tabs/ContainersTab.jsx` | 89 | tab | none | imported only by (1) |
| 3 | `.../workspace/tabs/CredentialsTab.jsx` | 43 | tab | none | imported only by (1) |
| 4 | `.../workspace/tabs/PermissionsTab.jsx` | 54 | tab | none | imported only by (1) |
| 5 | `.../workspace/tabs/ResourcesTab.jsx` | 58 | tab | none | imported only by (1) |
| 6 | `.../workspace/tabs/SessionDefaultsTab.jsx` | 151 | tab | none | imported only by (1) |
| 7 | `.../workspace/tabs/ToolsTab.jsx` | 46 | tab | none | imported only by (1) |
| 8 | `.../workspace/tabs/WorkersTab.jsx` | 81 | tab | none | imported only by (1) |
| 9 | `.../workspace/modals/ResourceCatalogModal.jsx` | 74 | modal (marker present) | none | imported only by orphan ResourcesTab (5) |

### Transitive orphans (no marker; unreachable only because all importers are orphans)
| # | File | Lines | Reason unreachable |
|---|---|---|---|
| 10 | `.../workspace/workspaceUtils.jsx` | 97 | imported ONLY by orphan (1) + orphan tabs/modals (2–9) |
| 11 | `.../workspace/modals/ContainerLogsModal.jsx` | 10 | imported only by orphan ContainersTab (2) |
| 12 | `.../workspace/modals/CredentialPickerModal.jsx` | 60 | imported only by orphan CredentialsTab (3) |
| 13 | `.../workspace/modals/WorkerEditorModal.jsx` | 136 | imported only by orphan WorkersTab (8) |

### Zero-importer orphans
| # | File | Lines | Notes |
|---|---|---|---|
| 14 | `src/components/SessionCreationModal.jsx` | 562 | no importers; imports FolderBrowser + WorkspaceSessionList |
| 15 | `src/components/SessionList.jsx` | 256 | no importers (grep hits elsewhere are only the substring "SessionListItem" in apiContracts.js strings) |
| 16 | `src/components/WorkspaceSessionList.jsx` | 369 | imported ONLY by orphan SessionCreationModal (14); test importer WorkspaceSessionList.test.jsx |
| 17 | `src/data/apiContracts.js` | 291 | no importers (self-documented "PURE DOCUMENTATION") |
| 18 | `src/types/workspace.ts` | 98 | no importers (self-comment "Nothing imports this file yet") |

### Trap avoided
`src/components/WorkspacePanel.jsx` (204 L) is **LIVE** — imported by `ConfigPanel.jsx`.
Do **not** confuse it with orphan (1) `src/components/workspace/WorkspacePanel.jsx` (303 L).

### Total-source vs live
- Non-test source files: **63**
- Live (reachable from `main.jsx`): **45**
- Orphans: **18**

### Dead-code tests (import orphan components)
| Test | Lines | Status | Covered describes |
|---|---|---|---|
| `__tests__/WorkspaceSessionList.test.jsx` | 248 | PURE DEAD-CODE TEST (imports only orphan WorkspaceSessionList.jsx) | empty states L76, rendering L111, interactions L174, deletion L205 |
| `__tests__/WorkspaceIntegration.test.jsx` | 307 | DEAD-CODE for the component (imports orphan workspace/WorkspacePanel.jsx L18 + live store/workspaceStore.js L19) | loading/header L92, tab navigation L126, edits reach store L157, New Session flow L192, safety advisory L217 |
| `__tests__/WorkspacePanel.test.jsx` | 736 | MIXED/PARTIAL (imports live root WorkspacePanel L16 **and** orphan workspace/WorkspacePanel.jsx as TabbedWorkspacePanel L19 + all 7 orphan tabs L21-27) | root sections live; "Phase 4 tabs — *" L482-686 and "tabbed panel" describes exercise DEAD code |

No test exists for `SessionCreationModal.jsx` or `SessionList.jsx`.

---

## §2 SHARED WIDGETS (imported by >1 live importer)

### Components
| Component | Lines | Hosts | Verdict |
|---|---|---|---|
| `VaultHealthBanner.jsx` | 107 | WorkspaceSelector.jsx:157 `<VaultHealthBanner />`; workspace/WorkspaceDetailPage.jsx:700 `<VaultHealthBanner />` — both NO props, self-contained (polls `/api/vault/status` via globalApi `fetchVaultStatus`) | **WIRED / CONSISTENT** |
| `ManageProvidersModal.jsx` | 231 | WorkspaceSelector.jsx:302-306 → `providers`, `sendCommand={() => {}}` (**NO-OP STUB**), `onClose`, `onProviderSaved={() => setShowProviders(false)}`; ConfigPanel.jsx:605-610 → `providers`, `sendCommand={sendCommand}` (**REAL**), `onClose`, `onProviderSaved` bumps providerVersion | **PARTIAL** — landing-page Add/Edit/Delete silently dead (Save → `sendCommand('save_provider')`, Delete → `sendCommand('delete_provider')` go nowhere). Landing button "⚙ Manage Providers" at WorkspaceSelector.jsx:255-256. Backend REST exists: `provider_routes.py` GET/POST/DELETE `/api/providers` |
| `PromptLibrary.jsx` | 424 | WorkspaceSelector.jsx:250 `<PromptLibrary />` (**no onSelectPrompt**); ConfigPanel.jsx:900 & 921 `<PromptLibrary onSelectPrompt={handleLoadPromptFromLibrary} />` | **PARTIAL** — PromptLibrary.jsx:145-147 `handleSelect` calls `onSelectPrompt?.(name)`; row onClick L365; on landing page a prompt-row click is a **silent no-op**. ConfigPanel "Prompt Library" section L895-921 |
| `FolderBrowser.jsx` | 400 | WorkspaceSelector.jsx:324 (LIVE) and SessionCreationModal.jsx:301 (ORPHAN) | effectively **single live host** — WIRED |
| `NewSessionModal.jsx` | 204 | WorkspaceSelector.jsx:298 `workspace={sessionTarget}`; workspace/WorkspaceDetailPage.jsx:799 `workspace={{id,name,path,root}}` (orphan workspace/WorkspacePanel.jsx:300) | **CONSISTENT** — same `workspace`/`onClose` contract |
| `MessageBubble.jsx` | 221 | chat/ChatPanel.jsx:184 `<MessageBubble key={i} msg={msg} index={i} />`; chat/WorkerOutputPanel.jsx:868 `<MessageBubble msg={msg} index={idx} />` | **WIRED / CONSISTENT** |

### Non-component shared modules (>1 live importer)
`globalApi.js` (5: GlobalCredentials, GlobalResources, VaultHealthBanner, VaultHealthPanel,
WorkspaceSelector); `router.js` (5); `store/workspaceStore.js` (7); `store/useStore.js` (4);
`permissionVocab.js` (2: WorkerManagementPanel, WorkspacePanel); `chat/adaptWorkerEvent.js`
(2: App, WorkerOutputPanel); `workspace/workspaceApi.js` (2: WorkspaceDetailPage,
useWorkspaceSummary).

**NOTE:** `SecurityDialog.jsx` is imported ONLY by `SessionTab.jsx` (1 host) → **NOT shared**
(contradicts the recon hint).

---

## §3 API-BASE AUDIT

`vite.config.js`: dev port 5173; proxy `/ws` → `ws://127.0.0.1:8000` (ws:true) and `/api` →
`http://127.0.0.1:8000`; `VITE_BACKEND_PORT` default 8000.

### Convention A — relative / same-origin (`API_BASE=''` or bare `/api/...`) → honors Vite proxy
`utils/globalApi.js:6` `const API_BASE=''` (used :10,:45,:58,:74);
`FolderBrowser.jsx:3` `''` (:92,101,123,158); `workspaceApi.js:6` `''` (:45,56,72);
`store/workspaceStore.js:14` `''` (:166,234,262,293,325-332,493,511,541,554,572,588,616,620);
`SessionCreationModal.jsx:5` `''` ORPHAN (:32,97,121); `WorkspaceSessionList.jsx:3` `''` ORPHAN (:58,88);
`DomainAllowlistEditor.jsx:33` & 96 bare `/api/workspace/{id}/domain_allowlist`;
`WorkerManagementPanel.jsx` (:578,621,639,677,705,750,780); `WorkerOutputPanel.jsx` (:619,649,676);
`RecordContainerPanel.jsx:28` `LIST_URL='/api/container-records'` (:311,332,355,426);
`SessionSidebar.jsx:72`; `OnboardingWizard.jsx` (:196,258,269,280,299,324);
`WorkspaceSelector.jsx:86`; `DockerfileEditor.jsx` (:31,62); `ContainerPanel.jsx` (:33,52).

### Convention B — absolute `http://${window.location.hostname}:${VITE_BACKEND_PORT||8000}` → BYPASSES Vite proxy
Breaks when backend is not on `hostname:8000`, or behind a proxy / in prod.
`App.jsx:50` WS_PORT; `App.jsx:1102` fetch(`http://${hostname}:${port}/api/logging/config`);
`App.jsx:1133/1137` `/api/health{,/containers}`; `App.jsx:1171` `/api/onboarding/status`;
`App.jsx:1333` PUT `/api/logging/config`; `ConfigPanel.jsx:47-48` BACKEND_PORT + API_BASE
(used :182 `/api/tools`, :209/:316 `/api/session/{id}/permissions`, :369 `/api/prompts/{name}`);
`PromptLibrary.jsx:3` API_BASE same template (:24,48,77,100,123).

### Convention C — absolute `ws://${window.location.hostname}:${WS_PORT||8000}/ws` → bypasses ws proxy
`App.jsx:51` WS_URL (`new WebSocket` App.jsx:251); `SessionTab.jsx:38` WS_URL
(`new WebSocket` SessionTab.jsx:314).

### Other
No literal hard-coded backend host other than via env default. Grep `https?://` also hits only UI
placeholders/defaults: `ProviderEditModal.jsx:187` placeholder `https://api.openai.com/v1`;
`OnboardingWizard.jsx:111/113` DEFAULT_BASE_URLS (openai/anthropic), :382 placeholder.

**SUMMARY:** 2 top-level addressing conventions — proxy-relative vs absolute host:port — or 3 if
split by scheme (relative, absolute http, absolute ws). Split confirmed (Chunk D): DomainAllowlistEditor,
ContainerPanel, WorkerOutputPanel, WorkerManagementPanel, RecordContainerPanel, SessionSidebar,
DockerfileEditor, OnboardingWizard all RELATIVE; ConfigPanel / PromptLibrary / App.jsx absolute.
**NOT confirmed as backend-breaking:** no file hardcodes a literal port constant other than the env default.

### Key evidence lines
```
App.jsx:1130        port = import.meta.env.VITE_BACKEND_PORT || '8000'
ConfigPanel.jsx:48  API_BASE = `http://${hostname}:${BACKEND_PORT}`
PromptLibrary.jsx:3 API_BASE = `http://${hostname}:${VITE_BACKEND_PORT||8000}`
ManageProvidersModal.jsx  handleSave → sendCommand('save_provider',{provider})
ManageProvidersModal.jsx  handleConfirmDelete → sendCommand('delete_provider',{provider_id})
ProviderEditModal imported by ManageProvidersModal (L12)
VaultHealthBanner.jsx:12  imports fetchVaultStatus from globalApi
```

---

## Appendix F2 — Inverse backend cross-check

_Source: .tm-fix-scratch/ui_chunk_f2.md (Tables A/B/C + top-10 notes). Verdict vocabulary: see "How to read this document"._

# UI Chunk F2 — Inverse cross-check (backend surface → UI usage)

Direction: enumerate every backend HTTP route + WS command + WS event, then find the
live frontend caller (or mark it unclaimed). "Live" = a non-test, non-orphan module
(orphan files from F1 listed in the briefing; calls inside them are labelled
`ORPHAN` and never count as live).

Verdicts: WIRED (live caller exists) · UNCLAIMED (no caller anywhere) ·
ORPHANED (only orphan/test callers) · MISMATCH (name/shape disagreement).
Dominant F2 labels: **UNCLAIMED** and **MISMATCH**.

Backend: `web_ui/backend/*.py` (server.py 3979L; 11 routers; ~76 HTTP routes + 1 WS).
Frontend: `web_ui/frontend/src` (live files only).

---

## Table A — REST routes (backend → live UI caller)

### A1. UNCLAIMED REST routes (no caller at all) — 14

| Method + path | backend@ | note |
|---|---|---|
| POST `/api/config/reset` | config_routes:71 | grep `config/reset` = 0 hits |
| POST `/api/config/mode` | config_routes:112 | grep `config/mode` = 0 hits |
| GET `/api/config/mode` | config_routes:179 | grep `config/mode` = 0 hits |
| GET `/api/providers` | provider_routes:70 | UI uses WS `get_providers` |
| POST `/api/providers` | provider_routes:80 | UI uses WS `save_provider` (no-op, F1) |
| DELETE `/api/providers/{id}` | provider_routes:111 | UI uses WS `delete_provider` (no-op, F1) |
| GET `/api/system/health` | health_routes:182 | UI hits `/api/health`, not this |
| GET `/api/session/{id}` | session_routes:245 | bare GET never fetched (WS `load_session` used); only ORPHAN `WorkspaceSessionList.jsx:88` |
| POST `/api/session/{id}/rename` | session_routes:281 | grep `session/rename` = 0; UI uses WS `rename_session` |
| GET `/api/workspace/{id}/mcp_servers` | workspace_routes:770 | grep `mcp_servers` = 0 hits |
| POST `/api/workspace/{id}/workers/stop_all` | workspace_routes:1055 | grep `stop_all` = 0 hits |
| GET `/api/workspace/{id}/permissions` | workspace_routes:1430 | only tests + PUT path; no live GET |
| POST `/api/workspace` (create) | workspace_routes:1518 | createWorkspace = `browse/create`×2 + `workspace/resolve`; never POSTs here |
| GET `/api/workspace/{id}` (detail) | workspace_routes:1774 | UI uses `/summary`, `/list`; bare detail unused |
| GET `/health` (bare) | server.py:2850 | UI uses `/api/health` |
| GET `/api/workspace/{id}/containers/{name}/status` | server.py:3164 | no live caller |

### A2. ORPHANED REST routes (only orphan/test callers) — 2

| Method + path | backend@ | only caller |
|---|---|---|
| GET `/api/workspace/{id}/containers/{name}/logs` | server.py:3621 | ORPHAN `ContainerLogsModal.jsx` |
| GET `/api/session/{id}` | session_routes:245 | ORPHAN `WorkspaceSessionList.jsx:88` (also UNCLAIMED, above) |

### A3. WIRED REST routes (live caller) — 57

| Method + path | backend@ | live caller |
|---|---|---|
| GET `/api/container-records` | cr:113 | RecordContainerPanel.jsx:28 |
| GET `/api/container-records/{id}` | cr:148 | RecordContainerPanel.jsx:13 |
| GET `/api/container-records/{id}/events` | cr:175 | RecordContainerPanel.jsx:14 |
| POST `/api/container-records/{id}/kill` | srv:3591 | RecordContainerPanel.jsx:17 |
| POST `/api/container-records/{id}/restart` | srv:3599 | RecordContainerPanel.jsx:18 |
| POST `/api/container-records/{id}/recreate` | srv:3607 | RecordContainerPanel.jsx:19 |
| GET `/api/global/summary` | global:244 | globalApi.js:19 |
| GET `/api/credentials` | global:312 | globalApi.js:36 |
| POST `/api/credentials` | global:329 | globalApi.js:45 |
| DELETE `/api/credentials/{name}` | global:346 | globalApi.js:58 |
| GET `/api/vault/repair/status` | vault:53 | globalApi.js:68 (+VaultHealthPanel) |
| POST `/api/vault/repair/apply` | vault:95 | globalApi.js:74 |
| GET `/api/logging/config` | log:33 | App.jsx:1102 |
| PUT `/api/logging/config` | log:53 | App.jsx:1333 |
| GET `/api/onboarding/status` | onb:86 | App.jsx:1171 |
| POST `/api/onboarding/complete` | onb:92 | OnboardingWizard.jsx:299,324 |
| POST `/api/onboarding/test-connection` | onb:122 | OnboardingWizard.jsx:196 |
| GET `/api/prompts` | pr:69 | PromptLibrary.jsx:24 |
| GET `/api/prompts/{f}` | pr:75 | PromptLibrary.jsx:123 |
| POST `/api/prompts/{f}` | pr:94 | PromptLibrary.jsx:48,77 |
| DELETE `/api/prompts/{f}` | pr:118 | PromptLibrary.jsx:100 |
| POST `/api/session/create` | sess:110 | workspaceStore.js:541 |
| GET `/api/session/list` | sess:191 | workspaceStore.js:554 |
| DELETE `/api/session/{id}` | sess:329 | workspaceStore.js:573 (DELETE) |
| GET `/api/session/{id}/permissions` | sess:465 | ConfigPanel.jsx:209 |
| PUT `/api/session/{id}/permissions` | sess:515 | ConfigPanel.jsx:316 |
| POST `/api/workspace/resolve` | ws:113 | workspaceStore.js:293; OnboardingWizard.jsx:280 |
| GET `/api/workspace/list` | ws:191 | workspaceStore.js:234 |
| GET `/api/workspace/templates` | ws:214 | WorkerManagementPanel.jsx:621 |
| GET `/api/workspace/{id}/dockerfile` | ws:376 | DockerfileEditor.jsx:31 |
| PUT `/api/workspace/{id}/dockerfile` | ws:754 | DockerfileEditor.jsx:62 |
| GET `/api/workspace/{id}/domain_allowlist` | ws:390 | DomainAllowlistEditor.jsx:33 |
| PUT `/api/workspace/{id}/domain_allowlist` | ws:405 | DomainAllowlistEditor.jsx:97 |
| GET `/api/workspace/{id}/workers` | ws:444 | workspaceStore.js:326-330 |
| GET `/api/workspace/{id}/workers/active` | ws:626 | WorkerManagementPanel.jsx:578 |
| POST `/api/workspace/{id}/workers` | ws:652 | WorkerManagementPanel.jsx:746 |
| PUT `/api/workspace/{id}/workers/{name}` | ws:688 | WorkerManagementPanel.jsx:745 |
| DELETE `/api/workspace/{id}/workers/{name}` | ws:728 | WorkerManagementPanel.jsx:781 |
| GET `/api/workspace/{id}/effective_permissions` | ws:785 | workspaceStore.js:328 |
| POST `/api/workspace/{id}/workers/{name}/stop` | ws:1112 | WorkerManagementPanel.jsx:640; WorkerOutputPanel.jsx:619; SessionSidebar.jsx:73 |
| POST `/api/workspace/{id}/workers/{name}/pause` | ws:1155 | WorkerManagementPanel.jsx:678; WorkerOutputPanel.jsx:649 |
| POST `/api/workspace/{id}/workers/{name}/resume` | ws:1267 | WorkerManagementPanel.jsx:706; WorkerOutputPanel.jsx:676 |
| PUT `/api/workspace/{id}/permissions` | ws:1466 | workspaceApi.js (updateWorkspacePermissions) |
| GET `/api/workspace/{id}/summary` | ws:1645 | workspaceApi.js:46 (fetchWorkspaceSummary) |
| GET `/api/browse` | srv:2724 | FolderBrowser.jsx:123 |
| POST `/api/browse/create` | srv:2803 | FolderBrowser.jsx:158; OnboardingWizard.jsx:258,269 |
| GET `/api/user-home` | srv:2844 | FolderBrowser.jsx:92,101; WorkspaceSelector.jsx:86 |
| GET `/api/tools` | srv:2761 | ConfigPanel.jsx:182; workspaceApi.js:72 (fetchTools) |
| GET `/api/health` | srv:2859 | App.jsx:1133 |
| GET `/api/health/containers` | srv:3679 | App.jsx:1137; workspaceStore.js:327 |
| GET `/api/vault/status` | srv:2865 | globalApi.js:24 (+VaultHealthBanner) |
| GET `/api/resource-catalog` | srv:2923 | globalApi.js:29 |
| GET `/api/container/integrity` | srv:2946 | ContainerPanel.jsx:52 |
| GET `/api/container/status` | srv:2994 | ContainerPanel.jsx:33 |
| GET `/api/workspace/{id}/containers` | srv:3126 | workspaceStore.js:330,582 |
| POST `/api/workspace/{id}/containers/{name}/start` | srv:3199 | workspaceStore.js containerAction('start') ← SessionSidebar |
| POST `/api/workspace/{id}/containers/{name}/stop` | srv:3230 | workspaceStore.js containerAction('stop') ← SessionSidebar |
| DELETE `/api/workspace/{id}/containers/{name}` | srv:3272 | workspaceStore.js containerAction('remove') ← SessionSidebar |

Non-API: GET `/` server.py:3795 = SPA root (not a route to check).

---

## Table B — WebSocket commands (`/ws`, server.py handler)

Backend **accepts 27**; frontend **sends 18**. 9 accepted commands have no sender.

### B1. Backend commands with NO frontend sender — UNCLAIMED (9)

| WS command | backend@ | note |
|---|---|---|
| `get_config` | server.py:1084 | grep = 0 hits in FE |
| `get_conversation` | server.py:1102 | grep = 0 |
| `stop_session` | server.py:1079 | FE only mentions in a comment (QueryBar docstring) |
| `resume_session` | server.py:1074 | never sent (`continue_session` used instead) |
| `save_session` | server.py:1734 | grep = 0 |
| `close_session` | server.py:2154 | only a comment; never actually sent |
| `set_project` | server.py:2357 | grep = 0 |
| `get_workspace_capabilities` | server.py:2536 | grep = 0 |
| `bootstrap_workspace` | server.py:2564 | grep = 0 |
| (doc-only) `update_config` | docstring L40 | no handler branch AND no sender — MISMATCH |

### B2. Backend commands WIRED (sender identified) — 18

| WS command | frontend sender |
|---|---|
| `start_session` | QueryBar.jsx:46 |
| `continue_session` | QueryBar.jsx:39,58 |
| `pause_session` | QueryBar.jsx:56 |
| `apply_config` | ConfigPanel.jsx:362; SessionSidebar.jsx:65 |
| `set_default_config` | ConfigPanel.jsx:457 |
| `save_provider` | ManageProvidersModal.jsx:66; OnboardingWizard.jsx:224 |
| `delete_provider` | ManageProvidersModal.jsx:81 |
| `get_providers` | SessionTab.jsx:422,745,788,818 |
| `get_available_tools` | SessionTab.jsx:427,627 |
| `list_sessions` | App.jsx:259,388,396,400,465 (hub) |
| `load_session` | SessionTab.jsx:391 (raw `ws.send`) |
| `load_more_messages` | SessionTab.jsx:1020 |
| `new_session` | SessionTab.jsx:1041 |
| `rename_session` | SessionTab.jsx:1088,1100 |
| `delete_session` | SessionTab.jsx:1146 |
| `get_open_sessions` | App.jsx:343 (hub, raw send) |
| `security_response` | SecurityDialog.jsx:65,74,85 |
| `rebuild_container` | ContainerPanel.jsx:89 |

---

## Table C — WebSocket events (backend emit → frontend consumer)

- **WIRED**: every event the backend actually emits (status_message, config_changed,
  conversation_changed, session_loaded, error, config_queued, config_apply_failed,
  default_config_saved, providers_list, provider_saved/deleted, tools_list,
  sessions_list, session_saved/deleted/renamed, open_sessions, session_closed,
  state_changed, tokens_updated, context_updated, more_messages, security_prompt,
  session_stop, logging_config_changed, all `worker:*` incl. worker:token_warning /
  turn_warning / time_warning / context_cleared / token_recovery) has a frontend
  `case '...'` (App.jsx 352-469; SessionTab.jsx 501-974; also WorkerOutputPanel + adaptWorkerEvent).

- **Backend emits, frontend IGNORES — UNCONSUMED (2):**
  | event | backend@ | note |
  |---|---|---|
  | `workspace_capabilities` | server.py:2553 | no `case` in FE (from UNCLAIMED `get_workspace_capabilities`) |
  | `workspace_bootstrapped` | server.py:2575 | no `case` in FE (from UNCLAIMED `bootstrap_workspace`) |

- **Frontend consumes, backend NEVER emits — DEAD handler (1):**
  | event | frontend@ | note |
  |---|---|---|
  | `session_cleared` | SessionTab.jsx:974 | grep backend = never emitted (docstring L75 likewise dead) |

- **MISMATCH (doc only):** docstring server.py:73 says `open_sessions_list`; code emits
  `open_sessions` (server.py:2148); FE listens for `open_sessions` (App.jsx:402).

---

## "How to reach" — top 10 actionable backend surfaces

1. `POST /api/config/mode` + `GET /api/config/mode` (config_routes:112/179) — no UI
   mode switch calls the HTTP route; mode is only passed inside `continue_session`/`start_session` config.
2. `GET/POST/DELETE /api/providers` (provider_routes) — full REST CRUD is dead; the UI
   provider modal drives the WS `save_provider`/`delete_provider` no-ops instead (see F1).
3. `POST /api/workspace/{id}/workers/stop_all` (workspace_routes:1055) — "stop all workers"
   capability exists but no button/flow reaches it.
4. `GET /api/workspace/{id}/containers/{name}/logs` (server.py:3621) — container log
   viewer only exists in the orphan `ContainerLogsModal`.
5. `GET /api/workspace/{id}/mcp_servers` (workspace_routes:770) — MCP server listing has no UI consumer.
6. `GET /api/system/health` (health_routes:182) — no UI (App uses `/api/health`).
7. `GET /api/workspace/{id}` bare detail (workspace_routes:1774) + `GET /api/workspace/{id}/permissions`
   (1430) — UI reads `/summary` instead; raw detail/permissions GET unused.
8. WS `get_workspace_capabilities` (2536) / `bootstrap_workspace` (2564) — commands never
   sent, and their events (`workspace_capabilities`/`workspace_bootstrapped`) are unconsumed.
9. WS `get_config`/`get_conversation`/`save_session`/`set_project`/`close_session`/`stop_session`/
   `resume_session` — accepted but never sent by the UI.
10. `POST /api/workspace` create (1518) + `POST /api/session/{id}/rename` (281) — REST
    equivalents exist but the UI routes creation through `resolve` and rename through WS.

_Caveat: this is a static literal/regex cross-check (greps over `web_ui/frontend/src`).
"UNCLAIMED" = no string literal for the endpoint in any live file; a dynamically-built
URL that never appears literally would be missed, though the central API modules
(globalApi.js, workspaceApi.js, workspaceStore.js) were inspected and use literals._

---

## Appendix G — Provider path verification

_Source: .tm-fix-scratch/ui_chunk_g.md (provider path verification (short)). Verdict vocabulary: see "How to read this document"._

# CHUNK G — Provider path verification (settling CHUNK A vs CHUNK F2)

Read-only verification. Repo root `/workspace` (= `.../ThoughtMachine-dev`). No source modified.

## TL;DR verdict
- **CHUNK A is CORRECT.**
- **CHUNK F2 is WRONG** on its claim that the provider save/delete paths are "documented no-ops".
- The **backend is fully implemented and persists on BOTH transports** (WebSocket *and* REST), sharing one store.
- The only real defect is **frontend wiring on the landing page** (`WorkspaceSelector`).

---

## 1. WebSocket commands — IMPLEMENTED & PERSISTING (not no-ops)

`save_provider` — `web_ui/backend/server.py:1571-1627`:
```
1571:  elif command == "save_provider":
1572:      # Save (add or update) a provider profile
1573:      from agent.config.provider_profile import ProviderManager, ProviderProfile
...
1590:      profile = ProviderProfile(**provider_data)     # (constructor)
1591:      manager.add_profile(profile)
1592:      manager.save()
...
1608:      await ws.send_json({ "type": "provider_saved", "provider": provider_data })   # 1609
1612:      await ws.send_json({ "type": "providers_list", "providers": safe_profiles })  # 1613
```

`delete_provider` — `web_ui/backend/server.py:1629-1677`:
```
1629:  elif command == "delete_provider":
...
1641:      manager.delete_profile(provider_id)
1642:      manager.save()
...
1658:      await ws.send_json({ "type": "provider_deleted", "provider_id": provider_id }) # 1659
1662:      await ws.send_json({ "type": "providers_list", "providers": safe_profiles })   # 1663
```

`get_providers` — `server.py:1529-1569`: `ProviderManager().list_profiles()` → emits `providers_list` with a `session_id` (F2 routing fix).

Failure branches emit `status_message` (e.g. `1620 "⚠ Failed to save provider"`, `1670 "⚠ Provider '{id}' not found"`) — proving real work with real error handling, not a stub.

## 2. REST endpoints — IMPLEMENTED & PERSISTING

`web_ui/backend/provider_routes.py` (122 lines). Header docstring L4-7:
> "Mirrors the WebSocket command surface (`get_providers` / `save_provider` / `delete_provider` in `server.py`) as REST endpoints backed by the **same** `ProviderManager` … which persists profiles to `~/.thoughtmachine/providers.json`."

```
L70  @router.get("/providers")            -> _provider_manager().list_profiles()   (L74)
L80  @router.post("/providers")           -> ProviderProfile(**provider_data) (99)
                                            manager.add_profile(profile)      (103)
                                            manager.save()                    (104)
L111 @router.delete("/providers/{id}")    -> manager.delete_profile(...)     (118)
                                            manager.save()                    (120)
```
Store file: `PROVIDERS_STORE = PROVIDERS_FILE` (L37), used identically by WS and REST.

## 3. Shared persistence → no divergence
Both transports import `ProviderManager` / `ProviderProfile` / `PROVIDERS_FILE` from `agent/config/provider_profile.py`. Same file, same store (`~/.thoughtmachine/providers.json`). There is **no second, divergent code path** and **no "documented no-op" anywhere**. F2's characterization does not match the source.

---

## 4. Frontend hosts — the ACTUAL defect (landing page only)

### 4a. Landing page: `WorkspaceSelector.jsx` — BROKEN (two independent reasons)
```jsx
301:        {showProviders && (
302:          <ManageProvidersModal
303:            providers={providers}
304:            sendCommand={() => {}}            // <-- NO-OP (confirmed)
305:            onClose={() => setShowProviders(false)}
306:            onProviderSaved={() => setShowProviders(false)}
307:          />
```
And the `providers` prop source (L78):
```jsx
78:  const providers = Array.isArray(summaryData.providers) ? summaryData.providers : []
```
`summaryData` = `GET /api/global/summary`. But `global_routes.py:_build_summary()` (L234-238) returns **only** `workspaces`, `active_sessions`, `active_containers` — **no `providers` key**. So on the landing page:
- `providers` is always `[]` → the modal always shows "No providers configured yet".
- `sendCommand` is a no-op → Add/Edit/Delete silently do nothing.

→ Landing-page "Manage Providers" is broken in BOTH directions (list + mutation).

### 4b. In-session: `ConfigPanel.jsx` — WORKS
```jsx
604:  {showManageProviders && (
605:    <ManageProvidersModal
606:      providers={providers}
607:      sendCommand={sendCommand}                 // REAL sender
608:      onClose={() => setShowManageProviders(false)}
609:      onProviderSaved={() => setProviderVersion(v => v + 1)}
```
`providers` here is a real prop: `SessionTab.jsx:1203  providers={providers}`, sourced from the store, populated by the `providers_list` handler (`SessionTab.jsx:803-808 → useStore.receiveProvidersList(...)`). `provider_saved` handler re-issues `get_providers` (`SessionTab.jsx:817-818`). So the in-session Manage Providers modal works end-to-end.

`ManageProvidersModal.jsx` itself is correct: `handleSave` → `sendCommand('save_provider', { provider: providerData })` (L65-69); `handleConfirmDelete` → `sendCommand('delete_provider', { provider_id: confirmDeleteId })` (L79-85). `ProviderEditModal.jsx` sends the full profile shape (L112-121): `id, label, provider_type, base_url, api_key, default_model, models[], timeout` — matching `ProviderProfile`.

**Practical consequence:** Add/Edit/Delete works from a **session's Config panel**, but is dead from the **landing page**.

---

## 5. Test coverage — explains the green-shipped bug
- **No frontend test exists** for `ManageProvidersModal` or `ProviderEditModal` (no such `*.test.jsx` under `web_ui/frontend/src/components/__tests__/`). Nothing asserts `sendCommand('save_provider' ...)` / `('delete_provider' ...)` from either host.
- `WorkspaceSelector.test.jsx:88` stubs `providers: []` in the summary fixture and only asserts the section **heading** renders (L147) — it never opens the modal, so the no-op `sendCommand` and the missing summary key go undetected.
- Backend: the only provider test file, `web_ui/backend/tests/test_ws_mock_provider.py`, is **0 bytes (empty placeholder)**.
- Net: the landing-page death is invisible to CI because every relevant path is either untested or mocked to empty. Green ≠ correct.

---

## 6. One-line fix implication
Frontend-only. Two issues to fix at `WorkspaceSelector.jsx`:
1. **L304**: replace `sendCommand={() => {}}` with a real sender (the hub WS `sendCommand`) so Add/Edit/Delete actually dispatch.
2. **L78 / global summary**: the `/api/global/summary` payload has no `providers` array, so the landing modal always lists nothing — either add `providers` to `_build_summary()` (`global_routes.py:234`) or fetch providers for the landing modal separately (e.g. REST `GET /api/providers`, which already exists).

(The single most likely intended fix is #1; #2 is a second, independent landing-page defect that must also be addressed for the list to populate.)

---

## Appendix Z — Background / original reconstruction (recon)

_Source: .tm-fix-scratch/ui_recon.md (438-line original recon: stack / routing / inventory / backend route families). Verdict vocabulary: see "How to read this document"._

# ThoughtMachine Web UI — Scoping Recon

Reconnaissance of the ThoughtMachine web UI (`web_ui/frontend/`) against the
FastAPI backend (`server.py` + `*_routes.py`). Purpose: map the surface so a
later "UI fix" pass can be chunked. No source was modified; the only artifact
written is this file.

Repo root (workspace): `/home/jojo/PycharmProjects/ThoughtMachine-dev`
Frontend root: `web_ui/frontend/`

---

## Q1 — Stack

- **Framework:** React `^18.3.1` + `react-dom ^18.3.1`. Plain JSX/JS (**no
  TypeScript**), built with **Vite `^5.4.0`** + `@vitejs/plugin-react ^4.3.0`.
- **Testing:** `vitest ^3.0.0` with `jsdom`; `@testing-library/{dom,react,user-event,jest-dom}`. Script `test = vitest run`, `test:watch`.
- **Runtime deps:** `react-markdown ^10.1.0`, `remark-gfm`, `react-window ^2.2.7`,
  `zustand ^4.5.0`, `react-router-dom ^6.26.2`. NOTE: `react-router-dom` is
  declared in `package.json` but **not installed / not used** — a `_comment`
  (line 6) states a dependency-free hash router is used instead (see Q2).
- **Scripts:** `dev = vite`, `build = vite build`, `preview = vite preview`.
  `engines.node >= 18`.
- **`vite.config.js`:** dev server port `5173` (`VITE_PORT`); proxy
  `/ws -> ws://127.0.0.1:8000` (`ws: true`) and `/api -> http://127.0.0.1:8000`.
  `VITE_BACKEND_PORT` default `8000`.
- **Entry chain:** `index.html` → `/src/main.jsx` (18 lines) → `App.jsx` (1374 lines).

## Q2 — Routing

`src/router.js` (51 lines): dependency-free **hash** router. `parseHash(hash)` returns:

| hash | route |
|---|---|
| `''`, `#`, `/`, `#/workspaces` | `{ view: 'selector' }` |
| `#/workspace/:id` | `{ view: 'workspace', id }` |
| `#/workspace/:wsId/session/:sid` | `{ view: 'session', id: sid, workspaceId: wsId }` |
| `#/session/:id` (legacy) | `{ view: 'session', id, workspaceId: null }` |
| anything else / malformed (`#/workspace/ws/session` without id) | `null` |

Exports `parseHash`, `useRoute`, `useNavigate`. Three-layer model:
selector → workspace → session. `router.test.js` asserts every branch.

## Q3 — Component inventory

`src/components/` (root, 33 files): `ChatPanel` (199), `ConfigPanel` (981),
`ContainerPanel` (177), `DockerfileEditor` (194), `DomainAllowlistEditor` (305),
`ErrorBoundary` (110), `FolderBrowser` (400), `GlobalContainers` (23),
`GlobalCredentials` (158), `GlobalResources` (62), `GlobalSessions` (54),
`LoggingPanel` (376), `ManageProvidersModal` (231), `OnboardingWizard` (540),
`PromptLibrary` (424), `ProviderEditModal` (302), `QueryBar` (125),
`RecordContainerPanel` (730), `SecurityDialog` (216), `SessionCreationModal`
(562), `SessionList` (256), `SessionSidebar` (222), `SessionTab` (1276),
`StatusBar` (42), `TabBar` (73), `VaultHealthBanner` (107), `VaultHealthPanel`
(486), `WorkerManagementPanel` (1329), `WorkerOutputPanel` (943),
`WorkerPanelArea` (165), `WorkspacePanel` (204), `WorkspaceSelector` (374),
`WorkspaceSessionList` (369).

`src/components/chat/`: `MessageBubble` (221), `adaptWorkerEvent.js` (499),
`workerEventRouting.js` (45).

`src/components/workspace/`: `WorkspaceDetailPage.jsx` (810),
`WorkspacePanel.jsx` (303), `useWorkspaceSummary.js` (45), `workspaceApi.js`
(76), `workspaceUtils.jsx` (97); `modals/`: `ContainerLogsModal` (10),
`CredentialPickerModal` (60), `NewSessionModal` (204), `ResourceCatalogModal`
(74), `WorkerEditorModal` (136); `tabs/`: `ContainersTab` (89),
`CredentialsTab` (43), `PermissionsTab` (54), `ResourcesTab` (58),
`SessionDefaultsTab` (151), `ToolsTab` (46), `WorkersTab` (81).

`src/`: `App.jsx` (1374), `main.jsx` (18), `router.js` (51), `globalApi.js` (84),
`sessionTabsStore.js` (128), `styles.css` (2182); `store/useStore.js` (285),
`store/workspaceStore.js` (657); `data/apiContracts.js` (291),
`data/permissionVocab.js` (51), `data/purposeDefinitions.json` (116);
`types/workspace.ts` (98).

### Orphan / dead code

Files marked on line 1 with
`// --- ORPHANED — replaced by WorkspaceDetailPage; do not import in new code ---`:

- `src/components/workspace/WorkspacePanel.jsx` (the _tabbed_ panel)
- **all** of `src/components/workspace/tabs/*`
- `src/components/workspace/modals/ResourceCatalogModal.jsx`

**Correction to earlier assumption:** `src/components/WorkspacePanel.jsx`
(root dir, 204 lines) is **NOT orphaned** — it has no marker and is still
imported by `ConfigPanel.jsx` (mounted as the default workspace section) and
exercised by `WorkspacePanel.test.jsx`. It is live.

`data/apiContracts.js` is **pure documentation** — nothing imports it.

## Q4 — First-pass interactive elements (per page)

Main entry pages, top-level controls:

**App.jsx**
- Backend-down banner: `Dismiss` button (L1239).
- Docker-degraded banner (L1248, informational).
- `TabBar` (L1258): session tabs (click = select), per-tab `✕` close,
  `+` new tab, `Logging` toggle.
- `WorkerPanelArea` (L1348): panel close/focus/resize/maximize/pin/move-left/right.
- `LoggingPanel` (L1325): close, retry, save.
- `OnboardingWizard` (L1366) shown while `onboardingDone === false`.

**WorkspaceSelector (landing / `#/workspaces`)**
- `+ New Workspace` (→ custom-workspace modal), `Refresh`.
- Workspace cards (role=button; click / Enter / Space → open workspace).
- `+ New Session` (→ workspace chooser modal: `<select>` + Cancel / Create Session).
- `⚙ Manage Providers` (opens `ManageProvidersModal`).
- Global sections: `GlobalSessions`, `PromptLibrary`, `VaultHealthPanel`,
  `GlobalCredentials`, `GlobalResources`, `GlobalContainers`.
- Custom-workspace modal: risk-acknowledgement checkbox + `Proceed` + `Cancel`;
  `FolderBrowser` to pick root.

**WorkspaceDetailPage (`#/workspace/:id`)** — tabs `Overview`, `Permissions &
Resources`, `Containers`, `Workers`, `Session Defaults` (placeholder),
`Tools`, `Credentials` (placeholder). Controls: `← Back to workspaces` link,
`+ New Session` (→ `NewSessionModal`), Host-execution toggle (role=switch, opens
confirm modal on enable), `Apply`, and per-tab controls.

**SessionTab (`#/workspace/:wsId/session/:sid`)**
- Header: `Rename` (inline input, Enter/Escape), `← Back to Workspace`,
  `Details` (toggles `SessionSidebar`), `Delete` (inline Yes/No confirm).
- Error banner: `Start New Session` (stale) or `✕` dismiss.
- Resize handle, `StatusBar`, `QueryBar`, `ChatPanel`, `ConfigPanel`,
  `SessionSidebar`, `SecurityDialog`, `WorkerPanelArea`.
- WS commands sent: `get_providers`, `get_available_tools`, `load_more_messages`,
  `new_session`, `rename_session`, `delete_session`.

**QueryBar** — `<textarea>` (placeholder "Enter your query…", Enter/Shift-Enter),
`▶ Run` / `⏸ Pause` / `Pausing…` toggle button.

**ChatPanel** — `← Load older messages` (load-more), `↑` previous-query jump,
`↓` scroll-to-bottom. Messages rendered by `MessageBubble`.

**ConfigPanel** (981 lines) — `Save as Default` header button; `TAB_KEYS` tab
bar; `Manage Providers`; model/provider/system-prompt/temp/etc. fields;
`Apply` button (disabled until dirty / connected).

**SessionSidebar** — close `✕`; tool checkboxes (toggle); per-worker `Stop`;
per-container `Start`/`Stop`.

**SessionList** — `+ New`; session cards (open tab); inline rename; `Rename` /
`Delete` per card.

**WorkerManagementPanel** — `+ New Worker`, `From Template` (template picker),
per-worker `Edit` / `Del`, `Pause`/`Resume` toggle, `Stop`, worker form
(name/description/system prompt/tools/permission JSON/timeouts/temp).

**WorkerOutputPanel** — `Stop` / `Pause` / `Resume`.

**OnboardingWizard** — 4 steps: Skip/Get Started → provider form (provider
select, base URL, API key + Show/Hide, model, `Test connection`, Back/`Save &
Continue`) → workspace form (name, description, host-resources checkbox, Back/
`Create Workspace`) → finish (Back/`Finish`).

**LoggingPanel** — close, level `<select>`, tag filter input, per-area tag
checkboxes + levels, truncation numbers, `Apply`, raw-config toggle.

**PromptLibrary** — `+ New Prompt`, list rows (select), edit/delete per row,
create/edit form (name + content, Save/Cancel).

**FolderBrowser** — breadcrumb buttons, `＋ New Folder`, `↑ Parent`, folder
rows, `Select This Folder`.

**DomainAllowlistEditor** — domain input + add, per-domain remove, `Save`.

**RecordContainerPanel** — `Refresh Records`, record rows (select), `Close
detail`, drift JSON toggles, action buttons (`Kill`/`Restart`/`Recreate`),
reason input.

**GlobalCredentials** — per-cred `Delete` + confirm, `+ Add Credential` (modal:
name/secret, Save/Cancel).

**TabBar** — tab select/close, `+` new, `Logging`.

## Q5 — API layer

`globalApi.js` (84 lines), `API_BASE = ''` (same-origin), `safeGet()` degrades to
`null` on failure. Helpers: `fetchGlobalSummary` → `GET /api/global/summary`;
`fetchVaultStatus` → `GET /api/vault/status`; `fetchResourceCatalog` →
`GET /api/resource-catalog`; `fetchCredentials` → `GET /api/credentials`;
`createCredential` → POST; `deleteCredential(name)` → DELETE;
`fetchVaultRepairStatus`; `fetchVaultRepairApply`.

Frontend fetch sites (file:line):

| file | endpoint(s) |
|---|---|
| `DomainAllowlistEditor.jsx:33/97` | `GET`/`PUT /api/workspace/{id}/domain_allowlist` |
| `WorkerManagementPanel.jsx:578` | `/api/workspace/{ws}/workers/active` |
| `WorkerManagementPanel.jsx:621` | `/api/workspace/templates` |
| `WorkerManagementPanel.jsx:640/678/706` | workers stop/pause/resume |
| `WorkerManagementPanel.jsx:745/746/781` | workers PUT/POST/DELETE |
| `WorkerOutputPanel.jsx:619/649/676` | workers stop/pause/resume |
| `RecordContainerPanel.jsx:28` | `/api/container-records` |
| `SessionSidebar.jsx:73` | `/workers/{name}/stop` |
| `OnboardingWizard.jsx:196/269/280/324` | onboarding test-connection / browse create / workspace resolve / complete |
| `WorkspaceSelector.jsx:86` | `/api/user-home` |
| `DockerfileEditor.jsx:31/62` | `GET`/`PUT` workspace dockerfile |
| `ContainerPanel.jsx:33/52` | `/api/container/status`, `/api/container/integrity` |
| `ConfigPanel.jsx:47-48` | `API_BASE = http://${hostname}:${VITE_BACKEND_PORT||8000}` |
| `ConfigPanel.jsx:315` | `PUT /api/session/{id}/permissions` |
| `ConfigPanel.jsx` (mount) | `GET /api/tools` |
| `workspaceApi.js` | `/api/workspace/*` |

Note the mixed addressing: most components fetch via `globalApi`/relative paths
(proxy-friendly), while `ConfigPanel.jsx` hard-codes `http://<host>:8000` —
which bypasses the Vite proxy.

`data/apiContracts.js` documents contracts but is imported nowhere.

## Q6 — Backend routes

Root FastAPI app in `server.py` (L814). `include_router` order (L2655-2667):
`workspace_router, onboarding_router, config_router, health_router,
logging_router, session_router, prompt_router, global_router, provider_router,
vault_repair_router, container_record_router`.

- **`workspace_routes.py`** prefix `/api/workspace`: `POST /resolve` (113);
  `GET /list` (191); `GET /templates` (214); `GET /{ws}/dockerfile` (376);
  `GET`/`PUT /{ws}/domain_allowlist` (390/405); `GET /{ws}/workers` (444);
  `GET /{ws}/workers/active` (626); `POST /{ws}/workers` (652);
  `PUT`/`DELETE /{ws}/workers/{name}` (688/728); `PUT /{ws}/dockerfile` (754);
  `GET /{ws}/mcp_servers` (770); `GET /{ws}/effective_permissions` (785);
  `POST /{ws}/workers/stop_all` (1055); `POST /{ws}/workers/{name}/stop`
  (1112) / `pause` (1155) / `resume` (1267); `GET`/`PUT /{ws}/permissions`
  (1430/1466); `POST ""` create (1518); `GET /{ws}/summary` (1645); `GET /{ws}` (1774).
- **`session_routes.py`** prefix `/api/session`: `POST /create` (110);
  `GET /list` (191); `GET /{id}` (245); `POST /{id}/rename` (281);
  `DELETE /{id}` (329); `GET`/`PUT /{id}/permissions` (465/515).
- **`global_routes.py`** prefix `/api`: `GET /global/summary` (244);
  `GET`/`POST /credentials` (312/329); `DELETE /credentials/{name}` (346).
- **`provider_routes.py`** prefix `/api`: `GET`/`POST /providers` (70/80);
  `DELETE /providers/{id}` (111).
- **`prompt_routes.py`** prefix `/api/prompts`: `GET ""` (69); `GET`/`POST`/`DELETE /{filename}` (75/94/118).
- **`config_routes.py`** prefix `/api/config`: `POST /reset` (71); `POST /mode`
  (112); `GET /mode` (179).
- **`onboarding_routes.py`** prefix `/api/onboarding`: `GET /status` (86);
  `POST /complete` (92); `POST /test-connection` (122).
- **`health_routes.py`** prefix `/api/system`: `GET /health` (182).
- **`logging_routes.py`** prefix `/api/logging`: `GET`/`PUT /config` (33/53).
- **`vault_repair_routes.py`** prefix `/api`: `GET /vault/repair/status` (53);
  `POST /vault/repair/apply` (95) — origin-guarded.
- **`container_record_routes.py`** prefix `/api/container-records`: `GET ""`
  (113); `GET /{record_id}` (148); `GET /{record_id}/events` (175).
- **`server.py` direct:** `WS /ws` (872); `GET /api/browse` (2724);
  `GET /api/tools` (2761); `POST /api/browse/create` (2803); `GET /api/user-home`
  (2844); `GET /health` (2850) + `/api/health` (2859); `GET /api/vault/status`
  (2865); `GET /api/resource-catalog` (2923); `GET /api/container/integrity`
  (2946); `GET /api/container/status` (2994); `GET /api/workspace/{id}/containers`
  (3126); `GET .../containers/{name}/status` (3164); `POST .../start` (3199) /
  `.../stop` (3230); `DELETE .../containers/{name}` (3272);
  `POST /api/container-records/{id}/kill` (3591) / `restart` (3599) /
  `recreate` (3607); `GET /api/workspace/{id}/containers/{name}/logs` (3621);
  `GET /api/health/containers` (3679); `GET /` (3795).

## Q7 — State & wiring

- **zustand stores:** `store/useStore.js` (285) — sessions, `sessionConfigs`,
  `sessionDrafts`, `tabRunningStates`, `sessionStates`; `store/workspaceStore.js`
  (657); `sessionTabsStore.js` (128) — tabs keyed by workspace, persisted to
  `localStorage['tm.sessionTabs.<wsId>']`.
- **WebSockets:** `App.jsx` owns the hub socket `ws://${hostname}:8000/ws`
  (`MAX_RECONNECT_ATTEMPTS = 5`). Hub commands: `list_sessions`,
  `get_open_sessions`. Hub events: `sessions_list`, `session_saved`,
  `session_deleted`, `session_renamed`, `session_closed`, `open_sessions`,
  `session_loaded`, `state_changed`. Each mounted `SessionTab` opens its **own**
  WS.
- **localStorage:** `tm.sessionTabs.*`, `tm.workerPanels.*` (incl. `.focused`),
  legacy `activeSessionId` (written L1196), `workerPanelState` (removed L1041),
  `thoughtmachine_last_workspace`, `lastSessionMode`.
- `ConfigPanel` uses `API_BASE = http://host:8000` (see Q5).

### App.jsx render (L780-1374)
Handlers: `handleSessionSaved` (hub `list_sessions`, L780),
`handleNewSessionCreated` (783), `handleSessionAdopted` (805),
`handleOpenNewTab` (822), `handleRegisterTab` (844, `tabActionsRef`).
Boot-restore effect (850, `hydratedRef`); panels restore (900); strip-hydrate
strip on workspace route (937); route-driven tab activation (948); keep-mounted
deck (982); persist panels (1013)/focused (1024); drop legacy `workerPanelState`
(1039); stale-key cleanup (1048); pending worker-selection flush (1084);
`fetchLoggingConfig` (1098, `GET /api/logging/config`); `checkBackendHealth`
(1126, `GET /api/health` then `/api/health/containers`, poll 10 s); onboarding
status (1167, `GET /api/onboarding/status`); persist `activeSessionId` (1194).

Render tree: `.app-container` → backendDown banner (1239) → docker-degraded
banner (1248) → `.app-main` → `.app-center.tab-content-area` →
`{ TabBar (1258; when route!=selector && currentWs && tabs.length>0) ; session
deck (1278; route==session; maps deckSessions → SessionTab) ; WorkspaceDetailPage
(1317; route==workspace) ; WorkspaceSelector (1318; route==selector) ; loading
placeholder (1320) }` → `LoggingPanel` (1325) → `WorkerPanelArea` (1348) →
`OnboardingWizard` (1366; only while `onboardingDone === false`).

Hub section (L1-230): `WS_URL`, `workerPanelInstanceKey = worker_name#instance_id`,
`bootSessionIds()`, state (`hubWs`, `hubReady`, `showLoggingPanel`,
`loggingConfig(+Error)`, `workerEvents` per session capped at 500 deduped by
`canonical type|timestamp|instance`, `dockerHealth`, `backendDown`,
`backendBannerDismissed`, `onboardingDone`), `hubSend`, `wizardSend`, panel
handlers (L250-600).

## Q8 — Tests (39 files)

Located over three dirs: `src/components/__tests__/` (35 files),
`src/components/workspace/WorkspaceDetailPage.test.jsx`,
`src/store/__tests__/{sessionTabsStore,useStore,workspaceStore}.test.js`.

**Wiring / integration tests (what they assert):**

- `router.test.js` (48) — pure `parseHash` for every branch (selector /
  workspace / nested session with explicit workspaceId / legacy session /
  malformed → null).
- `Navigation.test.jsx` (276) — renders the **real `App`** with stubbed
  fetch + mocked WebSocket; drives real hash navigation through the three layers
  (selector → `WorkspaceDetailPage` → `SessionTab`). Asserts: `← Back to
  workspaces` link; session-level `← Back to Workspace` goes to the **owning**
  workspace via nested URL; browser back/forward via hashchange; tabs are
  workspace-scoped in `sessionTabsStore.byWorkspace` (tabs under A never appear
  under B).
- `SessionRouteMounting.test.jsx` (220) — real `App`; on the workspace route NO
  `SessionTab` body mounts (no `.tab-wrapper`, hub WS is the only socket);
  on the nested session route the active session mounts its own WS and sends
  `load_session` (`session_id === 'sess-1'`).
- `SessionTabsIntegration.test.jsx` (389) — real `App` (hub + TabBar +
  SessionTab): `open_sessions` builds the strip **lazily** (one SessionTab WS for
  the active tab only); clicking a strip tab activates it and mounts its own WS
  (`load_session`); closing a tab is frontend-only (session survives in store);
  deep link restores persisted strip and activates target; persisted entry keeps
  2 tabs.
- `SessionTabHubStatus.test.jsx` (297) — real `App` on workspace route (strip
  visible, no per-session WS): hub-relayed `state_changed` paints tab classes
  (`.paused` vs `.idle`/`.pausing`/`.running`) and syncs `tabRunningStates[sid]` +
  `sessionStates[sid]`; live RUNNING→PAUSING→PAUSED→IDLE transitions; events
  missing `session_id`/`state` ignored; cross-workspace events don't paint the
  current strip; events arriving before the list still recorded.
- `SessionTabKeepMounted.test.jsx` (293) — real `App`; keep-mounted deck: hidden
  pane keeps WS OPEN and preserves draft across A→B→A; lazy mount (SessionTab
  mounts on first activation, later switches toggle
  `.session-tab-pane`/`.session-tab-pane-hidden`); leaving the session layer
  unmounts the deck (sockets close; re-entry mounts fresh, draft not resurrected).
- `ConfigPanelDraft.test.jsx` (424) — real `ConfigPanel`; permissions are
  REST-driven (`GET`/`PUT /api/session/{id}/permissions`) and tab-local; the
  store draft (`sessionDrafts`) carries only the 13 non-permission config keys,
  survives tab switches (unmount) and clears on apply-ok.
- `WorkspaceIntegration.test.jsx` (307) — real tabbed `workspace/WorkspacePanel`
  + real hash router + real workspace store; walks config load → header/advisory
  → tab navigation → store updates → New Session modal; store fans out to
  `/api/workspace/list`, `/{id}/effective_permissions`, `/api/health/containers`,
  `/{id}/workers`, `/{id}/containers`, `/api/session/list`.
  (NB: asserts against the **orphaned** tabbed panel — see Q3.)
- `WorkspaceDetailPage.test.jsx` (627) — real `WorkspaceDetailPage`: summary
  render, exact security-posture strings, host-execution toggle + confirm modal,
  permission-ceiling editing, containers/workers/tools tabs, placeholder tabs,
  no-dummy-data guarantee, `NewSessionModal`.
- `WorkspaceDetailPagePermissions.test.jsx` (347) — Permissions & Resources tab
  against stubbed `GET /api/workspace/{id}/summary`, `PUT .../permissions`,
  `/api/vault/status`; resource cards, dirty tracking, tool chips, apply flow,
  and the session-create body `{ mode:'engineer', workspace_id }` (omits
  `workspace_path`).
- `WorkspacePanel.test.jsx` (736) — real root `WorkspacePanel` **and** tabbed
  `workspace/WorkspacePanel`; child editors fetch on mount; effective-permission
  pills read `useStore` `sessionConfigs[sessionId].permissions ?? PERMISSION_DEFAULTS`.

Other suites (component-level): `AppHealthBanner`, `ConfigPanel`,
`GlobalContainers`, `GlobalCredentials`, `GlobalResources`, `GlobalSessions`,
`NewSessionModal`, `OnboardingWizard`, `RecordContainerPanel`, `SessionRename`,
`SessionSidebar`, `SessionTab`, `SessionWorkerIsolation`, `VaultHealthBanner`,
`VaultHealthBannerSeverity`, `VaultHealthPanel`, `WorkerManagementPanel`,
`WorkerManagementPanelActiveInstances`, `WorkerOutputPanel`, `WorkerPanelArea`,
`WorkspaceSelector`, `WorkspaceSessionList`; unit: `adaptWorkerEvent.test.js`,
`permissionVocab.test.jsx`, `workerEventRouting.test.jsx`; store:
`sessionTabsStore`, `useStore`, `workspaceStore`.

## Q9 — Known suspects (bugs / smells to fix)

**(a) "Save as Default" round-trip.** `ConfigPanel.jsx` header button
(label at L461, `onClick` L454-458): strips `session_permissions`, calls
`sendCommand('set_default_config', { config: defaultsPayload })`, sets pending
(L458). Status flows back via prop `defaultConfigSaveStatus` from
`SessionTab.jsx:837` on the `default_config_saved` WS event. Covered by
`ConfigPanel.test.jsx`.

**(b) Landing-page Providers are a silent no-op.** `WorkspaceSelector.jsx`
renders the Providers section → `ManageProvidersModal` with
`providers={summaryData.providers}` (from `GET /api/global/summary`) **and
`sendCommand={() => {}}` (a NO-OP STUB)**. `ManageProvidersModal.handleSave`
calls `sendCommand('save_provider', {provider})` and `handleConfirmDelete` calls
`sendCommand('delete_provider', {provider_id})` — both go nowhere. So add / edit
/ delete from the landing page **silently does nothing**. The backend REST routes
exist (`GET`/`POST`/`DELETE /api/providers`, `provider_routes.py` 70/80/111) but
the frontend uses WS. `ProviderEditModal` is a controlled form (id, label, type,
`base_url`, `api_key`, `default_model`, models, timeout).

## Q10 — Suggested chunk plan

Split the UI work into independent, reviewable chunks (each = one logical area,
testable against the existing vitest suites). Ordered by blast radius:

1. **Providers wiring (fix Q9-b).** Land `ManageProvidersModal` on the working
   provider path (REST `/api/providers` or a real WS command from `App`), stop
   passing the no-op stub. Small, self-contained; touches `WorkspaceSelector.jsx`
   (+ `ManageProvidersModal.jsx`). No existing test asserts the no-op, so add one.
2. **ConfigPanel "Save as Default" (verify Q9-a).** Confirm the
   `set_default_config` → `default_config_saved` round-trip end to end; if a bug
   surfaces, fix in `ConfigPanel.jsx` / `SessionTab.jsx`. Guards:
   `ConfigPanel.test.jsx`, `ConfigPanelDraft.test.jsx`.
3. **Orphan sweep.** Delete or formally quarantine the orphaned
   `workspace/WorkspacePanel.jsx`, `workspace/tabs/*`,
   `workspace/modals/ResourceCatalogModal.jsx`, and migrate/retire
   `WorkspaceIntegration.test.jsx` + the tabbed half of `WorkspacePanel.test.jsx`
   which currently exercise dead code. Refactor-only.
4. **API-base consistency.** Replace `ConfigPanel.jsx`'s hard-coded
   `http://host:8000` with proxy-relative `globalApi`/`''` so the Vite proxy is
   honored uniformly (Q5). Low risk, improves dev + prod parity.
5. **Landing & global panels.** `WorkspaceSelector` + `GlobalSessions` /
   `GlobalCredentials` / `GlobalResources` / `GlobalContainers` /
   `PromptLibrary` — audit each fetch (Q5) and each interactive element (Q4).
6. **Workspace layer.** `WorkspaceDetailPage` + its tabs + permission-flow
   (`PUT /api/workspace/{id}/permissions`), host-execution toggle. Guards:
   `WorkspaceDetailPage.test.jsx`, `WorkspaceDetailPagePermissions.test.jsx`.
7. **Session layer.** `SessionTab` + `ChatPanel` + `QueryBar` + `SessionSidebar`
   + `ConfigPanel` + `WorkerPanelArea` + `WorkerOutputPanel` WS command coverage;
   guards are the five real-`App` integration suites (Navigation,
   SessionRouteMounting, SessionTabsIntegration, SessionTabHubStatus,
   SessionTabKeepMounted).

Each chunk should run `npm test` (vitest) in `web_ui/frontend/`.

---

### Notes / cautions
- `react-router-dom` is declared but unused — do not assume it's available at
  runtime; the app uses the hand-rolled `router.js`.
- The five real-`App` integration suites each stub `fetch` + WebSocket; any change
  to `App.jsx` wiring (hub commands/events, routes, persistence keys) must keep
  those green.
- `data/apiContracts.js` is documentation only — safe to align but nothing
  depends on it.
