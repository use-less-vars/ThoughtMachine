import React from 'react';
import useStore, { PERMISSION_DEFAULTS } from '../store/useStore';
import WorkerManagementPanel from './WorkerManagementPanel';
import DockerfileEditor from './DockerfileEditor';
import DomainAllowlistEditor from './DomainAllowlistEditor';

// ── Catppuccin palette matching ConfigPanel ──────────────────────────────
const inputStyle = {
  background: '#1e1e2e',
  color: '#cdd6f4',
  border: '1px solid #585b70',
  borderRadius: '4px',
  padding: '0.4rem 0.5rem',
  fontSize: '0.85rem',
  width: '100%',
  boxSizing: 'border-box',
  outline: 'none',
};

const labelStyle = {
  display: 'block',
  marginBottom: '0.25rem',
  fontSize: '0.85rem',
  color: '#a6adc8',
};

const sectionStyle = {
  marginBottom: '1.25rem',
};

// ── Permission pill color map ────────────────────────────────────────────
const PILL_COLORS = {
  full:   { bg: '#a6e3a1', fg: '#1e1e2e', label: 'Full' },
  write:  { bg: '#a6e3a1', fg: '#1e1e2e', label: 'Write' },
  read:   { bg: '#89b4fa', fg: '#1e1e2e', label: 'Read' },
  ask:    { bg: '#f9e2af', fg: '#1e1e2e', label: 'Ask' },
  banned: { bg: '#f38ba8', fg: '#1e1e2e', label: 'Banned' },
  true:   { bg: '#a6e3a1', fg: '#1e1e2e', label: 'Enabled' },
  false:  { bg: '#f38ba8', fg: '#1e1e2e', label: 'Disabled' },
};

function getPill(value) {
  const key = String(value);
  return PILL_COLORS[key] || { bg: '#6c7086', fg: '#cdd6f4', label: key };
}

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

// ── Status/event badge colors ───────────────────────────────────────────
const STATUS_DOT_COLORS = {
  running:   '#a6e3a1',  // green
  idle:      '#585b70',  // grey
  completed: '#6c7086',  // grey
  failed:    '#f38ba8',  // red
};

const EVENT_BADGE_COLORS = {
  started:   { bg: '#a6e3a1', fg: '#1e1e2e', label: 'Started' },
  query:     { bg: '#89b4fa', fg: '#1e1e2e', label: 'Query' },
  tool_call: { bg: '#cba6f7', fg: '#1e1e2e', label: 'Tool Call' },
  completed: { bg: '#585b70', fg: '#cdd6f4', label: 'Completed' },
  error:     { bg: '#f38ba8', fg: '#1e1e2e', label: 'Error' },
};

function getEventBadge(eventType) {
  return EVENT_BADGE_COLORS[eventType] || { bg: '#585b70', fg: '#cdd6f4', label: eventType };
}

// ── Relative time helper ────────────────────────────────────────────────
function relativeTime(isoString) {
  if (!isoString) return '';
  const now = Date.now();
  const then = new Date(isoString).getTime();
  const diffSec = Math.floor((now - then) / 1000);
  if (diffSec < 0) return 'just now';
  if (diffSec < 5) return 'just now';
  if (diffSec < 60) return `${diffSec}s ago`;
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  return `${Math.floor(diffHr / 24)}d ago`;
}

// ── Truncate helper ─────────────────────────────────────────────────────
function truncate(str, maxLen = 120) {
  if (!str) return '';
  const s = typeof str === 'string' ? str : JSON.stringify(str);
  if (s.length <= maxLen) return s;
  return s.slice(0, maxLen) + '…';
}

// ── Worker name + dot ────────────────────────────────────────────────────
function WorkerDot({ status }) {
  const color = STATUS_DOT_COLORS[status] || '#585b70';
  return (
    <span
      style={{
        width: 8,
        height: 8,
        borderRadius: '50%',
        background: color,
        display: 'inline-block',
        flexShrink: 0,
      }}
    />
  );
}

// ── Event Log Overlay (moved to WorkerOutputPanel) ─────────────────────
// Worker event viewing now uses the persistent WorkerOutputPanel sidebar,
// wired via onSelectWorker callback passed up through ConfigPanel.
//
// NOTE: WorkerAutoOpenWatcher was removed in favor of the identical
// auto-open logic already built into WorkerManagementPanel. The
// WorkerManagementPanel component (rendered below) handles auto-opening
// the output panel when workers transition to ready/busy.


// ── Section: Effective Permissions ───────────────────────────────────────
function EffectivePermissionsSection({ sessionId }) {
  // Permissions now come straight from the store: the WS 'config_changed' /
  // 'session_loaded' events carry the exact payload the old REST endpoint
  // (/api/workspace/<id>/effective_permissions) wrapped as
  // { effective_permissions: ... } — server.py get_config calls
  // config_manager.resolve_effective_permissions(bridge._session_config) and
  // the store saves it verbatim in sessionConfigs[sessionId].permissions.
  // Reading from the store keeps the pills live after apply_config and drops
  // the one-shot fetch plus the loading/failed states.
  const permissions = useStore((s) => (sessionId ? (s.sessionConfigs[sessionId]?.permissions ?? null) : null));
  const ep = permissions || PERMISSION_DEFAULTS;
  const categories = ['filesystem', 'network', 'git', 'system', 'worker', 'container'];

  return (
    <div>
      {categories.map((cat) => {
        if (cat in ep) {
          return <PermissionPill key={cat} name={cat.charAt(0).toUpperCase() + cat.slice(1)} value={ep[cat]} />;
        }
        return null;
      })}
    </div>
  );
}

// ── Main WorkspacePanel ──────────────────────────────────────────────────
export default function WorkspacePanel({ workspaceId, sessionId, onSelectWorker, selectedWorker, isActive }) {
  if (!workspaceId) {
    return (
      <div style={{ color: '#6c7086', fontSize: '0.85rem', padding: '1rem 0', textAlign: 'center' }}>
        No workspace loaded.
      </div>
    );
  }

  return (
    <div>
      {/* Dockerfile */}
      <div style={sectionStyle}>
        <label style={labelStyle}><strong>Dockerfile</strong></label>
        <DockerfileEditor workspaceId={workspaceId} />
      </div>

      {/* Domain Allowlist */}
      <div style={sectionStyle}>
        <label style={labelStyle}><strong>Domain Allowlist</strong></label>
        <small style={{ color: '#6c7086', fontSize: '0.75rem', display: 'block', marginBottom: '0.3rem' }}>
          One domain per line. Wildcards supported (e.g. *.example.com).
        </small>
        <DomainAllowlistEditor workspaceId={workspaceId} />
      </div>

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
        <EffectivePermissionsSection sessionId={sessionId} />
      </div>
    </div>
  );
}
