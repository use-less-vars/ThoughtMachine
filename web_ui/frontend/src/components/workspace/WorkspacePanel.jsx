// --- WorkspacePanel.jsx ---
// Phase 2: the /workspace/:id page — a single-page "office blueprint" with
// tabbed sections (Resources, Permissions, Tools, Credentials, Containers,
// Workers, Session Defaults) plus a persistent safety advisory sidebar.
// All API calls go through the existing MOCK actions in workspaceStore.js
// (fetchWorkspaceConfig / updateWorkspaceConfig); real backend wiring lands
// in a later phase. Routing uses the dependency-free hash router (src/router.js).

import React, { useEffect, useState, useMemo } from 'react'
import { useRoute } from '../../router'
import useWorkspaceStore from '../../store/workspaceStore'
import purposeDefinitions from '../../data/purposeDefinitions.json'
import './WorkspacePanel.css'

const TABS = ['Resources', 'Permissions', 'Tools', 'Credentials', 'Containers', 'Workers', 'Session Defaults']
const DEFAULT_TAB = 'Session Defaults'

const RISK_CLASS = { Low: 'low', Medium: 'medium', High: 'high', Critical: 'critical' }

const PERMISSION_LEVELS = ['banned', 'read', 'write', 'ask', 'full', 'enabled']
const PERM_RANK = { banned: 0, read: 1, write: 2, ask: 3, full: 4 }

const PROVIDERS = ['Local LLM', 'OpenAI', 'Anthropic', 'DeepSeek']
const PRESET_OPTIONS = ['Agent (full)', 'Engineer (read-only)', 'Custom']

const CONTAINER_LIMIT = 4          // hardcoded workspace container limit
const DOCKER_AVAILABLE = true      // hardcoded until real docker detection (Phase 4)
const PROMPT_PREVIEW_LENGTH = 120  // worker systemPrompt truncation

// --- Permissions reconciliation ---
// Phase 1's store builds permissions as an ARRAY of { name, ceiling, effective }
// (see buildWorkspace in workspaceStore.js); some backend shapes may instead be
// an OBJECT like { git: 'write' }. Normalize to an array for rendering, and
// preserve the original shape when writing updates back.
function normalizePermissions(permissions) {
  if (Array.isArray(permissions)) return permissions
  if (permissions && typeof permissions === 'object') {
    return Object.entries(permissions).map(([name, effective]) => ({
      name,
      ceiling: effective,
      effective,
    }))
  }
  return []
}

function permissionsWithCeiling(original, name, ceiling) {
  if (Array.isArray(original)) {
    return original.map((p) => (p.name === name ? { ...p, ceiling, effective: ceiling } : p))
  }
  return { ...(original || {}), [name]: ceiling }
}

function permissionEffective(permissions, name) {
  const entry = normalizePermissions(permissions).find((p) => p.name === name)
  return entry ? entry.effective || entry.ceiling : null
}

function isHigherPermission(current, expected) {
  const c = PERM_RANK[current] ?? -1
  const e = PERM_RANK[expected] ?? -1
  return e >= 0 && c > e
}

function countOf(value) {
  if (Array.isArray(value)) return value.length
  if (value && typeof value === 'object') return Object.keys(value).length
  return 0
}

// --- Safety advisory (computed from the workspace, not the store's copy) ---
function computeAdvisory(workspace) {
  const risk = workspace.risk
  const resources = workspace.resources || []
  // Phase 1 resources carry `containerized` (boolean). A resource with
  // containerized:false is host-only — the spec's "non-containerizable".
  const hostOnlyEnabled = resources.filter((r) => r.containerized === false && r.enabled)

  const purpose = purposeDefinitions.find((p) => p.id === workspace.purposeId)
  const expectedNetwork = purpose?.defaults?.permissions?.network
  const currentNetwork = permissionEffective(workspace.permissions, 'network')
  const networkTooHigh = expectedNetwork && currentNetwork && isHigherPermission(currentNetwork, expectedNetwork)

  let status = 'green'
  let message = 'Low risk — standard guardrails apply.'
  const suggestions = []

  if (risk === 'High' || risk === 'Critical') {
    status = 'red'
    message = risk === 'Critical'
      ? 'Critical risk — isolated, containerized execution only.'
      : 'High risk — restricted environment required.'
  } else if (risk === 'Medium') {
    status = 'amber'
    message = 'Medium risk — extra review recommended.'
  }

  if (hostOnlyEnabled.length > 0) {
    for (const r of hostOnlyEnabled) {
      suggestions.push({ action: 'disable-resource', label: `Disable ${r.name}`, resource: r.name })
    }
    if (DOCKER_AVAILABLE) {
      if (status === 'green') {
        status = 'amber'
        message = 'Host-only resources enabled — containerize or disable them.'
      }
    } else {
      status = 'red'
      message = 'Host-only resources enabled and Docker is unavailable.'
      suggestions.push({ action: 'install-docker', label: 'Install Docker' })
    }
  }

  if (networkTooHigh) {
    if (status === 'green') {
      status = 'amber'
      message = 'Network permission is higher than this purpose requires.'
    }
    suggestions.push({ action: 'lower-network', label: `Lower network to ${expectedNetwork}`, level: expectedNetwork })
  }

  if (suggestions.length === 0) {
    suggestions.push({ action: 'none', label: 'All guardrails active — no action needed.', disabled: true })
  }

  return { status, message, suggestions }
}

// --- Small shared placeholder modal ---
function PlaceholderModal({ message, onClose }) {
  return (
    <div className="wp-modal-overlay" onClick={onClose}>
      <div className="wp-modal" role="dialog" aria-label={message} onClick={(e) => e.stopPropagation()}>
        <p>{message}</p>
        <button className="wp-btn" onClick={onClose}>Close</button>
      </div>
    </div>
  )
}

function Toggle({ checked, onChange, label }) {
  return (
    <label className="wp-toggle">
      <input type="checkbox" checked={!!checked} onChange={(e) => onChange(e.target.checked)} />
      <span className="wp-toggle-label">{label}</span>
    </label>
  )
}

// # --- Resources tab ---
function ResourcesTab({ workspace, update }) {
  const [showCatalog, setShowCatalog] = useState(false)
  const resources = workspace.resources || []
  const toggleResource = (name, enabled) => {
    update('resources', resources.map((r) => (r.name === name ? { ...r, enabled } : r)))
  }
  return (
    <div className="wp-section">
      <div className="wp-section-header">
        <h3>Resources</h3>
      </div>
      {resources.length === 0 ? (
        <p className="wp-empty">No resources defined for this workspace.</p>
      ) : (
        <div className="wp-resource-grid">
          {resources.map((r) => (
            <div className="wp-resource-card" key={r.name}>
              <div className="wp-resource-top">
                <span className="wp-resource-icon" role="img" aria-label={r.name}>{r.icon || '•'}</span>
                <span className={`wp-risk ${RISK_CLASS[r.risk] || 'low'}`}>{r.risk}</span>
              </div>
              <h4 className="wp-resource-name">{r.name}</h4>
              <p className="wp-resource-desc">{r.description || r.name}</p>
              <div className="wp-resource-badges">
                {r.containerized ? (
                  <span className="wp-badge wp-badge-containerized">✓ Containerized</span>
                ) : (
                  <span className="wp-badge wp-badge-hostonly">⚠ Host-only</span>
                )}
              </div>
              <Toggle
                checked={r.enabled}
                label={r.enabled ? 'Enabled' : 'Disabled'}
                onChange={(enabled) => toggleResource(r.name, enabled)}
              />
            </div>
          ))}
        </div>
      )}
      <div className="wp-section-footer">
        <button className="wp-btn" onClick={() => setShowCatalog(true)}>Add Resource</button>
      </div>
      {showCatalog && <PlaceholderModal message="Resource catalog coming soon" onClose={() => setShowCatalog(false)} />}
    </div>
  )
}

// # --- Permissions tab ---
function PermissionsTab({ workspace, update }) {
  const permissions = normalizePermissions(workspace.permissions)
  const changeCeiling = (name, ceiling) => {
    update('permissions', permissionsWithCeiling(workspace.permissions, name, ceiling))
  }
  return (
    <div className="wp-section">
      <div className="wp-section-header">
        <h3>Permissions</h3>
      </div>
      {permissions.length === 0 ? (
        <p className="wp-empty">No permissions defined for this workspace.</p>
      ) : (
        <table className="wp-table">
          <thead>
            <tr>
              <th>Permission</th>
              <th>Ceiling (workspace max)</th>
              <th>Effective (session default)</th>
            </tr>
          </thead>
          <tbody>
            {permissions.map((p) => (
              <tr key={p.name}>
                <td>{p.name}</td>
                <td>
                  <select
                    className="wp-select"
                    value={p.ceiling}
                    onChange={(e) => changeCeiling(p.name, e.target.value)}
                  >
                    {PERMISSION_LEVELS.map((level) => (
                      <option key={level} value={level}>{level}</option>
                    ))}
                  </select>
                </td>
                <td className="wp-effective">{p.effective ?? p.ceiling}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

// # --- Tools tab ---
function ToolsTab({ workspace, update }) {
  const tools = workspace.tools || []
  const groups = useMemo(() => {
    const map = {}
    for (const t of tools) {
      const res = t.resource || 'filesystem'
      if (!map[res]) map[res] = []
      map[res].push(t)
    }
    return Object.entries(map)
  }, [tools])
  const toggleTool = (name, patch) => {
    update('tools', tools.map((t) => (t.name === name ? { ...t, ...patch } : t)))
  }
  return (
    <div className="wp-section">
      <div className="wp-section-header">
        <h3>Tools</h3>
      </div>
      {tools.length === 0 ? (
        <p className="wp-empty">No tools configured for this workspace.</p>
      ) : (
        groups.map(([resource, groupTools]) => (
          <div className="wp-tool-group" key={resource}>
            <h4 className="wp-tool-group-title">{resource}</h4>
            {groupTools.map((t) => (
              <div className="wp-tool-row" key={t.name}>
                <span className="wp-tool-name">{t.name}</span>
                <span className="wp-badge wp-badge-muted">{t.permission || 'read'}</span>
                <Toggle checked={t.enabled} label="Enabled" onChange={(enabled) => toggleTool(t.name, { enabled })} />
                <Toggle checked={t.defaultOn} label="Default ON" onChange={(defaultOn) => toggleTool(t.name, { defaultOn })} />
              </div>
            ))}
          </div>
        ))
      )}
    </div>
  )
}

// # --- Credentials tab ---
function CredentialsTab({ workspace, update }) {
  const [showVault, setShowVault] = useState(false)
  const credentials = workspace.credentials || []
  const toggleAssigned = (name, assigned) => {
    update('credentials', credentials.map((c) => (c.name === name ? { ...c, assigned } : c)))
  }
  return (
    <div className="wp-section">
      <div className="wp-section-header">
        <h3>Credentials</h3>
        <button className="wp-btn" onClick={() => setShowVault(true)}>Add Credential</button>
      </div>
      {credentials.length === 0 ? (
        <p className="wp-empty">No credentials assigned to this workspace.</p>
      ) : (
        <ul className="wp-cred-list">
          {credentials.map((c) => (
            <li className="wp-cred-row" key={c.name}>
              <span className="wp-cred-name">{c.name}</span>
              <span className="wp-badge wp-badge-type">{c.type || 'generic'}</span>
              <code className="wp-cred-placeholder">{c.placeholder || `{{credential:${c.name}}}`}</code>
              <Toggle checked={c.assigned} label="Assigned" onChange={(assigned) => toggleAssigned(c.name, assigned)} />
            </li>
          ))}
        </ul>
      )}
      {showVault && <PlaceholderModal message="Vault credential manager coming soon" onClose={() => setShowVault(false)} />}
    </div>
  )
}

// # --- Containers tab ---
function ContainersTab({ workspace }) {
  // Filter out the system resource container (prefix tm-resource-).
  const containers = (workspace.containers || []).filter((c) => !(c.name || '').startsWith('tm-resource-'))
  const count = containers.length
  const isRunning = (status) => /^(running|active|up|started)$/i.test(status || '')
  return (
    <div className="wp-section">
      <div className="wp-section-header">
        <h3>Containers</h3>
        <span className="wp-limit">{count} of {CONTAINER_LIMIT} containers used</span>
      </div>
      {count === 0 ? (
        <p className="wp-empty">No containers running for this workspace.</p>
      ) : (
        <table className="wp-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Status</th>
              <th>Uptime</th>
              <th>Note</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {containers.map((c) => (
              <tr key={c.name}>
                <td>{c.name}</td>
                <td>
                  <span className={`wp-dot ${isRunning(c.status) ? 'wp-dot-green' : 'wp-dot-red'}`} />
                  {c.status || 'unknown'}
                </td>
                <td>{c.uptime || '—'}</td>
                <td>{c.note || '—'}</td>
                <td>
                  <div className="wp-btn-row">
                    <button className="wp-btn" onClick={() => console.log('[Containers] start', c.name)}>Start</button>
                    <button className="wp-btn" onClick={() => console.log('[Containers] stop', c.name)}>Stop</button>
                    <button className="wp-btn" onClick={() => console.log('[Containers] logs', c.name)}>Logs</button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

// # --- Workers tab ---
function WorkersTab({ workspace }) {
  const [expanded, setExpanded] = useState(null)
  const [showEditor, setShowEditor] = useState(false)
  const [showAddPreset, setShowAddPreset] = useState(false)
  const workers = workspace.workers || []
  return (
    <div className="wp-section">
      <div className="wp-section-header">
        <h3>Workers</h3>
      </div>
      {workers.length === 0 ? (
        <p className="wp-empty">No worker presets configured for this workspace.</p>
      ) : (
        <div className="wp-worker-grid">
          {workers.map((w) => {
            const isExpanded = expanded === w.name
            const prompt = w.systemPrompt || ''
            const preview = prompt.length > PROMPT_PREVIEW_LENGTH && !isExpanded
              ? prompt.slice(0, PROMPT_PREVIEW_LENGTH) + '…'
              : prompt
            return (
              <div className="wp-worker-card" key={w.name}>
                <div className="wp-worker-top">
                  <h4 className="wp-worker-name">{w.name}</h4>
                  <button className="wp-btn" onClick={() => setShowEditor(true)}>Edit</button>
                </div>
                <button
                  className="wp-worker-prompt"
                  onClick={() => setExpanded(isExpanded ? null : w.name)}
                  title={isExpanded ? 'Collapse' : 'Expand'}
                >
                  {preview || 'No system prompt set.'}
                </button>
                <div className="wp-worker-meta">
                  <span>{countOf(w.tools)} tools</span>
                  <span>{countOf(w.permissions)} permissions</span>
                  <span>{w.tokenLimit ?? '—'} tokens</span>
                </div>
              </div>
            )
          })}
        </div>
      )}
      <div className="wp-section-footer">
        <button className="wp-btn" onClick={() => setShowAddPreset(true)}>Add Preset</button>
      </div>
      {showEditor && <PlaceholderModal message="Worker preset editor coming soon" onClose={() => setShowEditor(false)} />}
      {showAddPreset && <PlaceholderModal message="Worker preset editor coming soon" onClose={() => setShowAddPreset(false)} />}
    </div>
  )
}

// # --- Session Defaults tab ---
// Local form state (keyed by workspace.id so it remounts on workspace switch);
// onBlur pushes each field, Save pushes the whole form and confirms.
function SessionDefaultsTab({ workspace, update }) {
  const defaults = workspace.sessionDefaults || {}
  const [form, setForm] = useState({
    systemPrompt: defaults.systemPrompt || '',
    tokenLimit: defaults.tokenLimit ?? 8000,
    temperature: defaults.temperature ?? 0.7,
    maxTurns: defaults.maxTurns ?? 20,
    toolOutputTokenLimit: defaults.toolOutputTokenLimit ?? 2000,
    allowedProviders: defaults.allowedProviders || [],
    defaultPreset: defaults.defaultPreset || 'balanced',
  })
  const [saved, setSaved] = useState(false)

  const commit = (next) => {
    setForm(next)
    update('sessionDefaults', next)
  }
  const commitField = (field, value) => commit({ ...form, [field]: value })

  const toggleProvider = (provider) => {
    const has = form.allowedProviders.includes(provider)
    const next = has
      ? form.allowedProviders.filter((p) => p !== provider)
      : [...form.allowedProviders, provider]
    commitField('allowedProviders', next)
  }

  const handleSave = () => {
    update('sessionDefaults', form)
    setSaved(true)
    window.setTimeout(() => setSaved(false), 1500)
  }

  // Show the current value even when it is not one of the standard options
  // (the store seeds defaultPreset: 'balanced').
  const presetOptions = PRESET_OPTIONS.includes(form.defaultPreset)
    ? PRESET_OPTIONS
    : [form.defaultPreset, ...PRESET_OPTIONS]

  return (
    <div className="wp-section">
      <div className="wp-section-header">
        <h3>Session Defaults</h3>
      </div>
      <div className="wp-form">
        <label className="wp-field wp-field-full">
          <span>System prompt</span>
          <textarea
            className="wp-textarea"
            rows={5}
            value={form.systemPrompt}
            onChange={(e) => setForm({ ...form, systemPrompt: e.target.value })}
            onBlur={(e) => commitField('systemPrompt', e.target.value)}
          />
        </label>

        <label className="wp-field">
          <span>Token limit</span>
          <input
            className="wp-input"
            type="number"
            min={1}
            value={form.tokenLimit}
            onChange={(e) => setForm({ ...form, tokenLimit: Number(e.target.value) || 0 })}
            onBlur={(e) => commitField('tokenLimit', Number(e.target.value) || 0)}
          />
        </label>

        <label className="wp-field">
          <span>Temperature — {form.temperature}</span>
          <input
            className="wp-slider"
            type="range"
            min={0}
            max={2}
            step={0.1}
            value={form.temperature}
            onChange={(e) => setForm({ ...form, temperature: Number(e.target.value) })}
            onBlur={(e) => commitField('temperature', Number(e.target.value))}
          />
        </label>

        <label className="wp-field">
          <span>Max turns</span>
          <input
            className="wp-input"
            type="number"
            min={1}
            max={200}
            value={form.maxTurns}
            onChange={(e) => setForm({ ...form, maxTurns: Number(e.target.value) || 0 })}
            onBlur={(e) => commitField('maxTurns', Number(e.target.value) || 0)}
          />
        </label>

        <label className="wp-field">
          <span>Tool output token limit</span>
          <input
            className="wp-input"
            type="number"
            min={1}
            value={form.toolOutputTokenLimit}
            onChange={(e) => setForm({ ...form, toolOutputTokenLimit: Number(e.target.value) || 0 })}
            onBlur={(e) => commitField('toolOutputTokenLimit', Number(e.target.value) || 0)}
          />
        </label>

        <div className="wp-field wp-field-full">
          <span>Allowed providers</span>
          <div className="wp-checkboxes">
            {PROVIDERS.map((p) => (
              <label key={p} className="wp-checkbox">
                <input
                  type="checkbox"
                  checked={form.allowedProviders.includes(p)}
                  onChange={() => toggleProvider(p)}
                />
                <span>{p}</span>
              </label>
            ))}
          </div>
        </div>

        <label className="wp-field">
          <span>Default session preset</span>
          <select
            className="wp-select"
            value={form.defaultPreset}
            onChange={(e) => commitField('defaultPreset', e.target.value)}
          >
            {presetOptions.map((o) => (
              <option key={o} value={o}>{o}</option>
            ))}
          </select>
        </label>

        <div className="wp-form-footer">
          <button className="wp-btn wp-btn-primary" onClick={handleSave}>Save</button>
          {saved && <span className="wp-saved">Saved</span>}
        </div>
      </div>
    </div>
  )
}

// # --- Main panel ---
export default function WorkspacePanel() {
  const route = useRoute()
  const workspaceId = route?.id
  const workspace = useWorkspaceStore((s) => s.currentWorkspace)
  const isLoading = useWorkspaceStore((s) => s.isLoading)
  const fetchWorkspaceConfig = useWorkspaceStore((s) => s.fetchWorkspaceConfig)
  const updateWorkspaceConfig = useWorkspaceStore((s) => s.updateWorkspaceConfig)

  const [activeTab, setActiveTab] = useState(DEFAULT_TAB)
  // Distinguishes "fetch not yet run for this id" (spinner) from
  // "fetched and not found" (error) when navigating between workspaces.
  const [fetchedId, setFetchedId] = useState(null)

  useEffect(() => {
    if (workspaceId) {
      fetchWorkspaceConfig(workspaceId)
      setFetchedId(workspaceId)
    }
  }, [workspaceId, fetchWorkspaceConfig])

  if (!workspaceId) {
    return (
      <div className="wp-panel">
        <div className="wp-error">
          <p>No workspace selected.</p>
          <a className="wp-back-link" href="#/workspaces">← Back to workspaces</a>
        </div>
      </div>
    )
  }

  const workspaceReady = fetchedId === workspaceId

  if (!workspaceReady || isLoading) {
    return (
      <div className="wp-panel">
        <div className="wp-loading">
          <span className="wp-spinner" aria-label="Loading workspace" />
          <p>Loading workspace…</p>
        </div>
      </div>
    )
  }

  if (!workspace || workspace.id !== workspaceId) {
    return (
      <div className="wp-panel">
        <div className="wp-error">
          <p>Workspace not found.</p>
          <p className="wp-error-hint">It may have been removed, or the link is wrong.</p>
          <a className="wp-back-link" href="#/workspaces">← Back to workspaces</a>
        </div>
      </div>
    )
  }

  const update = (field, value) => updateWorkspaceConfig(workspace.id, { [field]: value })
  const advisory = computeAdvisory(workspace)

  const handleSuggestion = (suggestion) => {
    if (suggestion.action === 'disable-resource') {
      update('resources', (workspace.resources || []).map((r) =>
        r.name === suggestion.resource ? { ...r, enabled: false } : r
      ))
    } else if (suggestion.action === 'lower-network') {
      update('permissions', permissionsWithCeiling(workspace.permissions, 'network', suggestion.level))
    } else if (suggestion.action === 'install-docker') {
      // DOCKER_AVAILABLE is hardcoded true, so this branch is unreachable today.
      console.log('[WorkspacePanel] Install Docker requested (Phase 4 will wire real detection)')
    }
  }

  return (
    <div className="wp-panel">
      <header className="wp-header">
        <a className="wp-back-link" href="#/workspaces">← Back to workspaces</a>
        <div className="wp-title-row">
          <h2 className="wp-title">{workspace.name}</h2>
          <span className={`wp-risk ${RISK_CLASS[workspace.risk] || 'low'}`}>{workspace.risk}</span>
        </div>
        <p className="wp-path">{workspace.path}</p>
        <button
          className="wp-btn wp-btn-primary wp-new-session"
          onClick={() => console.log('New session for workspace', workspace.id)}
        >
          New Session
        </button>
      </header>

      <div className="wp-body">
        <aside className="wp-advisory">
          <h3 className="wp-advisory-title">Safety Advisory</h3>
          <div className={`wp-advisory-card wp-advisory-${advisory.status}`}>
            <span className="wp-advisory-icon" role="img" aria-label={advisory.status}>
              {advisory.status === 'green' ? '🟢' : advisory.status === 'amber' ? '🟡' : '🔴'}
            </span>
            <p className="wp-advisory-message">{advisory.message}</p>
            <ul className="wp-advisory-suggestions">
              {advisory.suggestions.map((s, i) => (
                <li key={`${s.label}-${i}`}>
                  {s.disabled ? (
                    <span className="wp-advisory-ok">{s.label}</span>
                  ) : (
                    <button className="wp-btn wp-btn-suggestion" onClick={() => handleSuggestion(s)}>{s.label}</button>
                  )}
                </li>
              ))}
            </ul>
          </div>
        </aside>

        <main className="wp-main">
          <nav className="wp-tabs" role="tablist">
            {TABS.map((tab) => (
              <button
                key={tab}
                role="tab"
                aria-selected={activeTab === tab}
                className={`wp-tab ${activeTab === tab ? 'wp-tab-active' : ''}`}
                onClick={() => setActiveTab(tab)}
              >
                {tab}
              </button>
            ))}
          </nav>

          <section className="wp-content" role="tabpanel">
            {activeTab === 'Resources' && <ResourcesTab workspace={workspace} update={update} />}
            {activeTab === 'Permissions' && <PermissionsTab workspace={workspace} update={update} />}
            {activeTab === 'Tools' && <ToolsTab workspace={workspace} update={update} />}
            {activeTab === 'Credentials' && <CredentialsTab workspace={workspace} update={update} />}
            {activeTab === 'Containers' && <ContainersTab workspace={workspace} />}
            {activeTab === 'Workers' && <WorkersTab workspace={workspace} />}
            {activeTab === 'Session Defaults' && (
              <SessionDefaultsTab key={workspace.id} workspace={workspace} update={update} />
            )}
          </section>
        </main>
      </div>
    </div>
  )
}
