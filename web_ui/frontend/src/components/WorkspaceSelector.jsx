// --- WorkspaceSelector.jsx ---
// Landing view when no session tab is open (Phase 1): pick a purpose to
// create a workspace, or open an existing one from the sidebar list.
// Post-migration restore: the sidebar also lists live sessions (Bridge 1) and
// the grid offers a "Custom Workspace" card that binds an existing folder via
// POST /api/workspace/resolve (Bridge 2).
// Routing goes through the dependency-free hash router (src/router.js).

import React, { useState } from 'react'
import { useNavigate } from '../router'
import useWorkspaceStore from '../store/workspaceStore'
import useStore from '../store/useStore'
import FolderBrowser from './FolderBrowser'
import purposeDefinitions from '../data/purposeDefinitions.json'
import './WorkspaceSelector.css'

const RISK_CLASS = { Low: 'low', Medium: 'medium', High: 'high', Critical: 'critical' }

// Trim trailing separators so '/home/jojo' and '/home/jojo/' compare equal.
function normalizePath(p) {
  return String(p || '').replace(/[\\/]+$/, '')
}

function PurposeCard({ purpose, onSelect }) {
  const handleKeyDown = (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      onSelect(purpose)
    }
  }
  return (
    <article
      className="ws-card"
      role="button"
      tabIndex={0}
      onClick={() => onSelect(purpose)}
      onKeyDown={handleKeyDown}
    >
      <span className={`ws-risk ${RISK_CLASS[purpose.risk] || 'low'}`}>{purpose.risk}</span>
      <span className="ws-card-icon" role="img" aria-label={purpose.label}>
        {purpose.icon}
      </span>
      <h4 className="ws-card-title">{purpose.label}</h4>
      <p className="ws-card-desc">{purpose.description}</p>
      <div className="ws-card-details">
        <div className="ws-detail-line">{purpose.recommendedSettings}</div>
        <div className="ws-detail-line">Requires Docker: {purpose.requiresDocker ? 'yes' : 'no'}</div>
      </div>
    </article>
  )
}

export default function WorkspaceSelector() {
  const navigate = useNavigate()
  const workspaceList = useWorkspaceStore((s) => s.workspaceList)
  const createWorkspace = useWorkspaceStore((s) => s.createWorkspace)
  const fetchWorkspaces = useWorkspaceStore((s) => s.fetchWorkspaces)
  const resolveWorkspacePath = useWorkspaceStore((s) => s.resolveWorkspacePath)
  const sessions = useStore((s) => s.sessions)

  // Custom workspace modal state (Bridge 2).
  const [showCustomModal, setShowCustomModal] = useState(false)
  const [homeDir, setHomeDir] = useState('')
  const [selectedFolderPath, setSelectedFolderPath] = useState('')
  const [currentBrowsePath, setCurrentBrowsePath] = useState('')
  const [resolving, setResolving] = useState(false)
  const [error, setError] = useState('')
  const [acknowledgedRisk, setAcknowledgedRisk] = useState(false)

  const handlePurposeSelect = (purpose) => {
    const id = createWorkspace(purpose.id)
    if (id) navigate(`/workspace/${id}`)
  }

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

  // Session list comes live from useStore (App.jsx keeps it in sync via the hub
  // WS 'sessions_list' event), newest first.
  const sortedSessions = [...sessions].sort((a, b) => {
    const ta = a.updated_at ? new Date(a.updated_at).getTime() : 0
    const tb = b.updated_at ? new Date(b.updated_at).getTime() : 0
    return tb - ta
  })

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
      <aside className="ws-sidebar">
        <h3 className="ws-sidebar-title">Workspaces</h3>
        {workspaceList.length === 0 ? (
          <p className="ws-sidebar-empty">No workspaces yet. Create one to get started.</p>
        ) : (
          <ul className="ws-list">
            {workspaceList.map((ws) => (
              <li key={ws.id}>
                <button className="ws-item" onClick={() => navigate(`/workspace/${ws.id}`)}>
                  <span className="ws-item-name">{ws.name}</span>
                  <span className={`ws-risk ${RISK_CLASS[ws.risk] || 'low'}`}>{ws.risk}</span>
                  <span className="ws-item-path">{ws.path}</span>
                </button>
              </li>
            ))}
          </ul>
        )}

        <section className="ws-sessions" aria-label="Sessions">
          <div className="ws-sessions-title">Sessions</div>
          {sortedSessions.length === 0 ? (
            <p className="ws-sessions-empty">No sessions yet.</p>
          ) : (
            <ul className="ws-sessions-list">
              {sortedSessions.map((s) => (
                <li key={s.session_id}>
                  <button
                    className="ws-session-item"
                    onClick={() => navigate(`/session/${encodeURIComponent(s.session_id)}`)}
                  >
                    {s.name || 'Untitled Session'}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>

        <button className="ws-refresh-btn" onClick={() => fetchWorkspaces()}>
          Refresh
        </button>
      </aside>
      <main className="ws-main">
        <h2 className="ws-main-title">What kind of work do you want to do today?</h2>
        <div className="ws-grid">
          {purposeDefinitions.map((purpose) => (
            <PurposeCard key={purpose.id} purpose={purpose} onSelect={handlePurposeSelect} />
          ))}
          <article
            className="ws-card ws-card-custom"
            role="button"
            tabIndex={0}
            onClick={openCustomModal}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault()
                openCustomModal()
              }
            }}
          >
            <span className="ws-risk low">Custom</span>
            <span className="ws-card-icon" role="img" aria-label="Custom Workspace">
              📁
            </span>
            <span className="ws-card-title">Custom Workspace</span>
            <p className="ws-card-desc">Bind this workspace to an existing folder on your machine.</p>
            <div className="ws-card-details">
              <div className="ws-detail-line">The folder must be inside your home directory.</div>
            </div>
          </article>
        </div>
      </main>

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
    </div>
  )
}
