// --- GlobalContainers.jsx ---
// Lists active containers across workspaces. Read-only: no actions yet.

import React from 'react'

export default function GlobalContainers({ containers = [] }) {
  if (containers.length === 0) return <p className="gms-empty">No active containers.</p>

  return (
    <div className="gms-container-list">
      {containers.map((c, idx) => (
        <div className="gms-container-row" key={c.name || c.id || idx}>
          <span className="gms-container-name">{c.name}</span>
          <span className={`gms-badge gms-type-${c.type === 'resource' ? 'resource' : 'free'}`}>
            {c.type || 'free_use'}
          </span>
          <span className="gms-container-ws">{c.workspace_id}</span>
          <span className="gms-container-status">{c.status}</span>
        </div>
      ))}
    </div>
  )
}
