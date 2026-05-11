/*
 * Zustand store — the single source of truth for all UI state.
 *
 * State shape:
 *   session   { status, history, tokensIn, tokensOut, contextLength }
 *   config    { temperature, max_turns, provider, tools }
 *   securityQueue  (placeholder)
 *
 * All actions are thin wrappers around set().
 * The WebSocket handler in App.jsx calls these actions when events arrive.
 */

import { create } from 'zustand'

// ────────────────────────────────────────────────────────────────────────────
// Initial state
// ────────────────────────────────────────────────────────────────────────────
const initialState = {
  session: {
    status: 'IDLE',           // IDLE | RUNNING | PAUSED | WAITING_FOR_USER
    history: [],              // messages: { role, content }
    tokensIn: 0,
    tokensOut: 0,
    contextLength: 0,
    isRunning: false,         // true = agent thread alive, can continue_session
  },
  config: {
    temperature: 0.7,
    max_turns: 20,
    provider: 'openai',
    tools: [
      { name: 'bash', enabled: true },
      { name: 'file_read', enabled: false },
    ],
  },
  securityQueue: [],
}

// ────────────────────────────────────────────────────────────────────────────
// Store creation
// ────────────────────────────────────────────────────────────────────────────
const useStore = create((set) => ({
  ...initialState,

  setStatus: (status) =>
    set((s) => ({ session: { ...s.session, status } })),

  setRunning: (isRunning) =>
    set((s) => ({ session: { ...s.session, isRunning } })),

  setHistory: (history) =>
    set((s) => ({ session: { ...s.session, history } })),

  setTokens: (tokensIn, tokensOut) =>
    set((s) => ({ session: { ...s.session, tokensIn, tokensOut } })),

  setContextLength: (contextLength) =>
    set((s) => ({ session: { ...s.session, contextLength } })),

  setConfig: (config) => set({ config }),

  addStatusMessage: (text) =>
    set((s) => ({
      session: {
        ...s.session,
        history: [...s.session.history, { role: 'system', content: text }],
      },
    })),

  reset: () => set({ ...initialState }),
}))

export default useStore
