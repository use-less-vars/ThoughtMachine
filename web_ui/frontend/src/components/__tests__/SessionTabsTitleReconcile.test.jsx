// @vitest-environment jsdom
/*
 * SessionTabsTitleReconcile.test.jsx
 *
 * The tab strip keeps its own persisted copy of each session's title
 * (sessionTabsStore → localStorage `tm.sessionTabs.<ws>`). `open_sessions`
 * carries the authoritative server-side `name` for every open session, so the
 * App-side reconciliation (App.jsx, open_sessions handler) must use it to
 * REPAIR a stale persisted title, not merely fill an empty one.
 *
 * Drives the real App through the hub WS (no hand-rolled store mutations).
 */
import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, act, waitFor, cleanup } from '@testing-library/react'
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
const OTHER_WS = 'ws-other-2'

function stubBackend(entry = ENTRY) {
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
    [`/api/session/list?workspace_id=${id}`]: jsonOk([{ session_id: 'sess-1' }]),
    '/api/resource-catalog': jsonOk({ items: [] }),
  })
}

function persistedEntry(ws) {
  const raw = localStorage.getItem(`tm.sessionTabs.${ws}`)
  return raw ? JSON.parse(raw) : null
}

function stripTitles(ws) {
  return (persistedEntry(ws)?.tabs || []).map((t) => `${t.sessionId}=${t.title}`)
}

function allTabLabels() {
  return Array.from(document.querySelectorAll('.tab-item .tab-label')).map((n) => n.textContent)
}

beforeEach(() => {
  localStorage.clear()
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

describe('App tab-title reconciliation with the authoritative session name', () => {
  it('repairs a STALE persisted title, still fills an EMPTY one, and leaves another workspace alone', async () => {
    // Pre-existing persisted strips (what a reload restores):
    //  - this workspace: sess-1 carries a STALE title, sess-2 an EMPTY one.
    //  - another workspace: its tab title must not be touched.
    localStorage.setItem(
      `tm.sessionTabs.${ENTRY.id}`,
      JSON.stringify({
        v: 1,
        tabs: [
          { sessionId: 'sess-1', title: 'Stale Old Name' },
          { sessionId: 'sess-2', title: '' },
        ],
        activeSessionId: 'sess-1',
      })
    )
    localStorage.setItem(
      `tm.sessionTabs.${OTHER_WS}`,
      JSON.stringify({
        v: 1,
        tabs: [{ sessionId: 'sess-9', title: 'Other Workspace Name' }],
        activeSessionId: 'sess-9',
      })
    )

    render(<App />)

    await waitFor(() => {
      expect(MockWebSocket.instances.length).toBeGreaterThan(0)
    })
    const hub = MockWebSocket.instances[0]
    await act(async () => hub.open())

    // Authoritative server view (server.py get_open_sessions payload shape).
    const sessions = [
      { session_id: 'sess-1', name: 'Authoritative One', workspace_id: ENTRY.id },
      { session_id: 'sess-2', name: 'Authoritative Two', workspace_id: ENTRY.id },
    ]
    await act(async () => hub.receive({ type: 'sessions_list', sessions }))
    await act(async () => hub.receive({ type: 'open_sessions', sessions }))

    // Stale non-empty title corrected; empty title filled; order preserved.
    await waitFor(() => {
      expect(stripTitles(ENTRY.id)).toEqual([
        'sess-1=Authoritative One',
        'sess-2=Authoritative Two',
      ])
    })

    // In-memory store agrees with what was persisted.
    const inMem = useSessionTabsStore.getState().byWorkspace[ENTRY.id].tabs
    expect(inMem.map((t) => `${t.sessionId}=${t.title}`)).toEqual([
      'sess-1=Authoritative One',
      'sess-2=Authoritative Two',
    ])

    // The strip renders the corrected label.
    await waitFor(() => {
      expect(allTabLabels()).toContain('Authoritative One')
    })

    // The OTHER workspace's strip is untouched — both persisted and in memory.
    expect(stripTitles(OTHER_WS)).toEqual(['sess-9=Other Workspace Name'])
    expect(
      (useSessionTabsStore.getState().byWorkspace[OTHER_WS]?.tabs || []).map((t) => t.title)
    ).toEqual(['Other Workspace Name'])
  })
})
