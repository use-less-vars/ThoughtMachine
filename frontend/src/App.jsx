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

import React, { useEffect, useRef, useCallback, useState } from 'react'
import useStore from './store/useStore'
import SessionTab from './components/SessionTab'
import SessionList from './components/SessionList'
import TabBar from './components/TabBar'
import './styles.css'

const WS_URL = `ws://${window.location.hostname}:8000/ws`

let nextTabId = 1

export default function App() {
  const [tabs, setTabs] = useState([])           // { tabId, sessionId }
  const [activeTabId, setActiveTabId] = useState(null)
  const [showSessions, setShowSessions] = useState(false)
  const [tabRunningStates, setTabRunningStates] = useState({})
  const wsRef = useRef(null)
  const [hubWs, setHubWs] = useState(null)
  const tabActionsRef = useRef({})   // tabId -> { sendCommand }

  // ── Hub WebSocket (sessions list only) ─────────────────────────────────
  useEffect(() => {
    const ws = new WebSocket(WS_URL)
    wsRef.current = ws

    ws.onopen = () => {
      console.log("✅ [Hub WS] onopen")
      setHubWs(ws)
      ws.send(JSON.stringify({ command: 'list_sessions' }))
    }

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data)
        handleHubEvent(msg)
      } catch (err) {
        console.error('[Hub WS] Failed to parse message:', event.data, err)
      }
    }

    ws.onclose = (e) => {
      console.log("❌ [Hub WS] onclose", e.code, e.reason)
    }

    ws.onerror = (e) => {
      console.error("🔥 [Hub WS] error", e)
    }

    return () => ws.close()
  }, [])

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
        wsRef.current?.send(JSON.stringify({ command: 'list_sessions' }))
        break
      case 'session_deleted':
        wsRef.current?.send(JSON.stringify({ command: 'list_sessions' }))
        break
      case 'session_renamed':
        wsRef.current?.send(JSON.stringify({ command: 'list_sessions' }))
        break
      case 'open_sessions':
        console.log('[Hub WS] open_sessions received:', msg.sessions)
        if (msg.sessions && msg.sessions.length > 0) {
          msg.sessions.forEach(s => {
            loadTab(s.session_id)
          })
        }
        break
      default:
        // Other events (state_changed, conversation_changed, etc.)
        // are handled by individual SessionTab WebSockets.
        break
    }
  }

  // ── Hub sendCommand (only for sessions-list operations) ─────────────────
  const hubSend = useCallback((command, payload = {}) => {
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) return
    ws.send(JSON.stringify({ command, ...payload }))
  }, [])

  // ── Tab management ──────────────────────────────────────────────────────
  const addTab = useCallback((sessionId = null) => {
    const tabId = `tab-${nextTabId++}`
    setTabs((prev) => [...prev, { tabId, sessionId }])
    setActiveTabId(tabId)
  }, [])

  // Open a tab for an existing session (auto-load from hub WS or sidebar)
  const loadTab = useCallback((sessionId) => {
    // Don't create duplicate tabs for the same session.
    // Use functional updater to avoid stale closure on `tabs`.
    setTabs((prev) => {
      const existing = prev.find((t) => t.sessionId === sessionId)
      if (existing) {
        setActiveTabId(existing.tabId)
        return prev
      }
      const tabId = `tab-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
      setActiveTabId(tabId)
      return [...prev, { tabId, sessionId }]
    })
  }, [])

  // Initiate close: send close_session over the tab's own WS.
  // Do NOT remove from DOM yet — wait for server acknowledgement.
  const initiateCloseTab = useCallback((tabId) => {
    const actions = tabActionsRef.current[tabId]
    if (actions?.sendCommand) {
      actions.sendCommand('close_session')
    } else {
      // No WS connected — remove immediately
      removeTab(tabId)
    }
  }, [])

  // Actually remove the tab from DOM (called when server acknowledges close
  // via session_closed event, or on unexpected WS close).
  const removeTab = useCallback((tabId) => {
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
    addTab(null) // null sessionId = fresh session
  }, [addTab])

  // Open an existing session in a tab (called from SessionList sidebar)
  const handleOpenTab = useCallback((sessionId) => {
    loadTab(sessionId)
  }, [loadTab])

  const handleRunningChange = useCallback((tabId, isRunning) => {
    setTabRunningStates((prev) => ({ ...prev, [tabId]: isRunning }))
  }, [])

  const handleSessionSaved = useCallback((sessionId) => {
    // Refresh sessions list
    hubSend('list_sessions')
  }, [hubSend])

  const handleNewSessionCreated = useCallback((sessionId) => {
    // Update the tab that created this session with its new sessionId
    setTabs((prev) =>
      prev.map((t) => (t.sessionId === null ? { ...t, sessionId } : t))
    )
  }, [])

  // ── Tab action registry (for save from SessionList) ───────────────────
  const handleRegisterTab = useCallback((tabId, actions) => {
    tabActionsRef.current[tabId] = actions
  }, [])

  const handleSaveActiveTab = useCallback(() => {
    const actions = tabActionsRef.current[activeTabId]
    if (actions?.sendCommand) {
      actions.sendCommand('save_session')
    }
  }, [activeTabId])

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

  const activeTab = tabs.find((t) => t.tabId === activeTabId)

  // ── Render ──────────────────────────────────────────────────────────────
  return (
    <div className="app-container">
      <TabBar
        tabs={tabItems}
        activeTabId={activeTabId}
        onSelectTab={setActiveTabId}
        onCloseTab={initiateCloseTab}
        onNewTab={handleNewTab}
        runningStates={tabRunningStates}
      />

      <div className="app-main">
        {/* All tabs stay mounted; inactive ones hidden with display:none */}
        <div className="app-center tab-content-area">
          {tabs.length === 0 ? (
            <div className="empty-state">
              <p>Open a session or create a new one to get started.</p>
            </div>
          ) : (
            tabs.map((tab) => (
              <div
                key={tab.tabId}
                className="tab-wrapper"
                style={{ display: tab.tabId === activeTabId ? '' : 'none' }}
              >
                <SessionTab
                  sessionId={tab.sessionId}
                  tabId={tab.tabId}
                  onClose={() => removeTab(tab.tabId)}
                  onNewSession={handleNewSessionCreated}
                  onSessionSaved={handleSessionSaved}
                  onRegister={(actions) => handleRegisterTab(tab.tabId, actions)}
                  onRunningChange={handleRunningChange}
                />
              </div>
            ))
          )}
        </div>

        {/* Sessions sidebar — hidden by default, toggle with ☰ */}
        <div className={`session-sidebar ${showSessions ? 'open' : ''}`}>
          <SessionList
            sessions={sessions}
            onSave={handleSaveActiveTab}
            saveEnabled={activeTabId != null}
            onNew={handleNewTab}
            onOpenTab={handleOpenTab}
            onDelete={(sessionId) => hubSend('delete_session', { session_id: sessionId })}
            onRename={(sessionId, newName) =>
              hubSend('rename_session', { session_id: sessionId, new_name: newName })
            }
          />
        </div>

        {/* Toggle button for sessions sidebar */}
        <button
          className="session-toggle"
          onClick={() => setShowSessions((s) => !s)}
          title="Toggle sessions list"
        >
          ☰
        </button>
      </div>
    </div>
  )
}
