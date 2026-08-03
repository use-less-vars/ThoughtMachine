/*
 * useStore.js — Zustand store
 *
 * The store is the single source of truth for all per-session state. Each
 * session's { name, status, history, tokens, config, permissions, error }
 * lives in the slices below, so every consumer (SessionTab, TabBar, header,
 * sidebar) reads the same data and stays consistent:
 *   - sessions: the sessions list
 *   - sessionModes: per-session mode ('agent' | 'engineer' | 'custom'),
 *     written by App handlers and read by SessionTab
 *   - tabRunningStates: per-session running status string
 *     ('RUNNING' | 'PAUSED' | 'WAITING_FOR_USER' | ...), written by
 *     SessionTab and consumed (via a tabId-keyed map) by TabBar.
 *   - sessionConfigs: per-session config snapshot
 *     ({ [sessionId]: { config, permissions, providers, tools, isLoaded } })
 *   - sessionMessages: per-session conversation
 *     ({ [sessionId]: [messages] })
 *   - sessionStates: per-session runtime state
 *     ({ [sessionId]: { isRunning, state, contextLength, tokensIn, tokensOut } })
 *   - sessionErrors: per-session last error message string
 *     ({ [sessionId]: '...' }), written by SessionTab on 'error' / abnormal
 *     'session_stop' events and cleared on dismiss or session close.
 *
 * SessionTab does not keep status/history/tokens/config in local useState —
 * it subscribes via useStore selectors, and the WS event handlers in this
 * file (receiveSessionLoaded, receiveConfigChanged, receiveConversationChanged,
 * receiveStateChanged, receiveTokensUpdated, updateSessionName, the
 * sessionErrors slice, ...) mutate the store as events arrive.
 */

import { create } from 'zustand'

export const PERMISSION_DEFAULTS = {
  filesystem: 'read',
  network: 'banned',
  container: false,
  system: 'read',
  git: 'read',
  execution: 'banned',
}

// Default per-session entries created by registerSession / receive* actions.
const DEFAULT_SESSION_CONFIG = { config: null, permissions: null, providers: [], tools: [], isLoaded: false }
const DEFAULT_SESSION_STATE = { isRunning: false, state: null, contextLength: 0, tokensIn: 0, tokensOut: 0 }

// Normalize a tools list to plain names: string entries stay as-is,
// object entries (e.g. { name, enabled }) become their name.
function normalizeTools(tools) {
  return (tools || []).map((t) => (typeof t === 'string' ? t : t?.name ?? ''))
}

const initialState = {
  sessions: [],            // list of { session_id, name, created_at, updated_at, preview }
  sessionModes: {},        // { [sessionId]: 'agent' | 'engineer' | 'custom' }
  tabRunningStates: {},    // { [sessionId]: status string ('RUNNING' | 'PAUSED' | ...) }
  sessionConfigs: {},      // { [sessionId]: { config, permissions, providers, tools, isLoaded } }
  sessionMessages: {},     // { [sessionId]: [messages] }
  sessionStates: {},       // { [sessionId]: { isRunning, state, contextLength, tokensIn, tokensOut } }
  sessionErrors: {},        // { [sessionId]: last error message string }
}

const useStore = create((set) => ({
  ...initialState,

  setSessions: (sessions) => set({ sessions }),

  // Upsert a session's display name into the sessions list (the single source
  // of truth for names). Called on session_loaded / session_renamed / optimistic
  // rename so the header, TabBar and sidebar stay consistent immediately.
  updateSessionName: (sessionId, name) =>
    set((state) => {
      const exists = state.sessions.some((s) => s.session_id === sessionId)
      if (exists) {
        return { sessions: state.sessions.map((s) => (s.session_id === sessionId ? { ...s, name } : s)) }
      }
      return { sessions: [...state.sessions, { session_id: sessionId, name }] }
    }),

  setSessionMode: (sessionId, mode) =>
    set((state) => ({ sessionModes: { ...state.sessionModes, [sessionId]: mode } })),

  setTabRunningState: (sessionId, status) =>
    set((state) => ({ tabRunningStates: { ...state.tabRunningStates, [sessionId]: status } })),

  registerSession: (sessionId) =>
    set((state) => {
      // Create per-session entries if missing; never overwrite existing data.
      const config = state.sessionConfigs[sessionId] || DEFAULT_SESSION_CONFIG
      const messages = state.sessionMessages[sessionId] || []
      const runtimeState = state.sessionStates[sessionId] || DEFAULT_SESSION_STATE
      return {
        sessionConfigs: { ...state.sessionConfigs, [sessionId]: config },
        sessionMessages: { ...state.sessionMessages, [sessionId]: messages },
        sessionStates: { ...state.sessionStates, [sessionId]: runtimeState },
      }
    }),

  // Full purge — must touch all 7 slices defined in initialState.
  removeSession: (sessionId) =>
    set((state) => {
      // Destructure-rest: drop the session's entries from ALL per-session slices
      // (including the sessions list, sessionModes and tabRunningStates).
      const { [sessionId]: _removedConfig, ...sessionConfigs } = state.sessionConfigs
      const { [sessionId]: _removedMessages, ...sessionMessages } = state.sessionMessages
      const { [sessionId]: _removedStates, ...sessionStates } = state.sessionStates
      const { [sessionId]: _removedErrors, ...sessionErrors } = state.sessionErrors
      const { [sessionId]: _removedMode, ...sessionModes } = state.sessionModes
      const { [sessionId]: _removedRunning, ...tabRunningStates } = state.tabRunningStates
      return {
        sessionConfigs,
        sessionMessages,
        sessionStates,
        sessionErrors,
        sessionModes,
        tabRunningStates,
        sessions: state.sessions.filter((s) => s.session_id !== sessionId),
      }
    }),

  receiveSessionLoaded: (sessionId, payload) =>
    set((state) => ({
      // session_loaded is the authoritative snapshot: it carries metadata
      // (session_id/name/workspace) plus is_running so the session's runtime
      // state is set atomically on (re)load. Config / permissions / providers /
      // tools arrive via their own events, so default any missing fields here.
      sessionConfigs: {
        ...state.sessionConfigs,
        [sessionId]: {
          config: payload?.config ?? null,
          permissions: payload?.permissions ?? null,
          providers: payload?.providers || [],
          tools: normalizeTools(payload?.tools),
          isLoaded: true,
        },
      },
      // is_running from the payload is authoritative; a missing is_running
      // (older payloads) defaults to IDLE/False so nothing breaks.
      sessionStates: {
        ...state.sessionStates,
        [sessionId]: {
          ...(state.sessionStates[sessionId] || DEFAULT_SESSION_STATE),
          state: payload?.is_running ? 'RUNNING' : 'IDLE',
          isRunning: !!payload?.is_running,
        },
      },
    })),

  receiveConfigChanged: (sessionId, payload) =>
    set((state) => ({
      // REPLACE the whole entry (same shape as receiveSessionLoaded) — do not
      // merge with the previous config snapshot.
      sessionConfigs: {
        ...state.sessionConfigs,
        [sessionId]: {
          config: payload?.config ?? null,
          permissions: payload?.permissions ?? null,
          providers: payload?.providers || [],
          tools: normalizeTools(payload?.tools),
          isLoaded: true,
        },
      },
    })),

  receiveProvidersList: (sessionId, providers) =>
    set((state) => ({
      sessionConfigs: {
        ...state.sessionConfigs,
        [sessionId]: { ...(state.sessionConfigs[sessionId] || DEFAULT_SESSION_CONFIG), providers },
      },
    })),

  receiveToolsList: (sessionId, tools) =>
    set((state) => ({
      sessionConfigs: {
        ...state.sessionConfigs,
        [sessionId]: { ...(state.sessionConfigs[sessionId] || DEFAULT_SESSION_CONFIG), tools: normalizeTools(tools) },
      },
    })),

  receiveConversationChanged: (sessionId, messages) =>
    set((state) => ({ sessionMessages: { ...state.sessionMessages, [sessionId]: messages || [] } })),

  receiveStateChanged: (sessionId, newState) =>
    set((state) => ({
      sessionStates: {
        ...state.sessionStates,
        [sessionId]: {
          ...(state.sessionStates[sessionId] || DEFAULT_SESSION_STATE),
          isRunning: newState === 'RUNNING',
          state: newState,
        },
      },
    })),

  // Store the last error message for a session ("dead events → visible errors").
  // Written by SessionTab on 'error' events and abnormal 'session_stop' events;
  // read via s.sessionErrors[storeKey] to render the dismissible banner.
  setSessionError: (sessionId, message) =>
    set((state) => ({ sessionErrors: { ...state.sessionErrors, [sessionId]: message } })),

  clearSessionError: (sessionId) =>
    set((state) => {
      const { [sessionId]: _removedError, ...sessionErrors } = state.sessionErrors
      return { sessionErrors }
    }),

  updateContextLength: (sessionId, length) =>
    set((state) => ({
      sessionStates: {
        ...state.sessionStates,
        [sessionId]: { ...(state.sessionStates[sessionId] || DEFAULT_SESSION_STATE), contextLength: length },
      },
    })),

  receiveTokensUpdated: (sessionId, payload) =>
    set((state) => ({
      // tokens_updated payload carries { input, output } — FLAGGED EXTENSION:
      // tokensIn/tokensOut live in sessionStates so token info can be read from
      // the store instead of SessionTab local state.
      sessionStates: {
        ...state.sessionStates,
        [sessionId]: {
          ...(state.sessionStates[sessionId] || DEFAULT_SESSION_STATE),
          tokensIn: payload?.input ?? 0,
          tokensOut: payload?.output ?? 0,
        },
      },
    })),

  removeSessionState: (sessionId) =>
    set((state) => {
      // Destructure-rest: drop the per-session slices for a sessionId that is going away.
      const { [sessionId]: _removedMode, ...sessionModes } = state.sessionModes
      const { [sessionId]: _removedRunning, ...tabRunningStates } = state.tabRunningStates
      return { sessionModes, tabRunningStates }
    }),

  reset: () => set({ ...initialState }),
}))

export default useStore
