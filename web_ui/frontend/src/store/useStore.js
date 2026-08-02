/*
 * useStore.js — Zustand store
 *
 * After the multi-tab refactoring, the store holds data that is shared
 * across all tabs:
 *   - sessions: the sessions list
 *   - sessionModes: per-session mode ('agent' | 'engineer' | 'custom'),
 *     written by App handlers and read by SessionTab
 *   - tabRunningStates: per-session running status string
 *     ('RUNNING' | 'PAUSED' | 'WAITING_FOR_USER' | ...), written by
 *     SessionTab and consumed (via a tabId-keyed map) by TabBar.
 *   - workerEvents: per-session worker event log
 *     ({ [sessionId]: [{ type, timestamp, ... }] }), written via
 *     SessionTab's onWorkerEvent and capped at 500 events/session.
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
 * Each SessionTab manages its own local state (status, history, tokens,
 * config, etc.) via useState, since those are per-tab concerns.
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

// Canonical worker event types: these dedup against each other when they share
// a timestamp, so a final_response supersedes the worker_message /
// assistant_message emitted for the same step.
const CANONICAL_WORKER_EVENT_TYPES = new Set(['final_response', 'worker_message', 'assistant_message'])

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
  workerEvents: {},        // { [sessionId]: [{ type, timestamp, ... }] }
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

  addWorkerEvent: (sessionId, evt) =>
    set((state) => {
      const existing = state.workerEvents[sessionId] || []
      let events
      if (CANONICAL_WORKER_EVENT_TYPES.has(evt.type) && existing.some((e) => e.timestamp === evt.timestamp)) {
        // Dedup: a canonical event supersedes an earlier event with the same
        // timestamp (e.g. final_response replaces the worker_message for a step).
        events = existing.map((e) => (e.timestamp === evt.timestamp ? evt : e))
      } else {
        events = [...existing, evt]
      }
      // Cap each session at 500 events, dropping the oldest.
      if (events.length > 500) {
        events = events.slice(events.length - 500)
      }
      return { workerEvents: { ...state.workerEvents, [sessionId]: events } }
    }),

  clearWorkerEvents: (sessionId) =>
    set((state) => ({ workerEvents: { ...state.workerEvents, [sessionId]: [] } })),

  registerSession: (sessionId) =>
    set((state) => {
      // Create per-session entries if missing; never overwrite existing data.
      const config = state.sessionConfigs[sessionId] || DEFAULT_SESSION_CONFIG
      const messages = state.sessionMessages[sessionId] || []
      const runtimeState = state.sessionStates[sessionId] || DEFAULT_SESSION_STATE
      const events = state.workerEvents[sessionId] || []
      return {
        sessionConfigs: { ...state.sessionConfigs, [sessionId]: config },
        sessionMessages: { ...state.sessionMessages, [sessionId]: messages },
        sessionStates: { ...state.sessionStates, [sessionId]: runtimeState },
        workerEvents: { ...state.workerEvents, [sessionId]: events },
      }
    }),

  removeSession: (sessionId) =>
    set((state) => {
      // Destructure-rest: drop the session's entries from all per-session slices.
      const { [sessionId]: _removedConfig, ...sessionConfigs } = state.sessionConfigs
      const { [sessionId]: _removedMessages, ...sessionMessages } = state.sessionMessages
      const { [sessionId]: _removedStates, ...sessionStates } = state.sessionStates
      const { [sessionId]: _removedEvents, ...workerEvents } = state.workerEvents
      const { [sessionId]: _removedErrors, ...sessionErrors } = state.sessionErrors
      return { sessionConfigs, sessionMessages, sessionStates, workerEvents, sessionErrors }
    }),

  receiveSessionLoaded: (sessionId, payload) =>
    set((state) => ({
      // session_loaded carries metadata (session_id/name/workspace); config /
      // permissions / providers / tools arrive via their own events, so default
      // any missing fields here.
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
      const { [sessionId]: _removedEvents, ...workerEvents } = state.workerEvents
      return { sessionModes, tabRunningStates, workerEvents }
    }),

  reset: () => set({ ...initialState }),
}))

export default useStore
