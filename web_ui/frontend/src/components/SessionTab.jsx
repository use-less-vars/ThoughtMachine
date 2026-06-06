/*
 * SessionTab.jsx
 *
 * Self-contained session tab with its own WebSocket connection.
 * Each tab creates a separate bridge on the backend, enabling
 * independent session interaction per tab.
 *
 * Props:
 *   sessionId  — existing session to load, or null for a fresh session
 *   onClose    — called when the user or backend closes this tab
 *   onSessionSaved — called with (sessionId) so App can track new sessions
 *   onNewSession   — called with (sessionId) when a new session is created
 *
 * State (local — not in Zustand):
 *   status, history, tokensIn/Out, contextLength, isRunning, config
 *
 * Child components receive session state as props instead of reading
 * from the global store.
 */

import React, { useEffect, useRef, useState, useCallback } from 'react'
import ChatPanel from './ChatPanel'
import QueryBar from './QueryBar'
import StatusBar from './StatusBar'
import ConfigPanel from './ConfigPanel'
import SecurityDialog from './SecurityDialog'

const CONFIG_PANEL_MIN_WIDTH = 200
const CONFIG_PANEL_MAX_WIDTH = 500
const CONFIG_PANEL_DEFAULT_WIDTH = 280

const WS_URL = `ws://${window.location.hostname}:8000/ws`

// ────────────────────────────────────────────────────────────────────────────
// Initial per-tab state
// ────────────────────────────────────────────────────────────────────────────
const INITIAL_STATE = {
  status: 'IDLE',
  history: [],
  tokensIn: 0,
  tokensOut: 0,
  contextLength: 0,
  isRunning: false,
  config: null,
}

// ────────────────────────────────────────────────────────────────────────────
// Component
// ────────────────────────────────────────────────────────────────────────────
function SessionTab({ sessionId, tabId, hubReady, staggerMs = 0, loadOnConnect = true, onClose, onNewSession, onSessionSaved, onRegister, onRunningChange, onSessionRenamed }) {
  const [state, setState] = useState(INITIAL_STATE)
  const [currentSessionId, setCurrentSessionId] = useState(sessionId)
  const [providers, setProviders] = useState([])
  const [availableTools, setAvailableTools] = useState([])
  const wsRef = useRef(null)
  const closedRef = useRef(false)  // prevent double-close
  const tabConnectingRef = useRef(false)  // prevent StrictMode duplicate
  const pendingCommandsRef = useRef([])  // queued commands for retry
  const connectSessionWsRef = useRef(null)  // ref to avoid circular deps in sendCommand
  const [wsConnected, setWsConnected] = useState(false)
  const [defaultConfigSaveStatus, setDefaultConfigSaveStatus] = useState(null) // null | 'ok' | 'error'
  const [defaultConfigSaveMessage, setDefaultConfigSaveMessage] = useState('')
  const [securityPrompt, setSecurityPrompt] = useState(null) // null | { request_id, tool_name, capabilities, ... }
  const [isDeferred, setIsDeferred] = useState(false) // true = load skipped; waiting for activation
  const loadSentRef = useRef(false) // true once load_session has been sent (by any path)

  // ── Config panel resize state (persisted per tab) ──────────────────
  const [configPanelWidth, setConfigPanelWidth] = useState(() => {
    if (tabId) {
      const saved = localStorage.getItem(`config-panel-width:${tabId}`)
      if (saved) return Math.max(CONFIG_PANEL_MIN_WIDTH, Math.min(CONFIG_PANEL_MAX_WIDTH, Number(saved)))
    }
    return CONFIG_PANEL_DEFAULT_WIDTH
  })
  const dragRef = useRef(null) // { startX, startWidth }

  // Persist width changes
  const handleWidthChange = useCallback((newWidth) => {
    setConfigPanelWidth(newWidth)
    if (tabId) {
      localStorage.setItem(`config-panel-width:${tabId}`, String(newWidth))
    }
  }, [tabId])

  const handleResizeStart = useCallback((e) => {
    e.preventDefault()
    dragRef.current = { startX: e.clientX, startWidth: configPanelWidth }

    // Set cursor on body during drag
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'

    const handleMouseMove = (e) => {
      if (!dragRef.current) return
      const delta = e.clientX - dragRef.current.startX
      const newWidth = Math.max(CONFIG_PANEL_MIN_WIDTH, Math.min(CONFIG_PANEL_MAX_WIDTH, dragRef.current.startWidth + delta))
      handleWidthChange(newWidth)
    }

    const handleMouseUp = () => {
      dragRef.current = null
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      document.removeEventListener('mousemove', handleMouseMove)
      document.removeEventListener('mouseup', handleMouseUp)
    }

    document.addEventListener('mousemove', handleMouseMove)
    document.addEventListener('mouseup', handleMouseUp)
  }, [configPanelWidth])

  // ── Clear deferred state when data arrives from a triggered load ──────
  useEffect(() => {
    if (isDeferred && state.history.length > 0) {
      setIsDeferred(false)
      loadSentRef.current = true
    }
  }, [state.history, isDeferred])

  // ── Derived helpers ─────────────────────────────────────────────────────
  const update = useCallback((patch) => {
    setState((prev) => ({ ...prev, ...patch }))
  }, [])

  // ── Notify parent when running state changes (for tab color) ────────
  useEffect(() => {
    onRunningChange?.(tabId, state.status)
  }, [state.status, tabId, onRunningChange])

  // ── Debug: log whenever history changes ─────────────────────────────
  useEffect(() => {
    console.log('[SessionTab] history updated, length:', state.history.length,
      'first role:', state.history[0]?.role,
      'last role:', state.history[state.history.length - 1]?.role)
  }, [state.history])

  // ── sendCommand — sends over this tab's WebSocket with auto-queue ───
  // If the WS is not OPEN, the command is queued and resent once the
  // connection is re-established.
  const sendCommandRef = useRef(null)
  const sendCommand = useCallback((command, payload = {}) => {
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      console.warn('[SessionTab] WebSocket not connected. Command queued for retry.')
      pendingCommandsRef.current.push({ command, payload })
      // Attempt immediate reconnect
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current)
        reconnectTimeoutRef.current = null
      }
      connectSessionWsRef.current?.()
      return
    }
    console.log(`[SessionTab] Sending command: ${command}`, JSON.stringify({ command, ...payload }))
    ws.send(JSON.stringify({ command, ...payload }))
  }, [])
  sendCommandRef.current = sendCommand

  // ── WebSocket lifecycle with auto-reconnect ────────────────────────────
  const reconnectTimeoutRef = useRef(null)
  const sessionIdRef = useRef(sessionId)
  sessionIdRef.current = sessionId
  const onRegisterRef = useRef(onRegister)
  onRegisterRef.current = onRegister
  const loadOnConnectRef = useRef(loadOnConnect)
  loadOnConnectRef.current = loadOnConnect

  const connectSessionWs = useCallback(() => {
    // Guard: prevent duplicate connections from StrictMode double-mount
    if (tabConnectingRef.current) {
      console.log(`[SessionTab ${sessionId || 'new'}] Already connecting, skipping duplicate`)
      return
    }
    tabConnectingRef.current = true

    // Reset closed guard — allows StrictMode double-effect and reconnections
    closedRef.current = false

    // Clear any pending reconnect
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current)
      reconnectTimeoutRef.current = null
    }

    if (closedRef.current) return  // component unmounted (may be set during clearTimeout)

    const ws = new WebSocket(WS_URL)
    wsRef.current = ws

    ws.onopen = () => {
      console.log(`[SessionTab ${sessionId || 'new'}] WS onopen`)
      tabConnectingRef.current = false
      setWsConnected(true)

      // Drain any commands that were queued while disconnected
      const pending = pendingCommandsRef.current
      pendingCommandsRef.current = []
      for (const cmd of pending) {
        console.log(`[SessionTab] Sending queued command: ${cmd.command}`)
        ws.send(JSON.stringify({ command: cmd.command, ...cmd.payload }))
      }

      // Register sendCommand with parent (use ref to avoid stale closure)
      onRegisterRef.current?.({ sendCommand: sendCommandRef.current, getSessionId: () => currentSessionId })

      // If we have a sessionId, load it immediately (active tab) or defer (inactive tab)
      const sid = sessionIdRef.current
      if (sid) {
        if (loadOnConnectRef.current) {
          ws.send(JSON.stringify({
            command: 'load_session',
            session_id: sid
          }))
          console.log(`[SessionTab ${sid}] Sent load_session (active tab)`)
        } else {
          // Check if load_session was already queued by App trigger (user clicked tab before WS connected)
          const hasQueuedLoad = pending.some(cmd => cmd.command === 'load_session')
          if (!hasQueuedLoad) {
            setIsDeferred(true)
            console.log(`[SessionTab ${sid}] Deferred load (inactive tab)`)
          } else {
            console.log(`[SessionTab ${sid}] Load already queued, skipping deferred placeholder`)
          }
        }
      } else {
        ws.send(JSON.stringify({ command: 'new_session' }))
      }

      // Fetch providers and tools list for this session
      sendCommand('get_providers')
      sendCommand('get_available_tools')
    }

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data)
        handleEvent(msg)
      } catch (err) {
        console.error('[SessionTab] Failed to parse message:', event.data, err)
      }
    }

    ws.onclose = (e) => {
      tabConnectingRef.current = false
      setWsConnected(false)
      // 1001 = normal close (component unmounting), don't reconnect
      if (e.code !== 1001 && !closedRef.current) {
        const delay = 1000 + Math.random() * 3000  // 1–4s jitter
        console.log(`[SessionTab ${sessionIdRef.current || '?'}] disconnected, reconnecting in ${Math.round(delay)}ms...`)
        reconnectTimeoutRef.current = setTimeout(connectSessionWs, delay)
      }
    }

    ws.onerror = () => {
      // onclose fires right after onerror, so we let onclose handle reconnection
    }
  }, [])  // all external values via refs — no cascade on sessionId/onRegister change
  connectSessionWsRef.current = connectSessionWs

  // Connect only after hub is ready, with optional stagger delay
  // NOTE: deps = [hubReady] only — connectSessionWs is read via ref to avoid
  // reconnect cascades when sessionId/onRegister change at the parent.
  useEffect(() => {
    if (!hubReady) return

    const timer = setTimeout(() => {
      connectSessionWsRef.current()
    }, staggerMs)

    return () => {
      clearTimeout(timer)
      closedRef.current = true
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current)
      }
      try {
        wsRef.current?.close()
      } catch {
        // ignore — WebSocket may still be in CONNECTING state
      }
    }
  }, [hubReady])

  // ── Event router ─────────────────────────────────────────────────────────
  function handleEvent(msg) {
    // ── Debug: log conversation data ─────────────────────────────────
    if (msg.type === 'conversation_changed') {
      console.log('[SessionTab] conversation_changed:', {
        messagesCount: msg.messages?.length,
        firstMsg: msg.messages?.[0]?.content?.slice(0, 60),
        lastMsg: msg.messages?.[msg.messages?.length - 1]?.content?.slice(0, 60),
      })
    }
    switch (msg.type) {
      case 'state_changed':
        update({
          status: msg.state,
          isRunning: msg.is_running !== false,
        })
        break

      case 'tokens_updated':
        update({
          tokensIn: msg.input ?? 0,
          tokensOut: msg.output ?? 0,
        })
        break

      case 'context_updated':
        update({ contextLength: msg.context_length ?? 0 })
        break

      case 'conversation_changed':
        console.log('conversation_changed RAW:', msg)
        // Trust the server's is_system_notification flag — no index-based
        // fallback that could leak the flag to wrong messages (Bug 2 & 3).
        const serverMessages = msg.messages ?? [];
        const mergedMessages = serverMessages.map((m) => ({
          ...m,
          is_system_notification: m.is_system_notification || false,
        }));
        const notes = mergedMessages.filter(m => m.is_system_notification);
        if (notes.length > 0) console.log('🔔 SYSTEM NOTIFICATIONS:', notes);
        update({ history: mergedMessages })
        break

      case 'config_changed':
        // Replace config entirely with what the backend sends.
        // The backend is now the single source of truth; it always sends
        // a complete frontend-format config (including tools, provider, etc.).
        update({ config: msg.config })
        break

      case 'status_message':
        update((prev) => ({
          history: [
            ...prev.history,
            { role: 'system', content: msg.text ?? '', is_system_notification: true },
          ],
        }))
        break

      case 'session_loaded':
        // Track the session ID so subsequent continue_session calls use it
        if (msg.session_id) {
          setCurrentSessionId(msg.session_id)
        }
        // If this is a new session (tab had no sessionId), notify parent
        if (msg.session_id && !sessionId) {
          onNewSession?.(msg.session_id, msg.session_name)
        }
        break

      case 'providers_list':
        setProviders(msg.providers || [])
        break

      case 'provider_saved':
        console.log('[SessionTab] Provider saved:', msg.provider?.id)
        break

      case 'provider_deleted':
        console.log('[SessionTab] Provider deleted:', msg.provider_id)
        break

      case 'tools_list':
        setAvailableTools(msg.tools || [])
        break

      case 'default_config_saved':
        setDefaultConfigSaveStatus(msg.status)
        setDefaultConfigSaveMessage(msg.message || '')
        break

      case 'session_saved':
        onSessionSaved?.()
        break

      case 'session_renamed':
        // The session was renamed; update our currentSessionId if needed
        if (msg.session_id) {
          setCurrentSessionId(msg.session_id)
        }
        onSessionRenamed?.(msg.session_id, msg.new_name)
        break

      case 'session_closed':
        closedRef.current = true
        onClose?.()
        break

      case 'session_deleted':
        // Session was deleted from the store — close the tab
        onClose?.()
        break

      case 'security_prompt':
        // Show the security approval dialog (tool permission request)
        console.log('[SessionTab] security_prompt:', msg)
        setSecurityPrompt({
          request_id: msg.request_id,
          tool_name: msg.tool_name,
          capabilities: msg.capabilities || [],
          arguments: msg.arguments || {},
          description: msg.description || `Tool '${msg.tool_name || 'unknown'}' requires your approval.`,
        })
        break

      default:
        console.warn('[SessionTab] Unknown event type:', msg.type)
    }
  }


  // ── Render ───────────────────────────────────────────────────────────────
  // When deferred, show a placeholder instead of the full tab UI
  if (isDeferred) {
    return (
      <div className="session-tab-content session-tab-deferred">
        <div className="deferred-placeholder">
          <div className="deferred-placeholder-message">Click tab to load conversation</div>
        </div>
      </div>
    )
  }

  // Pass per-tab state to children as props
  return (
    <div className="session-tab-content">
      <StatusBar
        status={state.status}
        tokensIn={state.tokensIn}
        tokensOut={state.tokensOut}
        contextLength={state.contextLength}
      />
      <div className="app-main">
        <ConfigPanel
          config={state.config}
          sendCommand={sendCommand}
          providers={providers}
          availableTools={availableTools}
          panelWidth={configPanelWidth}
          wsConnected={wsConnected}
          defaultConfigSaveStatus={defaultConfigSaveStatus}
          defaultConfigSaveMessage={defaultConfigSaveMessage}
          onClearDefaultSaveStatus={() => {
            setDefaultConfigSaveStatus(null)
            setDefaultConfigSaveMessage('')
          }}
        />
        <div
          className="resize-handle"
          onMouseDown={handleResizeStart}
          title="Drag to resize"
        />
        <div className="app-center">
          {securityPrompt && (
            <SecurityDialog
              prompt={securityPrompt}
              sendCommand={sendCommand}
              onDismiss={() => setSecurityPrompt(null)}
            />
          )}
          <ChatPanel messages={state.history} />
          <QueryBar
            sendCommand={sendCommand}
            status={state.status}
            isRunning={state.isRunning}
            config={state.config}
            sessionId={currentSessionId}
          />
        </div>
        {/* Session list is rendered by App, not per-tab */}
      </div>
    </div>
  )
}

export default React.memo(SessionTab, (prevProps, nextProps) => {
  // Only re-render if session-specific props change
  // Ignore changes to callback props (onClose, onRegister, etc.)
  // which create new references on every parent render
  return (
    prevProps.sessionId === nextProps.sessionId &&
    prevProps.hubReady === nextProps.hubReady &&
    prevProps.staggerMs === nextProps.staggerMs &&
    prevProps.loadOnConnect === nextProps.loadOnConnect
  )
})
