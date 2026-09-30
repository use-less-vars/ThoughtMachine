import React from 'react';
import useStore, { PERMISSION_DEFAULTS } from '../store/useStore';
import WorkerManagementPanel from './WorkerManagementPanel';
import { getPill } from '../data/permissionVocab';

// ── Catppuccin palette matching ConfigPanel ──────────────────────────────
const labelStyle = {
  display: 'block',
  marginBottom: '0.25rem',
  fontSize: '0.85rem',
  color: '#a6adc8',
};

const sectionStyle = {
  marginBottom: '1.25rem',
};

function PermissionPill({ name, value }) {
  const p = getPill(value);
  return (
    <span
      style={{
        display: 'inline-block',
        background: p.bg,
        color: p.fg,
        borderRadius: '12px',
        padding: '0.15rem 0.6rem',
        fontSize: '0.75rem',
        fontWeight: 600,
        marginRight: '0.35rem',
        marginBottom: '0.25rem',
        whiteSpace: 'nowrap',
      }}
      title={`${name}: ${value}`}
    >
      {name}: {p.label}
    </span>
  );
}

// ── Section: Effective Permissions ───────────────────────────────────────
const CATEGORY_LABELS = {
  git: 'Git',
  filesystem: 'Filesystem',
  container: 'Container',
  network: 'Network',
  mcp: 'MCP',
  host_bash: 'Host Bash',
};

function EffectivePermissionsSection({ sessionId, effectivePermissions }) {
  // Preferred source: the session GET/PUT effective dict passed down by
  // ConfigPanel (sessionPerms.effective from /api/session/{id}/permissions),
  // which is authoritative for the currently loaded session.
  // Fallback (prop null): the store copy saved verbatim by the WS
  // 'config_changed' / 'session_loaded' events from
  // config_manager.resolve_effective_permissions(bridge._session_config)
  // (sessionConfigs[sessionId].permissions). Reading from the store keeps the
  // pills live after apply_config when no REST session profile is loaded.
  const permissions = useStore((s) => (sessionId ? (s.sessionConfigs[sessionId]?.permissions ?? null) : null));
  const ep = effectivePermissions || permissions || PERMISSION_DEFAULTS;
  const categories = ['git', 'filesystem', 'container', 'network', 'mcp', 'host_bash'];

  return (
    <div>
      {categories.map((cat) => {
        if (cat in ep) {
          return <PermissionPill key={cat} name={CATEGORY_LABELS[cat] || cat.charAt(0).toUpperCase() + cat.slice(1)} value={ep[cat]} />;
        }
        return null;
      })}
    </div>
  );
}

// ── Main SessionWorkspaceTab ─────────────────────────────────────────
// Session-scoped survivors of the retired WorkspacePanel: the session worker
// control (WorkerManagementPanel) and the resolved effective-permission pills
// (a (session_id, workspace_id) session-scope resolution). The read-only
// "Workspace Path" field stays in ConfigPanel. Workspace-scoped editors
// (blueprints, containers, dockerfile, domain allowlist) now live on the
// /workspace/:id page (WorkspaceDetailPage), not in the session view.
export default function SessionWorkspaceTab({ workspaceId, sessionId, onSelectWorker, selectedWorker, isActive, effectivePermissions }) {
  if (!workspaceId) {
    return (
      <div style={{ color: '#6c7086', fontSize: '0.85rem', padding: '1rem 0', textAlign: 'center' }}>
        No workspace loaded.
      </div>
    );
  }

  return (
    <div>
      {/* Workers */}
      <div style={sectionStyle}>
        <label style={labelStyle}><strong>Workers</strong></label>
        <WorkerManagementPanel workspaceId={workspaceId} onSelectWorker={onSelectWorker} selectedWorker={selectedWorker} sessionId={sessionId} isActive={isActive} />
      </div>

      {/* Effective Permissions */}
      <div style={sectionStyle}>
        <label style={labelStyle}><strong>Effective Permissions</strong></label>
        <small style={{ color: '#6c7086', fontSize: '0.75rem', display: 'block', marginBottom: '0.3rem' }}>
          Merged session + workspace capabilities.
        </small>
        <EffectivePermissionsSection sessionId={sessionId} effectivePermissions={effectivePermissions} />
      </div>
    </div>
  );
}
