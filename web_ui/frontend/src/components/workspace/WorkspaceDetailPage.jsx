// --- WorkspaceDetailPage.jsx ---
// Layer 2 core: the workspace detail page. Shows the overview (root path,
// security posture, host-execution state, live counts), a host-execution
// toggle that persists through PUT /api/workspace/{id}/permissions, and
// tabbed sections: Permissions & Resources, Containers, Workers, Tools
// (Session Defaults and Credentials remain placeholders this pass).
// Reads GET /api/workspace/{id}/summary via useWorkspaceSummary.

import React, { useEffect, useState } from 'react'
import useWorkspaceSummary from './useWorkspaceSummary'
import { fetchTools, updateWorkspacePermissions } from './workspaceApi'
import VaultHealthBanner from '../VaultHealthBanner'
import NewSessionModal from './modals/NewSessionModal'
import './WorkspaceDetailPage.css'

const TABS = [
  'Overview',
  'Permissions & Resources',
  'Containers',
  'Workers',
  'Session Defaults',
  'Tools',
  'Credentials',
]

const HOST_ENABLE_WARNING =
  'This allows resources to run on the host. Only enable for trusted, supervised workspaces.'

// Inline styles for visuals the stylesheet does not define (CSS files are out
// of scope this pass): the success message and the per-tool chips.
const SUCCESS_STYLE = { color: '#a6e3a1', fontSize: '0.78rem' }
const TOOL_CHIP_STYLE = {
  display: 'inline-block',
  background: '#313244',
  border: '1px solid #45475a',
  borderRadius: '10px',
  padding: '0.05rem 0.5rem',
  marginRight: '0.3rem',
  fontSize: '0.72rem',
  color: '#a6adc8',
}

function placeholderMessage(tab) {
  if (tab === 'Session Defaults') return 'Session defaults will be configurable here soon.'
  if (tab === 'Credentials') return 'Workspace credential attachments coming soon.'
  return 'implemented in next pass'
}

function TabPlaceholder({ tab }) {
  return (
    <div className="wdp-placeholder">
      <div className="wdp-placeholder-title">{tab}</div>
      <div className="wdp-placeholder-text">{placeholderMessage(tab)}</div>
    </div>
  )
}

// ---- Permissions & Resources tab --------------------------------------------

// Per-resource ceiling vocabulary. The PUT /{ws_id}/permissions validator
// (agent/config/resource_catalog.validate_workspace_permissions) accepts exactly
// the 10 ceiling keys in CEILING_KEY_ORDER with a per-resource canonical
// vocabulary -- the SAME full level set that resource accepts as a session
// grant (git write_on_feature_branch, network outbound, mcp connect/full are
// all valid ceiling levels too, so every dropdown below offers the full set):
//   - git accepts banned|ask|read|write_on_feature_branch|write
//   - git_read, git_write, filesystem, system, execution accept
//     banned|ask|read|write
//   - network accepts banned|ask|write|outbound
//   - mcp accepts banned|connect|full
//   - host_bash accepts banned|ask|allow
//   - container is a real boolean (true == write-level enabled)
// summary.resource_catalog only supplies card metadata (labels, descriptions,
// tools, execution context); when a ceiling key has no catalog entry the card
// falls back to CEILING_METADATA_FALLBACK so all 10 keys stay editable even
// with an empty catalog. tty/jtag appear in the catalog but are NOT
// permission-gated (the validator rejects them outright) and render as
// informational cards after the 10 editors. So the editors are driven from
// this table, never from entry.permission_grain_set.
const GENERIC_CEILING_LEVELS = ['banned', 'ask', 'read', 'write']
const GIT_CEILING_LEVELS = ['banned', 'ask', 'read', 'write_on_feature_branch', 'write']
const NETWORK_CEILING_LEVELS = ['banned', 'ask', 'write', 'outbound']
const MCP_CEILING_LEVELS = ['banned', 'connect', 'full']
const HOST_BASH_CEILING_LEVELS = ['banned', 'ask', 'allow']

export const CONTAINER_CEILING = 'boolean'

// Fixed render order for the 10 ceiling-gated keys (validator order).
const CEILING_KEY_ORDER = [
  'git',
  'git_read',
  'git_write',
  'filesystem',
  'network',
  'system',
  'execution',
  'mcp',
  'container',
  'host_bash',
]

// Display metadata for ceiling keys that may have no summary.resource_catalog
// row (the catalog usually covers git/filesystem/container/host_bash; these
// fallbacks keep every card readable even when a row is missing).
const CEILING_METADATA_FALLBACK = {
  git_read: {
    display_name: 'Git (read)',
    description: 'Read access to git repositories (fetch/log/diff/status).',
  },
  git_write: {
    display_name: 'Git (write)',
    description: 'Write access to git repositories (commit/push/branch).',
  },
  network: {
    display_name: 'Network',
    description: 'Outbound network access from the execution context.',
  },
  system: {
    display_name: 'System',
    description: 'System-level operations (processes, environment, users).',
  },
  execution: {
    display_name: 'Execution',
    description: 'Command and code execution in the workspace context.',
  },
  mcp: {
    display_name: 'MCP',
    description: 'MCP server connections and tool access.',
  },
  container: {
    display_name: 'Container',
    description: 'Container-sandboxed code execution (Docker runtime).',
  },
  host_bash: {
    display_name: 'Host bash',
    description: 'Direct shell command execution on the host machine.',
  },
}

export const WORKSPACE_CEILING_OPTIONS = {
  git: GIT_CEILING_LEVELS,
  git_read: GENERIC_CEILING_LEVELS,
  git_write: GENERIC_CEILING_LEVELS,
  filesystem: GENERIC_CEILING_LEVELS,
  network: NETWORK_CEILING_LEVELS,
  system: GENERIC_CEILING_LEVELS,
  execution: GENERIC_CEILING_LEVELS,
  mcp: MCP_CEILING_LEVELS,
  container: CONTAINER_CEILING,
  host_bash: HOST_BASH_CEILING_LEVELS,
}

export function containerCeilingEnabled(value) {
  return value === true || value === 'write'
}

function firstEnabledLevel(options) {
  return (Array.isArray(options) && options.find((level) => level !== 'banned')) || 'read'
}

function PermissionsResourcesTab({
  summary,
  localPermissions,
  onPermissionChange,
  onApply,
  saving,
  applyError,
  applySuccess,
  dirty,
}) {
  const catalog = Array.isArray(summary.resource_catalog) ? summary.resource_catalog : []
  const catalogByName = new Map(catalog.map((entry) => [entry.name, entry]))

  function renderResourceCard({ name, label, context, description, options, tools, current }) {
    const booleanCeiling = options === CONTAINER_CEILING
    const noCeilingControl = options === undefined
    const enabled = booleanCeiling ? containerCeilingEnabled(current) : current !== 'banned'
    return (
      <div className="wdp-resource-card" key={name}>
        <div className="wdp-resource-header">
          <span className="wdp-resource-name">{label}</span>
          {context !== null && (
            <span className="wdp-badge wdp-context-badge">{context}</span>
          )}
          {noCeilingControl ? (
            <span className="wdp-badge wdp-context-badge">Not permission-gated</span>
          ) : (
            <span
              className={
                enabled ? 'wdp-badge wdp-badge-enabled' : 'wdp-badge wdp-badge-disabled'
              }
            >
              {enabled ? 'Enabled' : 'Disabled'}
            </span>
          )}
        </div>
        <div className="wdp-resource-desc">{description}</div>
        <div className="wdp-resource-controls">
          {booleanCeiling ? (
            <React.Fragment>
              <label className="wdp-perm-label">Permission</label>
              <button
                type="button"
                className={'wdp-toggle' + (enabled ? ' wdp-toggle-on' : '')}
                onClick={() => onPermissionChange(name, !enabled)}
                role="switch"
                aria-checked={enabled}
                aria-label={'Toggle ' + label}
              >
                <span className="wdp-toggle-knob" />
              </button>
              <span className="wdp-toggle-state">{enabled ? 'On' : 'Off'}</span>
            </React.Fragment>
          ) : noCeilingControl ? (
            <span className="wdp-tools-none">No ceiling control.</span>
          ) : (
            <React.Fragment>
              <label className="wdp-perm-label">
                Permission
                <select
                  className="wdp-perm-select"
                  value={current}
                  onChange={(event) => onPermissionChange(name, event.target.value)}
                >
                  {options.map((level) => (
                    <option key={level} value={level}>
                      {level}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                className={'wdp-toggle' + (enabled ? ' wdp-toggle-on' : '')}
                onClick={() =>
                  onPermissionChange(name, enabled ? 'banned' : firstEnabledLevel(options))
                }
                role="switch"
                aria-checked={enabled}
                aria-label={'Toggle ' + label}
              >
                <span className="wdp-toggle-knob" />
              </button>
              <span className="wdp-toggle-state">{enabled ? 'On' : 'Off'}</span>
            </React.Fragment>
          )}
        </div>
        <div className="wdp-tools-list">
          <span className="wdp-tools-label">Tools:</span>
          {Array.isArray(tools) && tools.length > 0 ? (
            tools.map((tool) => (
              <span className="wdp-tools-chip" style={TOOL_CHIP_STYLE} key={tool}>
                {tool}
              </span>
            ))
          ) : (
            <span className="wdp-tools-none">none</span>
          )}
        </div>
      </div>
    )
  }

  // Fixed-order 10 ceiling-gated cards: every key the PUT validator accepts is
  // always editable here, catalog row or not (metadata falls back gracefully).
  const ceilingCards = CEILING_KEY_ORDER.map((name) => {
    const entry = catalogByName.get(name)
    const fallback = CEILING_METADATA_FALLBACK[name]
    return renderResourceCard({
      name,
      label: (entry && entry.display_name) || (fallback && fallback.display_name) || name,
      context: entry ? entry.default_execution_context || 'unknown context' : null,
      description:
        (entry && entry.description) ||
        (fallback && fallback.description) ||
        'No description provided.',
      options: WORKSPACE_CEILING_OPTIONS[name],
      tools: entry ? entry.tools : null,
      current: localPermissions?.[name] ?? 'banned',
    })
  })

  // Catalog rows outside the 10-key ceiling map (tty/jtag...) are not
  // permission-gated; keep them as informational cards after the editors.
  const informationalCards = catalog
    .filter((entry) => WORKSPACE_CEILING_OPTIONS[entry.name] === undefined)
    .map((entry) =>
      renderResourceCard({
        name: entry.name,
        label: entry.display_name || entry.name,
        context: entry.default_execution_context || 'unknown context',
        description: entry.description || 'No description provided.',
        options: undefined,
        tools: entry.tools,
        current: localPermissions?.[entry.name] ?? 'banned',
      })
    )

  return (
    <div className="wdp-tab-content">
      <div className="wdp-section-title">Resource permissions</div>
      {ceilingCards}
      {informationalCards}

      <div className="wdp-perm-apply-row">
        {applyError && <div className="wdp-apply-error">{applyError}</div>}
        {applySuccess && (
          <div className="wdp-apply-success" style={SUCCESS_STYLE}>
            {applySuccess}
          </div>
        )}
        <button type="button" className="wdp-apply" disabled={!dirty || saving} onClick={onApply}>
          {saving ? 'Saving…' : 'Apply Permissions'}
        </button>
      </div>
    </div>
  )
}

// ---- Containers tab ----------------------------------------------------------

function ContainersTab({ summary }) {
  const dockerfile = summary.dockerfile || null
  const containers = Array.isArray(summary.active_containers) ? summary.active_containers : []

  return (
    <div className="wdp-tab-content">
      <div className="wdp-card">
        <div className="wdp-card-label">Dockerfile</div>
        <div className="wdp-dockerfile-path">
          {dockerfile && dockerfile.path ? dockerfile.path : 'No Dockerfile path recorded'}
        </div>
        <div className="wdp-dockerfile-note">
          {dockerfile && dockerfile.content
            ? 'Dockerfile content available'
            : 'No Dockerfile content recorded'}
        </div>
      </div>

      <div className="wdp-card">
        <div className="wdp-card-label">Active containers</div>
        {containers.length === 0 ? (
          <div className="wdp-empty">No active containers.</div>
        ) : (
          <div className="wdp-container-list">
            {containers.map((container) => (
              <div className="wdp-container-row" key={container.id || container.name}>
                <div className="wdp-container-main">
                  <span className="wdp-container-name">{container.name || 'unnamed'}</span>
                  <span className="wdp-badge wdp-context-badge">
                    {container.type || 'unknown type'}
                  </span>
                </div>
                <div className="wdp-container-meta">
                  <span className="wdp-container-status">{container.status || 'unknown'}</span>
                  {container.id && <span className="wdp-container-id">{container.id}</span>}
                  {container.workspace_id && (
                    <span className="wdp-container-ws">{container.workspace_id}</span>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

// ---- Workers tab -------------------------------------------------------------

function normalizeWorkerTemplates(raw) {
  if (Array.isArray(raw)) return raw
  if (raw && typeof raw === 'object') {
    // Defensive: a dict keyed by worker name.
    return Object.keys(raw).map((key) => {
      const value = raw[key]
      if (value && typeof value === 'object') return { name: key, ...value }
      return { name: key }
    })
  }
  return []
}

function formatElapsed(elapsed) {
  if (elapsed == null || Number.isNaN(Number(elapsed))) return '—'
  const total = Math.max(0, Math.floor(Number(elapsed)))
  if (total < 60) return total + 's'
  return Math.floor(total / 60) + 'm ' + (total % 60) + 's'
}

function WorkersTab({ summary }) {
  const templates = normalizeWorkerTemplates(summary.worker_templates)
  const activeWorkers = Array.isArray(summary.active_workers) ? summary.active_workers : []

  return (
    <div className="wdp-tab-content">
      <div className="wdp-card">
        <div className="wdp-card-label">Worker Templates</div>
        {templates.length === 0 ? (
          <div className="wdp-empty">No worker templates defined.</div>
        ) : (
          <div className="wdp-worker-list">
            {templates.map((template, index) => (
              <div className="wdp-worker-template" key={template.name || index}>
                <div className="wdp-worker-name">{template.name || 'unnamed template'}</div>
                {template.description && (
                  <div className="wdp-worker-desc">{template.description}</div>
                )}
                {Array.isArray(template.tool_classes) && template.tool_classes.length > 0 && (
                  <div className="wdp-tools-list">
                    <span className="wdp-tools-label">Tool classes:</span>
                    {template.tool_classes.join(', ')}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="wdp-card">
        <div className="wdp-card-label">Active Workers</div>
        {activeWorkers.length === 0 ? (
          <div className="wdp-empty">No active workers.</div>
        ) : (
          <div className="wdp-worker-list">
            {activeWorkers.map((worker) => (
              <div
                className="wdp-active-worker"
                key={worker.worker_name + '-' + worker.instance_id}
              >
                <span className="wdp-worker-name">{worker.worker_name || 'unknown'}</span>
                <span className="wdp-worker-instance">
                  #{worker.instance_id != null ? worker.instance_id : '—'}
                </span>
                <span className="wdp-badge wdp-context-badge">{worker.status || 'unknown'}</span>
                <span className="wdp-elapsed" title="Elapsed time">
                  {formatElapsed(worker.elapsed)}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

// ---- Tools tab ---------------------------------------------------------------

function isPermissionCeilingTool(tool) {
  return (
    tool &&
    (tool.permission_level != null ||
      (typeof tool.disabled_reason === 'string' &&
        tool.disabled_reason.indexOf('allow_host_resources') !== -1))
  )
}

function ToolsTab() {
  const [tools, setTools] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [tick, setTick] = useState(0)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    fetchTools()
      .then((data) => {
        if (!cancelled) setTools(Array.isArray(data.tools) ? data.tools : [])
      })
      .catch((err) => {
        if (!cancelled) setError((err && err.message) || 'Failed to load tools')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [tick])

  if (loading && tools === null) {
    return (
      <div className="wdp-loading">
        <div className="wdp-spinner" aria-label="Loading tools" />
      </div>
    )
  }

  if (error && tools === null) {
    return (
      <div className="wdp-error">
        <div className="wdp-error-text">{error}</div>
        <button
          type="button"
          className="wdp-retry"
          onClick={() => setTick((t) => t + 1)}
        >
          Retry
        </button>
      </div>
    )
  }

  if (tools === null) return null

  return (
    <div className="wdp-tab-content">
      <div className="wdp-note">
        Tool availability is controlled globally and is read-only from this workspace.
      </div>
      {tools.length === 0 ? (
        <div className="wdp-empty">No tools available.</div>
      ) : (
        <div className="wdp-tool-list">
          {tools.map((tool) => (
            <div className="wdp-tool-row" key={tool.name}>
              <span className="wdp-tool-name">{tool.name}</span>
              <span
                className={
                  tool.enabled ? 'wdp-badge wdp-badge-enabled' : 'wdp-badge wdp-badge-disabled'
                }
              >
                {tool.enabled ? 'Enabled' : 'Disabled'}
              </span>
              {tool.permission_level != null && (
                <span className="wdp-tool-perm">permission: {tool.permission_level}</span>
              )}
              {isPermissionCeilingTool(tool) && (
                <span className="wdp-tool-ceiling">Controlled by permission ceiling</span>
              )}
              {tool.disabled_reason && (
                <span className="wdp-tool-reason">{tool.disabled_reason}</span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ---- Page --------------------------------------------------------------------

export default function WorkspaceDetailPage({ workspaceId }) {
  const { summary, loading, error, refetch } = useWorkspaceSummary(workspaceId)
  const [hostAllowed, setHostAllowed] = useState(false)
  const [showWarning, setShowWarning] = useState(false)
  const [localPermissions, setLocalPermissions] = useState(null)
  const [saving, setSaving] = useState(false)
  const [applyError, setApplyError] = useState(null)
  const [applySuccess, setApplySuccess] = useState(null)
  const [activeTab, setActiveTab] = useState('Overview')
  const [showNewSession, setShowNewSession] = useState(false)

  // Keep the toggle and the permissions editor in sync with the persisted
  // values each time the summary (re)loads — including after a successful
  // Apply (refetch) so pending edits are discarded on success.
  useEffect(() => {
    if (summary) {
      setHostAllowed(!!summary.allow_host_resources)
      setLocalPermissions({ ...(summary.permissions || {}) })
    }
  }, [summary])

  const hostChanged = summary ? hostAllowed !== !!summary.allow_host_resources : false
  const permissionsDirty =
    summary != null &&
    localPermissions != null &&
    JSON.stringify(localPermissions) !== JSON.stringify(summary.permissions || {})
  const pendingChanges = hostChanged || permissionsDirty

  const activeSessions = summary?.active_sessions?.length || 0
  const activeWorkers = summary?.active_workers?.length || 0
  const containerCount = summary?.active_containers?.length || 0

  const posture = summary && summary.allow_host_resources
    ? 'Host resources allowed. This workspace is suitable only for trusted, supervised work.'
    : 'Host resources are forbidden. This workspace can withstand interactions with untrusted content.'

  const handleToggle = () => {
    if (hostAllowed) {
      // Turning OFF is safe — apply immediately to the pending state.
      setHostAllowed(false)
      setApplyError(null)
      setApplySuccess(null)
    } else {
      // Turning ON requires explicit confirmation.
      setShowWarning(true)
    }
  }

  const confirmHostEnable = () => {
    setHostAllowed(true)
    setShowWarning(false)
    setApplyError(null)
    setApplySuccess(null)
  }

  const handlePermissionChange = (resourceName, level) => {
    setLocalPermissions((prev) => ({ ...(prev || {}), [resourceName]: level }))
    setApplyError(null)
    setApplySuccess(null)
  }

  // Shared by the header Apply button and the tab's "Apply Permissions"
  // button. Both send the merged permission state; allow_host_resources is
  // included ONLY when the host toggle actually changed (the backend treats
  // an absent key as "unchanged").
  const handleApply = async () => {
    if (!summary || !pendingChanges || saving) return
    setSaving(true)
    setApplyError(null)
    setApplySuccess(null)
    try {
      const payload = {
        permissions: localPermissions || summary.permissions || {},
        ...(hostChanged ? { allow_host_resources: hostAllowed } : {}),
      }
      await updateWorkspacePermissions(workspaceId, payload)
      setShowWarning(false)
      await refetch()
      setApplySuccess('Permissions updated')
    } catch (err) {
      // Keep the local pending state — the editor must NOT be reset on failure.
      setApplyError((err && err.message) || 'Failed to update workspace permissions')
    } finally {
      setSaving(false)
    }
  }

  if (!workspaceId) {
    return <div className="wdp-empty">No workspace selected.</div>
  }

  if (error && !summary) {
    return (
      <div className="wdp-error">
        <div className="wdp-error-text">{error}</div>
        <button type="button" className="wdp-retry" onClick={refetch}>
          Retry
        </button>
      </div>
    )
  }

  if (loading && !summary) {
    return (
      <div className="wdp-loading">
        <div className="wdp-spinner" aria-label="Loading workspace" />
      </div>
    )
  }

  if (!summary) return null

  return (
    <div className="wdp-panel">
      <div className="wdp-header">
        <div className="wdp-title-block">
          <div className="wdp-title">{summary.label || summary.workspace_id}</div>
          <div className="wdp-ws-id">{summary.workspace_id}</div>
        </div>
        <div className="wdp-header-controls">
          <button
            type="button"
            className="wdp-new-session-btn"
            onClick={() => setShowNewSession(true)}
          >
            + New Session
          </button>
          <div className="wdp-counts">
            <span className="wdp-count" title="Active sessions">
              {activeSessions} sessions
            </span>
            <span className="wdp-count" title="Active workers">
              {activeWorkers} workers
            </span>
            <span className="wdp-count" title="Active containers">
              {containerCount} containers
            </span>
          </div>
          <div className="wdp-toggle-row">
            <span className="wdp-toggle-label">Host execution</span>
            <button
              type="button"
              className={'wdp-toggle' + (hostAllowed ? ' wdp-toggle-on' : '')}
              onClick={handleToggle}
              role="switch"
              aria-checked={hostAllowed}
              aria-label="Toggle host resource execution"
            >
              <span className="wdp-toggle-knob" />
            </button>
            <span className="wdp-toggle-state">{hostAllowed ? 'On' : 'Off'}</span>
          </div>
          {pendingChanges && <span className="wdp-pending-hint">Unsaved changes</span>}
          <button
            type="button"
            className="wdp-apply"
            disabled={!pendingChanges || saving}
            onClick={handleApply}
          >
            {saving ? 'Saving…' : 'Apply'}
          </button>
        </div>
        {applyError && <div className="wdp-apply-error">{applyError}</div>}
        {applySuccess && (
          <div className="wdp-apply-success" style={SUCCESS_STYLE}>
            {applySuccess}
          </div>
        )}
      </div>

      <VaultHealthBanner />

      <div className="wdp-tabs" role="tablist">
        {TABS.map((tab) => (
          <button
            key={tab}
            type="button"
            role="tab"
            aria-selected={activeTab === tab}
            className={'wdp-tab' + (activeTab === tab ? ' wdp-tab-active' : '')}
            onClick={() => setActiveTab(tab)}
          >
            {tab}
          </button>
        ))}
      </div>

      <div className="wdp-body">
        {activeTab === 'Overview' ? (
          <div className="wdp-overview">
            <div className="wdp-card">
              <div className="wdp-card-label">Root path</div>
              <div className="wdp-root-path">{summary.root_path}</div>
            </div>
            <div className="wdp-card">
              <div className="wdp-card-label">Security posture</div>
              <div
                className={
                  'wdp-posture' +
                  (summary.allow_host_resources ? ' wdp-posture-host' : ' wdp-posture-safe')
                }
              >
                {posture}
              </div>
            </div>
            <div className="wdp-card">
              <div className="wdp-card-label">Host execution</div>
              <div className={summary.allow_host_resources ? 'wdp-host-on' : 'wdp-host-off'}>
                {summary.allow_host_resources
                  ? 'Host execution enabled'
                  : 'Host execution disabled'}
              </div>
            </div>
            <div className="wdp-card wdp-card-row">
              <div className="wdp-stat">
                <div className="wdp-stat-value">{activeSessions}</div>
                <div className="wdp-stat-label">Active sessions</div>
              </div>
              <div className="wdp-stat">
                <div className="wdp-stat-value">{containerCount}</div>
                <div className="wdp-stat-label">Active containers</div>
              </div>
            </div>
          </div>
        ) : activeTab === 'Permissions & Resources' ? (
          <PermissionsResourcesTab
            summary={summary}
            localPermissions={localPermissions}
            onPermissionChange={handlePermissionChange}
            onApply={handleApply}
            saving={saving}
            applyError={applyError}
            applySuccess={applySuccess}
            dirty={permissionsDirty}
          />
        ) : activeTab === 'Containers' ? (
          <ContainersTab summary={summary} />
        ) : activeTab === 'Workers' ? (
          <WorkersTab summary={summary} />
        ) : activeTab === 'Tools' ? (
          <ToolsTab />
        ) : (
          <TabPlaceholder tab={activeTab} />
        )}
      </div>

      {showWarning && (
        <div className="wdp-modal-overlay">
          <div className="wdp-modal">
            <div className="wdp-modal-title">Enable host resource execution?</div>
            <div className="wdp-modal-text">{HOST_ENABLE_WARNING}</div>
            <div className="wdp-modal-actions">
              <button
                type="button"
                className="wdp-modal-cancel"
                onClick={() => setShowWarning(false)}
              >
                Cancel
              </button>
              <button type="button" className="wdp-modal-confirm" onClick={confirmHostEnable}>
                Confirm
              </button>
            </div>
          </div>
        </div>
      )}

      {showNewSession && (
        <NewSessionModal
          workspace={{
            id: summary.workspace_id,
            name: summary.label || summary.workspace_id,
            path: summary.root_path || '',
            root: summary.root_path || '',
          }}
          onClose={() => setShowNewSession(false)}
        />
      )}
    </div>
  )
}
