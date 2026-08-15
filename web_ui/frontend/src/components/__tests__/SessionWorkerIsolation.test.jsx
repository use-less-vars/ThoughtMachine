// @vitest-environment jsdom
/*
 * SessionWorkerIsolation.test.jsx
 *
 * R6b — Frontend Truthfulness Sprint: worker-event isolation between sessions.
 *
 * Verifies that worker lifecycle events streamed on a session's own WebSocket
 * (tagged with that session_id) reach ONLY that session's WorkerOutputPanel:
 *   (a) events on wsA (sess-1) render in A's panel; switching to S2 resets the
 *       panel (no leak of A's events), and B's panel starts empty.
 *   (b) a worker:* event tagged with a DIFFERENT session_id arriving on wsA is
 *       dropped by SessionTab's session-mismatch filter — A's panel is
 *       unaffected and nothing crashes.
 *   (c) events stay per-session across tab switches + WS remount: B's panel
 *       shows only B's worker events, and after switching back to S1 (fresh WS)
 *       A's panel shows only A's worker events.
 *
 * Worker selection is driven through the real UI: WorkerManagementPanel (inside
 * ConfigPanel → WorkspacePanel, mounted by every SessionTab) fetches
 * `/api/workspace/{id}/workers` and renders clickable worker rows; clicking a
 * row calls onSelectWorker(workerName, workspaceId), which is the ONLY way
 * App's workerPanelState gets populated (preseeding localStorage does NOT work:
 * App's mount-time stale-key cleanup wipes workerPanelState while tabs=[]).
 *
 * NOTE: worker events MUST use 'worker:'-prefixed types ('worker:worker_spawned'
 * etc.) — bare 'worker_spawned' hits SessionTab's default case and is dropped.
 * Events MUST carry distinct timestamps (App dedup key = canonicalType|timestamp).
 *
 * STATUS: EXECUTED AND PASSING — 3/3 tests green via
 *   `npx vitest run src/components/__tests__/SessionWorkerIsolation.test.jsx --maxWorkers=1`
 */

import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor, act, within } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import App from '../../App'
import useStore from '../../store/useStore'
import useWorkspaceStore from '../../store/workspaceStore'
import useSessionTabsStore from '../../sessionTabsStore'

// ── Test doubles (same pattern as SessionTabsIntegration.test.jsx) ─────────
class MockWebSocket {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3
  static instances = []
  constructor(url) {
    this.url = url
    this.readyState = MockWebSocket.CONNECTING
    this.sent = []
    this.onopen = null
    this.onmessage = null
    this.onclose = null
    this.onerror = null
    MockWebSocket.instances.push(this)
  }
  send(data) { this.sent.push(data) }
  close(code = 1001) {
    this.readyState = MockWebSocket.CLOSED
    this.onclose?.({ code })
  }
  open() {
    this.readyState = MockWebSocket.OPEN
    this.onopen?.({})
  }
  receive(msg) {
    this.onmessage?.({ data: JSON.stringify(msg) })
  }
}

class MockResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const jsonOk = (data, status = 200) => ({
  ok: true,
  status,
  json: async () => data,
  text: async () => JSON.stringify(data),
})

const DEFAULT_FALLBACK = { ok: true, status: 200, json: async () => ({}), text: async () => '' }

function stubFetchByUrl(routes, defaultResponse = DEFAULT_FALLBACK) {
  const keys = Object.keys(routes)
  const fetchMock = vi.fn(async (url, opts) => {
    const match = keys
      .filter((k) => String(url).startsWith(k))
      .sort((a, b) => b.length - a.length)[0]
    if (!match) return defaultResponse
    const value = routes[match]
    return typeof value === 'function' ? value(url, opts) : value
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

const ENTRY = { id: 'ws-test-1', label: 'Code Development', root: '~/workspaces/ws-test-1' }

const WORKERS = [
  { name: 'wA', description: 'worker A', tools: ['FileEditor'], runtime_status: 'ready' },
  { name: 'wB', description: 'worker B', tools: [], runtime_status: 'ready' },
]

function stubBackend(entry = ENTRY, extra = {}) {
  const id = entry.id
  const routes = {
    '/api/workspace/list': jsonOk([entry]),
    [`/api/workspace/${id}/effective_permissions`]: jsonOk({
      effective_permissions: {
        filesystem: 'write',
        network: 'read',
        git: 'write',
        system: 'read',
        execution: 'banned',
        container: true,
      },
    }),
    '/api/health/containers': jsonOk({ docker: 'reachable' }),
    [`/api/workspace/${id}/workers`]: jsonOk(WORKERS),
    [`/api/workspace/${id}/containers`]: jsonOk({ containers: [] }),
    [`/api/session/list?workspace_id=${id}`]: jsonOk([]),
    '/api/session/create': jsonOk({ session_id: 's-1', mode: 'engineer', name: 'Fix bug' }),
    '/api/resource-catalog': jsonOk({ items: [] }),
    ...extra,
  }
  return stubFetchByUrl(routes)
}

const lastWs = () => MockWebSocket.instances[MockWebSocket.instances.length - 1]
const sentCommands = (ws) => ws.sent.map(JSON.parse)
const tabBar = () => document.querySelector('.tab-bar')
const activeTabLabel = () =>
  tabBar()?.querySelector('.tab-item.tab-active .tab-label')?.textContent || null

const TWO_SESSIONS = [
  { session_id: 'sess-1', workspace_id: ENTRY.id, name: 'S1' },
  { session_id: 'sess-2', workspace_id: ENTRY.id, name: 'S2' },
]

// ── Worker panel helpers ────────────────────────────────────────────────────
const workerPanel = () => document.querySelector('.worker-output-panel')
const panelText = () => workerPanel()?.textContent || ''
const panelRows = () => workerPanel()?.querySelectorAll('.worker-event-row').length ?? 0
const emptyStateText = () =>
  workerPanel()?.querySelector('.worker-output-empty')?.textContent ?? null
const panelHeader = () =>
  workerPanel()?.querySelector('.worker-output-header-label')?.textContent ?? null
const panelCtx = () =>
  workerPanel()?.querySelector('.worker-output-header-ctx')?.textContent ?? null

// ── Async setup helpers ─────────────────────────────────────────────────────
async function connectHub() {
  await waitFor(() => expect(MockWebSocket.instances.length).toBeGreaterThan(0))
  const hub = MockWebSocket.instances[0]
  act(() => hub.open())
  return hub
}

function seedStrip(hub, sessions) {
  act(() => hub.receive({ type: 'sessions_list', sessions }))
  act(() => hub.receive({ type: 'open_sessions', sessions }))
}

// The backend binds a session to its workspace via session_loaded; without
// this, SessionTab.workspaceId stays null and WorkerManagementPanel never
// fetches the worker list. The backend also pushes the session config via
// config_changed; without it ConfigPanel stays at 'Loading config...' and the
// workspace tab (which hosts WorkerManagementPanel) never renders.
function bindWorkspace(ws, sessionId) {
  act(() =>
    ws.receive({
      type: 'session_loaded',
      session_id: sessionId,
      workspace_id: ENTRY.id,
      name: sessionId === 'sess-1' ? 'S1' : 'S2',
    })
  )
  act(() =>
    ws.receive({
      type: 'config_changed',
      session_id: sessionId,
      config: {
        mode: 'custom',
        temperature: 0.7,
        max_turns: 10,
        provider: 'openai',
        model: 'gpt-4o',
        system_prompt: '',
        tools: [],
        session_permissions: {},
      },
    })
  )
}

async function mountFirstTab() {
  const hub = await connectHub()
  seedStrip(hub, TWO_SESSIONS)
  await waitFor(() => expect(MockWebSocket.instances.length).toBe(2))
  const wsA = lastWs()
  act(() => wsA.open())
  bindWorkspace(wsA, 'sess-1')
  return { hub, wsA }
}

// Click a worker row in the (always-mounted) WorkerManagementPanel of the
// active SessionTab — the real UI path that sets App's workerPanelState.
// NOTE: once a worker is selected, the WorkerOutputPanel bottom bar renders a
// second element with the exact worker name (span.worker-output-bottom-name,
// also titled). getAllByText + closest('.worker-output-panel') scopes the click
// to the row name span, which lives outside the panel.
async function selectWorker(name) {
  await waitFor(() => expect(screen.getAllByText(name).length).toBeGreaterThan(0))
  const row = screen.getAllByText(name).find((el) => !el.closest('.worker-output-panel'))
  expect(row).toBeTruthy()
  fireEvent.click(row)
  await waitFor(() => expect(panelHeader()).toBe(`Worker: ${name}`))
}

function sendWorkerEvent(ws, sessionId, workerName, evt) {
  act(() =>
    ws.receive({
      session_id: sessionId,
      worker_name: workerName,
      ...evt,
    })
  )
}

const spawnEvt = (ts) => ({ type: 'worker:worker_spawned', timestamp: ts, data: { worker_name: 'wA' } })
const statusEvt = (ts, extra = {}) => ({
  type: 'worker:worker_status',
  timestamp: ts,
  data: { status: 'running', message: 'Working on it', ...extra },
})
const completedEvt = (ts) => ({ type: 'worker:worker_completed', timestamp: ts, data: { worker_name: 'wA' } })
const ctxEvt = (ts, context_length) => ({
  type: 'worker:context_updated',
  timestamp: ts,
  context_length,
  critical_threshold: 80000,
})

// ── Setup / teardown ────────────────────────────────────────────────────────
beforeEach(() => {
  localStorage.clear()
  window.location.hash = `#/workspace/${ENTRY.id}`
  useStore.getState().reset()
  useWorkspaceStore.getState().reset()
  useWorkspaceStore.setState({ workspaceList: [{ ...ENTRY }] })
  useSessionTabsStore.getState().reset()
  MockWebSocket.instances = []
  vi.stubGlobal('WebSocket', MockWebSocket)
  vi.stubGlobal('ResizeObserver', MockResizeObserver)
  stubBackend()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

// ── Tests ───────────────────────────────────────────────────────────────────
describe('worker-event isolation between sessions (R6b)', () => {
  it('(a) sess-1 worker events render in A panel only; S2 panel starts empty and shows no leak', async () => {
    render(<App />)
    const { wsA } = await mountFirstTab()
    expect(activeTabLabel()).toBe('S1')

    // Workers fetch resolves → rows render → click wA row → panel opens (real UI path)
    await selectWorker('wA')
    expect(panelRows()).toBe(0)
    expect(emptyStateText()).toContain('Worker output appears here')

    // Stream sess-1 worker lifecycle events on wsA (distinct timestamps)
    sendWorkerEvent(wsA, 'sess-1', 'wA', spawnEvt('2025-01-01T00:00:01Z'))
    sendWorkerEvent(wsA, 'sess-1', 'wA', statusEvt('2025-01-01T00:00:02Z'))
    sendWorkerEvent(wsA, 'sess-1', 'wA', completedEvt('2025-01-01T00:00:03Z'))
    sendWorkerEvent(wsA, 'sess-1', 'wA', ctxEvt('2025-01-01T00:00:04Z', 12300))

    await waitFor(() => expect(panelRows()).toBe(3))
    expect(panelText()).toContain('🟢 Worker spawned: wA')
    expect(panelText()).toContain('⏳ Worker status: running — Working on it')
    expect(panelText()).toContain('✅ Worker completed: wA')
    expect(panelCtx()).toContain('ctx: 12.3K / 80.0K')
    expect(panelHeader()).toBe('Worker: wA')

    // Switch to S2 → new SessionTab mounts (new WS); panel resets to empty
    fireEvent.click(within(tabBar()).getByText('S2'))
    await waitFor(() => expect(MockWebSocket.instances.length).toBe(3))
    const wsB = lastWs()
    act(() => wsB.open())
    bindWorkspace(wsB, 'sess-2')
    expect(activeTabLabel()).toBe('S2')

    await selectWorker('wB')
    expect(panelHeader()).toBe('Worker: wB')
    expect(panelRows()).toBe(0)
    expect(emptyStateText()).toContain('Worker output appears here')
    expect(panelText()).not.toContain('Worker spawned: wA')
    expect(panelText()).not.toContain('Worker status')
    expect(panelText()).not.toContain('Worker completed')
  })

  it('(b) worker event tagged with a different session_id on wsA is dropped — A panel unaffected', async () => {
    render(<App />)
    const { wsA } = await mountFirstTab()

    await selectWorker('wA')

    // Legit sess-1 event
    sendWorkerEvent(wsA, 'sess-1', 'wA', spawnEvt('2025-01-01T00:00:01Z'))
    await waitFor(() => expect(panelRows()).toBe(1))
    expect(panelText()).toContain('🟢 Worker spawned: wA')

    // Cross-session event on the WRONG socket: tagged sess-2 → SessionTab
    // mismatch filter drops it (console.warn, no crash, no panel change)
    sendWorkerEvent(wsA, 'sess-2', 'wB', statusEvt('2025-01-01T00:00:02Z'))

    await waitFor(() => expect(panelRows()).toBe(1))
    expect(panelText()).not.toContain('Worker status')
    expect(panelText()).toContain('🟢 Worker spawned: wA')

    // S2 panel unaffected by anything sent on wsA
    fireEvent.click(within(tabBar()).getByText('S2'))
    await waitFor(() => expect(MockWebSocket.instances.length).toBe(3))
    const wsB = lastWs()
    act(() => wsB.open())
    bindWorkspace(wsB, 'sess-2')
    await selectWorker('wB')
    expect(panelRows()).toBe(0)
    expect(emptyStateText()).toContain('Worker output appears here')
    expect(panelText()).not.toContain('Worker spawned: wA')
    expect(panelText()).not.toContain('Worker status')
  })

  it('(c) events stay per-session across tab switches and WS remount', async () => {
    render(<App />)
    const { wsA } = await mountFirstTab()

    // S1: open wA panel, stream one spawn event
    await selectWorker('wA')
    sendWorkerEvent(wsA, 'sess-1', 'wA', spawnEvt('2025-01-01T00:00:01Z'))
    await waitFor(() => expect(panelRows()).toBe(1))
    expect(panelText()).toContain('🟢 Worker spawned: wA')

    // Switch to S2 (new WS), open wB panel
    fireEvent.click(within(tabBar()).getByText('S2'))
    await waitFor(() => expect(MockWebSocket.instances.length).toBe(3))
    const wsB = lastWs()
    act(() => wsB.open())
    bindWorkspace(wsB, 'sess-2')
    await selectWorker('wB')

    // S2's worker event renders only in B's panel
    sendWorkerEvent(wsB, 'sess-2', 'wB', {
      type: 'worker:worker_spawned',
      timestamp: '2025-01-01T00:00:02Z',
      data: { worker_name: 'wB' },
    })
    await waitFor(() => expect(panelRows()).toBe(1))
    expect(panelText()).toContain('🟢 Worker spawned: wB')
    expect(panelText()).not.toContain('🟢 Worker spawned: wA')

    // Back to S1 → fresh WS (wsA2) → A panel shows ONLY A's event
    fireEvent.click(within(tabBar()).getByText('S1'))
    await waitFor(() => expect(MockWebSocket.instances.length).toBe(4))
    const wsA2 = lastWs()
    act(() => wsA2.open())
    bindWorkspace(wsA2, 'sess-1')
    expect(activeTabLabel()).toBe('S1')

    await waitFor(() => expect(panelHeader()).toBe('Worker: wA'))
    await waitFor(() => expect(panelRows()).toBe(1))
    expect(panelText()).toContain('🟢 Worker spawned: wA')
    expect(panelText()).not.toContain('🟢 Worker spawned: wB')
  })
})
