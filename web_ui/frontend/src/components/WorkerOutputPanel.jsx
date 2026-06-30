import React, { useState, useEffect, useCallback, useRef } from 'react';

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

// ── "New events" floating button ──────────────────────────────────────
function NewEventsButton({ onClick }) {
  return (
    <div className="new-events-container" onClick={onClick}>
      <button className="new-events-btn">
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
            // Worker might have not appeared yet; only set error
            // if we've never had any data (check via ref, not state)
            if (!workerInfoRef.current) setWorkerError('Worker not found');
          }
        }
      })
      .catch((err) => {
        if (!workerInfoRef.current) setWorkerError(err.message);
      });
  }, [workspaceId, workerName]);

  const workerInfoRef = useRef(null);
  workerInfoRef.current = workerInfo;

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
    lastFetchTimeRef.current = null;
    eventsRef.current = [];
  }, [workspaceId, workerName]);

  // ── Events fetching with polling ──────────────────────────────────────
  const [events, setEvents] = useState([]);
  const [eventsError, setEventsError] = useState('');
  const [lastFetchTime, setLastFetchTime] = useState(null);
  const [hasNewEvents, setHasNewEvents] = useState(false);
  const scrollRef = useRef(null);
  const isAtBottomRef = useRef(true);
  const lastFetchTimeRef = useRef(null);
  const eventsRef = useRef([]);

  const runtimeStatus = workerInfo?.runtime_status || 'idle';

  const fetchEvents = useCallback(() => {
    if (!workspaceId || !workerName) return;
    const params = new URLSearchParams();
    params.set('limit', '50');
    if (lastFetchTimeRef.current) params.set('since', lastFetchTimeRef.current);

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
            const updated = [...prev, ...newEntries];
            eventsRef.current = updated;
            return updated;
          });
          const lastTs = data[data.length - 1].timestamp;
          if (lastTs) {
            lastFetchTimeRef.current = lastTs;
            setLastFetchTime(lastTs);
          }
        }
        setEventsError('');
      })
      .catch((err) => {
        if (eventsRef.current.length === 0) setEventsError(err.message);
      });
  }, [workspaceId, workerName]); // stable deps only — no cascading re-fetches

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
          <div key={key} className="worker-event worker-event-user">
            <div className="worker-event-bubble">
              {evt.request?.query || JSON.stringify(evt.request)}
            </div>
          </div>
        );

      case 'tool_call':
        const toolName = evt.request?.tool || 'unknown';
        const argsStr = JSON.stringify(evt.request?.args || {}, null, 2);
        const argsLines = argsStr.split('\n').length;
        return (
          <div key={key} className="worker-event">
            <div className="worker-event-tool-call-bubble">
              <div className="worker-event-tool-header">
                <span className="worker-event-tool-icon">🔧</span>
                <strong>{toolName}</strong>
                <span className="worker-event-tool-args-badge">{argsLines - 2} args</span>
              </div>
              <div className="worker-event-tool-args">
                <pre className="worker-event-tool-pre">{argsStr}</pre>
              </div>
            </div>
            <div className="worker-event-timestamp">
              {relativeTime(ts)}
            </div>
          </div>
        );

      case 'tool_result':
        const resultSuccess = evt.request?.success !== false;
        const resultStr = resultSuccess
          ? (evt.response?.result || '(empty)')
          : (evt.request?.error || evt.response?.result || 'Unknown error');
        const resultTruncated = resultStr.length > 500
          ? resultStr.slice(0, 500) + '…'
          : resultStr;
        return (
          <div key={key} className="worker-event">
            <div className={`worker-event-tool-result-bubble ${resultSuccess ? 'worker-event-tool-result-ok' : 'worker-event-tool-result-err'}`}>
              <div className="worker-event-tool-header">
                <span className="worker-event-tool-icon">{resultSuccess ? '✅' : '❌'}</span>
                <strong>{evt.request?.tool || 'unknown'} result</strong>
                {resultStr !== resultTruncated && (
                  <span className="worker-event-tool-truncated">truncated</span>
                )}
              </div>
              <div className="worker-event-tool-result-content">
                <pre className="worker-event-tool-pre">{resultTruncated}</pre>
              </div>
            </div>
            <div className="worker-event-timestamp">
              {relativeTime(ts)}
            </div>
          </div>
        );

      case 'system_notification': {
        const resp = evt.response || {};
        let subtitle = '';
        if (resp.token_count !== undefined) subtitle = `Tokens: ${resp.token_count}`;
        else if (resp.turn_count !== undefined) subtitle = `Turns: ${resp.turn_count}`;
        else if (resp.elapsed_seconds !== undefined) subtitle = `Elapsed: ${resp.elapsed_seconds}s`;

        return (
          <div key={key} className="worker-event-system">
            <div className="worker-event-pill">
              ⚠️ [SYSTEM] {resp.message || ''}
              {subtitle && (
                <div className="worker-event-system-subtitle">
                  {subtitle}
                </div>
              )}
            </div>
          </div>
        );
      }

      case 'final_response':
        return (
          <div key={key} className="worker-event worker-event-assistant">
            <div className="worker-event-bubble">
              {evt.response?.content || ''}
              {evt.response?.reasoning && (
                <div className="worker-event-reasoning-note">
                  (reasoning mode)
                </div>
              )}
            </div>
          </div>
        );

      case 'error':
        return (
          <div key={key} className="worker-event-error">
            <div className="worker-event-errortext">
              ❌ {evt.response?.error || evt.request?.error || 'Unknown error'}
            </div>
          </div>
        );

      // Legacy / lifecycle events
      case 'started':
      case 'completed':
      case 'stopped':
        return (
          <div key={key} className="worker-event-lifecycle">
            <span className="worker-event-pill">
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
          <div key={key} className="worker-event worker-event-user">
            <div className="worker-event-bubble">
              {typeof evt.request === 'string'
                ? evt.request
                : evt.request?.query || JSON.stringify(evt.request)}
            </div>
          </div>
        );

      default:
        // Unknown event type — show raw JSON
        return (
          <div key={key} className="worker-event-unknown">
            <div className="worker-event-unknown-label">
              {relativeTime(ts)} <span className="worker-event-unknown-event">{evt.event}</span>
            </div>
            <pre className="worker-event-unknown-json">
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
        className="worker-output-no-selection"
        style={{
          width: panelWidth,
          minWidth: PANEL_MIN,
          maxWidth: PANEL_MAX,
          flexShrink: 0,
          borderLeft: '1px solid var(--border)',
        }}
      >
        <div className="worker-output-no-selection-text">
          No worker selected.<br />Click a worker to view its output.
        </div>
      </div>
    );
  }

  // ── Render ────────────────────────────────────────────────────────────
  return (
    <div className="worker-output-wrapper">
      {/* Resize handle (left edge) */}
      <div
        className="resize-handle"
        onMouseDown={handleResizeStart}
        title="Drag to resize"
      />

      {/* Panel content */}
      <div
        className="worker-output-inner"
        style={{
          width: panelWidth,
          minWidth: PANEL_MIN,
          maxWidth: PANEL_MAX,
          flexShrink: 0,
        }}
      >
        {/* ── Status bar ─────────────────────────────────────────────── */}
        <div className="worker-output-header">
          {/* Worker name + status dot */}
          <div className="worker-output-header-top">
            <span
              className="worker-status-dot"
              style={{ background: statusDotColor(runtimeStatus) }}
            />
            <span className="worker-output-header-name">
              {workerName}
            </span>
            <span className="worker-output-header-status">
              {runtimeStatus}
            </span>
            <div style={{ flex: 1 }} />
            {onClose && (
              <button
                className="worker-output-close-btn"
                onClick={onClose}
                title="Close panel"
              >
                ✕
              </button>
            )}
          </div>

          {/* Current task */}
          {workerInfo?.current_task && (
            <div
              className="worker-output-header-task"
              title={workerInfo.current_task}
            >
              {truncate(workerInfo.current_task, 80)}
            </div>
          )}

          {/* Elapsed time + Stop button */}
          <div className="worker-output-header-bottom">
            <span className="worker-output-elapsed">
              ⏱ {elapsedTime(startTime)}
            </span>
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
              {stopError && (
                <span style={{ fontSize: '0.7rem', color: 'var(--danger)' }}>{stopError}</span>
              )}
              <button
                className="worker-output-stop-btn"
                onClick={handleStop}
                disabled={!canStop}
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
          className="worker-output-scroll"
        >
          {events.length === 0 && !eventsError && (
            <div className="worker-output-empty">
              {workerError || 'No events yet.'}
            </div>
          )}

          {events.length === 0 && eventsError && (
            <div className="worker-output-error">
              Events unavailable.
            </div>
          )}

          {events.map((evt, idx) => renderEvent(evt, idx))}

          {/* Floating "New events" button */}
          {hasNewEvents && (
            <NewEventsButton onClick={scrollToBottom} />
          )}

          {/* Scroll navigation buttons */}
          <div className="worker-scroll-nav">
            <button
              className="scroll-bottom-btn"
              onClick={scrollToBottom}
              title="Scroll to bottom"
            >
              ↓
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export default WorkerOutputPanel;
