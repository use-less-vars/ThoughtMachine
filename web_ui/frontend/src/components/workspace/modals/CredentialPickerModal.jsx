// --- modals/CredentialPickerModal.jsx ---
// Renamed from CredentialModal (WorkspacePanel.jsx, Phase 4 structural split).
// VAULT_PLACEHOLDERS moves in here with the component; behavior unchanged.

import React, { useState } from 'react'

const VAULT_PLACEHOLDERS = [
  { name: 'openai_api_key', hint: 'stored in vault' },
  { name: 'github_token', hint: 'stored in vault' },
  { name: 'docker_registry_auth', hint: 'stored in vault' },
  { name: 'ssh_deploy_key', hint: 'stored in vault' },
  { name: 'huggingface_token', hint: 'stored in vault' },
]

export default function CredentialPickerModal({ onAdd, onClose }) {
  const [checked, setChecked] = useState({})

  const toggleCheck = (name) => setChecked((prev) => ({ ...prev, [name]: !prev[name] }))

  const handleAdd = () => {
    const selected = VAULT_PLACEHOLDERS
      .filter((it) => checked[it.name])
      .map((it) => ({
        name: it.name,
        hint: it.hint,
        type: 'vault',
        assigned: true,
        placeholder: `{{credential:${it.name}}}`,
      }))
    if (selected.length > 0) onAdd(selected)
    onClose()
  }

  return (
    <div className="wp-modal-overlay" onClick={onClose}>
      <div className="wp-modal" role="dialog" aria-label="Add credential" onClick={(e) => e.stopPropagation()}>
        <h3 className="wp-modal-title">Add credential</h3>
        <div className="wp-modal-body">
          <div className="wp-checkbox-list">
            {VAULT_PLACEHOLDERS.map((c) => (
              <label key={c.name} className="wp-checkbox">
                <input
                  type="checkbox"
                  checked={!!checked[c.name]}
                  onChange={() => toggleCheck(c.name)}
                />
                <span>{c.name}</span>
              </label>
            ))}
          </div>
          <p className="wp-modal-hint">Vault integration coming soon — placeholder entries.</p>
        </div>
        <div className="wp-modal-footer">
          <button className="wp-btn" onClick={onClose}>Cancel</button>
          <button className="wp-btn wp-btn-primary" onClick={handleAdd}>Add selected</button>
        </div>
      </div>
    </div>
  )
}
