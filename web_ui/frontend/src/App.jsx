/*
 * App.jsx
 *
 * Root component — hub WebSocket for sessions list + tab management.
 *
 * Architecture (multi-tab):
 *   ┌──────────────────────────────────────────────────┐
 *   │                   TabBar                          │
 *   ├──────────────────────────────────────────────────┤
 *   │  ┌─ Tab 1 ──┐  ┌─ Tab 2 ──┐  ┌─ Tab 3 ──┐      │
 *   │  │ SessionTab │  │ SessionTab │  │ SessionTab │  │
 *   │  │ (own WS)  │  │ (own WS)  │  │ (own WS)  │  │
 *   │  └───────────┘  └───────────┘  └───────────┘      │
 *   ├──────────────────────────────────────────────────┤
 *   │              SessionList (sidebar)                │
 *   ├──────────────────────────────────────────────────┤
 *   │           WorkerOutputPanel (sidebar)             │
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
 */

import React, { useEffect, useRef, useCallback, useState, useMemo } from 'react'
import useStore from './store/useStore'
import SessionTab from './components/SessionTab'
import SessionList from './components/SessionList'
import TabBar from './components/TabBar'
import SessionActionsPanel from './components/SessionActionsPanel'
import WorkerOutputPanel from './components/WorkerOutputPanel'
import LoggingPanel from './components/LoggingPanel'
import './styles.css'

const WS_PORT = import.meta.env.VITE_BACKEND_PORT || '8000';
const WS_URL = `ws://${window.location.hostname}:${WS_PORT}/ws`

let nextTabId = 1

export default function App() {
  const [tabs, setTabs] = useState([])           // { tabId, sessionId }
  const [activeTabId, setActiveTabId] = useState(null)
  const [showSessions, setShowSessions] = useState(false)
  const [tabRunningStates, setTabRunningStates] = useState({})
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

  const [sessionPanelOpen, setSessionPanelOpen] = useState(false)
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
      case 'session_deleted':
        wsRef.current?.send(JSON.stringify({ command: 'list_sessions' }))
        break
      case 'session_renamed':
        wsRef.current?.send(JSON.stringify({ command: 'list_sessions' }))
        break
      case 'open_sessions':
        console.log('[Hub WS] open_sessions received:', msg.sessions)
        // Restore previously active tab (by session ID) instead of defaulting
        // to the last-loaded tab.  Tab IDs are ephemeral (regenerated on every
        // page load), so we persist the session ID instead.
        const savedSessionId = localStorage.getItem('activeSessionId')
        if (msg.sessions && msg.sessions.length > 0) {
          msg.sessions.forEach(s => {
            loadTab(s.session_id, savedSessionId)
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
  const addTab = useCallback((sessionId = null) => {
    const tabId = `tab-${nextTabId++}`
    setTabs((prev) => [...prev, { tabId, sessionId }])
    setActiveTabId(tabId)
  }, [])

  // Open a tab for an existing session (auto-load from hub WS or sidebar)
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
      // Mark this tab so handleRegisterTab will trigger deferred load
      // once the SessionTab WS connects. Without this, new tabs created
      // via "+" would get stuck in deferred mode (empty placeholder).
      tabLoadTriggeredRef.current[tabId] = true
      console.log(`[DEBUG App.loadTab] Set active + triggered for new tab ${tabId}`)
    } else {
      console.log(`[DEBUG App.loadTab] NOT preferred — skipping setActive/trigger for new tab ${tabId}`)
    }
  }, [])

  // Initiate close: send close_session over the tab's own WS.
  // Do NOT remove from DOM yet — wait for server acknowledgement.
  const initiateCloseTab = useCallback((tabId) => {
    const tab = tabsRef.current.find(t => t.tabId === tabId)
    const actions = tabActionsRef.current[tabId]
    if (actions?.sendCommand) {
      actions.sendCommand('close_session', { session_id: tab?.sessionId })
    } else {
      // No WS connected — remove immediately
      removeTab(tabId)
    }
  }, [])

  // Actually remove the tab from DOM (called when server acknowledges close
  // via session_closed event, or on unexpected WS close).
  const removeTab = useCallback((tabId) => {
    // Allow re-adding this session's tab later (e.g., reopen via sidebar)
    const closingTab = tabsRef.current.find((t) => t.tabId === tabId)
    if (closingTab?.sessionId) {
      loadedSessionIdsRef.current.delete(closingTab.sessionId)
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
  }, [activeTabId])

  const handleNewTab = useCallback(() => {
    // Send new_session via hub WS — the hub WS handler will respond
    // with session_loaded, which creates the tab with a real sessionId.
    hubSend('new_session')
  }, [hubSend])

  // Open an existing session in a tab (called from SessionList sidebar)
  const handleOpenTab = useCallback((sessionId) => {
    loadTab(sessionId)
  }, [loadTab])

  // ── Session actions panel toggle (shown/hidden via ⚙️ cogwheel) ────────
  // When a tab is active, opens the slide-in SessionActionsPanel with
  // Save As… (to name the current session) and Delete Session.
  // When no tabs are open, toggles the SessionList sidebar instead.
  const handleCogwheelClick = useCallback(() => {
    if (tabs.length > 0 && activeTabId) {
      setSessionPanelOpen((prev) => !prev)
    } else {
      setShowSessions((prev) => !prev)
    }
  }, [tabs.length, activeTabId])

  const handleOpenSessionFromPanel = useCallback((sessionId) => {
    loadTab(sessionId)
    setSessionPanelOpen(false)
  }, [loadTab])

  const handleRunningChange = useCallback((tabId, status) => {
    setTabRunningStates((prev) => ({ ...prev, [tabId]: status }))
  }, [])

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
    if (!sessionId) return
    setWorkerEvents(prev => {
      const events = prev[sessionId] || []
      const key = event.type + (event.timestamp || '')
      if (events.some(e => (e.type + (e.timestamp || '')) === key)) return prev  // dedup
      // Cap at 500 events per session (trim oldest)
      const updated = [...events, event]
      if (updated.length > 500) {
        return { ...prev, [sessionId]: updated.slice(-500) }
      }
      return { ...prev, [sessionId]: updated }
    })
  }, [])

  const handleSessionSaved = useCallback((sessionId) => {
    // Refresh sessions list
    hubSend('list_sessions')
  }, [hubSend])

  const handleNewSessionCreated = useCallback((sessionId, sessionName) => {
    // Update the tab that created this session with its new sessionId
    setTabs((prev) =>
      prev.map((t) => (t.sessionId === null ? { ...t, sessionId } : t))
    )
    // If we got a session name, immediately add/update it in the sessions store
    // so the tab label shows a human-readable name instead of a UUID truncation.
    if (sessionName) {
      const store = useStore.getState()
      const existing = store.sessions.find((s) => s.session_id === sessionId)
      if (!existing) {
        store.setSessions([
          ...store.sessions,
          { session_id: sessionId, name: sessionName },
        ])
      }
    }
  }, [])

  const handleOpenNewTab = useCallback((sessionId, sessionName) => {
    // Called by SessionTab when a workspace switch creates a NEW session
    // while the existing tab keeps the old session. Opens a fresh tab.
    console.log('[App] handleOpenNewTab: opening new tab for', sessionId)
    loadTab(sessionId)
    // Refresh sessions list so the new session appears in the sidebar
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ command: 'list_sessions' }))
    }
  }, [loadTab])

  // ── Tab selection handler with deferred-load trigger ─────────────────
  const handleSelectTab = useCallback((tabId) => {
    console.log(`[DEBUG App.handleSelectTab] tabId=${tabId}, tabs=`, tabsRef.current.map(t => t.tabId),
      'loadTriggered=', tabLoadTriggeredRef.current[tabId],
      'sessionId=', tabsRef.current.find(t => t.tabId === tabId)?.sessionId,
      'actionsExist=', !!tabActionsRef.current[tabId])
    setActiveTabId(tabId)
    // If this tab was deferred (didn't load on WS connect), trigger its load now
    if (!tabLoadTriggeredRef.current[tabId]) {
      tabLoadTriggeredRef.current[tabId] = true
      const tab = tabsRef.current.find((t) => t.tabId === tabId)
      if (tab?.sessionId) {
        const actions = tabActionsRef.current[tabId]
        if (actions?.sendCommand) {
          actions.sendCommand('load_session', { session_id: tab.sessionId })
          deferredLoadSentRef.current[tabId] = true
          console.log(`[App] Triggered deferred load for tab ${tabId}, session ${tab.sessionId}`)
        } else {
          console.log(`[DEBUG App.handleSelectTab] actions MISSING for tab ${tabId} — deferred won't fire yet`)
        }
      } else {
        console.log(`[DEBUG App.handleSelectTab] tab ${tabId} has no sessionId — skipping deferred load`)
      }
    } else {
      console.log(`[DEBUG App.handleSelectTab] tab ${tabId} already triggered — no deferred action`)
    }
  }, [])

  // ── Tab action registry (for save from SessionList) ───────────────────
  const handleRegisterTab = useCallback((tabId, actions) => {
    console.log(`[DEBUG App.handleRegisterTab] tabId=${tabId}, triggered=${!!tabLoadTriggeredRef.current[tabId]}, sent=${!!deferredLoadSentRef.current[tabId]}, sessionId=`, tabsRef.current.find(t => t.tabId === tabId)?.sessionId)
    tabActionsRef.current[tabId] = actions
    // If this tab was selected before its actions were registered, trigger deferred load now
    if (tabLoadTriggeredRef.current[tabId] && !deferredLoadSentRef.current[tabId]) {
      const tab = tabsRef.current.find((t) => t.tabId === tabId)
      if (tab?.sessionId) {
        console.log(`[DEBUG App.handleRegisterTab] FIRE! sending load_session for tab ${tabId}`)
        actions.sendCommand('load_session', { session_id: tab.sessionId })
        deferredLoadSentRef.current[tabId] = true
        console.log(`[App] Triggered deferred load for tab ${tabId} (via delayed registration)`)
      } else {
        console.log(`[DEBUG App.handleRegisterTab] tab ${tabId} has no sessionId — cannot send load_session`)
      }
    }
  }, [])

  // ── Reliable rename via per-tab WS (Task 2) ──────────────────────────
  const handleRename = useCallback((sessionId, newName) => {
    // Find a tab that owns this session
    const tabEntry = tabs.find((t) => t.sessionId === sessionId)
    if (tabEntry) {
      const actions = tabActionsRef.current[tabEntry.tabId]
      if (actions?.sendCommand) {
        actions.sendCommand('rename_session', { session_id: sessionId, new_name: newName })
        return
      }
    }
    // Fallback: use hub WS if no tab is open for this session
    hubSend('rename_session', { session_id: sessionId, new_name: newName })
  }, [tabs, hubSend])

  // ── Reliable delete via per-tab WS (Task 3) ──────────────────────────
  const handleDelete = useCallback((sessionId) => {
    // Find a tab that owns this session
    const tabEntry = tabs.find((t) => t.sessionId === sessionId)
    if (tabEntry) {
      const actions = tabActionsRef.current[tabEntry.tabId]
      if (actions?.sendCommand) {
        // Close the tab first, then delete
        initiateCloseTab(tabEntry.tabId)
        actions.sendCommand('delete_session', { session_id: sessionId })
        // Refresh sessions list immediately so the sidebar updates
        hubSend('list_sessions')
        return
      }
    }
    // Fallback: use hub WS if no tab is open for this session
    hubSend('delete_session', { session_id: sessionId })
    hubSend('list_sessions')
  }, [tabs, hubSend, initiateCloseTab])

  // After delete from panel, close the panel
  const handleDeleteFromPanel = useCallback((sessionId) => {
    handleDelete(sessionId)
    setSessionPanelOpen(false)
  }, [handleDelete])

  // ── Derive tab names from sessions list ─────────────────────────────────
  const sessions = useStore((s) => s.sessions)
  const sessionMap = {}
  for (const s of sessions) {
    sessionMap[s.session_id] = s.name || 'Untitled'
  }

  const tabItems = tabs.map((t) => ({
    id: t.tabId,
    name: t.sessionId ? (sessionMap[t.sessionId] || t.sessionId.slice(0, 8)) : 'New Session',
  }))

  const activeSessionName = activeSessionId ? (sessionMap[activeSessionId] || 'Untitled') : 'New Session'

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
      <TabBar
        tabs={tabItems}
        activeTabId={activeTabId}
        onSelectTab={handleSelectTab}
        onCloseTab={initiateCloseTab}
        onNewTab={handleNewTab}
        runningStates={tabRunningStates}
        onCogwheelClick={handleCogwheelClick}
        onLoggingClick={() => setShowLoggingPanel(prev => !prev)}
      />

      <div className="app-main">
        {/* All tabs stay mounted; inactive ones hidden with display:none */}
        <div className="app-center tab-content-area">
          {tabs.length === 0 ? (
            <div className="empty-state">
              <p>Open a session or create a new one to get started.</p>
            </div>
          ) : (
            tabs.map((tab, index) => (
              <div
                key={tab.tabId}
                className="tab-wrapper"
                style={{ display: tab.tabId === activeTabId ? '' : 'none' }}
              >
                <SessionTab
                  sessionId={tab.sessionId}
                  tabId={tab.tabId}
                  hubReady={hubReady}
                  isActive={tab.tabId === activeTabId}
                  staggerMs={(index + 1) * 200}   // +1 so first tab doesn't connect at 0ms (page still loading)
                  loadOnConnect={tab.sessionId === startupActiveSessionId || tab.tabId === activeTabId}
                  onClose={() => removeTab(tab.tabId)}
                  onNewSession={handleNewSessionCreated}
                  onOpenNewTab={handleOpenNewTab}
                  onSessionSaved={handleSessionSaved}
                  onRegister={(actions) => handleRegisterTab(tab.tabId, actions)}
                  onRunningChange={handleRunningChange}
                  onSessionRenamed={handleSessionRenamed}
                  selectedWorker={selectedWorker}
                  onSelectWorker={handleSelectWorker}
                  activeSessionId={activeSessionId}
                  onClearWorker={handleCloseWorkerPanel}
                  onWorkerEvent={handleWorkerEvent}
                  onLoggingConfigChanged={(config) => setLoggingConfig(config)}
                />
              </div>
            ))
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

        {/* Sessions sidebar — always visible when no tabs are open, toggle via ⚙️ cogwheel */}
        <div className={`session-sidebar ${(showSessions || tabs.length === 0) ? 'open' : ''}`}>
          <SessionList
            sessions={sessions}
            onNew={handleNewTab}
            onOpenTab={handleOpenTab}
            onDelete={handleDelete}
            onRename={handleRename}
          />
        </div>

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

      {/* Session Actions Panel */}
      {sessionPanelOpen && activeTab && (
        <SessionActionsPanel
          sessionId={activeSessionId}
          sessionName={activeSessionName}
          onClose={() => setSessionPanelOpen(false)}
          onRename={(id, name) => {
            handleRename(id, name)
            // Also send save_session to persist name immediately
            const tabEntry = tabs.find((t) => t.sessionId === id)
            if (tabEntry) {
              const actions = tabActionsRef.current[tabEntry.tabId]
              actions?.sendCommand?.('save_session')
            }
            setSessionPanelOpen(false)
          }}
          onDelete={handleDeleteFromPanel}
          sessionsList={sessions}
          onOpenSession={handleOpenSessionFromPanel}
        />
      )}
    </div>
  )
}
