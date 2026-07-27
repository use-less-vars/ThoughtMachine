import React, { useState, useEffect } from 'react'
import FolderBrowser from './FolderBrowser'
import WorkspaceSessionList from './WorkspaceSessionList'

const API_BASE = ''

const MODES = [
  { id: 'agent', label: 'Agent', desc: 'Full tools, no worker' },
  { id: 'engineer', label: 'Engineer', desc: 'Delegation only' },
  { id: 'custom', label: 'Custom', desc: 'Your tools, your prompt' },
]

export default function SessionCreationModal({ show, onCreate, onOpen, onCancel, isFirstLaunch }) {
  const [step, setStep] = useState(1)
  const [mode, setMode] = useState(() => {
    return localStorage.getItem('thoughtmachine_last_mode') || 'engineer'
  })
  const [selectedFolderPath, setSelectedFolderPath] = useState(null)
  const [workspaceId, setWorkspaceId] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [acknowledgedRisk, setAcknowledgedRisk] = useState(false)
  const [resolving, setResolving] = useState(false)
  const [homeDir, setHomeDir] = useState(null)
  const [showModePicker, setShowModePicker] = useState(false)
  const [currentBrowsePath, setCurrentBrowsePath] = useState(null)

  // Fetch home directory for vault warning — must resolve before folder browser renders
  useEffect(() => {
    if (!show) return
    fetch(`${API_BASE}/api/user-home`)
      .then(r => r.json())
      .then(data => {
        setHomeDir(data.home || null)
      })
      .catch(() => {
        setHomeDir(null)
      })
  }, [show])

  // Reset when modal opens
  useEffect(() => {
    if (show) {
      setStep(1)
      setWorkspaceId(null)
      setError('')
      setAcknowledgedRisk(false)
      setShowModePicker(false)
      function isAbsolutePath(p) {
        return p.startsWith('/') || /^[A-Za-z]:[\\/]/.test(p)
      }
      const stored = localStorage.getItem('thoughtmachine_last_workspace')
      if (stored && isAbsolutePath(stored)) {
        setSelectedFolderPath(stored)
      } else {
        setSelectedFolderPath(null)
      }
    }
  }, [show])

  // Derived
  const workspacePath = selectedFolderPath || ''
  const vaultPath = homeDir ? homeDir.replace(/\/+$/, '') + '/.thoughtmachine' : ''

  // Is a given path sensitive (home dir or vault path)?
  const isPathSensitive = (path) => {
    if (!path) return false
    const normalized = path.replace(/\/+$/, '')
    if (homeDir && normalized === homeDir.replace(/\/+$/, '')) return true
    if (vaultPath && normalized === vaultPath.replace(/\/+$/, '')) return true
    return false
  }

  // Sensitive checks
  const isBrowsedSensitive = isPathSensitive(currentBrowsePath)  // Step 1 warning (dynamic)
  const isSensitive = isPathSensitive(workspacePath)             // Step 2 mode picker (committed)

  // Reset acknowledgment when workspace changes
  useEffect(() => {
    setAcknowledgedRisk(false)
  }, [workspacePath])

  // Handle folder selection
  const handleFolderSelect = async (path) => {
    setSelectedFolderPath(path)
    localStorage.setItem('thoughtmachine_last_workspace', path || '')
    setAcknowledgedRisk(false)
    setError('')

    // If not sensitive — resolve immediately and go to Step 2
    if (!isPathSensitive(path)) {
      setResolving(true)
      try {
        const r = await fetch(`${API_BASE}/api/workspace/resolve`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path }),
        })
        if (!r.ok) throw new Error('Failed to resolve workspace')
        const data = await r.json()
        setWorkspaceId(data.workspace_id)
        setShowModePicker(false)
        setStep(2)
      } catch (err) {
        setError(err.message || 'Failed to resolve workspace')
      } finally {
        setResolving(false)
      }
    }
    // If sensitive — stay in Step 1, warning will appear below
  }

  // Proceed after acknowledging sensitive warning
  const handleProceedSensitive = async () => {
    setResolving(true)
    setError('')
    try {
      const r = await fetch(`${API_BASE}/api/workspace/resolve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: currentBrowsePath }),
      })
      if (!r.ok) throw new Error('Failed to resolve workspace')
      const data = await r.json()
      setWorkspaceId(data.workspace_id)
      setSelectedFolderPath(currentBrowsePath)
      localStorage.setItem('thoughtmachine_last_workspace', currentBrowsePath || '')
      setShowModePicker(false)
      setStep(2)
    } catch (err) {
      setError(err.message || 'Failed to resolve workspace')
    } finally {
      setResolving(false)
    }
  }

  const handleCreateNew = async () => {
    setLoading(true)
    setError('')
    try {
      localStorage.setItem('thoughtmachine_last_mode', mode)
      await onCreate(mode, workspaceId, selectedFolderPath)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  const handleOpenSession = (sessionId) => {
    if (onOpen) onOpen(sessionId)
  }

  const handleBack = () => {
    setStep(1)
    setWorkspaceId(null)
    setShowModePicker(false)
  }

  if (!show) return null

  const canCreateFromMode = !loading && (!isSensitive || acknowledgedRisk)

  const overlayStyle = {
    position: 'fixed',
    top: 0, left: 0, right: 0, bottom: 0,
    background: 'rgba(0,0,0,0.6)',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    zIndex: 1000,
  }

  const dialogStyle = {
    background: 'var(--bg-surface, #313244)',
    border: '1px solid var(--border, #585b70)',
    borderRadius: 'var(--radius, 8px)',
    padding: '1.5rem',
    width: '500px',
    maxWidth: '90vw',
    maxHeight: '80vh',
    overflowY: 'auto',
    boxShadow: '0 4px 20px rgba(0,0,0,0.4)',
  }

  const titleStyle = {
    margin: '0 0 1rem',
    color: 'var(--text-primary, #cdd6f4)',
    fontSize: '1.1rem',
  }

  const stepIndicatorStyle = {
    display: 'flex',
    gap: '0.5rem',
    marginBottom: '1rem',
    alignItems: 'center',
  }

  const stepDotStyle = (active) => ({
    padding: '0.15rem 0.6rem',
    borderRadius: '10px',
    fontSize: '0.7rem',
    fontWeight: 600,
    background: active ? 'var(--accent, #89b4fa)' : 'var(--bg-primary, #1e1e2e)',
    color: active ? '#fff' : 'var(--text-muted, #6c7086)',
    border: active ? 'none' : '1px solid var(--border, #45475a)',
  })

  const stepLineStyle = {
    flex: 1,
    height: '1px',
    background: 'var(--border, #45475a)',
    maxWidth: '40px',
  }

  const labelStyle = {
    display: 'block',
    marginBottom: '0.5rem',
    color: 'var(--text-secondary, #a6adc8)',
    fontSize: '0.85rem',
    fontWeight: 600,
  }

  const errorBoxStyle = {
    color: 'var(--danger, #f38ba8)',
    fontSize: '0.85rem',
    marginBottom: '0.75rem',
    padding: '0.4rem 0.6rem',
    background: 'rgba(243,139,168,0.1)',
    borderRadius: '4px',
  }

  const folderPathStyle = {
    fontSize: '0.75rem',
    color: 'var(--text-muted, #6c7086)',
    marginBottom: '0.75rem',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
  }

  const btnBase = {
    padding: '0.5rem 1rem',
    borderRadius: '4px',
    fontSize: '0.85rem',
    fontWeight: 600,
    cursor: 'pointer',
    border: 'none',
  }

  const switchViewStyle = {
    display: 'flex',
    justifyContent: 'flex-end',
    marginTop: '0.75rem',
  }

  return (
    <div
      className="modal-overlay"
      onClick={() => { if (!isFirstLaunch) onCancel() }}
      style={overlayStyle}
    >
      <div
        className="modal-dialog"
        onClick={e => e.stopPropagation()}
        style={dialogStyle}
      >
        {/* Step indicator */}
        <div style={stepIndicatorStyle}>
          <span style={stepDotStyle(step === 1)}>1. Workspace</span>
          <div style={stepLineStyle} />
          <span style={stepDotStyle(step === 2)}>2. Session</span>
        </div>

        {/* ───────── STEP 1: Browse & Select ───────── */}
        {step === 1 && (
          <>
            <h3 style={titleStyle}>
              {isFirstLaunch ? 'Welcome! Create your first session' : 'New Session'}
            </h3>

            {/* Workspace folder browser — waits for homeDir to avoid warning timing race */}
            <div style={{ marginBottom: '0.5rem' }}>
              <label style={labelStyle}>Workspace Folder</label>
              {homeDir === null ? (
                <div style={{
                  padding: '1.5rem',
                  textAlign: 'center',
                  color: 'var(--text-muted, #6c7086)',
                  fontSize: '0.85rem',
                  background: 'var(--bg-primary, #1e1e2e)',
                  border: '1px solid var(--border, #45475a)',
                  borderRadius: '6px',
                }}>
                  ⟳ Loading...
                </div>
              ) : (
                <FolderBrowser
                  onSelect={handleFolderSelect}
                  onNavigate={setCurrentBrowsePath}
                  startPath={selectedFolderPath}
                />
              )}
            </div>

            {/* Resolving indicator */}
            {resolving && (
              <div style={{ fontSize: '0.85rem', color: 'var(--text-secondary, #a6adc8)', marginBottom: '0.75rem' }}>
                ⟳ Resolving workspace...
              </div>
            )}

            {/* Error message */}
            {error && (
              <div style={errorBoxStyle}>
                ⚠ {error}
              </div>
            )}

            {/* Vault warning — appears in Step 1 when viewing a sensitive folder */}
            {isBrowsedSensitive && currentBrowsePath && !resolving && (
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
                <p style={{ color: '#fab387', fontSize: '0.8rem', margin: '0 0 0.75rem', lineHeight: 1.4 }}>
                  This workspace contains your ThoughtMachine configuration files. The agent will be able to read and modify its own settings, which could be a security risk if combined with internet access or untrusted tools.
                </p>
                <label style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', cursor: 'pointer', fontSize: '0.8rem', color: '#cdd6f4', marginBottom: '0.75rem' }}>
                  <input
                    type="checkbox"
                    checked={acknowledgedRisk}
                    onChange={e => setAcknowledgedRisk(e.target.checked)}
                  />
                  I understand the risks and want to proceed
                </label>
                <div style={{ display: 'flex', gap: '0.5rem', justifyContent: 'flex-end' }}>
                  <button
                    onClick={() => {
                      setSelectedFolderPath(null)
                      setAcknowledgedRisk(false)
                    }}
                    style={{
                      padding: '0.4rem 0.8rem',
                      background: 'transparent',
                      color: 'var(--text-primary, #cdd6f4)',
                      border: '1px solid var(--border, #585b70)',
                      borderRadius: '4px',
                      cursor: 'pointer',
                      fontSize: '0.8rem',
                    }}
                  >
                    Cancel
                  </button>
                  <button
                    onClick={handleProceedSensitive}
                    disabled={!acknowledgedRisk}
                    style={{
                      padding: '0.4rem 0.8rem',
                      background: acknowledgedRisk ? 'var(--accent, #89b4fa)' : 'var(--text-muted, #6c7086)',
                      color: '#fff',
                      border: 'none',
                      borderRadius: '4px',
                      cursor: acknowledgedRisk ? 'pointer' : 'not-allowed',
                      fontSize: '0.8rem',
                      fontWeight: 600,
                      opacity: acknowledgedRisk ? 1 : 0.6,
                    }}
                  >
                    Proceed
                  </button>
                </div>
              </div>
            )}

            {/* Cancel button */}
            <div style={{ display: 'flex', gap: '0.5rem', justifyContent: 'flex-end' }}>
              {!isFirstLaunch && (
                <button
                  onClick={onCancel}
                  disabled={loading || resolving}
                  style={{
                    ...btnBase,
                    background: 'transparent',
                    color: 'var(--text-primary, #cdd6f4)',
                    border: '1px solid var(--border, #585b70)',
                    cursor: (loading || resolving) ? 'not-allowed' : 'pointer',
                    opacity: (loading || resolving) ? 0.6 : 1,
                  }}
                >
                  Cancel
                </button>
              )}
            </div>
          </>
        )}

        {/* ───────── STEP 2: Sessions / Mode Picker ───────── */}
        {step === 2 && (
          <>
            {showModePicker ? (
              /* ── Mode picker view ── */
              <>
                <h3 style={titleStyle}>Choose Mode</h3>

                {/* Selected folder path */}
                <div style={folderPathStyle}>
                  📁 {selectedFolderPath}
                </div>

                {/* Mode selector */}
                <div style={{ marginBottom: '1.2rem' }}>
                  <label style={labelStyle}>Mode</label>
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

                {/* Vault warning (only for home dir or ~/.thoughtmachine) */}
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

                {error && (
                  <div style={errorBoxStyle}>
                    ⚠ {error}
                  </div>
                )}

                {/* Action buttons */}
                <div style={{ display: 'flex', gap: '0.5rem', justifyContent: 'flex-end' }}>
                  <button
                    onClick={() => setShowModePicker(false)}
                    style={{
                      ...btnBase,
                      background: 'transparent',
                      color: 'var(--text-primary, #cdd6f4)',
                      border: '1px solid var(--border, #585b70)',
                    }}
                  >
                    Back
                  </button>
                  <button
                    onClick={handleCreateNew}
                    disabled={!canCreateFromMode}
                    style={{
                      ...btnBase,
                      background: canCreateFromMode ? 'var(--accent, #89b4fa)' : 'var(--text-muted, #6c7086)',
                      color: '#fff',
                      cursor: canCreateFromMode ? 'pointer' : 'not-allowed',
                      opacity: canCreateFromMode ? 1 : 0.6,
                    }}
                  >
                    {loading ? 'Creating...' : 'Create Session'}
                  </button>
                </div>
              </>
            ) : (
              /* ── Session list view ── */
              <>
                <h3 style={titleStyle}>
                  Sessions in {workspacePath.split('/').pop() || 'workspace'}
                </h3>

                {/* Selected folder path */}
                <div style={folderPathStyle}>
                  📁 {selectedFolderPath}
                </div>

                {error && (
                  <div style={errorBoxStyle}>
                    ⚠ {error}
                  </div>
                )}

                <WorkspaceSessionList
                  workspaceId={workspaceId}
                  onOpen={handleOpenSession}
                  onNewSession={() => {
                    setShowModePicker(true)
                    setAcknowledgedRisk(false)
                    setError('')
                  }}
                  onBack={handleBack}
                />

                {!isFirstLaunch && (
                  <div style={switchViewStyle}>
                    <button
                      onClick={onCancel}
                      disabled={loading}
                      style={{
                        ...btnBase,
                        background: 'transparent',
                        color: 'var(--text-primary, #cdd6f4)',
                        border: '1px solid var(--border, #585b70)',
                        cursor: loading ? 'not-allowed' : 'pointer',
                        opacity: loading ? 0.6 : 1,
                      }}
                    >
                      Cancel
                    </button>
                  </div>
                )}
              </>
            )}
          </>
        )}
      </div>
    </div>
  )
}
