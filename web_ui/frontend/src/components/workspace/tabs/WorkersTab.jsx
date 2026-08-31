// --- ORPHANED — replaced by WorkspaceDetailPage; do not import in new code ---
// --- tabs/WorkersTab.jsx ---
// Moved verbatim from WorkspacePanel.jsx (Phase 4 structural split).
// The worker editor modal now lives at modals/WorkerEditorModal.jsx.

import React, { useState } from 'react'
import { countOf, PROMPT_PREVIEW_LENGTH, normalizePermissions } from '../workspaceUtils.jsx'
import WorkerEditorModal from '../modals/WorkerEditorModal'

export default function WorkersTab({ workspace, update }) {
  const [expanded, setExpanded] = useState(null)
  const [editing, setEditing] = useState(null)  // worker being edited (null = new)
  const [showEditor, setShowEditor] = useState(false)
  const [showAddPreset, setShowAddPreset] = useState(false)
  const workers = workspace.workers || []

  const handleSaveWorker = (next) => {
    if (editing) {
      // Replace in place, keyed by the previously edited name.
      update('workers', workers.map((w) => (w.name === editing.name ? next : w)))
    } else {
      // Append a new preset; the backend rejects duplicate names (409).
      update('workers', [...workers, next])
    }
    setShowEditor(false)
    setShowAddPreset(false)
    setEditing(null)
  }

  return (
    <div className="wp-section">
      <div className="wp-section-header">
        <h3>Workers</h3>
      </div>
      {workers.length === 0 ? (
        <p className="wp-empty">No worker presets configured for this workspace.</p>
      ) : (
        <div className="wp-worker-grid">
          {workers.map((w) => {
            const isExpanded = expanded === w.name
            const prompt = w.systemPrompt || ''
            const preview = prompt.length > PROMPT_PREVIEW_LENGTH && !isExpanded
              ? prompt.slice(0, PROMPT_PREVIEW_LENGTH) + '…'
              : prompt
            return (
              <div className="wp-worker-card" key={w.name}>
                <div className="wp-worker-top">
                  <h4 className="wp-worker-name">{w.name}</h4>
                  <span className="wp-badge wp-badge-muted">{w.runtimeStatus || 'ready'}</span>
                  <button
                    className="wp-btn"
                    onClick={() => { setEditing(w); setShowEditor(true) }}
                  >
                    Edit
                  </button>
                </div>
                <button
                  className="wp-worker-prompt"
                  onClick={() => setExpanded(isExpanded ? null : w.name)}
                  title={isExpanded ? 'Collapse' : 'Expand'}
                >
                  {preview || 'No system prompt set.'}
                </button>
                <div className="wp-worker-meta">
                  <span>{countOf(w.tools)} tools</span>
                  <span>{countOf(w.workerPermissions)} permissions</span>
                  <span>{w.tokenLimit ?? '—'} tokens</span>
                </div>
              </div>
            )
          })}
        </div>
      )}
      <div className="wp-section-footer">
        <button className="wp-btn" onClick={() => { setEditing(null); setShowAddPreset(true) }}>Add Preset</button>
      </div>
      {showEditor && <WorkerEditorModal key={editing ? editing.name : 'new'} worker={editing} onSave={handleSaveWorker} onClose={() => setShowEditor(false)} allTools={workspace.tools || []} allPermissions={normalizePermissions(workspace.permissions)} />}
      {showAddPreset && <WorkerEditorModal key="new-preset" worker={null} onSave={handleSaveWorker} onClose={() => setShowAddPreset(false)} allTools={workspace.tools || []} allPermissions={normalizePermissions(workspace.permissions)} />}
    </div>
  )
}
