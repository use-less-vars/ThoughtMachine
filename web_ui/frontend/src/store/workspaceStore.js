// --- workspaceStore.js ---
// Workspace Panel store (Phase 1). Deliberately SEPARATE from useStore.js
// (which is session-centric): this store owns workspace-level state —
// workspace list, current workspace, safety advisory.
// All actions are MOCK until backend endpoints exist; the store is seeded
// with two demo workspaces in initialState.

import { create } from 'zustand'
import purposeDefinitions from '../data/purposeDefinitions.json'

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

// --- Build a full Workspace object from a purpose definition ---
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
    risk: purpose.risk,
    purposeId: purpose.id,
    createdAt: new Date().toISOString(),
    resources,
    permissions,
    tools,
    credentials: [],
    containers: [],
    workers: [],
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

const initialState = {
  workspaceList: MOCK_WORKSPACES,
  currentWorkspace: null,
  safetyAdvisory: { status: 'green', message: '' },
  isLoading: false,
}

const useWorkspaceStore = create((set, get) => ({
  ...initialState,

  // TODO: wire to real API once backend endpoints exist
  fetchWorkspaces: () =>
    set({ workspaceList: MOCK_WORKSPACES, currentWorkspace: null, safetyAdvisory: { status: 'green', message: '' }, isLoading: false }),

  // TODO: wire to real API once backend endpoints exist
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

  // TODO: wire to real API once backend endpoints exist
  fetchWorkspaceConfig: (id) => {
    const workspace = get().workspaceList.find((w) => w.id === id) || null
    set({
      currentWorkspace: workspace,
      safetyAdvisory: workspace ? advisoryForRisk(workspace.risk) : { status: 'green', message: '' },
      isLoading: false,
    })
  },

  // TODO: wire to real API once backend endpoints exist
  updateWorkspaceConfig: (id, partial) => {
    const workspaceList = get().workspaceList.map((w) => (w.id === id ? { ...w, ...partial } : w))
    const merged = workspaceList.find((w) => w.id === id) || null
    const currentWorkspace =
      get().currentWorkspace && get().currentWorkspace.id === id
        ? { ...get().currentWorkspace, ...partial }
        : get().currentWorkspace
    set({
      workspaceList,
      currentWorkspace,
      safetyAdvisory: merged ? advisoryForRisk(merged.risk) : get().safetyAdvisory,
    })
  },

  reset: () => set({ ...initialState }),
}))

export default useWorkspaceStore
