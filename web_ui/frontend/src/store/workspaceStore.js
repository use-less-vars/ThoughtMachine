// --- workspaceStore.js ---
// Workspace Panel store (Phase 3): REAL API wiring against the FastAPI backend
// (web_ui/backend). Workspace list, effective permissions, Docker health,
// workers, containers and sessions come from the backend; the local-only parts
// (resources / tools / credentials / sessionDefaults) persist in localStorage
// under `tm.workspace.local.<id>` until backend endpoints exist for them.
// This store is deliberately SEPARATE from useStore.js (session-centric):
// it owns workspace-level state — workspace list, current workspace,
// safety advisory, docker availability and container/session views.

import { create } from 'zustand'
import purposeDefinitions from '../data/purposeDefinitions.json'

const API_BASE = ''

// --- Resource metadata (icon + description per resource key) ---
const RESOURCE_META = {
  git:        { icon: '🌿', description: 'Version control access' },
  filesystem: { icon: '📁', description: 'Filesystem access' },
  network:    { icon: '🌐', description: 'Network access' },
  container:  { icon: '🐳', description: 'Docker container execution' },
  serial:     { icon: '🔌', description: 'Serial / device access' },
}

// --- Tool → resource mapping ---
const TOOL_RESOURCE = {
  FileEditor:     'filesystem',
  FileReader:     'filesystem',
  SearchCodebase: 'filesystem',
  GitTool:        'git',
  DockerTool:     'container',
  HttpTool:       'network',
  SerialTool:     'serial',
}

// --- Ceiling per permission name (used when a purpose sets a lower default) ---
const PERMISSION_CEILINGS = {
  git:        'write',
  filesystem: 'write',
  network:    'write',
  container:  'enabled',
  serial:     'enabled',
}

// --- Safety advisory per risk level ---
function advisoryForRisk(risk) {
  switch (risk) {
    case 'Low':
      return { status: 'green', message: 'Low risk — standard guardrails apply.' }
    case 'Medium':
      return { status: 'amber', message: 'Medium risk — extra review recommended.' }
    case 'High':
      return { status: 'red', message: 'High risk — restricted environment required.' }
    case 'Critical':
      return { status: 'red', message: 'Critical risk — isolated, containerized execution only.' }
    default:
      return { status: 'green', message: '' }
  }
}

// --- Build a full Workspace object from a purpose definition (offline fallback) ---
function buildWorkspace(purpose, idOverride) {
  const id = idOverride || `ws-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
  const defaults = purpose.defaults
  const sd = defaults.sessionDefaults || {}

  const resources = (defaults.resources || []).map((name) => ({
    name,
    icon: (RESOURCE_META[name] && RESOURCE_META[name].icon) || '•',
    description: (RESOURCE_META[name] && RESOURCE_META[name].description) || name,
    containerized: !!purpose.requiresDocker,
    risk: purpose.risk,
    enabled: true,
  }))

  const permissions = Object.entries(defaults.permissions || {}).map(([name, effective]) => ({
    name,
    ceiling: PERMISSION_CEILINGS[name] || effective,
    effective,
  }))

  const tools = (defaults.tools || []).map((name) => ({
    name,
    resource: TOOL_RESOURCE[name] || 'filesystem',
    permission: 'read',
    enabled: true,
    defaultOn: true,
  }))

  return {
    id,
    name: purpose.label,
    path: `~/workspaces/${id}`,
    root: '',
    risk: purpose.risk,
    purposeId: purpose.id,
    createdAt: new Date().toISOString(),
    resources,
    permissions,
    tools,
    credentials: [],
    containers: [],
    workers: [],
    sessions: [],
    sessionDefaults: {
      systemPrompt: sd.system_prompt || '',
      tokenLimit: 8000,
      temperature: sd.temperature ?? 0.7,
      maxTurns: sd.max_turns ?? 20,
      toolOutputTokenLimit: 2000,
      allowedProviders: [],
      defaultPreset: 'balanced',
    },
  }
}

// --- Mock seed data (declared AFTER buildWorkspace — order matters) ---
// Used only as an offline fallback when the backend is unreachable.
const MOCK_WORKSPACES = (() => {
  const dev = buildWorkspace(
    purposeDefinitions.find((p) => p.id === 'code-development'),
    'ws-demo-dev'
  )
  dev.name = 'Demo Dev Workspace'
  dev.path = '~/workspaces/demo-dev'
  const writing = buildWorkspace(
    purposeDefinitions.find((p) => p.id === 'writing-research'),
    'ws-demo-writing'
  )
  writing.name = 'Demo Writing Workspace'
  writing.path = '~/workspaces/demo-writing'
  return [dev, writing]
})()

// --- Local overlay persistence (resources/tools/credentials/sessionDefaults) ---
function localKey(id) {
  return `tm.workspace.local.${id}`
}
function loadLocal(id) {
  try {
    return JSON.parse(localStorage.getItem(localKey(id))) || {}
  } catch {
    return {}
  }
}
function saveLocal(id, overlay) {
  try {
    localStorage.setItem(localKey(id), JSON.stringify(overlay))
  } catch {
    // storage full / unavailable — the overlay stays in memory only
  }
}

// --- Small fetch helpers ---
async function parseError(res) {
  try {
    const data = await res.json()
    return data.error || data.detail || `Request failed (${res.status})`
  } catch {
    return `Request failed (${res.status})`
  }
}

async function tryGet(url) {
  try {
    const res = await fetch(url)
    if (!res.ok) return null
    return await res.json()
  } catch {
    return null
  }
}

function omit(obj, key) {
  const next = { ...obj }
  delete next[key]
  return next
}

// --- Backend worker (snake_case config + runtime keys) → frontend shape ---
function mapWorker(w) {
  return {
    name: w.name,
    description: w.description || '',
    systemPrompt: w.system_prompt || '',
    tools: Array.isArray(w.tools) ? w.tools : [],
    workerPermissions: w.permission_footprint || w.worker_permissions || {},
    tokenLimit: w.warning_threshold_tokens ?? w.critical_threshold_tokens ?? null,
    temperature: w.temperature ?? null,
    maxTurns: w.max_turns ?? null,
    timeoutSeconds: w.timeout_seconds ?? null,
    runtimeStatus: w.runtime_status ?? null,
    currentTask: w.current_task ?? null,
    lastHeartbeat: w.last_heartbeat ?? null,
    error: w.error ?? null,
    sessionId: w.session_id ?? null,
    currentContextTokens: w.current_context_tokens ?? null,
    maxContextTokens: w.max_context_tokens ?? null,
    hasPersistedContext: !!w.has_persisted_context,
  }
}

// --- Frontend worker → backend WorkerDefinition body ---
function toBackendWorker(w) {
  return {
    name: w.name,
    description: w.description || '',
    system_prompt: w.systemPrompt || '',
    tools: Array.isArray(w.tools) ? w.tools : [],
    worker_permissions: w.workerPermissions || w.permissions || {},
  }
}

const initialState = {
  workspaceList: MOCK_WORKSPACES,
  currentWorkspace: null,
  safetyAdvisory: { status: 'green', message: '' },
  isLoading: false,
  dockerAvailable: null,  // tri-state: true=reachable, false=down, null=unverified
  error: '',
  sessions: [],
  containerStatus: {},   // container name → status string (optimistic overlay)
  busyContainers: {},    // container name → bool (action in flight)
}

const useWorkspaceStore = create((set, get) => ({
  ...initialState,

  // GET /api/workspace/list → workspace list (falls back to the two demo
  // workspaces when the backend is unreachable).
  fetchWorkspaces: async () => {
    set({ isLoading: true, error: '' })
    try {
      const res = await fetch(`${API_BASE}/api/workspace/list`)
      if (!res.ok) throw new Error(await parseError(res))
      const data = await res.json()
      const workspaceList = (Array.isArray(data) ? data : []).map((w) => ({
        id: w.id,
        name: w.label || w.id,
        path: w.root || '',
        root: w.root || '',
      }))
      set({ workspaceList, isLoading: false, error: '' })
    } catch (err) {
      set({
        workspaceList: MOCK_WORKSPACES,
        isLoading: false,
        error: err.message || 'Failed to load workspaces',
      })
    }
  },

  // Clears the store-level error message (error banner Dismiss / Retry).
  clearError: () => set({ error: '' }),

  // GET /api/resource-catalog — the backend now serves a bare array of
  // resource definitions; accept it directly. Older clients wrapped the
  // response in {items: [...]} — still supported. On failure (tryGet returns
  // null) fall back to the bundled placeholder catalog so the Resources 'Add
  // Resource' modal always has items to offer.
  fetchResourceCatalog: async () => {
    const data = await tryGet(`${API_BASE}/api/resource-catalog`)
    if (Array.isArray(data)) return data
    if (data && Array.isArray(data.items)) return data.items
    return [
      { name: 'workspace_files', description: 'Workspace file access' },
      { name: 'shared_memory', description: 'Shared memory across sessions' },
      { name: 'npm_cache', description: 'npm package cache' },
      { name: 'dataset_store', description: 'Dataset storage' },
      { name: 'llm_keys', description: 'LLM API key access' },
      { name: 'sandbox_volume', description: 'Isolated sandbox volume' },
    ]
  },

  // Local-only creation (Phase 1 WorkspaceSelector flow): builds the workspace
  // from a purpose definition, prepends it to the list and returns its id.
  createWorkspace: (purposeId) => {
    const purpose = purposeDefinitions.find((p) => p.id === purposeId)
    if (!purpose) return null
    const workspace = buildWorkspace(purpose)
    set((state) => ({
      workspaceList: [workspace, ...state.workspaceList],
      currentWorkspace: workspace,
      safetyAdvisory: advisoryForRisk(workspace.risk),
    }))
    return workspace.id
  },

  // POST /api/workspace/resolve {path} → registers an existing folder as a
  // backend workspace. Returns { workspace_id, root }; prepends it to the list
  // (unless it is already present) so the sidebar shows it immediately.
  resolveWorkspacePath: async (path) => {
    const res = await fetch(`${API_BASE}/api/workspace/resolve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }),
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) throw new Error(data.error || data.detail || 'Failed to resolve workspace')
    set((state) => {
      const exists = state.workspaceList.some((w) => w.id === data.workspace_id)
      if (exists) return {}
      const name = String(path).split('/').filter(Boolean).pop() || path
      return {
        workspaceList: [
          { id: data.workspace_id, name, path: data.root || path, root: data.root || path },
          ...state.workspaceList,
        ],
      }
    })
    return data
  },

  // Loads everything the panel needs for one workspace in parallel: list
  // entry, effective permissions, Docker health, workers, containers and the
  // workspace's sessions. Local-only parts come from the overlay (or the
  // purpose-derived defaults). On total failure: purpose fallback + error.
  fetchWorkspaceConfig: async (id) => {
    if (get().isLoading) return
    set({ isLoading: true, error: '' })

    const fallbackEntry = get().workspaceList.find((w) => w.id === id)
    try {
      const [listData, permsData, healthData, workersData, containersData, sessionsData] = await Promise.all([
        tryGet(`${API_BASE}/api/workspace/list`),
        tryGet(`${API_BASE}/api/workspace/${encodeURIComponent(id)}/effective_permissions`),
        tryGet(`${API_BASE}/api/health/containers`),
        tryGet(`${API_BASE}/api/workspace/${encodeURIComponent(id)}/workers`),
        fallbackEntry && fallbackEntry.root
          ? tryGet(`${API_BASE}/api/workspace/${encodeURIComponent(id)}/containers?workspace_path=${encodeURIComponent(fallbackEntry.root)}`)
          : Promise.resolve(null),
        tryGet(`${API_BASE}/api/session/list?workspace_id=${encodeURIComponent(id)}`),
      ])

      // Prefer a fresh list entry over the possibly-stale store copy.
      const entries = Array.isArray(listData) ? listData : get().workspaceList
      const entry = entries.find((w) => w.id === id) || fallbackEntry
      const root = (entry && (entry.root || entry.path)) || (fallbackEntry && fallbackEntry.root) || ''

      const overlay = loadLocal(id)
      const purpose =
        purposeDefinitions.find((p) => p.label === (entry && entry.label)) ||
        purposeDefinitions.find((p) => p.id === overlay.purposeId) ||
        null
      const purposeId = purpose ? purpose.id : overlay.purposeId || 'blank'
      const risk = purpose ? purpose.risk : overlay.risk || 'Low'

      // ── Permissions: backend effective permissions (container is a bool) ──
      const ep = permsData && permsData.effective_permissions
      let permissions = []
      if (ep) {
        permissions = ['filesystem', 'network', 'git', 'system', 'execution', 'container'].map((name) => {
          let effective = ep[name]
          if (name === 'container') effective = effective ? 'enabled' : 'banned'
          return { name, ceiling: effective, effective }
        })
      } else if (purpose) {
        permissions = Object.entries(purpose.defaults.permissions || {}).map(([name, effective]) => ({
          name,
          ceiling: PERMISSION_CEILINGS[name] || effective,
          effective,
        }))
      }

      // ── Docker availability (tri-state) ──
      // true only when the health payload reports Docker available; false when
      // it reports unavailable; null (unverified) when the health fetch itself
      // failed (tryGet returns null). Tolerates both the legacy flat string
      // ("reachable"/other) and the nested dispatch shape
      // ({"available": bool, "reason": ...}).
      const healthDocker = healthData && healthData.docker
      const dockerAvailable =
        healthData === null
          ? null
          : typeof healthDocker === 'string'
            ? healthDocker === 'reachable'
            : healthDocker
              ? healthDocker.available === true
              : false

      // ── Workers (bare array of config + runtime keys) ──
      const workers = Array.isArray(workersData) ? workersData.map(mapWorker) : []

      // ── Containers (wrapped in {"containers": [...]}) ──
      const containers = containersData && Array.isArray(containersData.containers) ? containersData.containers : []
      const containerStatus = {}
      containers.forEach((c) => {
        if (c.name) containerStatus[c.name] = c.status
      })

      // ── Sessions ──
      const sessions = Array.isArray(sessionsData) ? sessionsData : []

      // ── Local-only parts (resources/tools/credentials/sessionDefaults) ──
      const fallback = purpose ? buildWorkspace(purpose, id) : null
      const resources = overlay.resources || (fallback ? fallback.resources : [])
      const tools = overlay.tools || (fallback ? fallback.tools : [])
      const credentials = overlay.credentials || []
      const sessionDefaults = overlay.sessionDefaults || (fallback ? fallback.sessionDefaults : {
        systemPrompt: '',
        tokenLimit: 8000,
        temperature: 0.7,
        maxTurns: 20,
        toolOutputTokenLimit: 2000,
        allowedProviders: [],
        defaultPreset: 'balanced',
      })

      const workspace = {
        id,
        name: (entry && (entry.label || entry.id)) || (fallbackEntry && fallbackEntry.name) || id,
        path: root,
        root,
        risk,
        purposeId,
        createdAt: overlay.createdAt || new Date().toISOString(),
        resources,
        permissions,
        tools,
        credentials,
        containers,
        workers,
        sessions,
        sessionDefaults,
      }

      set((state) => ({
        currentWorkspace: workspace,
        workspaceList: state.workspaceList.map((w) =>
          w.id === id ? { ...w, name: workspace.name, path: root, root } : w
        ),
        safetyAdvisory: advisoryForRisk(risk),
        dockerAvailable,
        sessions,
        containerStatus: { ...state.containerStatus, ...containerStatus },
        isLoading: false,
        error: '',
      }))
    } catch (err) {
      // Total failure — fall back to purpose-derived defaults.
      const purpose =
        purposeDefinitions.find((p) => p.id === (loadLocal(id).purposeId || 'blank')) ||
        purposeDefinitions.find((p) => p.id === 'blank')
      const workspace = buildWorkspace(purpose, id)
      workspace.id = id
      workspace.name = (fallbackEntry && fallbackEntry.name) || id
      workspace.root = (fallbackEntry && fallbackEntry.root) || ''
      workspace.path = workspace.root
      set({
        currentWorkspace: workspace,
        isLoading: false,
        error: err.message || 'Failed to load workspace config',
      })
    }
  },

  // Merges local-only edits (instant UI feedback); persists workers to the
  // backend (diff by name: new → POST, gone → DELETE, kept → PUT) and the
  // local overlay (resources/tools/credentials/sessionDefaults) to localStorage.
  updateWorkspaceConfig: async (id, partial) => {
    const current = get().currentWorkspace
    const prev = current && current.id === id ? current : get().workspaceList.find((w) => w.id === id)
    const prevWorkers = partial.workers && prev ? prev.workers || [] : null

    // ── Merge into state first ──
    const workspaceList = get().workspaceList.map((w) => (w.id === id ? { ...w, ...partial } : w))
    const merged = workspaceList.find((w) => w.id === id) || null
    set((state) => ({
      workspaceList,
      currentWorkspace:
        state.currentWorkspace && state.currentWorkspace.id === id
          ? { ...state.currentWorkspace, ...partial }
          : state.currentWorkspace,
      safetyAdvisory: merged ? advisoryForRisk(merged.risk) : state.safetyAdvisory,
    }))

    // ── Persist local overlay ──
    const overlay = loadLocal(id)
    const nextOverlay = { ...overlay }
    for (const key of ['resources', 'tools', 'credentials', 'sessionDefaults', 'purposeId', 'risk']) {
      if (partial[key] !== undefined) nextOverlay[key] = partial[key]
    }
    saveLocal(id, nextOverlay)

    // ── Persist workers to the backend ──
    if (partial.workers && prevWorkers) {
      const prevNames = new Set(prevWorkers.map((w) => w.name))
      const nextNames = new Set(partial.workers.map((w) => w.name))
      const errors = []

      for (const w of partial.workers) {
        const isNew = !prevNames.has(w.name)
        const url = `${API_BASE}/api/workspace/${encodeURIComponent(id)}/workers${isNew ? '' : `/${encodeURIComponent(w.name)}`}`
        try {
          const res = await fetch(url, {
            method: isNew ? 'POST' : 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(toBackendWorker(w)),
          })
          if (!res.ok) {
            const data = await res.json().catch(() => ({}))
            errors.push(data.error || data.detail || `Worker '${w.name}' ${isNew ? 'create' : 'update'} failed (${res.status})`)
          }
        } catch (err) {
          errors.push(err.message || `Worker '${w.name}' ${isNew ? 'create' : 'update'} failed`)
        }
      }
      for (const w of prevWorkers) {
        if (!nextNames.has(w.name)) {
          try {
            const res = await fetch(
              `${API_BASE}/api/workspace/${encodeURIComponent(id)}/workers/${encodeURIComponent(w.name)}`,
              { method: 'DELETE' }
            )
            if (!res.ok) {
              const data = await res.json().catch(() => ({}))
              errors.push(data.error || data.detail || `Worker '${w.name}' delete failed (${res.status})`)
            }
          } catch (err) {
            errors.push(err.message || `Worker '${w.name}' delete failed`)
          }
        }
      }
      if (errors.length > 0) set({ error: errors.join('; ') })
    }

    return merged
  },

  // POST /api/session/create → returns the created session JSON.
  createSession: async (id, { name, mode } = {}) => {
    const workspace = get().workspaceList.find((w) => w.id === id) || get().currentWorkspace
    const payload = { mode: mode || 'agent' }
    if (name) payload.name = name
    payload.workspace_id = id
    if (workspace && workspace.root) payload.workspace_path = workspace.root
    const res = await fetch(`${API_BASE}/api/session/create`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) throw new Error(data.error || data.detail || 'Failed to create session')
    return data
  },

  // GET /api/session/list?workspace_id= → store.sessions + currentWorkspace.sessions
  fetchSessions: async (id) => {
    try {
      const res = await fetch(`${API_BASE}/api/session/list?workspace_id=${encodeURIComponent(id)}`)
      if (!res.ok) throw new Error(await parseError(res))
      const data = await res.json()
      const sessions = Array.isArray(data) ? data : []
      set((state) => ({
        sessions,
        currentWorkspace:
          state.currentWorkspace && state.currentWorkspace.id === id
            ? { ...state.currentWorkspace, sessions }
            : state.currentWorkspace,
      }))
    } catch (err) {
      set({ error: err.message || 'Failed to load sessions' })
    }
  },

  // DELETE /api/session/{session_id}?workspace_id= → refresh the list.
  deleteSession: async (id, sessionId) => {
    const res = await fetch(
      `${API_BASE}/api/session/${encodeURIComponent(sessionId)}?workspace_id=${encodeURIComponent(id)}`,
      { method: 'DELETE' }
    )
    const data = await res.json().catch(() => ({}))
    if (!res.ok) throw new Error(data.error || data.detail || 'Failed to delete session')
    await get().fetchSessions(id)
    return data
  },

  // GET /api/workspace/{ws_id}/containers?workspace_path=<root>
  fetchContainers: async (id) => {
    const workspace = get().workspaceList.find((w) => w.id === id) || get().currentWorkspace
    const root = workspace && (workspace.root || workspace.path)
    if (!root) return
    try {
      const res = await fetch(
        `${API_BASE}/api/workspace/${encodeURIComponent(id)}/containers?workspace_path=${encodeURIComponent(root)}`
      )
      if (!res.ok) throw new Error(await parseError(res))
      const data = await res.json()
      const containers = data && Array.isArray(data.containers) ? data.containers : []
      const statusMap = {}
      containers.forEach((c) => {
        if (c.name) statusMap[c.name] = c.status
      })
      set((state) => ({
        containerStatus: { ...state.containerStatus, ...statusMap },
        currentWorkspace:
          state.currentWorkspace && state.currentWorkspace.id === id
            ? { ...state.currentWorkspace, containers }
            : state.currentWorkspace,
        workspaceList: state.workspaceList.map((w) => (w.id === id ? { ...w, containers } : w)),
        error: '',
      }))
    } catch (err) {
      set({ error: err.message || 'Failed to load containers' })
    }
  },

  // start / stop / remove a named container; refreshes the list afterwards.
  containerAction: async (id, name, action) => {
    const workspace = get().workspaceList.find((w) => w.id === id) || get().currentWorkspace
    const root = workspace && (workspace.root || workspace.path)
    const base = `${API_BASE}/api/workspace/${encodeURIComponent(id)}/containers/${encodeURIComponent(name)}`
    const url = action === 'remove' ? base : `${base}/${action}`
    set((state) => ({ busyContainers: { ...state.busyContainers, [name]: true } }))
    try {
      const res = await fetch(`${url}?workspace_path=${encodeURIComponent(root || '')}`, {
        method: action === 'remove' ? 'DELETE' : 'POST',
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.error || data.detail || `Failed to ${action} container`)

      if (action === 'remove') {
        set((state) => ({
          containerStatus: omit(state.containerStatus, name),
          busyContainers: omit(state.busyContainers, name),
          error: '',
        }))
      } else {
        // Optimistic status; the refresh below reports the real docker state.
        set((state) => ({
          containerStatus: {
            ...state.containerStatus,
            [name]: action === 'stop' ? 'stopped' : 'running',
          },
          busyContainers: omit(state.busyContainers, name),
          error: '',
        }))
      }
      await get().fetchContainers(id).catch(() => {})
      return data
    } catch (err) {
      set((state) => ({
        busyContainers: omit(state.busyContainers, name),
        error: err.message || `Failed to ${action} container`,
      }))
      throw err
    }
  },

  reset: () => set({ ...initialState }),
}))

export default useWorkspaceStore
