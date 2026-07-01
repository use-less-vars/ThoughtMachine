import React, { useState, useEffect, useCallback, useRef } from 'react';

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

// ── Section: Dockerfile ──────────────────────────────────────────────────
function DockerfileSection({ workspaceId }) {
  const [content, setContent] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!workspaceId) return;
    setLoading(true);
    setError('');
    fetch(`/api/workspace/${workspaceId}/dockerfile`)
      .then(async (res) => {
        if (!res.ok) {
          if (res.status === 404) {
            setContent('(No custom Dockerfile)');
            return;
          }
          throw new Error(`HTTP ${res.status}`);
        }
        setContent(await res.text());
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [workspaceId]);

  if (loading) return <div style={{ color: '#6c7086', fontSize: '0.85rem' }}>Loading Dockerfile…</div>;
  if (error) return <div style={{ color: '#f38ba8', fontSize: '0.85rem' }}>Error: {error}</div>;

  return (
    <pre
      style={{
        ...inputStyle,
        fontFamily: 'monospace',
        fontSize: '0.75rem',
        lineHeight: '1.4',
        overflow: 'auto',
        maxHeight: '200px',
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-all',
        margin: 0,
      }}
    >
      {content}
    </pre>
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
export function WorkerAutoOpenWatcher({ workspaceId, onSelectWorker, selectedWorker }) {
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
  }, [workers, workspaceId, onSelectWorker]);

  // Poll workers every 3s
  useEffect(() => {
    if (!workspaceId) return;
    const fetchWorkers = () => {
      fetch(`/api/workspace/${workspaceId}/workers`)
        .then(res => res.ok ? res.json() : [])
        .then(data => setWorkers(Array.isArray(data) ? data : []))
        .catch(() => setWorkers([]));
    };
    fetchWorkers();
    const interval = setInterval(fetchWorkers, 3000);
    return () => clearInterval(interval);
  }, [workspaceId]);

  return null;
}

// ── Section: Workers ─────────────────────────────────────────────────────
function WorkersSection({ workspaceId, onSelectWorker, selectedWorker }) {
  const [workers, setWorkers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [stopErrors, setStopErrors] = useState({}); // name -> error message (clears after 3s)

  // Track previously seen (name, runtime_status) pairs to auto-open the panel
  // on transitions (null→running, completed→running, etc.) not just new names.
  const prevStatusMapRef = useRef(new Map());

  // Track workers the user manually dismissed (closed the panel on).
  // A dismissed worker won't auto-open again until its status transitions
  // away from 'running' (e.g. completes a task and starts a new one).
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
    const currentMap = new Map(workers.map(w => [w.name, w.runtime_status]));
    const prev = prevStatusMapRef.current;

    for (const [name, status] of currentMap) {
      // Skip workers the user manually dismissed (panel was closed on them).
      // If a dismissed worker is no longer alive, undismiss it so it can
      // re-trigger auto-open on the next spawn or query.
      if (dismissedWorkersRef.current.has(name)) {
        if (status !== 'busy' && status !== 'ready') {
          dismissedWorkersRef.current.delete(name);
        }
        continue;
      }

      const prevStatus = prev.get(name);
      // Trigger if: name is entirely new, OR an existing worker just became alive
      // (catches null→ready, completed→ready, error→ready, ready→busy)
      // 'ready' means freshly spawned, 'busy' means actively processing a query.
      if (!prev.has(name) || (prevStatus !== 'ready' && prevStatus !== 'busy' && (status === 'ready' || status === 'busy'))) {
        const worker = workers.find(w => w.name === name);
        if (worker) {
          onSelectWorker?.(worker.name, workspaceId);
        }
        break; // auto-select only one per poll cycle
      }
    }

    prevStatusMapRef.current = currentMap;
  }, [workers, workspaceId, onSelectWorker]);

  const fetchWorkers = useCallback(() => {
    if (!workspaceId) return;
    fetch(`/api/workspace/${workspaceId}/workers`)
      .then(async (res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        setWorkers(Array.isArray(data) ? data : []);
      })
      .catch(() => setWorkers([]))
      .finally(() => setLoading(false));
  }, [workspaceId]);

  useEffect(() => {
    fetchWorkers();
    const interval = setInterval(fetchWorkers, 3000);
    return () => clearInterval(interval);
  }, [fetchWorkers]);

  const handleStop = useCallback(async (name) => {
    try {
      const res = await fetch(`/api/workspace/${workspaceId}/workers/${encodeURIComponent(name)}/stop`, {
        method: 'POST',
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body?.detail?.error || `HTTP ${res.status}`);
      }
      // Optimistic update: mark worker as stopped locally
      setWorkers(prev => prev.map(w =>
        w.name === name ? { ...w, runtime_status: 'stopped' } : w
      ));
      // Clear any previous error for this worker
      setStopErrors(prev => { const n = { ...prev }; delete n[name]; return n; });
    } catch (err) {
      setStopErrors(prev => ({ ...prev, [name]: err.message }));
      setTimeout(() => {
        setStopErrors(prev => { const n = { ...prev }; delete n[name]; return n; });
      }, 3000);
    }
  }, [workspaceId]);

  const canStop = useCallback((status) => {
    return status === 'busy' || status === 'ready' || !status;
  }, []);

  if (loading) return <div style={{ color: '#6c7086', fontSize: '0.85rem' }}>Loading workers…</div>;
  if (workers.length === 0) return <div style={{ color: '#6c7086', fontSize: '0.85rem' }}>No workers running.</div>;

  return (
    <>
      <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
        {workers.map((w, i) => {
          const isRunning = canStop(w.runtime_status);
          const toolsList = Array.isArray(w.tools) ? w.tools : [];
          return (
            <li
              key={w.name || i}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '0.4rem',
                padding: '0.35rem 0.4rem',
                fontSize: '0.85rem',
                color: '#cdd6f4',
                borderBottom: i < workers.length - 1 ? '1px solid #45475a' : 'none',
                cursor: 'pointer',
                borderRadius: '4px',
                transition: 'background 0.15s',
                background: selectedWorker?.name === w.name && selectedWorker?.workspaceId === workspaceId ? '#45475a' : 'transparent',
              }}
              onMouseEnter={(e) => { e.currentTarget.style.background = '#585b70'; }}
              onMouseLeave={(e) => { e.currentTarget.style.background = selectedWorker?.name === w.name && selectedWorker?.workspaceId === workspaceId ? '#45475a' : 'transparent'; }}
            >
              <WorkerDot status={w.runtime_status} />
              <span
                style={{ fontWeight: 500, whiteSpace: 'nowrap' }}
                onClick={() => {
                  dismissedWorkersRef.current.delete(w.name);
                  onSelectWorker?.(w.name, workspaceId);
                }}
              >
                {w.name}
              </span>
              {/* Tools tooltip — native browser title attribute */}
              {toolsList.length > 0 && (
                <span
                  style={{
                    color: '#6c7086',
                    fontSize: '0.7rem',
                    whiteSpace: 'nowrap',
                    cursor: 'help',
                  }}
                  title={toolsList.join(', ')}
                >
                  ({toolsList.length})
                </span>
              )}
              <span style={{ color: '#6c7086', fontSize: '0.75rem', whiteSpace: 'nowrap' }}>
                {w.runtime_status || 'ready'}
              </span>
              <span style={{ color: '#a6adc8', fontSize: '0.75rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1, minWidth: 0 }}>
                {w.current_task ? truncate(w.current_task, 80) : ''}
              </span>
              {/* Stop button — only for workers that can be stopped */}
              {isRunning && (
                <button
                  onClick={(e) => { e.stopPropagation(); handleStop(w.name); }}
                  style={{
                    background: '#f38ba8',
                    color: '#1e1e2e',
                    border: 'none',
                    borderRadius: '4px',
                    padding: '0.15rem 0.5rem',
                    cursor: 'pointer',
                    fontWeight: 600,
                    fontSize: '0.7rem',
                    lineHeight: '1.4',
                    whiteSpace: 'nowrap',
                  }}
                  title={`Stop ${w.name}`}
                >
                  Stop
                </button>
              )}
              {w.last_heartbeat && (
                <span style={{ color: '#6c7086', fontSize: '0.7rem', whiteSpace: 'nowrap' }}>
                  {relativeTime(w.last_heartbeat)}
                </span>
              )}
              {/* Inline stop error */}
              {stopErrors[w.name] && (
                <span style={{ color: '#f38ba8', fontSize: '0.7rem', whiteSpace: 'nowrap' }}>
                  ✗ {stopErrors[w.name]}
                </span>
              )}
            </li>
          );
        })}
      </ul>

    </>
  );
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
export default function WorkspacePanel({ workspaceId, sessionId, onSelectWorker, selectedWorker }) {
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
        <DockerfileSection workspaceId={workspaceId} />
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
        <WorkersSection workspaceId={workspaceId} onSelectWorker={onSelectWorker} selectedWorker={selectedWorker} />
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
