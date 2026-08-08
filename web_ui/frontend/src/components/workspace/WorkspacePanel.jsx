// --- WorkspacePanel.jsx ---
// Phase 3: the /workspace/:id page — a single-page "office blueprint" with
// tabbed sections (Resources, Permissions, Tools, Credentials, Containers,
// Workers, Session Defaults) plus a persistent safety advisory sidebar.
// All API calls go through the REAL actions in workspaceStore.js
// (fetchWorkspaceConfig / updateWorkspaceConfig / fetchContainers /
// containerAction). Routing uses the dependency-free hash router (src/router.js).
//
// Phase 4: structural split — the tab components now live in ./tabs/, the
// modals in ./modals/, and the shared helpers in ./workspaceUtils.js. This
// file is the thin shell that wires them together (routing, store state,
// advisory sidebar, error banners, tab switching).

import React, { useEffect, useState } from 'react'
import { useRoute } from '../../router'
import useWorkspaceStore from '../../store/workspaceStore'
import purposeDefinitions from '../../data/purposeDefinitions.json'
import { RISK_CLASS, permissionsWithCeiling, permissionEffective, isHigherPermission } from './workspaceUtils.jsx'
import ResourcesTab from './tabs/ResourcesTab'
import PermissionsTab from './tabs/PermissionsTab'
import ToolsTab from './tabs/ToolsTab'
import CredentialsTab from './tabs/CredentialsTab'
import ContainersTab from './tabs/ContainersTab'
import WorkersTab from './tabs/WorkersTab'
import SessionDefaultsTab from './tabs/SessionDefaultsTab'
import NewSessionModal from './modals/NewSessionModal'
import './WorkspacePanel.css'

const TABS = ['Resources', 'Permissions', 'Tools', 'Credentials', 'Containers', 'Workers', 'Session Defaults']
const DEFAULT_TAB = 'Session Defaults'

// --- Safety advisory (computed from the workspace, not the store's copy) ---
function computeAdvisory(workspace, dockerAvailable) {
  const risk = workspace.risk
  const resources = workspace.resources || []
  // Phase 1 resources carry `containerized` (boolean). A resource with
  // containerized:false is host-only — the spec's "non-containerizable".
  const hostOnlyEnabled = resources.filter((r) => r.containerized === false && r.enabled)

  const purpose = purposeDefinitions.find((p) => p.id === workspace.purposeId)
  const requiresDocker = !!(purpose && purpose.requiresDocker)
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

  // Host-only resource warnings apply only to Docker-based purposes
  // (code-development, hardware-hacking, security-research). Workspaces whose
  // purpose does not require Docker (e.g. social/research) are not nagged
  // about containerization.
  if (requiresDocker && hostOnlyEnabled.length > 0) {
    for (const r of hostOnlyEnabled) {
      suggestions.push({ action: 'disable-resource', label: `Disable ${r.name}`, resource: r.name })
    }
    if (dockerAvailable !== false) {
      // null (unverified) is treated as not-down: amber, not red.
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
          <p className="wp-warn-text">Could not verify Docker status — some features may be unavailable.</p>
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
