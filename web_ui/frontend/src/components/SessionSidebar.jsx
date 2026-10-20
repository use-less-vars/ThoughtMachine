import React, { useCallback, useEffect, useState } from 'react'
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

// container is the one boolean session resource; render its raw boolean as a
// display label. Component-local on purpose — the store keeps booleans.
const renderPermissionValue = (value) =>
  typeof value === 'boolean' ? (value ? 'Enabled' : 'Disabled') : (value ?? '—')

// ── Ceiling-scale rank (mirror of the backend comparison) ────────────────
// The ceiling-vs-grant comparison uses the rank table that
// security/security_gate.py apply_workspace_ceiling actually caps with
// (security/resource_catalog.py WORKSPACE_CEILING_LEVELS_RANKS):
//   banned(0) < read(1) == connect(1) == none(1) < ask(2)
//       < write_on_feature_branch(2.5) < write(3) < outbound(4) == full(4)
// Deliberately NOT permissionVocab.PERMISSION_RANK_ORDER: that is the *grant*
// scale (_min_permission), where read < ask and write_on_feature_branch ties
// write — it cannot order a ceiling (a write_on_feature_branch ceiling must sit
// strictly between ask and write, and outbound must rank as unlimited).
const CEILING_RANK = {
  banned: 0,
  read: 1,
  connect: 1,
  none: 1,
  ask: 2,
  write_on_feature_branch: 2.5,
  write: 3,
  outbound: 4,
  full: 4,
}

// host_bash ranks on its OWN scale — banned(0) < ask(1) < allow(2) — not the
// generic table above ('allow' is absent there and 'ask' means something
// else); mirror of security/security_gate.py _HOST_BASH_CEILING_RANKS.
const HOST_BASH_CEILING_RANK = { banned: 0, ask: 1, allow: 2 }

// Rank a ceiling OR session-grant value on `name`'s ceiling scale; null when
// the value is not on that key's scale (rank undecidable). Mirrors
// security/security_gate.py _ceiling_scale_rank. container is boolean.
const ceilingScaleRank = (name, value) => {
  if (typeof value === 'boolean') {
    return name === 'host_bash' ? (value ? 2 : 0) : (value ? 3 : 0)
  }
  const label = String(value).toLowerCase()
  if (label === 'true' || label === 'false') {
    const asBool = label === 'true'
    return name === 'host_bash' ? (asBool ? 2 : 0) : (asBool ? 3 : 0)
  }
  const table = name === 'host_bash' ? HOST_BASH_CEILING_RANK : CEILING_RANK
  const rank = table[label]
  return rank === undefined ? null : rank
}

// "Cap only" marker condition. NOTE: the brief's original phrasing said
// "weaker than the session default"; the mechanism-true condition is
// "ceiling above the grant" — the workspace cap is MORE PERMISSIVE than what
// the session itself grants, so the cap alone grants nothing and the session
// grant has to be raised to enable the capability. A STRICTER ceiling (cap
// below the grant) and an EQUAL one show no marker: those are real caps, not
// an inert ceiling. Undecidable ranks (unknown value, absent ceiling, absent
// session grant) never render the marker.
const isCapOnly = (name, ceiling, grant) => {
  if (ceiling === undefined || ceiling === null) return false
  if (grant === undefined || grant === null) return false
  const ceilingRank = ceilingScaleRank(name, ceiling)
  const grantRank = ceilingScaleRank(name, grant)
  if (ceilingRank === null || grantRank === null) return false
  return ceilingRank > grantRank
}

/**
 * Slide-out session details panel (toggled from the SessionTab header).
 * Four read-only/action sections:
 *  1. Permissions  — workspace ceiling + session grant + effective (read-only)
 *  2. Active Tools — session tools checkboxes, applied via apply_config
 *  3. Workers      — runtime status per worker + Stop action
 *  4. Containers   — workspace containers (kind==='resource' excluded) + Start/Stop
 */
export default function SessionSidebar({ workspaceId, config, tools, sendCommand, onClose }) {
  const currentWorkspace = useWorkspaceStore((s) => s.currentWorkspace)
  const containerStatus = useWorkspaceStore((s) => s.containerStatus)
  const busyContainers = useWorkspaceStore((s) => s.busyContainers)
  const wsLoading = useWorkspaceStore((s) => s.isLoading)

  const [toolsOverride, setToolsOverride] = useState(null)
  const [stopError, setStopError] = useState(null)
  const [containerError, setContainerError] = useState(null)
  const [activeWorkers, setActiveWorkers] = useState([])
  const [workersError, setWorkersError] = useState(null)

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

  // Fetch the LIVE active workers (running instances) for this workspace.
  const fetchActiveWorkers = useCallback(async () => {
    if (!workspaceId) return
    try {
      setWorkersError(null)
      const res = await fetch(
        `/api/workspace/${encodeURIComponent(workspaceId)}/workers/active`
      )
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      const list = Array.isArray(data) ? data : []
      setActiveWorkers(
        list.map((e) => ({
          name: e.worker_name ?? e.name,
          instance_id: e.instance_id ?? null,
          status: e.status ?? 'unknown',
        }))
      )
    } catch (err) {
      setWorkersError(err.message || String(err))
      setActiveWorkers([])
    }
  }, [workspaceId])

  // Poll the active workers while the panel is open.
  useEffect(() => {
    fetchActiveWorkers()
    const interval = setInterval(fetchActiveWorkers, 3000)
    return () => clearInterval(interval)
  }, [fetchActiveWorkers])

  // Keep the optimistic tools override in sync once the backend confirms.
  useEffect(() => {
    setToolsOverride(null)
  }, [tools])

  const wsMatch = currentWorkspace?.id === workspaceId
  const permissions = wsMatch ? (currentWorkspace.permissions || []) : []
  // Whether the store resolved these permissions for an ACTIVE session. When
  // false, the panel must not present the backend's default response as
  // authoritative (see fetchWorkspaceConfig in store/workspaceStore.js).
  const permissionsSessionScoped = wsMatch && !!currentWorkspace.permissionsSessionScoped
  // Session permission values that exceed their workspace ceiling, as reported
  // by the backend (carried on the workspace by the store). [] when none.
  const permissionContradictions = wsMatch ? (currentWorkspace.permissionContradictions || []) : []
  // The session's OWN uncapped grant map, threaded through from the GET
  // /api/session/{id}/permissions envelope key `raw`. Never recomputed
  // client-side from the effective values (those are already ceiling-capped).
  const sessionGrants = wsMatch ? (currentWorkspace.sessionPermissionGrants || {}) : {}
  const workers = wsMatch ? activeWorkers : []
  const containers = (wsMatch ? (currentWorkspace.containers || []) : [])
    .filter((c) => c.kind !== 'resource')

  const effectiveTools = toolsOverride ?? (Array.isArray(tools) ? tools : [])

  const toggleTool = (name) => {
    if (!config) return
    const next = effectiveTools.includes(name)
      ? effectiveTools.filter((t) => t !== name)
      : [...effectiveTools, name]
    setToolsOverride(next)
    sendCommand('apply_config', { config: { ...config, tools: next } })
  }

  const handleStopWorker = async (name, instanceId) => {
    if (!workspaceId) return
    try {
      setStopError(null)
      const instanceQuery = instanceId != null ? `?instance_id=${instanceId}` : ''
      const res = await fetch(
        `/api/workspace/${encodeURIComponent(workspaceId)}/workers/${encodeURIComponent(name)}/stop${instanceQuery}`,
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
          {wsMatch && !wsLoading && !permissionsSessionScoped && (
            <p className="session-sidebar-hint">No active session — showing defaults</p>
          )}
          {wsMatch && !wsLoading && permissionContradictions.length > 0 && (
            <ul className="session-sidebar-hint" data-testid="permission-contradictions">
              {permissionContradictions.map((c) => (
                <li key={c.resource}>
                  {typeof c.reason === 'string' && c.reason
                    ? c.reason
                    : `session ${renderPermissionValue(c.session_value)} exceeds ceiling ${renderPermissionValue(c.workspace_value)}`}
                </li>
              ))}
            </ul>
          )}
          {wsLoading && wsMatch ? (
            <p className="session-sidebar-empty">Loading permissions...</p>
          ) : permissions.length === 0 ? (
            <p className="session-sidebar-empty">No permission data for this workspace.</p>
          ) : (
            <table className="session-sidebar-table">
              <thead>
                <tr>
                  <th>Permission</th>
                  <th>Ceiling (workspace max — cap, cannot grant)</th>
                  <th>Session grant (your choice within the cap)</th>
                  <th>Effective (what the agent actually has)</th>
                </tr>
              </thead>
              <tbody>
                {permissions.map((p) => {
                  const grant = sessionGrants[p.name]
                  return (
                    <tr key={p.name}>
                      <td>{p.name}</td>
                      <td>
                        {renderPermissionValue(p.ceiling)}
                        {isCapOnly(p.name, p.ceiling, grant) && (
                          <span
                            className="session-sidebar-cap-only"
                            data-testid={`cap-only-${p.name}`}
                          >{' '}cap only — will not grant; set the session grant to enable.</span>
                        )}
                      </td>
                      <td>{renderPermissionValue(grant)}</td>
                      <td>{renderPermissionValue(p.effective)}</td>
                    </tr>
                  )
                })}
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
            <p className="session-sidebar-empty">No active workers in this workspace.</p>
          ) : (
            <ul className="session-sidebar-workers">
              {workers.map((w) => {
                const status = w.status || 'unknown'
                const color = WORKER_STATUS_COLORS[status] || '#6c7086'
                return (
                  <li key={w.name} className="session-sidebar-worker">
                    <span className="session-sidebar-dot" style={{ background: color }} />
                    <div className="session-sidebar-worker-info">
                      <span className="session-sidebar-worker-name">{w.name}</span>
                      <span className="session-sidebar-worker-status">{status}</span>
                    </div>
                    <button className="session-sidebar-stop-btn" onClick={() => handleStopWorker(w.name, w.instance_id)}>
                      Stop
                    </button>
                  </li>
                )
              })}
            </ul>
          )}
          {stopError && <p className="session-sidebar-error">{stopError}</p>}
          {workersError && <p className="session-sidebar-error">{workersError}</p>}
        </section>

        {/* ── 4. Containers (kind==='resource' excluded) ──────────────────────── */}
        <section className="session-sidebar-section">
          <h4>Containers</h4>
          {containers.length === 0 ? (
            <p className="session-sidebar-empty">No containers for this workspace.</p>
          ) : (
            <ul className="session-sidebar-containers">
              {containers.map((c) => {
                const status = (containerStatus && containerStatus[c.name]) || c.state || 'unknown'
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
