import React, { memo, useState, useEffect, useCallback, useRef, useLayoutEffect } from 'react';
import { MessageBubble } from './chat/MessageBubble';
import adaptWorkerEvent, { isWorkerEventRenderable } from './chat/adaptWorkerEvent';
import useStore from '../store/useStore';

const PANEL_MIN = 250;
const PANEL_MAX = 600;
const PANEL_DEFAULT = 350;
const noop = () => {};


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
function WorkerOutputPanel({
  workspaceId,
  workerName,
  instanceId,
  instanceLabel,
  sessionId,
  onClose,
  incomingEvents = [],
  size = PANEL_DEFAULT,
  maximized = false,
  pinned = false,
  onResize = noop,
  onToggleMaximize,
  onTogglePin,
}) {

  
  // Panel width is controlled by the parent via props.size. The drag only
  // manipulates the DOM directly for smoothness, then reports the final
  // width via onResize (no localStorage persistence here anymore).
  const dragRef = useRef(null);
  const panelRef = useRef(null);

  const handleResizeStart = useCallback((e) => {
    e.preventDefault();
    const startX = e.clientX;
    const startWidth = size;
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
      // Report final width back to the parent (state now lives there)
      if (panelRef.current) {
        const finalWidth = parseInt(panelRef.current.style.width, 10);
        if (finalWidth && finalWidth !== size) {
          onResize(finalWidth);
        }
      }
    };

    document.addEventListener('mousemove', handleMouseMove);
    document.addEventListener('mouseup', handleMouseUp);
  }, [size, onResize]);

  // ── Worker info (name, status, current_task) ──────────────────────────
  const [workerInfo, setWorkerInfo] = useState(null);
  const [workerError, setWorkerError] = useState('');
  const [stopError, setStopError] = useState('');

  const workerInfoRef = useRef(null);
  workerInfoRef.current = workerInfo;

  const runtimeStatus = workerInfo?.runtime_status || 'ready';

  // F5: the status dot follows the owning session's running state — the same
  // signal as the session tab's Running/Idle indicator. It survives refreshes
  // because it comes from the store, not from the (possibly empty) event list.
  const isRunning = useStore((s) => (sessionId ? (s.sessionStates[sessionId]?.isRunning ?? false) : false));

  // Worker info is updated via WebSocket incomingEvents (no polling)

  // Reset when the worker identity OR the owning session changes.
  // App does not key this panel by sessionId, so switching sessions while the
  // same worker is selected changes props without remounting — without
  // sessionId in the deps the previous session's workerInfo (ctx counter,
  // current_task, runtime_status) and event stream would leak into the new
  // session's panel (stale header / stale stream).
  useEffect(() => {
    setWorkerInfo(null);
    setWorkerError('');
    setStopError('');
    setEvents([]);
    eventsRef.current = [];
    seenEventKeysRef.current = new Set();
  }, [workspaceId, workerName, instanceId, sessionId]);

  // ── Shared dedup key computation ──────────────────────────────────────
  // Events are received via WebSocket incomingEvents prop (no polling).
  // Normalize all assistant-message-like event names to a single canonical key
  // so that different WS event name variants representing the same logical event
  // are correctly deduplicated against each other.
  const seenEventKeysRef = useRef(new Set());

  // Helper: compute a dedup key for any event (either stored internal format
  // or raw incoming WS event).  Returns a string that is the same for any
  // event name variant of the same logical event at the same timestamp.
  const makeDedupKey = (eventName, timestamp) => {
    // Assistant-message-like events all map to the same canonical key
    let canonical
    if (
      eventName === 'worker_message' ||
      eventName === 'final_response' ||
      eventName === 'assistant_message' ||
      eventName === 'worker:worker_message' ||
      eventName === 'worker:assistant_message'
    ) {
      canonical = 'final_response'
    } else if (
      // Token warnings arrive via two backend paths:
      //   Path A (per-worker bus): worker:token_warning  → rawType='token_warning'
      //   Path B (global bus):     worker:system_notification → rawType='system_notification'
      // Both represent the same logical warning; canonicalize to the same key
      // so the second arrival is deduplicated away.
      eventName === 'token_warning' ||
      eventName === 'system_notification'
    ) {
      canonical = 'system_notification'
    } else {
      canonical = eventName
    }
    return canonical + '|' + (timestamp || '')
  }

  // ── Merge incoming WS events (from bridge via SessionTab) ────────────
  // Filter by worker name so cross-session WS events are correctly routed
  useEffect(() => {
    if (!incomingEvents || incomingEvents.length === 0) {
      return
    }

    console.log('[TOKEN_PIPELINE] WorkerOutputPanel: incomingEvents raw count=' + incomingEvents.length + ', total=', incomingEvents.map(e => ({type: e.type, worker_name: e.worker_name, context_length: e.context_length, critical_threshold: e.critical_threshold})));

    const relevantEvents = incomingEvents.filter(e => {
      const evtWorkerName = e.worker_name || e.response?.worker_name
      // REQUIRE a worker_name — events without one are main-agent events, not worker events
      if (!evtWorkerName || evtWorkerName !== workerName) return false
      // Instance scoping: match by id when both sides have it; otherwise fall
      // back to label when both sides have it. Events without comparable
      // instance info are kept for backward compatibility with older streams.
      const evtInstanceId = e.instance_id ?? e.response?.instance_id
      const evtInstanceLabel = e.instance_label ?? e.response?.instance_label
      if (evtInstanceId != null && instanceId != null) {
        return evtInstanceId === instanceId
      }
      if (evtInstanceLabel && instanceLabel) {
        return evtInstanceLabel === instanceLabel
      }
      return true
    })

    if (relevantEvents.length === 0) {
      console.log('[WorkerOutputPanel] incomingEvents: none relevant (workerName=' + workerName + ')');
      console.log('[TOKEN_PIPELINE] WorkerOutputPanel: ZERO relevant events — worker_name mismatch or absent');
      return
    }
    console.log('[WorkerOutputPanel] incomingEvents: processing', relevantEvents.length, 'events types:', relevantEvents.map(e=>e.type).join(','));

    // Also update live workerInfo status from WS events (instant, no poll lag)
    for (const e of relevantEvents) {
      const eventType = e.type?.replace('worker:', '')
      const status = e.data?.runtime_status || e.data?.status
      const tokensUpdate = {}
            if (eventType === 'context_updated' && e.context_length != null) {
              tokensUpdate.current_context_tokens = e.context_length;
              tokensUpdate.max_context_tokens = e.critical_threshold ?? tokensUpdate.max_context_tokens ?? 80000;
              console.log('[TOKEN_PIPELINE] WorkerOutputPanel: context_updated processing', {
                eventType, context_length: e.context_length, critical_threshold: e.critical_threshold,
                effective_max: tokensUpdate.max_context_tokens, worker_name: e.worker_name,
              });
              // Apply token updates to workerInfo BEFORE the continue
              // so the ctx: header updates live. The continue below only
              // prevents rendering context_updated as a message bubble.
              setWorkerInfo(prev => {
                const next = prev ? { ...prev, ...tokensUpdate } : { ...tokensUpdate };
                return next;
              });
            }
      // context_updated events now render as message bubbles (see the .filter and .map sections below)
      // The header update above still runs for live ctx: counter updates.
      // Always apply token updates to workerInfo (any event type may carry them)
      if (Object.keys(tokensUpdate).length > 0) {
        console.log('[PIPELINE:HOPS] WorkerOutputPanel: applying tokensUpdate to workerInfo', tokensUpdate)
        console.log('[TOKEN_PIPELINE] WorkerOutputPanel: calling setWorkerInfo with tokensUpdate', tokensUpdate)
        setWorkerInfo(prev => {
          const next = prev ? { ...prev, ...tokensUpdate } : { ...tokensUpdate };
          console.log('[TOKEN_PIPELINE] WorkerOutputPanel: setWorkerInfo result', { prev, next });
          return next;
        })
      }

      if (eventType === 'worker_spawned' && (status === 'ready' || status === 'busy')) {
        console.log('[PIPELINE:HOPS] WorkerOutputPanel: setWorkerInfo worker_spawned', { status })
        setWorkerInfo(prev => {
          const update = { runtime_status: status };
          if (!prev) return update;
          return { ...prev, ...update };
        })
      } else if (eventType === 'worker_status' && status) {
        console.log('[PIPELINE:HOPS] WorkerOutputPanel: setWorkerInfo worker_status', { status })
        setWorkerInfo(prev => {
          const update = { runtime_status: status, current_task: e.data?.current_task || prev?.current_task };
          if (!prev) return update;
          return { ...prev, ...update };
        })
      } else if (eventType === 'worker_completed') {
        console.log('[PIPELINE:HOPS] WorkerOutputPanel: setWorkerInfo worker_completed')
        setWorkerInfo(prev => {
          const update = { runtime_status: 'ready' };
          if (!prev) return update;
          return { ...prev, ...update };
        })
      } else if (eventType === 'worker_error') {
        console.log('[PIPELINE:HOPS] WorkerOutputPanel: setWorkerInfo worker_error', { error: e.data?.error })
        setWorkerInfo(prev => {
          const update = { runtime_status: 'error', error: e.data?.error || '' };
          if (!prev) return update;
          return { ...prev, ...update };
        })
      } else if (eventType === 'worker_paused') {
        console.log('[PIPELINE:HOPS] WorkerOutputPanel: setWorkerInfo worker_paused')
        setWorkerInfo(prev => {
          const update = { runtime_status: 'paused' };
          if (!prev) return update;
          return { ...prev, ...update };
        })
      } else if (eventType === 'worker_resumed') {
        console.log('[PIPELINE:HOPS] WorkerOutputPanel: setWorkerInfo worker_resumed')
        setWorkerInfo(prev => {
          const update = { runtime_status: 'ready' };
          if (!prev) return update;
          return { ...prev, ...update };
        })
      }
    }

    // ═══ DEDUP FILTERING — OUTSIDE setEvents, in the effect body ═══
    // First, filter by dedup key (using raw event type).
    const firstTimers = relevantEvents.filter(e => {
      const rawType = e.type?.replace('worker:', '') || ''

      const key = makeDedupKey(rawType, e.timestamp)
      console.warn('[DEDUP CHECK] rawType:', rawType, 'timestamp:', e.timestamp, 'key:', key, 'alreadySeen:', seenEventKeysRef.current.has(key));
      return !seenEventKeysRef.current.has(key)
    })

    // ── Transform filtered events to display format ──
    const newOnes = firstTimers.map(e => {
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
            // Preserve response_type so Respond tool output (with response_type='answer'/'question')
            // gets the correct 'final'/'question' styling via adaptWorkerEvent's is_final flag.
            response = {
              content: data.content || '',
              reasoning_content: data.reasoning_content || undefined,
              response_type: data.response_type || undefined,
            }
            return {
              event: 'worker_message',
              timestamp: e.timestamp,
              request: {},
              response,
            }
          }
          case 'token_warning': {
            const data = e.data || {}
            response = { type: 'token_warning', message: data.warning_message || data.message || '', token_count: data.token_count }
            return {
              event: 'system_notification',
              timestamp: e.timestamp,
              request: {},
              response,
            }
          }
          case 'turn_warning': {
            const data = e.data || {}
            response = { type: 'turn_warning', message: data.warning_message || data.message || '', turn_count: data.turn_count }
            return {
              event: 'system_notification',
              timestamp: e.timestamp,
              request: {},
              response,
            }
          }
          case 'time_warning': {
            const data = e.data || {}
            response = { type: 'time_warning', message: data.warning_message || data.message || '', elapsed_seconds: data.elapsed_seconds }
            return {
              event: 'system_notification',
              timestamp: e.timestamp,
              request: {},
              response,
            }
          }
          case 'token_recovery': {
            const data = e.data || {}
            response = { type: 'token_recovery', message: data.recovery_message || data.message || 'Token usage returned to safe levels' }
            return {
              event: 'token_recovery',
              timestamp: e.timestamp,
              request: {},
              response,
            }
          }
          case 'user_message': {
            // Preserve user query text
            const data = e.data || {}
            request = { query: e.request?.query || data.query || '' }
            response = { content: data.content || e.request?.query || '' }
            break
          }
          case 'tokens_updated':
            console.log('[TOKEN_PIPELINE] WorkerOutputPanel: tokens_updated mapped to display event', { input: e.input })
            return {
              event: 'tokens_updated',
              timestamp: e.timestamp,
              request: {},
              response: {},
              current_context_tokens: e.input ?? 0,
            }

          case 'context_summarized': {
            const data = e.data || {}
            console.log('[WorkerOutputPanel] context_summarized mapped to display event', { message: e.message })
            return {
              event: 'system_notification',
              timestamp: e.timestamp,
              request: {},
              response: {
                type: 'context_summarized',
                message: e.message || 'Context has been summarized. You now have a fresh context window and full access to tools.',
                context_length: data.context_length || data.token_count || null,
              },
            }
          }
          case 'context_updated':
            console.log('[TOKEN_PIPELINE] WorkerOutputPanel: context_updated mapped to display event', { context_length: e.context_length, worker_name: e.worker_name })
            return {
              event: 'context_updated',
              timestamp: e.timestamp,
              request: {},
              response: {},
              current_context_tokens: e.context_length ?? 0,
            }



          case 'system_notification': {
            const data = e.response || e.data || {}
            response = {
              type: data.type || 'system_notification',
              message: data.message || data.warning_message || '',
              token_count: data.token_count || 0,
              ...data
            }
            return {
              event: 'system_notification',
              session_id: sessionId || e.session_id || '',
              timestamp: e.timestamp || new Date().toISOString(),
              response,
            }
          }
          default:
            // For lifecycle events (worker_spawned, worker_status, etc.), keep existing behavior
            if (!e.request && !e.response) {
              request = {}
              response = { ...(e.data || {}), worker_name: e.worker_name || '' }
            }
            break
        }

        return {
          event: eventType,
          timestamp: e.timestamp,
          request,
          response,
          current_context_tokens: e.data?.current_context_tokens,
          max_context_tokens: e.data?.max_context_tokens,
        }
      })

    // ── Register dedup keys ONLY for events that render a visible bubble ──
    // Fix B: empty placeholder messages (no content/reasoning) must NOT consume
    // the dedup key — the same logical event typically arrives again with full
    // content (same canonical key + timestamp) and that later arrival must still
    // pass the dedup filter and render.
    for (let i = 0; i < firstTimers.length; i++) {
      if (isWorkerEventRenderable(newOnes[i])) {
        const rawType = firstTimers[i].type?.replace('worker:', '') || ''
        seenEventKeysRef.current.add(makeDedupKey(rawType, firstTimers[i].timestamp))
      }
    }

    // ═══ Bailout if nothing new ═══
    if (newOnes.length === 0) {
      console.warn('[DEDUP BAILOUT] All incoming events were deduplicated. incomingEvents:', incomingEvents, 'computed dedup keys:', relevantEvents.map(e => makeDedupKey(e.type?.replace('worker:', '') || '', e.timestamp)));
      return
    }

    // ═══ PURE state update — NO side effects ═══
    setEvents(prev => {
      const updated = [...prev, ...newOnes]
      eventsRef.current = updated
      return updated
    })
  }, [incomingEvents, workerName])

  // ── Events state ──────────────────────────────────────────────────────
  const [events, setEvents] = useState([]);
  const [hasNewEvents, setHasNewEvents] = useState(false);
  const scrollRef = useRef(null);
  const isAtBottomRef = useRef(true);
  const programmaticScrollRef = useRef(false);
  const prevCountRef = useRef(0);
  const eventsRef = useRef([]);

  // ── Smart scroll ──────────────────────────────────────────────────────
  // Track whether user is at bottom (suppressed during programmatic scroll)
  const handleScroll = useCallback(() => {
    if (programmaticScrollRef.current) {
      // This scroll event was triggered by programmatic scroll;
      // reset the guard and bail out.
      programmaticScrollRef.current = false;
      // Update isAtBottomRef so it reflects the user's actual scroll position
      // even when this scroll event was triggered programmatically.
      const el = scrollRef.current;
      if (el) {
        const threshold = 50;
        isAtBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < threshold;
        if (isAtBottomRef.current) setHasNewEvents(false);
      }
      return;
    }
    const el = scrollRef.current;
    if (!el) return;
    const threshold = 50;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < threshold;
    isAtBottomRef.current = atBottom;
    if (atBottom) setHasNewEvents(false);
  }, []);

  // Auto-scroll on new events if user was at bottom.
  // Use useLayoutEffect + double requestAnimationFrame to ensure DOM layout
  // (async syntax highlighting) is fully settled before measuring scrollHeight.
  // Only auto-scroll when event COUNT increases (append), not on array reference
  // changes (e.g., during session restore or restructuring).
  useLayoutEffect(() => {
    const currentCount = events.length
    const prevCount = prevCountRef.current
    prevCountRef.current = currentCount

    if (
      !isAtBottomRef.current ||
      !scrollRef.current ||
      currentCount <= prevCount ||
      currentCount === 0
    ) {
      return
    }

    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        if (scrollRef.current) {
          programmaticScrollRef.current = true;
          scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
        }
      });
    });
  }, [events]);

  // ResizeObserver — keep anchored at bottom during async expansion
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;

    const observer = new ResizeObserver(() => {
      if (isAtBottomRef.current && !programmaticScrollRef.current) {
        if (el.scrollHeight - el.scrollTop - el.clientHeight > 1) {
          programmaticScrollRef.current = true;
          el.scrollTop = el.scrollHeight;
        }
      }
    });

    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const scrollToBottom = () => {
    if (scrollRef.current) {
      programmaticScrollRef.current = true;
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
      const instanceQuery = instanceId != null ? `?instance_id=${instanceId}` : '';
      const res = await fetch(`/api/workspace/${workspaceId}/workers/${encodeURIComponent(workerName)}/stop${instanceQuery}`, {
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
  }, [workspaceId, workerName, instanceId]);

  const canStop = runtimeStatus === 'busy' || runtimeStatus === 'ready';

  // ── Pause / Resume handlers ──────────────────────────────────────────────
  const handlePause = useCallback(async () => {
    if (!workspaceId || !workerName) return;
    // Block control from non-owning sessions
    if (workerInfo && workerInfo.session_id && workerInfo.session_id !== sessionId) {
      setStopError('Cannot pause worker from another session');
      setTimeout(() => setStopError(''), 3000);
      return;
    }
    if (runtimeStatus === 'paused') return;
    setStopError('');
    try {
      const instanceQuery = instanceId != null ? `?instance_id=${instanceId}` : '';
      const res = await fetch(`/api/workspace/${workspaceId}/workers/${encodeURIComponent(workerName)}/pause${instanceQuery}`, {
        method: 'POST',
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body?.detail?.error || `HTTP ${res.status}`);
      }
      // Optimistic update — pause is in flight; flips to 'paused' when the
      // worker_paused WS event arrives (once the current turn completes).
      setWorkerInfo((prev) => prev ? { ...prev, runtime_status: 'pausing' } : prev);
    } catch (err) {
      setStopError(err.message);
      setTimeout(() => setStopError(''), 3000);
    }
  }, [workspaceId, workerName, instanceId, sessionId, runtimeStatus]);

  const handleResume = useCallback(async () => {
    if (!workspaceId || !workerName) return;
    // Block control from non-owning sessions
    if (workerInfo && workerInfo.session_id && workerInfo.session_id !== sessionId) {
      setStopError('Cannot resume worker from another session');
      setTimeout(() => setStopError(''), 3000);
      return;
    }
    setStopError('');
    try {
      const instanceQuery = instanceId != null ? `?instance_id=${instanceId}` : '';
      const res = await fetch(`/api/workspace/${workspaceId}/workers/${encodeURIComponent(workerName)}/resume${instanceQuery}`, {
        method: 'POST',
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body?.detail?.error || `HTTP ${res.status}`);
      }
      // Optimistic update
      setWorkerInfo((prev) => prev ? { ...prev, runtime_status: 'ready' } : prev);
    } catch (err) {
      setStopError(err.message);
      setTimeout(() => setStopError(''), 3000);
    }
  }, [workspaceId, workerName, instanceId, sessionId]);

  const canPause = runtimeStatus === 'busy' || runtimeStatus === 'ready';
  const canResume = runtimeStatus === 'paused';


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
          width: maximized ? '100%' : size,
          minWidth: maximized ? 0 : PANEL_MIN,
          maxWidth: maximized ? 'none' : PANEL_MAX,
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

  // (finalContentSet removed — was unused dead code)

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
          width: maximized ? '100%' : size,
          minWidth: maximized ? 0 : PANEL_MIN,
          maxWidth: maximized ? 'none' : PANEL_MAX,
          flexShrink: 0,
        }}
      >
        {/* ── Status bar (slim, matching main StatusBar) ────────────── */}
        <div className="worker-output-header">
          {/* Status dot — always visible. Reflects the owning session's running
              state (green + pulse when running, grey idle), matching the session
              tab's Running/Idle indicator (F5). No longer event-heuristic based,
              so it also renders correctly right after a browser refresh. */}
          <span
            className={'worker-status-dot' + (isRunning ? ' worker-status-dot-busy' : '')}
            style={{ background: isRunning ? '#4caf50' : '#9e9e9e' }}
            title={isRunning ? 'Worker session is running' : 'Worker session is idle'}
          />
          <span className="worker-status-label">
            {isRunning ? 'Running' : 'Idle'}
          </span>
          <span className="worker-output-header-label">
            Worker: {instanceLabel || workerName}
          </span>
          <span className="worker-output-header-ctx">
            ctx: {workerInfo && workerInfo.max_context_tokens > 0 ? `${formatTokens(workerInfo.current_context_tokens ?? 0)} / ${formatTokens(workerInfo.max_context_tokens)}` : '—'}
            {(() => { console.log('[TOKEN_PIPELINE] WorkerOutputPanel: RENDER header ctx', { workerInfo, max_gt_0: workerInfo?.max_context_tokens > 0, current: workerInfo?.current_context_tokens, max: workerInfo?.max_context_tokens }); return null; })()}
          </span>
          <div style={{ flex: 1 }} />
          {workerInfo?.current_task && (
            <span className="worker-output-header-task-inline" title={workerInfo.current_task}>
              {truncate(workerInfo.current_task, 50)}
            </span>
          )}
          {onToggleMaximize && (
            <button
              className="worker-output-maximize-btn worker-output-close-btn"
              onClick={onToggleMaximize}
              title={maximized ? 'Restore panel' : 'Maximize panel'}
            >
              {maximized ? '⤡' : '⤢'}
            </button>
          )}
          {onTogglePin && (
            <button
              className="worker-output-pin-btn worker-output-close-btn"
              onClick={onTogglePin}
              title={pinned ? 'Unpin panel' : 'Pin panel'}
            >
              {pinned ? '📌' : '📍'}
            </button>
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
          {events.length === 0 && (
            <div className="worker-output-empty">
              {/* Fix 2c: calm empty state with the required copy. A real
                  worker error (e.g. failed spawn) still overrides it. */}
              {workerError || 'Worker output appears here when the agent spawns a worker. History is not preserved across refreshes.'}
            </div>
          )}

          {runtimeStatus === 'paused' ? (
            <button
              className="worker-output-resume-btn"
              onClick={handleResume}
              disabled={!canResume}
            >
              ▶ Resume
            </button>
          ) : (
            <button
              className="worker-output-pause-btn"
              onClick={handlePause}
              disabled={!canPause}
            >
              ⏸ Pause
            </button>
          )}

          {events.map((evt, idx) => {
            const msg = adaptWorkerEvent(evt);

            if (!msg) {
              console.log('[WorkerOutputPanel] render: adaptWorkerEvent returned null for event', evt.event || 'unknown');
              return null;  // suppress events like user_message / query
            }

            // Skip empty assistant messages that have neither content nor reasoning_content.
            // The main ChatPanel handles this upstream — WorkerOutputPanel must filter
            // inline because raw events (especially WS-delivered) can arrive with empty
            // content before the full response is ready, producing invisible empty bubbles.
            if (!msg.content && !msg.reasoning_content && !msg.is_system_notification) {
              console.warn('[EMPTY EVENT FILTER] Event filtered due to empty content. evt:', evt, 'msg:', msg);
              return null;
            }

            // Use a robust unique key: _id (or fallback) + index + a session counter
            // This ensures no duplicate key warnings even if dedup misses.
            const key = (msg._id || evt.timestamp + '_' + evt.event) + '_' + idx;
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
          <span className="worker-output-bottom-name" title={instanceLabel || workerName}>
            {truncate(instanceLabel || workerName, 15)}
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

// ── Memoization ──────────────────────────────────────────────────────────
// App re-renders frequently (store updates, tab switches); the panel only
// needs to re-render when one of its real props actually changed. incomingEvents
// is compared by reference, then length, then element identity — the `|| []`
// fallback at the call site means two DIFFERENT empty arrays must count as
// equal, or the memo would never help while the panel has no events.
function workerOutputPropsEqual(prevProps, nextProps) {
  if (prevProps.workspaceId !== nextProps.workspaceId) return false;
  if (prevProps.workerName !== nextProps.workerName) return false;
  if (prevProps.instanceId !== nextProps.instanceId) return false;
  if (prevProps.instanceLabel !== nextProps.instanceLabel) return false;
  if (prevProps.sessionId !== nextProps.sessionId) return false;
  if (prevProps.onClose !== nextProps.onClose) return false;
  if (prevProps.size !== nextProps.size) return false;
  if (prevProps.maximized !== nextProps.maximized) return false;
  if (prevProps.pinned !== nextProps.pinned) return false;
  if (prevProps.onResize !== nextProps.onResize) return false;
  if (prevProps.onToggleMaximize !== nextProps.onToggleMaximize) return false;
  if (prevProps.onTogglePin !== nextProps.onTogglePin) return false;
  const prevEvents = prevProps.incomingEvents || [];
  const nextEvents = nextProps.incomingEvents || [];
  if (prevEvents === nextEvents) return true;
  if (prevEvents.length !== nextEvents.length) return false;
  for (let i = 0; i < prevEvents.length; i++) {
    if (prevEvents[i] !== nextEvents[i]) return false;
  }
  return true;
}
export default memo(WorkerOutputPanel, workerOutputPropsEqual);
