import React, { useCallback, useEffect, useRef, useState } from 'react'

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

const DRIFT_UNVERIFIED_TEXT = 'Drift not verified for this container.'
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

// The permission BADGE's `net` / `fs` parts describe the RUNNING container: the
// backend attaches `live_envelope` (derived from the live Docker inspect) to
// every entry and, when present, it wins. Records that predate that wiring, or
// live containers we could not inspect, fall back to the record envelope via
// `permissionsOf`.
//
// The `mem` part is DIFFERENT and always reads the RECORD's intent -- that is the
// limit the container was created with. `live_envelope.mem_limit` lags a
// recreate, so sourcing it here is what put two contradictory "mem" numbers on
// one row mid-recreate. Live-vs-intent divergence is the drift banner's job (it
// already reports it); it is not a badge value.
function badgeSourceOf(entry) {
  const live = entry.live_envelope
  if (live && typeof live === 'object') return live
  return permissionsOf(entry)
}

// `perms` drives the net/fs parts (live first); `intent` (the record envelope)
// supplies the mem figure unconditionally -- see the note above.
function badgeText(perms, intent) {
  const parts = []
  if (perms) {
    if ('network' in perms) parts.push(`net ${perms.network ? 'on' : 'off'}`)
    if ('filesystem' in perms) parts.push(`fs ${perms.filesystem}`)
  }
  if (intent && intent.mem_limit != null) parts.push(`mem ${intent.mem_limit}`)
  return parts.join(' · ')
}

// The input stages a NEW limit while the badge already shows the record's current
// one, so name both numbers instead of a bare "mem" that reads as a second,
// contradictory value.
function memFieldLabel(entry) {
  const perms = permissionsOf(entry)
  const current = perms && perms.mem_limit != null ? String(perms.mem_limit) : null
  return current ? `New limit (currently ${current})` : 'New limit'
}

// Drift notice precedence (highest first); `null` means the row renders NO drift
// element at all. Derived from the entry alone so it is unit-testable without
// mounting the panel:
//   1. `unavailable_reason` set (the live container's isolation is MORE
//      permissive than this session's grant) -> red, body names the reason
//   2. non-empty `permission_drift` array    -> neutral, body names the count
//   3. `permission_drift === null` (unknown) -> neutral, "not verified"
//   4. otherwise                             -> null (silent)
// `unavailable_reason` OUTRANKS the other branches: it is the only signal that
// the running container is actively outside the current grant.
// NOTE: branch 1 is LATENT -- `unavailable_reason` is session-scoped. It is
// produced only by `ContainerManager.list_containers()` when that manager was
// built with a `session_id` (persistent rows) and by `ContainerStatusTool`.
// The containers view builds its manager with `session_id=None`
// (`web_ui/backend/server.py:_make_container_manager`) and never sets the key,
// so the red notice cannot fire until that route can compare against a session.
export function driftDecision(entry) {
  const reason = entry.unavailable_reason
  if (typeof reason === 'string' && reason.trim() !== '') {
    return { severity: 'red', body: `${reason.trim()} Rebuild to apply the current permissions.` }
  }
  const drift = entry.permission_drift
  if (Array.isArray(drift) && drift.length > 0) {
    return {
      severity: 'neutral',
      body: `Container configuration has drifted from its recorded intent (${drift.length} item(s)). Restart to rebuild.`,
    }
  }
  if (entry.permission_drift === null) {
    return { severity: 'neutral', body: DRIFT_UNVERIFIED_TEXT }
  }
  return null
}

//: Post-action reconciliation. A successful mutating action prefers the state
//: the server just returned to update the row in place; when that state is
//: missing or stale (the record write can land AFTER the HTTP response), the
//: panel re-reads the list instead -- up to 3 extra reads, 500ms apart, until the
//: row agrees with the action. There is no permanent poller: the retries stop as
//: soon as the row matches, or when the budget is spent.
const REFRESH_RETRY_DELAY_MS = 500
const REFRESH_RETRY_ATTEMPTS = 3

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

// The action / resources routes answer with the post-action container-view entry
// (`_container_view_entry(record_after, manager, workspace_id)`) -- a FLAT entry
// `{id, ..., kind, state, intent_snapshot, permissions, live, ...}`. `remove`
// answers `{id, removed: true}` instead, which is deliberately NOT recognised
// here: there is no row left to patch.
function postActionEntry(body) {
  if (!body || typeof body !== 'object' || Array.isArray(body)) return null
  if (typeof body.id !== 'string' || body.id === '') return null
  if (typeof body.kind !== 'string' && typeof body.state !== 'string') return null
  return body
}

//: That entry is rendered WITHOUT a live inspect, so `live` and `live_envelope`
//: always arrive `null` in a success body. Copying those over the row would make
//: every successful action claim "no live container", so the list's live markers
//: win whenever the response has nothing to say about them.
const POST_ACTION_NULLABLE_KEYS = ['live', 'live_envelope']

function mergePostActionEntry(prev, next) {
  const merged = { ...prev, ...next }
  for (const key of POST_ACTION_NULLABLE_KEYS) {
    if (next[key] == null && prev[key] != null) merged[key] = prev[key]
  }
  return merged
}

// The record's mem_limit as the row displays it (permissions, else intent).
function memLimitOf(entry) {
  const perms = entry ? permissionsOf(entry) : null
  return perms && perms.mem_limit != null ? String(perms.mem_limit) : null
}

// What the row must look like once the action has settled. A resize is gated on
// the value the operator ASKED for (the route persists exactly that into the
// intent snapshot); a lifecycle action is gated on the state the response body
// reports. `null` means "unknown -- do not gate on it".
function actionExpectation(action, body, value) {
  if (action === 'remove') return { removed: true, state: null, mem_limit: null }
  const posted = postActionEntry(body)
  if (action === 'resources') {
    const requested = value != null && String(value) !== '' ? String(value) : null
    return { removed: false, state: null, mem_limit: requested || memLimitOf(posted) }
  }
  return {
    removed: false,
    state: posted && typeof posted.state === 'string' ? posted.state : null,
    mem_limit: null,
  }
}

function rowSatisfies(row, expected) {
  if (expected.removed) return !row
  if (!row) return false
  if (expected.state != null && row.state !== expected.state) return false
  if (expected.mem_limit != null && memLimitOf(row) !== expected.mem_limit) return false
  return true
}

function rowsSatisfy(rows, id, expected) {
  if (!Array.isArray(rows)) return false
  return rowSatisfies(rows.find((e) => e.id === id), expected)
}

function ContainerRow({ entry, memValue, onMemChange, onAction }) {
  const id = entry.id
  const state = entry.state
  const perms = badgeSourceOf(entry)
  const badge = badgeText(perms, permissionsOf(entry))
  const drift = driftDecision(entry)
  // A record that predates the permission snapshot (`permissions === null`)
  // keeps the historical "not yet wired" neutral copy; a red notice still wins.
  const driftBody =
    drift && drift.severity !== 'red' && entry.permissions == null
      ? DRIFT_NEUTRAL_TITLE
      : drift && drift.body
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

        {badge ? (
          <span
            className="permission-badge"
            data-testid={`permission-badge-${id}`}
            title={badge}
          >
            {badge}
          </span>
        ) : null}

        {driftBody ? (
          <span
            className={`container-drift container-drift--${drift.severity}`}
            data-testid={`container-drift-${id}`}
            data-drift={drift.severity}
            title={drift.severity === 'red' ? undefined : driftBody}
          >
            {driftBody}
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
          <span className="container-resources-label">{memFieldLabel(entry)}</span>
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
  // Abandons an in-flight reconcile loop when the panel unmounts: the retries
  // outlive the click that started them and must not keep fetching (or write
  // state) after teardown.
  const aliveRef = useRef(true)
  useEffect(() => {
    aliveRef.current = true
    return () => {
      aliveRef.current = false
    }
  }, [])

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

  // Returns the rows it just read (or null on failure) so the post-action
  // reconcile loop can decide whether another read is needed.
  const refresh = useCallback(async () => {
    try {
      const all = await load()
      setEntries(all)
      setError(null)
      return all
    } catch (err) {
      setError(err && err.message ? err.message : String(err))
      return null
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
        // The routes answer a failure with `{"error": <message>}` (server.py's
        // `_json_error`), NOT `{detail}` -- reading only `body.detail` is what
        // collapsed every 4xx into a bare `Action failed (400)` and hid the
        // reason (e.g. "mem_limit '4m' (4194304 bytes) is below the Docker
        // minimum of 6MB (6291456 bytes); use e.g. '6m' or '64m'"). The only
        // response that also carries a `code` is the permission gate's
        // hand-rolled 403 (`{"error": ..., "code": "permission_denied"}`).
        // `detail` stays as the fallback because a FastAPI-level validation
        // error still uses it.
        const message = body.error || body.detail || `Action failed (${res.status})`
        // Re-read server state even when the action failed: a 404 means the
        // record is already gone, so the row that caused it must be dropped
        // from the list -- otherwise every later click on it 404s again and
        // the row is an unrecoverable dead end on screen. `refresh()` runs
        // FIRST and the error is surfaced after it, because a successful
        // reload clears `error` (re-setting it afterwards keeps the message
        // visible exactly as before).
        await refresh()
        setError(message)
        return
      }
      // ── Success ─────────────────────────────────────────────────────────────
      // Prefer the state the server just returned: it is authoritative and saves
      // a round trip. It arrives as a container-view entry rendered WITHOUT a
      // live inspect (`live` / `live_envelope` are null), so it is MERGED over
      // the row rather than replacing it.
      const body = await res.json().catch(() => null)
      const posted = postActionEntry(body)
      if (posted) {
        setEntries((prev) =>
          prev.map((e) => (e.id === posted.id ? mergePostActionEntry(e, posted) : e))
        )
      }
      const expected = actionExpectation(action, body, value)
      if (posted && rowSatisfies(posted, expected)) return
      // The body was missing or stale (the record write can land AFTER the HTTP
      // response). Re-read, retrying briefly, until the row agrees with the
      // action -- this is the F5 the operator used to have to press.
      let rows = await refresh()
      for (let attempt = 0; attempt < REFRESH_RETRY_ATTEMPTS; attempt += 1) {
        if (!aliveRef.current) return
        if (rowsSatisfy(rows, entry.id, expected)) return
        await sleep(REFRESH_RETRY_DELAY_MS)
        if (!aliveRef.current) return
        rows = await refresh()
      }
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
