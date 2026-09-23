import React, { useCallback, useEffect, useState } from 'react'

// ── Worker blueprint editor (feature C.2) ────────────────────────────────────
//
// Blueprints are the workspace's `workers.json` definitions exposed as
// normalised `WorkerDefinition` payloads.  This panel lists them, opens an
// inline editor per selected row and PATCHes only the edited fields back.
//
// The fetch boundary is the only external dependency: no store, no props
// beyond `workspaceId`.

// Keys edited by the form.  They mirror the backend WorkerDefinition schema
// (name, description, system_prompt, tools, permission_footprint,
// warning_threshold_tokens, critical_threshold_tokens, ...).  Only the
// scalar-ish fields get a text control; structured fields (tools,
// permission_footprint, ...) are preserved untouched by the PATCH merge.
const EDIT_FIELDS = [
  { key: 'name', label: 'Name' },
  { key: 'description', label: 'Description' },
  { key: 'system_prompt', label: 'System prompt', multiline: true },
  { key: 'warning_threshold_tokens', label: 'Warning threshold (tokens)', numeric: true },
  { key: 'critical_threshold_tokens', label: 'Critical threshold (tokens)', numeric: true },
]

// Copy pinned by the C.2 contract: edits only affect workers spawned later.
const RESTART_TO_APPLY_HINT =
  'Edits are saved to the blueprint and apply to FUTURE worker spawns — restart a worker to apply them now.'

const EMPTY_COPY = 'No worker blueprints in this workspace.'

const rowThresholdText = (entry) => {
  const parts = []
  if (entry.warning_threshold_tokens != null) parts.push(`warn ${entry.warning_threshold_tokens}`)
  if (entry.critical_threshold_tokens != null) parts.push(`crit ${entry.critical_threshold_tokens}`)
  return parts.join(' · ')
}

// Numeric fields must reach the API as JSON numbers, not strings: the backend
// validates the merged dict through WorkerDefinition, which rejects "12345".
const coerceFieldValue = (field, value) => {
  if (!field.numeric) return value
  if (value === '' || value == null) return value
  const n = Number(value)
  return Number.isNaN(n) ? value : n
}

export default function WorkerBlueprintPanel({ workspaceId }) {
  const [blueprints, setBlueprints] = useState([])
  const [loadError, setLoadError] = useState(null)
  const [editing, setEditing] = useState(null) // { originalName, draft }
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)

  const base = `/api/workspace/${workspaceId}/workers/blueprints`

  const load = useCallback(async () => {
    const res = await fetch(base)
    if (!res.ok) throw new Error(`Failed to load worker blueprints (${res.status})`)
    const data = await res.json()
    if (Array.isArray(data)) return data
    if (data && Array.isArray(data.blueprints)) return data.blueprints
    return []
  }, [base])

  useEffect(() => {
    let cancelled = false
    load()
      .then((list) => {
        if (cancelled) return
        setBlueprints(list)
        setLoadError(null)
      })
      .catch((err) => {
        if (cancelled) return
        setLoadError(err && err.message ? err.message : String(err))
      })
    return () => {
      cancelled = true
    }
  }, [load])

  const openRow = (entry) => {
    const draft = {}
    for (const field of EDIT_FIELDS) draft[field.key] = entry[field.key]
    setError(null)
    setEditing({ originalName: entry.name, draft })
  }

  const handleChange = (key, value) => {
    setEditing((prev) => (prev ? { ...prev, draft: { ...prev.draft, [key]: value } } : prev))
  }

  const save = async () => {
    if (!editing) return
    const { originalName, draft } = editing
    setSaving(true)
    setError(null)

    const body = {}
    for (const field of EDIT_FIELDS) {
      body[field.key] = coerceFieldValue(field, draft[field.key])
    }

    try {
      const res = await fetch(`${base}/${encodeURIComponent(originalName)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      const payload = await res.json().catch(() => null)
      if (!res.ok) {
        const detail = payload && (payload.detail || payload.error)
        throw new Error(detail ? String(detail) : `Failed to save blueprint (${res.status})`)
      }
      const updated = payload && typeof payload === 'object' ? payload : body
      setBlueprints((prev) =>
        prev.map((entry) => (entry.name === originalName ? { ...entry, ...updated } : entry))
      )
      // Keep the editor open on the (possibly server-normalised) values.
      const nextDraft = {}
      for (const field of EDIT_FIELDS) nextDraft[field.key] = updated[field.key]
      setEditing({ originalName: updated.name != null ? updated.name : originalName, draft: nextDraft })
    } catch (err) {
      setError(err && err.message ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="worker-blueprint-panel" data-testid="worker-blueprint-panel">
      {loadError ? (
        <div className="blueprint-load-error" data-testid="blueprint-load-error" role="alert">
          {loadError}
        </div>
      ) : null}

      <div className="blueprint-list" data-testid="blueprint-list">
        {blueprints.length === 0 ? (
          <div className="blueprint-empty" data-testid="blueprint-empty">
            {EMPTY_COPY}
          </div>
        ) : (
          blueprints.map((entry, idx) => {
            const selected = editing && editing.originalName === entry.name
            return (
              <div
                key={`${entry.name}-${idx}`}
                className={
                  selected ? 'blueprint-row blueprint-row--selected' : 'blueprint-row'
                }
                data-testid={`blueprint-row-${entry.name}`}
                role="button"
                tabIndex={0}
                onClick={() => openRow(entry)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') openRow(entry)
                }}
              >
                <span className="blueprint-row-name">{entry.name}</span>
                {entry.description ? (
                  <span className="blueprint-row-description">{entry.description}</span>
                ) : null}
                <span className="blueprint-row-thresholds">{rowThresholdText(entry)}</span>
              </div>
            )
          })
        )}
      </div>

      {editing ? (
        <div className="blueprint-edit-form" data-testid="blueprint-edit-form">
          <div className="blueprint-edit-title">Edit blueprint: {editing.originalName}</div>

          {EDIT_FIELDS.map((field) =>
            field.multiline ? (
              <label key={field.key} className="blueprint-field">
                <span className="blueprint-field-label">{field.label}</span>
                <textarea
                  className="blueprint-field-input blueprint-field-input--multiline"
                  data-testid={`field-${field.key}`}
                  value={editing.draft[field.key] ?? ''}
                  onChange={(e) => handleChange(field.key, e.target.value)}
                />
              </label>
            ) : (
              <label key={field.key} className="blueprint-field">
                <span className="blueprint-field-label">{field.label}</span>
                <input
                  type="text"
                  className="blueprint-field-input"
                  data-testid={`field-${field.key}`}
                  value={editing.draft[field.key] ?? ''}
                  onChange={(e) => handleChange(field.key, e.target.value)}
                />
              </label>
            )
          )}

          <div className="blueprint-actions">
            <button
              type="button"
              className="btn btn-run"
              data-testid="blueprint-save"
              onClick={save}
              disabled={saving}
            >
              {saving ? 'Saving…' : 'Save'}
            </button>
          </div>

          {error ? (
            <div className="blueprint-error" data-testid="blueprint-error" role="alert">
              {error}
            </div>
          ) : null}

          <div className="restart-to-apply-hint" data-testid="restart-to-apply-hint">
            {RESTART_TO_APPLY_HINT}
          </div>
        </div>
      ) : null}
    </div>
  )
}
