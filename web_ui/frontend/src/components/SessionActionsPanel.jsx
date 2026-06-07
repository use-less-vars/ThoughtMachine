/*
 * SessionActionsPanel.jsx
 *
 * Slide-in panel for session actions: Save As…, Delete Session,
 * and a list of Saved Sessions to quickly open.
 *
 * Props:
 *   sessionId     — current session id
 *   sessionName   — current session name
 *   onClose       — called to close the panel
 *   onRename      — called with (sessionId, newName)
 *   onDelete      — called with (sessionId)
 *   sessionsList  — array of { session_id, name, updated_at }
 *   onOpenSession — called with (sessionId)
 */

import React, { useState, useMemo } from 'react'

export default function SessionActionsPanel({
  sessionId,
  sessionName,
  onClose,
  onRename,
  onDelete,
  sessionsList = [],
  onOpenSession,
}) {
  const [showSaveAsInput, setShowSaveAsInput] = useState(false)
  const [saveAsName, setSaveAsName] = useState(sessionName || '')
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false)
  // Sort sessions by updated_at descending
  const sortedSessions = useMemo(() => {
    return [...sessionsList].sort((a, b) => {
      const aTime = new Date(a.updated_at || 0).getTime()
      const bTime = new Date(b.updated_at || 0).getTime()
      return bTime - aTime
    })
  }, [sessionsList])

  const handleSaveAs = () => {
    const name = saveAsName.trim()
    if (!name) return
    onRename?.(sessionId, name)
    setShowSaveAsInput(false)
    setSaveAsName('')
  }

  const handleDeleteConfirm = () => {
    onDelete?.(sessionId)
    setShowDeleteConfirm(false)
  }

  const formatDate = (dateStr) => {
    if (!dateStr) return ''
    try {
      const d = new Date(dateStr)
      return d.toLocaleDateString(undefined, {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      })
    } catch {
      return dateStr
    }
  }

  return (
    <>
      {/* Overlay backdrop */}
      <div className="session-actions-overlay" onClick={onClose} />

      {/* Slide-in panel */}
      <div className="session-actions-panel">
        <div className="session-actions-header">
          <h3>Session Actions</h3>
          <button className="session-actions-close" onClick={onClose} title="Close panel">
            ✕
          </button>
        </div>

        {/* Save As… */}
        <div className="session-actions-section">
          <h4>Save As…</h4>
          {!showSaveAsInput ? (
            <button
              className="btn btn-accent"
              onClick={() => {
                setSaveAsName(sessionName || '')
                setShowSaveAsInput(true)
              }}
            >
              💾 Save As…
            </button>
          ) : (
            <div className="session-actions-save-form">
              <input
                className="session-actions-input"
                type="text"
                value={saveAsName}
                onChange={(e) => setSaveAsName(e.target.value)}
                placeholder={sessionName || 'Session name'}
                autoFocus
                onKeyDown={(e) => {
                  if (e.key === 'Enter') handleSaveAs()
                  if (e.key === 'Escape') setShowSaveAsInput(false)
                }}
              />
              <div className="session-actions-save-form-buttons">
                <button className="btn btn-accent btn-sm" onClick={handleSaveAs}>
                  Save
                </button>
                <button className="btn btn-sm" onClick={() => setShowSaveAsInput(false)}>
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>

        {/* Delete Session */}
        <div className="session-actions-section">
          <h4>Delete Session</h4>
          {!showDeleteConfirm ? (
            <button className="btn btn-danger" onClick={() => setShowDeleteConfirm(true)}>
              🗑 Delete Session
            </button>
          ) : (
            <div className="session-actions-delete-confirm">
              <p>Delete this session? This cannot be undone.</p>
              <div className="session-actions-delete-confirm-buttons">
                <button className="btn btn-danger btn-sm" onClick={handleDeleteConfirm}>
                  Delete
                </button>
                <button className="btn btn-sm" onClick={() => setShowDeleteConfirm(false)}>
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>



        {/* Saved Sessions list */}
        <div className="session-actions-section session-actions-section-grow">
          <h4>Saved Sessions</h4>
          <div className="session-actions-list">
            {sortedSessions.length === 0 ? (
              <div className="session-actions-empty">No saved sessions yet.</div>
            ) : (
              sortedSessions.map((s) => (
                <div
                  key={s.session_id}
                  className={`session-actions-item ${s.session_id === sessionId ? 'session-actions-item-active' : ''}`}
                  onClick={() => onOpenSession?.(s.session_id)}
                  title={s.name || s.session_id}
                >
                  <span className="session-actions-item-name">
                    {s.name || s.session_id.slice(0, 8)}
                  </span>
                  <span className="session-actions-item-date">
                    {formatDate(s.updated_at)}
                  </span>
                </div>
              ))
            )}
          </div>
        </div>
      </div>
    </>
  )
}
