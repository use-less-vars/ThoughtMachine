import React, { useState } from 'react'

const MODE_LABELS = { agent: 'Agent', engineer: 'Engineer', custom: 'Custom' }

function timeAgo(isoString) {
  if (!isoString) return ''
  try {
    const date = new Date(isoString)
    if (isNaN(date.getTime())) return ''
    const diff = Date.now() - date.getTime()
    const mins = Math.floor(diff / 60000)
    if (mins < 1) return 'just now'
    if (mins < 60) return `${mins}m ago`
    const hrs = Math.floor(mins / 60)
    if (hrs < 24) return `${hrs}h ago`
    const days = Math.floor(hrs / 24)
    return `${days}d ago`
  } catch {
    return ''
  }
}

export default function SessionList({ sessions, onNew, onOpenTab, onDelete, onRename }) {
  const [editingId, setEditingId] = useState(null)
  const [editName, setEditName] = useState('')

  const handleStartRename = (sessionId, currentName) => {
    setEditingId(sessionId)
    setEditName(currentName || '')
  }

  const handleSubmitRename = (sessionId) => {
    const trimmed = editName.trim()
    if (trimmed && onRename) {
      onRename(sessionId, trimmed)
    }
    setEditingId(null)
    setEditName('')
  }

  const handleCancelRename = () => {
    setEditingId(null)
    setEditName('')
  }

  const sidebarStyle = {
    display: 'flex',
    flexDirection: 'column',
    height: '100%',
    background: 'var(--bg-surface, #313244)',
  }

  const headerStyle = {
    padding: '0.75rem 1rem',
    borderBottom: '1px solid var(--border, #45475a)',
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
  }

  const headerTitleStyle = {
    color: 'var(--text-primary, #cdd6f4)',
    fontSize: '0.9rem',
    fontWeight: 700,
  }

  const newBtnStyle = {
    padding: '0.3rem 0.7rem',
    background: 'var(--accent, #89b4fa)',
    color: '#fff',
    border: 'none',
    borderRadius: '4px',
    cursor: 'pointer',
    fontSize: '0.75rem',
    fontWeight: 600,
  }

  const listStyle = {
    flex: 1,
    overflowY: 'auto',
    padding: '0.5rem 0',
  }

  const cardStyle = {
    padding: '0.5rem 0.75rem',
    margin: '0 0.5rem 0.3rem',
    borderRadius: '6px',
    cursor: 'pointer',
    border: '1px solid transparent',
    transition: 'background 0.1s, border-color 0.1s',
  }

  const cardTitleRow = {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: '0.3rem',
  }

  const nameStyle = {
    color: 'var(--text-primary, #cdd6f4)',
    fontSize: '0.8rem',
    fontWeight: 600,
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    flex: 1,
    minWidth: 0,
  }

  const modeBadgeStyle = {
    fontSize: '0.6rem',
    fontWeight: 600,
    padding: '0.05rem 0.3rem',
    borderRadius: '3px',
    background: 'rgba(166,227,161,0.2)',
    color: '#a6e3a1',
    whiteSpace: 'nowrap',
    flexShrink: 0,
  }

  const previewStyle = {
    fontSize: '0.7rem',
    color: 'var(--text-muted, #6c7086)',
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    marginTop: '0.1rem',
  }

  const actionsRow = {
    display: 'flex',
    gap: '0.25rem',
    marginTop: '0.25rem',
  }

  const actionBtnStyle = {
    background: 'none',
    border: 'none',
    color: 'var(--text-muted, #6c7086)',
    cursor: 'pointer',
    fontSize: '0.7rem',
    padding: '0.1rem 0.3rem',
    borderRadius: '3px',
  }

  const emptyStyle = {
    padding: '1rem',
    textAlign: 'center',
    color: 'var(--text-muted, #6c7086)',
    fontSize: '0.8rem',
  }

  const sortedSessions = [...(sessions || [])].sort((a, b) => {
    const aTime = a.updated_at || a.created_at || ''
    const bTime = b.updated_at || b.created_at || ''
    return bTime.localeCompare(aTime)
  })

  return (
    <div style={sidebarStyle}>
      <div style={headerStyle}>
        <span style={headerTitleStyle}>Sessions</span>
        <button style={newBtnStyle} onClick={onNew}>
          + New
        </button>
      </div>

      <div style={listStyle}>
        {sortedSessions.length === 0 ? (
          <div style={emptyStyle}>
            No sessions yet.<br />Click <strong>+ New</strong> to begin.
          </div>
        ) : (
          sortedSessions.map(session => (
            <div
              key={session.session_id}
              style={cardStyle}
              onClick={() => onOpenTab(session.session_id)}
              onMouseEnter={e => {
                e.currentTarget.style.background = 'rgba(137,180,250,0.06)'
                e.currentTarget.style.borderColor = 'var(--accent, #89b4fa)'
              }}
              onMouseLeave={e => {
                e.currentTarget.style.background = 'transparent'
                e.currentTarget.style.borderColor = 'transparent'
              }}
            >
              <div style={cardTitleRow}>
                {editingId === session.session_id ? (
                  <input
                    style={{
                      flex: 1,
                      background: 'var(--bg-primary, #1e1e2e)',
                      border: '1px solid var(--accent, #89b4fa)',
                      borderRadius: '3px',
                      color: 'var(--text-primary, #cdd6f4)',
                      fontSize: '0.8rem',
                      padding: '0.15rem 0.3rem',
                      outline: 'none',
                    }}
                    value={editName}
                    onChange={e => setEditName(e.target.value)}
                    onKeyDown={e => {
                      if (e.key === 'Enter') handleSubmitRename(session.session_id)
                      if (e.key === 'Escape') handleCancelRename()
                    }}
                    autoFocus
                    onClick={e => e.stopPropagation()}
                  />
                ) : (
                  <span style={nameStyle} title={session.name}>
                    {session.name || 'Untitled'}
                  </span>
                )}
                <span style={modeBadgeStyle}>
                  {MODE_LABELS[session.mode] || session.mode || 'Agent'}
                </span>
              </div>

              <div style={previewStyle}>
                <span style={{ opacity: 0.7 }}>{timeAgo(session.updated_at || session.created_at)}</span>
                {session.preview && <span> — {session.preview}</span>}
              </div>

              <div style={actionsRow}>
                <button
                  style={actionBtnStyle}
                  onClick={e => {
                    e.stopPropagation()
                    handleStartRename(session.session_id, session.name)
                  }}
                  title="Rename"
                >
                  Rename
                </button>
                <button
                  style={{ ...actionBtnStyle, color: 'var(--danger, #f38ba8)' }}
                  onClick={e => {
                    e.stopPropagation()
                    if (window.confirm(`Delete "${session.name || 'Untitled'}"?`)) {
                      onDelete(session.session_id)
                    }
                  }}
                  title="Delete"
                >
                  Delete
                </button>
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  )
}
