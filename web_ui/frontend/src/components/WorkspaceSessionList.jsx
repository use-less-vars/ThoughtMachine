import React, { useState, useEffect, useCallback } from 'react'

const API_BASE = ''

function formatTimestamp(isoString) {
  if (!isoString) return ''
  try {
    const date = new Date(isoString)
    if (isNaN(date.getTime())) return isoString
    const now = new Date()
    const diffMs = now - date
    const diffMin = Math.floor(diffMs / 60000)
    const diffHrs = Math.floor(diffMs / 3600000)
    const diffDays = Math.floor(diffMs / 86400000)

    if (diffMin < 1) return 'just now'
    if (diffMin < 60) return `${diffMin}m ago`
    if (diffHrs < 24) return `${diffHrs}h ago`
    if (diffDays < 7) return `${diffDays}d ago`

    return date.toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
      year: date.getFullYear() !== now.getFullYear() ? 'numeric' : undefined,
    })
  } catch {
    return isoString
  }
}

function modeLabel(mode) {
  const labels = { agent: 'Agent', engineer: 'Engineer', custom: 'Custom' }
  return labels[mode] || mode || 'Agent'
}

function modeColor(mode) {
  const colors = {
    agent: '#a6e3a1',
    engineer: '#89b4fa',
    custom: '#f9e2af',
  }
  return colors[mode] || '#a6adc8'
}

export default function SessionList({ workspaceId, onOpen, onNewSession, onBack }) {
  const [sessions, setSessions] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [deleting, setDeleting] = useState(null) // session_id being deleted

  const fetchSessions = useCallback(() => {
    if (!workspaceId) {
      setLoading(false)
      return
    }
    setLoading(true)
    setError('')
    const url = `${API_BASE}/api/session/list?workspace_id=${encodeURIComponent(workspaceId)}`
    fetch(url)
      .then(r => {
        if (!r.ok) throw new Error(`Server returned ${r.status}`)
        return r.json()
      })
      .then(data => {
        // Sort: most recent first
        const sorted = (data || []).sort((a, b) => {
          const aTime = a.updated_at || a.created_at || ''
          const bTime = b.updated_at || b.created_at || ''
          return bTime.localeCompare(aTime)
        })
        setSessions(sorted)
      })
      .catch(err => {
        setError(err.message || 'Failed to load sessions')
      })
      .finally(() => setLoading(false))
  }, [workspaceId])

  useEffect(() => {
    fetchSessions()
  }, [fetchSessions])

  const handleDelete = async (sessionId, e) => {
    e.stopPropagation()
    if (!window.confirm('Delete this session? This cannot be undone.')) return
    setDeleting(sessionId)
    try {
      const r = await fetch(`${API_BASE}/api/session/${encodeURIComponent(sessionId)}`, {
        method: 'DELETE',
      })
      if (!r.ok) throw new Error('Failed to delete')
      setSessions(prev => prev.filter(s => s.session_id !== sessionId))
    } catch (err) {
      setError(err.message || 'Failed to delete session')
    } finally {
      setDeleting(null)
    }
  }

  // ── Styles ──────────────────────────────────────────────────────────

  const containerStyle = {
    display: 'flex',
    flexDirection: 'column',
    gap: '0.75rem',
  }

  const headerStyle = {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
  }

  const headerLabelStyle = {
    color: 'var(--text-secondary, #a6adc8)',
    fontSize: '0.85rem',
    fontWeight: 600,
  }

  const backBtnStyle = {
    background: 'none',
    border: '1px solid var(--border, #585b70)',
    color: 'var(--text-primary, #cdd6f4)',
    borderRadius: '4px',
    padding: '0.3rem 0.6rem',
    cursor: 'pointer',
    fontSize: '0.8rem',
  }

  const newBtnStyle = {
    padding: '0.5rem 1rem',
    background: 'var(--accent, #89b4fa)',
    color: '#fff',
    border: 'none',
    borderRadius: '4px',
    cursor: 'pointer',
    fontSize: '0.85rem',
    fontWeight: 600,
  }

  const listStyle = {
    display: 'flex',
    flexDirection: 'column',
    gap: '0.4rem',
    maxHeight: '320px',
    overflowY: 'auto',
  }

  const cardStyle = {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    padding: '0.6rem 0.75rem',
    background: 'var(--bg-primary, #1e1e2e)',
    border: '1px solid var(--border, #45475a)',
    borderRadius: '6px',
    cursor: 'pointer',
    transition: 'border-color 0.15s, background 0.15s',
  }

  const cardBodyStyle = {
    flex: 1,
    minWidth: 0,
    overflow: 'hidden',
  }

  const cardTitleRow = {
    display: 'flex',
    alignItems: 'center',
    gap: '0.5rem',
    marginBottom: '0.2rem',
  }

  const cardNameStyle = {
    color: 'var(--text-primary, #cdd6f4)',
    fontSize: '0.9rem',
    fontWeight: 600,
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
  }

  const modeBadgeStyle = (mode) => ({
    fontSize: '0.65rem',
    fontWeight: 600,
    padding: '0.1rem 0.35rem',
    borderRadius: '3px',
    background: modeColor(mode) + '22',
    color: modeColor(mode),
    border: `1px solid ${modeColor(mode)}44`,
    whiteSpace: 'nowrap',
    flexShrink: 0,
  })

  const cardMetaStyle = {
    display: 'flex',
    alignItems: 'center',
    gap: '0.5rem',
    fontSize: '0.75rem',
    color: 'var(--text-muted, #6c7086)',
  }

  const previewStyle = {
    fontSize: '0.75rem',
    color: 'var(--text-secondary, #a6adc8)',
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    marginTop: '0.15rem',
  }

  const cardActionsStyle = {
    display: 'flex',
    gap: '0.35rem',
    flexShrink: 0,
    marginLeft: '0.5rem',
  }

  const openBtnStyle = {
    padding: '0.3rem 0.7rem',
    background: 'var(--accent, #89b4fa)',
    color: '#fff',
    border: 'none',
    borderRadius: '4px',
    cursor: 'pointer',
    fontSize: '0.75rem',
    fontWeight: 600,
  }

  const deleteBtnStyle = {
    padding: '0.3rem 0.5rem',
    background: 'transparent',
    color: 'var(--danger, #f38ba8)',
    border: '1px solid rgba(243,139,168,0.3)',
    borderRadius: '4px',
    cursor: 'pointer',
    fontSize: '0.7rem',
    opacity: deleting ? 0.5 : 1,
  }

  const loadingStyle = {
    padding: '1.5rem',
    textAlign: 'center',
    color: 'var(--text-secondary, #a6adc8)',
    fontSize: '0.85rem',
  }

  const errorStyle = {
    color: 'var(--danger, #f38ba8)',
    fontSize: '0.85rem',
    padding: '0.4rem 0.6rem',
    background: 'rgba(243,139,168,0.1)',
    borderRadius: '4px',
  }

  const emptyStyle = {
    padding: '2rem 1rem',
    textAlign: 'center',
    color: 'var(--text-muted, #6c7086)',
    fontSize: '0.85rem',
    lineHeight: 1.5,
  }

  if (!workspaceId) {
    return (
      <div style={containerStyle}>
        <div style={headerStyle}>
          <span style={headerLabelStyle}>Sessions</span>
        </div>
        <div style={emptyStyle}>
          No workspace selected.
        </div>
      </div>
    )
  }

  return (
    <div style={containerStyle}>
      {/* Header with back and new session */}
      <div style={headerStyle}>
        <button style={backBtnStyle} onClick={onBack}>
          ← Back
        </button>
        <button style={newBtnStyle} onClick={onNewSession}>
          + New Session
        </button>
      </div>

      {/* Error */}
      {error && (
        <div style={errorStyle}>
          ⚠ {error}
        </div>
      )}

      {/* Loading */}
      {loading && (
        <div style={loadingStyle}>
          <span style={{ opacity: 0.7 }}>⟳</span> Loading sessions...
        </div>
      )}

      {/* Session list */}
      {!loading && !error && (
        <>
          {sessions.length === 0 ? (
            <div style={emptyStyle}>
              No sessions yet in this workspace.<br />
              Click <strong>+ New Session</strong> to create one.
            </div>
          ) : (
            <div style={listStyle}>
              {sessions.map(session => (
                <div
                  key={session.session_id}
                  style={cardStyle}
                  onClick={() => onOpen(session.session_id)}
                  onMouseEnter={e => {
                    e.currentTarget.style.borderColor = 'var(--accent, #89b4fa)'
                    e.currentTarget.style.background = 'rgba(137,180,250,0.06)'
                  }}
                  onMouseLeave={e => {
                    e.currentTarget.style.borderColor = 'var(--border, #45475a)'
                    e.currentTarget.style.background = 'var(--bg-primary, #1e1e2e)'
                  }}
                >
                  <div style={cardBodyStyle}>
                    <div style={cardTitleRow}>
                      <span style={cardNameStyle}>{session.name || 'Untitled Session'}</span>
                      <span style={modeBadgeStyle(session.mode)}>
                        {modeLabel(session.mode)}
                      </span>
                    </div>
                    <div style={cardMetaStyle}>
                      <span>🕐 {formatTimestamp(session.updated_at || session.created_at)}</span>
                    </div>
                    {session.preview && (
                      <div style={previewStyle}>
                        {session.preview}
                      </div>
                    )}
                  </div>
                  <div style={cardActionsStyle}>
                    <button
                      style={openBtnStyle}
                      onClick={(e) => {
                        e.stopPropagation()
                        onOpen(session.session_id)
                      }}
                    >
                      Open
                    </button>
                    <button
                      style={deleteBtnStyle}
                      disabled={deleting === session.session_id}
                      onClick={(e) => handleDelete(session.session_id, e)}
                    >
                      {deleting === session.session_id ? '…' : '✕'}
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  )
}
