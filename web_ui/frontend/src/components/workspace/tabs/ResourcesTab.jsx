// --- ORPHANED — replaced by WorkspaceDetailPage; do not import in new code ---
// --- tabs/ResourcesTab.jsx ---
// Moved verbatim from WorkspacePanel.jsx (Phase 4 structural split).

import React, { useState } from 'react'
import { Toggle, RISK_CLASS } from '../workspaceUtils.jsx'
import ResourceCatalogModal from '../modals/ResourceCatalogModal'

export default function ResourcesTab({ workspace, update }) {
  const [showCatalog, setShowCatalog] = useState(false)
  const resources = workspace.resources || []
  const toggleResource = (name, enabled) => {
    update('resources', resources.map((r) => (r.name === name ? { ...r, enabled } : r)))
  }
  const addResources = (items) => {
    const existing = new Set(resources.map((r) => r.name))
    update('resources', [...resources, ...items.filter((it) => !existing.has(it.name))])
  }
  return (
    <div className="wp-section">
      <div className="wp-section-header">
        <h3>Resources</h3>
      </div>
      {resources.length === 0 ? (
        <p className="wp-empty">No resources defined for this workspace.</p>
      ) : (
        <div className="wp-resource-grid">
          {resources.map((r) => (
            <div className="wp-resource-card" key={r.name}>
              <div className="wp-resource-top">
                <span className="wp-resource-icon" role="img" aria-label={r.name}>{r.icon || '•'}</span>
                <span className={`wp-risk ${RISK_CLASS[r.risk] || 'low'}`}>{r.risk}</span>
              </div>
              <h4 className="wp-resource-name">{r.name}</h4>
              <p className="wp-resource-desc">{r.description || r.name}</p>
              <div className="wp-resource-badges">
                {r.containerized ? (
                  <span className="wp-badge wp-badge-containerized">✓ Containerized</span>
                ) : (
                  <span className="wp-badge wp-badge-hostonly">⚠ Host-only</span>
                )}
              </div>
              <Toggle
                checked={r.enabled}
                label={r.enabled ? 'Enabled' : 'Disabled'}
                onChange={(enabled) => toggleResource(r.name, enabled)}
              />
            </div>
          ))}
        </div>
      )}
      <div className="wp-section-footer">
        <button className="wp-btn" onClick={() => setShowCatalog(true)}>Add Resource</button>
      </div>
      {showCatalog && <ResourceCatalogModal onAdd={addResources} onClose={() => setShowCatalog(false)} />}
    </div>
  )
}
