// @vitest-environment jsdom
/*
 * DeepLinkSessionValidation.test.jsx — App-level coverage for the boundary-layer
 * deep-link admission check.
 *
 * A bare session deep link (#/session/<id>) carries no workspace, so App used to
 * open a tab for ANY session id. App now probes
 * /api/session/list?workspace_id=<resolved ws> first (tri-state, fail-open):
 *   (i)  the session IS listed        → the tab is created and activated
 *   (ii) the session is NOT listed    → no tab is created, a warning is logged
 *                                       and the user lands on the workspace
 *                                       SELECTOR (never on the workspace route
 *                                       supplied in the URL — that id is itself
 *                                       client-supplied).
 */
import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, waitFor, act, cleanup } from '@testing-library/react'
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

const LIST_KEY = `/api/session/list?workspace_id=${ENTRY.id}`

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

function tabsFor(wsId) {
  return useSessionTabsStore.getState().byWorkspace[wsId]?.tabs || []
}

describe('App deep link — session membership validation', () => {
  it('opens the tab when the workspace listing confirms the session', async () => {
    // BARE deep link (no workspace in the URL): App has to learn the workspace
    // from sessions_list and then ask the listing whether sess-1 is in it.
    window.location.hash = '#/session/sess-1'
    stubBackend(ENTRY, {
      [LIST_KEY]: jsonOk([{ session_id: 'sess-1', workspace_id: ENTRY.id, name: 'S1' }]),
    })
    render(<App />)
    const hub = await connectHub()
    await act(async () =>
      hub.receive({
        type: 'sessions_list',
        sessions: [{ session_id: 'sess-1', workspace_id: ENTRY.id, name: 'S1' }],
      })
    )
    // Nothing is open server-side — the tab can only come from the deep link,
    // and only after the probe answered `true`.
    await act(async () => hub.receive({ type: 'open_sessions', sessions: [] }))

    await waitFor(() => {
      expect(tabsFor(ENTRY.id).map((t) => t.sessionId)).toEqual(['sess-1'])
      expect(useSessionTabsStore.getState().byWorkspace[ENTRY.id].activeSessionId).toBe('sess-1')
    })
    // The admitted tab really mounts (hub + 1 SessionTab WS).
    await waitFor(() => {
      expect(MockWebSocket.instances.length).toBe(2)
    })
  })

  it('refuses the deep link when the listing does not contain the session', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    window.location.hash = '#/session/sess-1'
    stubBackend(ENTRY, {
      // The workspace exists and answers — sess-1 is simply not in it.
      [LIST_KEY]: jsonOk([{ session_id: 'sess-other', workspace_id: ENTRY.id, name: 'Other' }]),
    })
    render(<App />)
    const hub = await connectHub()
    await act(async () =>
      hub.receive({
        type: 'sessions_list',
        sessions: [{ session_id: 'sess-1', workspace_id: ENTRY.id, name: 'S1' }],
      })
    )
    await act(async () => hub.receive({ type: 'open_sessions', sessions: [] }))

    // Refused → the selector, not a workspace route (the URL id is client-supplied).
    await waitFor(() => {
      expect(window.location.hash).toBe('#/workspaces')
    })
    // No tab was ever created for the foreign session.
    expect(tabsFor(ENTRY.id)).toHaveLength(0)
    expect(warnSpy).toHaveBeenCalled()
    // No SessionTab WS was mounted for it either (hub only).
    expect(MockWebSocket.instances.length).toBe(1)
    warnSpy.mockRestore()
  })
})
