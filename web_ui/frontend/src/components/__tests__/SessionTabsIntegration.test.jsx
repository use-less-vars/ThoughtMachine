// @vitest-environment jsdom
/*
 * SessionTabsIntegration.test.jsx — App-level integration for the R7
 * per-workspace session tab strip.
 *
 * Renders the REAL App (hub WS + TabBar + SessionTab) with a stubbed global
 * WebSocket and fetch:
 *   - open_sessions builds the strip LAZILY (strip entries only; exactly ONE
 *     SessionTab WS for the active tab)
 *   - clicking a strip tab activates it and mounts its OWN WS (load_session)
 *   - closing a tab is frontend-only (the session survives in the store)
 *   - a session deep link restores the persisted strip and activates the
 *     target session
 */
import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  render,
  screen,
  fireEvent,
  cleanup,
  waitFor,
  act,
  within,
} from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import App from '../../App'
import useStore from '../../store/useStore'
import useWorkspaceStore from '../../store/workspaceStore'
import useSessionTabsStore from '../../sessionTabsStore'

// Mock WebSocket (App + SessionTab both use the global WebSocket constants).
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

  send(data) {
    this.sent.push(data)
  }

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
  constructor(callback) {
    this.callback = callback
  }
  observe() {}
  unobserve() {}
  disconnect() {}
}

function jsonOk(data, status = 200) {
  return { ok: true, status, json: async () => data, text: async () => JSON.stringify(data) }
}

const DEFAULT_FALLBACK = { ok: true, status: 200, json: async () => ({}), text: async () => '' }

function stubFetchByUrl(routes, defaultResponse = DEFAULT_FALLBACK) {
  const fetchMock = vi.fn(async (url, options) => {
    const key = Object.keys(routes)
      .filter((k) => String(url).includes(k))
      .sort((a, b) => b.length - a.length)[0]
    if (!key) return defaultResponse
    const resp = routes[key]
    return typeof resp === 'function' ? resp(url, options) : resp
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

const ENTRY = { id: 'ws-test-1', label: 'Code Development', root: '~/workspaces/ws-test-1' }

function stubBackend(entry = ENTRY, extra = {}) {
  const id = entry.id
  return stubFetchByUrl({
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
    [`/api/workspace/${id}/workers`]: jsonOk([]),
    [`/api/workspace/${id}/containers`]: jsonOk({ containers: [] }),
    [`/api/session/list?workspace_id=${id}`]: jsonOk([]),
    '/api/session/create': jsonOk({ session_id: 's-1', mode: 'engineer', name: 'Fix bug' }),
    '/api/resource-catalog': jsonOk({ items: [] }),
    ...extra,
  })
}

function lastWs() {
  return MockWebSocket.instances[MockWebSocket.instances.length - 1]
}

function sentCommands(ws) {
  return ws.sent.map((data) => JSON.parse(data))
}

function tabBar() {
  return document.querySelector('.tab-bar')
}

function activeTabLabel() {
  return tabBar()?.querySelector('.tab-item.tab-active .tab-label')?.textContent || null
}

const TWO_SESSIONS = [
  { session_id: 'sess-1', workspace_id: ENTRY.id, name: 'S1' },
  { session_id: 'sess-2', workspace_id: ENTRY.id, name: 'S2' },
]

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

// Open the hub WS (App connects on mount) and feed it the standard session
// events: sessions_list (teaches workspace_id mappings) then open_sessions
// (builds the strip lazily + flips hubReady so SessionTabs may connect).
async function connectHub() {
  await waitFor(() => {
    expect(MockWebSocket.instances.length).toBeGreaterThan(0)
  })
  const hub = MockWebSocket.instances[0]
  await act(async () => hub.open())
  return hub
}

async function seedStrip(hub, sessions) {
  await act(async () => hub.receive({ type: 'sessions_list', sessions }))
  await act(async () => hub.receive({ type: 'open_sessions', sessions }))
}

describe('App session tab strip — open_sessions restore', () => {
  it('builds a lazy strip from open_sessions and mounts only the active tab', async () => {
    render(<App />)
    const hub = await connectHub()
    await seedStrip(hub, TWO_SESSIONS)

    // Strip shows both sessions; the first is active.
    await waitFor(() => {
      const bar = tabBar()
      expect(bar).toBeTruthy()
      expect(within(bar).getByText('S1')).toBeInTheDocument()
      expect(within(bar).getByText('S2')).toBeInTheDocument()
      expect(activeTabLabel()).toBe('S1')
    })

    // Exactly ONE SessionTab WebSocket exists (hub + 1 active tab — the
    // inactive strip entry mounts nothing).
    await waitFor(() => {
      expect(MockWebSocket.instances.length).toBe(2)
    })
    const tabWs = lastWs()
    expect(tabWs).not.toBe(hub)
    expect(tabWs.url.endsWith('/ws')).toBe(true)
  })

  it('clicking a strip tab activates it and mounts its own WS that loads it', async () => {
    render(<App />)
    const hub = await connectHub()
    await seedStrip(hub, TWO_SESSIONS)
    await waitFor(() => {
      expect(MockWebSocket.instances.length).toBe(2)
    })
    const ws1 = lastWs()
    await act(async () => ws1.open())

    fireEvent.click(within(tabBar()).getByText('S2'))

    // The new active tab mounts a NEW SessionTab WebSocket.
    await waitFor(() => {
      expect(MockWebSocket.instances.length).toBe(3)
    })
    expect(activeTabLabel()).toBe('S2')
    const ws2 = lastWs()
    expect(ws2).not.toBe(ws1)
    await act(async () => ws2.open())
    await waitFor(() => {
      const load = sentCommands(ws2).find((c) => c.command === 'load_session')
      expect(load).toBeTruthy()
      expect(load.session_id).toBe('sess-2')
    })
  })

  it('closing the active tab removes it from the strip but keeps the session server-side', async () => {
    render(<App />)
    const hub = await connectHub()
    await seedStrip(hub, TWO_SESSIONS)
    await waitFor(() => {
      expect(MockWebSocket.instances.length).toBe(2)
    })
    // Make sess-2 the active tab first, then close it.
    fireEvent.click(within(tabBar()).getByText('S2'))
    await waitFor(() => {
      expect(MockWebSocket.instances.length).toBe(3)
    })
    expect(activeTabLabel()).toBe('S2')

    fireEvent.click(tabBar().querySelector('.tab-item.tab-active .tab-close-btn'))

    await waitFor(() => {
      expect(within(tabBar()).queryByText('S2')).not.toBeInTheDocument()
      expect(within(tabBar()).getByText('S1')).toBeInTheDocument()
    })
    // Frontend-only close: the session survives in the sessions list.
    expect(useStore.getState().sessions.some((s) => s.session_id === 'sess-2')).toBe(true)
    // The route follows the neighbor tab.
    await waitFor(() => {
      expect(window.location.hash).toBe('#/session/sess-1')
      expect(activeTabLabel()).toBe('S1')
    })
  })

  it('session deep link restores the persisted strip and activates the target', async () => {
    // Persisted strip from a previous page load (active sess-2 there).
    localStorage.setItem(
      `tm.sessionTabs.${ENTRY.id}`,
      JSON.stringify({
        v: 1,
        tabs: [
          { sessionId: 'sess-1', title: 'S1' },
          { sessionId: 'sess-2', title: 'S2' },
        ],
        activeSessionId: 'sess-2',
      })
    )
    // Deep link straight into sess-1 (e.g. a bookmarked URL).
    window.location.hash = '#/session/sess-1'
    render(<App />)
    const hub = await connectHub()
    await act(async () =>
      hub.receive({ type: 'sessions_list', sessions: TWO_SESSIONS })
    )
    await act(async () =>
      hub.receive({
        type: 'open_sessions',
        sessions: TWO_SESSIONS.map(({ session_id, name }) => ({ session_id, name })),
      })
    )

    // Strip restored from localStorage with BOTH tabs; deep-linked sess-1
    // is the active one (route wins over the persisted activeSessionId).
    await waitFor(() => {
      const bar = tabBar()
      expect(bar).toBeTruthy()
      expect(within(bar).getByText('S1')).toBeInTheDocument()
      expect(within(bar).getByText('S2')).toBeInTheDocument()
      expect(activeTabLabel()).toBe('S1')
    })
    // Exactly one SessionTab WS, and it loads the deep-linked session.
    await waitFor(() => {
      expect(MockWebSocket.instances.length).toBe(2)
    })
    const ws = lastWs()
    await act(async () => ws.open())
    await waitFor(() => {
      const load = sentCommands(ws).find((c) => c.command === 'load_session')
      expect(load).toBeTruthy()
      expect(load.session_id).toBe('sess-1')
    })
  })

  it('page reload restores the persisted strip and remounts the previously active session', async () => {
    // Simulate a prior page load: strip persisted with sess-2 active.
    localStorage.setItem(
      `tm.sessionTabs.${ENTRY.id}`,
      JSON.stringify({
        v: 1,
        tabs: [
          { sessionId: 'sess-1', title: 'Tab 1' },
          { sessionId: 'sess-2', title: 'Tab 2' },
        ],
        activeSessionId: 'sess-2',
      })
    )
    // Fresh mount = simulated reload; landing on the workspace route.
    window.location.hash = `#/workspace/${ENTRY.id}`
    render(<App />)
    const hub = await connectHub()
    await act(async () =>
      hub.receive({ type: 'sessions_list', sessions: TWO_SESSIONS })
    )
    await act(async () =>
      hub.receive({
        type: 'open_sessions',
        sessions: TWO_SESSIONS.map(({ session_id, name }) => ({ session_id, name })),
      })
    )

    // Strip restored from localStorage with BOTH tabs; the previously active
    // sess-2 is active (open_sessions must not override it).
    await waitFor(() => {
      const bar = tabBar()
      expect(bar).toBeTruthy()
      expect(within(bar).getByText('Tab 1')).toBeInTheDocument()
      expect(within(bar).getByText('Tab 2')).toBeInTheDocument()
      expect(activeTabLabel()).toBe('Tab 2')
    })
    // Exactly one SessionTab WS (hub + 1) and it loads the previously active
    // session; sess-1 is strip-only (no mount, no WS).
    await waitFor(() => {
      expect(MockWebSocket.instances.length).toBe(2)
    })
    const ws = lastWs()
    expect(ws).not.toBe(hub)
    await act(async () => ws.open())
    await waitFor(() => {
      const load = sentCommands(ws).find((c) => c.command === 'load_session')
      expect(load).toBeTruthy()
      expect(load.session_id).toBe('sess-2')
    })
    expect(MockWebSocket.instances.length).toBe(2)

    // Second fresh mount (another reload) is idempotent — no duplicate tabs.
    cleanup()
    MockWebSocket.instances = []
    render(<App />)
    const hub2 = await connectHub()
    await act(async () =>
      hub2.receive({ type: 'sessions_list', sessions: TWO_SESSIONS })
    )
    await act(async () =>
      hub2.receive({
        type: 'open_sessions',
        sessions: TWO_SESSIONS.map(({ session_id, name }) => ({ session_id, name })),
      })
    )
    await waitFor(() => {
      const bar = tabBar()
      expect(bar).toBeTruthy()
      expect(within(bar).getAllByText('Tab 1')).toHaveLength(1)
      expect(within(bar).getAllByText('Tab 2')).toHaveLength(1)
      expect(activeTabLabel()).toBe('Tab 2')
    })
    // Persisted entry still holds exactly two tabs.
    const persisted = JSON.parse(localStorage.getItem(`tm.sessionTabs.${ENTRY.id}`))
    expect(persisted.tabs).toHaveLength(2)
  })
})
