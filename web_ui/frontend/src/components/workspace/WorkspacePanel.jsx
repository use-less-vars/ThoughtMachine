// --- WorkspacePanel.jsx ---
// Phase 3: the /workspace/:id page — a single-page "office blueprint" with
// tabbed sections (Resources, Permissions, Tools, Credentials, Containers,
// Workers, Session Defaults) plus a persistent safety advisory sidebar.
// All API calls go through the REAL actions in workspaceStore.js
// (fetchWorkspaceConfig / updateWorkspaceConfig / fetchContainers /
// containerAction). Routing uses the dependency-free hash router (src/router.js).

import React, { useEffect, useState, useMemo } from 'react'
import { useRoute } from '../../router'
import useWorkspaceStore from '../../store/workspaceStore'
import purposeDefinitions from '../../data/purposeDefinitions.json'
import NewSessionModal from './NewSessionModal'
import './WorkspacePanel.css'

const TABS = ['Resources', 'Permissions', 'Tools', 'Credentials', 'Containers', 'Workers', 'Session Defaults']
const DEFAULT_TAB = 'Session Defaults'

const RISK_CLASS = { Low: 'low', Medium: 'medium', High: 'high', Critical: 'critical' }

const PERMISSION_LEVELS = ['banned', 'read', 'write', 'ask', 'full', 'enabled']
const PERM_RANK = { banned: 0, read: 1, write: 2, ask: 3, full: 4 }

const PROVIDERS = ['Local LLM', 'OpenAI', 'Anthropic', 'DeepSeek']
const PRESET_OPTIONS = ['Agent (full)', 'Engineer (read-only)', 'Custom']

const CONTAINER_LIMIT = 4          // workspace container limit (matches the backend)
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
function computeAdvisory(workspace, dockerAvailable) {
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
    if (dockerAvailable !== false) {
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
function PlaceholderModal({ title, message, onClose }) {
  return (
    <div className="wp-modal-overlay" onClick={onClose}>
      <div className="wp-modal" role="dialog" aria-label={title || message} onClick={(e) => e.stopPropagation()}>
        {title && <h3 className="wp-modal-title">{title}</h3>}
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

// # --- Resource catalog modal ---
// Add Resource: checklist of available resources. The server catalog
// (GET /api/resource-catalog) does not exist in the backend yet (see
// apiContracts.js pending section) — store.fetchResourceCatalog falls back to
// a bundled placeholder list so the modal always has items to offer.
function ResourceCatalogModal({ onAdd, onClose }) {
  const fetchResourceCatalog = useWorkspaceStore((s) => s.fetchResourceCatalog)
  const [catalog, setCatalog] = useState([])
  const [checked, setChecked] = useState({})

  useEffect(() => {
    let alive = true
    fetchResourceCatalog().then((items) => {
      if (!alive) return
      const list = Array.isArray(items) ? items : []
      setCatalog(list)
      setChecked(Object.fromEntries(list.map((it) => [it.name, true])))
    })
    return () => { alive = false }
  }, [fetchResourceCatalog])

  const toggleCheck = (name) => setChecked((prev) => ({ ...prev, [name]: !prev[name] }))

  const handleAdd = () => {
    const selected = catalog
      .filter((it) => checked[it.name])
      .map((it) => ({
        name: it.name,
        description: it.description || it.name,
        icon: '•',
        containerized: true,
        risk: 'Low',
        enabled: true,
      }))
    if (selected.length > 0) onAdd(selected)
    onClose()
  }

  return (
    <div className="wp-modal-overlay" onClick={onClose}>
      <div className="wp-modal" role="dialog" aria-label="Add resource" onClick={(e) => e.stopPropagation()}>
        <h3 className="wp-modal-title">Add resource</h3>
        <div className="wp-modal-body">
          <div className="wp-checkbox-list">
            {catalog.map((it) => (
              <div key={it.name}>
                <label className="wp-checkbox">
                  <input
                    type="checkbox"
                    checked={!!checked[it.name]}
                    onChange={() => toggleCheck(it.name)}
                  />
                  <span>{it.name}</span>
                </label>
                <p className="wp-resource-desc">{it.description}</p>
              </div>
            ))}
          </div>
          <p className="wp-modal-hint">Server catalog coming soon — showing bundled placeholder list.</p>
        </div>
        <div className="wp-modal-footer">
          <button className="wp-btn" onClick={onClose}>Cancel</button>
          <button className="wp-btn wp-btn-primary" onClick={handleAdd}>Add selected</button>
        </div>
      </div>
    </div>
  )
}

// # --- Vault credential modal ---
const VAULT_PLACEHOLDERS = [
  { name: 'openai_api_key', hint: 'stored in vault' },
  { name: 'github_token', hint: 'stored in vault' },
  { name: 'docker_registry_auth', hint: 'stored in vault' },
  { name: 'ssh_deploy_key', hint: 'stored in vault' },
  { name: 'huggingface_token', hint: 'stored in vault' },
]

function CredentialModal({ onAdd, onClose }) {
  const [checked, setChecked] = useState({})

  const toggleCheck = (name) => setChecked((prev) => ({ ...prev, [name]: !prev[name] }))

  const handleAdd = () => {
    const selected = VAULT_PLACEHOLDERS
      .filter((it) => checked[it.name])
      .map((it) => ({
        name: it.name,
        hint: it.hint,
        type: 'vault',
        assigned: true,
        placeholder: `{{credential:${it.name}}}`,
      }))
    if (selected.length > 0) onAdd(selected)
    onClose()
  }

  return (
    <div className="wp-modal-overlay" onClick={onClose}>
      <div className="wp-modal" role="dialog" aria-label="Add credential" onClick={(e) => e.stopPropagation()}>
        <h3 className="wp-modal-title">Add credential</h3>
        <div className="wp-modal-body">
          <div className="wp-checkbox-list">
            {VAULT_PLACEHOLDERS.map((c) => (
              <label key={c.name} className="wp-checkbox">
                <input
                  type="checkbox"
                  checked={!!checked[c.name]}
                  onChange={() => toggleCheck(c.name)}
                />
                <span>{c.name}</span>
              </label>
            ))}
          </div>
          <p className="wp-modal-hint">Vault integration coming soon — placeholder entries.</p>
        </div>
        <div className="wp-modal-footer">
          <button className="wp-btn" onClick={onClose}>Cancel</button>
          <button className="wp-btn wp-btn-primary" onClick={handleAdd}>Add selected</button>
        </div>
      </div>
    </div>
  )
}

// # --- Resources tab ---
function ResourcesTab({ workspace, update }) {
  const [showCatalog, setShowCatalog] = useState(false)
  const resources = workspace.resources || []
  const toggleResource = (name, enabled) => {
    update('resources', resources.map((r) => (r.name === name ? { ...r, enabled } : r)))
  }
  const addResources = (items) => {
    const existing = new Set(resources.map((r) => r.name))
    update('resources', [...resources, ...items.filter((it) => !existing.has(it.name))])
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
      {showCatalog && <ResourceCatalogModal onAdd={addResources} onClose={() => setShowCatalog(false)} />}
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
  const addCredentials = (items) => {
    const existing = new Set(credentials.map((c) => c.name))
    update('credentials', [...credentials, ...items.filter((it) => !existing.has(it.name))])
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
      {showVault && <CredentialModal onAdd={addCredentials} onClose={() => setShowVault(false)} />}
    </div>
  )
}

// # --- Containers tab ---
function fmtUptime(seconds) {
  if (seconds == null || Number.isNaN(Number(seconds))) return '—'
  const s = Math.max(0, Math.floor(Number(seconds)))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${s % 60}s`
  const h = Math.floor(m / 60)
  return `${h}h ${m % 60}m`
}

function ContainersTab({ workspace, dockerAvailable, containerStatus, busyContainers, onRefresh, onAction, onError }) {
  const [logsFor, setLogsFor] = useState(null)
  // Filter out the system resource container (prefix tm-resource-).
  const containers = (workspace.containers || []).filter((c) => !(c.name || '').startsWith('tm-resource-'))
  const count = containers.length
  const isRunning = (status) => /^(running|active|up|started)$/i.test(status || '')
  const runAction = (name, action) => {
    if (!onAction) return
    onAction(name, action).catch((err) => onError && onError(err.message || String(err)))
  }
  return (
    <div className="wp-section">
      <div className="wp-section-header">
        <h3>Containers</h3>
        <span className="wp-limit">{count} of {CONTAINER_LIMIT} containers used</span>
      </div>
      {dockerAvailable === false ? (
        <div className="wp-empty">
          <p className="wp-error-text">Docker is unreachable from the server — container actions are disabled.</p>
        </div>
      ) : count === 0 ? (
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
            {containers.map((c) => {
              const status = (containerStatus && containerStatus[c.name]) || c.status || 'unknown'
              const running = isRunning(status)
              const busy = !!(busyContainers && busyContainers[c.name])
              return (
                <tr key={c.name} className={busy ? 'wp-busy' : ''}>
                  <td>{c.name}</td>
                  <td>
                    <span className={`wp-dot ${running ? 'wp-dot-green' : 'wp-dot-red'}`} />
                    {status}
                  </td>
                  <td>{fmtUptime(c.uptime_seconds)}</td>
                  <td>{c.note || '—'}</td>
                  <td>
                    <div className="wp-container-actions">
                      {running ? (
                        <button className="wp-btn" disabled={busy} onClick={() => runAction(c.name, 'stop')}>
                          {busy ? '…' : 'Stop'}
                        </button>
                      ) : (
                        <button className="wp-btn" disabled={busy} onClick={() => runAction(c.name, 'start')}>
                          {busy ? '…' : 'Start'}
                        </button>
                      )}
                      <button className="wp-btn wp-btn-danger" disabled={busy} onClick={() => runAction(c.name, 'remove')}>
                        {busy ? '…' : 'Remove'}
                      </button>
                      <button className="wp-btn" onClick={() => setLogsFor(c.name)}>Logs</button>
                    </div>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
      <div className="wp-section-footer">
        <span className="wp-poll-hint">Auto-refreshes every 5s while this tab is open.</span>
        <button className="wp-btn wp-refresh-btn" onClick={onRefresh} disabled={dockerAvailable === false}>Refresh</button>
      </div>
      {logsFor && (
        <PlaceholderModal title="Container logs" message="Live container logs are coming soon." onClose={() => setLogsFor(null)} />
      )}
    </div>
  )
}

// # --- Worker editor modal ---
function WorkerEditor({ worker, onSave, onClose, allTools, allPermissions }) {
  const isNew = !worker
  const [name, setName] = useState(isNew ? '' : (worker.name || ''))
  const [description, setDescription] = useState(worker ? (worker.description || '') : '')
  const [systemPrompt, setSystemPrompt] = useState(worker ? (worker.systemPrompt || '') : '')
  // Tools: array of enabled tool name strings (store worker.tools shape).
  const [tools, setTools] = useState(worker && Array.isArray(worker.tools) ? [...worker.tools] : [])
  // Permissions: [name, value] footprint pairs (store worker.workerPermissions shape).
  const [permissionEntries, setPermissionEntries] = useState(
    worker && worker.workerPermissions ? Object.entries(worker.workerPermissions) : []
  )
  const [customTool, setCustomTool] = useState('')
  const [customPermission, setCustomPermission] = useState('')
  const [error, setError] = useState('')
  const toolNames = (allTools || []).map((t) => (typeof t === 'string' ? t : t.name)).filter(Boolean)
  const allToolNames = Array.from(new Set([...toolNames, ...tools]))
  const permNames = (allPermissions || []).map((p) => (typeof p === 'string' ? p : p.name)).filter(Boolean)
  const allPermNames = Array.from(new Set([...permNames, ...permissionEntries.map(([k]) => k)]))
  const permValue = (permName) => {
    const entry = permissionEntries.find(([k]) => k === permName)
    if (entry) return entry[1]
    const wp = (allPermissions || []).find((p) => (typeof p === 'string' ? p : p.name) === permName)
    return wp && typeof wp === 'object' ? (wp.effective || wp.ceiling || 'read') : 'read'
  }
  const toggleTool = (toolName) => {
    setTools((prev) => (prev.includes(toolName) ? prev.filter((t) => t !== toolName) : [...prev, toolName]))
  }
  const togglePermission = (permName) => {
    setPermissionEntries((prev) => {
      const has = prev.some(([k]) => k === permName)
      if (has) return prev.filter(([k]) => k !== permName)
      return [...prev, [permName, permValue(permName)]]
    })
  }
  const addCustomTool = () => {
    const t = customTool.trim()
    if (!t) return
    setTools((prev) => (prev.includes(t) ? prev : [...prev, t]))
    setCustomTool('')
  }
  const addCustomPermission = () => {
    const raw = customPermission.trim()
    if (!raw) return
    const idx = raw.indexOf(':')
    const key = (idx > 0 ? raw.slice(0, idx) : raw).trim()
    const value = idx > 0 ? raw.slice(idx + 1).trim() : permValue(key)
    if (!key) return
    setPermissionEntries((prev) => [...prev.filter(([k]) => k !== key), [key, value]])
    setCustomPermission('')
  }

  const handleSave = () => {
    const trimmedName = name.trim()
    if (!trimmedName) {
      setError('Worker name is required.')
      return
    }
    const workerPermissions = {}
    for (const [k, v] of permissionEntries) workerPermissions[k] = v
    onSave({
      name: trimmedName,
      description: description.trim(),
      systemPrompt,
      tools,
      workerPermissions,
    })
  }

  return (
    <div className="wp-modal-overlay" onClick={onClose}>
      <div
        className="wp-modal wp-worker-editor"
        role="dialog"
        aria-label={isNew ? 'Add worker preset' : `Edit ${worker.name}`}
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="wp-modal-title">{isNew ? 'Add Worker Preset' : `Edit ${worker.name}`}</h3>
        <div className="wp-modal-body">
          <label className="wp-field">
            <span className="wp-field-label">Name</span>
            <input className="wp-input" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. code-reviewer" />
          </label>
          <label className="wp-field">
            <span className="wp-field-label">Description</span>
            <input className="wp-input" value={description} onChange={(e) => setDescription(e.target.value)} placeholder="What this worker does" />
          </label>
          <label className="wp-field">
            <span className="wp-field-label">System prompt</span>
            <textarea className="wp-textarea" rows={5} value={systemPrompt} onChange={(e) => setSystemPrompt(e.target.value)} />
          </label>
          <div className="wp-field">
            <span className="wp-field-label">Tools</span>
            <div className="wp-checkbox-list">
              {allToolNames.map((t) => (
                <label key={t} className="wp-checkbox">
                  <input type="checkbox" checked={tools.includes(t)} onChange={() => toggleTool(t)} />
                  <span>{t}</span>
                </label>
              ))}
            </div>
            <div className="wp-add-custom">
              <input className="wp-input" value={customTool} onChange={(e) => setCustomTool(e.target.value)} placeholder="custom tool name" />
              <button className="wp-btn" onClick={addCustomTool}>Add</button>
            </div>
          </div>
          <div className="wp-field">
            <span className="wp-field-label">Permissions</span>
            <div className="wp-checkbox-list">
              {allPermNames.map((p) => (
                <label key={p} className="wp-checkbox">
                  <input type="checkbox" checked={permissionEntries.some(([k]) => k === p)} onChange={() => togglePermission(p)} />
                  <span>{p}</span>
                </label>
              ))}
            </div>
            <div className="wp-add-custom">
              <input className="wp-input" value={customPermission} onChange={(e) => setCustomPermission(e.target.value)} placeholder="custom:value (e.g. filesystem:write)" />
              <button className="wp-btn" onClick={addCustomPermission}>Add</button>
            </div>
          </div>
          {error && <p className="wp-error-text">{error}</p>}
        </div>
        <div className="wp-modal-footer">
          <button className="wp-btn" onClick={onClose}>Cancel</button>
          <button className="wp-btn wp-btn-primary" onClick={handleSave}>Save</button>
        </div>
      </div>
    </div>
  )
}

// # --- Workers tab ---
function WorkersTab({ workspace, update }) {
  const [expanded, setExpanded] = useState(null)
  const [editing, setEditing] = useState(null)  // worker being edited (null = new)
  const [showEditor, setShowEditor] = useState(false)
  const [showAddPreset, setShowAddPreset] = useState(false)
  const workers = workspace.workers || []

  const handleSaveWorker = (next) => {
    if (editing) {
      // Replace in place, keyed by the previously edited name.
      update('workers', workers.map((w) => (w.name === editing.name ? next : w)))
    } else {
      // Append a new preset; the backend rejects duplicate names (409).
      update('workers', [...workers, next])
    }
    setShowEditor(false)
    setShowAddPreset(false)
    setEditing(null)
  }

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
                  <span className="wp-badge wp-badge-muted">{w.runtimeStatus || 'ready'}</span>
                  <button
                    className="wp-btn"
                    onClick={() => { setEditing(w); setShowEditor(true) }}
                  >
                    Edit
                  </button>
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
                  <span>{countOf(w.workerPermissions)} permissions</span>
                  <span>{w.tokenLimit ?? '—'} tokens</span>
                </div>
              </div>
            )
          })}
        </div>
      )}
      <div className="wp-section-footer">
        <button className="wp-btn" onClick={() => { setEditing(null); setShowAddPreset(true) }}>Add Preset</button>
      </div>
      {showEditor && <WorkerEditor key={editing ? editing.name : 'new'} worker={editing} onSave={handleSaveWorker} onClose={() => setShowEditor(false)} allTools={workspace.tools || []} allPermissions={normalizePermissions(workspace.permissions)} />}
      {showAddPreset && <WorkerEditor key="new-preset" worker={null} onSave={handleSaveWorker} onClose={() => setShowAddPreset(false)} allTools={workspace.tools || []} allPermissions={normalizePermissions(workspace.permissions)} />}
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
  const dockerAvailable = useWorkspaceStore((s) => s.dockerAvailable)
  const containerStatus = useWorkspaceStore((s) => s.containerStatus)
  const busyContainers = useWorkspaceStore((s) => s.busyContainers)
  const fetchContainers = useWorkspaceStore((s) => s.fetchContainers)
  const containerAction = useWorkspaceStore((s) => s.containerAction)
  const storeError = useWorkspaceStore((s) => s.error)
  const clearError = useWorkspaceStore((s) => s.clearError)

  const [activeTab, setActiveTab] = useState(DEFAULT_TAB)
  const [showNewSession, setShowNewSession] = useState(false)
  const [actionError, setActionError] = useState('')
  const [advisoryDockerHint, setAdvisoryDockerHint] = useState('')
  // Distinguishes "fetch not yet run for this id" (spinner) from
  // "fetched and not found" (error) when navigating between workspaces.
  const [fetchedId, setFetchedId] = useState(null)

  useEffect(() => {
    if (workspaceId) {
      fetchWorkspaceConfig(workspaceId)
      setFetchedId(workspaceId)
    }
  }, [workspaceId, fetchWorkspaceConfig])

  // Poll the containers list while the Containers tab is open (5s cadence).
  // Uses workspace?.id / workspace?.root in deps (workspace object identity
  // changes on every store update).
  useEffect(() => {
    if (activeTab !== 'Containers' || !workspace || !workspace.root) return undefined
    const poll = () => fetchContainers(workspace.id).catch(() => {})
    poll()
    const timer = setInterval(poll, 5000)
    return () => clearInterval(timer)
  }, [activeTab, workspace?.id, workspace?.root, fetchContainers])

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
  const advisory = computeAdvisory(workspace, dockerAvailable)

  const handleSuggestion = (suggestion) => {
    if (suggestion.action === 'disable-resource') {
      update('resources', (workspace.resources || []).map((r) =>
        r.name === suggestion.resource ? { ...r, enabled: false } : r
      ))
    } else if (suggestion.action === 'lower-network') {
      update('permissions', permissionsWithCeiling(workspace.permissions, 'network', suggestion.level))
    } else if (suggestion.action === 'install-docker') {
      setAdvisoryDockerHint('Install Docker and restart the server to enable containerized execution.')
    }
  }

  const handleContainerAction = (name, action) => {
    setActionError('')
    return containerAction(workspace.id, name, action)
  }

  const handleDismissError = () => {
    setActionError('')
    clearError()
  }
  const handleRetryError = () => {
    setActionError('')
    fetchWorkspaceConfig(workspaceId)
    clearError()
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
          onClick={() => setShowNewSession(true)}
        >
          New Session
        </button>
      </header>

      {(storeError || actionError) && (
        <div className="wp-error-banner">
          <p className="wp-error-text">{actionError || storeError}</p>
          <div className="wp-banner-actions">
            <button className="wp-btn" onClick={handleDismissError}>Dismiss</button>
            <button className="wp-btn" onClick={handleRetryError}>Retry</button>
          </div>
        </div>
      )}
      {dockerAvailable === null && (
        <div className="wp-warn-banner">
          <p className="wp-warn-text">Could not verify Docker status — container actions may fail.</p>
        </div>
      )}

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
            {advisoryDockerHint && (
              <p className="wp-error-text wp-advisory-hint">{advisoryDockerHint}</p>
            )}
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
            {activeTab === 'Containers' && (
              <ContainersTab
                workspace={workspace}
                dockerAvailable={dockerAvailable}
                containerStatus={containerStatus}
                busyContainers={busyContainers}
                onRefresh={() => fetchContainers(workspace.id).catch(() => {})}
                onAction={handleContainerAction}
                onError={setActionError}
              />
            )}
            {activeTab === 'Workers' && <WorkersTab workspace={workspace} update={update} />}
            {activeTab === 'Session Defaults' && (
              <SessionDefaultsTab key={workspace.id} workspace={workspace} update={update} />
            )}
          </section>
        </main>
      </div>
      {showNewSession && <NewSessionModal workspace={workspace} onClose={() => setShowNewSession(false)} />}
    </div>
  )
}
