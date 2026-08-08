// --- NewSessionModal.jsx ---
// Phase 3: real session management for the Workspace Panel. Lists the
// workspace's existing sessions (GET /api/session/list), creates new ones
// (POST /api/session/create), and deletes them (DELETE /api/session/{id}).
// "Open" hands off to the main app by navigating to '#/session/<id>' —
// App.jsx's session route loads the session tab directly.

import React, { useEffect, useState } from 'react'
import useWorkspaceStore from '../../../store/workspaceStore'
import '../WorkspacePanel.css'

const MODES = [
  { id: 'agent', label: 'Agent', desc: 'Full tools, no worker' },
  { id: 'engineer', label: 'Engineer', desc: 'Delegation only' },
  { id: 'custom', label: 'Custom', desc: 'Your tools, your prompt' },
]

export default function NewSessionModal({ workspace, onClose }) {
  const createSession = useWorkspaceStore((s) => s.createSession)
  const deleteSession = useWorkspaceStore((s) => s.deleteSession)
  const fetchSessions = useWorkspaceStore((s) => s.fetchSessions)
  const storeSessions = useWorkspaceStore((s) => s.sessions)
  const sessions = storeSessions.length > 0 ? storeSessions : workspace.sessions || []

  const [name, setName] = useState('')
  const [mode, setMode] = useState(() => localStorage.getItem('thoughtmachine_last_mode') || 'engineer')
  const [creating, setCreating] = useState(false)
  const [deletingId, setDeletingId] = useState(null)
  const [error, setError] = useState('')
  const [created, setCreated] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    fetchSessions(workspace.id)
      .catch(() => {})
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [workspace.id, fetchSessions])

  const handleCreate = async () => {
    setCreating(true)
    setError('')
    try {
      const data = await createSession(workspace.id, {
        name: name.trim() || undefined,
        mode,
      })
      localStorage.setItem('thoughtmachine_last_mode', mode)
      setCreated(data)
      fetchSessions(workspace.id).catch(() => {})
    } catch (err) {
      setError(err.message || 'Failed to create session')
    } finally {
      setCreating(false)
    }
  }

  const handleDelete = async (sessionId, sessionName) => {
    if (!window.confirm(`Delete session "${sessionName || 'Untitled'}"?`)) return
    setDeletingId(sessionId)
    setError('')
    try {
      await deleteSession(workspace.id, sessionId)
    } catch (err) {
      setError(err.message || 'Failed to delete session')
    } finally {
      setDeletingId(null)
    }
  }

  // Navigate straight to the session route; App.jsx opens a tab for it.
  const openSession = (sessionId) => {
    localStorage.setItem('activeSessionId', sessionId)
    window.location.hash = '#/session/' + encodeURIComponent(sessionId)
  }

  const formatTime = (iso) => {
    if (!iso) return ''
    try {
      const d = new Date(iso)
      return isNaN(d.getTime()) ? iso : d.toLocaleString()
    } catch {
      return iso
    }
  }

  return (
    <div className="wp-modal-overlay" onClick={onClose}>
      <div
        className="wp-modal wp-new-session-modal"
        role="dialog"
        aria-label="New session"
        onClick={(e) => e.stopPropagation()}
      >
        <h4 className="wp-modal-title">New Session</h4>
        <p className="wp-modal-path">{workspace.name} — {workspace.path || workspace.root || ''}</p>

        {created ? (
          <div className="wp-success">
            <p>Session created.</p>
            <p className="wp-success-id">Session ID: <code>{created.session_id}</code></p>
            <div className="wp-modal-footer">
              <button className="wp-btn" onClick={onClose}>Close</button>
              <button className="wp-btn wp-btn-primary" onClick={() => openSession(created.session_id)}>
                Open Session
              </button>
            </div>
          </div>
        ) : (
          <>
            <label className="wp-field">
              <span>Name (optional)</span>
              <input
                className="wp-input"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. Refactor auth module"
                onKeyDown={(e) => {
                  if (e.key === 'Enter') handleCreate()
                }}
              />
            </label>

            <div className="wp-mode-group">
              <span className="wp-field-label">Mode</span>
              {MODES.map((m) => (
                <button
                  key={m.id}
                  type="button"
                  className={`wp-mode-option ${mode === m.id ? 'wp-mode-option-active' : ''}`}
                  onClick={() => setMode(m.id)}
                >
                  <span className="wp-mode-name">{m.label}</span>
                  <span className="wp-mode-desc">{m.desc}</span>
                </button>
              ))}
            </div>

            {error && <p className="wp-error-text">{error}</p>}

            <div className="wp-modal-footer">
              <button className="wp-btn" onClick={onClose}>Cancel</button>
              <button className="wp-btn wp-btn-primary" disabled={creating} onClick={handleCreate}>
                {creating ? 'Creating…' : 'Create Session'}
              </button>
            </div>
          </>
        )}

        <div className="wp-session-list">
          <h5 className="wp-session-title">Sessions in this workspace</h5>
          {loading ? (
            <p className="wp-empty">Loading sessions…</p>
          ) : sessions.length === 0 ? (
            <p className="wp-empty">No sessions yet.</p>
          ) : (
            sessions.map((s) => (
              <div className="wp-session-row" key={s.session_id}>
                <div className="wp-session-info">
                  <span className="wp-session-name">{s.name || 'Untitled'}</span>
                  <span className="wp-session-meta">
                    <span className={`wp-mode-badge wp-mode-badge-${s.mode || 'agent'}`}>{s.mode || 'agent'}</span>
                    {formatTime(s.updated_at)}
                  </span>
                </div>
                <div className="wp-session-actions">
                  <button className="wp-btn" onClick={() => openSession(s.session_id)}>Open</button>
                  <button
                    className="wp-btn wp-btn-danger"
                    disabled={deletingId === s.session_id}
                    onClick={() => handleDelete(s.session_id, s.name)}
                  >
                    {deletingId === s.session_id ? '…' : 'Delete'}
                  </button>
                </div>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  )
}
