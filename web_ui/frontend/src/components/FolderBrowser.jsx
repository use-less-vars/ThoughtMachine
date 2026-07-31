import React, { useState, useEffect, useCallback } from 'react'

const API_BASE = ''

// ── Cross-platform path helpers ──────────────────────────────────────────────
// Handles POSIX (/home/foo), Windows (C:\Users\foo) and UNC (\\server\share)
// paths so "Up", breadcrumbs and folder navigation work identically on
// Windows and macOS/Linux.  (Windows polish sprint: the old code split on '/'
// only, which made C:\Users\foo's parent collapse to '/'.)
const isWindowsPath = (p) => /^[A-Za-z]:[\\/]/.test(p) || p.startsWith('\\\\')

function getParentPath(path) {
  if (!path) return null
  const p = String(path)
  // Roots have no parent
  if (p === '/') return null
  if (/^[A-Za-z]:[\\/]?$/.test(p)) return null                        // C:\  or  C:
  if (/^\\\\[^\\/]+[\\/][^\\/]+[\\/]?$/.test(p)) return null    // \\server\share
  const isWin = isWindowsPath(p)
  const norm = p.replace(/\\/g, '/').replace(/\/+$/, '')
  const idx = norm.lastIndexOf('/')
  if (idx < 0) return null
  let parent = norm.slice(0, idx)
  if (isWin) {
    parent = parent.replace(/\//g, '\\')
    if (/^[A-Za-z]:$/.test(parent)) parent += '\\'   // C:  →  C:\
  } else {
    parent = parent === '' ? '/' : parent
  }
  return parent
}

function joinPath(base, name) {
  if (!base) return name
  const b = String(base).replace(/[\\/]+$/, '')
  return b + (isWindowsPath(b) ? '\\' : '/') + name
}

function buildBreadcrumbs(path) {
  if (!path) return []
  const p = String(path)
  const isWin = isWindowsPath(p)
  const norm = p.replace(/\\/g, '/').replace(/\/+$/, '')
  const crumbs = []
  if (isWin) {
    const driveMatch = norm.match(/^([A-Za-z]:)(.*)$/)
    if (driveMatch) {
      // Windows drive: C:\ → C:\Users → C:\Users\foo
      let acc = driveMatch[1] + '\\'
      crumbs.push({ name: driveMatch[1], path: acc, sep: '' })
      for (const part of driveMatch[2].split('/').filter(Boolean)) {
        acc += part + '\\'
        crumbs.push({ name: part, path: acc, sep: '\\' })
      }
    } else if (norm.startsWith('//')) {
      // UNC share: \\server\share → first crumb is the server root
      const parts = norm.split('/').filter(Boolean)
      let acc = '\\\\'
      for (let i = 0; i < parts.length; i++) {
        acc += parts[i] + '\\'
        crumbs.push({ name: i === 0 ? '\\\\' + parts[i] : parts[i], path: acc, sep: i === 0 ? '' : '\\' })
      }
    }
  } else {
    // POSIX: / → /home → /home/foo
    crumbs.push({ name: '/', path: '/', sep: '' })
    let acc = ''
    for (const part of norm.split('/').filter(Boolean)) {
      acc += '/' + part
      crumbs.push({ name: part, path: acc, sep: '/' })
    }
  }
  return crumbs
}

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
    if (!currentPath) return
    const parent = getParentPath(currentPath)
    if (!parent) return   // already at a root (/, C:\ or \\server\share)
    navigateTo(parent)
  }, [currentPath, navigateTo])

  // Create a new folder inside the currently viewed directory (New Folder button)
  const handleCreateFolder = useCallback(async () => {
    if (!currentPath) return
    const name = window.prompt('New folder name:', '')
    if (!name) return
    const trimmed = name.trim()
    if (!trimmed) return
    try {
      const res = await fetch(`${API_BASE}/api/browse/create`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ parent_path: currentPath, name: trimmed }),
      })
      const data = await res.json()
      if (data.success) {
        setError('')
        navigateTo(currentPath)  // refresh listing to reveal the new folder
      } else {
        setError(data.error || 'Failed to create folder')
      }
    } catch {
      setError('Network error while creating folder')
    }
  }, [currentPath, navigateTo])

  // Build breadcrumb segments from current path (cross-platform)
  const breadcrumbs = buildBreadcrumbs(currentPath)

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
        {breadcrumbs.map((seg, i) => (
          <React.Fragment key={seg.path}>
            {i > 0 && <span style={separatorStyle}>{seg.sep || '/'}</span>}
            <button
              style={breadcrumbBtnStyle}
              onClick={() => navigateTo(seg.path)}
              title={seg.path}
            >
              {seg.name}
            </button>
          </React.Fragment>
        ))}
        <span style={{ flex: 1 }} />
        {currentPath && (
          <button
            style={breadcrumbBtnStyle}
            onClick={handleCreateFolder}
            title={`Create a new folder in ${currentPath}`}
          >
            ＋ New Folder
          </button>
        )}
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
          {currentPath && getParentPath(currentPath) && (
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
                    onClick={() => navigateTo(joinPath(currentPath, entry.name))}
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
