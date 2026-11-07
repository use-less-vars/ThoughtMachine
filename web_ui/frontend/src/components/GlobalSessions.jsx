// --- GlobalSessions.jsx ---
// Groups active sessions by workspace and renders each as a row that
// navigates to the session view.

import React from 'react'
import { useNavigate } from '../router'

function formatStartedAt(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (isNaN(d.getTime())) return ''
  return d.toLocaleString()
}

export default function GlobalSessions({ sessions = [], workspaces = [] }) {
  const navigate = useNavigate()

  if (sessions.length === 0) return <p className="gms-empty">No active sessions.</p>

  // Preserve the order in which workspaces first appear.
  const groups = new Map()
  sessions.forEach((s) => {
    const wsId = s.workspace_id
    if (!groups.has(wsId)) groups.set(wsId, [])
    groups.get(wsId).push(s)
  })

  return (
    <div className="gms-session-groups">
      {Array.from(groups.entries()).map(([wsId, groupSessions]) => {
        const ws = workspaces.find((w) => w.id === wsId)
        const title = (ws && ws.label) || wsId || 'Unknown workspace'
        return (
          <div key={wsId || 'unknown'}>
            <div className="gms-session-group-title">{title}</div>
            {groupSessions.map((s) => (
              <button
                type="button"
                className="gms-session-row"
                key={s.session_id}
                onClick={() => navigate(`/session/${encodeURIComponent(s.session_id)}`)}
              >
                <span className="gms-session-name">{s.name || 'Untitled Session'}</span>
                <span className="gms-session-mode">{s.mode}</span>
                <span className="gms-session-workers">{s.worker_count ?? 0} workers</span>
                <span className="gms-session-started">{formatStartedAt(s.started_at)}</span>
              </button>
            ))}
          </div>
        )
      })}
    </div>
  )
}
