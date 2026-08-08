// --- tabs/CredentialsTab.jsx ---
// Moved verbatim from WorkspacePanel.jsx (Phase 4 structural split).
// The vault-picker modal now lives at modals/CredentialPickerModal.jsx.

import React, { useState } from 'react'
import { Toggle } from '../workspaceUtils.jsx'
import CredentialPickerModal from '../modals/CredentialPickerModal'

export default function CredentialsTab({ workspace, update }) {
  const [showVault, setShowVault] = useState(false)
  const credentials = workspace.credentials || []
  const toggleAssigned = (name, assigned) => {
    update('credentials', credentials.map((c) => (c.name === name ? { ...c, assigned } : c)))
  }
  const addCredentials = (items) => {
    const existing = new Set(credentials.map((c) => c.name))
    update('credentials', [...credentials, ...items.filter((it) => !existing.has(it.name))])
  }
  return (
    <div className="wp-section">
      <div className="wp-section-header">
        <h3>Credentials</h3>
        <button className="wp-btn" onClick={() => setShowVault(true)}>Add Credential</button>
      </div>
      {credentials.length === 0 ? (
        <p className="wp-empty">No credentials assigned to this workspace.</p>
      ) : (
        <ul className="wp-cred-list">
          {credentials.map((c) => (
            <li className="wp-cred-row" key={c.name}>
              <span className="wp-cred-name">{c.name}</span>
              <span className="wp-badge wp-badge-type">{c.type || 'generic'}</span>
              <code className="wp-cred-placeholder">{c.placeholder || `{{credential:${c.name}}}`}</code>
              <Toggle checked={c.assigned} label="Assigned" onChange={(assigned) => toggleAssigned(c.name, assigned)} />
            </li>
          ))}
        </ul>
      )}
      {showVault && <CredentialPickerModal onAdd={addCredentials} onClose={() => setShowVault(false)} />}
    </div>
  )
}
