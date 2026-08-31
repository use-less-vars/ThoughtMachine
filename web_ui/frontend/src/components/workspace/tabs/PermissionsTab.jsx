// --- ORPHANED — replaced by WorkspaceDetailPage; do not import in new code ---
// --- tabs/PermissionsTab.jsx ---
// Moved verbatim from WorkspacePanel.jsx (Phase 4 structural split).
// The ceiling dropdown lists PERMISSION_CEILINGS, which includes 'enabled'
// (the value the container permission map reports for effective permissions).

import React from 'react'
import { normalizePermissions, permissionsWithCeiling, PERMISSION_CEILINGS } from '../workspaceUtils.jsx'

export default function PermissionsTab({ workspace, update }) {
  const permissions = normalizePermissions(workspace.permissions)
  const changeCeiling = (name, ceiling) => {
    update('permissions', permissionsWithCeiling(workspace.permissions, name, ceiling))
  }
  return (
    <div className="wp-section">
      <div className="wp-section-header">
        <h3>Permissions</h3>
      </div>
      {permissions.length === 0 ? (
        <p className="wp-empty">No permissions defined for this workspace.</p>
      ) : (
        <table className="wp-table">
          <thead>
            <tr>
              <th>Permission</th>
              <th>Ceiling (workspace max)</th>
              <th>Effective (session default)</th>
            </tr>
          </thead>
          <tbody>
            {permissions.map((p) => (
              <tr key={p.name}>
                <td>{p.name}</td>
                <td>
                  <select
                    className="wp-select"
                    value={p.ceiling}
                    onChange={(e) => changeCeiling(p.name, e.target.value)}
                  >
                    {PERMISSION_CEILINGS.map((level) => (
                      <option key={level} value={level}>{level}</option>
                    ))}
                  </select>
                </td>
                <td className="wp-effective">{p.effective ?? p.ceiling}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
