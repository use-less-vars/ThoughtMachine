import React, { useState, useEffect, useCallback, useRef } from 'react';

// ── Inline Catppuccin palette (matching WorkspacePanel) ────────────────────
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

const btnPrimary = {
  background: '#89b4fa',
  color: '#1e1e2e',
  border: 'none',
  borderRadius: '4px',
  padding: '0.35rem 0.75rem',
  fontSize: '0.8rem',
  cursor: 'pointer',
  fontWeight: 600,
};

const btnDanger = {
  background: '#f38ba8',
  color: '#1e1e2e',
  border: 'none',
  borderRadius: '4px',
  padding: '0.35rem 0.75rem',
  fontSize: '0.8rem',
  cursor: 'pointer',
  fontWeight: 600,
};

const btnGhost = {
  background: 'transparent',
  color: '#89b4fa',
  border: '1px solid #89b4fa',
  borderRadius: '4px',
  padding: '0.25rem 0.5rem',
  fontSize: '0.75rem',
  cursor: 'pointer',
};

const modalOverlay = {
  position: 'fixed',
  inset: 0,
  background: 'rgba(0,0,0,0.6)',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  zIndex: 1000,
};

const modalBox = {
  background: '#313244',
  border: '1px solid #585b70',
  borderRadius: '8px',
  padding: '1.25rem',
  width: '560px',
  maxWidth: '90vw',
  maxHeight: '85vh',
  overflowY: 'auto',
  color: '#cdd6f4',
};

const STATUS_DOT_COLORS = {
  ready: '#585b70',    /* grey — idle, spawned but not doing anything */
  running: '#a6e3a1',  /* green — real runtime_status for an active worker */
  busy: '#a6e3a1',     /* green with pulse — actively processing */
  completed: '#6c7086', /* muted grey — finished */
  error: '#f38ba8',    /* red — something went wrong */
  pausing: '#f9e2af',  /* amber — pause in flight (not yet paused) */
  paused: '#f9e2af',   /* amber — paused; matches session StatusBar */
  stopped: '#313244',  /* dark/off — not spawned */
};

// ── Helper: relative time ──────────────────────────────────────────────────
function relativeTime(ts) {
  if (!ts) return '';
  const diffMs = Date.now() - new Date(ts).getTime();
  const diffMin = Math.floor(diffMs / 60000);
  if (diffMin < 1) return 'just now';
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  return `${Math.floor(diffHr / 24)}d ago`;
}

// ── Helper: compact token count (12400 -> '12.4k') ──────────────────────────
function formatTokens(n) {
  if (n == null) return '';
  if (n >= 1000) {
    const k = n / 1000;
    return `${k % 1 === 0 ? k.toFixed(0) : k.toFixed(1)}k`;
  }
  return String(n);
}

// ── Helper: human-readable duration (130 -> '2m 10s', 3600 -> '1h') ────────
function formatDuration(sec) {
  if (sec == null || sec < 0 || !Number.isFinite(sec)) return '';
  const s = Math.round(sec);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const remS = s % 60;
  if (m < 60) return remS === 0 ? `${m}m` : `${m}m ${remS}s`;
  const h = Math.floor(m / 60);
  const remM = m % 60;
  return remM === 0 ? `${h}h` : `${h}h ${remM}m`;
}

// ── Helper: truncate ───────────────────────────────────────────────────────
function truncate(str, maxLen = 120) {
  if (!str) return '';
  const s = typeof str === 'string' ? str : JSON.stringify(str);
  if (s.length <= maxLen) return s;
  return s.slice(0, maxLen) + '…';
}

// ── Helper: per-instance identity key (name + instance_id) ────────────────
function workerInstanceKey(w) {
  return `${w.name}#${w.instance_id ?? '0'}`;
}

// ── Status dot ─────────────────────────────────────────────────────────────
function WorkerDot({ status }) {
  const color = STATUS_DOT_COLORS[status] || '#585b70';
  const isBusy = status === 'busy';
  return (
    <span
      className={isBusy ? 'worker-dot-busy' : ''}
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

// ── Permission pill ────────────────────────────────────────────────────────
function PermissionPill({ name, value }) {
  const key = String(value);
  const PILL_COLORS = {
    full:   { bg: '#a6e3a1', fg: '#1e1e2e', label: 'Full' },
    write:  { bg: '#a6e3a1', fg: '#1e1e2e', label: 'Write' },
    read:   { bg: '#89b4fa', fg: '#1e1e2e', label: 'Read' },
    ask:    { bg: '#f9e2af', fg: '#1e1e2e', label: 'Ask' },
    banned: { bg: '#f38ba8', fg: '#1e1e2e', label: 'Banned' },
    true:   { bg: '#a6e3a1', fg: '#1e1e2e', label: 'Enabled' },
    false:  { bg: '#f38ba8', fg: '#1e1e2e', label: 'Disabled' },
  };
  const pill = PILL_COLORS[key] || { bg: '#6c7086', fg: '#cdd6f4', label: key };
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: '0.25rem',
        background: pill.bg,
        color: pill.fg,
        borderRadius: '10px',
        padding: '0.1rem 0.5rem',
        fontSize: '0.7rem',
        fontWeight: 600,
        whiteSpace: 'nowrap',
      }}
      title={`${name}: ${value}`}
    >
      {name}:{pill.label}
    </span>
  );
}

// ── Empty state ────────────────────────────────────────────────────────────
function EmptyState({ message, onNew, onFromTemplate }) {
  return (
    <div style={{ textAlign: 'center', padding: '1.5rem 0', color: '#6c7086' }}>
      <div style={{ fontSize: '0.85rem', marginBottom: '0.75rem' }}>{message}</div>
      <div style={{ display: 'flex', gap: '0.5rem', justifyContent: 'center' }}>
        <button style={btnPrimary} onClick={onNew}>+ New Worker</button>
        {onFromTemplate && (
          <button style={btnGhost} onClick={onFromTemplate}>From Template</button>
        )}
      </div>
    </div>
  );
}

// ── Delete confirmation dialog ─────────────────────────────────────────────
function DeleteConfirm({ name, onConfirm, onCancel }) {
  return (
    <div style={modalOverlay} onClick={onCancel}>
      <div
        className="delete-confirm-dialog"
        style={modalBox}
        onClick={(e) => e.stopPropagation()}
      >
        <h3 style={{ margin: '0 0 0.5rem', fontSize: '1rem', color: '#f38ba8' }}>
          Delete Worker
        </h3>
        <p style={{ fontSize: '0.85rem', color: '#a6adc8', margin: '0 0 1rem' }}>
          Are you sure you want to delete <strong>{name}</strong>? This cannot be undone.
        </p>
        <div style={{ display: 'flex', gap: '0.5rem', justifyContent: 'flex-end' }}>
          <button style={{ ...btnGhost, color: '#cdd6f4', borderColor: '#585b70' }} onClick={onCancel}>
            Cancel
          </button>
          <button style={btnDanger} onClick={() => onConfirm(name)}>
            Delete
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Template picker ────────────────────────────────────────────────────────
function TemplatePicker({ templates, onSelect, onCancel }) {
  return (
    <div style={modalOverlay} onClick={onCancel}>
      <div
        style={{ ...modalBox, width: '440px' }}
        onClick={(e) => e.stopPropagation()}
      >
        <h3 style={{ margin: '0 0 0.75rem', fontSize: '1rem' }}>New Worker from Template</h3>
        {templates.length === 0 && (
          <p style={{ color: '#6c7086', fontSize: '0.85rem' }}>No templates available.</p>
        )}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
          {templates.map((t) => (
            <button
              key={t.name}
              onClick={() => onSelect(t)}
              style={{
                background: '#45475a',
                color: '#cdd6f4',
                border: '1px solid #585b70',
                borderRadius: '6px',
                padding: '0.6rem 0.75rem',
                cursor: 'pointer',
                textAlign: 'left',
                fontSize: '0.85rem',
                transition: 'background 0.15s',
              }}
              onMouseEnter={(e) => { e.currentTarget.style.background = '#585b70'; }}
              onMouseLeave={(e) => { e.currentTarget.style.background = '#45475a'; }}
            >
              <strong>{t.name}</strong>
              <span style={{ color: '#a6adc8', marginLeft: '0.5rem', fontSize: '0.78rem' }}>
                {t.description}
              </span>
              {t.tools && (
                <span style={{ color: '#6c7086', marginLeft: '0.4rem', fontSize: '0.7rem' }}>
                  ({t.tools.length} tools)
                </span>
              )}
            </button>
          ))}
        </div>
        <div style={{ marginTop: '0.75rem', textAlign: 'right' }}>
          <button style={{ ...btnGhost, color: '#cdd6f4', borderColor: '#585b70' }} onClick={onCancel}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Worker form modal (create / edit) ──────────────────────────────────────
const FORM_WIDTH = '100%';
const formRow = {
  marginBottom: '0.6rem',
};

function WorkerFormModal({ worker, templates, onSave, onCancel, isCreateFromTemplate }) {
  const isEdit = !!worker && !isCreateFromTemplate;
  const [name, setName] = useState(worker?.name || '');
  const [description, setDescription] = useState(worker?.description || '');
  const [systemPrompt, setSystemPrompt] = useState(worker?.system_prompt || '');
  const [toolsText, setToolsText] = useState(
    Array.isArray(worker?.tools) ? worker.tools.join('\n') : ''
  );
  const [permFootprint, setPermFootprint] = useState(
    worker?.permission_footprint ? JSON.stringify(worker.permission_footprint, null, 2) : '{}'
  );
  const [timeoutSeconds, setTimeoutSeconds] = useState(worker?.timeout_seconds ?? '');
  const [maxContextTokens, setMaxContextTokens] = useState(worker?.max_context_tokens ?? '');
  const [warningThresholdTokens, setWarningThresholdTokens] = useState(worker?.warning_threshold_tokens ?? '');
  const [turnLimit, setTurnLimit] = useState(worker?.turn_limit ?? '');
  const [temperature, setTemperature] = useState(worker?.temperature ?? '');
  const [saveError, setSaveError] = useState('');
  const [saving, setSaving] = useState(false);

  const handleSave = async () => {
    setSaveError('');
    setSaving(true);

    // Validate: name, tools parsing, perm footprint parsing
    if (!name.trim()) {
      setSaveError('Name is required.');
      setSaving(false);
      return;
    }

    let tools;
    try {
      tools = toolsText
        .split('\n')
        .map((s) => s.trim())
        .filter(Boolean);
    } catch {
      setSaveError('Invalid tools list.');
      setSaving(false);
      return;
    }

    let footprint;
    try {
      footprint = permFootprint.trim() ? JSON.parse(permFootprint) : {};
    } catch {
      setSaveError('Permission footprint must be valid JSON (e.g. {"filesystem": "write"}).');
      setSaving(false);
      return;
    }

    const payload = {
      name: name.trim(),
      description: description.trim(),
      system_prompt: systemPrompt,
      tools,
      permission_footprint: footprint,
    };

    // Only include optional fields if they have a value
    if (timeoutSeconds !== '') payload.timeout_seconds = Number(timeoutSeconds);
    if (maxContextTokens !== '') payload.max_context_tokens = Number(maxContextTokens);
    if (warningThresholdTokens !== '') payload.warning_threshold_tokens = Number(warningThresholdTokens);
    if (turnLimit !== '') payload.turn_limit = Number(turnLimit);
    if (temperature !== '') payload.temperature = Number(temperature);

    onSave(payload, isEdit);
    setSaving(false);
  };

  const inputErrorStyle = saveError ? { borderColor: '#f38ba8' } : {};

  return (
    <div style={modalOverlay} onClick={onCancel}>
      <div
        style={modalBox}
        onClick={(e) => e.stopPropagation()}
      >
        <h3 style={{ margin: '0 0 0.75rem', fontSize: '1rem' }}>
          {isEdit ? `Edit Worker: ${worker.name}` : 'New Worker'}
        </h3>

        {/* Name */}
        <div style={formRow}>
          <label style={labelStyle}>Name *</label>
          <input
            style={{ ...inputStyle, ...inputErrorStyle }}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. my-coder"
            disabled={isEdit} /* name is the identity key — can't change */
          />
        </div>

        {/* Description */}
        <div style={formRow}>
          <label style={labelStyle}>Description</label>
          <input
            style={inputStyle}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="What this worker does"
          />
        </div>

        {/* System prompt */}
        <div style={formRow}>
          <label style={labelStyle}>System Prompt</label>
          <textarea
            style={{ ...inputStyle, minHeight: '80px', resize: 'vertical', fontFamily: 'inherit' }}
            value={systemPrompt}
            onChange={(e) => setSystemPrompt(e.target.value)}
            placeholder="Instructions for the worker agent…"
          />
        </div>

        {/* Tools (one per line) */}
        <div style={formRow}>
          <label style={labelStyle}>
            Tools{' '}
            <span style={{ color: '#6c7086', fontSize: '0.75rem' }}>(one per line)</span>
          </label>
          <textarea
            style={{ ...inputStyle, minHeight: '64px', resize: 'vertical', fontFamily: 'monospace' }}
            value={toolsText}
            onChange={(e) => setToolsText(e.target.value)}
            placeholder="ApplyEdits&#10;CodeModifier&#10;FileEditor"
          />
        </div>

        {/* Permission footprint (JSON) */}
        <div style={formRow}>
          <label style={labelStyle}>
            Permission Footprint{' '}
            <span style={{ color: '#6c7086', fontSize: '0.75rem' }}>(JSON)</span>
          </label>
          <textarea
            style={{ ...inputStyle, minHeight: '48px', resize: 'vertical', fontFamily: 'monospace' }}
            value={permFootprint}
            onChange={(e) => setPermFootprint(e.target.value)}
            placeholder='{"filesystem": "write", "execution": "docker"}'
          />
        </div>

        {/* Optional overrides — inline row */}
        <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap', marginBottom: '0.6rem' }}>
          <div style={{ flex: '1 1 120px', minWidth: '80px' }}>
            <label style={labelStyle}>Timeout (s)</label>
            <input
              style={inputStyle}
              type="number"
              value={timeoutSeconds}
              onChange={(e) => setTimeoutSeconds(e.target.value)}
              placeholder="default"
            />
          </div>
          <div style={{ flex: '1 1 120px', minWidth: '80px' }}>
            <label style={labelStyle}>Max Tokens</label>
            <input
              style={inputStyle}
              type="number"
              value={maxContextTokens}
              onChange={(e) => setMaxContextTokens(e.target.value)}
              placeholder="default"
            />
          </div>
          <div style={{ flex: '1 1 120px', minWidth: '80px' }}>
            <label style={labelStyle}>Warn Threshold</label>
            <input
              style={inputStyle}
              type="number"
              value={warningThresholdTokens}
              onChange={(e) => setWarningThresholdTokens(e.target.value)}
              placeholder="default"
            />
          </div>
          <div style={{ flex: '1 1 80px', minWidth: '60px' }}>
            <label style={labelStyle}>Turns</label>
            <input
              style={inputStyle}
              type="number"
              value={turnLimit}
              onChange={(e) => setTurnLimit(e.target.value)}
              placeholder="∞"
            />
          </div>
          <div style={{ flex: '1 1 80px', minWidth: '60px' }}>
            <label style={labelStyle}>Temp.</label>
            <input
              style={inputStyle}
              type="number"
              step="0.1"
              min="0"
              max="2"
              value={temperature}
              onChange={(e) => setTemperature(e.target.value)}
              placeholder="default"
            />
          </div>
        </div>

        {/* Error message */}
        {saveError && (
          <div style={{ color: '#f38ba8', fontSize: '0.8rem', marginBottom: '0.5rem' }}>
            {saveError}
          </div>
        )}

        {/* Buttons */}
        <div style={{ display: 'flex', gap: '0.5rem', justifyContent: 'flex-end', marginTop: '0.5rem' }}>
          <button
            style={{ ...btnGhost, color: '#cdd6f4', borderColor: '#585b70' }}
            onClick={onCancel}
            disabled={saving}
          >
            Cancel
          </button>
          <button
            style={{ ...btnPrimary, opacity: saving ? 0.7 : 1 }}
            onClick={handleSave}
            disabled={saving}
          >
            {saving ? 'Saving…' : isEdit ? 'Update Worker' : 'Create Worker'}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Main WorkerManagementPanel ─────────────────────────────────────────────
export default function WorkerManagementPanel({
  workspaceId,
  sessionId,
  onSelectWorker,
  selectedWorker,
  isActive,
}) {
  // ── State ────────────────────────────────────────────────────────────────
  const [workers, setWorkers] = useState([]);        // from GET /api/workspace/{ws_id}/workers (config + runtime)
  const [loading, setLoading] = useState(true);
  const [stopErrors, setStopErrors] = useState({});

  // Modal state
  const [showForm, setShowForm] = useState(false);
  const [editingWorker, setEditingWorker] = useState(null);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(null); // worker name string
  const [showTemplatePicker, setShowTemplatePicker] = useState(false);
  const [templates, setTemplates] = useState([]);
  const [isCreateFromTemplate, setIsCreateFromTemplate] = useState(false);

  // ── Auto-open logic (from old WorkersSection) ────────────────────────────
  const prevStatusMapRef = useRef(new Map());
  const dismissedWorkersRef = useRef(new Set());
  const prevSelectedWorkerRef = useRef(selectedWorker);

  useEffect(() => {
    const prev = prevSelectedWorkerRef.current;
    if (prev && !selectedWorker) {
      dismissedWorkersRef.current.add(workerInstanceKey(prev));
    }
    prevSelectedWorkerRef.current = selectedWorker;
  }, [selectedWorker]);

  useEffect(() => {
    const currentMap = new Map(workers.map((w) => [workerInstanceKey(w), w.runtime_status]));
    const prev = prevStatusMapRef.current;

    for (const [key, status] of currentMap) {
      if (dismissedWorkersRef.current.has(key)) {
        if (status !== 'busy' && status !== 'ready') {
          dismissedWorkersRef.current.delete(key);
        }
        continue;
      }

      const prevStatus = prev.get(key);
      if (
        !prev.has(key) ||
        (prevStatus !== 'ready' && prevStatus !== 'busy' && (status === 'ready' || status === 'busy'))
      ) {
        const worker = workers.find((w) => workerInstanceKey(w) === key);
        if (worker) {
          onSelectWorker?.(worker.name, workspaceId, worker.instance_id, worker.instance_label);
        }
        break;
      }
    }

    prevStatusMapRef.current = currentMap;
  }, [workers, workspaceId, onSelectWorker]);

  // ── Fetch workers (with runtime status) ──────────────────────────────────
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

  // ── Fetch templates ──────────────────────────────────────────────────────
  const fetchTemplates = useCallback(() => {
    fetch('/api/workspace/templates')
      .then(async (res) => {
        if (!res.ok) return;
        setTemplates(await res.json());
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    fetchTemplates();
  }, [fetchTemplates]);

  // ── Stop worker ──────────────────────────────────────────────────────────
  const handleStop = useCallback(
    async (name, instanceId) => {
      const key = workerInstanceKey({ name, instance_id: instanceId });
      const instanceQuery = instanceId != null ? `?instance_id=${instanceId}` : '';
      try {
        const res = await fetch(
          `/api/workspace/${workspaceId}/workers/${encodeURIComponent(name)}/stop${instanceQuery}`,
          { method: 'POST' }
        );
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          throw new Error(body?.detail?.error || `HTTP ${res.status}`);
        }
        setWorkers((prev) =>
          prev.map((w) =>
            w.name === name && (w.instance_id ?? null) === (instanceId ?? null)
              ? { ...w, runtime_status: 'stopped' }
              : w
          )
        );
        setStopErrors((prev) => {
          const n = { ...prev };
          delete n[key];
          return n;
        });
      } catch (err) {
        setStopErrors((prev) => ({ ...prev, [key]: err.message }));
        setTimeout(() => {
          setStopErrors((prev) => {
            const n = { ...prev };
            delete n[key];
            return n;
          });
        }, 3000);
      }
    },
    [workspaceId]
  );

  const handlePause = useCallback(async (name, instanceId) => {
    const key = workerInstanceKey({ name, instance_id: instanceId });
    const instanceQuery = instanceId != null ? `?instance_id=${instanceId}` : '';
    try {
      const res = await fetch(
        `/api/workspace/${workspaceId}/workers/${encodeURIComponent(name)}/pause${instanceQuery}`,
        { method: 'POST' }
      );
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body?.detail?.error || `HTTP ${res.status}`);
      }
      setWorkers((prev) =>
        prev.map((w) =>
          w.name === name && (w.instance_id ?? null) === (instanceId ?? null)
            ? { ...w, runtime_status: 'pausing' }
            : w
        )
      );
      setStopErrors((prev) => { const n = { ...prev }; delete n[key]; return n; });
    } catch (err) {
      setStopErrors((prev) => ({ ...prev, [key]: err.message }));
      setTimeout(() => {
        setStopErrors((prev) => { const n = { ...prev }; delete n[key]; return n; });
      }, 3000);
    }
  }, [workspaceId]);

  const handleResume = useCallback(async (name, instanceId) => {
    const key = workerInstanceKey({ name, instance_id: instanceId });
    const instanceQuery = instanceId != null ? `?instance_id=${instanceId}` : '';
    try {
      const res = await fetch(
        `/api/workspace/${workspaceId}/workers/${encodeURIComponent(name)}/resume${instanceQuery}`,
        { method: 'POST' }
      );
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body?.detail?.error || `HTTP ${res.status}`);
      }
      setWorkers((prev) =>
        prev.map((w) =>
          w.name === name && (w.instance_id ?? null) === (instanceId ?? null)
            ? { ...w, runtime_status: 'ready' }
            : w
        )
      );
      setStopErrors((prev) => { const n = { ...prev }; delete n[key]; return n; });
    } catch (err) {
      setStopErrors((prev) => ({ ...prev, [key]: err.message }));
      setTimeout(() => {
        setStopErrors((prev) => { const n = { ...prev }; delete n[key]; return n; });
      }, 3000);
    }
  }, [workspaceId]);

  const canStop = useCallback((status) => {
    return status === 'busy' || status === 'ready' || !status;
  }, []);

  const canPause = useCallback((status) => {
    return status === 'busy' || status === 'ready';
  }, []);

  const canResume = useCallback((status) => {
    return status === 'paused';
  }, []);

  // ── Create / Update worker ───────────────────────────────────────────────
  const handleSaveWorker = useCallback(
    async (payload, isEdit) => {
      const url = isEdit
        ? `/api/workspace/${workspaceId}/workers/${encodeURIComponent(payload.name)}`
        : `/api/workspace/${workspaceId}/workers`;
      const method = isEdit ? 'PUT' : 'POST';

      try {
        const res = await fetch(url, {
          method,
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          throw new Error(body?.detail || `HTTP ${res.status}`);
        }
        // Refresh the worker list
        setShowForm(false);
        setEditingWorker(null);
        fetchWorkers();
      } catch (err) {
        // Surface error in the form
        setShowForm(false);
        setEditingWorker(null);
        // Re-fetch anyway to stay in sync
        fetchWorkers();
        // Show a brief alert
        alert(`Failed to ${isEdit ? 'update' : 'create'} worker: ${err.message}`);
      }
    },
    [workspaceId, fetchWorkers]
  );

  // ── Delete worker ────────────────────────────────────────────────────────
  const handleDeleteWorker = useCallback(
    async (name) => {
      try {
        const res = await fetch(
          `/api/workspace/${workspaceId}/workers/${encodeURIComponent(name)}`,
          { method: 'DELETE' }
        );
        if (!res.ok && res.status !== 204) {
          const body = await res.json().catch(() => ({}));
          throw new Error(body?.detail || `HTTP ${res.status}`);
        }
        setShowDeleteConfirm(null);
        fetchWorkers();
      } catch (err) {
        alert(`Failed to delete worker: ${err.message}`);
        setShowDeleteConfirm(null);
      }
    },
    [workspaceId, fetchWorkers]
  );

  // ── Open form for editing ────────────────────────────────────────────────
  const openEdit = useCallback((worker) => {
    setEditingWorker(worker);
    setIsCreateFromTemplate(false);
    setShowForm(true);
  }, []);

  // ── Open form for new worker from template ───────────────────────────────
  const openNewFromTemplate = useCallback((template) => {
    setShowTemplatePicker(false);
    setIsCreateFromTemplate(true);
    setEditingWorker({
      name: template.name,
      description: template.description || '',
      system_prompt: template.system_prompt || '',
      tools: template.tools || [],
      permission_footprint: template.permission_footprint || {},
      timeout_seconds: template.timeout_seconds,
      max_context_tokens: template.max_context_tokens,
      warning_threshold_tokens: template.warning_threshold_tokens,
      turn_limit: template.turn_limit,
      temperature: template.temperature,
    });
    setShowForm(true);
  }, []);

  // ── Render ───────────────────────────────────────────────────────────────
  if (!workspaceId) {
    return (
      <div style={{ color: '#6c7086', fontSize: '0.85rem', padding: '1rem 0', textAlign: 'center' }}>
        No workspace loaded.
      </div>
    );
  }

  if (loading) {
    return (
      <div style={{ color: '#6c7086', fontSize: '0.85rem' }}>Loading workers…</div>
    );
  }

  return (
    <>
      {/* ── Action buttons ────────────────────────────────────────────── */}
      <div style={{ display: 'flex', gap: '0.4rem', marginBottom: '0.5rem' }}>
        <button
          style={btnPrimary}
          onClick={() => {
            setEditingWorker(null);
            setIsCreateFromTemplate(false);
            setShowForm(true);
          }}
        >
          + New Worker
        </button>
        <button
          style={btnGhost}
          onClick={() => {
            fetchTemplates();
            setShowTemplatePicker(true);
          }}
        >
          From Template
        </button>
      </div>

      {/* ── Worker list ───────────────────────────────────────────────── */}
      {workers.length === 0 ? (
        <EmptyState
          message="No workers configured. Create one now, or start from a template."
          onNew={() => {
            setEditingWorker(null);
            setIsCreateFromTemplate(false);
            setShowForm(true);
          }}
          onFromTemplate={() => {
            fetchTemplates();
            setShowTemplatePicker(true);
          }}
        />
      ) : (
        <div
          style={{
            border: '1px solid #45475a',
            borderRadius: '6px',
            overflow: 'hidden',
          }}
        >
          {/* Column headers */}
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '0.4rem',
              padding: '0.4rem 0.5rem',
              background: '#181825',
              borderBottom: '1px solid #45475a',
              fontSize: '0.75rem',
              color: '#6c7086',
              fontWeight: 600,
              textTransform: 'uppercase',
            }}
          >
            <span style={{ width: 8, flexShrink: 0 }} />
            <span style={{ flex: '0 0 130px', minWidth: 0 }}>Name</span>
            <span style={{ flex: 1, minWidth: 0 }}>Description</span>
            <span style={{ flex: '0 0 60px', textAlign: 'center' }}>Tools</span>
            <span style={{ flex: '0 0 90px', textAlign: 'center' }}>Status</span>
            <span style={{ flex: '0 0 80px', textAlign: 'right' }}>Actions</span>
          </div>

          {/* Rows */}
          {workers.map((w, i) => {
            const isRunning = canStop(w.runtime_status);
            const toolsList = Array.isArray(w.tools) ? w.tools : [];
            const perms = w.permission_footprint || {};
            const instKey = workerInstanceKey(w);
            const dispLabel = w.instance_label || w.name;
            const isSelected =
              selectedWorker?.name === w.name &&
              selectedWorker?.workspaceId === workspaceId &&
              (selectedWorker.instance_id == null || selectedWorker.instance_id === w.instance_id);

            return (
              <div
                key={w.name ? instKey : i}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: '0.4rem',
                  padding: '0.45rem 0.5rem',
                  fontSize: '0.85rem',
                  color: '#cdd6f4',
                  borderBottom: i < workers.length - 1 ? '1px solid #45475a' : 'none',
                  cursor: 'pointer',
                  borderRadius: '0',
                  transition: 'background 0.15s',
                  background: isSelected ? '#45475a' : 'transparent',
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.background = '#585b70';
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.background = isSelected ? '#45475a' : 'transparent';
                }}
                onClick={() => {
                  dismissedWorkersRef.current.delete(instKey);
                  onSelectWorker?.(w.name, workspaceId, w.instance_id, w.instance_label);
                }}
              >
                {/* Status dot */}
                <WorkerDot status={w.runtime_status} />

                {/* Name */}
                <span
                  style={{
                    flex: '0 0 130px',
                    fontWeight: 600,
                    whiteSpace: 'nowrap',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    minWidth: 0,
                  }}
                  title={dispLabel}
                >
                  {dispLabel}
                </span>

                {/* Description */}
                <span
                  style={{
                    flex: 1,
                    color: '#a6adc8',
                    fontSize: '0.8rem',
                    whiteSpace: 'nowrap',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    minWidth: 0,
                  }}
                  title={w.description || ''}
                >
                  {truncate(w.description, 50) || <span style={{ color: '#6c7086' }}>—</span>}
                </span>

                {/* Tools count */}
                <span
                  style={{
                    flex: '0 0 60px',
                    textAlign: 'center',
                    color: '#6c7086',
                    fontSize: '0.78rem',
                    cursor: 'help',
                  }}
                  title={toolsList.join(', ')}
                >
                  {toolsList.length}
                </span>

                {/* Status + runtime info */}
                <div
                  style={{
                    flex: '0 0 90px',
                    textAlign: 'center',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    gap: '0.25rem',
                    fontSize: '0.75rem',
                  }}
                >
                  {w.runtime_status ? (
                    <>
                      <span
                        style={{
                          color:
                            w.runtime_status === 'error'
                              ? '#f38ba8'
                              : w.runtime_status === 'busy' || w.runtime_status === 'running'
                                ? '#89b4fa'
                                : w.runtime_status === 'pausing' || w.runtime_status === 'paused'
                                  ? '#f9e2af'
                                  : '#a6adc8',
                        }}
                      >
                        {w.runtime_status === 'pausing'
                          ? 'Pausing…'
                          : w.runtime_status === 'paused'
                            ? 'Paused'
                            : w.runtime_status}
                      </span>
                      {/* Pause / Resume toggle */}
                      {w.runtime_status === 'paused' ? (
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            handleResume(w.name, w.instance_id);
                          }}
                          style={{
                            background: '#a6e3a1',
                            color: '#1e1e2e',
                            border: 'none',
                            borderRadius: '3px',
                            padding: '0.1rem 0.4rem',
                            cursor: 'pointer',
                            fontWeight: 600,
                            fontSize: '0.65rem',
                            lineHeight: '1.4',
                            marginRight: '4px',
                          }}
                          title={`Resume ${dispLabel}`}
                        >
                          ▶ Resume
                        </button>
                      ) : w.runtime_status === 'pausing' ? (
                        <button
                          disabled
                          style={{
                            background: '#f9e2af',
                            color: '#1e1e2e',
                            border: 'none',
                            borderRadius: '3px',
                            padding: '0.1rem 0.4rem',
                            cursor: 'default',
                            opacity: 0.6,
                            fontWeight: 600,
                            fontSize: '0.65rem',
                            lineHeight: '1.4',
                            marginRight: '4px',
                          }}
                          title={`Pausing ${dispLabel}…`}
                        >
                          ⏸ Pausing…
                        </button>
                      ) : (
                        canPause(w.runtime_status) && (
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              handlePause(w.name, w.instance_id);
                            }}
                            style={{
                              background: '#f9e2af',
                              color: '#1e1e2e',
                              border: 'none',
                              borderRadius: '3px',
                              padding: '0.1rem 0.4rem',
                              cursor: 'pointer',
                              fontWeight: 600,
                              fontSize: '0.65rem',
                              lineHeight: '1.4',
                              marginRight: '4px',
                            }}
                            title={`Pause ${dispLabel}`}
                          >
                            ⏸ Pause
                          </button>
                        )
                      )}
                      {/* Stop button */}
                      {isRunning && (
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            handleStop(w.name, w.instance_id);
                          }}
                          style={{
                            background: '#f38ba8',
                            color: '#1e1e2e',
                            border: 'none',
                            borderRadius: '3px',
                            padding: '0.1rem 0.4rem',
                            cursor: 'pointer',
                            fontWeight: 600,
                            fontSize: '0.65rem',
                            lineHeight: '1.4',
                          }}
                          title={`Stop ${dispLabel}`}
                        >
                          Stop
                        </button>
                      )}
                      {/* manual-pause badge (payload.paused_manually) */}
                      {w.paused_manually && (
                        <span
                          title="Paused manually"
                          style={{
                            background: '#f9e2af',
                            color: '#1e1e2e',
                            borderRadius: '3px',
                            padding: '0.1rem 0.3rem',
                            fontSize: '0.6rem',
                            fontWeight: 600,
                            whiteSpace: 'nowrap',
                          }}
                        >
                          manual pause
                        </span>
                      )}
                      {/* compact token indicator */}
                      {w.current_context_tokens != null && (
                        <span style={{ color: '#a6adc8', fontSize: '0.65rem' }} title="Context tokens">
                          ctx: {formatTokens(w.current_context_tokens)}
                          {w.max_context_tokens != null
                            ? ` / ${formatTokens(w.max_context_tokens)}`
                            : ''}
                        </span>
                      )}
                      {/* time since last query (falls back to heartbeat) */}
                      {(w.time_since_last_query != null && w.time_since_last_query >= 0) ||
                      w.last_heartbeat ? (
                        <span
                          style={{ color: '#6c7086', fontSize: '0.65rem' }}
                          title="Time since last query"
                        >
                          {w.time_since_last_query != null && w.time_since_last_query >= 0
                            ? formatDuration(w.time_since_last_query)
                            : relativeTime(w.last_heartbeat)}
                        </span>
                      ) : null}
                    </>
                  ) : (
                    <span style={{ color: '#6c7086' }}>—</span>
                  )}
                </div>

                {/* Actions: Edit / Delete */}
                <div
                  style={{
                    flex: '0 0 80px',
                    textAlign: 'right',
                    display: 'flex',
                    gap: '0.3rem',
                    justifyContent: 'flex-end',
                  }}
                  onClick={(e) => e.stopPropagation()}
                >
                  <button
                    style={{
                      background: 'transparent',
                      color: '#89b4fa',
                      border: 'none',
                      cursor: 'pointer',
                      fontSize: '0.75rem',
                      padding: '0.1rem 0.3rem',
                      borderRadius: '3px',
                    }}
                    onClick={() => openEdit(w)}
                    title="Edit worker"
                  >
                    Edit
                  </button>
                  <button
                    style={{
                      background: 'transparent',
                      color: '#f38ba8',
                      border: 'none',
                      cursor: 'pointer',
                      fontSize: '0.75rem',
                      padding: '0.1rem 0.3rem',
                      borderRadius: '3px',
                    }}
                    onClick={() => setShowDeleteConfirm(w.name)}
                    title="Delete worker"
                  >
                    Del
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* ── Runtime bottom bar: current task for selected worker ──────── */}
      {workers.map((w) => {
        const instKey = workerInstanceKey(w);
        if (
          selectedWorker?.name === w.name &&
          selectedWorker?.workspaceId === workspaceId &&
          (selectedWorker.instance_id == null || selectedWorker.instance_id === w.instance_id) &&
          w.current_task
        ) {
          return (
            <div
              key={`task-${instKey}`}
              style={{
                marginTop: '0.4rem',
                fontSize: '0.8rem',
                color: '#a6adc8',
                padding: '0.25rem 0.35rem',
                background: '#1e1e2e',
                borderRadius: '4px',
                border: '1px solid #45475a',
                whiteSpace: 'nowrap',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
              }}
              title={w.current_task}
            >
              <strong>{w.instance_label || w.name}:</strong> {truncate(w.current_task, 100)}
            </div>
          );
        }
        return null;
      })}

      {/* ── Inline stop errors ────────────────────────────────────────── */}
      {Object.entries(stopErrors).map(([name, err]) => (
        <div
          key={`err-${name}`}
          style={{
            marginTop: '0.3rem',
            fontSize: '0.75rem',
            color: '#f38ba8',
          }}
        >
          ✗ {name}: {err}
        </div>
      ))}

      {/* ── Permission pills for running worker ───────────────────────── */}
      {workers
        .filter((w) => w.runtime_status && w.runtime_status !== 'stopped')
        .map((w) => {
          const perms = w.permission_footprint || {};
          const permEntries = Object.entries(perms);
          if (permEntries.length === 0) return null;
          return (
            <div
              key={`perms-${workerInstanceKey(w)}`}
              style={{
                display: 'flex',
                gap: '0.3rem',
                flexWrap: 'wrap',
                marginTop: '0.3rem',
              }}
            >
              <span style={{ color: '#6c7086', fontSize: '0.7rem', marginRight: '0.2rem' }}>
                {w.instance_label || w.name}:
              </span>
              {permEntries.map(([k, v]) => (
                <PermissionPill key={k} name={k} value={v} />
              ))}
            </div>
          );
        })}

      {/* ── Modals ────────────────────────────────────────────────────── */}

      {/* Create / Edit form */}
      {showForm && (
        <WorkerFormModal
          worker={editingWorker}
          templates={templates}
          isCreateFromTemplate={isCreateFromTemplate}
          onSave={handleSaveWorker}
          onCancel={() => {
            setShowForm(false);
            setEditingWorker(null);
            setIsCreateFromTemplate(false);
          }}
        />
      )}

      {/* Delete confirmation */}
      {showDeleteConfirm && (
        <DeleteConfirm
          name={showDeleteConfirm}
          onConfirm={handleDeleteWorker}
          onCancel={() => setShowDeleteConfirm(null)}
        />
      )}

      {/* Template picker */}
      {showTemplatePicker && (
        <TemplatePicker
          templates={templates}
          onSelect={openNewFromTemplate}
          onCancel={() => setShowTemplatePicker(false)}
        />
      )}
    </>
  );
}
