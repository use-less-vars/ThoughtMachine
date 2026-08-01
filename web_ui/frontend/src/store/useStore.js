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

const initialState = {
  sessions: [],            // list of { session_id, name, created_at, updated_at, preview }
  sessionModes: {},        // { [sessionId]: 'agent' | 'engineer' | 'custom' }
  tabRunningStates: {},    // { [sessionId]: status string ('RUNNING' | 'PAUSED' | ...) }
}

const useStore = create((set) => ({
  ...initialState,

  setSessions: (sessions) => set({ sessions }),

  setSessionMode: (sessionId, mode) =>
    set((state) => ({ sessionModes: { ...state.sessionModes, [sessionId]: mode } })),

  setTabRunningState: (sessionId, status) =>
    set((state) => ({ tabRunningStates: { ...state.tabRunningStates, [sessionId]: status } })),

  removeSessionState: (sessionId) =>
    set((state) => {
      // Destructure-rest: drop both slices for a sessionId that is going away.
      const { [sessionId]: _removedMode, ...sessionModes } = state.sessionModes
      const { [sessionId]: _removedRunning, ...tabRunningStates } = state.tabRunningStates
      return { sessionModes, tabRunningStates }
    }),

  reset: () => set({ ...initialState }),
}))

export default useStore
