/*
 * SessionList.jsx
 *
 * Right‑sidebar panel that displays saved sessions.
 * Supports opening (in a new tab), deleting, and renaming sessions.
 *
 * Props:
 *   sessions   — array of { session_id, name, created_at, updated_at, preview }
 *   onNew      — create a new tab
 *   onOpenTab  — called with (sessionId) to open a session in a new tab
 *   onDelete   — called with (sessionId)
 *   onRename   — called with (sessionId, newName)
 *   onSave     — called to save the active tab's session
 *   saveEnabled — boolean, whether save button is enabled
 */

import React, { useState } from 'react'

export default function SessionList({ sessions, onNew, onOpenTab, onDelete, onRename, onSave, saveEnabled }) {
  const [renamingId, setRenamingId] = useState(null)
  const [renameValue, setRenameValue] = useState('')

  const startRename = (sessionId, currentName) => {
    setRenamingId(sessionId)
    setRenameValue(currentName || '')
  }

  const submitRename = (sessionId) => {
    if (renameValue.trim()) {
      onRename(sessionId, renameValue.trim())
    }
    setRenamingId(null)
    setRenameValue('')
  }

  const cancelRename = () => {
    setRenamingId(null)
    setRenameValue('')
  }

  const formatDate = (isoStr) => {
    if (!isoStr) return ''
    try {
      const d = new Date(isoStr)
      return d.toLocaleDateString(undefined, {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      })
    } catch {
      return isoStr
    }
  }

  return (
    <div className="session-list-panel">
      <div className="session-list-header">
        <h3>Sessions</h3>
      </div>

      <div className="session-list-actions">
        {onSave && (
          <button
            className="btn btn-save"
            onClick={onSave}
            disabled={!saveEnabled}
            title={saveEnabled ? 'Save current session' : 'No active session to save'}
          >
            💾 Save Current
          </button>
        )}
        <button className="btn btn-new" onClick={onNew}>
          ✨ New Session
        </button>
      </div>

      <div className="session-list-items">
        {sessions.length === 0 && (
          <p className="session-list-empty">No saved sessions yet.</p>
        )}
        {sessions.map((s) => (
          <div key={s.session_id} className="session-item">
            {renamingId === s.session_id ? (
              <div className="session-rename-form">
                <input
                  type="text"
                  value={renameValue}
                  onChange={(e) => setRenameValue(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') submitRename(s.session_id)
                    if (e.key === 'Escape') cancelRename()
                  }}
                  autoFocus
                  className="session-rename-input"
                />
                <button className="btn btn-sm" onClick={() => submitRename(s.session_id)}>
                  ✓
                </button>
                <button className="btn btn-sm" onClick={cancelRename}>
                  ✗
                </button>
              </div>
            ) : (
              <>
                <div
                  className="session-item-info"
                  onClick={() => onOpenTab(s.session_id)}
                  title="Open in new tab"
                >
                  <div className="session-item-name">
                    {s.name || 'Untitled'}
                  </div>
                  <div className="session-item-meta">
                    {formatDate(s.updated_at || s.created_at)}
                    {s.preview && ` — ${s.preview}`}
                  </div>
                </div>
                <div className="session-item-actions">
                  <button
                    className="btn btn-icon"
                    onClick={() => startRename(s.session_id, s.name)}
                    title="Rename"
                  >
                    ✏️
                  </button>
                  <button
                    className="btn btn-icon"
                    onClick={() => onDelete(s.session_id)}
                    title="Delete"
                  >
                    🗑️
                  </button>
                </div>
              </>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
