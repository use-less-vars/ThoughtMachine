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

const initialState = {
  sessions: [],            // list of { session_id, name, created_at, updated_at, preview }
  sessionModes: {},        // { [sessionId]: 'agent' | 'engineer' | 'custom' }
  tabRunningStates: {},    // { [sessionId]: status string ('RUNNING' | 'PAUSED' | ...) }
  workerEvents: {},        // { [sessionId]: [{ type, timestamp, ... }] }
}

const useStore = create((set) => ({
  ...initialState,

  setSessions: (sessions) => set({ sessions }),

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
