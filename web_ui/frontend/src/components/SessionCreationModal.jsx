import React, { useState, useEffect, useCallback } from 'react'

const API_BASE = ''

const MODES = [
  { id: 'agent',    label: 'Agent',    desc: 'Full tools, no worker' },
  { id: 'engineer', label: 'Engineer', desc: 'Delegation only' },
  { id: 'custom',   label: 'Custom',   desc: 'Your tools, your prompt' },
]

export default function SessionCreationModal({ show, onCreate, onCancel, isFirstLaunch }) {
  const [mode, setMode] = useState(() => {
    return localStorage.getItem('thoughtmachine_last_mode') || 'engineer'
  })
  const [workspaces, setWorkspaces] = useState([])
  const [selectedWorkspaceId, setSelectedWorkspaceId] = useState(null)
  const [customPath, setCustomPath] = useState('')
  const [useCustomPath, setUseCustomPath] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [acknowledgedRisk, setAcknowledgedRisk] = useState(false)
  const [workspacesLoading, setWorkspacesLoading] = useState(false)
  const [showNewFolder, setShowNewFolder] = useState(false)
  const [newFolderPath, setNewFolderPath] = useState('')

  // Fetch workspace list on mount
  useEffect(() => {
    if (!show) return
    setWorkspacesLoading(true)
    fetch(`${API_BASE}/api/workspace/list`)
      .then(r => r.json())
      .then(data => {
        const list = data.workspaces || data || []
        setWorkspaces(list)
        if (list.length > 0 && !selectedWorkspaceId) {
          setSelectedWorkspaceId(list[0].id)
        }
      })
      .catch(() => {
        setWorkspaces([])
      })
      .finally(() => setWorkspacesLoading(false))
  }, [show])

  // Sorted workspaces by folder name
  const sortedWorkspaces = React.useMemo(() => {
    return [...workspaces].sort((a, b) => {
      const nameA = (a.label || (a.root ? a.root.split('/').filter(Boolean).pop() : '') || a.id || '').toLowerCase()
      const nameB = (b.label || (b.root ? b.root.split('/').filter(Boolean).pop() : '') || b.id || '').toLowerCase()
      return nameA.localeCompare(nameB)
    })
  }, [workspaces])

  // Folder name counts for disambiguation
  const folderNameCounts = React.useMemo(() => {
    const counts = {}
    workspaces.forEach(w => {
      const name = w.label || (w.root ? w.root.split('/').filter(Boolean).pop() : '') || '?'
      counts[name] = (counts[name] || 0) + 1
    })
    return counts
  }, [workspaces])

  const getOptionLabel = (w) => {
    const folderName = w.label || (w.root ? w.root.split('/').filter(Boolean).pop() : '') || w.id || '?'
    const count = folderNameCounts[folderName] || 1
    return count > 1 ? `${folderName} (${w.id})` : folderName
  }

  // Derived: current workspace path
  const currentWorkspace = useCustomPath
    ? { id: null, root: customPath, label: customPath.split('/').filter(Boolean).pop() || customPath }
    : workspaces.find(w => w.id === selectedWorkspaceId) || null

  const workspacePath = currentWorkspace?.root || ''

  function isSensitivePath(path) {
    const parts = path.replace(/\\/g, '/').split('/').filter(Boolean)
    return parts.some(p => p === '.thoughtmachine')
  }

  // Sensitive directory check
  const isSensitive = (() => {
    if (!workspacePath) return false
    const lower = workspacePath.toLowerCase()
    if (isSensitivePath(lower)) return true
    if (lower === '/root' || lower.startsWith('/home/') || lower === '/home') return true
    return false
  })()

  // Reset acknowledgment when workspace changes
  useEffect(() => {
    setAcknowledgedRisk(false)
  }, [workspacePath])

  const handleCreate = async () => {
    setLoading(true)
    setError('')
    try {
      const workspaceId = useCustomPath ? null : selectedWorkspaceId
      const workspacePath = useCustomPath ? customPath : null
      localStorage.setItem('thoughtmachine_last_mode', mode)
      localStorage.setItem('thoughtmachine_last_workspace', selectedWorkspaceId || customPath)
      await onCreate(mode, workspaceId, workspacePath)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  const handleUseCustomPath = () => {
    if (customPath && customPath.trim()) {
      const match = workspaces.find(w => customPath.trim().startsWith(w.root) || w.root.endsWith(customPath.trim()))
      if (match) {
        setSelectedWorkspaceId(match.id)
        setUseCustomPath(false)
      } else {
        setUseCustomPath(true)
      }
    }
  }

  const handleCreateNewFolder = () => {
    if (newFolderPath && newFolderPath.trim()) {
      setCustomPath(newFolderPath.trim())
      setUseCustomPath(true)
      setShowNewFolder(false)
      setNewFolderPath('')
    }
  }

  if (!show) return null

  const canCreate = !loading && (!isSensitive || acknowledgedRisk) && (selectedWorkspaceId || customPath)

  return (
    <div
      className="modal-overlay"
      onClick={() => { if (!isFirstLaunch) onCancel() }}
      style={{
        position: 'fixed',
        top: 0, left: 0, right: 0, bottom: 0,
        background: 'rgba(0,0,0,0.6)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        zIndex: 1000,
      }}
    >
      <div
        className="modal-dialog"
        onClick={e => e.stopPropagation()}
        style={{
          background: 'var(--bg-surface, #313244)',
          border: '1px solid var(--border, #585b70)',
          borderRadius: 'var(--radius, 8px)',
          padding: '1.5rem',
          width: '440px',
          maxWidth: '90vw',
          maxHeight: '80vh',
          overflowY: 'auto',
          boxShadow: '0 4px 20px rgba(0,0,0,0.4)',
        }}
      >
        <h3 style={{ margin: '0 0 1rem', color: 'var(--text-primary, #cdd6f4)', fontSize: '1.1rem' }}>
          {isFirstLaunch ? 'Welcome! Create your first session' : 'New Session'}
        </h3>

        {/* Mode selector */}
        <div style={{ marginBottom: '1.2rem' }}>
          <label style={{ display: 'block', marginBottom: '0.5rem', color: 'var(--text-secondary, #a6adc8)', fontSize: '0.85rem', fontWeight: 600 }}>
            Mode
          </label>
          <div style={{ display: 'flex', gap: '0.5rem' }}>
            {MODES.map(m => (
              <button
                key={m.id}
                onClick={() => setMode(m.id)}
                style={{
                  flex: 1,
                  padding: '0.6rem 0.4rem',
                  border: mode === m.id ? '2px solid var(--accent, #89b4fa)' : '1px solid var(--border, #585b70)',
                  borderRadius: '6px',
                  background: mode === m.id ? 'rgba(137,180,250,0.1)' : 'transparent',
                  color: mode === m.id ? 'var(--accent, #89b4fa)' : 'var(--text-primary, #cdd6f4)',
                  cursor: 'pointer',
                  textAlign: 'center',
                  fontSize: '0.8rem',
                  lineHeight: 1.3,
                }}
              >
                <div style={{ fontWeight: 600 }}>{m.label}</div>
                <div style={{ fontSize: '0.7rem', opacity: 0.7, marginTop: '0.15rem' }}>{m.desc}</div>
              </button>
            ))}
          </div>
        </div>

        {/* Workspace selector */}
        <div style={{ marginBottom: '1.2rem' }}>
          <label style={{ display: 'block', marginBottom: '0.5rem', color: 'var(--text-secondary, #a6adc8)', fontSize: '0.85rem', fontWeight: 600 }}>
            Workspace
          </label>

          <div style={{ display: 'flex', gap: '0.4rem', marginBottom: '0.4rem' }}>
            <select
              value={showNewFolder ? '__new__' : (useCustomPath ? '__custom__' : (selectedWorkspaceId || ''))}
              onChange={e => {
                const val = e.target.value
                if (val === '__custom__') {
                  setUseCustomPath(true)
                  setShowNewFolder(false)
                } else if (val === '__new__') {
                  // handled by the button, but prevent stray selection
                } else {
                  setUseCustomPath(false)
                  setShowNewFolder(false)
                  setSelectedWorkspaceId(val)
                }
              }}
              disabled={workspacesLoading}
              style={{
                flex: 1,
                padding: '0.5rem',
                background: 'var(--bg-primary, #1e1e2e)',
                color: 'var(--text-primary, #cdd6f4)',
                border: '1px solid var(--border, #585b70)',
                borderRadius: '4px',
                fontSize: '0.85rem',
              }}
            >
              {workspacesLoading ? (
                <option>Loading workspaces...</option>
              ) : sortedWorkspaces.length === 0 ? (
                <option value="">No workspaces found</option>
              ) : (
                sortedWorkspaces.map(w => (
                  <option key={w.id} value={w.id}>
                    {getOptionLabel(w)}
                  </option>
                ))
              )}
              {sortedWorkspaces.length > 0 && <option value="__custom__">\u2500\u2500 Browse custom folder \u2500\u2500</option>}
            </select>
            <button
              onClick={() => {
                setShowNewFolder(!showNewFolder)
                if (showNewFolder) {
                  setNewFolderPath('')
                } else {
                  setUseCustomPath(false)
                }
              }}
              style={{
                padding: '0.5rem 0.75rem',
                background: showNewFolder ? 'var(--danger, #f38ba8)' : 'var(--accent, #89b4fa)',
                color: '#fff',
                border: 'none',
                borderRadius: '4px',
                cursor: 'pointer',
                fontSize: '0.8rem',
                whiteSpace: 'nowrap',
              }}
            >
              {showNewFolder ? 'Cancel' : '+ New Folder'}
            </button>
          </div>

          {/* New Folder inline input */}
          {showNewFolder && (
            <div style={{ display: 'flex', gap: '0.4rem', alignItems: 'center', marginTop: '0.2rem', marginBottom: '0.4rem' }}>
              <input
                type="text"
                value={newFolderPath}
                onChange={e => setNewFolderPath(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') handleCreateNewFolder() }}
                placeholder="/home/user/my-new-project"
                autoFocus
                style={{
                  flex: 1,
                  padding: '0.5rem',
                  background: 'var(--bg-primary, #1e1e2e)',
                  color: 'var(--text-primary, #cdd6f4)',
                  border: '1px solid var(--accent, #89b4fa)',
                  borderRadius: '4px',
                  fontSize: '0.85rem',
                }}
              />
              <button
                onClick={handleCreateNewFolder}
                disabled={!newFolderPath.trim()}
                style={{
                  padding: '0.5rem 0.75rem',
                  background: 'var(--accent, #89b4fa)',
                  color: '#fff',
                  border: 'none',
                  borderRadius: '4px',
                  cursor: newFolderPath.trim() ? 'pointer' : 'not-allowed',
                  fontSize: '0.8rem',
                  whiteSpace: 'nowrap',
                  opacity: newFolderPath.trim() ? 1 : 0.6,
                }}
              >
                Create & Select
              </button>
            </div>
          )}

          {/* Custom path inline input (when __custom__ selected and not in new folder mode) */}
          {useCustomPath && !showNewFolder ? (
            <div style={{ display: 'flex', gap: '0.4rem', alignItems: 'center', marginTop: '0.2rem' }}>
              <input
                type="text"
                value={customPath}
                onChange={e => setCustomPath(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') handleUseCustomPath() }}
                placeholder="/home/user/my-new-project"
                style={{
                  flex: 1,
                  padding: '0.5rem',
                  background: 'var(--bg-primary, #1e1e2e)',
                  color: 'var(--text-primary, #cdd6f4)',
                  border: '1px solid var(--border, #585b70)',
                  borderRadius: '4px',
                  fontSize: '0.85rem',
                }}
              />
              <button
                onClick={handleUseCustomPath}
                style={{
                  padding: '0.5rem 0.75rem',
                  background: 'var(--accent, #89b4fa)',
                  color: '#fff',
                  border: 'none',
                  borderRadius: '4px',
                  cursor: 'pointer',
                  fontSize: '0.8rem',
                  whiteSpace: 'nowrap',
                }}
              >
                Use this path
              </button>
            </div>
          ) : !showNewFolder && currentWorkspace ? (
            <div style={{ fontSize: '0.75rem', color: 'var(--text-muted, #6c7086)', marginTop: '0.2rem' }}>
              \ud83d\udcc1 {currentWorkspace.root || 'Unknown path'}
            </div>
          ) : null}
        </div>

        {/* Sensitive directory warning */}
        {isSensitive && (
          <div style={{
            background: 'rgba(249,226,175,0.15)',
            border: '1px solid rgba(249,226,175,0.4)',
            borderRadius: '6px',
            padding: '0.75rem',
            marginBottom: '1rem',
          }}>
            <div style={{ color: '#f9e2af', fontSize: '0.85rem', fontWeight: 600, marginBottom: '0.4rem' }}>
              ⚠️ ThoughtMachine Config Warning
            </div>
            <p style={{ color: '#fab387', fontSize: '0.8rem', margin: '0 0 0.5rem', lineHeight: 1.4 }}>
              This workspace contains your ThoughtMachine configuration files. The agent will be able to read and modify its own settings, which could be a security risk if combined with internet access or untrusted tools.
            </p>
            <label style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', cursor: 'pointer', fontSize: '0.8rem', color: '#cdd6f4' }}>
              <input
                type="checkbox"
                checked={acknowledgedRisk}
                onChange={e => setAcknowledgedRisk(e.target.checked)}
              />
              I understand the risks and want to proceed
            </label>
          </div>
        )}

        {/* Error message */}
        {error && (
          <div style={{ color: 'var(--danger, #f38ba8)', fontSize: '0.85rem', marginBottom: '0.75rem', padding: '0.4rem 0.6rem', background: 'rgba(243,139,168,0.1)', borderRadius: '4px' }}>
            \u26a0 {error}
          </div>
        )}

        {/* Action buttons */}
        <div style={{ display: 'flex', gap: '0.5rem', justifyContent: 'flex-end' }}>
          {!isFirstLaunch && (
            <button
              onClick={onCancel}
              disabled={loading}
              style={{
                padding: '0.5rem 1rem',
                background: 'transparent',
                color: 'var(--text-primary, #cdd6f4)',
                border: '1px solid var(--border, #585b70)',
                borderRadius: '4px',
                cursor: loading ? 'not-allowed' : 'pointer',
                fontSize: '0.85rem',
                opacity: loading ? 0.6 : 1,
              }}
            >
              Cancel
            </button>
          )}
          <button
            onClick={handleCreate}
            disabled={!canCreate}
            style={{
              padding: '0.5rem 1rem',
              background: canCreate ? 'var(--accent, #89b4fa)' : 'var(--text-muted, #6c7086)',
              color: '#fff',
              border: 'none',
              borderRadius: '4px',
              cursor: canCreate ? 'pointer' : 'not-allowed',
              fontSize: '0.85rem',
              fontWeight: 600,
              opacity: canCreate ? 1 : 0.6,
            }}
          >
            {loading ? 'Creating...' : 'Create Session'}
          </button>
        </div>
      </div>
    </div>
  )
}
