// --- GlobalResources.jsx ---
// Renders the shared resource catalog as cards. Editing is not implemented
// yet, so the Edit button is disabled.

import React, { useEffect, useState } from 'react'
import { fetchResourceCatalog } from '../globalApi'

export default function GlobalResources() {
  const [resources, setResources] = useState([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      const data = await fetchResourceCatalog()
      if (cancelled) return
      setResources(data)
      setLoading(false)
    }
    load()
    return () => {
      cancelled = true
    }
  }, [])

  if (loading) return <p className="gms-empty">Loading resources…</p>
  if (resources.length === 0) return <p className="gms-empty">No resources available.</p>

  return (
    <div className="gms-resource-grid">
      {resources.map((r) => (
        <article className="gms-resource-card" key={r.id || r.name}>
          <div className="gms-resource-head">
            <h4 className="gms-resource-name">{r.display_name || r.name}</h4>
            {r.dockerfile_reference ? (
              <span className="gms-badge gms-badge-container">Containerized</span>
            ) : null}
          </div>
          <p className="gms-resource-desc">{r.description}</p>
          {Array.isArray(r.permission_grain_set) && r.permission_grain_set.length > 0 ? (
            <div className="gms-grain-row">
              {r.permission_grain_set.map((g) => (
                <span className="gms-badge gms-grain" key={g}>
                  {g}
                </span>
              ))}
            </div>
          ) : null}
          {r.default_execution_context ? (
            <div className="gms-resource-line">Context: {r.default_execution_context}</div>
          ) : null}
          {Array.isArray(r.tools) && r.tools.length > 0 ? (
            <div className="gms-resource-line">Tools: {r.tools.join(', ')}</div>
          ) : null}
          <button type="button" className="gms-btn-disabled" disabled title="Coming soon">
            Edit
          </button>
        </article>
      ))}
    </div>
  )
}
