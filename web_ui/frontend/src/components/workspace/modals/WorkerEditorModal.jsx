// --- modals/WorkerEditorModal.jsx ---
// Renamed from WorkerEditor (WorkspacePanel.jsx, Phase 4 structural split).
// Verbatim move — behavior and props unchanged.

import React, { useState } from 'react'

export default function WorkerEditorModal({ worker, onSave, onClose, allTools, allPermissions }) {
  const isNew = !worker
  const [name, setName] = useState(isNew ? '' : (worker.name || ''))
  const [description, setDescription] = useState(worker ? (worker.description || '') : '')
  const [systemPrompt, setSystemPrompt] = useState(worker ? (worker.systemPrompt || '') : '')
  // Tools: array of enabled tool name strings (store worker.tools shape).
  const [tools, setTools] = useState(worker && Array.isArray(worker.tools) ? [...worker.tools] : [])
  // Permissions: [name, value] footprint pairs (store worker.workerPermissions shape).
  const [permissionEntries, setPermissionEntries] = useState(
    worker && worker.workerPermissions ? Object.entries(worker.workerPermissions) : []
  )
  const [customTool, setCustomTool] = useState('')
  const [customPermission, setCustomPermission] = useState('')
  const [error, setError] = useState('')
  const toolNames = (allTools || []).map((t) => (typeof t === 'string' ? t : t.name)).filter(Boolean)
  const allToolNames = Array.from(new Set([...toolNames, ...tools]))
  const permNames = (allPermissions || []).map((p) => (typeof p === 'string' ? p : p.name)).filter(Boolean)
  const allPermNames = Array.from(new Set([...permNames, ...permissionEntries.map(([k]) => k)]))
  const permValue = (permName) => {
    const entry = permissionEntries.find(([k]) => k === permName)
    if (entry) return entry[1]
    const wp = (allPermissions || []).find((p) => (typeof p === 'string' ? p : p.name) === permName)
    return wp && typeof wp === 'object' ? (wp.effective || wp.ceiling || 'read') : 'read'
  }
  const toggleTool = (toolName) => {
    setTools((prev) => (prev.includes(toolName) ? prev.filter((t) => t !== toolName) : [...prev, toolName]))
  }
  const togglePermission = (permName) => {
    setPermissionEntries((prev) => {
      const has = prev.some(([k]) => k === permName)
      if (has) return prev.filter(([k]) => k !== permName)
      return [...prev, [permName, permValue(permName)]]
    })
  }
  const addCustomTool = () => {
    const t = customTool.trim()
    if (!t) return
    setTools((prev) => (prev.includes(t) ? prev : [...prev, t]))
    setCustomTool('')
  }
  const addCustomPermission = () => {
    const raw = customPermission.trim()
    if (!raw) return
    const idx = raw.indexOf(':')
    const key = (idx > 0 ? raw.slice(0, idx) : raw).trim()
    const value = idx > 0 ? raw.slice(idx + 1).trim() : permValue(key)
    if (!key) return
    setPermissionEntries((prev) => [...prev.filter(([k]) => k !== key), [key, value]])
    setCustomPermission('')
  }

  const handleSave = () => {
    const trimmedName = name.trim()
    if (!trimmedName) {
      setError('Worker name is required.')
      return
    }
    const workerPermissions = {}
    for (const [k, v] of permissionEntries) workerPermissions[k] = v
    onSave({
      name: trimmedName,
      description: description.trim(),
      systemPrompt,
      tools,
      workerPermissions,
    })
  }

  return (
    <div className="wp-modal-overlay" onClick={onClose}>
      <div
        className="wp-modal wp-worker-editor"
        role="dialog"
        aria-label={isNew ? 'Add worker preset' : `Edit ${worker.name}`}
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="wp-modal-title">{isNew ? 'Add Worker Preset' : `Edit ${worker.name}`}</h3>
        <div className="wp-modal-body">
          <label className="wp-field">
            <span className="wp-field-label">Name</span>
            <input className="wp-input" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. code-reviewer" />
          </label>
          <label className="wp-field">
            <span className="wp-field-label">Description</span>
            <input className="wp-input" value={description} onChange={(e) => setDescription(e.target.value)} placeholder="What this worker does" />
          </label>
          <label className="wp-field">
            <span className="wp-field-label">System prompt</span>
            <textarea className="wp-textarea" rows={5} value={systemPrompt} onChange={(e) => setSystemPrompt(e.target.value)} />
          </label>
          <div className="wp-field">
            <span className="wp-field-label">Tools</span>
            <div className="wp-checkbox-list">
              {allToolNames.map((t) => (
                <label key={t} className="wp-checkbox">
                  <input type="checkbox" checked={tools.includes(t)} onChange={() => toggleTool(t)} />
                  <span>{t}</span>
                </label>
              ))}
            </div>
            <div className="wp-add-custom">
              <input className="wp-input" value={customTool} onChange={(e) => setCustomTool(e.target.value)} placeholder="custom tool name" />
              <button className="wp-btn" onClick={addCustomTool}>Add</button>
            </div>
          </div>
          <div className="wp-field">
            <span className="wp-field-label">Permissions</span>
            <div className="wp-checkbox-list">
              {allPermNames.map((p) => (
                <label key={p} className="wp-checkbox">
                  <input type="checkbox" checked={permissionEntries.some(([k]) => k === p)} onChange={() => togglePermission(p)} />
                  <span>{p}</span>
                </label>
              ))}
            </div>
            <div className="wp-add-custom">
              <input className="wp-input" value={customPermission} onChange={(e) => setCustomPermission(e.target.value)} placeholder="custom:value (e.g. filesystem:write)" />
              <button className="wp-btn" onClick={addCustomPermission}>Add</button>
            </div>
          </div>
          {error && <p className="wp-error-text">{error}</p>}
        </div>
        <div className="wp-modal-footer">
          <button className="wp-btn" onClick={onClose}>Cancel</button>
          <button className="wp-btn wp-btn-primary" onClick={handleSave}>Save</button>
        </div>
      </div>
    </div>
  )
}
