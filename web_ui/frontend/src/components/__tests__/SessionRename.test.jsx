// @vitest-environment jsdom
/*
 * SessionRename.test.jsx — App-level integration for session rename.
 *
 * Renders the REAL App (hub WS + TabBar + SessionTab) with a stubbed global
 * WebSocket and fetch. Verifies that renaming a session updates the tab
 * strip title IMMEDIATELY (no page reload) through BOTH rename paths:
 *   (a) the inline rename UI in the SessionTab header (Enter commit), and
 *   (b) WS session_renamed events — delivered on the per-session tab WS
 *       (backend rename_session broadcast) or relayed on the hub.
 * Also verifies the renamed title persists across tab switches and reloads
 * via the sessionTabsStore localStorage persistence model.
 */
import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  render,
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
        git: 'write',
        filesystem: 'write',
        container: true,
        network: 'read',
        mcp: 'banned',
        host_bash: 'banned',
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

function allTabLabels() {
  return Array.from(document.querySelectorAll('.tab-item .tab-label')).map((n) => n.textContent)
}

function persistedTabs() {
  const raw = localStorage.getItem(`tm.sessionTabs.${ENTRY.id}`)
  return raw ? JSON.parse(raw).tabs : []
}

function persistedActiveSessionId() {
  const raw = localStorage.getItem(`tm.sessionTabs.${ENTRY.id}`)
  return raw ? JSON.parse(raw).activeSessionId : null
}

function headerName() {
  return document.querySelector('.session-header-name')?.textContent || null
}

const TWO_SESSIONS = [
  { session_id: 'sess-1', workspace_id: ENTRY.id, name: 'S1' },
  { session_id: 'sess-2', workspace_id: ENTRY.id, name: 'S2' },
]

beforeEach(() => {
  localStorage.clear()
  // Nested route: the session view lives at #/workspace/:wsId/session/:sid.
  window.location.hash = `#/workspace/${ENTRY.id}/session/sess-1`
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

// Bind a session to its own tab WS: session_loaded + config_changed bring the
// SessionTab out of its loading/deferred state and render the full UI
// (including the inline rename header).
async function bindWorkspace(ws, sessionId) {
  await act(async () =>
    ws.receive({ type: 'session_loaded', session_id: sessionId, workspace_id: ENTRY.id, name: 'S1' })
  )
  await act(async () =>
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

// Full boot: connect hub, build the two-session strip, and load the active
// session (sess-1) into its SessionTab. Returns { hub, wsA }.
async function mountFirstTab() {
  const hub = await connectHub()
  await seedStrip(hub, TWO_SESSIONS)
  await waitFor(() => {
    expect(MockWebSocket.instances.length).toBe(2)
  })
  const wsA = lastWs()
  expect(wsA).not.toBe(hub)
  await act(async () => wsA.open())
  await bindWorkspace(wsA, 'sess-1')
  return { hub, wsA }
}

// Rename the ACTIVE session via the inline header UI (click rename, type,
// commit with Enter — same as the real flow).
function renameActiveViaUI(newName) {
  const btn = document.querySelector('.session-header-rename-btn')
  expect(btn).toBeTruthy()
  fireEvent.click(btn)
  const input = document.querySelector('.session-header-input')
  expect(input).toBeTruthy()
  fireEvent.change(input, { target: { value: newName } })
  fireEvent.keyDown(input, { key: 'Enter' })
}

describe('App session rename — tab title updates immediately', () => {
  it('renaming via the UI updates the tab strip title, persisted state, and header instantly', async () => {
    render(<App />)
    const { hub, wsA } = await mountFirstTab()

    // Baseline: strip shows S1 as the active tab; header shows the session name.
    await waitFor(() => {
      expect(activeTabLabel()).toBe('S1')
      expect(headerName()).toBe('S1')
    })

    renameActiveViaUI('Renamed One')

    // Tab strip title updates immediately (no reload, no open_sessions round trip).
    await waitFor(() => {
      expect(activeTabLabel()).toBe('Renamed One')
    })
    expect(allTabLabels()).toEqual(['Renamed One', 'S2'])

    // Persisted strip state carries the new title.
    const persisted = persistedTabs()
    expect(persisted.find((t) => t.sessionId === 'sess-1').title).toBe('Renamed One')
    expect(persistedActiveSessionId()).toBe('sess-1')

    // Shared sessions store + header reflect the new name.
    expect(useStore.getState().sessions.find((s) => s.session_id === 'sess-1').name).toBe('Renamed One')
    expect(headerName()).toBe('Renamed One')

    // The rename command was actually sent to the backend on the tab WS.
    const rename = sentCommands(wsA).find((c) => c.command === 'rename_session')
    expect(rename).toBeTruthy()
    expect(rename.session_id).toBe('sess-1')
    expect(rename.new_name).toBe('Renamed One')
  })

  it('a WS session_renamed event on the tab WS updates the tab title immediately', async () => {
    render(<App />)
    const { wsA } = await mountFirstTab()
    await waitFor(() => {
      expect(activeTabLabel()).toBe('S1')
    })

    // Remote/API rename: backend broadcasts session_renamed to the session's
    // tab WS (rename_session command path). SessionTab funnels it to App.
    await act(async () =>
      wsA.receive({ type: 'session_renamed', session_id: 'sess-1', new_name: 'Remote Rename' })
    )

    await waitFor(() => {
      expect(activeTabLabel()).toBe('Remote Rename')
    })
    // Persisted state updated too.
    expect(persistedTabs().find((t) => t.sessionId === 'sess-1').title).toBe('Remote Rename')
    expect(useStore.getState().sessions.find((s) => s.session_id === 'sess-1').name).toBe('Remote Rename')
  })

  it('a session_renamed event relayed on the hub updates the inactive strip tab title', async () => {
    render(<App />)
    const { hub } = await mountFirstTab()
    await waitFor(() => {
      expect(allTabLabels()).toEqual(['S1', 'S2'])
    })

    // Rename the OTHER session (sess-2, strip-only — not mounted) via a hub
    // session_renamed event: the strip entry must update immediately.
    await act(async () =>
      hub.receive({ type: 'session_renamed', session_id: 'sess-2', new_name: 'S2 Renamed' })
    )

    await waitFor(() => {
      expect(allTabLabels()).toEqual(['S1', 'S2 Renamed'])
    })
    expect(persistedTabs().find((t) => t.sessionId === 'sess-2').title).toBe('S2 Renamed')
  })

  it('the renamed title survives tab switches and page reloads (persistence model)', async () => {
    render(<App />)
    const { hub } = await mountFirstTab()
    await waitFor(() => {
      expect(activeTabLabel()).toBe('S1')
    })

    renameActiveViaUI('Persisted Name')
    await waitFor(() => {
      expect(activeTabLabel()).toBe('Persisted Name')
    })

    // Switch to the other tab and back — the S1 tab keeps its renamed title.
    fireEvent.click(within(tabBar()).getByText('S2'))
    await waitFor(() => {
      expect(activeTabLabel()).toBe('S2')
      expect(MockWebSocket.instances.length).toBe(3)
    })
    const ws2 = lastWs()
    await act(async () => ws2.open())

    fireEvent.click(within(tabBar()).getByText('Persisted Name'))
    await waitFor(() => {
      expect(activeTabLabel()).toBe('Persisted Name')
    })

    // Page reload: fresh App mount; the persisted strip (renamed title +
    // activeSessionId) must be restored and open_sessions must NOT clobber it.
    cleanup()
    MockWebSocket.instances = []
    render(<App />)
    const hub2 = await connectHub()
    await seedStrip(hub2, TWO_SESSIONS)  // carries the OLD names S1/S2

    await waitFor(() => {
      const bar = tabBar()
      expect(bar).toBeTruthy()
      expect(activeTabLabel()).toBe('Persisted Name')
      expect(within(bar).getByText('Persisted Name')).toBeInTheDocument()
      expect(within(bar).getByText('S2')).toBeInTheDocument()
    })
    // Persisted entry still holds the renamed title, exactly one S1 tab.
    const persisted = persistedTabs()
    expect(persisted).toHaveLength(2)
    expect(persisted.find((t) => t.sessionId === 'sess-1').title).toBe('Persisted Name')
    expect(within(tabBar()).getAllByText('Persisted Name')).toHaveLength(1)
  })
})
