// @vitest-environment jsdom
/*
 * Navigation.test.jsx — TASK 1: three-layer navigation
 * (workspaces → workspace → session) rendered through the REAL App.
 *
 * Renders App with stubbed fetch + WebSocket and drives real hash navigation
 * to verify the three-layer model end-to-end:
 *
 *   workspaces (selector)  →  workspace (WorkspaceDetailPage)  →  session (SessionTab)
 *
 *   - the shared App-level <LayerNav> breadcrumb carries the back affordances:
 *     a 'Workspaces' crumb links to the selector (#/workspaces);
 *   - the workspace crumb links to the OWNING
 *     workspace — the nested session URL (#/workspace/:wsId/session/:sid)
 *     carries the owning workspaceId explicitly (route.workspaceId), so the
 *     back button reaches the workspace level immediately, even before any
 *     session_loaded / workspace_path matching could supply it;
 *   - browser back/forward works because every navigation is a hash assignment
 *     (its own history entry) and the router re-parses on each hashchange;
 *   - session tabs are workspace-scoped in sessionTabsStore.byWorkspace —
 *     tabs opened under workspace A are never listed under workspace B.
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

// The workspaceStore does NOT expose the raw backend entry. fetchWorkspaces
// maps `{ id, label, root }` → `{ id, name: label || id, path, root }`
// (see workspaceStore.js), and App reads `.name` off workspaceList. Seed the
// store with the REAL mapped shape — seeding only `.label` bypassed the
// contract and hid a would-be crumb regression.
const ENTRY_STORE = { id: ENTRY.id, name: ENTRY.label, path: ENTRY.root, root: ENTRY.root }
const ENTRY2_STORE = { id: ENTRY2.id, name: ENTRY2.label, path: ENTRY2.root, root: ENTRY2.root }

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

describe('Three-layer navigation: workspaces → workspace → session', () => {
  it('selector route (#/workspaces): landing renders cards, no workspace/session chrome', async () => {
    window.location.hash = '#/workspaces'
    render(<App />)
    expect(await screen.findByText('Global Management')).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: /Code Development/ })).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: /Second Workspace/ })).toBeInTheDocument()
    expect(document.querySelector('.wdp-panel')).toBeNull()
    expect(screen.queryByTitle('S1')).toBeNull()
    expect(document.querySelector('.tab-wrapper')).toBeNull()
  })

  it('workspace route: detail page carries a back-to-workspaces link returning to the selector', async () => {
    window.location.hash = `#/workspace/${ENTRY.id}`
    render(<App />)
    const hub = await connectHub()
    seedWorkspaceSessions(hub)
    await screen.findByRole('tab', { name: 'Permissions & Resources' })
    // Detail title comes from the summary payload (`.label`).
    await waitFor(() => expect(document.querySelector('.wdp-title')).toHaveTextContent('Code Development'))
    // The breadcrumb workspace crumb must pin the REAL name from the store
    // (`workspaceList[].name`), never the 'Workspace' fallback literal.
    const navWsB = within(screen.getByTestId('layer-nav'))
    expect(navWsB.getByText('Code Development')).toHaveAttribute('aria-current', 'page')
    expect(navWsB.queryByText('Workspace')).toBeNull()
    const backLink = within(screen.getByTestId('layer-nav')).getByRole('link', { name: 'Workspaces' })
    expect(backLink).toHaveAttribute('href', '#/workspaces')
    fireEvent.click(backLink)
    await waitFor(() => expect(window.location.hash).toBe('#/workspaces'))
    expect(await screen.findByText('Global Management')).toBeInTheDocument()
    expect(document.querySelector('.wdp-panel')).toBeNull()
  })

  it('nested session route: Back to Workspace goes to the owning workspace, not the selector', async () => {
    window.location.hash = `#/workspace/${ENTRY.id}/session/sess-1`
    render(<App />)
    const hub = await connectHub()
    seedWorkspaceSessions(hub)
    await waitFor(() => { expect(MockWebSocket.instances.length).toBe(2) })
    expect(document.querySelector('.tab-wrapper')).toBeTruthy()
    const tabWs = MockWebSocket.instances[1]
    expect(tabWs).not.toBe(hub)
    await act(async () => tabWs.open())
    act(() => tabWs.receive({ type: 'session_loaded', session_id: 'sess-1', workspace_id: ENTRY.id, session_name: 'S1' }))
    const nav = await screen.findByTestId('layer-nav')
    const backBtn = within(nav)
      .getAllByRole('link')
      .find((a) => a.getAttribute('href') === `#/workspace/${ENTRY.id}`)
    expect(backBtn).toBeTruthy()
    // The workspace crumb link is labelled by the REAL workspace name.
    expect(backBtn).toHaveTextContent('Code Development')
    fireEvent.click(backBtn)
    await waitFor(() => expect(window.location.hash).toBe(`#/workspace/${ENTRY.id}`))
    expect(await screen.findByRole('tab', { name: 'Permissions & Resources' })).toBeInTheDocument()
    expect(document.querySelector('.tab-wrapper')).toBeNull()
    const navWs = screen.getByTestId('layer-nav')
    // Workspace view: crumb is the CURRENT (non-link) node showing the REAL name.
    expect(within(navWs).getByText('Code Development')).toHaveAttribute('aria-current', 'page')
    expect(within(navWs).queryByText('Workspace')).toBeNull()
    expect(within(navWs).getByRole('link', { name: 'Workspaces' })).toBeInTheDocument()
  })

  it('browser back/forward retraces session → workspace → selector and forward again', async () => {
    window.location.hash = '#/workspaces'
    render(<App />)
    expect(await screen.findByText('Global Management')).toBeInTheDocument()
    const hub = await connectHub()
    seedWorkspaceSessions(hub)
    // Layer 3 → 2: open the workspace detail from the selector card.
    fireEvent.click(await screen.findByRole('button', { name: /Code Development/ }))
    await waitFor(() => expect(window.location.hash).toBe(`#/workspace/${ENTRY.id}`))
    expect(await screen.findByRole('tab', { name: 'Permissions & Resources' })).toBeInTheDocument()
    // Layer 2 → 1: open session S1 from the workspace strip.
    fireEvent.click(screen.getByTitle('S1'))
    await waitFor(() => expect(window.location.hash).toBe(`#/workspace/${ENTRY.id}/session/sess-1`))
    await waitFor(() => { expect(MockWebSocket.instances.length).toBe(2) })
    const tabWs = MockWebSocket.instances[1]
    expect(tabWs).not.toBe(hub)
    await act(async () => tabWs.open())
    act(() => tabWs.receive({ type: 'session_loaded', session_id: 'sess-1', workspace_id: ENTRY.id, session_name: 'S1' }))
    const navBf = await screen.findByTestId('layer-nav')
    expect(
      within(navBf)
        .getAllByRole('link')
        .some((a) => a.getAttribute('href') === `#/workspace/${ENTRY.id}`)
    ).toBe(true)
    // Browser back: session → workspace.
    await act(async () => { window.history.back(); await new Promise((r) => setTimeout(r, 30)) })
    await waitFor(() => expect(window.location.hash).toBe(`#/workspace/${ENTRY.id}`))
    expect(await screen.findByRole('tab', { name: 'Permissions & Resources' })).toBeInTheDocument()
    expect(document.querySelector('.tab-wrapper')).toBeNull()
    // Browser back: workspace → selector.
    await act(async () => { window.history.back(); await new Promise((r) => setTimeout(r, 30)) })
    await waitFor(() => expect(window.location.hash).toBe('#/workspaces'))
    expect(await screen.findByText('Global Management')).toBeInTheDocument()
    // Browser forward: selector → workspace → session again.
    await act(async () => { window.history.forward(); await new Promise((r) => setTimeout(r, 30)) })
    await waitFor(() => expect(window.location.hash).toBe(`#/workspace/${ENTRY.id}`))
    expect(await screen.findByRole('tab', { name: 'Permissions & Resources' })).toBeInTheDocument()
    await act(async () => { window.history.forward(); await new Promise((r) => setTimeout(r, 30)) })
    await waitFor(() => expect(window.location.hash).toBe(`#/workspace/${ENTRY.id}/session/sess-1`))
    expect(document.querySelector('.tab-wrapper')).toBeTruthy()
  })

  it('tabs are workspace-scoped: sessions of one workspace never appear in another workspace strip', async () => {
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
    // Workspace A strip lists only A's tabs.
    await screen.findByRole('tab', { name: 'Permissions & Resources' })
    await waitFor(() => expect(screen.getByTitle('S1')).toBeInTheDocument())
    expect(screen.getByTitle('S2')).toBeInTheDocument()
    expect(screen.queryByTitle('S3')).toBeNull()
    // Workspace B strip lists only B's tabs.
    await navigateHash(`#/workspace/${ENTRY2.id}`)
    await waitFor(() => expect(document.querySelector('.wdp-title')).toHaveTextContent('Second Workspace'))
    expect(within(screen.getByTestId('layer-nav')).getByText('Second Workspace')).toHaveAttribute('aria-current', 'page')
    await waitFor(() => expect(screen.getByTitle('S3')).toBeInTheDocument())
    expect(screen.queryByTitle('S1')).toBeNull()
    expect(screen.queryByTitle('S2')).toBeNull()
  })
})
