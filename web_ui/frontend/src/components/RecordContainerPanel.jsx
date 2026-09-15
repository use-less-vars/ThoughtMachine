// --- RecordContainerPanel.jsx ---
// Read-only container-record panel for the Global Management landing page.
//
// Pass 2a: list view (10s poll + manual "Refresh Records"), record selection,
// a detail view (record fields + live drift) and the selected record's merged
// event log. Pass 2b adds the write half: kill / restart / recreate POST to the
// record-keyed routes (never the name-keyed legacy routes), attributed to a
// fixed `actor` plus an optional free-text `reason`. Success refetches the
// detail AND the list; every failure mode gets its own user-visible message.
//
// Read surface (record-keyed only):
//   GET /api/container-records
//   GET /api/container-records/{record_id}?workspace_id=<ws>
//   GET /api/container-records/{record_id}/events?workspace_id=<ws>
//
// Write surface (record-keyed only; confirmed against server.py):
//   POST /api/container-records/{record_id}/kill?workspace_id=<ws>
//   POST /api/container-records/{record_id}/restart?workspace_id=<ws>
//   POST /api/container-records/{record_id}/recreate?workspace_id=<ws>
//        JSON body {actor, reason?} -- `reason` is OMITTED when empty.
//
// Drift shape (per finding): {drift_class, event_type, expected, actual,
// signature}. `drift === null` means the live state could NOT be inspected
// (Docker unreachable) -- distinct from `[]`, which means "no drift".

import React, { useCallback, useEffect, useState } from 'react'

const LIST_URL = '/api/container-records'
//: List poll cadence (ms).
const POLL_MS = 10000
//: Fixed actor id for the (2b) write actions; read requests are actor-less.
const ACTOR = 'operator'

// ── Small pure helpers ──────────────────────────────────────────────────────

function recordKey(record, index) {
  if (!record || typeof record !== 'object') return `record-${index}`
  return record.container_id || record.id || record.name || `record-${index}`
}

function jsonText(value) {
  try {
    return JSON.stringify(value)
  } catch (err) {
    return String(value)
  }
}

function displayValue(value) {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return jsonText(value)
}

//: Show only the head of a (16-char) drift signature inline; the full value is
//: available as the cell's title and in the expandable raw JSON.
function truncateSignature(signature) {
  if (typeof signature !== 'string' || !signature) return '—'
  return signature.length > 12 ? `${signature.slice(0, 12)}…` : signature
}

//: 'unknown' (drift === null/undefined -> live state unreadable), 'clean' ([])
//: or 'drift' (non-empty findings list).
function driftStatus(drift) {
  if (drift === null || drift === undefined || !Array.isArray(drift)) return 'unknown'
  return drift.length === 0 ? 'clean' : 'drift'
}

function driftLabel(drift) {
  const status = driftStatus(drift)
  if (status === 'unknown') return 'drift: unknown'
  if (status === 'clean') return 'drift: clean'
  return `drift: ${drift.length}`
}

async function readBody(response) {
  try {
    return await response.json()
  } catch (err) {
    return null
  }
}

//: Map one failed read to a distinct {kind, message} pair so every read-failure
//: path (permission denied / actor missing / record gone / Docker unreachable /
//: backend unreachable) reads differently in the UI.
function classifyFailure(response, body) {
  const code = body && typeof body === 'object' ? body.code : null
  const message =
    body && typeof body === 'object' && body.error ? String(body.error) : ''
  const status = response && typeof response.status === 'number' ? response.status : 0

  if (code === 'permission_denied' || status === 403) {
    return {
      kind: 'permission_denied',
      message: `Permission denied: ${
        message || 'this session may not read container records.'
      }`,
    }
  }
  if (
    status === 401 ||
    code === 'actor_required' ||
    code === 'actor_missing' ||
    /\bactor\b/i.test(message)
  ) {
    return {
      kind: 'actor_missing',
      message: `Actor context missing: ${
        message || 'the read was rejected because no actor was supplied.'
      }`,
    }
  }
  if (status === 404) {
    return {
      kind: 'record_gone',
      message: 'Record no longer exists — it may have been pruned or replaced.',
    }
  }
  if (/docker|daemon|cannot connect|connection refused/i.test(message)) {
    return {
      kind: 'docker_unreachable',
      message: `Docker unreachable: ${
        message || 'the live container state could not be inspected.'
      }`,
    }
  }
  if (status === 409) {
    return {
      kind: 'conflict',
      message: `Action conflict: ${
        message || 'the container is not in a state that permits this action.'
      }`,
    }
  }
  if (status === 503) {
    return {
      kind: 'service_unavailable',
      message: `Service unavailable: ${
        message || 'the operation could not be completed on the backend.'
      }`,
    }
  }
  if (status === 400) {
    return {
      kind: 'bad_request',
      message: `Bad request: ${
        message || 'the request was rejected (missing workspace_id?).'
      }`,
    }
  }
  return {
    kind: 'error',
    message: message || `Read failed (HTTP ${status || 'unknown'}).`,
  }
}

function networkFailure(err) {
  return {
    kind: 'backend_unreachable',
    message: `Backend unreachable — ${
      err && err.message ? err.message : 'the API could not be reached.'
    }`,
  }
}

// ── Write-action (pass 2b) helpers ──────────────────────────────────────────

//: The three record-keyed verbs, in display order.
const ACTIONS = ['kill', 'restart', 'recreate']

//: Docker states in which the container is live -- the only ones "kill" targets.
const LIVE_STATES = new Set(['running', 'restarting', 'paused'])
//: Docker states that imply a container object exists (a handle is resolvable).
const EXISTING_STATES = new Set([
  'running',
  'restarting',
  'paused',
  'exited',
  'created',
  'stopped',
])

function actionLabel(action) {
  if (action === 'kill') return 'Kill'
  if (action === 'restart') return 'Restart'
  return 'Recreate'
}

function actionPendingLabel(action) {
  if (action === 'kill') return 'Killing…'
  if (action === 'restart') return 'Restarting…'
  return 'Recreating…'
}

//: Per-action preconditions, derived from the record alone. Every write needs a
//: container `name` (server.py:3510-3511 -> 409 without one); `kill` additionally
//: needs a *live* container (server.py:3548-3550 -> 409 when the handle is
//: missing) while `restart`/`recreate` only need a resolvable handle
//: (server.py:3513-3520 -- a stale `docker_id`, or a live lookup by name).
function actionEnablement(record) {
  // record.state is an open vocabulary; enablement here is a UX hint, not a contract. Server 409 is authoritative.
  const state = record && typeof record.state === 'string' ? record.state : ''
  const hasName = Boolean(record && record.name)
  const hasHandle =
    Boolean(record && record.docker_id) || EXISTING_STATES.has(state)
  return {
    kill: hasName && LIVE_STATES.has(state),
    restart: hasName && hasHandle,
    recreate: hasName && hasHandle,
  }
}

// ── Sub-components ──────────────────────────────────────────────────────────

function FieldRow({ label, value }) {
  return (
    <>
      <dt className="rcp-field-label">{label}</dt>
      <dd className="rcp-field-value">{displayValue(value)}</dd>
    </>
  )
}

function DriftTable({ findings, expandedKey, onToggle }) {
  return (
    <table className="rcp-drift-table">
      <caption className="rcp-drift-caption">
        Live drift findings ({findings.length})
      </caption>
      <thead>
        <tr>
          <th scope="col">drift_class</th>
          <th scope="col">event_type</th>
          <th scope="col">expected</th>
          <th scope="col">actual</th>
          <th scope="col">signature</th>
          <th scope="col">raw</th>
        </tr>
      </thead>
      <tbody>
        {findings.map((finding, index) => {
          const key = `${finding.drift_class}|${finding.event_type}|${finding.signature}|${index}`
          const open = expandedKey === key
          return (
            <React.Fragment key={key}>
              <tr className="rcp-drift-row">
                <td className="rcp-drift-class">{displayValue(finding.drift_class)}</td>
                <td>{displayValue(finding.event_type)}</td>
                <td className="rcp-drift-value">{displayValue(finding.expected)}</td>
                <td className="rcp-drift-value">{displayValue(finding.actual)}</td>
                <td className="rcp-drift-signature" title={displayValue(finding.signature)}>
                  {truncateSignature(finding.signature)}
                </td>
                <td>
                  <button
                    type="button"
                    className="rcp-drift-toggle"
                    aria-expanded={open}
                    aria-label={`${open ? 'Hide' : 'Show'} raw JSON for ${finding.drift_class}`}
                    onClick={() => onToggle(key)}
                  >
                    {open ? '▾' : '▸'}
                  </button>
                </td>
              </tr>
              {open && (
                <tr className="rcp-drift-raw-row">
                  <td colSpan={6}>
                    <code className="rcp-drift-raw">
                      {JSON.stringify(finding, null, 2)}
                    </code>
                  </td>
                </tr>
              )}
            </React.Fragment>
          )
        })}
      </tbody>
    </table>
  )
}

// ── Panel ───────────────────────────────────────────────────────────────────

export default function RecordContainerPanel() {
  const [records, setRecords] = useState(null)
  const [listLoading, setListLoading] = useState(true)
  const [listError, setListError] = useState(null)

  const [selectedId, setSelectedId] = useState(null)
  const [detail, setDetail] = useState(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState(null)
  const [events, setEvents] = useState(null)
  const [eventsError, setEventsError] = useState(null)
  const [expandedDriftKey, setExpandedDriftKey] = useState(null)

  // Pass-2b write-action state: a fixed actor plus an optional free-text reason,
  // plus bookkeeping for the in-flight request and its success/failure feedback.
  const [actor] = useState(ACTOR)
  const [reason, setReason] = useState('')
  const [pendingAction, setPendingAction] = useState(null)
  const [actionError, setActionError] = useState(null)
  const [actionNotice, setActionNotice] = useState(null)

  const loadList = useCallback(async () => {
    setListLoading(true)
    try {
      const response = await fetch(LIST_URL)
      const body = await readBody(response)
      if (!response || !response.ok) {
        setListError(classifyFailure(response, body))
        return
      }
      setRecords(Array.isArray(body && body.records) ? body.records : [])
      setListError(null)
    } catch (err) {
      setListError(networkFailure(err))
    } finally {
      setListLoading(false)
    }
  }, [])

  const loadDetail = useCallback(async (recordId, workspaceId) => {
    if (!recordId) return
    setDetailLoading(true)
    const query = workspaceId ? `?workspace_id=${encodeURIComponent(workspaceId)}` : ''
    try {
      const response = await fetch(
        `${LIST_URL}/${encodeURIComponent(recordId)}${query}`
      )
      const body = await readBody(response)
      if (!response || !response.ok) {
        setDetailError(classifyFailure(response, body))
        setDetail(null)
      } else {
        setDetail(body)
        setDetailError(null)
      }
    } catch (err) {
      setDetailError(networkFailure(err))
      setDetail(null)
    } finally {
      setDetailLoading(false)
    }
  }, [])

  const loadEvents = useCallback(async (recordId, workspaceId) => {
    if (!recordId) return
    const query = workspaceId ? `?workspace_id=${encodeURIComponent(workspaceId)}` : ''
    try {
      const response = await fetch(
        `${LIST_URL}/${encodeURIComponent(recordId)}/events${query}`
      )
      const body = await readBody(response)
      if (!response || !response.ok) {
        setEventsError(classifyFailure(response, body))
        setEvents([])
      } else {
        setEvents(Array.isArray(body && body.events) ? body.events : [])
        setEventsError(null)
      }
    } catch (err) {
      setEventsError(networkFailure(err))
      setEvents([])
    }
  }, [])

  useEffect(() => {
    loadList()
  }, [loadList])

  // 10s polling on the list only (no WebSocket/SSE).
  useEffect(() => {
    const timer = setInterval(() => {
      loadList()
    }, POLL_MS)
    return () => clearInterval(timer)
  }, [loadList])

  const list = Array.isArray(records) ? records : []
  const selectedRecord =
    list.find((record, index) => recordKey(record, index) === selectedId) || null
  const selectedWorkspaceId =
    selectedRecord && selectedRecord.workspace_id ? selectedRecord.workspace_id : ''

  useEffect(() => {
    if (!selectedId) return
    loadDetail(selectedId, selectedWorkspaceId)
    loadEvents(selectedId, selectedWorkspaceId)
  }, [selectedId, selectedWorkspaceId, loadDetail, loadEvents])

  const handleRefresh = () => {
    loadList()
    if (selectedId) {
      loadDetail(selectedId, selectedWorkspaceId)
      loadEvents(selectedId, selectedWorkspaceId)
    }
  }

  //: Fire one record-keyed write action. On success refetch the detail AND the
  //: list (never optimistic); on failure surface a distinct message per mode.
  //: `reason` is omitted from the body entirely when blank (never an empty
  //: string), and the workspace_id travels as a query parameter.
  const performAction = useCallback(
    async (action) => {
      if (!selectedId) return
      if (!selectedWorkspaceId) {
        setActionError({
          kind: 'bad_request',
          message: 'Cannot run the action: the record has no workspace_id.',
        })
        return
      }
      setPendingAction(action)
      setActionError(null)
      setActionNotice(null)
      const query = `?workspace_id=${encodeURIComponent(selectedWorkspaceId)}`
      const requestBody = { actor }
      const trimmedReason = reason.trim()
      if (trimmedReason) requestBody.reason = trimmedReason
      try {
        const response = await fetch(
          `${LIST_URL}/${encodeURIComponent(selectedId)}/${action}${query}`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(requestBody),
          }
        )
        const payload = await readBody(response)
        if (!response || !response.ok) {
          setActionError(classifyFailure(response, payload))
          return
        }
        await loadList()
        loadDetail(selectedId, selectedWorkspaceId)
        loadEvents(selectedId, selectedWorkspaceId)
        setActionNotice(`${actionLabel(action)} applied to ${selectedId}.`)
      } catch (err) {
        setActionError(networkFailure(err))
      } finally {
        setPendingAction(null)
      }
    },
    [
      actor,
      reason,
      selectedId,
      selectedWorkspaceId,
      loadList,
      loadDetail,
      loadEvents,
    ]
  )

  const detailDrift = detail && typeof detail === 'object' ? detail.drift : undefined
  const driftFindings = Array.isArray(detailDrift) ? detailDrift : []
  const eventList = Array.isArray(events) ? events : []
  const intentSnapshot =
    detail && detail.intent_snapshot && typeof detail.intent_snapshot === 'object'
      ? detail.intent_snapshot
      : {}
  const intentKeys = Object.keys(intentSnapshot)
  //: Which write actions the selected record currently permits (see helpers).
  const enablement = actionEnablement(detail || selectedRecord)

  return (
    <div className="rcp-panel">
      <div className="gms-section-head">
        <h3 className="gms-section-title">Container Records</h3>
        <button
          type="button"
          className="ws-modal-btn rcp-refresh-btn"
          onClick={handleRefresh}
          disabled={listLoading}
        >
          Refresh Records
        </button>
      </div>

      <p className="rcp-hint">
        Durable container records. The list refreshes every 10 seconds.
      </p>

      {listError && (
        <p className="gms-error rcp-error" role="alert">
          {listError.message}
        </p>
      )}

      {listLoading && list.length === 0 && !listError && (
        <p className="rcp-hint">Loading records…</p>
      )}

      {!listLoading && !listError && list.length === 0 && (
        <p className="gms-empty rcp-empty">
          No container records yet. Start a container to create one.
        </p>
      )}

      {list.length > 0 && (
        <ul className="rcp-record-list">
          {list.map((record, index) => {
            const key = recordKey(record, index)
            const status = driftStatus(record.drift)
            const selected = key === selectedId
            return (
              <li key={key}>
                <button
                  type="button"
                  className={`rcp-record-row${selected ? ' rcp-record-row-selected' : ''}`}
                  aria-pressed={selected}
                  onClick={() => setSelectedId(key)}
                >
                  <span className="rcp-record-name">
                    {record.name || record.id || key}
                  </span>
                  <span className="rcp-badge rcp-state">
                    {record.state || 'unknown'}
                  </span>
                  <span className="rcp-record-lifecycle">
                    {record.lifecycle_class || '—'}
                  </span>
                  <span
                    className={`rcp-drift-indicator rcp-drift-${status}`}
                    title={
                      status === 'unknown'
                        ? 'Live Docker state could not be inspected (Docker unreachable?)'
                        : undefined
                    }
                  >
                    {driftLabel(record.drift)}
                  </span>
                  <span className="rcp-record-updated">
                    {record.updated_at || '—'}
                  </span>
                </button>
              </li>
            )
          })}
        </ul>
      )}

      {!selectedId && (
        <p className="rcp-hint">
          Select a record to see its details and event log.
        </p>
      )}

      {selectedId && (
        <section className="rcp-detail" aria-label="Record detail">
          <div className="rcp-detail-head">
            <h4 className="rcp-detail-title">
              {detail?.name || selectedRecord?.name || selectedId}
            </h4>
            <button
              type="button"
              className="ws-modal-btn"
              onClick={() => setSelectedId(null)}
            >
              Close detail
            </button>
          </div>

          {detailLoading && !detail && !detailError && (
            <p className="rcp-hint">Loading record…</p>
          )}

          {detailError && (
            <p className="gms-error rcp-error" role="alert">
              {detailError.message}
            </p>
          )}

          {detail && (
            <>
              <dl className="rcp-fields">
                <FieldRow label="Record id" value={detail.id} />
                <FieldRow label="Container id" value={detail.container_id} />
                <FieldRow label="Name" value={detail.name} />
                <FieldRow label="Workspace" value={detail.workspace_id} />
                <FieldRow label="State" value={detail.state} />
                <FieldRow label="Lifecycle class" value={detail.lifecycle_class} />
                <FieldRow label="Owner" value={detail.owner} />
                <FieldRow label="Docker id" value={detail.docker_id} />
                <FieldRow label="Purpose" value={detail.purpose} />
                <FieldRow label="Created at" value={detail.created_at} />
                <FieldRow label="Updated at" value={detail.updated_at} />
                <FieldRow label="Restart policy" value={detail.restart_policy} />
                <FieldRow label="Retention days" value={detail.retention_days} />
                <FieldRow label="Schema version" value={detail.schema_version} />
                <FieldRow label="Inferred" value={detail.inferred ? 'yes' : 'no'} />
              </dl>

              {detail.notes && <p className="rcp-notes">Notes: {detail.notes}</p>}

              <h5 className="rcp-subsection-title">Intent snapshot</h5>
              {intentKeys.length === 0 ? (
                <p className="gms-empty rcp-empty">No intent snapshot recorded.</p>
              ) : (
                <dl className="rcp-fields rcp-intent">
                  {intentKeys.map((name) => (
                    <FieldRow key={name} label={name} value={intentSnapshot[name]} />
                  ))}
                </dl>
              )}

              <h5 className="rcp-subsection-title">Drift</h5>
              {driftStatus(detailDrift) === 'unknown' && (
                <p className="rcp-drift-unknown" role="status">
                  Drift unknown — the live Docker state could not be inspected
                  (Docker unreachable?).
                </p>
              )}
              {driftStatus(detailDrift) === 'clean' && (
                <p className="rcp-drift-clean" role="status">
                  No drift detected.
                </p>
              )}
              {driftFindings.length > 0 && (
                <>
                  <p className="rcp-drift-detected" role="status">
                    Drift detected — {driftFindings.length} finding
                    {driftFindings.length === 1 ? '' : 's'}.
                  </p>
                  <DriftTable
                    findings={driftFindings}
                    expandedKey={expandedDriftKey}
                    onToggle={(key) =>
                      setExpandedDriftKey(expandedDriftKey === key ? null : key)
                    }
                  />
                </>
              )}
            </>
          )}

          <section className="rcp-events" aria-label="Event log">
            <h5 className="rcp-subsection-title">Event log</h5>
            {eventsError && (
              <p className="gms-error rcp-error" role="alert">
                {eventsError.message}
              </p>
            )}
            {!eventsError && eventList.length === 0 && (
              <p className="gms-empty rcp-empty">
                No events recorded for this record.
              </p>
            )}
            {eventList.length > 0 && (
              <ul className="rcp-event-list">
                {eventList.map((event, index) => (
                  <li
                    className="rcp-event-row"
                    key={`${event && event.timestamp}-${index}`}
                  >
                    <span className="rcp-event-time">
                      {displayValue(event && event.timestamp)}
                    </span>
                    <span className="rcp-badge rcp-event-type">
                      {displayValue(event && event.event_type)}
                    </span>
                    <span className="rcp-event-actor">
                      actor: {displayValue(event && event.actor)}
                    </span>
                    <code className="rcp-event-payload">
                      {jsonText((event && event.payload) || {})}
                    </code>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <div className="rcp-actions" role="group" aria-label="Record actions">
            {ACTIONS.map((action) => (
              <button
                key={action}
                type="button"
                className={`ws-modal-btn rcp-action-btn rcp-action-${action}`}
                onClick={() => performAction(action)}
                disabled={pendingAction !== null || !enablement[action]}
                title={
                  enablement[action]
                    ? undefined
                    : 'Not permitted for this record state'
                }
              >
                {pendingAction === action
                  ? actionPendingLabel(action)
                  : actionLabel(action)}
              </button>
            ))}
            {pendingAction && (
              <p className="rcp-action-pending" role="status">
                {actionPendingLabel(pendingAction)} (waiting for the backend…)
              </p>
            )}
            {actionError && (
              <p className="gms-error rcp-error" role="alert">
                {actionError.message}
              </p>
            )}
            {actionNotice && (
              <p className="rcp-action-notice" role="status">
                {actionNotice}
              </p>
            )}
            <div className="rcp-action-context">
              <span className="rcp-actor">Actor: {actor}</span>
              <label className="rcp-reason-label">
                Reason
                <input
                  type="text"
                  className="rcp-reason-input"
                  value={reason}
                  placeholder="optional note for write actions"
                  onChange={(event) => setReason(event.target.value)}
                />
              </label>
            </div>
          </div>
        </section>
      )}
    </div>
  )
}
