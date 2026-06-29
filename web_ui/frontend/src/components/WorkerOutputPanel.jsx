import React, { useState, useEffect, useCallback, useRef } from 'react';

// ── Catppuccin palette ────────────────────────────────────────────────
const colors = {
  base: '#1e1e2e',
  mantle: '#181825',
  crust: '#11111b',
  surface0: '#313244',
  surface1: '#45475a',
  surface2: '#585b70',
  overlay0: '#6c7086',
  overlay1: '#7f849c',
  subtext0: '#a6adc8',
  subtext1: '#bac2de',
  text: '#cdd6f4',
  lavender: '#b4befe',
  blue: '#89b4fa',
  green: '#a6e3a1',
  yellow: '#f9e2af',
  red: '#f38ba8',
  mauve: '#cba6f7',
  teal: '#94e2d5',
  peach: '#fab387',
  accent: '#89b4fa',
};

const PANEL_MIN = 250;
const PANEL_MAX = 600;
const PANEL_DEFAULT = 350;

const STATUS_DOT = {
  running: { bg: '#a6e3a1', label: 'Running' },
  idle: { bg: '#f9e2af', label: 'Idle' },
  completed: { bg: '#6c7086', label: 'Completed' },
  error: { bg: '#f38ba8', label: 'Error' },
  stopped: { bg: '#f38ba8', label: 'Stopped' },
};

function statusDotColor(status) {
  return STATUS_DOT[status]?.bg || '#6c7086';
}

function relativeTime(isoString) {
  if (!isoString) return '';
  const now = Date.now();
  const then = new Date(isoString).getTime();
  const diffSec = Math.floor((now - then) / 1000);
  if (diffSec < 5) return 'just now';
  if (diffSec < 60) return `${diffSec}s ago`;
  const diffMin = Math.floor(diffSec / 60);
  const secs = diffSec % 60;
  if (diffMin < 60) return `${diffMin}m ${secs}s ago`;
  const diffHr = Math.floor(diffMin / 60);
  const mins = diffMin % 60;
  return `${diffHr}h ${mins}m ago`;
}

function elapsedTime(startIso) {
  if (!startIso) return '—';
  const now = Date.now();
  const then = new Date(startIso).getTime();
  const diffSec = Math.floor((now - then) / 1000);
  if (diffSec < 0) return '0s';
  const mins = Math.floor(diffSec / 60);
  const secs = diffSec % 60;
  if (mins === 0) return `${secs}s`;
  return `${mins}m ${secs}s`;
}

function truncate(str, maxLen) {
  if (!str) return '';
  return str.length > maxLen ? str.slice(0, maxLen) + '…' : str;
}

// ── Collapsible block (for tool_call / tool_result) ────────────────────
function CollapsibleBlock({ header, headerColor, children, defaultOpen }) {
  const [open, setOpen] = useState(defaultOpen || false);
  return (
    <div
      style={{
        background: colors.mantle,
        border: `1px solid ${colors.surface1}`,
        borderRadius: '6px',
        marginBottom: '0.35rem',
        overflow: 'hidden',
      }}
    >
      <div
        onClick={() => setOpen(!open)}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: '0.4rem',
          padding: '0.35rem 0.5rem',
          cursor: 'pointer',
          fontSize: '0.8rem',
          color: headerColor || colors.text,
          userSelect: 'none',
        }}
      >
        <span style={{ flexShrink: 0, fontSize: '0.7rem', color: colors.overlay0 }}>
          {open ? '▼' : '▶'}
        </span>
        <span style={{ flex: 1, wordBreak: 'break-word' }}>{header}</span>
      </div>
      {open && (
        <div
          style={{
            padding: '0.35rem 0.5rem 0.5rem 0.5rem',
            borderTop: `1px solid ${colors.surface1}`,
          }}
        >
          {children}
        </div>
      )}
    </div>
  );
}

// ── "New events" floating button ──────────────────────────────────────
function NewEventsButton({ onClick }) {
  return (
    <div
      onClick={onClick}
      style={{
        position: 'sticky',
        bottom: '0.5rem',
        display: 'flex',
        justifyContent: 'center',
        pointerEvents: 'none',
        zIndex: 5,
      }}
    >
      <button
        style={{
          background: colors.blue,
          color: colors.base,
          border: 'none',
          borderRadius: '20px',
          padding: '0.35rem 1rem',
          cursor: 'pointer',
          fontWeight: 600,
          fontSize: '0.8rem',
          pointerEvents: 'auto',
          boxShadow: '0 2px 8px rgba(0,0,0,0.4)',
        }}
      >
        ↓ New events
      </button>
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────
function WorkerOutputPanel({ workspaceId, workerName, onClose }) {
  // Panel resize state (self-contained)
  const [panelWidth, setPanelWidth] = useState(PANEL_DEFAULT);
  const dragRef = useRef(null);
  const storageKey = 'worker-output-panel-width';

  // Restore persisted width
  useEffect(() => {
    const saved = localStorage.getItem(storageKey);
    if (saved) {
      const w = Math.max(PANEL_MIN, Math.min(PANEL_MAX, Number(saved)));
      setPanelWidth(w);
    }
  }, []);

  const handleResizeStart = useCallback((e) => {
    e.preventDefault();
    dragRef.current = { startX: e.clientX, startWidth: panelWidth };
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';

    const handleMouseMove = (e) => {
      if (!dragRef.current) return;
      // Panel is on the right → dragging left decreases width
      const delta = e.clientX - dragRef.current.startX;
      const newWidth = Math.max(PANEL_MIN, Math.min(PANEL_MAX, dragRef.current.startWidth - delta));
      setPanelWidth(newWidth);
    };

    const handleMouseUp = () => {
      dragRef.current = null;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleMouseUp);
    };

    document.addEventListener('mousemove', handleMouseMove);
    document.addEventListener('mouseup', handleMouseUp);
  }, [panelWidth]);

  // Persist width changes
  useEffect(() => {
    localStorage.setItem(storageKey, String(panelWidth));
  }, [panelWidth]);

  // ── Worker info (name, status, current_task) ──────────────────────────
  const [workerInfo, setWorkerInfo] = useState(null);
  const [workerError, setWorkerError] = useState('');
  const [stopError, setStopError] = useState('');

  const fetchWorkerInfo = useCallback(() => {
    if (!workspaceId || !workerName) return;
    fetch(`/api/workspace/${workspaceId}/workers`)
      .then(async (res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (Array.isArray(data)) {
          const found = data.find((w) => w.name === workerName);
          if (found) {
            setWorkerInfo(found);
            setWorkerError('');
          } else {
            // Worker might have not appeared yet; don't clear info
            if (!workerInfo) setWorkerError('Worker not found');
          }
        }
      })
      .catch((err) => {
        if (!workerInfo) setWorkerError(err.message);
      });
  }, [workspaceId, workerName, workerInfo]);

  useEffect(() => {
    fetchWorkerInfo();
    const interval = setInterval(fetchWorkerInfo, 3000);
    return () => clearInterval(interval);
  }, [fetchWorkerInfo]);

  // Reset when workerName changes
  useEffect(() => {
    setWorkerInfo(null);
    setWorkerError('');
    setStopError('');
    setEvents([]);
    setLastFetchTime(null);
  }, [workspaceId, workerName]);

  // ── Events fetching with polling ──────────────────────────────────────
  const [events, setEvents] = useState([]);
  const [eventsError, setEventsError] = useState('');
  const [lastFetchTime, setLastFetchTime] = useState(null);
  const [hasNewEvents, setHasNewEvents] = useState(false);
  const scrollRef = useRef(null);
  const isAtBottomRef = useRef(true);

  const runtimeStatus = workerInfo?.runtime_status || 'idle';

  const fetchEvents = useCallback(() => {
    if (!workspaceId || !workerName) return;
    const params = new URLSearchParams();
    params.set('limit', '50');
    if (lastFetchTime) params.set('since', lastFetchTime);

    fetch(`/api/workspace/${workspaceId}/workers/${encodeURIComponent(workerName)}/events?${params}`)
      .then(async (res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (Array.isArray(data) && data.length > 0) {
          setEvents((prev) => {
            const existingTimestamps = new Set(prev.map((e) => e.timestamp + e.event));
            const newEntries = data.filter((e) => !existingTimestamps.has(e.timestamp + e.event));
            if (newEntries.length === 0) return prev;
            // Only mark "has new events" if user is not at bottom
            if (!isAtBottomRef.current) setHasNewEvents(true);
            return [...prev, ...newEntries];
          });
          const lastTs = data[data.length - 1].timestamp;
          if (lastTs) setLastFetchTime(lastTs);
        }
        setEventsError('');
      })
      .catch((err) => {
        if (events.length === 0) setEventsError(err.message);
      });
  }, [workspaceId, workerName, lastFetchTime, events.length]);

  // Poll period: 1s if running, 3s otherwise
  const pollInterval = runtimeStatus === 'running' ? 1000 : 3000;

  useEffect(() => {
    if (!workspaceId || !workerName) return;
    fetchEvents();
    const interval = setInterval(fetchEvents, pollInterval);
    return () => clearInterval(interval);
  }, [fetchEvents, pollInterval, workspaceId, workerName]);

  // ── Smart scroll ──────────────────────────────────────────────────────
  // Track whether user is at bottom
  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const threshold = 50;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < threshold;
    isAtBottomRef.current = atBottom;
    if (atBottom) setHasNewEvents(false);
  }, []);

  // Auto-scroll on new events if user was at bottom
  useEffect(() => {
    if (isAtBottomRef.current && scrollRef.current && events.length > 0) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [events]);

  const scrollToBottom = () => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
    setHasNewEvents(false);
    isAtBottomRef.current = true;
  };

  // ── Stop handler ──────────────────────────────────────────────────────
  const handleStop = useCallback(async () => {
    if (!workspaceId || !workerName) return;
    setStopError('');
    try {
      const res = await fetch(`/api/workspace/${workspaceId}/workers/${encodeURIComponent(workerName)}/stop`, {
        method: 'POST',
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body?.detail?.error || `HTTP ${res.status}`);
      }
      // Optimistic update
      setWorkerInfo((prev) => prev ? { ...prev, runtime_status: 'stopped' } : prev);
    } catch (err) {
      setStopError(err.message);
      setTimeout(() => setStopError(''), 3000);
    }
  }, [workspaceId, workerName]);

  const canStop = runtimeStatus === 'running' || runtimeStatus === 'idle';

  // ── Compute elapsed time from first event ─────────────────────────────
  const startTime = workerInfo?.started ||
    (events.length > 0 ? events[0].timestamp : null);

  // ── Elapsed timer tick ────────────────────────────────────────────────
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (runtimeStatus !== 'running') return;
    const interval = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(interval);
  }, [runtimeStatus]);

  // ── Render helpers ────────────────────────────────────────────────────
  function renderEvent(evt, idx) {
    const key = evt.timestamp + evt.event + idx;
    const ts = evt.timestamp;

    switch (evt.event) {
      case 'user_message':
        return (
          <div key={key} style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: '0.4rem' }}>
            <div
              style={{
                background: colors.surface1,
                color: colors.text,
                borderRadius: '12px 12px 4px 12px',
                padding: '0.45rem 0.75rem',
                maxWidth: '85%',
                fontSize: '0.82rem',
                lineHeight: '1.4',
                wordBreak: 'break-word',
              }}
            >
              {evt.request?.query || JSON.stringify(evt.request)}
            </div>
          </div>
        );

      case 'tool_call':
        return (
          <div key={key} style={{ marginBottom: '0.4rem' }}>
            <CollapsibleBlock
              header={
                <span>
                  🧰 <strong>Tool:</strong> {evt.request?.tool || 'unknown'}
                </span>
              }
              headerColor={colors.mauve}
            >
              <pre
                style={{
                  margin: 0,
                  fontSize: '0.75rem',
                  color: colors.subtext0,
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                  fontFamily: 'monospace',
                }}
              >
                {JSON.stringify(evt.request?.args || {}, null, 2)}
              </pre>
            </CollapsibleBlock>
            <div style={{ fontSize: '0.7rem', color: colors.overlay0, marginLeft: '0.5rem' }}>
              {relativeTime(ts)}
            </div>
          </div>
        );

      case 'tool_result':
        const success = evt.request?.success !== false;
        return (
          <div key={key} style={{ marginBottom: '0.4rem' }}>
            <CollapsibleBlock
              header={
                <span>
                  📋 <strong>Result:</strong> {evt.request?.tool || 'unknown'}
                  {' '}{success ? '✅' : '❌'}
                </span>
              }
              headerColor={success ? colors.green : colors.red}
            >
              {success ? (
                <pre
                  style={{
                    margin: 0,
                    fontSize: '0.75rem',
                    color: colors.subtext0,
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-word',
                    fontFamily: 'monospace',
                  }}
                >
                  {evt.response?.result || '(empty)'}
                </pre>
              ) : (
                <div style={{ color: colors.red, fontSize: '0.8rem' }}>
                  {evt.request?.error || evt.response?.result || 'Unknown error'}
                </div>
              )}
            </CollapsibleBlock>
            <div style={{ fontSize: '0.7rem', color: colors.overlay0, marginLeft: '0.5rem' }}>
              {relativeTime(ts)}
            </div>
          </div>
        );

      case 'system_notification': {
        const resp = evt.response || {};
        const notifType = resp.type || 'info';
        let subtitle = '';
        if (resp.token_count !== undefined) subtitle = `Tokens: ${resp.token_count}`;
        else if (resp.turn_count !== undefined) subtitle = `Turns: ${resp.turn_count}`;
        else if (resp.elapsed_seconds !== undefined) subtitle = `Elapsed: ${resp.elapsed_seconds}s`;

        return (
          <div key={key} style={{ marginBottom: '0.4rem', textAlign: 'center' }}>
            <div
              style={{
                display: 'inline-block',
                background: colors.mantle,
                border: `1px solid ${colors.surface1}`,
                borderRadius: '8px',
                padding: '0.3rem 0.7rem',
                fontSize: '0.78rem',
                color: colors.yellow,
                maxWidth: '90%',
                wordBreak: 'break-word',
              }}
            >
              ⚠️ [SYSTEM] {resp.message || ''}
              {subtitle && (
                <div style={{ fontSize: '0.7rem', color: colors.overlay0, marginTop: '0.15rem' }}>
                  {subtitle}
                </div>
              )}
            </div>
          </div>
        );
      }

      case 'final_response':
        return (
          <div key={key} style={{ display: 'flex', justifyContent: 'flex-start', marginBottom: '0.4rem' }}>
            <div
              style={{
                background: colors.surface0,
                color: colors.text,
                borderRadius: '12px 12px 12px 4px',
                padding: '0.45rem 0.75rem',
                maxWidth: '85%',
                fontSize: '0.82rem',
                lineHeight: '1.4',
                wordBreak: 'break-word',
              }}
            >
              {evt.response?.content || ''}
              {evt.response?.reasoning && (
                <div style={{ fontSize: '0.7rem', color: colors.overlay0, marginTop: '0.3rem', fontStyle: 'italic' }}>
                  (reasoning mode)
                </div>
              )}
            </div>
          </div>
        );

      case 'error':
        return (
          <div key={key} style={{ marginBottom: '0.4rem' }}>
            <div style={{ color: colors.red, fontSize: '0.82rem', wordBreak: 'break-word' }}>
              ❌ {evt.response?.error || evt.request?.error || 'Unknown error'}
            </div>
          </div>
        );

      // Legacy / lifecycle events
      case 'started':
      case 'completed':
      case 'stopped':
        return (
          <div key={key} style={{ textAlign: 'center', marginBottom: '0.4rem' }}>
            <span
              style={{
                display: 'inline-block',
                background: colors.mantle,
                color: colors.overlay0,
                borderRadius: '10px',
                padding: '0.15rem 0.6rem',
                fontSize: '0.72rem',
                fontWeight: 600,
              }}
            >
              {evt.event === 'started'
                ? '⬤ Worker started'
                : evt.event === 'completed'
                  ? '■ Worker completed'
                  : '⏹ Worker stopped'}
            </span>
          </div>
        );

      case 'query':
        // Old format — treat like user_message
        return (
          <div key={key} style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: '0.4rem' }}>
            <div
              style={{
                background: colors.surface1,
                color: colors.text,
                borderRadius: '12px 12px 4px 12px',
                padding: '0.45rem 0.75rem',
                maxWidth: '85%',
                fontSize: '0.82rem',
                wordBreak: 'break-word',
              }}
            >
              {typeof evt.request === 'string'
                ? evt.request
                : evt.request?.query || JSON.stringify(evt.request)}
            </div>
          </div>
        );

      default:
        // Unknown event type — show raw JSON
        return (
          <div key={key} style={{ marginBottom: '0.3rem' }}>
            <div style={{ fontSize: '0.7rem', color: colors.overlay0 }}>
              {relativeTime(ts)} <span style={{ color: colors.subtext0 }}>{evt.event}</span>
            </div>
            <pre
              style={{
                margin: 0,
                fontSize: '0.72rem',
                color: colors.subtext0,
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
                fontFamily: 'monospace',
                background: colors.mantle,
                padding: '0.3rem 0.5rem',
                borderRadius: '4px',
              }}
            >
              {JSON.stringify(evt, null, 2)}
            </pre>
          </div>
        );
    }
  }

  // ── No worker selected state ──────────────────────────────────────────
  if (!workerName) {
    return (
      <div
        style={{
          width: panelWidth,
          minWidth: PANEL_MIN,
          maxWidth: PANEL_MAX,
          flexShrink: 0,
          background: colors.base,
          borderLeft: `1px solid ${colors.surface1}`,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          height: '100%',
          fontFamily: 'sans-serif',
        }}
      >
        <div style={{ textAlign: 'center', color: colors.overlay0, fontSize: '0.85rem', padding: '1rem' }}>
          No worker selected.<br />Click a worker to view its output.
        </div>
      </div>
    );
  }

  // ── Render ────────────────────────────────────────────────────────────
  return (
    <div
      style={{
        display: 'flex',
        height: '100%',
        fontFamily: 'sans-serif',
      }}
    >
      {/* Resize handle (left edge) */}
      <div
        onMouseDown={handleResizeStart}
        title="Drag to resize"
        style={{
          width: '5px',
          cursor: 'col-resize',
          background: 'transparent',
          flexShrink: 0,
          position: 'relative',
          zIndex: 10,
          transition: 'background 0.15s',
        }}
        onMouseEnter={(e) => { e.currentTarget.style.background = colors.accent; }}
        onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent'; }}
      />

      {/* Panel content */}
      <div
        style={{
          width: panelWidth,
          minWidth: PANEL_MIN,
          maxWidth: PANEL_MAX,
          flexShrink: 0,
          background: colors.base,
          display: 'flex',
          flexDirection: 'column',
          height: '100%',
          overflow: 'hidden',
        }}
      >
        {/* ── Status bar ─────────────────────────────────────────────── */}
        <div
          style={{
            padding: '0.6rem 0.75rem',
            borderBottom: `1px solid ${colors.surface1}`,
            flexShrink: 0,
          }}
        >
          {/* Worker name + status dot */}
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', marginBottom: '0.25rem' }}>
            <span
              style={{
                width: '8px',
                height: '8px',
                borderRadius: '50%',
                background: statusDotColor(runtimeStatus),
                flexShrink: 0,
              }}
            />
            <span style={{ fontWeight: 600, fontSize: '0.9rem', color: colors.text }}>
              {workerName}
            </span>
            <span style={{ fontSize: '0.72rem', color: colors.overlay0, textTransform: 'capitalize' }}>
              {runtimeStatus}
            </span>
            <div style={{ flex: 1 }} />
            {onClose && (
              <button
                onClick={onClose}
                title="Close panel"
                style={{
                  background: 'transparent',
                  color: colors.overlay0,
                  border: 'none',
                  cursor: 'pointer',
                  fontSize: '1rem',
                  lineHeight: 1,
                  padding: '0.15rem 0.3rem',
                  borderRadius: '4px',
                  transition: 'background 0.15s',
                }}
                onMouseEnter={(e) => { e.currentTarget.style.background = colors.surface1; }}
                onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent'; }}
              >
                ✕
              </button>
            )}
          </div>

          {/* Current task */}
          {workerInfo?.current_task && (
            <div
              style={{
                fontSize: '0.78rem',
                color: colors.subtext0,
                marginBottom: '0.2rem',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
              title={workerInfo.current_task}
            >
              {truncate(workerInfo.current_task, 80)}
            </div>
          )}

          {/* Elapsed time + Stop button */}
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '0.5rem' }}>
            <span style={{ fontSize: '0.75rem', color: colors.overlay0 }}>
              ⏱ {elapsedTime(startTime)}
            </span>
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
              {stopError && (
                <span style={{ fontSize: '0.7rem', color: colors.red }}>{stopError}</span>
              )}
              <button
                onClick={handleStop}
                disabled={!canStop}
                style={{
                  background: canStop ? colors.red : colors.surface2,
                  color: canStop ? colors.base : colors.overlay0,
                  border: 'none',
                  borderRadius: '4px',
                  padding: '0.2rem 0.6rem',
                  cursor: canStop ? 'pointer' : 'not-allowed',
                  fontWeight: 600,
                  fontSize: '0.72rem',
                  lineHeight: '1.4',
                }}
              >
                ⏹ Stop
              </button>
            </div>
          </div>
        </div>

        {/* ── Conversation stream ────────────────────────────────────── */}
        <div
          ref={scrollRef}
          onScroll={handleScroll}
          style={{
            flex: 1,
            overflowY: 'auto',
            padding: '0.6rem 0.75rem',
            background: colors.base,
            position: 'relative',
          }}
        >
          {events.length === 0 && !eventsError && (
            <div style={{ textAlign: 'center', color: colors.overlay0, padding: '2rem', fontSize: '0.85rem' }}>
              {workerError || 'No events yet.'}
            </div>
          )}

          {events.length === 0 && eventsError && (
            <div style={{ textAlign: 'center', color: colors.red, padding: '2rem', fontSize: '0.82rem' }}>
              Events unavailable.
            </div>
          )}

          {events.map((evt, idx) => renderEvent(evt, idx))}

          {/* Floating "New events" button */}
          {hasNewEvents && (
            <NewEventsButton onClick={scrollToBottom} />
          )}
        </div>
      </div>
    </div>
  );
}

export default WorkerOutputPanel;
