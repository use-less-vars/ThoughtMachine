import React, { useCallback, useEffect, useState } from 'react'

// ── Copy pinned by the C.1 contract ──────────────────────────────────────
const SHARED_TITLE = 'Stopping affects other sessions in this workspace.'

// Verb-correct shared line for the confirm dialog. The `stop` string is the
// C.1-pinned copy (== SHARED_TITLE); the other verbs are derived from the same
// `<Gerund> affects other sessions in this workspace.` template so a start or
// restart prompt never claims "Stopping".
const SHARED_ACTIONS = {
  stop: 'Stopping',
  start: 'Starting',
  restart: 'Restarting',
  resize: 'Resizing',
  remove: 'Removing',
}
const SHARED_SUFFIX = 'affects other sessions in this workspace.'
function sharedLineFor(action) {
  const gerund = SHARED_ACTIONS[action] || SHARED_ACTIONS.stop
  return `${gerund} ${SHARED_SUFFIX}`
}

//: Fixed actor id for the record-keyed write actions (matches
//: RecordContainerPanel). The backend `_resolve_actor` requires a non-empty
//: body `actor` OR an `X-Actor` header, else the request 401s before any
//: handler runs. Both are sent so either resolution path succeeds.
const ACTOR = 'operator'

const DRIFT_AMBER_TEXT = 'Running under older permissions — restart to apply.'
const DRIFT_NEUTRAL_TITLE =
  'Drift check not yet wired for this container — see backend/record-create-permission-snapshot.'

const EPHEMERAL_EMPTY = 'No ephemeral containers.'
const PERSISTENT_EMPTY = 'No persistent containers.'
const RESOURCE_EMPTY = 'No resource containers.'

const ACTION_LABELS = {
  stop: 'Stop',
  start: 'Start',
  restart: 'Restart',
  remove: 'Remove',
  resources: 'Resize',
}

// Which lifecycle controls a row exposes for a given state. `remove` is added
// separately by kind: ephemeral rows and persistent rows both offer it, which
// mirrors the route's ALLOW-list (it reaps `ephemeral` and `persistent` and
// refuses every other class before any Docker verb). The panel attributes
// `actor=operator` on every request -- that token is attribution, not
// authentication; the route's gate, not this UI, is what authorises the call.
// Resource containers stay operator-managed outside this panel, so a resource
// row never offers `remove`.
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

// The permission BADGE must describe the RUNNING container, not the record's
// frozen creation envelope. The backend attaches `live_envelope` (derived from
// the live Docker inspect) to every entry; when present it wins. Records that
// predate that wiring, or live containers we could not inspect, fall back to
// the record envelope via `permissionsOf`.
function badgeSourceOf(entry) {
  const live = entry.live_envelope
  if (live && typeof live === 'object') return live
  return permissionsOf(entry)
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
  const perms = badgeSourceOf(entry)
  const drift = driftState(entry)
  const isOom = state === 'oom'
  const noteText = typeof entry.note === 'string' ? entry.note.trim() : ''

  const actions = lifecycleActions(state)
  actions.push('resources')
  // Persistent containers are removable from this panel: the route's allow-list
  // covers `ephemeral` and `persistent` and refuses every other class, and the
  // panel only ever *attributes* `actor=operator` (attribution is not
  // authentication -- the backend gate decides). The container's note is
  // surfaced in the confirm copy so whoever else is using it can see whose work
  // it was. Resource rows stay operator-managed outside this panel.
  if (entry.kind === 'ephemeral' || entry.kind === 'persistent') actions.push('remove')

  return (
    <div
      className={entry.live === false ? 'container-row container-row--missing' : 'container-row'}
      data-testid={`container-row-${id}`}
      style={entry.live === false ? { opacity: 0.5 } : undefined}
    >
      <div className="container-row-main">
        <span className="container-row-name" title={entry.name}>{entry.name}</span>

        <span
          data-testid={`container-state-${id}`}
          className={isOom ? 'container-state-chip container-state-chip--oom' : 'container-state-chip'}
        >
          {state}
        </span>

        {entry.live === false ? (
          <span
            className="container-missing-badge"
            data-testid={`container-missing-${id}`}
            title="No live container for this record — it is not running."
          >
            no live container
          </span>
        ) : null}

        {entry.shared ? (
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

        {noteText ? (
          <span
            className="container-row-note"
            data-testid={`container-note-${id}`}
            title={noteText}
            style={{
              display: 'block',
              maxWidth: '100%',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {noteText}
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
  const [showArchived, setShowArchived] = useState(false)
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
    // PC3: a single FLAT `containers` list; the legacy session/workspace
    // split is gone and the UI groups entries by `kind`.
    const arr = Array.isArray(data && data.containers) ? data.containers : []
    return arr
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
      const headers = { 'Content-Type': 'application/json', 'X-Actor': ACTOR }
      const res =
        action === 'resources'
          ? await fetch(`${base}/resources`, {
              method: 'PATCH',
              headers,
              body: JSON.stringify({ mem_limit: value, actor: ACTOR }),
            })
          : await fetch(`${base}/action`, {
              method: 'POST',
              headers,
              body: JSON.stringify({ action, actor: ACTOR }),
            })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        setError(body.detail || `Action failed (${res.status})`)
        return
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

  // Dead records (no live Docker container) are hidden from the live view by
  // default; the "Show archived" toggle reveals them. This is display-only —
  // nothing here sweeps or deletes a record.
  const visible = (e) => showArchived || e.live !== false

  const ephemeral = entries.filter((e) => e.kind === 'ephemeral' && visible(e))
  const persistent = entries.filter((e) => e.kind === 'persistent' && visible(e))
  const resource = entries.filter((e) => e.kind === 'resource' && visible(e))

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
    // Removing a persistent container deletes workspace property, so the actor
    // is shown the note its previous user left — the coordination mechanism.
    const note = typeof entry.note === 'string' ? entry.note.trim() : ''
    const noteLine =
      action === 'remove' && entry.kind === 'persistent' && note
        ? ` Its note reads: "${note}".`
        : ''
    if (entry.shared) return `${base}${noteLine} ${sharedLineFor(verb)}`
    return `${base}${noteLine}`
  }

  return (
    <div className="container-list-panel">
      {error ? (
        <div className="container-list-error" role="alert">
          {error}
        </div>
      ) : null}

      <div className="container-list-toolbar">
        <label className="container-show-archived">
          <input
            type="checkbox"
            data-testid="container-show-archived"
            checked={showArchived}
            onChange={(e) => setShowArchived(e.target.checked)}
          />
          <span>Show archived</span>
        </label>
      </div>

      {renderGroup('ephemeral', 'container-group-ephemeral', 'Ephemeral containers', ephemeral, EPHEMERAL_EMPTY)}
      {renderGroup('persistent', 'container-group-persistent', 'Persistent containers', persistent, PERSISTENT_EMPTY)}
      {renderGroup('resource', 'container-group-resource', 'Resource containers', resource, RESOURCE_EMPTY)}

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
