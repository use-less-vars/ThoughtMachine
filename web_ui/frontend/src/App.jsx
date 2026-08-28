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

import React, { useEffect, useRef, useCallback, useState, useMemo } from 'react'
import useStore from './store/useStore'
import useWorkspaceStore from './store/workspaceStore'
import useSessionTabsStore from './sessionTabsStore'
import SessionTab from './components/SessionTab'
import TabBar from './components/TabBar'
import WorkerPanelArea from './components/WorkerPanelArea'
import { isWorkerEventRenderable } from './components/chat/adaptWorkerEvent'
import { routeEventsToPanels } from './components/chat/workerEventRouting'
import LoggingPanel from './components/LoggingPanel'
import OnboardingWizard from './components/OnboardingWizard'
import WorkspaceSelector from './components/WorkspaceSelector'
import WorkspacePanel from './components/workspace/WorkspacePanel'
import { useRoute, useNavigate } from './router'
import './styles.css'

const WS_PORT = import.meta.env.VITE_BACKEND_PORT || '8000';
const WS_URL = `ws://${window.location.hostname}:${WS_PORT}/ws`
const TABS_PREFIX = 'tm.sessionTabs.'
const WORKER_PANELS_PREFIX = 'tm.workerPanels.'
const PANEL_SIZE_MIN = 250
const PANEL_SIZE_MAX = 600
const PANEL_SIZE_DEFAULT = 350
// Instance key uniquely identifying a worker panel within a session
// (worker name + instance id; missing instance id treated as '0').
const workerPanelInstanceKey = (panel) => `${panel.worker_name}#${panel.instance_id ?? '0'}`
// Session ids that already have tabs in the tab store. Used at boot to
// decide which per-session worker-panel state to restore from localStorage.
function bootSessionIds() {
  try {
    const tabState = useSessionTabsStore.getState()
    const sessions = new Set()
    Object.values(tabState.byWorkspace || {}).forEach(we => {
      (we?.tabs || []).forEach(t => {
        if (t?.sessionId) sessions.add(t.sessionId)
      })
    })
    return sessions
  } catch {
    return new Set()
  }
}

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
  const tabsSeenOnceRef = useRef(new Set())      // session ids ever seen in tabs (guards the LS sweep)
  const panelsRestoredRef = useRef(false)        // worker-panel LS restore ran once
  const bootWsRef = useRef(null)                 // workspace hydrated at boot
  const [workspaceKnownTick, setWorkspaceKnownTick] = useState(0)  // bump when a session→workspace mapping appears

  // ── Worker panels state per session ───────────────────────────────────
  // workerPanelsBySession: { [sessionId]: [panel, ...] } — panel = { instance_id,
  //   instance_label, worker_name, workspaceId, size, maximized, pinned, order }
  // focusedPanelBySession: { [sessionId]: instanceKey | null } — focused panel
  //   per session (null → fall back to the most recently opened panel).
  // Persisted per-session in localStorage (tm.workerPanels.<sid> and .focused).
  const [workerPanelsBySession, setWorkerPanelsBySession] = useState(() => {
    try {
      const sessions = bootSessionIds()
      const result = {}
      sessions.forEach(sid => {
        const saved = localStorage.getItem(WORKER_PANELS_PREFIX + sid)
        if (!saved) return
        const parsed = JSON.parse(saved)
        if (!Array.isArray(parsed)) return
        const panels = parsed.filter(p => p && typeof p === 'object' && p.worker_name)
        if (panels.length > 0) result[sid] = panels
      })
      return result
    } catch {
      return {}
    }
  })
  const [focusedPanelBySession, setFocusedPanelBySession] = useState(() => {
    try {
      const sessions = bootSessionIds()
      const result = {}
      sessions.forEach(sid => {
        const saved = localStorage.getItem(WORKER_PANELS_PREFIX + sid + '.focused')
        if (saved != null) result[sid] = saved
      })
      return result
    } catch {
      return {}
    }
  })

  const [showLoggingPanel, setShowLoggingPanel] = useState(false)
  const [loggingConfig, setLoggingConfig] = useState(null)
  const [loggingConfigError, setLoggingConfigError] = useState(null)
  const [workerEvents, setWorkerEvents] = useState({})  // { [sessionId]: [event, ...] } live WS worker events
  const [dockerHealth, setDockerHealth] = useState(null)  // /api/health/containers payload (null while loading or backend down)
  const [backendDown, setBackendDown] = useState(false)   // health fetch failed or non-200 → backend unreachable
  const [backendBannerDismissed, setBackendBannerDismissed] = useState(false)
  const [onboardingDone, setOnboardingDone] = useState(null)  // null = unknown / fetch failed (wizard hidden); false → show wizard

  const pendingWorkerSelectionRef = useRef(null)  // { workerName, workspaceId, instanceId, instanceLabel } queued before activeSessionId is set
  const tabActionsRef = useRef({})                // tabId -> { sendCommand, getSessionId }
  const healthInFlightRef = useRef(false)         // guards overlapping health polls

  // ── Per-workspace tab strip selectors (currentWs-scoped) ─────────────
  const storeEntry = useSessionTabsStore(s => (currentWs ? s.byWorkspace[currentWs] : null))
  const tabs = storeEntry?.tabs || []
  const activeSessionId = storeEntry?.activeSessionId || null
  const activeTab = tabs.find(t => t.sessionId === activeSessionId) || null
  // Focused worker panel per session: explicit focus wins, else the most
  // recently opened (last) panel. selectedWorker exposes the consumer
  // contract: name, workspaceId, instance_id, instance_label.
  const focusedPanelInstanceKeyFor = (sid) => {
    const panels = workerPanelsBySession[sid] || []
    if (panels.length === 0) return null
    const focused = focusedPanelBySession[sid]
    if (focused != null && panels.some(p => workerPanelInstanceKey(p) === focused)) return focused
    return workerPanelInstanceKey(panels[panels.length - 1])
  }
  const focusedInstanceKey = activeSessionId ? focusedPanelInstanceKeyFor(activeSessionId) : null
  const selectedWorker = (activeSessionId && focusedInstanceKey)
    ? (() => {
        const panels = workerPanelsBySession[activeSessionId] || []
        const entry = panels.find(p => workerPanelInstanceKey(p) === focusedInstanceKey) || null
        return entry ? { ...entry, name: entry.worker_name, workspaceId: entry.workspaceId } : null
      })()
    : null
  // Worker events pre-routed to per-panel buckets ({ [instanceKey]: [event, ...] }).
  // Routing is pure — every event goes to EVERY panel it matches.
  const routedEvents = useMemo(
    () => routeEventsToPanels(workerEvents[activeSessionId] || [], workerPanelsBySession[activeSessionId] || []),
    [workerEvents, activeSessionId, workerPanelsBySession]
  )

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

  // ── Wizard sendCommand (used ONLY for save_provider) ───────────────────
  // Returns false when the hub WS is not open so the wizard can surface a
  // "backend not ready" message instead of silently dropping the provider.
  const wizardSend = useCallback((command, payload = {}) => {
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) return false
    ws.send(JSON.stringify({ command, ...payload }))
    return true
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

  const openPanel = useCallback((sessionId, { name, workspaceId, instance_id, instance_label }) => {
    if (!sessionId || !name) return
    const newKey = `${name}#${instance_id ?? '0'}`
    setWorkerPanelsBySession(prev => {
      const panels = prev[sessionId] || []
      if (panels.some(p => workerPanelInstanceKey(p) === newKey)) return prev
      return {
        ...prev,
        [sessionId]: [
          ...panels,
          {
            instance_id,
            instance_label,
            worker_name: name,
            workspaceId,
            size: PANEL_SIZE_DEFAULT,
            maximized: false,
            pinned: false,
            order: panels.length,
          },
        ],
      }
    })
    setFocusedPanelBySession(prev => ({ ...prev, [sessionId]: newKey }))
  }, [])

  const closePanel = useCallback((sessionId, instanceKey) => {
    if (!sessionId || !instanceKey) return
    const panels = workerPanelsBySession[sessionId] || []
    const idx = panels.findIndex(p => workerPanelInstanceKey(p) === instanceKey)
    if (idx === -1) return
    const nextPanels = panels.filter((_, i) => i !== idx).map((p, i) => ({ ...p, order: i }))
    setWorkerPanelsBySession(prev => ({ ...prev, [sessionId]: nextPanels }))
    setFocusedPanelBySession(prev => {
      if (prev[sessionId] !== instanceKey) return prev
      const nextKey = nextPanels.length > 0
        ? workerPanelInstanceKey(nextPanels[Math.min(idx, nextPanels.length - 1)])
        : null
      return { ...prev, [sessionId]: nextKey }
    })
  }, [workerPanelsBySession])

  const reorderPanel = useCallback((sessionId, instanceKey, direction) => {
    if (!sessionId || !instanceKey) return
    const panels = workerPanelsBySession[sessionId] || []
    const idx = panels.findIndex(p => workerPanelInstanceKey(p) === instanceKey)
    if (idx === -1) return
    const to = direction === 'left' ? idx - 1 : idx + 1
    if (to < 0 || to >= panels.length) return
    const next = panels.slice()
    const [moved] = next.splice(idx, 1)
    next.splice(to, 0, moved)
    setWorkerPanelsBySession(prev => ({
      ...prev,
      [sessionId]: next.map((p, i) => ({ ...p, order: i })),
    }))
  }, [workerPanelsBySession])

  const resizePanel = useCallback((sessionId, instanceKey, width) => {
    if (!sessionId || !instanceKey) return
    const clamped = Math.max(PANEL_SIZE_MIN, Math.min(PANEL_SIZE_MAX, width))
    setWorkerPanelsBySession(prev => {
      const panels = prev[sessionId] || []
      if (!panels.some(p => workerPanelInstanceKey(p) === instanceKey)) return prev
      return {
        ...prev,
        [sessionId]: panels.map(p => (workerPanelInstanceKey(p) === instanceKey ? { ...p, size: clamped } : p)),
      }
    })
  }, [])

  const toggleMaximize = useCallback((sessionId, instanceKey) => {
    if (!sessionId || !instanceKey) return
    setWorkerPanelsBySession(prev => {
      const panels = prev[sessionId] || []
      if (!panels.some(p => workerPanelInstanceKey(p) === instanceKey)) return prev
      return {
        ...prev,
        [sessionId]: panels.map(p => (workerPanelInstanceKey(p) === instanceKey ? { ...p, maximized: !p.maximized } : p)),
      }
    })
  }, [])

  const togglePin = useCallback((sessionId, instanceKey) => {
    if (!sessionId || !instanceKey) return
    setWorkerPanelsBySession(prev => {
      const panels = prev[sessionId] || []
      if (!panels.some(p => workerPanelInstanceKey(p) === instanceKey)) return prev
      return {
        ...prev,
        [sessionId]: panels.map(p => (workerPanelInstanceKey(p) === instanceKey ? { ...p, pinned: !p.pinned } : p)),
      }
    })
  }, [])

  const focusPanel = useCallback((sessionId, instanceKey) => {
    if (!sessionId || !instanceKey) return
    setFocusedPanelBySession(prev => ({ ...prev, [sessionId]: instanceKey }))
  }, [])

  const handleSelectWorker = useCallback((workerName, workspaceId, instanceId, instanceLabel) => {
    if (!activeSessionId) {
      // Queue the selection — activeSessionId may not be available yet
      pendingWorkerSelectionRef.current = { workerName, workspaceId, instanceId, instanceLabel }
      return
    }
    pendingWorkerSelectionRef.current = null
    openPanel(activeSessionId, { name: workerName, workspaceId, instance_id: instanceId, instance_label: instanceLabel })
  }, [activeSessionId, openPanel])

  const handleCloseWorkerPanel = useCallback(() => {
    if (!activeSessionId) return
    const panels = workerPanelsBySession[activeSessionId] || []
    if (panels.length === 0) return
    const focused = focusedPanelBySession[activeSessionId]
    const key = (focused != null && panels.some(p => workerPanelInstanceKey(p) === focused))
      ? focused
      : workerPanelInstanceKey(panels[panels.length - 1])
    closePanel(activeSessionId, key)
  }, [activeSessionId, workerPanelsBySession, focusedPanelBySession, closePanel])

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
      // Dedup key helper: canonical type + timestamp + instance identity (when present)
      const evtKey = (e) => {
        const eRawType = e.type?.replace('worker:', '') || ''
        const eCanonicalType = (
          eRawType === 'worker_message' ||
          eRawType === 'final_response' ||
          eRawType === 'assistant_message'
        ) ? 'final_response' : eRawType
        const instancePart =
          (e.instance_id != null ? `|i${e.instance_id}` : '') +
          (e.instance_label ? `|l${e.instance_label}` : '')
        return eCanonicalType + '|' + (e.timestamp || '') + instancePart
      }
      const key = evtKey(event)
      const incomingVisible = isWorkerEventRenderable(event)
      // Only renderable stored events can block a duplicate — an earlier empty
      // placeholder must not consume the dedup key, otherwise the full-content
      // message for the same logical event is wrongly dropped. Non-renderable
      // incoming events are always stored (the panel needs them for the live
      // ctx counter and token updates), never blocked.
      if (incomingVisible && events.some(e => {
        if (!isWorkerEventRenderable(e)) return false
        return evtKey(e) === key
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

  // ── Restore per-session worker panels from localStorage ───────────────
  // Runs once, AFTER the R7 boot hydration (the tab store is still empty when
  // the useState initializers above run, so boot-time restore cannot happen
  // there). Merges saved panels/focused keys for every session that already
  // has a tab, only ADDING sessions that are not yet in memory (no clobber).
  useEffect(() => {
    if (panelsRestoredRef.current) return
    panelsRestoredRef.current = true
    try {
      const tabState = useSessionTabsStore.getState()
      const sessionIds = new Set()
      Object.values(tabState.byWorkspace || {}).forEach(we => {
        (we?.tabs || []).forEach(t => {
          if (t?.sessionId) sessionIds.add(t.sessionId)
        })
      })
      sessionIds.forEach(sid => {
        const saved = localStorage.getItem(WORKER_PANELS_PREFIX + sid)
        if (saved) {
          try {
            const parsed = JSON.parse(saved)
            if (Array.isArray(parsed)) {
              const panels = parsed.filter(p => p && typeof p === 'object' && p.worker_name)
              if (panels.length > 0) {
                setWorkerPanelsBySession(prev => (prev[sid] ? prev : { ...prev, [sid]: panels }))
              }
            }
          } catch {
            // malformed entry — ignore
          }
        }
        const focused = localStorage.getItem(WORKER_PANELS_PREFIX + sid + '.focused')
        if (focused != null) {
          setFocusedPanelBySession(prev => (prev[sid] != null ? prev : { ...prev, [sid]: focused }))
        }
      })
    } catch {
      // localStorage unavailable — memory state still works this session
    }
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

  // ── Persist worker panels state to localStorage (per session) ───────────
  useEffect(() => {
    Object.entries(workerPanelsBySession).forEach(([sid, panels]) => {
      try {
        localStorage.setItem(WORKER_PANELS_PREFIX + sid, JSON.stringify(panels))
      } catch {
        // ignore quota / security errors — memory state still works this session
      }
    })
  }, [workerPanelsBySession])

  // Focused key per session: null → remove the focused key entirely.
  useEffect(() => {
    Object.entries(focusedPanelBySession).forEach(([sid, key]) => {
      try {
        if (key == null) {
          localStorage.removeItem(WORKER_PANELS_PREFIX + sid + '.focused')
        } else {
          localStorage.setItem(WORKER_PANELS_PREFIX + sid + '.focused', key)
        }
      } catch {
        // ignore
      }
    })
  }, [focusedPanelBySession])

  // One-time migration: drop the legacy global 'workerPanelState' key.
  useEffect(() => {
    try {
      localStorage.removeItem('workerPanelState')
    } catch {
      // ignore
    }
  }, [])

  // ── Clean up stale keys when sessions are removed from tabs ───────────
  useEffect(() => {
    const activeSessionIds = new Set(tabs.map(t => t.sessionId).filter(Boolean))
    // Remember every session id ever seen in tabs. The LS sweep below only
    // removes keys for sessions that were seen at least once and are gone
    // now, so a mount-time run with empty/stale tabs can never wipe other
    // (not yet hydrated) workspaces' panel state.
    activeSessionIds.forEach(sid => tabsSeenOnceRef.current.add(sid))
    setWorkerPanelsBySession(prev => {
      const stale = Object.keys(prev).filter(id => !activeSessionIds.has(id))
      if (stale.length === 0) return prev
      const next = { ...prev }
      stale.forEach(id => delete next[id])
      return next
    })
    setFocusedPanelBySession(prev => {
      const stale = Object.keys(prev).filter(id => !activeSessionIds.has(id))
      if (stale.length === 0) return prev
      const next = { ...prev }
      stale.forEach(id => delete next[id])
      return next
    })
    // Remove persisted per-session keys for sessions that no longer have tabs.
    // Skipped until at least one tab has been observed (mount runs with empty
    // tabs must not wipe freshly-restored localStorage).
    if (tabsSeenOnceRef.current.size > 0) {
      Object.keys(localStorage).forEach(key => {
        if (!key.startsWith(WORKER_PANELS_PREFIX)) return
        const sid = key.slice(WORKER_PANELS_PREFIX.length).replace(/\.focused$/, '')
        if (tabsSeenOnceRef.current.has(sid) && !activeSessionIds.has(sid)) {
          localStorage.removeItem(key)
        }
      })
    }
  }, [tabs])

  // ── Flush any pending worker selection once activeSessionId becomes available ──
  useEffect(() => {
    const pending = pendingWorkerSelectionRef.current
    if (pending && activeSessionId) {
      pendingWorkerSelectionRef.current = null
      openPanel(activeSessionId, {
        name: pending.workerName,
        workspaceId: pending.workspaceId,
        instance_id: pending.instanceId,
        instance_label: pending.instanceLabel,
      })
    }
  }, [activeSessionId, openPanel])

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

  // ── Poll backend health (drives the backend-down + degraded-Docker banners) ─
  // Two-step probe, run on mount then every 10s:
  //   Step 1  GET /api/health           — liveness only. A fetch failure or
  //             non-200 means the backend itself is down → the
  //             "Backend not running" banner.
  //   Step 2  GET /api/health/containers — only when Step 1 succeeded. A 200
  //             renders the structured docker payload (available/reason/hint);
  //             a failure here leaves Docker "unverified" (no banner, no raw
  //             error text) — the backend is up, so the backend-down banner
  //             would be wrong.
  // The ref guard skips a poll tick while the previous request is still in
  // flight, so polls never overlap.
  const checkBackendHealth = useCallback(async () => {
    if (healthInFlightRef.current) return
    healthInFlightRef.current = true
    const hostname = window.location.hostname
    const port = import.meta.env.VITE_BACKEND_PORT || '8000'
    try {
      // Step 1: backend liveness
      const healthRes = await fetch(`http://${hostname}:${port}/api/health`)
      if (!healthRes.ok) throw new Error(`HTTP ${healthRes.status}`)
      try {
        // Step 2: structured Docker availability (only when the backend is up)
        const res = await fetch(`http://${hostname}:${port}/api/health/containers`)
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        setDockerHealth(await res.json())
      } catch (err) {
        // Backend is up but the containers probe failed → Docker unverified.
        // Deliberately NOT backendDown: no banner, no raw error text.
        console.error('Failed to fetch Docker health:', err)
        setDockerHealth(null)
      }
      setBackendDown(false)
      // Recovery re-arms the "Backend not running" banner for a future outage.
      setBackendBannerDismissed(false)
    } catch (err) {
      console.error('Failed to fetch backend health:', err)
      setDockerHealth(null)
      setBackendDown(true)
    } finally {
      healthInFlightRef.current = false
    }
  }, [])

  useEffect(() => {
    checkBackendHealth()
    const interval = setInterval(checkBackendHealth, 10000)
    return () => clearInterval(interval)
  }, [checkBackendHealth])

  // ── First-run wizard: fetch onboarding status once on mount ────────────
  // null (fetch failed / non-OK) keeps the wizard hidden — the backend-down
  // banner already covers an unreachable backend.
  useEffect(() => {
    let cancelled = false
    const hostname = window.location.hostname
    const port = import.meta.env.VITE_BACKEND_PORT || '8000'
    fetch(`http://${hostname}:${port}/api/onboarding/status`)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json()
      })
      .then((data) => {
        if (!cancelled) setOnboardingDone(data.onboarding_complete === true)
      })
      .catch((err) => {
        console.error('Failed to fetch onboarding status:', err)
      })
    return () => {
      cancelled = true
    }
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

  // ── Docker availability (tri-state, mirrors workspaceStore) ──────────
  // null while loading; true/false from the health payload. Tolerates both
  // the legacy flat string ("reachable"/other) and the structured dispatch
  // shape ({"available": bool, "reason": ..., "hint": ...}).
  const dockerInfo = dockerHealth?.docker
  const dockerAvailable =
    dockerHealth === null
      ? null
      : typeof dockerInfo === 'string'
        ? dockerInfo === 'reachable'
        : dockerInfo
          ? dockerInfo.available === true
          : false
  const dockerHint =
    dockerInfo && typeof dockerInfo === 'object' ? dockerInfo.hint : null
  // Only the actionable hint is shown — never raw error text. Fall back to a
  // still-actionable generic line when the hint is missing/empty (the banner
  // already prefixes "⚠ Docker unavailable — ").
  const degradedText =
    dockerHint && typeof dockerHint === 'string' && dockerHint.trim()
      ? dockerHint
      : 'see the backend startup log for details'

  // ── Render ────────────────────────────────────────────────────────────
  return (
    <div className="app-container">
      {backendDown && !backendBannerDismissed && (
        <div className="docker-health-banner" role="alert">
          <span className="docker-health-banner-text">Backend not running — check logs/backend_startup.log</span>
          <button
            className="docker-health-banner-dismiss"
            onClick={() => setBackendBannerDismissed(true)}
            aria-label="Dismiss"
          >
            ✕
          </button>
        </div>
      )}
      {!backendDown && dockerAvailable === false && (
        <div className="docker-health-banner" role="alert">
          ⚠ Docker unavailable — {degradedText}
        </div>
      )}
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

        {/* Worker Panel Area — multi-panel sidebar for worker event logs */}
        <WorkerPanelArea
          sessionId={activeSessionId}
          workspaceId={currentWs}
          panels={workerPanelsBySession[activeSessionId] || []}
          focusedKey={focusedPanelBySession[activeSessionId] || null}
          eventsByKey={routedEvents}
          onClose={(key) => closePanel(activeSessionId, key)}
          onFocus={(key) => focusPanel(activeSessionId, key)}
          onResize={(key, w) => resizePanel(activeSessionId, key, w)}
          onToggleMaximize={(key) => toggleMaximize(activeSessionId, key)}
          onTogglePin={(key) => togglePin(activeSessionId, key)}
          onMoveLeft={(key) => reorderPanel(activeSessionId, key, 'left')}
          onMoveRight={(key) => reorderPanel(activeSessionId, key, 'right')}
        />

      </div>

      {/* First-run setup wizard — last child of .app-container */}
      {onboardingDone === false && (
        <OnboardingWizard
          onFinished={() => setOnboardingDone(true)}
          sendCommand={wizardSend}
        />
      )}
    </div>
  )
}
