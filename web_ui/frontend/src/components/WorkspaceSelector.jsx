// --- WorkspaceSelector.jsx ---
// Global Management landing view: workspaces, active sessions, active
// containers, shared resources, vault credentials, sysprompts and providers.
// Data comes from /api/global/summary (degrades gracefully when the backend
// does not expose it yet). Routing goes through the dependency-free hash
// router (src/router.js).

import React, { useCallback, useEffect, useState } from 'react'
import { useNavigate } from '../router'
import useWorkspaceStore from '../store/workspaceStore'
import FolderBrowser from './FolderBrowser'
import VaultHealthBanner from './VaultHealthBanner'
import GlobalSessions from './GlobalSessions'
import GlobalContainers from './GlobalContainers'
import GlobalResources from './GlobalResources'
import GlobalCredentials from './GlobalCredentials'
import PromptLibrary from './PromptLibrary'
import ManageProvidersModal from './ManageProvidersModal'
import { fetchGlobalSummary } from '../globalApi'
import './WorkspaceSelector.css'

// Trim trailing separators so '/home/jojo' and '/home/jojo/' compare equal.
function normalizePath(p) {
  return String(p || '').replace(/[\\/]+$/, '')
}

function formatLastActive(iso) {
  if (!iso) return 'never'
  const d = new Date(iso)
  if (isNaN(d.getTime())) return String(iso)
  return d.toLocaleString()
}

export default function WorkspaceSelector() {
  const navigate = useNavigate()
  const resolveWorkspacePath = useWorkspaceStore((s) => s.resolveWorkspacePath)

  const [summary, setSummary] = useState(null)
  const [loading, setLoading] = useState(true)
  const [refreshError, setRefreshError] = useState('')

  // Custom workspace modal state (Bridge 2).
  const [showCustomModal, setShowCustomModal] = useState(false)
  const [homeDir, setHomeDir] = useState('')
  const [selectedFolderPath, setSelectedFolderPath] = useState('')
  const [currentBrowsePath, setCurrentBrowsePath] = useState('')
  const [resolving, setResolving] = useState(false)
  const [error, setError] = useState('')
  const [acknowledgedRisk, setAcknowledgedRisk] = useState(false)

  const [showProviders, setShowProviders] = useState(false)

  const loadSummary = useCallback(async () => {
    setLoading(true)
    setRefreshError('')
    const data = await fetchGlobalSummary()
    if (data) setSummary(data)
    setLoading(false)
  }, [])

  useEffect(() => {
    loadSummary()
  }, [loadSummary])

  const summaryData = summary || {}
  const workspaces = Array.isArray(summaryData.workspaces) ? summaryData.workspaces : []
  const sessions = Array.isArray(summaryData.active_sessions) ? summaryData.active_sessions : []
  const containers = Array.isArray(summaryData.active_containers) ? summaryData.active_containers : []
  const providers = Array.isArray(summaryData.providers) ? summaryData.providers : []

  const openCustomModal = () => {
    setError('')
    setAcknowledgedRisk(false)
    setSelectedFolderPath('')
    setShowCustomModal(true)
    // Discover the user's home directory (used by the sensitive-folder warning).
    fetch('/api/user-home')
      .then((r) => r.json().catch(() => ({})))
      .then((data) => {
        if (data && data.home) setHomeDir(data.home)
      })
      .catch(() => {})
  }

  // Binding a workspace to $HOME or the vault would put unconfined files at the
  // agent's fingertips — require an explicit acknowledgement before proceeding.
  const isPathSensitive = (path) => {
    const p = normalizePath(path)
    const home = normalizePath(homeDir)
    if (!home || !p) return false
    return p === home || p === normalizePath(`${home}/.thoughtmachine`)
  }

  const resolve = async (path) => {
    setResolving(true)
    setError('')
    try {
      const data = await resolveWorkspacePath(path)
      setShowCustomModal(false)
      navigate(`/workspace/${data.workspace_id}`)
    } catch (err) {
      setError(err.message || 'Failed to resolve workspace')
    } finally {
      setResolving(false)
    }
  }

  const handleFolderSelect = (path) => {
    setSelectedFolderPath(path)
    try {
      localStorage.setItem('thoughtmachine_last_workspace', path)
    } catch {
      // storage unavailable — the selection still works in memory
    }
    if (isPathSensitive(path)) return // show the acknowledgement UI instead
    resolve(path)
  }

  const handleProceedSensitive = () => {
    const path = currentBrowsePath || selectedFolderPath
    if (!path) return
    setSelectedFolderPath(path)
    resolve(path)
  }

  const sensitiveSelected = !!selectedFolderPath && isPathSensitive(selectedFolderPath)

  return (
    <div className="ws-selector">
      <VaultHealthBanner />
      <main className="ws-main">
        <div className="gms-header">
          <h2 className="ws-main-title">Global Management</h2>
          <div className="gms-header-actions">
            <button className="ws-modal-btn" onClick={openCustomModal}>
              + New Workspace
            </button>
            <button className="ws-refresh-btn" onClick={loadSummary} disabled={loading}>
              Refresh
            </button>
          </div>
        </div>

        {refreshError && <p className="gms-error">{refreshError}</p>}

        <section className="gms-section" aria-label="Workspaces">
          <h3 className="gms-section-title">Workspaces</h3>
          {loading && workspaces.length === 0 ? (
            <p className="gms-empty">Loading workspaces…</p>
          ) : workspaces.length === 0 ? (
            <p className="gms-empty">No workspaces yet. Create one to get started.</p>
          ) : (
            <div className="ws-grid">
              {workspaces.map((ws) => (
                <article
                  className="ws-card"
                  role="button"
                  tabIndex={0}
                  onClick={() => navigate(`/workspace/${ws.id}`)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault()
                      navigate(`/workspace/${ws.id}`)
                    }
                  }}
                  key={ws.id}
                >
                  <div className="gms-card-head">
                    <h4 className="ws-card-title">{ws.label || ws.id}</h4>
                    <span className={`ws-risk ${ws.allow_host_resources ? 'allow' : 'forbid'}`}>
                      Host: {ws.allow_host_resources ? 'allowed' : 'forbidden'}
                    </span>
                  </div>
                  <p className="ws-card-desc">{ws.id}</p>
                  <div className="gms-card-stats">
                    <span className="gms-stat">{ws.active_sessions_count ?? 0} sessions</span>
                    <span className="gms-stat">{ws.total_workers ?? 0} workers</span>
                  </div>
                  <div className="ws-card-details">
                    <div className="ws-detail-line">Status: {ws.status || 'unknown'}</div>
                    <div className="ws-detail-line">Last active: {formatLastActive(ws.last_active)}</div>
                  </div>
                </article>
              ))}
            </div>
          )}
        </section>

        <section className="gms-section" aria-label="Active Sessions">
          <h3 className="gms-section-title">Active Sessions</h3>
          <GlobalSessions sessions={sessions} workspaces={workspaces} />
        </section>

        <section className="gms-section" aria-label="Active Containers">
          <h3 className="gms-section-title">Active Containers</h3>
          <GlobalContainers containers={containers} />
        </section>

        <section className="gms-section" aria-label="Global Resources">
          <h3 className="gms-section-title">Global Resources</h3>
          <GlobalResources />
        </section>

        <section className="gms-section" aria-label="Global Credentials">
          <h3 className="gms-section-title">Global Credentials</h3>
          <GlobalCredentials />
        </section>

        <section className="gms-section" aria-label="Global Sysprompts">
          <h3 className="gms-section-title">Global Sysprompts</h3>
          <PromptLibrary />
        </section>

        <section className="gms-section" aria-label="Providers">
          <h3 className="gms-section-title">Providers</h3>
          <button className="ws-modal-btn" onClick={() => setShowProviders(true)}>
            ⚙ Manage Providers
          </button>
        </section>

        {showProviders && (
          <ManageProvidersModal
            providers={providers}
            sendCommand={() => {}}
            onClose={() => setShowProviders(false)}
            onProviderSaved={() => setShowProviders(false)}
          />
        )}

        {showCustomModal && (
          <div className="ws-modal-overlay" onClick={() => setShowCustomModal(false)}>
            <div
              className="ws-modal-dialog"
              role="dialog"
              aria-label="Create custom workspace"
              onClick={(e) => e.stopPropagation()}
            >
              <h3 className="ws-modal-title">Create Custom Workspace</h3>
              <p className="ws-modal-hint">
                Pick an existing folder on this machine to use as the workspace root. It must be inside
                your home directory.
              </p>
              <div className="ws-modal-label">Workspace Folder</div>
              <FolderBrowser
                onSelect={handleFolderSelect}
                onNavigate={setCurrentBrowsePath}
                startPath={selectedFolderPath || undefined}
              />

              {resolving && <p className="ws-modal-resolving">Resolving workspace…</p>}

              {error && <p className="ws-modal-error">⚠ {error}</p>}

              {sensitiveSelected && (
                <div className="ws-modal-warning" role="alert">
                  <p className="ws-modal-warning-text">
                    You are binding this workspace to {selectedFolderPath}. Files in your home directory
                    or vault are not confined to a workspace sandbox. Proceed only if you understand the
                    risk.
                  </p>
                  <label className="ws-modal-ack">
                    <input
                      type="checkbox"
                      checked={acknowledgedRisk}
                      onChange={(e) => setAcknowledgedRisk(e.target.checked)}
                    />
                    I understand the risk and want to proceed
                  </label>
                  <button
                    className="ws-modal-btn ws-modal-btn-danger"
                    disabled={!acknowledgedRisk || resolving}
                    onClick={handleProceedSensitive}
                  >
                    Proceed
                  </button>
                </div>
              )}

              <div className="ws-modal-actions">
                <button
                  className="ws-modal-btn"
                  onClick={() => setShowCustomModal(false)}
                  disabled={resolving}
                >
                  Cancel
                </button>
              </div>
            </div>
          </div>
        )}
      </main>
    </div>
  )
}
