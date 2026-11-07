// @vitest-environment jsdom
/*
 * SessionTabsWorkspaceBucketing.test.jsx — regression cover for the
 * "every workspace's open sessions piled into one tab strip" bug.
 *
 * `open_sessions` is GLOBAL: the server lists every open session from every
 * workspace (open_sessions.json is not workspace-scoped). App.jsx used to
 * resolve an unknown workspace with `lastKnownWorkspaceRef || currentWsRef`,
 * i.e. "whatever workspace is on screen", so ~50 sessions from all workspaces
 * landed in a single strip.
 *
 * The contract asserted here:
 *   1. an `open_sessions` entry whose workspace is unknown (no workspace_id,
 *      no sessions_list mapping) creates NO tab — the resolver fails CLOSED;
 *   2. an entry carrying workspace_id is bucketed into ITS workspace only;
 *   3. a stale tab filed under the wrong strip (e.g. persisted in localStorage
 *      by an earlier build) is repaired — removed from the wrong strip.
 */
import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor, act, within } from '@testing-library/react'
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

  send(data) { this.sent.push(data) }
  close(code = 1001) { this.readyState = MockWebSocket.CLOSED; this.onclose?.({ code }) }
  open() { this.readyState = MockWebSocket.OPEN; this.onopen?.({}) }
  receive(msg) { this.onmessage?.({ data: JSON.stringify(msg) }) }
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
const ENTRY2 = { id: 'ws-2', label: 'Second Workspace', root: '~/workspaces/ws-2' }

const ENTRY_STORE = { id: ENTRY.id, name: ENTRY.label, path: ENTRY.root, root: ENTRY.root }
const ENTRY2_STORE = { id: ENTRY2.id, name: ENTRY2.label, path: ENTRY2.root, root: ENTRY2.root }

const SUMMARY = {
  workspace_id: ENTRY.id, label: ENTRY.label, root_path: ENTRY.root,
  allow_host_resources: false, permissions: {}, resource_catalog: [],
  active_sessions: [], active_workers: [], containers_in_use: [], containers_listed: 0,
  dockerfile: null, worker_templates: [], tools: [],
}

function stubBackend() {
  stubFetchByUrl({
    '/api/workspace/list': jsonOk([ENTRY, ENTRY2]),
    '/api/onboarding/status': jsonOk({ onboarding_complete: true }),
    [`/api/workspace/${ENTRY.id}/summary`]: jsonOk(SUMMARY),
    [`/api/workspace/${ENTRY2.id}/summary`]: jsonOk({
      ...SUMMARY, workspace_id: ENTRY2.id, label: ENTRY2.label, root_path: ENTRY2.root,
    }),
    '/api/global/summary': jsonOk({
      workspaces: [
        { id: ENTRY.id, label: ENTRY.label, root: ENTRY.root },
        { id: ENTRY2.id, label: ENTRY2.label, root: ENTRY2.root },
      ],
      active_sessions: [], containers_in_use: [], containers_listed: 0, providers: [],
    }),
  })
}

async function connectHub() {
  await waitFor(() => { expect(MockWebSocket.instances.length).toBeGreaterThan(0) })
  const hub = MockWebSocket.instances[0]
  await act(async () => hub.open())
  return hub
}

function tabsFor(ws) {
  return (useSessionTabsStore.getState().byWorkspace[ws]?.tabs || []).map((t) => t.sessionId)
}

function persistedTabs(ws) {
  try {
    const raw = localStorage.getItem(`tm.sessionTabs.${ws}`)
    if (!raw) return null
    return (JSON.parse(raw).tabs || []).map((t) => t.sessionId)
  } catch {
    return null
  }
}

beforeEach(() => {
  localStorage.clear()
  useStore.getState().reset()
  useWorkspaceStore.getState().reset()
  useWorkspaceStore.setState({ workspaceList: [{ ...ENTRY_STORE }, { ...ENTRY2_STORE }] })
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

describe('open_sessions workspace bucketing', () => {
  it('fails closed: unknown-workspace sessions never land in the on-screen strip', async () => {
    window.location.hash = `#/workspace/${ENTRY.id}`
    render(<App />)
    const hub = await connectHub()
    await screen.findByRole('tab', { name: 'Permissions & Resources' })
    // No sessions_list, no workspace_id — the owning workspace is unknowable.
    act(() =>
      hub.receive({
        type: 'open_sessions',
        sessions: [
          { session_id: 'orphan-1', name: 'Orphan One' },
          { session_id: 'orphan-2', name: 'Orphan Two' },
        ],
      })
    )
    await waitFor(() => { expect(screen.queryByTitle('Orphan One')).toBeNull() })
    expect(screen.queryByTitle('Orphan Two')).toBeNull()
    expect(tabsFor(ENTRY.id)).toEqual([])
    // Nothing was persisted for ANY strip either — a guess would have written one.
    expect(persistedTabs(ENTRY.id)).toBeNull()
  })

  it('buckets each session into the workspace the server reports', async () => {
    window.location.hash = `#/workspace/${ENTRY.id}`
    render(<App />)
    const hub = await connectHub()
    await screen.findByRole('tab', { name: 'Permissions & Resources' })
    act(() =>
      hub.receive({
        type: 'open_sessions',
        sessions: [
          { session_id: 'a-1', workspace_id: ENTRY.id, name: 'A One' },
          { session_id: 'b-1', workspace_id: ENTRY2.id, name: 'B One' },
        ],
      })
    )
    await waitFor(() => expect(screen.getByTitle('A One')).toBeInTheDocument())
    // The other workspace's session is NOT in this strip…
    expect(screen.queryByTitle('B One')).toBeNull()
    expect(tabsFor(ENTRY.id)).toEqual(['a-1'])
    // …it is held in its own workspace's strip.
    expect(tabsFor(ENTRY2.id)).toEqual(['b-1'])
    expect(persistedTabs(ENTRY2.id)).toEqual(['b-1'])
  })

  it('repairs a stale tab persisted under the wrong strip', async () => {
    window.location.hash = `#/workspace/${ENTRY.id}`
    // An earlier build filed a second workspace's session under this strip.
    localStorage.setItem(
      `tm.sessionTabs.${ENTRY.id}`,
      JSON.stringify({
        v: 1,
        tabs: [
          { sessionId: 'foreign-1', title: 'Foreign' },
          { sessionId: 'home-1', title: 'Home' },
        ],
        activeSessionId: 'foreign-1',
      })
    )
    render(<App />)
    const hub = await connectHub()
    await screen.findByRole('tab', { name: 'Permissions & Resources' })
    act(() =>
      hub.receive({
        type: 'open_sessions',
        sessions: [
          { session_id: 'foreign-1', workspace_id: ENTRY2.id, name: 'Foreign' },
          { session_id: 'home-1', workspace_id: ENTRY.id, name: 'Home' },
        ],
      })
    )
    // The foreign tab is gone from the strip (in memory AND in storage) and
    // the active tab falls back to the session this workspace really owns.
    await waitFor(() => expect(tabsFor(ENTRY.id)).toEqual(['home-1']))
    expect(useSessionTabsStore.getState().byWorkspace[ENTRY.id].activeSessionId).toBe('home-1')
    expect(persistedTabs(ENTRY.id)).toEqual(['home-1'])
    expect(screen.queryByTitle('Foreign')).toBeNull()
    expect(screen.getByTitle('Home')).toBeInTheDocument()
    // …and it now lives in the workspace that owns it.
    expect(tabsFor(ENTRY2.id)).toEqual(['foreign-1'])
    expect(within(screen.getByTestId('layer-nav')).getByText('Code Development'))
      .toHaveAttribute('aria-current', 'page')
  })
})
