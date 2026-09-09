// @vitest-environment jsdom
/*
 * SessionTabKeepMounted.test.jsx — Task 2: keep-mounted session deck.
 *
 * Renders the REAL App with a stubbed global WebSocket + fetch and drives
 * real tab clicks on the session strip to verify the keep-mounted deck:
 *
 *   1. panes stay mounted while hidden — a hidden pane keeps its WebSocket
 *      OPEN and preserves in-memory state (e.g. a draft typed into the
 *      query textarea) across A→B→A tab switches; switching back reuses the
 *      same WS (no remount, no new WebSocket instance);
 *   2. lazy mount — a tab's SessionTab mounts only when first activated;
 *      once mounted it stays in the deck and later switches only toggle
 *      visibility (.session-tab-pane / .session-tab-pane-hidden);
 *   3. workspace-scoped lifetime — leaving the session layer (back to the
 *      workspace route) unmounts the whole deck: panes disappear, their
 *      session WebSockets close (only the workspace hub stays OPEN), and
 *      re-entering the session layer mounts the active tab fresh again
 *      (draft is NOT resurrected — state is in-memory only).
 *
 * STATUS: written for the keep-mounted deck regression (Task 2).
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
  within,
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

const SUMMARY = {
  workspace_id: ENTRY.id, label: ENTRY.label, root_path: ENTRY.root,
  allow_host_resources: false, permissions: {}, resource_catalog: [],
  active_sessions: [], active_workers: [], active_containers: [],
  dockerfile: null, worker_templates: [], tools: [],
}

function stubBackend() {
  stubFetchByUrl({
    '/api/workspace/list': jsonOk([ENTRY, ENTRY2]),
    '/api/onboarding/status': jsonOk({ onboarding_complete: true }),
    [`/api/workspace/${ENTRY.id}/summary`]: jsonOk(SUMMARY),
    [`/api/workspace/${ENTRY2.id}/summary`]: jsonOk({ ...SUMMARY, workspace_id: ENTRY2.id, label: ENTRY2.label, root_path: ENTRY2.root }),
    '/api/global/summary': jsonOk({
      workspaces: [
        { id: ENTRY.id, label: ENTRY.label, root: ENTRY.root },
        { id: ENTRY2.id, label: ENTRY2.label, root: ENTRY2.root },
      ],
      active_sessions: [], active_containers: [], providers: [],
    }),
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
  await waitFor(() => { expect(MockWebSocket.instances.length).toBeGreaterThan(0) })
  const hub = MockWebSocket.instances[0]
  await act(async () => hub.open())
  return hub
}

// Change the hash the way a real navigation does (router re-parses on hashchange).
async function navigateHash(next) {
  await act(async () => {
    window.location.hash = next
    await new Promise((r) => setTimeout(r, 0))
  })
}

// ── Deck / pane helpers ──────────────────────────────────────────────────────
const tabBar = () => document.querySelector('.tab-bar')
const lastWs = () => MockWebSocket.instances[MockWebSocket.instances.length - 1]
const allPanes = () => Array.from(document.querySelectorAll('.session-tab-pane'))
const visiblePane = () => document.querySelector('.session-tab-pane:not(.session-tab-pane-hidden)')
const visibleTextarea = () => visiblePane()?.querySelector('textarea.query-input') || null
const openSockets = () => MockWebSocket.instances.filter((i) => i.readyState !== MockWebSocket.CLOSED)

// Opens the session WS (as the backend would) and confirms the session loaded,
// which makes SessionTab render its query bar.
async function openSession(ws, sessionId, name) {
  await act(async () => ws.open())
  act(() =>
    ws.receive({
      type: 'session_loaded',
      session_id: sessionId,
      workspace_id: ENTRY.id,
      session_name: name,
    })
  )
}

beforeEach(() => {
  localStorage.clear()
  useStore.getState().reset()
  useWorkspaceStore.getState().reset()
  useWorkspaceStore.setState({ workspaceList: [{ ...ENTRY }, { ...ENTRY2 }] })
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

describe('keep-mounted session deck (Task 2)', () => {
  it('keeps a hidden pane mounted and preserves its draft across A→B→A', async () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    window.location.hash = `#/workspace/${ENTRY.id}/session/sess-1`
    render(<App />)
    const hub = await connectHub()
    seedWorkspaceSessions(hub)
    await waitFor(() => { expect(MockWebSocket.instances.length).toBe(2) })
    const wsA = lastWs()
    await openSession(wsA, 'sess-1', 'S1')
    await waitFor(() => expect(visibleTextarea()).toBeTruthy())

    // Type a draft into S1's query bar.
    fireEvent.change(visibleTextarea(), { target: { value: 'draft A text' } })
    expect(visibleTextarea().value).toBe('draft A text')

    // Switch to S2 → S1's pane goes .session-tab-pane-hidden but STAYS mounted.
    fireEvent.click(within(tabBar()).getByText('S2'))
    await waitFor(() => expect(window.location.hash).toBe(`#/workspace/${ENTRY.id}/session/sess-2`))
    await waitFor(() => { expect(MockWebSocket.instances.length).toBe(3) })
    const wsB = lastWs()
    await openSession(wsB, 'sess-2', 'S2')
    await waitFor(() => expect(visibleTextarea()).toBeTruthy())

    let panes = allPanes()
    expect(panes.length).toBe(2)
    expect(panes[0].classList.contains('session-tab-pane-hidden')).toBe(true)
    expect(panes[0].querySelector('textarea.query-input').value).toBe('draft A text')
    expect(visiblePane()).toBe(panes[1])

    // Back to S1 → SAME pane + WS reused (no remount): still 3 instances.
    fireEvent.click(within(tabBar()).getByText('S1'))
    await waitFor(() => expect(window.location.hash).toBe(`#/workspace/${ENTRY.id}/session/sess-1`))
    await waitFor(() => expect(visibleTextarea()).toBeTruthy())
    expect(MockWebSocket.instances.length).toBe(3)
    panes = allPanes()
    expect(panes.length).toBe(2)
    expect(visiblePane()).toBe(panes[0])
    expect(panes[0].querySelector('textarea.query-input').value).toBe('draft A text')
    expect(errSpy).not.toHaveBeenCalled()
    errSpy.mockRestore()
  })

  it('lazy mount: a tab mounts on first activation; later switches only toggle visibility', async () => {
    window.location.hash = `#/workspace/${ENTRY.id}/session/sess-1`
    render(<App />)
    const hub = await connectHub()
    seedWorkspaceSessions(hub)
    await waitFor(() => { expect(MockWebSocket.instances.length).toBe(2) })
    expect(document.querySelector('.tab-wrapper')).toBeTruthy()
    expect(allPanes().length).toBe(1)

    // First visit to S2 mounts its pane (new WS instance).
    fireEvent.click(within(tabBar()).getByText('S2'))
    await waitFor(() => { expect(MockWebSocket.instances.length).toBe(3) })
    expect(allPanes().length).toBe(2)

    // Switch-backs reuse the mounted panes — no further WS instances.
    fireEvent.click(within(tabBar()).getByText('S1'))
    await waitFor(() => expect(window.location.hash).toBe(`#/workspace/${ENTRY.id}/session/sess-1`))
    expect(MockWebSocket.instances.length).toBe(3)
    expect(allPanes().length).toBe(2)

    fireEvent.click(within(tabBar()).getByText('S2'))
    await waitFor(() => expect(window.location.hash).toBe(`#/workspace/${ENTRY.id}/session/sess-2`))
    expect(MockWebSocket.instances.length).toBe(3)
    expect(allPanes().length).toBe(2)
  })

  it('leaving the session layer unmounts the deck (workspace-scoped lifetime)', async () => {
    window.location.hash = `#/workspace/${ENTRY.id}/session/sess-1`
    render(<App />)
    const hub = await connectHub()
    seedWorkspaceSessions(hub)
    await waitFor(() => { expect(MockWebSocket.instances.length).toBe(2) })
    const wsA = lastWs()
    await openSession(wsA, 'sess-1', 'S1')
    await waitFor(() => expect(visibleTextarea()).toBeTruthy())
    fireEvent.change(visibleTextarea(), { target: { value: 'draft A text' } })

    // Mount S2 too, so the deck holds both panes before we leave the layer.
    fireEvent.click(within(tabBar()).getByText('S2'))
    await waitFor(() => { expect(MockWebSocket.instances.length).toBe(3) })
    const wsB = lastWs()
    await openSession(wsB, 'sess-2', 'S2')
    await waitFor(() => expect(visibleTextarea()).toBeTruthy())
    expect(allPanes().length).toBe(2)

    // Back to the workspace route → the whole deck unmounts; both session
    // WebSockets close. Only the workspace hub stays OPEN.
    await navigateHash(`#/workspace/${ENTRY.id}`)
    await waitFor(() => expect(document.querySelector('.tab-wrapper')).toBeNull())
    await waitFor(() => { expect(openSockets().length).toBe(1) })
    expect(openSockets()[0]).toBe(hub)

    // Re-enter the session layer: the active tab mounts FRESH (new WS), the
    // deck starts with a single pane, and the old draft is NOT resurrected
    // (pane state is in-memory only, wiped by the unmount).
    await navigateHash(`#/workspace/${ENTRY.id}/session/sess-1`)
    await waitFor(() => { expect(openSockets().length).toBe(2) })
    const wsC = lastWs()
    expect(wsC).not.toBe(wsA)
    await waitFor(() => { expect(allPanes().length).toBe(1) })
    await openSession(wsC, 'sess-1', 'S1')
    await waitFor(() => expect(visibleTextarea()).toBeTruthy())
    expect(visibleTextarea().value).toBe('')
  })
})
