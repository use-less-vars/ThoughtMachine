// NOTE: frontend is pure JS (no tsconfig) — type-contract file, keep in sync
// manually until TypeScript is wired in. Nothing imports this file yet.

export interface Resource {
  name: string
  icon: string
  description: string
  containerized: boolean
  risk: string
  enabled: boolean
}

export interface Permission {
  name: string
  ceiling: string
  effective: string
}

export interface Tool {
  name: string
  resource: string
  permission: string
  enabled: boolean
  defaultOn: boolean
}

export interface Credential {
  name: string
  type: string
  assigned: boolean
  placeholder: string
}

export interface Container {
  name: string
  status: string
  uptime: string
  note: string
}

export interface WorkerPreset {
  name: string
  systemPrompt: string
  tools: string[]
  permissions: string[]
  tokenLimit: number
}

export interface SessionDefaults {
  systemPrompt: string
  tokenLimit: number
  temperature: number
  maxTurns: number
  toolOutputTokenLimit: number
  allowedProviders: string[]
  defaultPreset: string
}

export interface Workspace {
  id: string
  name: string
  path: string
  risk: string
  purposeId: string
  createdAt: string
  resources: Resource[]
  permissions: Permission[]
  tools: Tool[]
  credentials: Credential[]
  containers: Container[]
  workers: WorkerPreset[]
  sessionDefaults: SessionDefaults
}

export interface SafetyAdvisory {
  status: 'green' | 'amber' | 'red'
  message: string
}

export interface PurposeCard {
  id: string
  label: string
  icon: string
  description: string
  risk: string
  requiresDocker: boolean
  recommendedSettings: string
  defaults: {
    resources: string[]
    permissions: Record<string, string>
    tools: string[]
    sessionDefaults: {
      max_turns: number
      temperature: number
      system_prompt: string
    }
  }
}
