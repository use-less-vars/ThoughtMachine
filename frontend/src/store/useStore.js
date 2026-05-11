/*
 * useStore.js — Zustand store
 *
 * After the multi-tab refactoring, the store only holds data that is
 * shared across all tabs: the sessions list.
 *
 * Each SessionTab manages its own local state (status, history, tokens,
 * config, etc.) via useState, since those are per-tab concerns.
 */

import { create } from 'zustand'

const initialState = {
  sessions: [],  // list of { session_id, name, created_at, updated_at, preview }
}

const useStore = create((set) => ({
  ...initialState,

  setSessions: (sessions) => set({ sessions }),

  reset: () => set({ ...initialState }),
}))

export default useStore
