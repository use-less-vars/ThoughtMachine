import React, { useState, useEffect, useCallback, useRef } from 'react';
import { MessageBubble } from './chat/MessageBubble';
import adaptWorkerEvent from './chat/adaptWorkerEvent';

const PANEL_MIN = 250;
const PANEL_MAX = 600;
const PANEL_DEFAULT = 350;

const STATUS_DOT = {
  ready: { bg: '#585b70', label: 'Idle' },        /* grey — spawned but not doing anything */
  busy: { bg: '#a6e3a1', label: 'Running' },      /* green with pulse — actively processing */
  completed: { bg: '#6c7086', label: 'Completed' }, /* muted grey — done */
  error: { bg: '#f38ba8', label: 'Error' },       /* red — failed */
  stopped: { bg: '#313244', label: 'Stopped' },   /* dark/off — not spawned */
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
            setWorkerInfo(prev => {
              if (!prev) return found;
              return { ...found, ...prev, runtime_status: prev.runtime_status ?? found.runtime_status };
            });
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

  // Worker info is updated via WebSocket incomingEvents (no polling)

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
    console.log('[WorkerOutputPanel] WS incomingEvents:', relevantEvents.length, 'events for', workerName, relevantEvents.map(e=>e.type).join(','));

    // Also update live workerInfo status from WS events (instant, no poll lag)
    for (const e of relevantEvents) {
      const eventType = e.type?.replace('worker:', '')
      const status = e.data?.runtime_status || e.data?.status
      if (eventType === 'worker_spawned' && (status === 'ready' || status === 'busy')) {
        setWorkerInfo(prev => {
          const update = { runtime_status: status };
          if (!prev) return update;
          return { ...prev, ...update };
        })
      } else if (eventType === 'worker_status' && status) {
        setWorkerInfo(prev => {
          const update = { runtime_status: status, current_task: e.data?.current_task || prev?.current_task };
          if (!prev) return update;
          return { ...prev, ...update };
        })
      } else if (eventType === 'worker_completed') {
        setWorkerInfo(prev => {
          const update = { runtime_status: 'ready' };
          if (!prev) return update;
          return { ...prev, ...update };
        })
      } else if (eventType === 'worker_error') {
        setWorkerInfo(prev => {
          const update = { runtime_status: 'error', error: e.data?.error || '' };
          if (!prev) return update;
          return { ...prev, ...update };
        })
      }
    }

    setEvents(prev => {
      const existingKeys = new Set(prev.map(e => e.event + (e.timestamp || '')))
      const newOnes = relevantEvents
        .filter(e => !existingKeys.has((e.type?.replace('worker:', '') || '') + (e.timestamp || '')))
        .map(e => {
          const eventType = e.type?.replace('worker:', '') || 'unknown'
          let request = e.request || {}
          let response = e.response || {}

          switch (eventType) {
            case 'tool_call': {
              const data = e.data || {}
              let args = {}
              try {
                if (data.arguments) {
                  args = typeof data.arguments === 'string' ? JSON.parse(data.arguments) : data.arguments
                }
              } catch (_) { /* ignore parse errors */ }
              request = { tool: data.tool_name || 'unknown', args }
              break
            }
            case 'tool_result': {
              const data = e.data || {}
              request = { tool: data.tool_name || 'unknown', success: data.success !== false }
              response = { result: data.result || '' }
              break
            }
            case 'assistant_message': {
              const data = e.data || {}
              // Map to worker_message since adaptWorkerEvent handles that case
              response = { content: data.content || '', reasoning_content: data.reasoning_content || undefined }
              return {
                event: 'worker_message',
                timestamp: e.timestamp,
                request: {},
                response,
              }
            }
            case 'token_warning': {
              const data = e.data || {}
              response = { type: 'token_warning', message: data.warning_message || '', token_count: data.token_count }
              return {
                event: 'system_notification',
                timestamp: e.timestamp,
                request: {},
                response,
              }
            }
            default:
              // For lifecycle events (worker_spawned, worker_status, etc.), keep existing behavior
              if (!e.request && !e.response) {
                request = {}
                response = e.data || { error: e.error, status: e.status, worker_name: e.worker_name }
              }
              break
          }

          return {
            event: eventType,
            timestamp: e.timestamp,
            request,
            response,
          }
        })
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
    console.log('[WorkerOutputPanel] fetchEvents called for', workerName, 'limit=50', params.toString());

    fetch(`/api/workspace/${workspaceId}/workers/${encodeURIComponent(workerName)}/events?${params}`)
      .then(async (res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (Array.isArray(data) && data.length > 0) {
          console.log('[WorkerOutputPanel] fetched', data.length, 'events for', workerName, 'types:', data.map(e=>e.event).join(','));
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
        } else {
          console.log('[WorkerOutputPanel] no new events for', workerName);
        }
        setEventsError('');
      })
      .catch((err) => {
        if (eventsRef.current.length === 0) setEventsError(err.message);
      });
  }, [workspaceId, workerName]); // stable deps only — no cascading re-fetches

  // Events are received via WebSocket incomingEvents prop (no polling)

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

  // Build a set of all final (worker_message) content strings for dedup
  const finalContentSet = new Set()
  events.forEach(evt => {
    if (evt.event === 'worker_message') {
      const content = evt.response?.content || ''
      if (content) finalContentSet.add(content.trim())
    }
  })

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
            className={'worker-status-dot' + (runtimeStatus === 'busy' ? ' worker-status-dot-busy' : '')}
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

          {/* Dedup: content strings from worker_message events */}
          {events.map((evt, idx) => {
            const msg = adaptWorkerEvent(evt);
            // Skip lifecycle events (worker_spawned, worker_status,
            // worker_completed, worker_error) from filesystem polling.
            // These come in real-time via WebSocket (incomingEvents) and
            // should NOT be duplicated from the events.jsonl file.
            if (msg && msg.is_worker_event) {
              return null;
            }
            // Dedup: skip tool_call/tool_result events if their content
            // matches content already shown in a worker_message (final response)
            if (msg && (evt.event === 'tool_result' || evt.event === 'tool_call')) {
              const msgContent = msg.content || msg.tool_input || msg.result || ''
              if (msgContent && typeof msgContent === 'string') {
                const trimmed = msgContent.trim()
                if (trimmed) {
                  let isRedundant = false
                  for (const finalContent of finalContentSet) {
                    if (finalContent.includes(trimmed) || trimmed.includes(finalContent)) {
                      isRedundant = true
                      break
                    }
                  }
                  if (isRedundant) {
                    console.log('[WorkerOutputPanel] dedup: skipping', evt.event, 'as its content appears in worker_message')
                    return null
                  }
                }
              }
            }
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
