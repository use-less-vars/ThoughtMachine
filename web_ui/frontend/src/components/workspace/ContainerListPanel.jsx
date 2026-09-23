import React, { useCallback, useEffect, useState } from 'react'

// ── Copy pinned by the C.1 contract ──────────────────────────────────────
const SHARED_TITLE = 'Stopping affects other sessions in this workspace.'
const DRIFT_AMBER_TEXT = 'Running under older permissions — restart to apply.'
const DRIFT_NEUTRAL_TITLE =
  'Drift check not yet wired for this container — see backend/record-create-permission-snapshot.'

const EPHEMERAL_EMPTY = 'No ephemeral containers in this workspace.'
const RUNTIME_EMPTY = 'No runtime resources in use.'

const ACTION_LABELS = {
  stop: 'Stop',
  start: 'Start',
  restart: 'Restart',
  remove: 'Remove',
  resources: 'Resize',
}

// Which lifecycle controls a row exposes for a given state. `remove` is added
// separately for ephemeral rows only (the server refuses runtime removal).
function lifecycleActions(state) {
  if (state === 'running') return ['stop', 'restart']
  if (state === 'paused') return ['start', 'restart']
  return ['start']
}

// Primary source is the live `permissions` dict; `intent_snapshot` is the
// fallback for entries that predate the permission snapshot wiring.
function permissionsOf(entry) {
  if (entry.permissions && typeof entry.permissions === 'object') return entry.permissions
  if (entry.intent_snapshot && typeof entry.intent_snapshot === 'object') return entry.intent_snapshot
  return null
}

function badgeText(perms) {
  if (!perms) return ''
  const parts = []
  if ('network' in perms) parts.push(`net ${perms.network ? 'on' : 'off'}`)
  if ('filesystem' in perms) parts.push(`fs ${perms.filesystem}`)
  if (perms.mem_limit != null) parts.push(`mem ${perms.mem_limit}`)
  return parts.join(' · ')
}

// Drift indicator is 3-state:
//   * non-empty `permission_drift` array  -> amber ("running under older ...")
//   * `permissions === null`              -> neutral ("not yet wired")
//   * otherwise (permissions present, no drift) -> silent (no element)
function driftState(entry) {
  const drift = entry.permission_drift
  if (Array.isArray(drift) && drift.length > 0) return 'amber'
  if (entry.permissions == null) return 'neutral'
  return 'silent'
}

function ContainerRow({ entry, memValue, onMemChange, onAction }) {
  const id = entry.id
  const state = entry.state
  const perms = permissionsOf(entry)
  const drift = driftState(entry)
  const isOom = state === 'oom'

  const actions = lifecycleActions(state)
  actions.push('resources')
  if (entry.kind === 'ephemeral') actions.push('remove')

  return (
    <div className="container-row" data-testid={`container-row-${id}`}>
      <div className="container-row-main">
        <span className="container-row-name" title={entry.name}>{entry.name}</span>

        <span
          data-testid={`container-state-${id}`}
          className={isOom ? 'container-state-chip container-state-chip--oom' : 'container-state-chip'}
        >
          {state}
        </span>

        {entry.kind === 'runtime' && entry.shared ? (
          <span
            className="container-shared-chip"
            data-testid={`container-shared-${id}`}
            title={SHARED_TITLE}
          >
            shared
          </span>
        ) : null}

        {perms ? (
          <span
            className="permission-badge"
            data-testid={`permission-badge-${id}`}
            title={badgeText(perms)}
          >
            {badgeText(perms)}
          </span>
        ) : null}

        {drift === 'amber' ? (
          <span
            className="container-drift container-drift--amber"
            data-testid={`container-drift-${id}`}
            data-drift="amber"
          >
            {DRIFT_AMBER_TEXT}
          </span>
        ) : null}

        {drift === 'neutral' ? (
          <span
            className="container-drift container-drift--neutral"
            data-testid={`container-drift-${id}`}
            data-drift="neutral"
            title={DRIFT_NEUTRAL_TITLE}
          >
            {DRIFT_NEUTRAL_TITLE}
          </span>
        ) : null}
      </div>

      <div className="container-row-controls">
        <label className="container-resources-field">
          <span className="container-resources-label">mem</span>
          <input
            type="text"
            className="container-resources-mem"
            data-testid={`container-resources-mem-${id}`}
            value={memValue}
            placeholder="512m"
            aria-label={`Memory limit for ${entry.name}`}
            onChange={(e) => onMemChange(id, e.target.value)}
          />
        </label>

        {actions.map((action) => (
          <button
            key={action}
            type="button"
            className="container-action"
            data-testid={`container-action-${id}-${action}`}
            onClick={() => onAction(entry, action)}
          >
            {ACTION_LABELS[action]}
          </button>
        ))}
      </div>
    </div>
  )
}

export default function ContainerListPanel({ workspaceId }) {
  const [entries, setEntries] = useState([])
  const [error, setError] = useState(null)
  const [pending, setPending] = useState(null) // { entry, action, value }
  const [memEdits, setMemEdits] = useState({})

  // Inline fetch boundary. Guards mirror the backend envelope: a non-2xx
  // response or an explicit success:false is an error, and missing arrays are
  // tolerated as empty.
  const load = useCallback(async () => {
    const res = await fetch(`/api/workspace/${workspaceId}/containers`)
    if (!res.ok) throw new Error(`Failed to load containers (${res.status})`)
    const data = await res.json()
    if (data && data.success === false) {
      throw new Error(data.error || 'Failed to load containers')
    }
    const sessionArr = Array.isArray(data && data.session) ? data.session : []
    const workspaceArr = Array.isArray(data && data.workspace) ? data.workspace : []
    return [...sessionArr, ...workspaceArr]
  }, [workspaceId])

  useEffect(() => {
    let cancelled = false
    load()
      .then((all) => {
        if (cancelled) return
        setEntries(all)
        setError(null)
      })
      .catch((err) => {
        if (cancelled) return
        setError(err && err.message ? err.message : String(err))
      })
    return () => {
      cancelled = true
    }
  }, [load])

  const refresh = useCallback(async () => {
    try {
      const all = await load()
      setEntries(all)
      setError(null)
    } catch (err) {
      setError(err && err.message ? err.message : String(err))
    }
  }, [load])

  const memValueFor = (entry) => {
    if (Object.prototype.hasOwnProperty.call(memEdits, entry.id)) return memEdits[entry.id]
    const perms = permissionsOf(entry)
    return perms && perms.mem_limit != null ? String(perms.mem_limit) : ''
  }

  const handleMemChange = (id, value) => {
    setMemEdits((prev) => ({ ...prev, [id]: value }))
  }

  // Opening any mutating control only stages a pending action; no HTTP is
  // dispatched until the blocking confirm modal is accepted.
  const requestAction = (entry, action) => {
    setPending({ entry, action, value: memValueFor(entry) })
  }

  const dispatch = useCallback(
    async ({ entry, action, value }) => {
      const base = `/api/workspace/${workspaceId}/containers/${entry.id}`
      if (action === 'resources') {
        await fetch(`${base}/resources`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ mem_limit: value }),
        })
      } else {
        await fetch(`${base}/action`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action }),
        })
      }
      await refresh()
    },
    [workspaceId, refresh]
  )

  const confirmOk = () => {
    const task = pending
    setPending(null)
    if (task) dispatch(task)
  }

  const confirmCancel = () => setPending(null)

  const ephemeral = entries.filter((e) => e.kind === 'ephemeral')
  const runtime = entries.filter((e) => e.kind === 'runtime')

  const renderGroup = (kind, testid, title, list, emptyCopy) => (
    <div className={`container-group container-group--${kind}`} data-testid={testid}>
      <div className="container-group-title">{title}</div>
      {list.length === 0 ? (
        <div className="container-empty">{emptyCopy}</div>
      ) : (
        list.map((entry) => (
          <ContainerRow
            key={entry.id}
            entry={entry}
            memValue={memValueFor(entry)}
            onMemChange={handleMemChange}
            onAction={requestAction}
          />
        ))
      )}
    </div>
  )

  const confirmMessage = () => {
    if (!pending) return ''
    const { entry, action } = pending
    const verb = action === 'resources' ? 'resize' : action
    const base = `Are you sure you want to ${verb} ${entry.name}?`
    if (entry.shared) return `${base} ${SHARED_TITLE}`
    return base
  }

  return (
    <div className="container-list-panel">
      {error ? (
        <div className="container-list-error" role="alert">
          {error}
        </div>
      ) : null}

      {renderGroup('ephemeral', 'container-group-ephemeral', 'Ephemeral containers', ephemeral, EPHEMERAL_EMPTY)}
      {renderGroup('runtime', 'container-group-runtime', 'Runtime resources', runtime, RUNTIME_EMPTY)}

      {pending ? (
        <div className="container-confirm-overlay">
          <div className="container-confirm-dialog" data-testid="confirm-dialog" role="dialog" aria-modal="true">
            <div className="container-confirm-message" data-testid="confirm-message">
              {confirmMessage()}
            </div>
            <div className="container-confirm-actions">
              <button type="button" className="btn btn-cancel" data-testid="confirm-cancel" onClick={confirmCancel}>
                Cancel
              </button>
              <button type="button" className="btn btn-run" data-testid="confirm-ok" onClick={confirmOk}>
                Confirm
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  )
}
