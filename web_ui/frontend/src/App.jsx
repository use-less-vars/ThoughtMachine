/*
 * App.jsx
 *
 * Root component — hub WebSocket for sessions list + tab management.
 *
 * Architecture (workspace panel shell, Phase 4):
 *   ┌──────────────────────────────────────────────────┐
 *   │  [WorkspacePanel | WorkspaceSelector]   route    │
 *   │  ┌─ SessionTab ─┐ (all tabs stay mounted; the    │
 *   │  │ (own WS)     │  route-active one is visible)  │
 *   │  └──────────────┘                                │
 *   ├──────────────────────────────────────────────────┤
 *   │           WorkerOutputPanel (sidebar)            │
 *   └──────────────────────────────────────────────────┘
 *
 * App maintains one "hub" WebSocket that only handles:
 *   - list_sessions → sessions_list
 *   - session_saved
 *   - session_deleted
 *   - session_renamed
 *
 * Each SessionTab creates its OWN WebSocket for session interaction.
 * App manages a tabs array: [ { tabId, sessionId }, ... ]
 *
 * Routing (see src/router.js):
 *   #/workspaces          → WorkspaceSelector
 *   #/workspace/:id       → WorkspacePanel
 *   #/session/:sessionId  → the matching SessionTab (created on demand)
 */

import React, { useEffect, useRef, useCallback, useState, useMemo } from 'react'
import useStore from './store/useStore'
import SessionTab from './components/SessionTab'
import WorkerOutputPanel from './components/WorkerOutputPanel'
import { isWorkerEventRenderable } from './components/chat/adaptWorkerEvent'
import LoggingPanel from './components/LoggingPanel'
import WorkspaceSelector from './components/WorkspaceSelector'
import WorkspacePanel from './components/workspace/WorkspacePanel'
import { useRoute, useNavigate } from './router'
import './styles.css'

const WS_PORT = import.meta.env.VITE_BACKEND_PORT || '8000';
const WS_URL = `ws://${window.location.hostname}:${WS_PORT}/ws`

let nextTabId = 1

export default function App() {
  const [tabs, setTabs] = useState([])           // { tabId, sessionId }
  const [activeTabId, setActiveTabId] = useState(null)
  const wsRef = useRef(null)
  const [hubWs, setHubWs] = useState(null)
  const hubHasConnectedOnceRef = useRef(false)   // persist past StrictMode double-mount
  const loadedSessionIdsRef = useRef(new Set())   // robust dedup: track sessions already converted to tabs
  const [hubReady, setHubReady] = useState(false)
  // ── Worker panel state per session ─────────────────────────────────
  // Map: sessionId -> { name, workspaceId } | null
  // Persisted in localStorage so panel state survives tab switches & page reloads.
  const [workerPanelState, setWorkerPanelState] = useState(() => {
    try {
      const saved = localStorage.getItem('workerPanelState')
      return saved ? JSON.parse(saved) : {}
    } catch {
      return {}
    }
  })

  // --- Route (dependency-free hash router — see src/router.js) ---
  const route = useRoute()
  const navigate = useNavigate()
  const [showLoggingPanel, setShowLoggingPanel] = useState(false)
  const [loggingConfig, setLoggingConfig] = useState(null)
  const [loggingConfigError, setLoggingConfigError] = useState(null)
  const [workerEvents, setWorkerEvents] = useState({})  // { [sessionId]: [event, ...] } live WS worker events

  const pendingWorkerSelectionRef = useRef(null)  // { workerName, workspaceId } queued before activeSessionId is set
  const tabActionsRef = useRef({})
  const tabLoadTriggeredRef = useRef({})   // track which deferred tabs have had load triggered
  const deferredLoadSentRef = useRef({})   // track whether load_session was actually sent via sendCommand
  const tabsRef = useRef(tabs)
  tabsRef.current = tabs

  // ── Derive active session from the selected tab (must be before any hooks that reference it) ──
  const activeTab = tabs.find((t) => t.tabId === activeTabId)
  const activeSessionId = activeTab?.sessionId
  const selectedWorker = activeSessionId ? (workerPanelState[activeSessionId] ?? null) : null

  // Snapshot which session ID was active at startup (from localStorage),
  // so we know which tabs should load on WS connect vs defer.
  const startupActiveSessionId = useMemo(() => {
    return localStorage.getItem('activeSessionId')
  }, [])   // tabId -> { sendCommand, getSessionId }

  // ── Hub WebSocket (sessions list only) with auto-reconnect ────────────
  const reconnectTimeoutRef = useRef(null)
  const reconnectAttemptsRef = useRef(0)
  const MAX_RECONNECT_ATTEMPTS = 5
  const connectHub = useCallback(() => {
    // Guard: prevent duplicate connections (StrictMode double-mount)
    if (hubHasConnectedOnceRef.current) {
      console.log('[Hub WS] Already connected once, skipping duplicate')
      return null
    }
    // Set immediately — before new WebSocket() — so StrictMode remount sees it
    hubHasConnectedOnceRef.current = true

    // Clear any pending reconnect
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current)
      reconnectTimeoutRef.current = null
    }

    const ws = new WebSocket(WS_URL)
    wsRef.current = ws

    ws.onopen = () => {
      console.log("[Hub WS] onopen")
      // Already set at connectHub entry; this is just a sanity double-set
      hubHasConnectedOnceRef.current = true
      // Reset reconnect counter on successful connection
      reconnectAttemptsRef.current = 0
      setHubWs(ws)
      ws.send(JSON.stringify({ command: 'list_sessions' }))
    }

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data)
        handleHubEventRef.current(msg)
        // Tabs may connect only after hub has received open_sessions
        if (msg.type === 'open_sessions') {
          console.log('[Hub WS] open_sessions processed, tabs may connect now')
          setHubReady(true)
        }
      } catch (err) {
        console.error('[Hub WS] Failed to parse message:', event.data, err)
      }
    }

    ws.onclose = (e) => {
      // Guard: only act if this WS is still the "current" one.
      // In StrictMode, WS1 (the cleanup WS) closes but WS2 (the remount WS)
      // is already connected.  WS1's onclose fires asynchronously, so we
      // must ignore it to prevent a false "reset this WS" event that would
      // trigger an unnecessary reconnect (WS3) → duplicate loadTab calls.
      if (wsRef.current !== ws) {
        console.log('[Hub WS] ignoring stale onclose (not the current WS)')
        return
      }
      setHubWs(null)
      setHubReady(false)
      // 1001 = normal close (component unmounting / page unload), keep flag, don't reconnect
      if (e.code !== 1001) {
        reconnectAttemptsRef.current += 1
        if (reconnectAttemptsRef.current > MAX_RECONNECT_ATTEMPTS) {
          console.warn(`[Hub WS] Reconnect limit (${MAX_RECONNECT_ATTEMPTS}) reached after ${reconnectAttemptsRef.current} attempts — giving up`)
          return
        }
        hubHasConnectedOnceRef.current = false  // allow reconnection on real errors
        // First retry is faster (0.5–1s), subsequent attempts use wider jitter (1–4s)
        const delay = reconnectAttemptsRef.current <= 1
          ? 500 + Math.random() * 500
          : 1000 + Math.random() * 3000
        console.log(`[Hub WS] disconnected (attempt ${reconnectAttemptsRef.current}/${MAX_RECONNECT_ATTEMPTS}), reconnecting in ${Math.round(delay)}ms...`)
        reconnectTimeoutRef.current = setTimeout(connectHub, delay)
      }
    }

    ws.onerror = () => {
      // onclose fires right after onerror, so we let onclose handle reconnection
    }

    return ws
  }, [])

  // ── WebSocket lifecycle + clean close on page unload ──────────────
  useEffect(() => {
    const ws = connectHub()

    const handleBeforeUnload = () => {
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current)
        reconnectTimeoutRef.current = null
      }
      // Close with code 1001 (going away) — the reconnect logic skips 1001,
      // so stale/old pages won't keep reconnecting on refresh/close.
      try {
        const current = wsRef.current
        if (current && (current.readyState === WebSocket.OPEN || current.readyState === WebSocket.CONNECTING)) {
          current.close(1001, 'page unload')
        }
      } catch {
        // ignore — WebSocket may already be closed
      }
    }

    window.addEventListener('beforeunload', handleBeforeUnload)

    return () => {
      window.removeEventListener('beforeunload', handleBeforeUnload)
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current)
      }
      // Clear ref + guard BEFORE calling close().  This ensures that if
      // WS1's onclose fires asynchronously (after the remount has already
      // happened under StrictMode), the identity guard in onclose will see
      // wsRef.current !== ws1 and block the stale event.  Resetting the
      // guard also lets the remount create a fresh WebSocket (WS2).
      wsRef.current = null
      hubHasConnectedOnceRef.current = false
      ws?.close()  // may be null if connectHub guard skipped duplicate
    }
  }, [connectHub])

  // ── Request open sessions when hub WS connects ──────────────────────────
  useEffect(() => {
    if (hubWs && hubWs.readyState === WebSocket.OPEN) {
      hubWs.send(JSON.stringify({ command: 'get_open_sessions' }))
      console.log('[Hub WS] Sent get_open_sessions')
    }
  }, [hubWs])

  // ── Hub event router ────────────────────────────────────────────────────
  function handleHubEvent(msg) {
    const store = useStore.getState()
    switch (msg.type) {
      case 'sessions_list':
        store.setSessions(msg.sessions ?? [])
        break
      case 'session_saved':
        // session_saved is never sent to the hub WS (only to tab WSes).
        // Tab WS triggers refresh via onSessionSaved callback → hubSend('list_sessions').
        break
      case 'session_deleted': {
        // Fix 4c: purge the deleted session from ALL store slices, close any
        // tab still showing it, then re-sync the sidebar session list.
        const deletedId = msg.session_id
        if (deletedId) {
          useStore.getState().removeSession(deletedId)
          const affectedTabs = tabsRef.current.filter((t) => t.sessionId === deletedId)
          affectedTabs.forEach((t) => removeTab(t.tabId))
          // If the user is viewing the deleted session, leave it — the route
          // no longer has a matching tab, so drop back to the workspace list.
          if (route?.view === 'session' && route.id === deletedId) {
            navigate('/workspaces')
          }
        }
        wsRef.current?.send(JSON.stringify({ command: 'list_sessions' }))
        break
      }
      case 'session_renamed':
        wsRef.current?.send(JSON.stringify({ command: 'list_sessions' }))
        break
      case 'session_closed':
        // re-fetch the full session list to keep sidebar in sync
        wsRef.current?.send(JSON.stringify({ command: 'list_sessions' }))
        break
      case 'open_sessions':
        console.log('[Hub WS] open_sessions received:', msg.sessions)
        // Restore previously active tab (by session ID) instead of defaulting
        // to the last-loaded tab.  Tab IDs are ephemeral (regenerated on every
        // page load), so we persist the session ID instead.
        const savedSessionId = localStorage.getItem('activeSessionId')
        if (msg.sessions && msg.sessions.length > 0) {
          // Activate the persisted session when it is still open; otherwise
          // fall back to the first open session (loadTab navigates for the
          // preferred one).
          const openIds = new Set(msg.sessions.map((s) => s.session_id))
          const preferred = (savedSessionId && openIds.has(savedSessionId))
            ? savedSessionId
            : msg.sessions[0].session_id
          msg.sessions.forEach(s => {
            loadTab(s.session_id, preferred)
          })
        }
        // hubReady is set to true in onmessage after handleHubEvent returns
        break
      case 'session_loaded':
        // New session created via hub WS — open a tab with the real sessionId
        if (msg.session_id) {
          console.log('[Hub WS] session_loaded, opening tab for', msg.session_id)
          loadTab(msg.session_id)
          // Refresh sessions list so the new session appears in the sidebar
          wsRef.current?.send(JSON.stringify({ command: 'list_sessions' }))
        }
        break

      default:
        // Other events (state_changed, conversation_changed, etc.)
        // are handled by individual SessionTab WebSockets.
        break
    }
  }

  // Ref to capture the latest handleHubEvent (avoids stale closure in ws.onmessage)
  // connectHub is useCallback([]) with zero deps, so ws.onmessage captures the
  // initial handleHubEvent.  This ref ensures ws.onmessage always calls the
  // current version, even after re-renders.
  const handleHubEventRef = useRef(handleHubEvent)
  handleHubEventRef.current = handleHubEvent

  // ── Hub sendCommand (only for sessions-list operations) ─────────────────
  const hubSend = useCallback((command, payload = {}) => {
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) return
    ws.send(JSON.stringify({ command, ...payload }))
  }, [])

  // ── Handle session renamed (triggered by SessionTab via callback) ───────
  const handleSessionRenamed = useCallback((sessionId, newName) => {
    hubSend('list_sessions')
  }, [hubSend])

  // ── Tab management ──────────────────────────────────────────────────────
  // Open a tab for an existing session (auto-load from hub WS or route).
  // preferredSessionId — if set, only make this tab active if its sessionId matches.
  const loadTab = useCallback((sessionId, preferredSessionId) => {
    console.log(`[DEBUG App.loadTab] sessionId=${sessionId}, preferredSessionId=${preferredSessionId}`)

    // ── Robust dedup via Set ref (beyond stale closure) ────────────────
    // The loadedSessionIdsRef Set is synchronous (not a React state), so
    // multiple loadTab calls within the same event loop tick can check it
    // immediately.  This prevents duplicate tabs from WS1's stale onclose
    // → reconnect → second get_open_sessions race condition.
    if (loadedSessionIdsRef.current.has(sessionId)) {
      console.log(`[DEBUG App.loadTab] SKIP (already loaded session ${sessionId}) — activating existing tab`)
      // Activate existing tab if preferred
      const existing = tabsRef.current.find((t) => t.sessionId === sessionId)
      if (existing && (!preferredSessionId || existing.sessionId === preferredSessionId)) {
        setActiveTabId(existing.tabId)
        tabLoadTriggeredRef.current[existing.tabId] = true
        navigate(`/session/${encodeURIComponent(sessionId)}`)
      }
      return
    }
    loadedSessionIdsRef.current.add(sessionId)

    const tabId = `tab-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
    console.log(`[DEBUG App.loadTab] CREATING new tab ${tabId} for session ${sessionId}, preferred=${preferredSessionId}`)

    // Add the tab to state (pure updater — no setActiveTabId inside)
    setTabs((prev) => {
      // Belt-and-suspenders: double-check inside the updater too
      if (prev.find((t) => t.sessionId === sessionId)) {
        console.log(`[DEBUG App.loadTab] Double-check: tab already in state for ${sessionId}`)
        return prev
      }
      return [...prev, { tabId, sessionId }]
    })

    // Set active tab OUTSIDE the updater — avoids React anti-pattern
    // of calling setActiveTabId from inside setTabs's updater function.
    if (!preferredSessionId || sessionId === preferredSessionId) {
      setActiveTabId(tabId)
      // Log the tab count after adding
      // Mark this tab so handleRegisterTab will trigger deferred load
      // once the SessionTab WS connects. Without this, new tabs created
      // via "+" would get stuck in deferred mode (empty placeholder).
      tabLoadTriggeredRef.current[tabId] = true
      navigate(`/session/${encodeURIComponent(sessionId)}`)
      console.log(`[DEBUG App.loadTab] Set active + triggered for new tab ${tabId}`)
    } else {
      console.log(`[DEBUG App.loadTab] NOT preferred — skipping setActive/trigger for new tab ${tabId}`)
    }
  }, [navigate])

  // Actually remove the tab from DOM (called when server acknowledges close
  // via session_closed event, or on unexpected WS close).
  const removeTab = useCallback((tabId) => {
    // Allow re-adding this session's tab later (e.g., reopen via sidebar)
    const closingTab = tabsRef.current.find((t) => t.tabId === tabId)
    if (closingTab?.sessionId) {
      loadedSessionIdsRef.current.delete(closingTab.sessionId)
      // Fix 4c: drop ALL store slices for this session on tab close, then
      // re-sync the sidebar session list.
      useStore.getState().removeSession(closingTab.sessionId)
      hubSend('list_sessions')
    }
    setTabs((prev) => {
      const idx = prev.findIndex((t) => t.tabId === tabId)
      if (idx === -1) return prev  // already removed
      const next = prev.filter((t) => t.tabId !== tabId)
      if (tabId === activeTabId) {
        const newIdx = Math.min(idx, next.length - 1)
        setActiveTabId(next.length > 0 ? next[newIdx >= 0 ? newIdx : 0].tabId : null)
      }
      return next
    })
    delete tabActionsRef.current[tabId]
  }, [activeTabId, hubSend])

  const handleSelectWorker = useCallback((workerName, workspaceId) => {
    if (!activeSessionId) {
      // Queue the selection — activeSessionId may not be available yet
      // (e.g., worker spawned before session_loaded WS message arrives).
      // A useEffect below flushes this when activeSessionId becomes set.
      pendingWorkerSelectionRef.current = { workerName, workspaceId }
      return
    }
    pendingWorkerSelectionRef.current = null
    setWorkerPanelState(prev => ({
      ...prev,
      [activeSessionId]: { name: workerName, workspaceId }
    }))
  }, [activeSessionId])

  const handleCloseWorkerPanel = useCallback(() => {
    if (!activeSessionId) return
    setWorkerPanelState(prev => ({
      ...prev,
      [activeSessionId]: null
    }))
  }, [activeSessionId])

  // ── Handle worker lifecycle events from SessionTab WS ────────────────
  const handleWorkerEvent = useCallback((sessionId, event) => {
    console.log('[PIPELINE:HOPS] App.handleWorkerEvent: called', { type: event.type, sessionId, context_length: event.context_length, source: event.source })
    console.log('[TOKEN_PIPELINE] App.handleWorkerEvent: called', { type: event.type, sessionId, context_length: event.context_length, source: event.source })
    if (!sessionId) {
      console.warn('[TOKEN_PIPELINE] App.handleWorkerEvent: DROPPED — sessionId is falsy')
      return
    }
    setWorkerEvents(prev => {
      const events = prev[sessionId] || []
      // Use canonical dedup key: normalize worker_message/assistant_message/final_response
      // variants to 'final_response' so the same logical event arriving with different
      // WS type values (e.g. worker:assistant_message vs worker:worker_message) is caught.
      const rawType = event.type?.replace('worker:', '') || ''
      const canonicalType = (
        rawType === 'worker_message' ||
        rawType === 'final_response' ||
        rawType === 'assistant_message'
      ) ? 'final_response' : rawType
      const key = canonicalType + '|' + (event.timestamp || '')
      const incomingVisible = isWorkerEventRenderable(event)
      // Fix B: only renderable stored events can block a duplicate — an earlier
      // empty placeholder must not consume the dedup key, otherwise the full-content
      // message for the same logical event is wrongly dropped. Non-renderable
      // incoming events are always stored (the panel needs them for the live ctx:
      // counter and token updates), never blocked.
      if (incomingVisible && events.some(e => {
        if (!isWorkerEventRenderable(e)) return false
        const eRawType = e.type?.replace('worker:', '') || ''
        const eCanonicalType = (
          eRawType === 'worker_message' ||
          eRawType === 'final_response' ||
          eRawType === 'assistant_message'
        ) ? 'final_response' : eRawType
        return (eCanonicalType + '|' + (e.timestamp || '')) === key
      })) {
        console.log('[TOKEN_PIPELINE] App.handleWorkerEvent: DEDUPED event', { type: event.type, key })
        return prev  // dedup
      }
      // Cap at 500 events per session (trim oldest)
      const updated = [...events, event]
      if (updated.length > 500) {
        return { ...prev, [sessionId]: updated.slice(-500) }
      }
      console.log('[PIPELINE:HOPS] App.handleWorkerEvent: stored event', { type: event.type, sessionId, count: updated.length })
      console.log('[TOKEN_PIPELINE] App.handleWorkerEvent: stored event', { type: event.type, sessionId, count: updated.length })
      return { ...prev, [sessionId]: updated }
    })
  }, [])

  const handleSessionSaved = useCallback((sessionId) => {
    // Refresh sessions list
    hubSend('list_sessions')
  }, [hubSend])

  const handleNewSessionCreated = useCallback((tabId, sessionId, sessionName) => {
    // Update the tab that created this session with its new sessionId.
    // Rebinds BY tabId so this covers both fresh tabs (entry has sessionId
    // null) and load-error recovery (entry still points at a dead session
    // id). The tab keeps its identity; only the session id it points at
    // changes.
    const oldId = tabsRef.current.find((t) => t.tabId === tabId)?.sessionId
    setTabs((prev) =>
      prev.map((t) => (t.tabId === tabId ? { ...t, sessionId } : t))
    )
    // Keep the dedup set in sync so a later open_sessions replay activates
    // this tab instead of duplicating it.
    loadedSessionIdsRef.current.add(sessionId)
    if (oldId && localStorage.getItem('activeSessionId') === oldId) {
      localStorage.setItem('activeSessionId', sessionId)
    }
    // Record the session name immediately in the shared store authority
    // (upsert) so the tab label shows a human-readable name right away.
    if (sessionName) {
      useStore.getState().updateSessionName(sessionId, sessionName)
    }
    // Refresh the sidebar so the new session appears in the list.
    hubSend('list_sessions')
    // Follow the new session in the router — the session route shows the
    // freshly rebound tab (route effect activates it by tab.sessionId).
    navigate(`/session/${encodeURIComponent(sessionId)}`)
  }, [hubSend, navigate])

  // ── Handle tab session adoption (intentional replacement) ────────────────
  // Called by SessionTab when a session_loaded flagged `replacement: true`
  // rebinds an existing tab to a NEW session id (workspace switch via
  // apply_config). The tab keeps its identity; only the session id it points
  // at changes. Update the tabs entry, keep the dedup set in sync (so a later
  // open_sessions replay activates this tab instead of duplicating it), and
  // persist the active session across reloads.
  const handleSessionAdopted = useCallback((tabId, newSessionId) => {
    const oldId = tabsRef.current.find((t) => t.tabId === tabId)?.sessionId
    setTabs((prev) =>
      prev.map((t) => (t.tabId === tabId ? { ...t, sessionId: newSessionId } : t))
    )
    loadedSessionIdsRef.current.add(newSessionId)
    if (oldId && localStorage.getItem('activeSessionId') === oldId) {
      localStorage.setItem('activeSessionId', newSessionId)
    }
    // Refresh the sidebar so the replacement session appears in the list.
    hubSend('list_sessions')
    // Follow the adopted session in the router.
    navigate(`/session/${encodeURIComponent(newSessionId)}`)
  }, [hubSend, navigate])

  const handleOpenNewTab = useCallback((sessionId, sessionName) => {
    // Called by SessionTab when a workspace switch creates a NEW session
    // while the existing tab keeps the old session. Opens a fresh tab.
    console.log('[App] handleOpenNewTab: opening new tab for', sessionId, sessionName)
    loadTab(sessionId)
    // Initialize store slices for the new session (keyed by sessionId)
    useStore.getState().setSessionMode(sessionId, localStorage.getItem('lastSessionMode') || 'engineer')
    useStore.getState().setTabRunningState(sessionId, false)
    // Record the session name immediately in the shared store authority
    // (upsert) so the new tab shows a human-readable label right away.
    if (sessionName) {
      useStore.getState().updateSessionName(sessionId, sessionName)
    }
    // Refresh sessions list so the new session appears in the sidebar
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ command: 'list_sessions' }))
    }
  }, [loadTab])

  // ── Tab action registry (for save from SessionTab) ─────────────────────
  const handleRegisterTab = useCallback((tabId, actions) => {
    console.log(`[DEBUG App.handleRegisterTab] tabId=${tabId}, triggered=${!!tabLoadTriggeredRef.current[tabId]}, sent=${!!deferredLoadSentRef.current[tabId]}, sessionId=`, tabsRef.current.find(t => t.tabId === tabId)?.sessionId)
    tabActionsRef.current[tabId] = actions
    // load_session is sent by the tab's own onopen (deduped); do not send here.
    // (Fix 4b) This previously re-sent load_session for tabs selected before
    // their WS registered — racing the tab's onopen send and producing
    // duplicate load_session commands. The tab owns the single load path.
  }, [])

  // ── Persist worker panel state to localStorage ──────────────────────────
  useEffect(() => {
    localStorage.setItem('workerPanelState', JSON.stringify(workerPanelState))
  }, [workerPanelState])

  // ── Clean up stale keys when sessions are removed from tabs ─────────────
  useEffect(() => {
    const activeSessionIds = new Set(tabs.map(t => t.sessionId).filter(Boolean))
    setWorkerPanelState(prev => {
      const stale = Object.keys(prev).filter(id => !activeSessionIds.has(id))
      if (stale.length === 0) return prev
      const next = { ...prev }
      stale.forEach(id => delete next[id])
      return next
    })
  }, [tabs])

  // ── Flush any pending worker selection once activeSessionId becomes available ─
  useEffect(() => {
    const pending = pendingWorkerSelectionRef.current
    if (pending && activeSessionId) {
      pendingWorkerSelectionRef.current = null
      setWorkerPanelState(prev => ({
        ...prev,
        [activeSessionId]: { name: pending.workerName, workspaceId: pending.workspaceId }
      }))
    }
  }, [activeSessionId])

  // ── Route-driven session loading ────────────────────────────────────────
  // The session route (#/session/<id>) is the single entry point for opening
  // a session: if a tab for that session already exists, activate it;
  // otherwise create one (loadTab navigates + activates for the new tab).
  useEffect(() => {
    if (route?.view !== 'session' || !route.id) return
    const existing = tabsRef.current.find((t) => t.sessionId === route.id)
    if (existing) {
      setActiveTabId(existing.tabId)
      tabLoadTriggeredRef.current[existing.tabId] = true
      return
    }
    if (!loadedSessionIdsRef.current.has(route.id)) {
      loadTab(route.id)
    }
  }, [route, loadTab])

  // ── Fetch logging config (callable for retry) ────────────────────────────────────
  const fetchLoggingConfig = useCallback(() => {
    const hostname = window.location.hostname
    const port = import.meta.env.VITE_BACKEND_PORT || '8000'
    setLoggingConfigError(null)
    fetch(`http://${hostname}:${port}/api/logging/config`)
      .then(res => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json()
      })
      .then(data => setLoggingConfig(data))
      .catch(err => {
        console.error('Failed to fetch logging config:', err)
        setLoggingConfigError(err.message || 'Failed to load')
      })
  }, [])

  // ── Fetch initial logging config on mount ────────────────────────────────────────
  useEffect(() => {
    fetchLoggingConfig()
  }, [fetchLoggingConfig])

  // ── Persist active session ID in localStorage (stable across page loads) ──
  useEffect(() => {
    if (activeSessionId) {
      localStorage.setItem('activeSessionId', activeSessionId)
    }
    // Don't clear on unmount/mount — the old key must survive page reload
    // so the open_sessions handler can read it before any tab is active.
  }, [activeSessionId])

  // ── Render ──────────────────────────────────────────────────────────────
  return (
    <div className="app-container">
      <div className="app-main">
        {/* All tabs stay mounted (hidden via display:none) so each keeps its
            per-session WebSocket alive across route changes. The visible tab
            is the one matching the session route. */}
        <div className="app-center tab-content-area">
          {tabs.map((tab, index) => {
            const visible = route?.view === 'session' && route.id === tab.sessionId
            return (
              <div
                key={tab.tabId}
                className="tab-wrapper"
                style={{ display: visible ? '' : 'none' }}
              >
                <SessionTab
                  sessionId={tab.sessionId}
                  tabId={tab.tabId}
                  hubReady={hubReady}
                  isActive={tab.tabId === activeTabId}
                  staggerMs={(index + 1) * 200}   // +1 so first tab doesn't connect at 0ms (page still loading)
                  loadOnConnect={tab.sessionId === startupActiveSessionId || tab.sessionId === route?.id}
                  onClose={() => removeTab(tab.tabId)}
                  onNewSession={(sid, name) => handleNewSessionCreated(tab.tabId, sid, name)}
                  onSessionAdopted={(newId) => handleSessionAdopted(tab.tabId, newId)}
                  onOpenNewTab={handleOpenNewTab}
                  onSessionSaved={handleSessionSaved}
                  onRegister={(actions) => handleRegisterTab(tab.tabId, actions)}
                  onSessionRenamed={handleSessionRenamed}
                  selectedWorker={selectedWorker}
                  onSelectWorker={handleSelectWorker}
                  activeSessionId={activeSessionId}
                  onClearWorker={handleCloseWorkerPanel}
                  onWorkerEvent={handleWorkerEvent}
                  onLoggingConfigChanged={(config) => setLoggingConfig(config)}
                />
              </div>
            )
          })}

          {/* Route views — rendered above the (hidden) tabs */}
          {route?.view === 'workspace' && <WorkspacePanel />}
          {route?.view === 'selector' && <WorkspaceSelector />}
          {route?.view === 'session' && !tabs.some((t) => t.sessionId === route.id) && (
            <div className="session-loading-placeholder">Loading session…</div>
          )}
        </div>

        {/* Logging Panel — toggle via button above */}
        {showLoggingPanel && (
          <LoggingPanel
            config={loggingConfig}
            configError={loggingConfigError}
            onRetry={fetchLoggingConfig}
            onSaveConfig={async (configPayload) => {
              const hostname = window.location.hostname
              const port = import.meta.env.VITE_BACKEND_PORT || '8000'
              const res = await fetch(`http://${hostname}:${port}/api/logging/config`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(configPayload),
              })
              if (!res.ok) throw new Error(`HTTP ${res.status}`)
              const data = await res.json()
              setLoggingConfig(data.config || data)
              return data.config || data
            }}
            onClose={() => setShowLoggingPanel(false)}
          />
        )}

        {/* Worker Output Panel — right sidebar for worker event logs */}
        <div className="worker-output-panel">
          {selectedWorker && (
            <WorkerOutputPanel
              workspaceId={selectedWorker.workspaceId}
              workerName={selectedWorker.name}
              sessionId={activeSessionId}
              onClose={handleCloseWorkerPanel}
              incomingEvents={workerEvents[activeSessionId] || []}
            />
          )}
        </div>

      </div>
    </div>
  )
}
