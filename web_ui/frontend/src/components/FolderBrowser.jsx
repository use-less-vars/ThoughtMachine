import React, { useState, useEffect, useCallback } from 'react'

const API_BASE = ''

export default function FolderBrowser({ onSelect, startPath, onNavigate }) {
  const [currentPath, setCurrentPath] = useState(null)
  const [homePath, setHomePath] = useState(null)
  const [entries, setEntries] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  // Notify parent when navigated path changes
  const notifyPath = useCallback((path) => {
    if (onNavigate && path) onNavigate(path)
  }, [onNavigate])

  // Fetch initial directory
  useEffect(() => {
    if (startPath) {
      // Fetch homePath (needed for "Select This Folder" logic) while navigating
      fetch(`${API_BASE}/api/user-home`)
        .then(r => r.json())
        .then(data => {
          if (data.home) setHomePath(data.home)
        })
        .catch(() => {})
      navigateTo(startPath)
    } else {
      setLoading(true)
      fetch(`${API_BASE}/api/user-home`)
        .then(r => r.json())
        .then(data => {
          if (data.home) {
            setHomePath(data.home)
            navigateTo(data.home)
          } else {
            setError('Could not determine home directory')
            setLoading(false)
          }
        })
        .catch(() => {
          setError('Failed to load home directory')
          setLoading(false)
        })
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const navigateTo = useCallback((path) => {
    if (!path) return
    setLoading(true)
    setError('')
    fetch(`${API_BASE}/api/browse?path=${encodeURIComponent(path)}`)
      .then(r => r.json())
      .then(data => {
        if (data.success) {
          setCurrentPath(data.current_path)
          notifyPath(data.current_path)
          // Show directories only, filter hidden (starting with .), sorted
          const dirs = (data.entries || [])
            .filter(e => e.is_dir && !e.name.startsWith('.'))
            .sort((a, b) => a.name.localeCompare(b.name))
          setEntries(dirs)
        } else {
          setError(data.error || 'Failed to browse directory')
          setEntries([])
        }
      })
      .catch(() => setError('Network error while browsing'))
      .finally(() => setLoading(false))
  }, [])

  const navigateUp = useCallback(() => {
    if (!currentPath || currentPath === '/') return
    const parent = currentPath.replace(/\/+$/, '').split('/').slice(0, -1).join('/') || '/'
    navigateTo(parent)
  }, [currentPath, navigateTo])

  // Build breadcrumb segments from current path
  const breadcrumbs = []
  if (currentPath) {
    const parts = currentPath.replace(/\/+$/, '').split('/').filter(Boolean)
    let accumulated = ''
    for (const part of parts) {
      accumulated += '/' + part
      breadcrumbs.push({ name: part, path: accumulated })
    }
  }

  // Always show "Select This Folder" when a folder is being viewed
  const showSelectButton = !!currentPath

  const containerStyle = {
    background: 'var(--bg-primary, #1e1e2e)',
    border: '1px solid var(--border, #45475a)',
    borderRadius: '6px',
    overflow: 'hidden',
  }

  const breadcrumbStyle = {
    display: 'flex',
    flexWrap: 'wrap',
    alignItems: 'center',
    gap: '0.15rem',
    padding: '0.5rem 0.6rem',
    background: 'var(--bg-secondary, #181825)',
    borderBottom: '1px solid var(--border, #45475a)',
    fontSize: '0.8rem',
    fontFamily: 'var(--font-mono, monospace)',
    minHeight: '32px',
  }

  const breadcrumbBtnStyle = {
    background: 'none',
    border: 'none',
    color: 'var(--accent, #89b4fa)',
    cursor: 'pointer',
    padding: '0.1rem 0.25rem',
    borderRadius: '3px',
    fontSize: '0.8rem',
    fontFamily: 'inherit',
    whiteSpace: 'nowrap',
  }

  const separatorStyle = {
    color: 'var(--text-muted, #6c7086)',
    margin: '0 0.1rem',
    userSelect: 'none',
  }

  const listStyle = {
    maxHeight: '240px',
    overflowY: 'auto',
    padding: '0.25rem 0',
  }

  const entryRowStyle = {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    padding: '0.3rem 0.6rem',
    cursor: 'pointer',
    borderRadius: '3px',
    transition: 'background 0.1s',
  }

  const entryNameStyle = {
    color: 'var(--text-primary, #cdd6f4)',
    fontSize: '0.85rem',
    display: 'flex',
    alignItems: 'center',
    gap: '0.35rem',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
    flex: 1,
    minWidth: 0,
  }

  const upBtnOuterStyle = {
    display: 'flex',
    alignItems: 'center',
    gap: '0.35rem',
    width: '100%',
    padding: '0.35rem 0.6rem',
    background: 'var(--bg-secondary, #181825)',
    border: 'none',
    borderBottom: '1px solid var(--border, #45475a)',
    color: 'var(--text-secondary, #a6adc8)',
    fontSize: '0.8rem',
    fontStyle: 'italic',
    cursor: 'pointer',
    textAlign: 'left',
  }

  const selectBtnStyle = {
    display: 'block',
    width: 'calc(100% - 1.2rem)',
    margin: '0.5rem 0.6rem',
    padding: '0.5rem 0',
    background: 'var(--accent, #89b4fa)',
    color: '#fff',
    border: 'none',
    borderRadius: '5px',
    cursor: 'pointer',
    fontSize: '0.85rem',
    fontWeight: 600,
    textAlign: 'center',
  }

  const loadingStyle = {
    padding: '1.5rem',
    textAlign: 'center',
    color: 'var(--text-secondary, #a6adc8)',
    fontSize: '0.85rem',
  }

  const errorStyle = {
    padding: '0.5rem 0.6rem',
    color: 'var(--danger, #f38ba8)',
    fontSize: '0.8rem',
    background: 'rgba(243,139,168,0.1)',
  }

  const emptyStyle = {
    padding: '1.5rem',
    textAlign: 'center',
    color: 'var(--text-muted, #6c7086)',
    fontSize: '0.85rem',
  }

  return (
    <div style={containerStyle}>
      {/* Breadcrumb navigation */}
      <div style={breadcrumbStyle}>
        <button
          style={breadcrumbBtnStyle}
          onClick={() => navigateTo('/')}
          title="Root directory"
        >
          /
        </button>
        {breadcrumbs.map((seg, i) => (
          <React.Fragment key={seg.path}>
            <span style={separatorStyle}>/</span>
            <button
              style={breadcrumbBtnStyle}
              onClick={() => navigateTo(seg.path)}
              title={seg.path}
            >
              {seg.name}
            </button>
          </React.Fragment>
        ))}
      </div>

      {/* Error message */}
      {error && !loading && (
        <div style={errorStyle}>⚠ {error}</div>
      )}

      {/* Loading spinner */}
      {loading && (
        <div style={loadingStyle}>
          <span style={{ opacity: 0.7 }}>⟳</span> Loading...
        </div>
      )}

      {/* Directory listing */}
      {!loading && !error && (
        <>
          {/* Separate "Up" button above the list */}
          {currentPath && currentPath !== '/' && (
            <button
              style={upBtnOuterStyle}
              onClick={navigateUp}
            >
              ↑ Parent
            </button>
          )}

          <div style={listStyle}
            onMouseOver={e => {
              if (e.target.closest('[data-row]')) {
                const row = e.target.closest('[data-row]')
                const all = row.parentElement.querySelectorAll('[data-row]')
                all.forEach(r => r.style.background = 'transparent')
                row.style.background = 'rgba(137,180,250,0.08)'
              }
            }}
          >
            {entries.length === 0 ? (
              <div style={emptyStyle}>
                (empty directory — no subfolders)
              </div>
            ) : (
              entries.map(entry => (
                <div
                  key={entry.name}
                  data-row={entry.name}
                  style={entryRowStyle}
                  onMouseEnter={e => e.currentTarget.style.background = 'rgba(137,180,250,0.08)'}
                  onMouseLeave={e => e.currentTarget.style.background = 'transparent'}
                >
                  <span
                    style={entryNameStyle}
                    onClick={() => navigateTo(currentPath.replace(/\/+$/, '') + '/' + entry.name)}
                  >
                    📁 {entry.name}
                  </span>
                </div>
              ))
            )}
          </div>

          {/* "Select This Folder" button */}
          {showSelectButton && (
            <button
              style={selectBtnStyle}
              onClick={() => onSelect(currentPath)}
            >
              Select This Folder
            </button>
          )}
        </>
      )}
    </div>
  )
}
