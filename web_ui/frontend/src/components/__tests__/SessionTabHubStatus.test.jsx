// @vitest-environment jsdom
/*
 * SessionTabHubStatus.test.jsx — TASK 3: tab-strip live status via hub relay.
 *
 * Renders the REAL App on the workspace route (#/workspace/<id>) — where the
 * tab strip is visible but NO SessionTab / per-session WebSocket is mounted —
 * and verifies that hub-relayed state_changed events keep the strip's status
 * classes and the store (tabRunningStates / sessionStates) in sync:
 *
 *   - state_changed {session_id, state:'PAUSED'} paints .paused on the tab
 *     (distinct from .idle / .pausing / .running) and records
 *     tabRunningStates[sid] === 'PAUSED' + sessionStates[sid] (isRunning false);
 *   - live transitions RUNNING → PAUSING → PAUSED → IDLE update the class and
 *     store each step (isRunning true only while RUNNING);
 *   - events missing session_id or state are silently ignored (no crash, no
 *     store write, no console.error);
 *   - state_changed for a session owned by ANOTHER workspace never paints the
 *     current strip (workspace scoping via the session→workspace mapping);
 *   - state_changed arriving BEFORE the sessions list still records the state
 *     (tab later renders with the correct class — no ordering dependency).
 */
import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor, act } from '@testing-library/react'
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

function sendStateChanged(hub, payload) {
  act(() => hub.receive({ type: 'state_changed', ...payload }))
}

async function connectHub() {
  await waitFor(() => { expect(MockWebSocket.instances.length).toBeGreaterThan(0) })
  const hub = MockWebSocket.instances[0]
  await act(async () => hub.open())
  return hub
}

// The strip's TabBar items carry title=name; scope to .tab-item (the WDP has
// its own role="tab" sub-tabs we must not confuse with session tabs).
function tabItem(label) {
  return Array.from(document.querySelectorAll('.tab-item')).find(
    (el) => el.querySelector('.tab-label')?.textContent === label
  )
}

async function findTabItem(label) {
  await waitFor(() => expect(tabItem(label)).toBeTruthy())
  return tabItem(label)
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

describe('Tab-strip live status via hub-relayed state_changed (workspace route)', () => {
  it('initially idle; PAUSED paints .paused and records PAUSED in the store', async () => {
    window.location.hash = `#/workspace/${ENTRY.id}`
    render(<App />)
    const hub = await connectHub()
    seedWorkspaceSessions(hub)
    // No status yet → the strip falls back to the idle class, store untouched.
    const idleTab = await findTabItem('S1')
    expect(idleTab.classList.contains('idle')).toBe(true)
    expect(useStore.getState().tabRunningStates['sess-1']).toBeUndefined()

    sendStateChanged(hub, { session_id: 'sess-1', state: 'PAUSED', is_running: false })

    await waitFor(() => {
      const tab = tabItem('S1')
      expect(tab.classList.contains('paused')).toBe(true)
      expect(tab.classList.contains('idle')).toBe(false)
      expect(tab.classList.contains('pausing')).toBe(false)
      expect(tab.classList.contains('running')).toBe(false)
    })
    expect(useStore.getState().tabRunningStates['sess-1']).toBe('PAUSED')
    expect(useStore.getState().sessionStates['sess-1']).toMatchObject({ state: 'PAUSED', isRunning: false })
  })

  it('transitions RUNNING → PAUSING → PAUSED → IDLE update class + store each step', async () => {
    window.location.hash = `#/workspace/${ENTRY.id}`
    render(<App />)
    const hub = await connectHub()
    seedWorkspaceSessions(hub)
    await findTabItem('S1')

    sendStateChanged(hub, { session_id: 'sess-1', state: 'RUNNING', is_running: true })
    await waitFor(() => {
      expect(tabItem('S1').classList.contains('running')).toBe(true)
      expect(useStore.getState().tabRunningStates['sess-1']).toBe('RUNNING')
      expect(useStore.getState().sessionStates['sess-1'].isRunning).toBe(true)
    })

    // PAUSING is an interim state → dedicated .pausing class, not running.
    sendStateChanged(hub, { session_id: 'sess-1', state: 'PAUSING', is_running: false })
    await waitFor(() => {
      const tab = tabItem('S1')
      expect(tab.classList.contains('pausing')).toBe(true)
      expect(tab.classList.contains('running')).toBe(false)
      expect(tab.classList.contains('paused')).toBe(false)
      expect(useStore.getState().tabRunningStates['sess-1']).toBe('PAUSING')
      expect(useStore.getState().sessionStates['sess-1'].isRunning).toBe(false)
    })

    // Fully paused → amber .paused (distinct from the yellow .pausing).
    sendStateChanged(hub, { session_id: 'sess-1', state: 'PAUSED', is_running: false })
    await waitFor(() => {
      const tab = tabItem('S1')
      expect(tab.classList.contains('paused')).toBe(true)
      expect(tab.classList.contains('pausing')).toBe(false)
      expect(useStore.getState().sessionStates['sess-1'].state).toBe('PAUSED')
    })

    sendStateChanged(hub, { session_id: 'sess-1', state: 'IDLE', is_running: false })
    await waitFor(() => {
      const tab = tabItem('S1')
      expect(tab.classList.contains('idle')).toBe(true)
      expect(tab.classList.contains('paused')).toBe(false)
      expect(useStore.getState().sessionStates['sess-1'].state).toBe('IDLE')
    })
  })

  it('state_changed without session_id or state is silently ignored', async () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    window.location.hash = `#/workspace/${ENTRY.id}`
    render(<App />)
    const hub = await connectHub()
    seedWorkspaceSessions(hub)
    await findTabItem('S1')

    sendStateChanged(hub, { state: 'PAUSED', is_running: false })          // no session_id
    sendStateChanged(hub, { session_id: 'sess-1', is_running: true })      // no state
    sendStateChanged(hub, { session_id: 'sess-1', state: '', is_running: true }) // empty state

    expect(useStore.getState().tabRunningStates['sess-1']).toBeUndefined()
    expect(useStore.getState().sessionStates['sess-1']).toBeUndefined()
    await waitFor(() => expect(tabItem('S1').classList.contains('idle')).toBe(true))
    expect(errSpy).not.toHaveBeenCalled()
    errSpy.mockRestore()
  })

  it('state_changed for another workspace session never paints the current strip', async () => {
    const THREE_SESSIONS = [
      { session_id: 'sess-1', workspace_id: ENTRY.id, name: 'S1' },
      { session_id: 'sess-2', workspace_id: ENTRY.id, name: 'S2' },
      { session_id: 'sess-3', workspace_id: ENTRY2.id, name: 'S3' },
    ]
    window.location.hash = `#/workspace/${ENTRY.id}`
    render(<App />)
    const hub = await connectHub()
    act(() => hub.receive({ type: 'sessions_list', sessions: THREE_SESSIONS }))
    act(() =>
      hub.receive({
        type: 'open_sessions',
        sessions: THREE_SESSIONS.map(({ session_id, name }) => ({ session_id, name })),
      })
    )
    await findTabItem('S1')

    sendStateChanged(hub, { session_id: 'sess-3', state: 'RUNNING', is_running: true })
    await new Promise((r) => setTimeout(r, 0))

    // The visible strip (workspace ENTRY) is untouched...
    const tab = tabItem('S1')
    expect(tab.classList.contains('running')).toBe(false)
    expect(tab.classList.contains('idle')).toBe(true)
    // ...and the foreign session's state never leaks into the store.
    expect(useStore.getState().tabRunningStates['sess-3']).toBeUndefined()
    expect(useStore.getState().sessionStates['sess-3']).toBeUndefined()
  })

  it('state_changed arriving before the sessions list still records state (no ordering dependency)', async () => {
    window.location.hash = `#/workspace/${ENTRY.id}`
    render(<App />)
    const hub = await connectHub()
    // Event first — no sessions known yet (only the route-derived currentWs).
    sendStateChanged(hub, { session_id: 'sess-1', state: 'PAUSED', is_running: false })
    expect(useStore.getState().tabRunningStates['sess-1']).toBe('PAUSED')

    seedWorkspaceSessions(hub)
    // Tab created afterwards renders with the already-known paused status.
    await waitFor(() => {
      const tab = tabItem('S1')
      expect(tab).toBeTruthy()
      expect(tab.classList.contains('paused')).toBe(true)
      expect(useStore.getState().sessionStates['sess-1'].state).toBe('PAUSED')
    })
  })
})
