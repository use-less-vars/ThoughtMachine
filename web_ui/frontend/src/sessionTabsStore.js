/*
 * sessionTabsStore.js
 *
 * Per-workspace session tab strip state.
 *
 * byWorkspace[workspaceId] = { tabs: [{ sessionId, title }], activeSessionId }
 *
 * Persisted to localStorage under `tm.sessionTabs.<workspaceId>` as
 * { v: 1, tabs: [{ sessionId, title }], activeSessionId } on every mutation.
 * hydrate(ws) loads the persisted entry back into memory (validated).
 *
 * The strip is frontend-only state: closing a tab here does NOT close the
 * server-side session (no removeSession call). Server deletions arrive via
 * the hub `session_deleted` event and are handled in App.jsx.
 */
import { create } from 'zustand'

const STORAGE_PREFIX = 'tm.sessionTabs.'

function loadEntry(ws) {
  try {
    const raw = localStorage.getItem(STORAGE_PREFIX + ws)
    if (!raw) return null
    const parsed = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object') return null
    return parsed
  } catch {
    return null
  }
}

function saveEntry(ws, entry) {
  try {
    localStorage.setItem(STORAGE_PREFIX + ws, JSON.stringify({ v: 1, ...entry }))
  } catch {
    // ignore quota / security errors — memory state still works this session
  }
}

// Validate a persisted entry: drop falsy sessionIds, coerce shapes, and make
// sure activeSessionId actually points at one of the tabs (fallback: first).
function sanitize(entry) {
  const rawTabs = Array.isArray(entry?.tabs) ? entry.tabs : []
  const tabs = rawTabs
    .filter((t) => t && typeof t === 'object' && t.sessionId)
    .map((t) => ({ sessionId: String(t.sessionId), title: typeof t.title === 'string' ? t.title : '' }))
  let activeSessionId = entry?.activeSessionId || null
  if (activeSessionId && !tabs.some((t) => t.sessionId === activeSessionId)) {
    activeSessionId = tabs.length > 0 ? tabs[0].sessionId : null
  }
  return { tabs, activeSessionId }
}

const initialState = {
  byWorkspace: {},
}

const useSessionTabsStore = create((set, get) => ({
  ...initialState,

  // Add a tab for a session. No-ops on duplicates. A workspace's FIRST tab
  // becomes active automatically; later tabs are added lazily (strip only).
  openTab: (ws, { sessionId, title }) => {
    if (!ws || !sessionId) return
    const entry = get().byWorkspace[ws] || { tabs: [], activeSessionId: null }
    if (entry.tabs.some((t) => t.sessionId === sessionId)) return
    const tabs = [...entry.tabs, { sessionId, title: title || '' }]
    const activeSessionId = entry.activeSessionId || sessionId
    const next = { ...get().byWorkspace, [ws]: { tabs, activeSessionId } }
    set({ byWorkspace: next })
    saveEntry(ws, next[ws])
  },

  // Close a tab (frontend-only; the session stays open server-side).
  // If the active tab is closed, the neighbor is activated: prefer the tab
  // that took its place (next), else the previous one, else null.
  closeTab: (ws, sessionId) => {
    if (!ws || !sessionId) return
    const entry = get().byWorkspace[ws]
    if (!entry) return
    const idx = entry.tabs.findIndex((t) => t.sessionId === sessionId)
    if (idx === -1) return
    const tabs = entry.tabs.filter((t) => t.sessionId !== sessionId)
    let activeSessionId = entry.activeSessionId
    if (activeSessionId === sessionId) {
      const neighbor = tabs[idx] || tabs[idx - 1] || null
      activeSessionId = neighbor ? neighbor.sessionId : null
    }
    const next = { ...get().byWorkspace, [ws]: { tabs, activeSessionId } }
    set({ byWorkspace: next })
    saveEntry(ws, next[ws])
  },

  setActiveTab: (ws, sessionId) => {
    if (!ws || !sessionId) return
    const entry = get().byWorkspace[ws]
    if (!entry || !entry.tabs.some((t) => t.sessionId === sessionId)) return
    const next = { ...get().byWorkspace, [ws]: { ...entry, activeSessionId: sessionId } }
    set({ byWorkspace: next })
    saveEntry(ws, next[ws])
  },

  setTabTitle: (ws, sessionId, title) => {
    if (!ws || !sessionId) return
    const entry = get().byWorkspace[ws]
    if (!entry) return
    const tabs = entry.tabs.map((t) => (t.sessionId === sessionId ? { ...t, title: title || '' } : t))
    const next = { ...get().byWorkspace, [ws]: { ...entry, tabs } }
    set({ byWorkspace: next })
    saveEntry(ws, next[ws])
  },

  // Load the persisted entry for a workspace into memory (validated).
  // Idempotent: every mutation persists, so an in-memory entry is always
  // at least as fresh as localStorage.
  hydrate: (ws) => {
    if (!ws) return { tabs: [], activeSessionId: null }
    const existing = get().byWorkspace[ws]
    if (existing) return existing
    const loaded = sanitize(loadEntry(ws))
    set({ byWorkspace: { ...get().byWorkspace, [ws]: loaded } })
    return loaded
  },

  reset: () => set({ ...initialState }),
}))

export default useSessionTabsStore
