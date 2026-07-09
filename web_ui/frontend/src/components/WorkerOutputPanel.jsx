import React, { useState, useEffect, useCallback, useRef } from 'react';
import { MessageBubble } from './chat/MessageBubble';
import adaptWorkerEvent from './chat/adaptWorkerEvent';

const PANEL_MIN = 250;
const PANEL_MAX = 600;
const PANEL_DEFAULT = 350;

const STATUS_DOT = {
  ready: { bg: '#a6e3a1', label: 'Ready' },      /* green solid — alive, waiting */
  busy: { bg: '#a6e3a1', label: 'Busy' },        /* green pulsing — processing */
  completed: { bg: '#6c7086', label: 'Completed' }, /* grey — done */
  error: { bg: '#f38ba8', label: 'Error' },       /* red — failed */
  stopped: { bg: '#f38ba8', label: 'Stopped' },   /* red — stopped */
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

function formatTokens(n) {
  if (n == null) return '—';
  return n >= 1000 ? (n / 1000).toFixed(1) + 'K' : String(n);
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
function WorkerOutputPanel({ workspaceId, workerName, sessionId, onClose, incomingEvents = [] }) {
  // Panel resize state (self-contained)
  const [panelWidth, setPanelWidth] = useState(PANEL_DEFAULT);
  const dragRef = useRef(null);
  const panelRef = useRef(null);
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
    const startX = e.clientX;
    const startWidth = panelWidth;
    dragRef.current = 'dragging';
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';

    const handleMouseMove = (e) => {
      if (!dragRef.current) return;
      // Panel is on the right → dragging left decreases width
      const delta = e.clientX - startX;
      const newWidth = Math.max(PANEL_MIN, Math.min(PANEL_MAX, startWidth - delta));
      // Direct DOM manipulation for smooth jank-free resize
      if (panelRef.current) {
        panelRef.current.style.width = `${newWidth}px`;
      }
    };

    const handleMouseUp = () => {
      dragRef.current = null;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleMouseUp);
      // Sync final width back to React state
      if (panelRef.current) {
        const finalWidth = parseInt(panelRef.current.style.width, 10);
        if (finalWidth && finalWidth !== panelWidth) {
          setPanelWidth(finalWidth);
        }
      }
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
  }, [workspaceId, workerName, sessionId]);

  const workerInfoRef = useRef(null);
  workerInfoRef.current = workerInfo;

  const runtimeStatus = workerInfo?.runtime_status || 'ready';

  // Poll period for worker info: 1s if busy (aligns with events), 3s otherwise
  const workerInfoPollInterval = runtimeStatus === 'busy' ? 1000 : 3000;

  useEffect(() => {
    fetchWorkerInfo();
    const interval = setInterval(fetchWorkerInfo, workerInfoPollInterval);
    return () => clearInterval(interval);
  }, [fetchWorkerInfo, workerInfoPollInterval]);

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

  // ── Merge incoming WS events (from bridge via SessionTab) ────────────
  // Filter by worker name so cross-session WS events are correctly routed
  useEffect(() => {
    if (!incomingEvents || incomingEvents.length === 0) return
    const relevantEvents = incomingEvents.filter(e => {
      const evtWorkerName = e.worker_name || e.response?.worker_name
      return !evtWorkerName || evtWorkerName === workerName
    })
    if (relevantEvents.length === 0) return
    setEvents(prev => {
      const existingKeys = new Set(prev.map(e => e.event + (e.timestamp || '')))
      const newOnes = relevantEvents
        .filter(e => !existingKeys.has((e.type?.replace('worker:', '') || '') + (e.timestamp || '')))
        .map(e => ({
          event: e.type?.replace('worker:', '') || 'unknown',
          timestamp: e.timestamp,
          request: e.request || {},
          response: e.response || { error: e.error, status: e.status, worker_name: e.worker_name },
        }))
      if (newOnes.length === 0) return prev
      const updated = [...prev, ...newOnes]
      eventsRef.current = updated
      return updated
    })
  }, [incomingEvents, workerName])

  // ── Events fetching with polling ──────────────────────────────────────
  const [events, setEvents] = useState([]);
  const [eventsError, setEventsError] = useState('');
  const [lastFetchTime, setLastFetchTime] = useState(null);
  const [hasNewEvents, setHasNewEvents] = useState(false);
  const scrollRef = useRef(null);
  const isAtBottomRef = useRef(true);
  const lastFetchTimeRef = useRef(null);
  const eventsRef = useRef([]);

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
  const pollInterval = runtimeStatus === 'busy' ? 1000 : 3000;

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

  // ── Stop handler (session-scoped control) ───────────────────────────────
  const handleStop = useCallback(async () => {
    if (!workspaceId || !workerName) return;
    // Block control from non-owning sessions
    if (workerInfo && workerInfo.session_id && workerInfo.session_id !== sessionId) {
      setStopError('Cannot stop worker from another session');
      setTimeout(() => setStopError(''), 3000);
      return;
    }
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

  const canStop = runtimeStatus === 'busy' || runtimeStatus === 'ready';

  // ── Compute elapsed time from first event ─────────────────────────────
  const startTime = workerInfo?.started ||
    (events.length > 0 ? events[0].timestamp : null);

  // ── Elapsed timer tick ────────────────────────────────────────────────
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (runtimeStatus !== 'busy') return;
    const interval = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(interval);
  }, [runtimeStatus]);

  // ── Render helpers ────────────────────────────────────────────────────
  // renderEvent removed in Phase B Step 3 — replaced by adaptWorkerEvent + MessageBubble below

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

  const latestEvent = events.length > 0 ? events[events.length - 1] : null;

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
        ref={panelRef}
        className="worker-output-inner"
        style={{
          width: panelWidth,
          minWidth: PANEL_MIN,
          maxWidth: PANEL_MAX,
          flexShrink: 0,
        }}
      >
        {/* ── Status bar (slim, matching main StatusBar) ────────────── */}
        <div className="worker-output-header">
          <span
            className="worker-status-dot"
            style={{ background: statusDotColor(runtimeStatus) }}
          />
          <span className="worker-output-header-label">
            Worker: {workerName}
          </span>
          <span className="worker-output-header-ctx">
            ctx: {(workerInfo || events.length > 0) ? `${formatTokens(latestEvent?.current_context_tokens ?? workerInfo?.current_context_tokens ?? 0)} / ${formatTokens(latestEvent?.max_context_tokens ?? workerInfo?.max_context_tokens ?? 0)}` : '—'}
          </span>
          <div style={{ flex: 1 }} />
          {workerInfo?.current_task && (
            <span className="worker-output-header-task-inline" title={workerInfo.current_task}>
              {truncate(workerInfo.current_task, 50)}
            </span>
          )}
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

          {events.map((evt, idx) => {
            const msg = adaptWorkerEvent(evt);
            if (!msg) return null;  // suppress events like user_message / query
            const key = msg._id || (evt.timestamp + evt.event + idx);
            return (
              <div key={key} className="worker-event-row">
                <span className="worker-event-timestamp">
                  {relativeTime(evt.timestamp)}
                </span>
                <MessageBubble msg={msg} index={idx} />
              </div>
            );
          })}

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

        {/* ── Bottom bar (matches main QueryBar height) ─────────────── */}
        <div className="worker-output-bottom-bar">
          <span className="worker-output-bottom-name" title={workerName}>
            {truncate(workerName, 15)}
          </span>
          <span className="worker-output-bottom-elapsed">
            ⏱ {elapsedTime(startTime)}
          </span>
          <div style={{ flex: 1 }} />
          {stopError && (
            <span className="worker-output-bottom-error">{stopError}</span>
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
  );
}

export default WorkerOutputPanel;
