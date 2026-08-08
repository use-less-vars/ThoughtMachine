// --- WorkspaceSelector.jsx ---
// Landing view when no session tab is open (Phase 1): pick a purpose to
// create a workspace, or open an existing one from the sidebar list.
// Routing goes through the dependency-free hash router (src/router.js).

import React from 'react'
import { useNavigate } from '../router'
import useWorkspaceStore from '../store/workspaceStore'
import purposeDefinitions from '../data/purposeDefinitions.json'
import './WorkspaceSelector.css'

const RISK_CLASS = { Low: 'low', Medium: 'medium', High: 'high', Critical: 'critical' }

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

  const handlePurposeSelect = (purpose) => {
    const id = createWorkspace(purpose.id)
    if (id) navigate(`/workspace/${id}`)
  }

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
        </div>
      </main>
    </div>
  )
}
