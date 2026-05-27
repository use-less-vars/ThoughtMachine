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

 */

import React, { useState, useEffect, useRef, useMemo } from 'react'
import { List } from 'react-window'

const ITEM_HEIGHT = 72

/**
 * Row component for react-window v2 List.
 * Receives { index, style } plus spread rowProps (sessions, callbacks, etc.)
 */
const Row = React.memo(({ index, style, sessions, renamingId, renameValue, onOpenTab, onDelete, startRename, submitRename, cancelRename, setRenameValue, deleteConfirmId, onRequestDelete }) => {
  const s = sessions && sessions[index]
  if (!s) return null

  const isRenaming = renamingId === s.session_id

  const handleKeyDown = (e) => {
    if (e.key === 'Enter') submitRename(s.session_id)
    if (e.key === 'Escape') cancelRename()
  }

  return (
    <div style={style}>
      <div className={`session-item${isRenaming ? ' session-item-renaming' : ''}`}>
        {isRenaming ? (
          <div className="session-rename-form">
            <input
              type="text"
              value={renameValue}
              onChange={(e) => setRenameValue(e.target.value)}
              onKeyDown={handleKeyDown}
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
                className={`btn btn-icon${deleteConfirmId === s.session_id ? ' btn-deleting' : ''}`}
                onClick={() => onRequestDelete(s.session_id)}
                title="Delete"
              >
                🗑️
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  )
})

function formatDate(isoStr) {
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

export default function SessionList({ sessions, onNew, onOpenTab, onDelete, onRename }) {
  const [renamingId, setRenamingId] = useState(null)
  const [renameValue, setRenameValue] = useState('')
  const [deleteConfirmId, setDeleteConfirmId] = useState(null)
  const [listHeight, setListHeight] = useState(300)
  const panelRef = useRef(null)
  const headerRef = useRef(null)
  const mountTimerRef = useRef(null)


  // Profile initial render of the virtual list
  useEffect(() => {
    if (!mountTimerRef.current) {
      console.time('SessionList.mount')
      mountTimerRef.current = true
    }
    return () => { mountTimerRef.current = null }
  }, [])

  // Measure available height for the virtual list
  useEffect(() => {
    const panel = panelRef.current
    if (!panel) return

    const measure = () => {
      const headerEl = headerRef.current
      const headerH = headerEl ? headerEl.offsetHeight : 130
      const panelStyle = window.getComputedStyle(panel)
      const padTop = parseFloat(panelStyle.paddingTop) || 0
      const padBot = parseFloat(panelStyle.paddingBottom) || 0
      const available = panel.clientHeight - headerH - padTop - padBot
      setListHeight(Math.max(available, 100))
    }

    // Use ResizeObserver to catch layout changes (sidebar toggle, etc.)
    const observer = new ResizeObserver(measure)
    observer.observe(panel)

    // Also measure on session count change
    measure()

    return () => observer.disconnect()
  }, [sessions.length])

  // Profile once list has items and measured height > 0
  useEffect(() => {
    if (sessions.length > 0 && listHeight > 0 && mountTimerRef.current) {
      console.timeEnd('SessionList.mount')
      mountTimerRef.current = false
    }
  }, [sessions.length, listHeight])

  const startRename = (sessionId, currentName) => {
    setRenamingId(sessionId)
    setRenameValue(currentName || '')
  }

  const onRequestDelete = (sessionId) => {
    setDeleteConfirmId(sessionId)
  }

  const onConfirmDelete = () => {
    if (deleteConfirmId) {
      onDelete(deleteConfirmId)
    }
    setDeleteConfirmId(null)
  }

  const onCancelDelete = () => {
    setDeleteConfirmId(null)
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

  // Memoize rowProps to avoid unnecessary re-renders of rows
  const rowProps = useMemo(() => ({
    sessions,
    renamingId,
    renameValue,
    onOpenTab,
    onDelete,
    startRename,
    submitRename,
    cancelRename,
    setRenameValue,
    deleteConfirmId,
    onRequestDelete,
  }), [sessions, renamingId, renameValue, onOpenTab, onDelete, onRename, deleteConfirmId, onRequestDelete])

  return (
    <div className="session-list-panel" ref={panelRef}>
      <div ref={headerRef}>
        <div className="session-list-header">
          <h3>Sessions</h3>
        </div>
        <div className="session-list-actions">
        <button className="btn btn-new" onClick={onNew}>
          ✨ New Session
        </button>
      </div>
      </div>

      {sessions.length === 0 ? (
        <p className="session-list-empty">No saved sessions yet.</p>
      ) : (
        <List
          rowComponent={Row}
          rowCount={sessions.length}
          rowHeight={ITEM_HEIGHT}
          rowProps={rowProps}
          style={{ height: listHeight }}
          overscanCount={3}

        />
      )}

      {/* Delete confirmation overlay */}
      {deleteConfirmId && (
        <div className="delete-confirm-overlay" onClick={onCancelDelete}>
          <div className="delete-confirm-dialog" onClick={(e) => e.stopPropagation()}>
            <p>Are you sure you want to delete this session?</p>
            <div className="delete-confirm-actions">
              <button className="btn btn-sm" onClick={onCancelDelete}>
                Cancel
              </button>
              <button className="btn btn-sm btn-delete-confirm" onClick={onConfirmDelete}>
                Delete
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
