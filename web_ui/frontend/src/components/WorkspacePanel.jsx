import React, { useState, useEffect, useCallback, useRef } from 'react';
import WorkerManagementPanel from './WorkerManagementPanel';
import DockerfileEditor from './DockerfileEditor';

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

// ── Section: Domain Allowlist ────────────────────────────────────────────
function DomainAllowlistSection({ workspaceId }) {
  const [domains, setDomains] = useState([]);
  const [text, setText] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [saveError, setSaveError] = useState('');

  useEffect(() => {
    if (!workspaceId) return;
    setLoading(true);
    fetch(`/api/workspace/${workspaceId}/domain_allowlist`)
      .then(async (res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        setDomains(Array.isArray(data) ? data : []);
      })
      .catch(() => setDomains([]))
      .finally(() => setLoading(false));
  }, [workspaceId]);

  useEffect(() => {
    setText(domains.join('\n'));
  }, [domains]);

  const handleSave = useCallback(async () => {
    const list = text
      .split('\n')
      .map((s) => s.trim())
      .filter(Boolean);
    setSaveError('');
    setSaving(true);
    try {
      const res = await fetch(`/api/workspace/${workspaceId}/domain_allowlist`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ domains: list }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setDomains(list);
      setSaved(true);
      setSaveError('');
      setTimeout(() => setSaved(false), 2000);
    } catch (err) {
      setSaveError(err.message);
    } finally {
      setSaving(false);
    }
  }, [workspaceId, text]);

  if (loading) return <div style={{ color: '#6c7086', fontSize: '0.85rem' }}>Loading domain allowlist…</div>;

  return (
    <div>
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={5}
        style={{
          ...inputStyle,
          fontFamily: 'monospace',
          fontSize: '0.8rem',
          resize: 'vertical',
          marginBottom: '0.4rem',
        }}
        placeholder="one domain per line, e.g.&#10;*.github.com&#10;api.openai.com"
      />
      {saveError && (
        <div style={{ color: '#f38ba8', fontSize: '0.8rem', marginBottom: '0.4rem' }}>
          Error: {saveError}
        </div>
      )}
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
        <button
          onClick={handleSave}
          disabled={saving}
          style={{
            background: saved ? '#a6e3a1' : '#89b4fa',
            color: '#1e1e2e',
            border: 'none',
            borderRadius: '4px',
            padding: '0.3rem 0.75rem',
            cursor: saving ? 'wait' : 'pointer',
            fontWeight: 600,
            fontSize: '0.8rem',
          }}
        >
          {saving ? 'Saving…' : saved ? '✓ Saved' : 'Save'}
        </button>
        <small style={{ color: '#6c7086', fontSize: '0.75rem' }}>
          {domains.length} domain{domains.length !== 1 ? 's' : ''}
        </small>
      </div>
    </div>
  );
}

// ── Status/event badge colors ───────────────────────────────────────────
const STATUS_DOT_COLORS = {
  running:   '#a6e3a1',  // green
  idle:      '#f9e2af',  // yellow
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

// ── Worker Auto-Open Watcher (always-running, no visual output) ───────────
// Polls worker status and auto-opens the output panel when a worker
// transitions to ready/busy. Renders nothing — can be placed anywhere.
export function WorkerAutoOpenWatcher({ workspaceId, onSelectWorker, onClearWorker, selectedWorker, sessionId, isActive }) {
  const [workers, setWorkers] = useState([]);

  // Track previously seen (name, runtime_status) pairs
  const prevStatusMapRef = useRef(new Map());
  // Track workers the user manually dismissed
  const dismissedWorkersRef = useRef(new Set());

  // Remember the previous selectedWorker to detect when the panel is closed.
  const prevSelectedWorkerRef = useRef(selectedWorker);

  // When the user closes the panel (selectedWorker → null), mark the
  // previously-selected worker as dismissed so it won't re-auto-open
  // until its status changes.
  useEffect(() => {
    const prev = prevSelectedWorkerRef.current;
    if (prev && !selectedWorker) {
      dismissedWorkersRef.current.add(prev.name);
    }
    prevSelectedWorkerRef.current = selectedWorker;
  }, [selectedWorker]);

  // Auto-open panel when a worker appears or transitions to 'running'
  useEffect(() => {
    if (!isActive) return;
    const currentMap = new Map(workers.map(w => [w.name, w.runtime_status]));
    const prev = prevStatusMapRef.current;

    for (const [name, status] of currentMap) {
      // Skip dismissed workers
      if (dismissedWorkersRef.current.has(name)) {
        if (status !== 'busy' && status !== 'ready') {
          dismissedWorkersRef.current.delete(name);
        }
        continue;
      }

      const prevStatus = prev.get(name);
      if (!prev.has(name) || (prevStatus !== 'ready' && prevStatus !== 'busy' && (status === 'ready' || status === 'busy'))) {
        onSelectWorker?.(name, workspaceId);
        break;
      }
    }

    prevStatusMapRef.current = currentMap;
  }, [workers, workspaceId, onSelectWorker, isActive]);

  // If selectedWorker is set but doesn't match any active worker for this session, clear it
  useEffect(() => {
    if (!isActive) return;
    if (selectedWorker && workers.length === 0) {
      onClearWorker?.();
    }
  }, [isActive, selectedWorker, workers, onClearWorker]);

  // Poll workers every 3s
  useEffect(() => {
    if (!workspaceId || !isActive) return;
    const fetchWorkers = () => {
      fetch(`/api/workspace/${workspaceId}/workers`)
        .then(res => res.ok ? res.json() : [])
        .then(data => {
          const filtered = Array.isArray(data) ? data.filter(w => !sessionId || w.session_id === sessionId) : [];
          setWorkers(filtered);
        })
        .catch(() => setWorkers([]));
    };
    fetchWorkers();
    const interval = setInterval(fetchWorkers, 3000);
    return () => clearInterval(interval);
  }, [workspaceId, isActive, sessionId]);

  return null;
}

// ── Section: Effective Permissions ───────────────────────────────────────
function EffectivePermissionsSection({ workspaceId, sessionId }) {
  const [perms, setPerms] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!workspaceId) return;
    setLoading(true);
    const params = new URLSearchParams();
    if (sessionId) params.set('session_id', sessionId);
    fetch(`/api/workspace/${workspaceId}/effective_permissions?${params}`)
      .then(async (res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        setPerms(await res.json());
      })
      .catch(() => setPerms(null))
      .finally(() => setLoading(false));
  }, [workspaceId, sessionId]);

  if (loading) return <div style={{ color: '#6c7086', fontSize: '0.85rem' }}>Loading effective permissions…</div>;
  if (!perms) return <div style={{ color: '#f38ba8', fontSize: '0.85rem' }}>Failed to load effective permissions.</div>;

  const ep = perms.effective_permissions || {};
  const categories = ['filesystem', 'network', 'git', 'system', 'execution', 'container'];

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
        <DomainAllowlistSection workspaceId={workspaceId} />
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
        <EffectivePermissionsSection workspaceId={workspaceId} sessionId={sessionId} />
      </div>
    </div>
  );
}
