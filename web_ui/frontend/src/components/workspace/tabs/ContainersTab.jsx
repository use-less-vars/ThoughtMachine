// --- tabs/ContainersTab.jsx ---
// Moved verbatim from WorkspacePanel.jsx (Phase 4 structural split).
// The "Container logs" placeholder now lives at modals/ContainerLogsModal.jsx.

import React, { useState } from 'react'
import { fmtUptime, CONTAINER_LIMIT } from '../workspaceUtils.jsx'
import ContainerLogsModal from '../modals/ContainerLogsModal'

export default function ContainersTab({ workspace, dockerAvailable, containerStatus, busyContainers, onRefresh, onAction, onError }) {
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
        <ContainerLogsModal onClose={() => setLogsFor(null)} />
      )}
    </div>
  )
}
