// --- GlobalCredentials.jsx ---
// Lists vault credentials (names only, never secrets), supports delete with an
// inline two-step confirm and adding a new credential via a modal form.

import React, { useCallback, useEffect, useState } from 'react'
import { createCredential, deleteCredential, fetchCredentials } from '../globalApi'

export default function GlobalCredentials() {
  const [credentials, setCredentials] = useState([])
  const [loading, setLoading] = useState(true)
  const [showAdd, setShowAdd] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState(null)
  const [name, setName] = useState('')
  const [secret, setSecret] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    const d = await fetchCredentials()
    if (Array.isArray(d)) setCredentials(d)
    setLoading(false)
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const closeAdd = () => {
    if (busy) return
    setShowAdd(false)
    setName('')
    setSecret('')
    setError('')
  }

  const submitAdd = async (e) => {
    e.preventDefault()
    setError('')
    if (!name.trim() || !secret) {
      setError('Name and secret are required.')
      return
    }
    setBusy(true)
    const ok = await createCredential({ name: name.trim(), secret })
    setBusy(false)
    if (!ok) {
      setError('Failed to save credential.')
      return
    }
    setName('')
    setSecret('')
    setShowAdd(false)
    load()
  }

  const confirmDelete = async () => {
    const target = deleteTarget
    if (!target) return
    setError('')
    setBusy(true)
    const ok = await deleteCredential(target)
    setBusy(false)
    setDeleteTarget(null)
    if (!ok) {
      setError(`Failed to delete credential '${target}'.`)
    }
    load()
  }

  return (
    <div>
      {!showAdd && error && <p className="gms-error">{error}</p>}
      {loading ? (
        <p className="gms-empty">Loading credentials…</p>
      ) : credentials.length === 0 ? (
        <p className="gms-empty">No credentials stored in the vault.</p>
      ) : (
        <div>
          {credentials.map((c) => (
            <div className="gms-cred-row" key={c.name}>
              <span className="gms-cred-name">{c.name}</span>
              <span className="gms-cred-mask">••••••••</span>
              {deleteTarget === c.name ? (
                <>
                  <button type="button" className="gms-cred-confirm" onClick={confirmDelete}>
                    Confirm
                  </button>
                  <button type="button" className="gms-cred-cancel" onClick={() => setDeleteTarget(null)}>
                    Cancel
                  </button>
                </>
              ) : (
                <button type="button" className="gms-cred-delete" onClick={() => setDeleteTarget(c.name)}>
                  Delete
                </button>
              )}
            </div>
          ))}
        </div>
      )}

      <div style={{ marginTop: '0.6rem' }}>
        <button
          type="button"
          className="ws-modal-btn"
          onClick={() => {
            setError('')
            setShowAdd(true)
          }}
        >
          + Add Credential
        </button>
      </div>

      {showAdd && (
        <div className="gms-modal-overlay" onClick={closeAdd}>
          <div
            className="gms-modal-dialog"
            role="dialog"
            aria-label="Add credential"
            onClick={(e) => e.stopPropagation()}
          >
            <h4 className="ws-modal-title">Add Credential</h4>
            <form onSubmit={submitAdd} style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
              <label className="gms-form-field">
                Name
                <input
                  className="gms-form-input"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  autoFocus
                />
              </label>
              <label className="gms-form-field">
                Secret
                <input
                  className="gms-form-input"
                  type="password"
                  value={secret}
                  onChange={(e) => setSecret(e.target.value)}
                />
              </label>
              {error && <p className="gms-modal-error">{error}</p>}
              <div className="ws-modal-actions">
                <button type="button" className="ws-modal-btn" onClick={closeAdd} disabled={busy}>
                  Cancel
                </button>
                <button type="submit" className="ws-modal-btn" disabled={busy}>
                  Save
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  )
}
