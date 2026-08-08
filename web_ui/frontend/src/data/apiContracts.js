// --- apiContracts.js ---
// API contract documentation for the Phase 3 Workspace Panel.
//
// This module is PURE DOCUMENTATION (dependency-free): nothing imports it at
// runtime. It exists so the frontend/backend boundary stays reviewable in one
// place and so the status of every endpoint the UI touches is explicit.
//
// Routers: workspace_router (prefix `/api/workspace`) and session_router
// (prefix `/api/session`) live in web_ui/backend; the container + health
// endpoints live in server.py.
//
// `status` semantics:
//   'implemented' — the route exists in the backend (verified by grepping the
//                   decorator + path in web_ui/backend); the entry carries
//                   `notes: 'backend source: <file>'`.
//   'pending'     — no backend route found; the entry describes the current
//                   UI fallback so the gap stays visible.

/**
 * API endpoint contract table.
 * implemented: routes the backend actually serves today.
 * pending:     documented contracts with no backend route yet — the UI falls
 *              back (localStorage overlay, bundled placeholder data, or a
 *              placeholder modal).
 * Each entry: { method, path, query?, status, notes }.
 */
export const API_ENDPOINTS = {
  implemented: [
    // --- Workspaces ---------------------------------------------------------
    {
      method: 'GET',
      path: '/api/workspace/list',
      status: 'implemented',
      notes: 'backend source: web_ui/backend/workspace_routes.py',
    },
    {
      method: 'GET',
      path: '/api/workspace/:workspace_id/effective_permissions',
      query: { session_id: 'optional string' },
      status: 'implemented',
      notes: 'backend source: web_ui/backend/workspace_routes.py',
    },

    // --- Health / Docker ----------------------------------------------------
    {
      method: 'GET',
      path: '/api/health/containers',
      status: 'implemented',
      notes: 'backend source: web_ui/backend/server.py (response { docker: status })',
    },

    // --- Workers ------------------------------------------------------------
    {
      method: 'GET',
      path: '/api/workspace/:workspace_id/workers',
      query: { name: 'optional string (filter single worker)' },
      status: 'implemented',
      notes: 'backend source: web_ui/backend/workspace_routes.py',
    },
    {
      method: 'POST',
      path: '/api/workspace/:workspace_id/workers',
      status: 'implemented',
      notes: 'backend source: web_ui/backend/workspace_routes.py (201 created; 409 duplicate name)',
    },
    {
      method: 'PUT',
      path: '/api/workspace/:workspace_id/workers/:name',
      status: 'implemented',
      notes: 'backend source: web_ui/backend/workspace_routes.py (404 if worker not found)',
    },
    {
      method: 'DELETE',
      path: '/api/workspace/:workspace_id/workers/:name',
      status: 'implemented',
      notes: 'backend source: web_ui/backend/workspace_routes.py (204 No Content)',
    },

    // --- Containers ---------------------------------------------------------
    {
      method: 'GET',
      path: '/api/workspace/:workspace_id/containers',
      query: { workspace_path: 'string (workspace root)' },
      status: 'implemented',
      notes: 'backend source: web_ui/backend/server.py (response wrapped in { containers: [...] })',
    },
    {
      method: 'POST',
      path: '/api/workspace/:workspace_id/containers/:container_name/start',
      query: { workspace_path: 'string (workspace root)' },
      status: 'implemented',
      notes: 'backend source: web_ui/backend/server.py',
    },
    {
      method: 'POST',
      path: '/api/workspace/:workspace_id/containers/:container_name/stop',
      query: { workspace_path: 'string (workspace root)' },
      status: 'implemented',
      notes: 'backend source: web_ui/backend/server.py',
    },
    {
      method: 'DELETE',
      path: '/api/workspace/:workspace_id/containers/:container_name',
      query: { workspace_path: 'string (workspace root)' },
      status: 'implemented',
      notes: 'backend source: web_ui/backend/server.py',
    },

    // --- Sessions -----------------------------------------------------------
    {
      method: 'POST',
      path: '/api/session/create',
      status: 'implemented',
      notes: 'backend source: web_ui/backend/session_routes.py (response_model CreateSessionResponse)',
    },
    {
      method: 'GET',
      path: '/api/session/list',
      query: { workspace_id: 'optional string (filter by workspace)' },
      status: 'implemented',
      notes: 'backend source: web_ui/backend/session_routes.py (response_model List[SessionListItem])',
    },
    {
      method: 'DELETE',
      path: '/api/session/:session_id',
      query: { workspace_id: 'optional string' },
      status: 'implemented',
      notes: 'backend source: web_ui/backend/session_routes.py (404 if session not found)',
    },
  ],

  pending: [
    // --- Workspace config persistence ---------------------------------------
    {
      method: 'PUT',
      path: '/api/workspace/:workspace_id/config',
      status: 'pending',
      notes: 'No backend route. UI fallback: updateWorkspaceConfig persists resources / tools / credentials / sessionDefaults / purposeId / risk to localStorage under `tm.workspace.local.<id>`.',
    },

    // --- Resource catalog ---------------------------------------------------
    {
      method: 'GET',
      path: '/api/resource-catalog',
      status: 'pending',
      notes: 'No backend route. UI fallback: store.fetchResourceCatalog returns the bundled placeholder catalog (workspace_files, shared_memory, npm_cache, dataset_store, llm_keys, sandbox_volume) when tryGet fails.',
    },

    // --- Vault credentials --------------------------------------------------
    {
      method: 'GET',
      path: '/api/vault/credentials',
      status: 'pending',
      notes: 'No backend route. UI fallback: CredentialModal offers VAULT_PLACEHOLDERS entries (openai_api_key, github_token, docker_registry_auth, ssh_deploy_key, huggingface_token) with hint "stored in vault".',
    },

    // --- Container logs -----------------------------------------------------
    {
      method: 'GET',
      path: '/api/workspace/:workspace_id/containers/:container_name/logs',
      query: { workspace_path: 'string (workspace root)' },
      status: 'pending',
      notes: 'No backend route. UI fallback: the Containers tab "Logs" button opens a PlaceholderModal ("Live container logs are coming soon.").',
    },
  ],
}

// ---------------------------------------------------------------------------
// OBJECT SHAPES (frontend-facing, as produced/consumed by workspaceStore.js)
// ---------------------------------------------------------------------------

/**
 * Workspace object (frontend shape — store / workspaceStore.js).
 * @typedef {Object} Workspace
 * @property {string}   id          — workspace id (backend `id`; local fallback `ws-<ts>-<rand>`)
 * @property {string}   name        — display label (backend `label` or purpose label)
 * @property {string}   path        — display path, e.g. `~/workspaces/<id>`
 * @property {string}   root        — absolute workspace root path (backend `root`); used as
 *                                    `workspace_path` query param for container endpoints
 * @property {string}   risk        — 'Low' | 'Medium' | 'High' | 'Critical'
 * @property {string}   purposeId   — purpose definition id (e.g. 'code-development')
 * @property {string}   createdAt   — ISO timestamp
 * @property {Array<Resource>}    resources     — granted resources (git/filesystem/network/container/serial)
 * @property {Array<Permission>}  permissions   — name/ceiling/effective triples
 * @property {Array<Tool>}        tools         — enabled tool definitions
 * @property {Array<Credential>}  credentials   — stored credentials (local overlay)
 * @property {Array<Container>}   containers    — docker containers (backend GET containers)
 * @property {Array<Worker>}      workers       — worker definitions + runtime status (backend /workers)
 * @property {Array<Session>}     sessions      — sessions for this workspace (backend /session/list)
 * @property {SessionDefaults}    sessionDefaults — per-workspace session defaults (local overlay)
 */

/**
 * Resource object.
 * @typedef {Object} Resource
 * @property {string}  name        — 'git' | 'filesystem' | 'network' | 'container' | 'serial'
 * @property {string}  icon        — emoji icon
 * @property {string}  description — human description
 * @property {boolean} containerized — derived from purposeDefinitions requiresDocker
 * @property {string}  risk        — inherited purpose risk
 * @property {boolean} enabled     — whether the resource is currently granted
 */

/**
 * Permission object.
 * @typedef {Object} Permission
 * @property {string} name      — 'git' | 'filesystem' | 'network' | 'container' | 'serial'
 * @property {string} ceiling   — max allowed value ('write' | 'enabled' | ...)
 * @property {string} effective — current effective value
 */

/**
 * Tool object.
 * @typedef {Object} Tool
 * @property {string} name       — tool class name (e.g. 'FileEditor')
 * @property {string} resource   — owning resource key (TOOL_RESOURCE mapping)
 * @property {string} permission — required permission level
 * @property {boolean} enabled   — granted?
 * @property {boolean} defaultOn — on by default?
 */

/**
 * Worker object (frontend shape — store mapWorker()).
 * @typedef {Object} Worker
 * @property {string}  name              — unique worker name
 * @property {string}  description       — human description
 * @property {string}  systemPrompt      — system prompt (backend `system_prompt`)
 * @property {string[]} tools            — allowed tool class names
 * @property {Object}  workerPermissions — permission footprint (backend `worker_permissions`)
 * @property {number|null} tokenLimit    — warning/critical threshold tokens
 * @property {number|null} temperature
 * @property {number|null} maxTurns
 * @property {number|null} timeoutSeconds
 * @property {string|null} runtimeStatus  — backend `runtime_status`
 * @property {string|null} currentTask    — backend `current_task`
 * @property {string|null} lastHeartbeat  — backend `last_heartbeat`
 * @property {string|null} error
 * @property {string|null} sessionId      — backend `session_id`
 * @property {number|null} currentContextTokens
 * @property {number|null} maxContextTokens
 * @property {boolean} hasPersistedContext
 */

/**
 * Container object (frontend shape).
 * @typedef {Object} Container
 * @property {string} name   — container name
 * @property {string} status — 'running' | 'stopped' | 'created' | 'exited' | ...
 * @property {string} [id]   — docker container id (when backend provides it)
 * @property {string} [image]
 */

/**
 * Session object (frontend shape — backend SessionListItem).
 * @typedef {Object} Session
 * @property {string} session_id   — session id
 * @property {string} name         — display name
 * @property {string} mode         — 'agent' | 'engineer' | 'custom' | ...
 * @property {string} workspace_id — owning workspace
 * @property {string} created_at   — ISO timestamp
 * @property {string} updated_at   — ISO timestamp
 * @property {string} [preview]    — last message preview
 */

/**
 * SessionDefaults object (local overlay until backend support).
 * @typedef {Object} SessionDefaults
 * @property {string} systemPrompt
 * @property {number} tokenLimit
 * @property {number} temperature
 * @property {number} maxTurns
 * @property {number} toolOutputTokenLimit
 * @property {string[]} allowedProviders
 * @property {string} defaultPreset
 */

export default API_ENDPOINTS
