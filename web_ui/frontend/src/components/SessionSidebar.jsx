import React, { useEffect, useState } from 'react'
import useWorkspaceStore from '../store/workspaceStore'
import './SessionSidebar.css'

const WORKER_STATUS_COLORS = {
  ready: '#a6e3a1',
  busy: '#f9e2af',
  pausing: '#f9e2af',
  paused: '#f9e2af',
  error: '#f38ba8',
  stopped: '#6c7086',
}

const isRunningStatus = (status) => /^(running|active|up|started)$/i.test(status || '')

/**
 * Slide-out session details panel (toggled from the SessionTab header).
 * Four read-only/action sections:
 *  1. Permissions  — workspace ceiling + session-default effective (read-only)
 *  2. Active Tools — session tools checkboxes, applied via apply_config
 *  3. Workers      — runtime status per worker + Stop action
 *  4. Containers   — workspace containers (tm-resource-* excluded) + Start/Stop
 */
export default function SessionSidebar({ workspaceId, config, tools, sendCommand, onClose }) {
  const currentWorkspace = useWorkspaceStore((s) => s.currentWorkspace)
  const containerStatus = useWorkspaceStore((s) => s.containerStatus)
  const busyContainers = useWorkspaceStore((s) => s.busyContainers)
  const wsLoading = useWorkspaceStore((s) => s.isLoading)

  const [toolsOverride, setToolsOverride] = useState(null)
  const [stopError, setStopError] = useState(null)
  const [containerError, setContainerError] = useState(null)

  // Refresh workspace data (permissions/workers/containers) while open.
  useEffect(() => {
    if (!workspaceId) return
    const store = useWorkspaceStore.getState()
    store.fetchWorkspaceConfig(workspaceId)
    store.fetchContainers(workspaceId)
    const interval = setInterval(() => {
      useWorkspaceStore.getState().fetchContainers(workspaceId)
    }, 5000)
    return () => clearInterval(interval)
  }, [workspaceId])

  // Keep the optimistic tools override in sync once the backend confirms.
  useEffect(() => {
    setToolsOverride(null)
  }, [tools])

  const wsMatch = currentWorkspace?.id === workspaceId
  const permissions = wsMatch ? (currentWorkspace.permissions || []) : []
  const workers = wsMatch ? (currentWorkspace.workers || []) : []
  const containers = (wsMatch ? (currentWorkspace.containers || []) : [])
    .filter((c) => !(c.name || '').startsWith('tm-resource-'))

  const effectiveTools = toolsOverride ?? (Array.isArray(tools) ? tools : [])

  const toggleTool = (name) => {
    if (!config) return
    const next = effectiveTools.includes(name)
      ? effectiveTools.filter((t) => t !== name)
      : [...effectiveTools, name]
    setToolsOverride(next)
    sendCommand('apply_config', { config: { ...config, tools: next } })
  }

  const handleStopWorker = async (name) => {
    if (!workspaceId) return
    try {
      setStopError(null)
      const res = await fetch(
        `/api/workspace/${encodeURIComponent(workspaceId)}/workers/${encodeURIComponent(name)}/stop`,
        { method: 'POST' }
      )
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body?.detail?.error || `HTTP ${res.status}`)
      }
    } catch (err) {
      setStopError(err.message || String(err))
    }
  }

  const runContainerAction = async (name, action) => {
    if (!workspaceId) return
    try {
      setContainerError(null)
      await useWorkspaceStore.getState().containerAction(workspaceId, name, action)
    } catch (err) {
      setContainerError(err.message || String(err))
    }
  }

  return (
    <div className="session-sidebar" role="complementary" aria-label="Session details">
      <div className="session-sidebar-header">
        <span className="session-sidebar-title">Session Details</span>
        <button className="session-sidebar-close" onClick={onClose} title="Close panel">✕</button>
      </div>

      <div className="session-sidebar-body">
        {/* ── 1. Permissions (read-only ceiling + effective) ─────────────── */}
        <section className="session-sidebar-section">
          <h4>Permissions</h4>
          {wsLoading && wsMatch ? (
            <p className="session-sidebar-empty">Loading permissions...</p>
          ) : permissions.length === 0 ? (
            <p className="session-sidebar-empty">No permission data for this workspace.</p>
          ) : (
            <table className="session-sidebar-table">
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
                    <td>{p.ceiling ?? '—'}</td>
                    <td>{p.effective ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>

        {/* ── 2. Active tools (session) ───────────────────────────────────── */}
        <section className="session-sidebar-section">
          <h4>Active Tools</h4>
          {!config ? (
            <p className="session-sidebar-empty">Session config not loaded yet.</p>
          ) : effectiveTools.length === 0 ? (
            <p className="session-sidebar-empty">No tools enabled for this session.</p>
          ) : (
            <ul className="session-sidebar-tools">
              {effectiveTools.map((t) => (
                <li key={t}>
                  <label className="session-sidebar-tool-row">
                    <input
                      type="checkbox"
                      checked={effectiveTools.includes(t)}
                      onChange={() => toggleTool(t)}
                    />
                    <span>{t}</span>
                  </label>
                </li>
              ))}
            </ul>
          )}
        </section>

        {/* ── 3. Workers ──────────────────────────────────────────────────── */}
        <section className="session-sidebar-section">
          <h4>Workers</h4>
          {wsLoading && wsMatch ? (
            <p className="session-sidebar-empty">Loading workers...</p>
          ) : workers.length === 0 ? (
            <p className="session-sidebar-empty">No workers configured for this workspace.</p>
          ) : (
            <ul className="session-sidebar-workers">
              {workers.map((w) => {
                const status = w.runtimeStatus || w.runtime_status || 'unknown'
                const color = WORKER_STATUS_COLORS[status] || '#6c7086'
                return (
                  <li key={w.name} className="session-sidebar-worker">
                    <span className="session-sidebar-dot" style={{ background: color }} />
                    <div className="session-sidebar-worker-info">
                      <span className="session-sidebar-worker-name">{w.name}</span>
                      <span className="session-sidebar-worker-status">{status}</span>
                    </div>
                    <button className="session-sidebar-stop-btn" onClick={() => handleStopWorker(w.name)}>
                      Stop
                    </button>
                  </li>
                )
              })}
            </ul>
          )}
          {stopError && <p className="session-sidebar-error">{stopError}</p>}
        </section>

        {/* ── 4. Containers (tm-resource-* excluded) ──────────────────────── */}
        <section className="session-sidebar-section">
          <h4>Containers</h4>
          {containers.length === 0 ? (
            <p className="session-sidebar-empty">No containers for this workspace.</p>
          ) : (
            <ul className="session-sidebar-containers">
              {containers.map((c) => {
                const status = (containerStatus && containerStatus[c.name]) || c.status || 'unknown'
                const running = isRunningStatus(status)
                const busy = !!(busyContainers && busyContainers[c.name])
                return (
                  <li key={c.name} className="session-sidebar-container">
                    <span className={`session-sidebar-dot ${running ? 'session-sidebar-dot-green' : 'session-sidebar-dot-red'}`} />
                    <div className="session-sidebar-container-info">
                      <span className="session-sidebar-container-name">{c.name}</span>
                      <span className="session-sidebar-container-status">{status}</span>
                    </div>
                    <button
                      className="session-sidebar-ctl-btn"
                      disabled={busy}
                      onClick={() => runContainerAction(c.name, running ? 'stop' : 'start')}
                    >
                      {busy ? '…' : running ? 'Stop' : 'Start'}
                    </button>
                  </li>
                )
              })}
            </ul>
          )}
          {containerError && <p className="session-sidebar-error">{containerError}</p>}
        </section>
      </div>
    </div>
  )
}
