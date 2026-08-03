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
 * State (session-scoped — in Zustand store, sub-step 2.2):
 *   status, history, tokensIn/Out, contextLength, isRunning, config,
 *   providers, availableTools — keyed by sessionId, read via selectors
 *
 * Child components receive session state as props sourced from store
 * selectors; prop-drilling removal is sub-step 2.4.
 */

import React, { useEffect, useRef, useState, useCallback } from 'react'
import useStore from '../store/useStore'
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

// ── Fix 3a: stable empty array + memoized equality for sessionMessages ──
// SessionTab re-renders on ANY store slice change (status, tokens, etc.);
// without a stable messages reference, React.memo(ChatPanel) is defeated and
// ChatPanel re-renders (and re-runs its scroll effects) on every update.
const EMPTY_MESSAGES = []
// Chat messages carry no id/message_id in this app (verified: only session /
// workspace metadata has ids), so compare role + content of the FIRST and
// LAST messages. Length + first/last-message equality matches the store's
// update pattern (streaming mutates the LAST message; earlier messages are
// immutable, so the first message pins the conversation start).
function messagesEqual(a, b) {
  if (a === b) return true
  if (!a || !b) return false
  if (a.length !== b.length) return false
  if (a.length === 0) return true
  const fa = a[0]
  const fb = b[0]
  const la = a[a.length - 1]
  const lb = b[b.length - 1]
  return !!(fa && fb && la && lb) &&
    fa.role === fb.role && fa.content === fb.content &&
    la.role === lb.role && la.content === lb.content
}

// ────────────────────────────────────────────────────────────────────────────
// Initial per-tab state
// ────────────────────────────────────────────────────────────────────────────
// Session-scoped initial values now live in the Zustand store
// (see DEFAULT_SESSION_CONFIG / DEFAULT_SESSION_STATE in useStore.js).

// ────────────────────────────────────────────────────────────────────────────
// Component
// ────────────────────────────────────────────────────────────────────────────
function SessionTab({ sessionId, tabId, hubReady, staggerMs = 0, loadOnConnect = true, isActive = false, onClose, onNewSession, onOpenNewTab, onSessionSaved, onRegister, onSessionRenamed, selectedWorker, onSelectWorker, onWorkerEvent, onLoggingConfigChanged }) {
  const [currentSessionId, setCurrentSessionId] = useState(sessionId)
  const [isRenaming, setIsRenaming] = useState(false)
  const renameInputRef = useRef(null)
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false)
  const deleteConfirmRef = useRef(null)
  const currentSessionIdRef = useRef(currentSessionId)
  const [workspaceId, setWorkspaceId] = useState(null) // session metadata; no store slice yet (kept local)
  const wsRef = useRef(null)
  const closedRef = useRef(false)  // prevent double-close
  const tabConnectingRef = useRef(false)  // prevent StrictMode duplicate
  const pendingCommandsRef = useRef([])  // queued commands for retry
  const connectSessionWsRef = useRef(null)  // ref to avoid circular deps in sendCommand
  const [wsConnected, setWsConnected] = useState(false)
  const [defaultConfigSaveStatus, setDefaultConfigSaveStatus] = useState(null) // null | 'ok' | 'error'
  const [securityPrompt, setSecurityPrompt] = useState(null) // null | { request_id, tool_name, capabilities, ... }
  const [containerRebuildResult, setContainerRebuildResult] = useState(null) // null | { status, buildLog }
  const [sessionReady, setSessionReady] = useState(false) // true after session_loaded confirms session is ready
  // Fix 3b: stale-session recovery (backend restart). staleSessionRef gates
  // sendCommand (must be a ref — sendCommand is a []-deps callback); the state
  // drives banner rendering. pendingAdoptRef stashes the replacement session
  // the backend created for the dead id so 'Start New Session' can adopt it
  // through the same acceptance path a fresh tab uses.
  const [staleSession, setStaleSession] = useState(false)
  const staleSessionRef = useRef(false)
  const pendingAdoptRef = useRef(null)
  const [isDeferred, setIsDeferred] = useState(false) // true = load skipped; waiting for activation
  const [totalMessages, setTotalMessages] = useState(0) // total messages in the session
  const [hasMore, setHasMore] = useState(false) // true if older messages are available
  const [scrollToBottomKey, setScrollToBottomKey] = useState(0) // incremented to force scroll to bottom (R3)
  const loadSentRef = useRef(false) // true once load_session has been sent (by any path)
  const dataReceivedRef = useRef(false) // true once we've received a response to our load
  // Read mode from the store (sessionModes is keyed by sessionId; the prop
  // was removed — SessionTab reads it directly like other consumers).
  const mode = useStore((s) => (sessionId ? (s.sessionModes[sessionId] || null) : null))
  const modeRef = useRef(mode)
  modeRef.current = mode
  // ── Store subscriptions (sub-step 2.2): session-scoped state lives in Zustand ──
  const storeKey = currentSessionId || sessionId
  // Single source of truth for the display name: store.sessions (refreshed by
  // sessions_list, upserted immediately via updateSessionName on load/rename).
  const sessionName = useStore((s) =>
    storeKey ? (s.sessions.find((x) => x.session_id === storeKey)?.name || '') : ''
  )
  const sessionConfig = useStore((s) => s.sessionConfigs[storeKey])
  const sessionMessages = useStore((s) => s.sessionMessages[storeKey], messagesEqual)
  const sessionState = useStore((s) => s.sessionStates[storeKey])
  const sessionError = useStore((s) => (storeKey ? (s.sessionErrors[storeKey] || '') : ''))
  const config = sessionConfig?.config ?? null
  const providers = sessionConfig?.providers ?? []
  const availableTools = sessionConfig?.tools ?? []
  const history = sessionMessages ?? EMPTY_MESSAGES
  const status = sessionState?.state ?? 'IDLE'
  const isRunning = sessionState?.isRunning ?? false
  const contextLength = sessionState?.contextLength ?? 0
  const tokensIn = sessionState?.tokensIn ?? 0
  const tokensOut = sessionState?.tokensOut ?? 0
  const isActiveRef = useRef(isActive)
  isActiveRef.current = isActive
  const prevIsActiveRef = useRef(isActive)
  const providersRef = useRef(providers)
  providersRef.current = providers
  const configRef = useRef(config)
  configRef.current = config
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
  }, [isDeferred, history])

  // ── Derived helpers ─────────────────────────────────────────────────────
  // ── Keep currentSessionIdRef in sync (avoids stale closure in handleEvent) ──
  useEffect(() => {
    currentSessionIdRef.current = currentSessionId
  }, [currentSessionId])

  // ── Report running state to the store (for tab color) ────────────────
  // Written directly to Zustand (keyed by sessionId); previously went via
  // the onRunningChange prop keyed by tabId.
  useEffect(() => {
    const sid = currentSessionIdRef.current || sessionId
    if (sid) {
      useStore.getState().setTabRunningState(sid, status)
    }
  }, [status, sessionId])

  // ── sendCommand — sends over this tab's WebSocket with auto-queue ───
  // If the WS is not OPEN, the command is queued and resent once the
  // connection is re-established.
  const sendCommandRef = useRef(null)
  const sendCommand = useCallback((command, payload = {}) => {
    // Fix 3b: stale session — block ALL further commands (load_more, rename,
    // delete, security responses, config apply, query sends, ...) until the
    // user starts a new session.
    if (staleSessionRef.current) {
      console.warn('[SessionTab] Stale session — command blocked:', command)
      return
    }
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
    // Fix 3a: force ChatPanel scroll to bottom when the user sends a query.
    // Scoped to the two query-send commands ONLY — sendCommand is generic and
    // also serves get_providers / get_available_tools / load_more_messages /
    // rename_session / delete_session, which must NOT yank the viewport.
    // setScrollToBottomKey is a stable useState setter, so this stays valid
    // inside the []-deps callback.
    if (command === 'start_session' || command === 'continue_session') {
      setScrollToBottomKey(k => k + 1)
    }
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

    // Listener-ordering fix: register message/close/error listeners BEFORE
    // onopen so no event can be missed if the connection opens before the
    // onopen handler is installed (reconnects, StrictMode remounts, or any
    // WS implementation that fires `open` synchronously). onopen is the only
    // handler that sends commands, so it is attached last.
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
      // Fix 4b: reset one-shot guards so a reconnect can send load_session
      // again (the connection is fresh; the per-connection dedup restarts).
      loadSentRef.current = false
      dataReceivedRef.current = false
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

    ws.onopen = () => {
      console.log(`[SessionTab ${sessionId || 'new'}] WS onopen, isActive=${isActiveRef.current}, loadOnConnect=${loadOnConnectRef.current}, sessionId=${sessionIdRef.current}`)
      tabConnectingRef.current = false
      setWsConnected(true)

      // Fix 3b: stale session — don't re-load the dead session or re-drain
      // queued commands on reconnect (raw ws.send below bypasses sendCommand).
      if (staleSessionRef.current) {
        console.warn('[SessionTab] Stale session — skipping load_session / queued drain on reconnect')
        return
      }

      // Drain any commands that were queued while disconnected
      const pending = pendingCommandsRef.current
      pendingCommandsRef.current = []
      for (const cmd of pending) {
        console.log(`[SessionTab] Sending queued command: ${cmd.command}`)
        try {
          wsRef.current?.send(JSON.stringify({ command: cmd.command, ...cmd.payload }))
        } catch (err) {
          console.warn(`[SessionTab] queued command send failed (socket closed): ${cmd.command}`, err.message)
        }
      }

      // Register sendCommand with parent (use ref to avoid stale closure)
      onRegisterRef.current?.({ sendCommand: sendCommandRef.current, getSessionId: () => currentSessionId })

      // If we have a sessionId, load it immediately (active tab) or defer (inactive tab)
      // Fix 4b: the tab owns the single, deduped load path. On EVERY connect
      // (initial or reconnect) we send load_session for an existing session —
      // active tabs reload immediately, and inactive tabs still send it so
      // their session state refreshes after a disconnect. The ONLY skip is a
      // stale session (Fix 3b, above) or a closing/unmounting tab.
      const sid = sessionIdRef.current
      if (sid) {
        // Defer load_session by one tick so the parent's synchronous setup (handlers, etc.) completes first
        setTimeout(() => {
          if (loadSentRef.current) return  // Fix 4b: one load_session per connection
          loadSentRef.current = true
          try {
            wsRef.current?.send(JSON.stringify({
              command: 'load_session',
              session_id: sid
            }))
          } catch (err) {
            console.warn('[SessionTab] load_session send failed (socket closed):', err.message)
          }
        }, 0)
        if (loadOnConnectRef.current) {
          console.log(`[SessionTab ${sid}] Sent load_session (active tab)`)
        } else {
          // Fix 4b: inactive tabs still send load_session (state may have
          // changed while disconnected); keep the placeholder until the
          // response (or activation) clears it.
          setIsDeferred(true)
          console.log(`[SessionTab ${sid}] Sent load_session (deferred tab)`)
        }
      } else {
        if (loadSentRef.current) return  // Fix 4b: one new_session per connection
        loadSentRef.current = true
        try {
          wsRef.current?.send(JSON.stringify({
            command: 'new_session',
            mode: modeRef.current || 'custom',
          }))
        } catch (err) {
          console.warn('[SessionTab] new_session send failed (socket closed):', err.message)
        }
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
        if (msg.session_id && currentSessionIdRef.current && msg.session_id !== currentSessionIdRef.current) {
          console.warn('[SessionTab] state_changed for different session, ignoring:', msg.session_id)
          break
        }
        useStore.getState().receiveStateChanged(msg.session_id || currentSessionIdRef.current, msg.state)
        break

      case 'tokens_updated':
        if (msg.session_id && currentSessionIdRef.current && msg.session_id !== currentSessionIdRef.current) {
          console.warn('[SessionTab] tokens_updated for different session, ignoring:', msg.session_id)
          break
        }
        console.log('[TOKEN_PIPELINE] SessionTab: tokens_updated arrived', { type: msg.type, input: msg.input, output: msg.output, source: msg.source })
        useStore.getState().receiveTokensUpdated(msg.session_id || currentSessionIdRef.current, msg)
        // Forward to worker panel if this is a worker-sourced token update
        // IMPORTANT: Use currentSessionIdRef to avoid stale closure (connectSessionWs has [] deps)
        if (msg.source === 'worker') {
          const effectiveSessionId = currentSessionIdRef.current || sessionId
          console.log('[TOKEN_PIPELINE] SessionTab: forwarding tokens_updated to worker panel', { sessionId: effectiveSessionId })
          onWorkerEventRef.current?.(effectiveSessionId, msg)
        }
        break

      case 'context_updated':
        if (msg.session_id && currentSessionIdRef.current && msg.session_id !== currentSessionIdRef.current) {
          console.warn('[SessionTab] context_updated for different session, ignoring:', msg.session_id)
          break
        }
        console.log('[TOKEN_PIPELINE] SessionTab: context_updated arrived', { context_length: msg.context_length, source: msg.source, worker_name: msg.worker_name })
        useStore.getState().updateContextLength(msg.session_id || currentSessionIdRef.current, msg.context_length ?? 0)
        // Forward to worker panel if this is a worker-sourced context update
        // IMPORTANT: Use currentSessionIdRef to avoid stale closure (connectSessionWs has [] deps)
        if (msg.source === 'worker') {
          const effectiveSessionId = currentSessionIdRef.current || sessionId
          console.log('[TOKEN_PIPELINE] SessionTab: forwarding context_updated to worker panel', { sessionId: effectiveSessionId, worker_name: msg.worker_name })
          onWorkerEventRef.current?.(effectiveSessionId, msg)
        }
        break

      case 'conversation_changed':
        if (msg.session_id && currentSessionIdRef.current && msg.session_id !== currentSessionIdRef.current) {
          console.warn('[SessionTab] conversation_changed for different session, ignoring:', msg.session_id)
          break
        }
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
        // TODO: Backend dedup tracked for cleanup sprint — the backend re-emits
        // "[SYSTEM NOTIFICATION] Configuration updated: provider_id=..." on every
        // query when a pending config is re-applied, even when the provider is
        // unchanged from what this tab already displays.  Suppress the
        // user-visible bubble here; proper dedup belongs on the backend.
        const activeProvider = configRef.current?.provider || configRef.current?.provider_id
        const visibleMessages = mergedMessages.filter((m) => {
          if (!m.is_system_notification) return true
          const text = m.content || ''
          if (!text.startsWith('[SYSTEM NOTIFICATION] Configuration updated')) return true
          if (!activeProvider) return true
          const provMatch = text.match(/(?:provider_id|provider)=([^\s,]+)/)
          return !(provMatch && provMatch[1] === String(activeProvider))
        })
        const notes = visibleMessages.filter(m => m.is_system_notification);
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
        useStore.getState().receiveConversationChanged(msg.session_id || currentSessionIdRef.current, visibleMessages)
        // Pagination metadata from the server
        if (msg.total_count !== undefined) {
          setTotalMessages(msg.total_count)
        }
        setHasMore(msg.has_more === true)
        break

      case 'more_messages':
        if (msg.session_id && currentSessionIdRef.current && msg.session_id !== currentSessionIdRef.current) {
          console.warn('[SessionTab] more_messages for different session, ignoring:', msg.session_id)
          break
        }
        // Prepend older messages to the current history
        console.log('[SessionTab] more_messages:', msg.messages?.length, 'messages, offset:', msg.offset, 'has_more:', msg.has_more)
        const olderMessages = (msg.messages ?? []).map((m) => ({
          ...m,
          is_system_notification: m.is_system_notification || false,
        }))
        {
          const key = msg.session_id || currentSessionIdRef.current
          const prevHistory = useStore.getState().sessionMessages[key] || []
          useStore.getState().receiveConversationChanged(key, [...olderMessages, ...prevHistory])
        }
        setHasMore(msg.has_more === true)
        break

      case 'config_changed':
        // Defensive guard: apply_config must NEVER trigger tab creation or
        // session switching.  If a config_changed event ever carries a
        // session_id for a DIFFERENT session, ignore it here — tab management
        // is driven exclusively by new_session / session_loaded.
        if (msg.session_id && currentSessionIdRef.current && msg.session_id !== currentSessionIdRef.current) {
          console.warn('[SessionTab] config_changed for different session, ignoring:', msg.session_id)
          break
        }
        // Replace config entirely with what the backend sends.
        // The backend is now the single source of truth; it always sends
        // a complete frontend-format config (including tools, provider, etc.).
        useStore.getState().receiveConfigChanged(msg.session_id || currentSessionIdRef.current, msg)
        // Re-fetch available tools if the mode changed (e.g. session loaded
        // from disk where mode wasn't known at initial WS connection time).
        if (msg.config?.mode && msg.config.mode !== (availableToolsModeRef.current || 'custom')) {
          availableToolsModeRef.current = msg.config.mode
          sendCommand('get_available_tools', { mode: msg.config.mode })
        }
        break

      case 'rebuild_result':
        if (msg.session_id && currentSessionIdRef.current && msg.session_id !== currentSessionIdRef.current) {
          console.warn('[SessionTab] rebuild_result for different session, ignoring:', msg.session_id)
          break
        }
        setContainerRebuildResult({ status: msg.status, buildLog: msg.build_log })
        break

      case 'status_message':
        if (msg.session_id && currentSessionIdRef.current && msg.session_id !== currentSessionIdRef.current) {
          console.warn('[SessionTab] status_message for different session, ignoring:', msg.session_id)
          break
        }
        {
          const key = msg.session_id || currentSessionIdRef.current
          const prevHistory = useStore.getState().sessionMessages[key] || []
          useStore.getState().receiveConversationChanged(key, [
            ...prevHistory,
            { role: 'system', content: msg.text ?? '', is_system_notification: true },
          ])
        }
        break

      case 'session_loaded':
        dataReceivedRef.current = true;
        if (msg.workspace_id) {
          // Session metadata — no dedicated store slice yet (kept local).
          setWorkspaceId(msg.workspace_id)
        }
        if (msg.session_id) {
          const expectedSessionId = currentSessionIdRef.current
          // Fix 3b: stale-session detection. After a backend restart, old
          // session ids are invalid: load_session on a dead id makes the
          // backend create a REPLACEMENT session and reply session_loaded with
          // a DIFFERENT session_id. That must never silently rebind this tab to
          // another session's data — show a recovery banner instead.
          // A fresh tab (currentSessionIdRef null/undefined) accepts any
          // session id (normal new-session creation).
          if (expectedSessionId && expectedSessionId !== msg.session_id) {
            console.warn('[SessionTab] STALE SESSION: expected', expectedSessionId, 'got', msg.session_id, '— backend may have restarted')
            // Fix 4c: purge the dead session's store slices (config, messages,
            // state, mode, running state, sessions list, ...) so none of its
            // data can leak into the replacement session.
            useStore.getState().removeSession(expectedSessionId)
            staleSessionRef.current = true
            setStaleSession(true)
            setSessionReady(false)
            // Stash the replacement so 'Start New Session' can adopt it via the
            // same path a fresh tab uses (register + receiveSessionLoaded +
            // setCurrentSessionId + onNewSession).
            pendingAdoptRef.current = msg
            useStore.getState().setSessionError(
              expectedSessionId,
              'This session is no longer available (backend may have restarted).'
            )
            break
          }
          // Normal path: fresh tab or confirmed same session.
          pendingAdoptRef.current = null
          useStore.getState().registerSession(msg.session_id)
          useStore.getState().receiveSessionLoaded(msg.session_id, msg)
          // F2: the onopen-time get_providers reply may have landed before
          // session_loaded (routed under a null/stale key because the backend
          // reply had no session_id yet). If the freshly loaded session has no
          // providers cached, re-request so the reply carries the real
          // session_id and the provider dropdown populates.
          {
            const loadedConfig = useStore.getState().sessionConfigs[msg.session_id]
            const loadedProviders = loadedConfig?.providers
            if (!loadedProviders || loadedProviders.length === 0) {
              sendCommand('get_providers')
            }
          }
          setCurrentSessionId(msg.session_id)
          if (msg.session_name) useStore.getState().updateSessionName(msg.session_id, msg.session_name)
          setSessionReady(true)
          // Notify parent that this tab now has a real sessionId
          if (!sessionId) {
            onNewSession?.(msg.session_id, msg.session_name)
          }
        }
        break

      case 'providers_list':
        if (msg.session_id && currentSessionIdRef.current && msg.session_id !== currentSessionIdRef.current) {
          console.warn('[SessionTab] providers_list for different session, ignoring:', msg.session_id)
          break
        }
        useStore.getState().receiveProvidersList(msg.session_id || currentSessionIdRef.current, msg.providers || [])
        break

      case 'provider_saved':
        if (msg.session_id && currentSessionIdRef.current && msg.session_id !== currentSessionIdRef.current) {
          console.warn('[SessionTab] provider_saved for different session, ignoring:', msg.session_id)
          break
        }
        console.log('[SessionTab] Provider saved:', msg.provider?.id)
        // Refresh providers from the backend (it will push a fresh providers_list)
        sendCommand('get_providers')
        break

      case 'provider_deleted':
        if (msg.session_id && currentSessionIdRef.current && msg.session_id !== currentSessionIdRef.current) {
          console.warn('[SessionTab] provider_deleted for different session, ignoring:', msg.session_id)
          break
        }
        console.log('[SessionTab] Provider deleted:', msg.provider_id)
        break

      case 'tools_list':
        if (msg.session_id && currentSessionIdRef.current && msg.session_id !== currentSessionIdRef.current) {
          console.warn('[SessionTab] tools_list for different session, ignoring:', msg.session_id)
          break
        }
        useStore.getState().receiveToolsList(msg.session_id || currentSessionIdRef.current, msg.tools || [])
        break

      case 'default_config_saved':
        if (msg.session_id && currentSessionIdRef.current && msg.session_id !== currentSessionIdRef.current) {
          console.warn('[SessionTab] default_config_saved for different session, ignoring:', msg.session_id)
          break
        }
        setDefaultConfigSaveStatus(msg.status)
        break

      case 'session_saved':
        if (msg.session_id && currentSessionIdRef.current && msg.session_id !== currentSessionIdRef.current) {
          console.warn('[SessionTab] session_saved for different session, ignoring:', msg.session_id)
          break
        }
        onSessionSaved?.()
        break

      case 'session_renamed':
        if (msg.session_id && currentSessionIdRef.current && msg.session_id !== currentSessionIdRef.current) {
          console.warn('[SessionTab] session_renamed for different session, ignoring:', msg.session_id)
          break
        }
        // The session was renamed; update our currentSessionId if needed
        if (msg.session_id) {
          setCurrentSessionId(msg.session_id)
        }
        if (msg.new_name) useStore.getState().updateSessionName(msg.session_id, msg.new_name)
        onSessionRenamed?.(msg.session_id, msg.new_name)
        break

      case 'session_closed':
        if (msg.session_id && currentSessionIdRef.current && msg.session_id !== currentSessionIdRef.current) {
          console.warn('[SessionTab] session_closed for different session, ignoring:', msg.session_id)
          break
        }
        closedRef.current = true
        setSessionReady(false)
        onClose?.()
        break

      case 'session_deleted':
        if (msg.session_id && currentSessionIdRef.current && msg.session_id !== currentSessionIdRef.current) {
          console.warn('[SessionTab] session_deleted for different session, ignoring:', msg.session_id)
          break
        }
        // Session was deleted from the store — close the tab
        setSessionReady(false)
        onClose?.()
        break

      case 'security_prompt':
        if (msg.session_id && currentSessionIdRef.current && msg.session_id !== currentSessionIdRef.current) {
          console.warn('[SessionTab] security_prompt for different session, ignoring:', msg.session_id)
          break
        }
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
        if (msg.session_id && currentSessionIdRef.current && msg.session_id !== currentSessionIdRef.current) {
          console.warn('[SessionTab] worker event for different session, ignoring:', msg.session_id)
          break
        }
        // For worker:tokens_updated, also update the token counter display
        // (belt-and-suspenders — the bridge should flatten to 'tokens_updated',
        //  but if it doesn't, this ensures the counter still updates).
        if (msg.type === 'worker:tokens_updated') {
          useStore.getState().receiveTokensUpdated(msg.session_id || currentSessionIdRef.current, {
            input: msg.data?.total_input ?? 0,
            output: msg.data?.total_output ?? 0,
          })
        }
        // Use refs to avoid stale closure (connectSessionWs has [] deps)
        if (onWorkerEventRef.current) {
          onWorkerEventRef.current(currentSessionIdRef.current || currentSessionId, msg)
        }
        break

      case 'logging_config_changed':
        if (msg.session_id && currentSessionIdRef.current && msg.session_id !== currentSessionIdRef.current) {
          console.warn('[SessionTab] logging_config_changed for different session, ignoring:', msg.session_id)
          break
        }
        if (onLoggingConfigChangedRef.current) {
          onLoggingConfigChangedRef.current(msg.config)
        }
        break

      case 'error':
        // bridge.py broadcasts {"type":"error","error_type":...,"message":...,"traceback":...}
        // via EventForwarder, which does NOT inject session_id into the payload.
        if (msg.session_id && currentSessionIdRef.current && msg.session_id !== currentSessionIdRef.current) {
          console.warn('[SessionTab] error for different session, ignoring:', msg.session_id)
          break
        }
        useStore.getState().setSessionError(msg.session_id || currentSessionIdRef.current, msg.error || msg.message || 'Unknown error')
        break

      case 'session_stop':
        // Defensive: the bridge normally folds session_stop into state_changed
        // + conversation_changed and never forwards the raw type, but handle it
        // here so the tab can never stay stuck in RUNNING. Only surface a
        // banner when the stop was abnormal (not 'completed'/'ok').
        useStore.getState().receiveStateChanged(msg.session_id || currentSessionIdRef.current, 'IDLE')
        if (msg.stop_reason && msg.stop_reason !== 'completed' && msg.stop_reason !== 'ok') {
          useStore.getState().setSessionError(msg.session_id || currentSessionIdRef.current, `Session stopped: ${msg.stop_reason}`)
        }
        break

      case 'session_cleared':
        // bridge.py broadcasts session_cleared ({} — no session_id) on
        // close_session; the conversation is gone, so drop it and any error.
        useStore.getState().receiveConversationChanged(msg.session_id || currentSessionIdRef.current, [])
        useStore.getState().clearSessionError(msg.session_id || currentSessionIdRef.current)
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
    const offset = history.length
    sendCommand('load_more_messages', { offset, limit: 20 })
  }, [history.length, sendCommand])

  // ── Start New Session (stale-session recovery) ─────────────────────────────
  // Fix 3b: adopt the replacement session the backend created for the dead id
  // (stashed by the stale branch of session_loaded) using the exact acceptance
  // sequence a fresh tab uses, then notify App via the onNewSession prop (same
  // mechanism as normal new-session creation).
  const startNewSession = useCallback(() => {
    staleSessionRef.current = false
    setStaleSession(false)
    const pending = pendingAdoptRef.current
    pendingAdoptRef.current = null
    if (!pending?.session_id) {
      console.warn('[SessionTab] startNewSession: no stashed replacement session')
      return
    }
    if (pending.workspace_id) setWorkspaceId(pending.workspace_id)
    useStore.getState().registerSession(pending.session_id)
    useStore.getState().receiveSessionLoaded(pending.session_id, pending)
    setCurrentSessionId(pending.session_id)
    if (pending.session_name) useStore.getState().updateSessionName(pending.session_id, pending.session_name)
    setSessionReady(true)
    useStore.getState().clearSessionError(storeKey)
    onNewSession?.(pending.session_id, pending.session_name)
  }, [storeKey, onNewSession])

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
              defaultValue={sessionName}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  const newName = e.target.value.trim()
                  if (newName && newName !== sessionName) {
                    sendCommand('rename_session', { session_id: currentSessionId, new_name: newName })
                    useStore.getState().updateSessionName(currentSessionId, newName)
                    onSessionRenamed?.(currentSessionId, newName)
                  }
                  setIsRenaming(false)
                } else if (e.key === 'Escape') {
                  setIsRenaming(false)
                }
              }}
              onBlur={(e) => {
                const newName = e.target.value.trim()
                if (newName && newName !== sessionName) {
                  sendCommand('rename_session', { session_id: currentSessionId, new_name: newName })
                  useStore.getState().updateSessionName(currentSessionId, newName)
                  onSessionRenamed?.(currentSessionId, newName)
                }
                setIsRenaming(false)
              }}
              autoFocus
            />
          ) : (
            <>
              <span className="session-header-name">{sessionName || 'Untitled'}</span>
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
      {sessionError ? (
        <div className="session-error-banner" role="alert">
          <span className="session-error-banner-text">⚠ {sessionError}</span>
          {staleSession ? (
            <button
              className="session-error-banner-action"
              onClick={startNewSession}
            >Start New Session</button>
          ) : (
            <button
              className="session-error-banner-dismiss"
              onClick={() => useStore.getState().clearSessionError(storeKey)}
              title="Dismiss"
            >✕</button>
          )}
        </div>
      ) : null}
      <StatusBar
        status={status}
        tokensIn={tokensIn}
        tokensOut={tokensOut}
        contextLength={contextLength}
      />
      <div className="app-main">
        <ConfigPanel
          mode={mode}
          config={config}
          sendCommand={sendCommand}
          providers={providers}
          availableTools={availableTools}
          panelWidth={configPanelWidth}
          wsConnected={wsConnected}
          workspaceId={workspaceId}
          sessionId={currentSessionId}
          defaultConfigSaveStatus={defaultConfigSaveStatus}
          onClearDefaultSaveStatus={() => {
            setDefaultConfigSaveStatus(null)
          }}
          containerRebuildResult={containerRebuildResult}
          onClearRebuildResult={() => setContainerRebuildResult(null)}
          selectedWorker={selectedWorker}
          onSelectWorker={onSelectWorker}
          isActive={isActive}
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
          <ChatPanel messages={history} loadMore={loadMore} hasMore={hasMore} scrollToBottomKey={scrollToBottomKey} />
          <QueryBar
            sendCommand={sendCommand}
            status={status}
            isRunning={isRunning}
            config={config}
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
