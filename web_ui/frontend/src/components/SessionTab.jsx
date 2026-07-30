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

const WS_PORT = import.meta.env.VITE_BACKEND_PORT || '8000';
const WS_URL = `ws://${window.location.hostname}:${WS_PORT}/ws`

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
  workspaceId: null,
}

// ────────────────────────────────────────────────────────────────────────────
// Component
// ────────────────────────────────────────────────────────────────────────────
function SessionTab({ mode = null, sessionId, tabId, hubReady, staggerMs = 0, loadOnConnect = true, isActive = false, onClose, onNewSession, onOpenNewTab, onSessionSaved, onRegister, onRunningChange, onSessionRenamed, selectedWorker, onSelectWorker, activeSessionId, onClearWorker, onWorkerEvent, onLoggingConfigChanged, sessionName = '' }) {
  const [state, setState] = useState(INITIAL_STATE)
  const [currentSessionId, setCurrentSessionId] = useState(sessionId)
  const [displayName, setDisplayName] = useState(sessionName || '')
  const [isRenaming, setIsRenaming] = useState(false)
  const renameInputRef = useRef(null)
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false)
  const deleteConfirmRef = useRef(null)
  const currentSessionIdRef = useRef(currentSessionId)
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
  const [containerRebuildResult, setContainerRebuildResult] = useState(null) // null | { status, buildLog }
  const [sessionReady, setSessionReady] = useState(false) // true after session_loaded confirms session is ready
  const [isDeferred, setIsDeferred] = useState(false) // true = load skipped; waiting for activation
  const [totalMessages, setTotalMessages] = useState(0) // total messages in the session
  const [hasMore, setHasMore] = useState(false) // true if older messages are available
  const [scrollToBottomKey, setScrollToBottomKey] = useState(0) // incremented to force scroll to bottom (R3)
  const loadSentRef = useRef(false) // true once load_session has been sent (by any path)
  const dataReceivedRef = useRef(false) // true once we've received a response to our load
  const modeRef = useRef(mode)
  modeRef.current = mode
  const isActiveRef = useRef(isActive)
  isActiveRef.current = isActive
  const prevIsActiveRef = useRef(isActive)
  const providersRef = useRef(providers)
  providersRef.current = providers
  const availableToolsRef = useRef(availableTools)
  availableToolsRef.current = availableTools
  const availableToolsModeRef = useRef(null)
  const isWsConnectedOrConnecting = () => {
    const ws = wsRef.current
    return ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)
  }

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

  // ── Clear deferred state when a response arrives from a triggered load ──
  // Exits deferred on ANY conversation_changed or session_loaded response,
  // even if the session is empty (history.length === 0).
  useEffect(() => {
    if (isDeferred && dataReceivedRef.current) {
      setIsDeferred(false)
      loadSentRef.current = true
    }
  }, [isDeferred, state.history])

  // ── Derived helpers ─────────────────────────────────────────────────────
  // ── Keep currentSessionIdRef in sync (avoids stale closure in handleEvent) ──
  useEffect(() => {
    currentSessionIdRef.current = currentSessionId
  }, [currentSessionId])

  const update = useCallback((patch) => {
    setState((prev) => ({
      ...prev,
      ...(typeof patch === 'function' ? patch(prev) : patch),
    }))
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
  const onWorkerEventRef = useRef(onWorkerEvent)
  onWorkerEventRef.current = onWorkerEvent
  const onLoggingConfigChangedRef = useRef(onLoggingConfigChanged)
  onLoggingConfigChangedRef.current = onLoggingConfigChanged

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
      console.log(`[SessionTab ${sessionId || 'new'}] WS onopen, isActive=${isActiveRef.current}, loadOnConnect=${loadOnConnectRef.current}, sessionId=${sessionIdRef.current}`)
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
        ws.send(JSON.stringify({
          command: 'new_session',
          mode: modeRef.current || 'custom',
        }))
      }

      // Fetch providers and tools list only if not already cached
      if (!providersRef.current || providersRef.current.length === 0) {
        sendCommand('get_providers')
      } else {
        console.log('[SessionTab] Skipping get_providers (already cached)')
      }
      if (!availableToolsRef.current || availableToolsRef.current.length === 0) {
        sendCommand('get_available_tools', { mode: mode || 'custom' })
        if (mode) {
          availableToolsModeRef.current = mode
        }
      } else {
        console.log('[SessionTab] Skipping get_available_tools (already cached)')
      }
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

  // ── Activation safety net (reconnect on tab switch) ────────────────────
  // This fires when the user clicks an inactive tab. If the tab somehow lost
  // its WebSocket (e.g. server restart), this reconnects it.
  // NOTE: On initial mount, the mount effect (hubReady + staggerMs) handles
  // the first connection attempt. This effect only activates on isActive
  // *transitions* (false → true), not on the initial mount where isActive
  // is already true.
  useEffect(() => {
    const prevIsActive = prevIsActiveRef.current
    prevIsActiveRef.current = isActive
    console.log(`[DEBUG SessionTab Activate effect] tabId=${tabId}, sessionId=${sessionId}, isActive=${isActive}, prevIsActive=${prevIsActive}, hubReady=${hubReady}, wsConnected=${isWsConnectedOrConnecting()}, tabConnecting=${tabConnectingRef.current}`)
    // Skip on initial mount — mount effect handles the first connection
    if (prevIsActive === isActive) return
    // Only reconnect on inactive → active transitions (user clicked tab)
    if (!isActive) return
    if (!hubReady) return
    if (isWsConnectedOrConnecting()) {
      console.log(`[DEBUG SessionTab Activate effect] tab ${tabId} already connected — skipping`)
      return
    }
    if (tabConnectingRef.current) return

    console.log(`[SessionTab ${sessionId || 'new'}] Activating — connecting WS now`)
    closedRef.current = false
    connectSessionWsRef.current()
  }, [hubReady, isActive])

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
        console.log('[TOKEN_PIPELINE] SessionTab: tokens_updated arrived', { type: msg.type, input: msg.input, output: msg.output, source: msg.source })
        update({
          tokensIn: msg.input ?? 0,
          tokensOut: msg.output ?? 0,
        })
        // Forward to worker panel if this is a worker-sourced token update
        // IMPORTANT: Use currentSessionIdRef to avoid stale closure (connectSessionWs has [] deps)
        if (msg.source === 'worker') {
          const effectiveSessionId = currentSessionIdRef.current || sessionId
          console.log('[TOKEN_PIPELINE] SessionTab: forwarding tokens_updated to worker panel', { sessionId: effectiveSessionId })
          onWorkerEventRef.current?.(effectiveSessionId, msg)
        }
        break

      case 'context_updated':
        console.log('[TOKEN_PIPELINE] SessionTab: context_updated arrived', { context_length: msg.context_length, source: msg.source, worker_name: msg.worker_name })
        update({ contextLength: msg.context_length ?? 0 })
        // Forward to worker panel if this is a worker-sourced context update
        // IMPORTANT: Use currentSessionIdRef to avoid stale closure (connectSessionWs has [] deps)
        if (msg.source === 'worker') {
          const effectiveSessionId = currentSessionIdRef.current || sessionId
          console.log('[TOKEN_PIPELINE] SessionTab: forwarding context_updated to worker panel', { sessionId: effectiveSessionId, worker_name: msg.worker_name })
          onWorkerEventRef.current?.(effectiveSessionId, msg)
        }
        break

      case 'conversation_changed':
        console.log('conversation_changed RAW:', msg)
        // Mark that we've received a response (even if empty) so deferred
        // tabs can exit their placeholder state.
        dataReceivedRef.current = true;
        // Trust the server's is_system_notification flag — no index-based
        // fallback that could leak the flag to wrong messages (Bug 2 & 3).
        const serverMessages = msg.messages ?? [];
        const mergedMessages = serverMessages.map((m) => ({
          ...m,
          is_system_notification: m.is_system_notification || false,
        }));
        const notes = mergedMessages.filter(m => m.is_system_notification);
        if (notes.length > 0) console.log('🔔 SYSTEM NOTIFICATIONS:', notes);
        // Detect context compaction messages — force scroll to bottom (R3)
        if (notes.some(m => {
          const text = (m.content || '').toLowerCase()
          return text.includes('context now free') ||
                 text.includes('summar') ||
                 text.includes('compact') ||
                 text.includes('messages removed')
        })) {
          setScrollToBottomKey(k => k + 1)
        }
        update({ history: mergedMessages })
        // Pagination metadata from the server
        if (msg.total_count !== undefined) {
          setTotalMessages(msg.total_count)
        }
        setHasMore(msg.has_more === true)
        break

      case 'more_messages':
        // Prepend older messages to the current history
        console.log('[SessionTab] more_messages:', msg.messages?.length, 'messages, offset:', msg.offset, 'has_more:', msg.has_more)
        const olderMessages = (msg.messages ?? []).map((m) => ({
          ...m,
          is_system_notification: m.is_system_notification || false,
        }))
        update((prev) => ({
          history: [...olderMessages, ...prev.history],
        }))
        setHasMore(msg.has_more === true)
        break

      case 'config_changed':
        // Replace config entirely with what the backend sends.
        // The backend is now the single source of truth; it always sends
        // a complete frontend-format config (including tools, provider, etc.).
        update({ config: msg.config })
        // Re-fetch available tools if the mode changed (e.g. session loaded
        // from disk where mode wasn't known at initial WS connection time).
        if (msg.config?.mode && msg.config.mode !== (availableToolsModeRef.current || 'custom')) {
          availableToolsModeRef.current = msg.config.mode
          sendCommand('get_available_tools', { mode: msg.config.mode })
        }
        break

      case 'rebuild_result':
        setContainerRebuildResult({ status: msg.status, buildLog: msg.build_log })
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
        dataReceivedRef.current = true;
        if (msg.workspace_id) {
          update({ workspaceId: msg.workspace_id })
        }
        if (msg.session_id) {
          if (sessionId && sessionId !== msg.session_id) {
            // Tab already has a different session → this is a workspace switch
            // that created a new session. Notify parent to open a new tab for it,
            // while keeping this tab pointing to the original session.
            console.log('[SessionTab] session_loaded for DIFFERENT session, opening new tab:', msg.session_id)
            onOpenNewTab?.(msg.session_id, msg.session_name)
          } else {
            // Fresh tab (no sessionId yet) → update currentSessionId
            setCurrentSessionId(msg.session_id)
            setDisplayName(msg.session_name || displayName)
            setSessionReady(true)
            // Notify parent that this tab now has a real sessionId
            if (!sessionId) {
              onNewSession?.(msg.session_id, msg.session_name)
            }
          }
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
        setDisplayName(msg.new_name || displayName)
        onSessionRenamed?.(msg.session_id, msg.new_name)
        break

      case 'session_closed':
        closedRef.current = true
        setSessionReady(false)
        onClose?.()
        break

      case 'session_deleted':
        // Session was deleted from the store — close the tab
        setSessionReady(false)
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

      // ── Worker lifecycle events + per-worker bus events (real-time from bridge) ──
      case 'worker:tool_call':
      case 'worker:tool_result':
      // worker:token_warning/turn_warning/time_warning now come via per-worker bus (not global bus)
      case 'worker:token_warning':
      case 'worker:turn_warning':
      case 'worker:time_warning':
      case 'worker:assistant_message':
      case 'worker:worker_spawned':
      case 'worker:worker_status':
      case 'worker:worker_completed':
      case 'worker:worker_error':
      case 'worker:system_notification':
      case 'worker:user_message':
      case 'worker:worker_message':
      case 'worker:tokens_updated':
      case 'worker:context_updated':
      case 'worker:context_summarized':
      case 'worker:context_cleared':
      case 'worker:token_recovery':
        // For worker:tokens_updated, also update the token counter display
        // (belt-and-suspenders — the bridge should flatten to 'tokens_updated',
        //  but if it doesn't, this ensures the counter still updates).
        if (msg.type === 'worker:tokens_updated') {
          update({
            tokensIn: msg.data?.total_input ?? 0,
            tokensOut: msg.data?.total_output ?? 0,
          })
        }
        // Use refs to avoid stale closure (connectSessionWs has [] deps)
        if (onWorkerEventRef.current) {
          onWorkerEventRef.current(currentSessionIdRef.current || currentSessionId, msg)
        }
        break

      case 'logging_config_changed':
        if (onLoggingConfigChangedRef.current) {
          onLoggingConfigChangedRef.current(msg.config)
        }
        break

      default:
        console.warn('[SessionTab] Unknown event type:', msg.type)
    }
  }

  // ── Global keydown: auto-focus query input on keystroke ───────────────────
  // Whenever the user presses a key and no form field (input/textarea/select)
  // is focused, bounce focus to the query bar. This handles the common case
  // where someone clicks a copy button (which doesn't steal focus thanks to
  // the onMouseDown trick in CopyButton) or clicks on blank space, then just
  // starts typing — the next keystroke lands in the prompt.
  useEffect(() => {
    const handler = (e) => {
      // Never steal focus from active form fields
      const tag = e.target?.tagName?.toLowerCase()
      if (tag === 'input' || tag === 'textarea' || tag === 'select') return
      if (e.target?.isContentEditable) return

      // Don't intercept keyboard shortcuts (Ctrl+C, Cmd+V, etc.)
      if (e.ctrlKey || e.metaKey || e.altKey) return

      // Only redirect printable-character keystrokes — arrow keys, Escape,
      // Tab, Enter, etc. are left alone
      if (e.key.length !== 1) return

      const queryInput = document.querySelector('.query-input')
      if (queryInput && document.activeElement !== queryInput) {
        queryInput.focus()
      }
    }

    document.addEventListener('keydown', handler, true)  // capture phase
    return () => document.removeEventListener('keydown', handler, true)
  }, [])


  // ── Load more (pagination) ────────────────────────────────────────────────
  const loadMore = useCallback(() => {
    const offset = state.history.length
    sendCommand('load_more_messages', { offset, limit: 20 })
  }, [state.history.length, sendCommand])

  // ── Auto-focus delete confirm button ────────────────────────────────────────
  useEffect(() => {
    if (showDeleteConfirm && deleteConfirmRef.current) {
      deleteConfirmRef.current.focus()
    }
  }, [showDeleteConfirm])

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
      {/* ── Inline rename header ──────────────────────── */}
      <div className="session-header">
        {currentSessionId ? (
          isRenaming ? (
            <input
              ref={renameInputRef}
              className="session-header-input"
              defaultValue={displayName}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  const newName = e.target.value.trim()
                  if (newName && newName !== displayName) {
                    sendCommand('rename_session', { session_id: currentSessionId, new_name: newName })
                    setDisplayName(newName)
                    onSessionRenamed?.(currentSessionId, newName)
                  }
                  setIsRenaming(false)
                } else if (e.key === 'Escape') {
                  setIsRenaming(false)
                }
              }}
              onBlur={(e) => {
                const newName = e.target.value.trim()
                if (newName && newName !== displayName) {
                  sendCommand('rename_session', { session_id: currentSessionId, new_name: newName })
                  setDisplayName(newName)
                  onSessionRenamed?.(currentSessionId, newName)
                }
                setIsRenaming(false)
              }}
              autoFocus
            />
          ) : (
            <>
              <span className="session-header-name">{displayName || 'Untitled'}</span>
              <button
                className="session-header-rename-btn"
                onClick={() => {
                  setIsRenaming(true)
                  setTimeout(() => renameInputRef.current?.focus(), 0)
                }}
                title="Rename session"
              >
                Rename
              </button>
              <div className="session-header-spacer" />

              {showDeleteConfirm ? (
                <span className="session-header-delete-confirm">
                  <span className="session-header-delete-warn">Delete?</span>
                  <button
                    ref={deleteConfirmRef}
                    className="session-header-btn session-header-confirm-delete-btn"
                    onClick={() => {
                      sendCommand('delete_session', { session_id: currentSessionId })
                      setShowDeleteConfirm(false)
                    }}
                  >
                    Yes
                  </button>
                  <button
                    className="session-header-btn session-header-cancel-btn"
                    onClick={() => setShowDeleteConfirm(false)}
                  >
                    No
                  </button>
                </span>
              ) : (
                <button
                  className="session-header-btn session-header-delete-btn"
                  onClick={() => {
                    setShowDeleteConfirm(true)
                    setTimeout(() => deleteConfirmRef.current?.focus(), 0)
                  }}
                  title="Delete session"
                >
                  Delete
                </button>
              )}
            </>
          )
        ) : null}
      </div>
      <StatusBar
        status={state.status}
        tokensIn={state.tokensIn}
        tokensOut={state.tokensOut}
        contextLength={state.contextLength}
      />
      <div className="app-main">
        <ConfigPanel
          mode={mode}
          config={state.config}
          sendCommand={sendCommand}
          providers={providers}
          availableTools={availableTools}
          panelWidth={configPanelWidth}
          wsConnected={wsConnected}
          workspaceId={state.workspaceId}
          sessionId={currentSessionId}
          defaultConfigSaveStatus={defaultConfigSaveStatus}
          defaultConfigSaveMessage={defaultConfigSaveMessage}
          onClearDefaultSaveStatus={() => {
            setDefaultConfigSaveStatus(null)
            setDefaultConfigSaveMessage('')
          }}
          containerRebuildResult={containerRebuildResult}
          onClearRebuildResult={() => setContainerRebuildResult(null)}
          selectedWorker={selectedWorker}
          onSelectWorker={onSelectWorker}
          isActive={isActive}
          activeSessionId={activeSessionId}
          onClearWorker={onClearWorker}
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
          <ChatPanel messages={state.history} loadMore={loadMore} hasMore={hasMore} scrollToBottomKey={scrollToBottomKey} />
          <QueryBar
            sendCommand={sendCommand}
            status={state.status}
            isRunning={state.isRunning}
            config={state.config}
            mode={mode}
            sessionId={currentSessionId}
            sessionReady={sessionReady}
          />
        </div>
        {/* Session list is rendered by App, not per-tab */}
      </div>
    </div>
  )
}

export default React.memo(SessionTab, (prevProps, nextProps) => {
  // Only re-render if session-specific props change
  // or selectedWorker changes (affects worker highlighting)
  // Ignore changes to callback props (onClose, onRegister, etc.)
  // which create new references on every parent render
  return (
    prevProps.mode === nextProps.mode &&
    prevProps.sessionId === nextProps.sessionId &&
    prevProps.hubReady === nextProps.hubReady &&
    prevProps.staggerMs === nextProps.staggerMs &&
    prevProps.loadOnConnect === nextProps.loadOnConnect &&
    prevProps.isActive === nextProps.isActive &&
    prevProps.selectedWorker?.name === nextProps.selectedWorker?.name &&
    prevProps.selectedWorker?.workspaceId === nextProps.selectedWorker?.workspaceId &&
    prevProps.onLoggingConfigChanged === nextProps.onLoggingConfigChanged
  )
})
