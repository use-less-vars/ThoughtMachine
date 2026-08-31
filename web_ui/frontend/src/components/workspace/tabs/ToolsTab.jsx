// --- ORPHANED — replaced by WorkspaceDetailPage; do not import in new code ---
// --- tabs/ToolsTab.jsx ---
// Moved verbatim from WorkspacePanel.jsx (Phase 4 structural split).

import React, { useMemo } from 'react'
import { Toggle } from '../workspaceUtils.jsx'

export default function ToolsTab({ workspace, update }) {
  const tools = workspace.tools || []
  const groups = useMemo(() => {
    const map = {}
    for (const t of tools) {
      const res = t.resource || 'filesystem'
      if (!map[res]) map[res] = []
      map[res].push(t)
    }
    return Object.entries(map)
  }, [tools])
  const toggleTool = (name, patch) => {
    update('tools', tools.map((t) => (t.name === name ? { ...t, ...patch } : t)))
  }
  return (
    <div className="wp-section">
      <div className="wp-section-header">
        <h3>Tools</h3>
      </div>
      {tools.length === 0 ? (
        <p className="wp-empty">No tools configured for this workspace.</p>
      ) : (
        groups.map(([resource, groupTools]) => (
          <div className="wp-tool-group" key={resource}>
            <h4 className="wp-tool-group-title">{resource}</h4>
            {groupTools.map((t) => (
              <div className="wp-tool-row" key={t.name}>
                <span className="wp-tool-name">{t.name}</span>
                <span className="wp-badge wp-badge-muted">{t.permission || 'read'}</span>
                <Toggle checked={t.enabled} label="Enabled" onChange={(enabled) => toggleTool(t.name, { enabled })} />
                <Toggle checked={t.defaultOn} label="Default ON" onChange={(defaultOn) => toggleTool(t.name, { defaultOn })} />
              </div>
            ))}
          </div>
        ))
      )}
    </div>
  )
}
