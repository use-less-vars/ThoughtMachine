/*
 * App.jsx
 *
 * Root component — hub WebSocket for sessions list + per-workspace tab strip.
 *
 * Architecture (workspace panel shell, Phase 4 + R7):
 *   ┌──────────────────────────────────────────────────────────────────────┐
 *   │  [WorkspacePanel | WorkspaceSelector]   route                        │
 *   │  [TabBar strip]  — per-workspace session tabs (frontend-only state)  │
 *   │  ┌─ SessionTab ─┐ (ONLY the active session tab is mounted; each      │
 *   │  │ (own WS)     │  keeps its own WebSocket; inactive tabs are strip  │
 *   │  └──────────────┘  entries only — no mount, no WS)                   │
 *   ├──────────────────────────────────────────────────────────────────────┤
 *   │           WorkerOutputPanel (sidebar)                                │
 *   └──────────────────────────────────────────────────────────────────────┘
 *
 * App maintains one "hub" WebSocket that only handles:
 *   - list_sessions        → sessions_list
 *   - get_open_sessions    → open_sessions (R7 restore/merge)
 *   - session_saved / session_deleted / session_renamed / session_closed
 *
 * Each SessionTab creates its OWN WebSocket for session interaction.
 *
 * Tab state lives in src/sessionTabsStore.js, keyed by workspace id and
 * persisted to localStorage (`tm.sessionTabs.<workspaceId>`). Closing a tab
 * is frontend-only — the session stays open server-side.
 *
 * Routing (see src/router.js):
 *   #/workspaces          → WorkspaceSelector (no strip)
 *   #/workspace/:id       → WorkspacePanel + strip + active session tab
 *   #/session/:sessionId  → tabbed session view (tab ensured + activated)
 */

import React, { useEffect, useRef, useCallback, useState } from 'react'
import useStore from './store/useStore'
import useWorkspaceStore from './store/workspaceStore'
import useSessionTabsStore from './sessionTabsStore'
import SessionTab from './components/SessionTab'
import TabBar from './components/TabBar'
import WorkerOutputPanel from './components/WorkerOutputPanel'
import { isWorkerEventRenderable } from './components/chat/adaptWorkerEvent'
import LoggingPanel from './components/LoggingPanel'
import WorkspaceSelector from './components/WorkspaceSelector'
import WorkspacePanel from './components/workspace/WorkspacePanel'
import { useRoute, useNavigate } from './router'
import './styles.css'

const WS_PORT = import.meta.env.VITE_BACKEND_PORT || '8000';
const WS_URL = `ws://${window.location.hostname}:${WS_PORT}/ws`
const TABS_PREFIX = 'tm.sessionTabs.'

export default function App() {
  const route = useRoute()
  const navigate = useNavigate()

  // ── Workspace context (sessionId → workspaceId mapping) ──────────────
  const sessionWorkspacesRef = useRef({})   // sessionId → workspaceId (learned from server)
  const lastKnownWorkspaceRef = useRef(null)
  const currentWsRef = useRef(null)
  const routeRef = useRef(route)
  routeRef.current = route

  // The workspace that owns the current tab strip. For workspace routes it is
  // the route id; for session routes it is whatever workspace we know the
  // session belongs to (learned from sessions_list / open_sessions /
  // session_loaded via onWorkspaceKnown); otherwise the last known workspace.
  const currentWs = route?.view === 'workspace'
    ? route.id
    : route?.view === 'session'
      ? (sessionWorkspacesRef.current[route.id] || lastKnownWorkspaceRef.current || null)
      : (lastKnownWorkspaceRef.current || null)
  currentWsRef.current = currentWs

  const wsRef = useRef(null)
  const [hubWs, setHubWs] = useState(null)
  const hubHasConnectedOnceRef = useRef(false)   // persist past StrictMode double-mount
  const [hubReady, setHubReady] = useState(false)
  const hydratedRef = useRef(false)              // boot-time restore ran once
  const bootWsRef = useRef(null)                 // workspace hydrated at boot
  const [workspaceKnownTick, setWorkspaceKnownTick] = useState(0)  // bump when a session→workspace mapping appears

  // ── Worker panel state per session ────────────────────────────────────
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

  const [showLoggingPanel, setShowLoggingPanel] = useState(false)
  const [loggingConfig, setLoggingConfig] = useState(null)
  const [loggingConfigError, setLoggingConfigError] = useState(null)
  const [workerEvents, setWorkerEvents] = useState({})  // { [sessionId]: [event, ...] } live WS worker events

  const pendingWorkerSelectionRef = useRef(null)  // { workerName, workspaceId } queued before activeSessionId is set
  const tabActionsRef = useRef({})                // tabId -> { sendCommand, getSessionId }

  // ── Per-workspace tab strip selectors (currentWs-scoped) ─────────────
  const storeEntry = useSessionTabsStore(s => (currentWs ? s.byWorkspace[currentWs] : null))
  const tabs = storeEntry?.tabs || []
  const activeSessionId = storeEntry?.activeSessionId || null
  const activeTab = tabs.find(t => t.sessionId === activeSessionId) || null
  const selectedWorker = activeSessionId ? (workerPanelState[activeSessionId] ?? null) : null

  const tabRunningStates = useStore(s => s.tabRunningStates)
  const routeSessionConfig = useStore(s => (route?.view === 'session' && route.id ? s.sessionConfigs[route.id] : undefined))
  const workspaceList = useWorkspaceStore(s => s.workspaceList)

  // Human-readable session name from the shared store (sessions_list data).
  const nameFromStore = (sessionId) => {
    if (!sessionId) return ''
    const s = useStore.getState().sessions.find(x => x.session_id === sessionId)
    return s?.name || ''
  }

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
      hubHasConnectedOnceRef.current = true
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

  // ── WebSocket lifecycle + clean close on page unload ─────────────────
  useEffect(() => {
    const ws = connectHub()

    const handleBeforeUnload = () => {
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current)
        reconnectTimeoutRef.current = null
      }
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
      wsRef.current = null
      hubHasConnectedOnceRef.current = false
      ws?.close()  // may be null if connectHub guard skipped duplicate
    }
  }, [connectHub])

  // ── Request open sessions when hub WS connects ───────────────────────
  useEffect(() => {
    if (hubWs && hubWs.readyState === WebSocket.OPEN) {
      hubWs.send(JSON.stringify({ command: 'get_open_sessions' }))
      console.log('[Hub WS] Sent get_open_sessions')
    }
  }, [hubWs])

  // ── Hub event router ─────────────────────────────────────────────────
  function handleHubEvent(msg) {
    const store = useStore.getState()
    switch (msg.type) {
      case 'sessions_list': {
        const list = msg.sessions ?? []
        store.setSessions(list)
        // sessions_list entries carry workspace_id (session registry) — use
        // them to learn which workspace owns each session.
        let changed = false
        list.forEach(s => {
          if (s.workspace_id && sessionWorkspacesRef.current[s.session_id] !== s.workspace_id) {
            sessionWorkspacesRef.current[s.session_id] = s.workspace_id
            lastKnownWorkspaceRef.current = s.workspace_id
            changed = true
          }
        })
        if (changed) setWorkspaceKnownTick(t => t + 1)
        break
      }
      case 'session_saved':
        // session_saved is never sent to the hub WS (only to tab WSes).
        // Tab WS triggers refresh via onSessionSaved callback → hubSend('list_sessions').
        break
      case 'session_deleted': {
        const deletedId = msg.session_id
        if (deletedId) {
          useStore.getState().removeSession(deletedId)
          const ws = sessionWorkspacesRef.current[deletedId] || currentWsRef.current
          if (ws) useSessionTabsStore.getState().closeTab(ws, deletedId)
          delete sessionWorkspacesRef.current[deletedId]
          // If the user is viewing the deleted session, drop back to the
          // workspace list (the tab is gone, so there is nothing left to show).
          if (route?.view === 'session' && route.id === deletedId) {
            navigate('/workspaces')
          }
        }
        wsRef.current?.send(JSON.stringify({ command: 'list_sessions' }))
        break
      }
      case 'session_renamed':
      case 'session_closed':
        // re-fetch the full session list to keep sidebar + mappings in sync
        wsRef.current?.send(JSON.stringify({ command: 'list_sessions' }))
        break
      case 'open_sessions':
        console.log('[Hub WS] open_sessions received:', msg.sessions)
        // R7 restore/merge: add server-open sessions that are not yet in the
        // persisted strip (LAZY — strip entries only; the active tab is the
        // persisted activeSessionId, restored by hydrate at boot).
        {
          const sessions = Array.isArray(msg.sessions) ? msg.sessions : []
          const openIds = new Set(sessions.map(s => s.session_id))
          let changed = false
          sessions.forEach(s => {
            // open_sessions carries no workspace_id — fall back to the mapping
            // learned from sessions_list / session_loaded / previous events.
            const knownWs = useStore.getState().sessions.find(x => x.session_id === s.session_id)?.workspace_id
            const ws = s.workspace_id || knownWs || sessionWorkspacesRef.current[s.session_id] || lastKnownWorkspaceRef.current || currentWsRef.current
            if (!ws) return
            if (s.workspace_id || knownWs) {
              if (sessionWorkspacesRef.current[s.session_id] !== ws) {
                sessionWorkspacesRef.current[s.session_id] = ws
                changed = true
              }
              lastKnownWorkspaceRef.current = ws
            }
            const st = useSessionTabsStore.getState()
            const entry = st.byWorkspace[ws]
            if (!entry?.tabs.some(t => t.sessionId === s.session_id)) {
              st.openTab(ws, { sessionId: s.session_id, title: s.name || '' })
            }
          })
          // Legacy migration: no persisted tm.sessionTabs.<ws> state, but a
          // legacy 'activeSessionId' key points at a session that is still
          // open server-side → restore it as the active tab (and persist).
          const ws = currentWsRef.current
          if (ws) {
            const st = useSessionTabsStore.getState()
            const entry = st.byWorkspace[ws]
            if (entry) {
              let persisted = null
              try { persisted = localStorage.getItem(TABS_PREFIX + ws) } catch { /* ignore */ }
              const legacy = (() => { try { return localStorage.getItem('activeSessionId') } catch { return null } })()
              if (!persisted && legacy && openIds.has(legacy) && entry.tabs.some(t => t.sessionId === legacy)) {
                st.setActiveTab(ws, legacy)
              }
              // st is a pre-mutation snapshot — re-fetch before reading post-mutation state.
              const fresh = useSessionTabsStore.getState().byWorkspace[ws]
              if (!fresh.activeSessionId && fresh.tabs.length > 0) {
                useSessionTabsStore.getState().setActiveTab(ws, fresh.tabs[0].sessionId)
              }
            }
          }
          if (changed) setWorkspaceKnownTick(t => t + 1)
        }
        break
      case 'session_loaded':
        // New session created via hub WS — open a tab with the real sessionId
        if (msg.session_id) {
          loadTab(msg.session_id, undefined, msg.workspace_id)
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
  const handleHubEventRef = useRef(handleHubEvent)
  handleHubEventRef.current = handleHubEvent

  // ── Hub sendCommand (only for sessions-list operations) ───────────────
  const hubSend = useCallback((command, payload = {}) => {
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) return
    ws.send(JSON.stringify({ command, ...payload }))
  }, [])

  // ── Handle session renamed (triggered by SessionTab via callback) ─────
  const handleSessionRenamed = useCallback((sessionId, newName) => {
    hubSend('list_sessions')
  }, [hubSend])

  // ── SessionTab reports the workspace it loaded into (session_loaded) ──
  const handleWorkspaceKnown = useCallback((sessionId, workspaceId) => {
    if (!sessionId || !workspaceId) return
    sessionWorkspacesRef.current[sessionId] = workspaceId
    lastKnownWorkspaceRef.current = workspaceId
    setWorkspaceKnownTick(t => t + 1)
  }, [])

  // ── Tab management (per-workspace strip) ──────────────────────────────
  // Open a tab for a session that exists server-side. With a workspaceIdHint
  // (session_loaded) the workspace is known; otherwise fall back to learned
  // mappings / last known workspace. Only a preferred session becomes active.
  const loadTab = useCallback((sessionId, preferredSessionId, workspaceIdHint) => {
    if (!sessionId) return
    const st = useSessionTabsStore.getState()
    const ws = workspaceIdHint || sessionWorkspacesRef.current[sessionId] || lastKnownWorkspaceRef.current || currentWsRef.current
    if (!ws) return
    if (workspaceIdHint) {
      sessionWorkspacesRef.current[sessionId] = workspaceIdHint
      lastKnownWorkspaceRef.current = workspaceIdHint
    }
    const entry = st.byWorkspace[ws]
    if (entry?.tabs.some(t => t.sessionId === sessionId)) {
      // Tab already exists — only activate when nothing is active yet.
      if ((!preferredSessionId || sessionId === preferredSessionId) && !st.byWorkspace[ws].activeSessionId) {
        st.setActiveTab(ws, sessionId)
        navigate(`/session/${encodeURIComponent(sessionId)}`)
      }
      return
    }
    st.openTab(ws, { sessionId, title: nameFromStore(sessionId) })
    if (!preferredSessionId || sessionId === preferredSessionId) {
      st.setActiveTab(ws, sessionId)
      navigate(`/session/${encodeURIComponent(sessionId)}`)
    }
  }, [navigate])

  // Close a tab — FRONTEND ONLY: the session stays open server-side.
  // If the active tab is closed, activate the neighbor (store picks it) and
  // follow it; with no tabs left, leave the session view (or stay on the
  // workspace view, which simply shows no strip).
  const handleCloseTab = useCallback((ws, sessionId) => {
    if (!ws || !sessionId) return
    const st = useSessionTabsStore.getState()
    const wasActive = st.byWorkspace[ws]?.activeSessionId === sessionId
    st.closeTab(ws, sessionId)
    if (wasActive) {
      // Re-fetch: st is a pre-mutation snapshot (zustand set() replaces the
      // state object), so reading st.byWorkspace here would return the OLD
      // active tab (the one just closed).
      const newActive = useSessionTabsStore.getState().byWorkspace[ws]?.activeSessionId
      if (newActive) {
        navigate(`/session/${encodeURIComponent(newActive)}`)
      } else if (routeRef.current?.view === 'session') {
        navigate('/workspaces')
      }
    }
  }, [navigate])

  const handleSelectWorker = useCallback((workerName, workspaceId) => {
    if (!activeSessionId) {
      // Queue the selection — activeSessionId may not be available yet
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

  // ── Handle worker lifecycle events from SessionTab WS ─────────────────
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
      // Only renderable stored events can block a duplicate — an earlier empty
      // placeholder must not consume the dedup key, otherwise the full-content
      // message for the same logical event is wrongly dropped. Non-renderable
      // incoming events are always stored (the panel needs them for the live
      // ctx counter and token updates), never blocked.
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

  const handleNewSessionCreated = useCallback((sessionId, sessionName) => {
    // SessionTab created a brand-new session (tab WS session_loaded).
    if (!sessionId) return
    const ws = currentWsRef.current
    if (!ws) return
    const st = useSessionTabsStore.getState()
    if (!st.byWorkspace[ws]?.tabs.some(t => t.sessionId === sessionId)) {
      st.openTab(ws, { sessionId, title: sessionName || nameFromStore(sessionId) })
    }
    st.setActiveTab(ws, sessionId)
    if (sessionName) {
      useStore.getState().updateSessionName(sessionId, sessionName)
    }
    // Refresh the sidebar so the new session appears in the list.
    hubSend('list_sessions')
    navigate(`/session/${encodeURIComponent(sessionId)}`)
  }, [hubSend, navigate])

  // ── Handle tab session adoption (intentional replacement) ─────────────
  // Called by SessionTab when a session_loaded flagged `replacement: true`
  // rebinds the ACTIVE tab to a NEW session id (workspace switch via
  // apply_config). Replace the old tab entry and follow the new session.
  const handleSessionAdopted = useCallback((oldSessionId, newSessionId) => {
    if (!newSessionId || newSessionId === oldSessionId) return
    const ws = sessionWorkspacesRef.current[newSessionId] || currentWsRef.current
    if (!ws) return
    const st = useSessionTabsStore.getState()
    if (oldSessionId) st.closeTab(ws, oldSessionId)
    if (!st.byWorkspace[ws]?.tabs.some(t => t.sessionId === newSessionId)) {
      st.openTab(ws, { sessionId: newSessionId, title: nameFromStore(newSessionId) })
    }
    st.setActiveTab(ws, newSessionId)
    hubSend('list_sessions')
    navigate(`/session/${encodeURIComponent(newSessionId)}`)
  }, [hubSend, navigate])

  const handleOpenNewTab = useCallback((sessionId, sessionName) => {
    // Called by SessionTab when a workspace switch creates a NEW session
    // while the active tab keeps the old session. Opens a fresh tab.
    if (!sessionId) return
    const ws = currentWsRef.current
    if (!ws) return
    const st = useSessionTabsStore.getState()
    if (!st.byWorkspace[ws]?.tabs.some(t => t.sessionId === sessionId)) {
      st.openTab(ws, { sessionId, title: sessionName || nameFromStore(sessionId) })
    }
    st.setActiveTab(ws, sessionId)
    // Initialize store slices for the new session (keyed by sessionId)
    useStore.getState().setSessionMode(sessionId, localStorage.getItem('lastSessionMode') || 'engineer')
    useStore.getState().setTabRunningState(sessionId, false)
    if (sessionName) {
      useStore.getState().updateSessionName(sessionId, sessionName)
    }
    hubSend('list_sessions')
    navigate(`/session/${encodeURIComponent(sessionId)}`)
  }, [hubSend, navigate])

  // ── Tab action registry (for save from SessionTab) ────────────────────
  const handleRegisterTab = useCallback((tabId, actions) => {
    tabActionsRef.current[tabId] = actions
    // load_session is sent by the tab's own onopen (deduped); do not send here.
  }, [])

  // ── R7 BOOT restore: hydrate the persisted strip for the boot workspace ──
  useEffect(() => {
    if (hydratedRef.current) return
    const initialRoute = routeRef.current
    let bootWs = null
    if (initialRoute?.view === 'workspace') {
      bootWs = initialRoute.id
    } else if (initialRoute?.view === 'session') {
      const target = initialRoute.id
      try {
        const keys = Object.keys(localStorage)
        const findWs = (sid) => {
          for (const key of keys) {
            if (!key.startsWith(TABS_PREFIX)) continue
            const parsed = JSON.parse(localStorage.getItem(key) || 'null')
            if (parsed?.tabs?.some(t => t.sessionId === sid)) return key.slice(TABS_PREFIX.length)
          }
          return null
        }
        bootWs = findWs(target)
        if (!bootWs) {
          // Legacy migration source: old global activeSessionId key
          const legacy = localStorage.getItem('activeSessionId')
          if (legacy === target) bootWs = findWs(legacy)
        }
      } catch {
        // malformed localStorage — fall through to open_sessions flow
      }
    }
    if (bootWs) {
      const entry = useSessionTabsStore.getState().hydrate(bootWs)
      bootWsRef.current = bootWs
      if (entry.activeSessionId && initialRoute?.view !== 'session') {
        // Re-persist (no-op when already hydrated); keeps legacy key in sync.
        useSessionTabsStore.getState().setActiveTab(bootWs, entry.activeSessionId)
      }
    }
    hydratedRef.current = true
  }, [])

  // ── Hydrate the strip whenever we land on a workspace route ───────────
  useEffect(() => {
    if (route?.view === 'workspace' && route.id) {
      useSessionTabsStore.getState().hydrate(route.id)
      lastKnownWorkspaceRef.current = route.id
    }
  }, [route])

  // ── Route-driven tab activation ───────────────────────────────────────
  // The session route (#/session/<id>) is the single entry point for opening
  // a session: if a tab for that session already exists, activate it;
  // otherwise create one (lazily — strip entry only until it becomes active).
  useEffect(() => {
    if (route?.view === 'session' && route.id) {
      // (b) Learn the workspace from the loaded session config when we don't
      // know it yet (deep link where the tab's own session_loaded hasn't come).
      const cfg = routeSessionConfig
      if (cfg?.config?.workspace_path && !sessionWorkspacesRef.current[route.id]) {
        const wsPath = cfg.config.workspace_path
        const match = workspaceList.find(w => w.root === wsPath || w.path === wsPath)
        if (match) {
          sessionWorkspacesRef.current[route.id] = match.id
          lastKnownWorkspaceRef.current = match.id
        }
      }
      // (a) Ensure the tab exists and is active in the owning workspace.
      const ws = currentWs || bootWsRef.current || null
      if (ws) {
        const st = useSessionTabsStore.getState()
        if (!st.byWorkspace[ws]) st.hydrate(ws)
        if (!st.byWorkspace[ws].tabs.some(t => t.sessionId === route.id)) {
          st.openTab(ws, { sessionId: route.id, title: nameFromStore(route.id) })
        }
        st.setActiveTab(ws, route.id)
      }
    } else if (route?.view === 'workspace') {
      lastKnownWorkspaceRef.current = route.id
    }
  }, [route, workspaceKnownTick, routeSessionConfig, currentWs])

  // ── Persist worker panel state to localStorage ────────────────────────
  useEffect(() => {
    localStorage.setItem('workerPanelState', JSON.stringify(workerPanelState))
  }, [workerPanelState])

  // ── Clean up stale keys when sessions are removed from tabs ───────────
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

  // ── Flush any pending worker selection once activeSessionId becomes available ──
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

  // ── Fetch logging config (callable for retry) ─────────────────────────
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

  // ── Fetch initial logging config on mount ─────────────────────────────
  useEffect(() => {
    fetchLoggingConfig()
  }, [fetchLoggingConfig])

  // ── Persist active session ID in localStorage (legacy key, still written;
  //    the new tm.sessionTabs.<ws> entry is the source of truth) ─────────
  useEffect(() => {
    if (activeSessionId) {
      localStorage.setItem('activeSessionId', activeSessionId)
    }
    // Don't clear on unmount/mount — the old key must survive page reload
    // so the open_sessions handler can read it before any tab is active.
  }, [activeSessionId])

  // ── Render ────────────────────────────────────────────────────────────
  return (
    <div className="app-container">
      <div className="app-main">
        <div className="app-center tab-content-area">
          {/* Per-workspace session tab strip (frontend-only state).
              Only shown on workspace/session views — never on the selector. */}
          {route?.view !== 'selector' && currentWs && tabs.length > 0 && (
            <TabBar
              tabs={tabs.map(t => ({ id: t.sessionId, name: t.title }))}
              activeTabId={activeSessionId}
              onSelectTab={(id) => {
                useSessionTabsStore.getState().setActiveTab(currentWs, id)
                navigate(`/session/${encodeURIComponent(id)}`)
              }}
              onCloseTab={(id) => handleCloseTab(currentWs, id)}
              onNewTab={() => navigate(`/workspace/${encodeURIComponent(currentWs)}`)}
              onLoggingClick={() => setShowLoggingPanel(true)}
              runningStates={tabRunningStates}
            />
          )}

          {/* ONLY the active session tab is mounted (own WebSocket, own load).
              Inactive tabs are strip entries only — no mount, no WS. */}
          {activeTab && (
            <div className="tab-wrapper" key={activeTab.sessionId}>
              <SessionTab
                sessionId={activeTab.sessionId}
                tabId={`tab-${activeTab.sessionId}`}
                hubReady={hubReady}
                isActive={true}
                staggerMs={0}
                loadOnConnect={true}
                onClose={() => handleCloseTab(currentWs, activeTab.sessionId)}
                onNewSession={handleNewSessionCreated}
                onSessionAdopted={(newId) => handleSessionAdopted(activeSessionId, newId)}
                onOpenNewTab={handleOpenNewTab}
                onSessionSaved={handleSessionSaved}
                onRegister={handleRegisterTab}
                onSessionRenamed={handleSessionRenamed}
                selectedWorker={selectedWorker}
                onSelectWorker={handleSelectWorker}
                activeSessionId={activeSessionId}
                onClearWorker={handleCloseWorkerPanel}
                onWorkerEvent={handleWorkerEvent}
                onLoggingConfigChanged={(config) => setLoggingConfig(config)}
                onWorkspaceKnown={handleWorkspaceKnown}
              />
            </div>
          )}

          {/* Route views */}
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
