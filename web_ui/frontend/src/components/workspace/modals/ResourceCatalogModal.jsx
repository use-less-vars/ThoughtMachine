// --- ORPHANED — replaced by WorkspaceDetailPage; do not import in new code ---
// --- modals/ResourceCatalogModal.jsx ---
// Moved verbatim from WorkspacePanel.jsx (Phase 4 structural split).
// Add Resource: checklist of available resources. The server catalog
// (GET /api/resource-catalog) does not exist in the backend yet (see
// apiContracts.js pending section) — store.fetchResourceCatalog falls back to
// a bundled placeholder list so the modal always has items to offer.

import React, { useEffect, useState } from 'react'
import useWorkspaceStore from '../../../store/workspaceStore'

export default function ResourceCatalogModal({ onAdd, onClose }) {
  const fetchResourceCatalog = useWorkspaceStore((s) => s.fetchResourceCatalog)
  const [catalog, setCatalog] = useState([])
  const [checked, setChecked] = useState({})

  useEffect(() => {
    let alive = true
    fetchResourceCatalog().then((items) => {
      if (!alive) return
      const list = Array.isArray(items) ? items : []
      setCatalog(list)
      setChecked(Object.fromEntries(list.map((it) => [it.name, true])))
    })
    return () => { alive = false }
  }, [fetchResourceCatalog])

  const toggleCheck = (name) => setChecked((prev) => ({ ...prev, [name]: !prev[name] }))

  const handleAdd = () => {
    const selected = catalog
      .filter((it) => checked[it.name])
      .map((it) => ({
        name: it.name,
        description: it.description || it.name,
        icon: '•',
        containerized: true,
        risk: 'Low',
        enabled: true,
      }))
    if (selected.length > 0) onAdd(selected)
    onClose()
  }

  return (
    <div className="wp-modal-overlay" onClick={onClose}>
      <div className="wp-modal" role="dialog" aria-label="Add resource" onClick={(e) => e.stopPropagation()}>
        <h3 className="wp-modal-title">Add resource</h3>
        <div className="wp-modal-body">
          <div className="wp-checkbox-list">
            {catalog.map((it) => (
              <div key={it.name}>
                <label className="wp-checkbox">
                  <input
                    type="checkbox"
                    checked={!!checked[it.name]}
                    onChange={() => toggleCheck(it.name)}
                  />
                  <span>{it.name}</span>
                </label>
                <p className="wp-resource-desc">{it.description}</p>
              </div>
            ))}
          </div>
          <p className="wp-modal-hint">Server catalog coming soon — showing bundled placeholder list.</p>
        </div>
        <div className="wp-modal-footer">
          <button className="wp-btn" onClick={onClose}>Cancel</button>
          <button className="wp-btn wp-btn-primary" onClick={handleAdd}>Add selected</button>
        </div>
      </div>
    </div>
  )
}
