// @vitest-environment jsdom
/**
 * WorkerPanelArea.test.jsx — flexible worker panels (multi-panel area).
 *
 * Covers the 5 required scenarios:
 *   1. MULTI-PANEL — >1 panel renders a tab strip with N tabs; ALL bodies stay
 *      mounted (focused visible display:block, others display:none); clicking
 *      a tab calls onFocus and moves the active class.
 *   2. REORDER/RESIZE/MAXIMIZE/PIN — chevrons call onMoveLeft/onMoveRight (no
 *      focus side-effect); resize drag reports a clamped width (250..600);
 *      maximize/pin buttons only render when their handlers are provided and
 *      toggle their titles; maximized body width becomes 100%.
 *   3. CLOSE-ONE-OF-MANY — closing a panel calls onClose(key) and the
 *      remaining panel stays mounted (single-panel fallback UI).
 *   4. SINGLE-INSTANCE FALLBACK UI — exactly 1 panel renders a plain
 *      .worker-output-panel with NO tab strip and NO maximize/pin chrome;
 *      0 panels renders null.
 *   5. App-level semantics — real App + WMP auto-open + row click produces 2
 *      panels; chevrons reorder; maximize/pin flip App state; closing the
 *      focused panel leaves the other mounted.
 */
import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor, act } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import WorkerPanelArea from '../WorkerPanelArea'
import useStore from '../../store/useStore'
import App from '../../App'
import useWorkspaceStore from '../../store/workspaceStore'
import useSessionTabsStore from '../../sessionTabsStore'

class MockResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

// ── Fixtures ────────────────────────────────────────────────────────────────
const panelA = {
  worker_name: 'wA',
  instance_id: null,
  instance_label: null,
  size: 350,
  maximized: false,
  pinned: false,
}
const panelB = {
  worker_name: 'wB',
  instance_id: null,
  instance_label: null,
  size: 350,
  maximized: false,
  pinned: false,
}

function baseProps(overrides = {}) {
  return {
    sessionId: 'sess-1',
    workspaceId: 'ws-test-1',
    panels: [panelA, panelB],
    focusedKey: 'wA#0',
    eventsByKey: {},
    onClose: vi.fn(),
    onFocus: vi.fn(),
    onResize: vi.fn(),
    onToggleMaximize: vi.fn(),
    onTogglePin: vi.fn(),
    onMoveLeft: vi.fn(),
    onMoveRight: vi.fn(),
    ...overrides,
  }
}

// ── DOM helpers ─────────────────────────────────────────────────────────────
const tabs = () => [...document.querySelectorAll('.worker-panel-tab')]
const tabLabels = () =>
  [...document.querySelectorAll('.worker-panel-tab-label')].map((el) => el.textContent)
const bodies = () => [...document.querySelectorAll('.worker-panel-body')]
const chevrons = () => [...document.querySelectorAll('.worker-panel-tab-chevron')]
const closeBtns = () => [...document.querySelectorAll('button[aria-label="Close panel"]')]
const headerText = (root) =>
  root.querySelector('.worker-output-header-label')?.textContent ?? null

// ── Setup / teardown (component level) ──────────────────────────────────────
beforeEach(() => {
  localStorage.clear()
  useStore.getState().reset()
  vi.stubGlobal('ResizeObserver', MockResizeObserver)
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('WorkerPanelArea (component)', () => {
  it('renders a tab strip for multiple panels, keeps all bodies mounted, and focuses on tab click', () => {
    const onFocus = vi.fn()
    const { rerender } = render(<WorkerPanelArea {...baseProps({ onFocus })} />)

    const panelRoot = document.querySelector('.worker-output-panel')
    expect(panelRoot).toHaveClass('multi')

    // Tab strip: 2 tabs in panel order, first active, titles from labels
    expect(tabs()).toHaveLength(2)
    expect(tabLabels()).toEqual(['wA', 'wB'])
    expect(tabs()[0]).toHaveClass('active')
    expect(tabs()[1]).not.toHaveClass('active')
    expect(tabs()[0]).toHaveAttribute('title', 'wA')

    // All bodies mounted; focused body visible AND first in DOM order
    expect(bodies()).toHaveLength(2)
    expect(bodies()[0].style.display).toBe('block')
    expect(bodies()[1].style.display).toBe('none')
    expect(headerText(bodies()[0])).toBe('Worker: wA')

    // Clicking the second tab focuses it (no rerender needed for the callback)
    fireEvent.click(tabs()[1])
    expect(onFocus).toHaveBeenCalledWith('wB#0')

    // After the parent updates focusedKey, active class + visible body move
    rerender(<WorkerPanelArea {...baseProps({ onFocus, focusedKey: 'wB#0' })} />)
    expect(tabs()[1]).toHaveClass('active')
    expect(tabs()[0]).not.toHaveClass('active')
    expect(bodies()[0].style.display).toBe('block')
    expect(headerText(bodies()[0])).toBe('Worker: wB')
    expect(bodies()[1].style.display).toBe('none')
  })

  it('chevrons call onMoveLeft/onMoveRight with disabled endpoints, without focusing', () => {
    const onMoveLeft = vi.fn()
    const onMoveRight = vi.fn()
    const onFocus = vi.fn()
    render(<WorkerPanelArea {...baseProps({ onMoveLeft, onMoveRight, onFocus })} />)

    const ch = chevrons()
    expect(ch).toHaveLength(4)
    // Tab0: left disabled (first), right enabled; Tab1: left enabled, right disabled
    expect(ch[0]).toBeDisabled()
    expect(ch[1]).not.toBeDisabled()
    expect(ch[2]).not.toBeDisabled()
    expect(ch[3]).toBeDisabled()

    fireEvent.click(ch[1])
    expect(onMoveRight).toHaveBeenCalledWith('wA#0')
    expect(onFocus).not.toHaveBeenCalled()

    fireEvent.click(ch[2])
    expect(onMoveLeft).toHaveBeenCalledWith('wB#0')
    expect(onFocus).not.toHaveBeenCalled()
  })

  it('resize drag reports a clamped width (250..600) to onResize with the panel key', () => {
    const onResize = vi.fn()
    render(<WorkerPanelArea {...baseProps({ onResize })} />)

    // First resize-handle in DOM belongs to the focused (wA) body
    const handle = document.querySelector('.resize-handle')

    // Drag far left (clientX 400 → 100): delta=-300 → 350+300=650 → clamp 600
    fireEvent.mouseDown(handle, { clientX: 400 })
    fireEvent.mouseMove(document, { clientX: 100 })
    fireEvent.mouseUp(document)
    expect(onResize).toHaveBeenLastCalledWith('wA#0', 600)

    // Drag far right (clientX 400 → 900): delta=500 → 350-500=-150 → clamp 250
    fireEvent.mouseDown(handle, { clientX: 400 })
    fireEvent.mouseMove(document, { clientX: 900 })
    fireEvent.mouseUp(document)
    expect(onResize).toHaveBeenLastCalledWith('wA#0', 250)
    expect(onResize).toHaveBeenCalledTimes(2)
  })

  it('maximize/pin controls only appear when handlers are provided and toggle state', () => {
    const { rerender } = render(
      <WorkerPanelArea {...baseProps({ onToggleMaximize: undefined, onTogglePin: undefined })} />
    )
    // Single-instance chrome hidden in multi mode too when handlers absent
    expect(document.querySelector('.worker-output-maximize-btn')).toBeNull()
    expect(document.querySelector('.worker-output-pin-btn')).toBeNull()

    const onToggleMaximize = vi.fn()
    const onTogglePin = vi.fn()
    rerender(<WorkerPanelArea {...baseProps({ onToggleMaximize, onTogglePin })} />)

    const maxBtn = document.querySelector('.worker-output-maximize-btn')
    const pinBtn = document.querySelector('.worker-output-pin-btn')
    expect(maxBtn).toHaveAttribute('title', 'Maximize panel')
    expect(pinBtn).toHaveAttribute('title', 'Pin panel')

    fireEvent.click(maxBtn)
    expect(onToggleMaximize).toHaveBeenCalledWith('wA#0')
    fireEvent.click(pinBtn)
    expect(onTogglePin).toHaveBeenCalledWith('wA#0')

    // Maximized panel → inner width 100%, button flips to Restore
    rerender(
      <WorkerPanelArea
        {...baseProps({ onToggleMaximize, onTogglePin, panels: [{ ...panelA, maximized: true }, panelB] })}
      />
    )
    const inner = document.querySelector('.worker-output-inner')
    expect(inner.style.width).toBe('100%')
    expect(document.querySelector('.worker-output-maximize-btn')).toHaveAttribute(
      'title',
      'Restore panel'
    )

    // Pinned panel → 📌 prefix in its tab label, pin button flips to Unpin
    rerender(
      <WorkerPanelArea
        {...baseProps({ onToggleMaximize, onTogglePin, panels: [{ ...panelA, pinned: true }, panelB] })}
      />
    )
    expect(tabLabels()[0]).toBe('📌 wA')
    expect(document.querySelector('.worker-output-pin-btn')).toHaveAttribute('title', 'Unpin panel')
  })

  it('closing a panel calls onClose and the remaining panel falls back to single-panel layout', () => {
    const onClose = vi.fn()
    const onFocus = vi.fn()
    const { rerender } = render(<WorkerPanelArea {...baseProps({ onClose, onFocus })} />)

    const btns = closeBtns()
    expect(btns).toHaveLength(2)
    fireEvent.click(btns[0])
    expect(onClose).toHaveBeenCalledWith('wA#0')
    expect(onFocus).not.toHaveBeenCalled()

    // Parent closes wA → only wB remains → plain single-panel fallback
    rerender(<WorkerPanelArea {...baseProps({ onClose, onFocus, panels: [panelB], focusedKey: 'wB#0' })} />)
    const root = document.querySelector('.worker-output-panel')
    expect(root).not.toHaveClass('multi')
    expect(document.querySelector('.worker-panel-tabs')).toBeNull()
    expect(bodies()).toHaveLength(0)
    // Single-panel close button carries no aria-label (tab close buttons only)
    expect(closeBtns()).toHaveLength(0)
    expect(headerText(root)).toBe('Worker: wB')
  })

  it('single panel → plain panel, no tab strip, no maximize/pin chrome', () => {
    render(<WorkerPanelArea {...baseProps({ panels: [panelA] })} />)
    const root = document.querySelector('.worker-output-panel')
    expect(root).not.toHaveClass('multi')
    expect(document.querySelector('.worker-panel-tabs')).toBeNull()
    expect(document.querySelector('.worker-output-maximize-btn')).toBeNull()
    expect(document.querySelector('.worker-output-pin-btn')).toBeNull()
    expect(headerText(root)).toBe('Worker: wA')
  })

  it('renders null when there are no panels (empty array or undefined)', () => {
    const { container, rerender } = render(<WorkerPanelArea {...baseProps({ panels: [] })} />)
    expect(container.firstChild).toBeNull()

    rerender(<WorkerPanelArea {...baseProps({ panels: undefined })} />)
    expect(container.firstChild).toBeNull()
  })

  it('uses instance_label in tabs/titles and instance-aware keys when present', () => {
    const panels = [
      { ...panelA, instance_id: 7, instance_label: 'Label A' },
      { ...panelB, instance_id: 8, instance_label: 'Label B' },
    ]
    render(<WorkerPanelArea {...baseProps({ panels, focusedKey: 'wB#8' })} />)

    expect(tabs()[0]).toHaveAttribute('title', 'Label A')
    expect(tabLabels()).toEqual(['Label A', 'Label B'])
    // focused = wB#8 → its body is first in DOM order and visible
    expect(bodies()[0].style.display).toBe('block')
    expect(headerText(bodies()[0])).toBe('Worker: Label B')
    expect(bodies()[1].style.display).toBe('none')
  })
})

// ── App-level integration (full harness, mirrors SessionWorkerIsolation) ────
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

const TWO_SESSIONS = [
  { session_id: 'sess-1', workspace_id: ENTRY.id, name: 'S1' },
  { session_id: 'sess-2', workspace_id: ENTRY.id, name: 'S2' },
]

const panelHeader = () =>
  document.querySelector('.worker-output-panel')
    ?.querySelector('.worker-output-header-label')?.textContent ?? null
const visibleBody = () =>
  [...document.querySelectorAll('.worker-panel-body')].find((b) => b.style.display === 'block')

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

async function selectWorker(name) {
  await waitFor(() => expect(screen.getAllByText(name).length).toBeGreaterThan(0))
  const row = screen.getAllByText(name).find((el) => !el.closest('.worker-output-panel'))
  expect(row).toBeTruthy()
  fireEvent.click(row)
  await waitFor(() => expect(panelHeader()).toBe(`Worker: ${name}`))
}

describe('multi-panel area in the real App', () => {
  beforeEach(() => {
    // Nested route: SessionTab body mounts only on session routes.
    window.location.hash = `#/workspace/${ENTRY.id}/session/sess-1`
    useWorkspaceStore.getState().reset()
    useWorkspaceStore.setState({ workspaceList: [{ ...ENTRY }] })
    useSessionTabsStore.getState().reset()
    MockWebSocket.instances = []
    vi.stubGlobal('WebSocket', MockWebSocket)
    stubBackend()
  })

  it('reorder / maximize / pin / close flow with two panels', async () => {
    render(<App />)
    await mountFirstTab()

    // WMP auto-opens wA once the workers fetch resolves
    await waitFor(() => expect(panelHeader()).toBe('Worker: wA'))

    // Click the wB row → second panel, focused
    await selectWorker('wB')
    await waitFor(() => expect(document.querySelectorAll('.worker-panel-tab')).toHaveLength(2))
    expect(tabLabels()).toEqual(['wA', 'wB'])

    // Move wA right via its right chevron (tab order [wA, wB] → [wB, wA])
    fireEvent.click(document.querySelectorAll('.worker-panel-tab-chevron')[1])
    await waitFor(() => expect(tabLabels()).toEqual(['wB', 'wA']))

    // Focused panel is wB — maximize it → inner width 100%
    fireEvent.click(visibleBody().querySelector('.worker-output-maximize-btn'))
    await waitFor(() =>
      expect(visibleBody().querySelector('.worker-output-inner').style.width).toBe('100%')
    )

    // Pin it → 📌 prefix on the (first) tab label
    fireEvent.click(visibleBody().querySelector('.worker-output-pin-btn'))
    await waitFor(() =>
      expect(document.querySelector('.worker-panel-tab-label').textContent).toBe('📌 wB')
    )

    // Close the focused wB panel → single wA panel remains, no multi chrome
    fireEvent.click(document.querySelectorAll('button[aria-label="Close panel"]')[0])
    await waitFor(() => expect(document.querySelectorAll('.worker-output-panel')).toHaveLength(1))
    const root = document.querySelector('.worker-output-panel')
    expect(root).not.toHaveClass('multi')
    expect(document.querySelector('.worker-panel-tabs')).toBeNull()
    expect(panelHeader()).toBe('Worker: wA')
  })
})
