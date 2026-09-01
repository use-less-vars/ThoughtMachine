// @vitest-environment jsdom
/*
 * SessionRouteMounting.test.jsx — SessionTab body mounts ONLY on session
 * routes (TASK 3 nested routes).
 *
 * Renders the REAL App with a stubbed global WebSocket + fetch:
 *   - workspace route  → strip may exist but NO SessionTab body mounts
 *     (no .tab-wrapper, hub WS stays the only WebSocket; WorkspaceDetailPage
 *     renders instead)
 *   - nested session route (#/workspace/:wsId/session/:sid) → the active
 *     session mounts its own WS and sends load_session
 */
import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  render,
  screen,
  cleanup,
  waitFor,
  act,
  fireEvent,
} from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import App from '../../App'
import useStore from '../../store/useStore'
import useWorkspaceStore from '../../store/workspaceStore'
import useSessionTabsStore from '../../sessionTabsStore'

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
  constructor(callback) { this.callback = callback }
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

const SUMMARY = {
  workspace_id: ENTRY.id,
  label: ENTRY.label,
  root_path: ENTRY.root,
  allow_host_resources: false,
  permissions: {},
  resource_catalog: [],
  active_sessions: [],
  active_workers: [],
  active_containers: [],
  dockerfile: null,
  worker_templates: [],
  tools: [],
}

function stubBackend() {
  stubFetchByUrl({
    '/api/workspace/list': jsonOk([ENTRY]),
    // Hide the first-run wizard: onboarding is complete.
    '/api/onboarding/status': jsonOk({ onboarding_complete: true }),
    // WorkspaceDetailPage reads the summary to render its tabbed body.
    [`/api/workspace/${ENTRY.id}/summary`]: jsonOk(SUMMARY),
  })
}

const TWO_SESSIONS = [
  { session_id: 'sess-1', workspace_id: ENTRY.id, name: 'S1' },
  { session_id: 'sess-2', workspace_id: ENTRY.id, name: 'S2' },
]

function seedWorkspaceSessions(hub) {
  act(() => hub.receive({ type: 'sessions_list', sessions: TWO_SESSIONS }))
  act(() =>
    hub.receive({
      type: 'open_sessions',
      sessions: TWO_SESSIONS.map(({ session_id, name }) => ({ session_id, name })),
    })
  )
}

async function connectHub() {
  await waitFor(() => {
    expect(MockWebSocket.instances.length).toBeGreaterThan(0)
  })
  const hub = MockWebSocket.instances[0]
  await act(async () => hub.open())
  return hub
}

function sentCommands(ws) {
  return (ws.sent || []).map((d) => {
    try { return JSON.parse(d) } catch { return null }
  }).filter(Boolean)
}

beforeEach(() => {
  localStorage.clear()
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

describe('SessionTab body mounts only on session routes', () => {
  it('workspace route: no SessionTab body mounts, hub WS stays the only socket', async () => {
    window.location.hash = `#/workspace/${ENTRY.id}`
    render(<App />)
    const hub = await connectHub()
    seedWorkspaceSessions(hub)

    // Strip exists (tabs were lazily created), but NO session body mounts.
    await waitFor(() => {
      expect(MockWebSocket.instances.length).toBe(1)
    })
    expect(document.querySelector('.tab-wrapper')).toBeNull()
    expect(screen.queryByText(/Loading session/)).not.toBeInTheDocument()

    // The workspace route view renders instead. WorkspaceDetailPage mounts
    // and the Permissions & Resources tab (default is Overview) shows the
    // empty-catalog message because the stubbed summary has resource_catalog: [].
    await screen.findByRole('tab', { name: 'Permissions & Resources' })
    fireEvent.click(screen.getByRole('tab', { name: 'Permissions & Resources' }))
    await waitFor(() => {
      expect(screen.getByText('No resource catalog available.')).toBeInTheDocument()
    })
  })

  it('nested session route: active session mounts its own WS and loads it', async () => {
    window.location.hash = `#/workspace/${ENTRY.id}/session/sess-1`
    render(<App />)
    const hub = await connectHub()
    seedWorkspaceSessions(hub)

    await waitFor(() => {
      expect(MockWebSocket.instances.length).toBe(2)
    })
    expect(document.querySelector('.tab-wrapper')).toBeTruthy()
    const tabWs = MockWebSocket.instances[1]
    expect(tabWs).not.toBe(hub)
    await act(async () => tabWs.open())
    await waitFor(() => {
      const load = sentCommands(tabWs).find((c) => c.command === 'load_session')
      expect(load).toBeTruthy()
      expect(load.session_id).toBe('sess-1')
    })
  })
})
