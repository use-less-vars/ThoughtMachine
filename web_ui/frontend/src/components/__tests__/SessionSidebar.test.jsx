// @vitest-environment jsdom
/*
 * SessionSidebar.test.jsx — session details slide-out panel tests (Phase 4).
 *
 * Tests the REAL SessionSidebar (src/components/SessionSidebar.jsx) against
 * the real workspaceStore + useStore with a stubbed global fetch (same
 * pattern as SessionTab.test.jsx / workspaceStore.test.js).
 *
 * DEVIATIONS FROM THE ORIGINAL SPEC (matched to real code):
 *   - Spec named startContainer/stopContainer store actions; the real store
 *     exposes a single containerAction(id, name, 'start'|'stop'|'remove').
 *   - Spec called for a "containers used" counter in the sidebar; the real
 *     component has none (asserted absent in the containers test).
 *   - Stop-worker endpoint in the spec matches the real code:
 *     POST /api/workspace/{ws}/workers/{name}/stop.
 */

import React from 'react'
import {
  describe,
  it,
  expect,
  vi,
  beforeEach,
  afterEach,
} from 'vitest'
import {
  render,
  screen,
  fireEvent,
  cleanup,
  act,
  waitFor,
  within,
} from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import SessionSidebar from '../SessionSidebar'
import SessionTab from '../SessionTab'
import useWorkspaceStore from '../../store/workspaceStore'
import useStore from '../../store/useStore'

// ---------------------------------------------------------------------------
// Mock WebSocket (SessionTab wiring test) + ResizeObserver (jsdom lacks it)
// ---------------------------------------------------------------------------
class MockWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  static instances = [];

  constructor(url) {
    this.url = url;
    this.readyState = MockWebSocket.CONNECTING;
    this.sent = [];
    this.onopen = null;
    this.onmessage = null;
    this.onclose = null;
    this.onerror = null;
    MockWebSocket.instances.push(this);
  }

  send(data) {
    this.sent.push(data);
  }

  close(code = 1001) {
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.({ code });
  }

  open() {
    this.readyState = MockWebSocket.OPEN;
    this.onopen?.({});
  }

  receive(msg) {
    this.onmessage?.({ data: JSON.stringify(msg) });
  }
}

class MockResizeObserver {
  constructor(callback) {
    this.callback = callback;
  }
  observe() {}
  unobserve() {}
  disconnect() {}
}

// ---------------------------------------------------------------------------
// fetch stubs — substring routes, longest key wins
// ---------------------------------------------------------------------------
function jsonOk(data, status = 200) {
  return { ok: true, status, json: async () => data, text: async () => JSON.stringify(data) }
}

function jsonErr(detail, status = 500) {
  return { ok: false, status, json: async () => ({ detail }), text: async () => JSON.stringify({ detail }) }
}

// Default fallback for URLs these tests don't care about (e.g. the
// ConfigPanel /api/tools fetch when the panel mounts inside SessionTab) —
// mirrors SessionTab.test.jsx's generic stub.
const DEFAULT_FALLBACK = {
  ok: true,
  status: 200,
  json: async () => ({ tools: [] }),
  text: async () => '',
}

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

// Backend for ws-1: list entry, effective permissions (container:true →
// 'enabled'), reachable docker health, two workers, empty containers/sessions.
const DEFAULT_ROUTES = {
  '/api/workspace/list': jsonOk([{ id: 'ws-1', label: 'Code Development', root: '/root' }]),
  '/api/workspace/ws-1/effective_permissions': jsonOk({
    effective_permissions: {
      filesystem: 'read',
      network: 'banned',
      git: 'write',
      system: 'read',
      execution: 'banned',
      container: true,
    },
  }),
  '/api/health/containers': jsonOk({ docker: 'reachable' }),
  '/api/workspace/ws-1/workers': jsonOk([
    { name: 'w1', runtime_status: 'ready' },
    { name: 'w2', runtime_status: 'busy' },
  ]),
  '/api/workspace/ws-1/containers': jsonOk({ containers: [] }),
  '/api/session/list': jsonOk([]),
}

function stubBackend(extra = {}) {
  return stubFetchByUrl({ ...DEFAULT_ROUTES, ...extra })
}

// ---------------------------------------------------------------------------
// Render helpers
// ---------------------------------------------------------------------------
function renderSidebar(props = {}) {
  const sendCommand = vi.fn()
  const onClose = vi.fn()
  const merged = {
    workspaceId: 'ws-1',
    config: { mode: 'custom' },
    tools: ['read_file', 'web_search'],
    sendCommand,
    onClose,
    ...props,
  }
  const utils = render(<SessionSidebar {...merged} />)
  return {
    sendCommand,
    onClose,
    rerender: (next) => utils.rerender(<SessionSidebar {...{ ...merged, ...next }} />),
  }
}

function renderTab(props = {}) {
  const mocks = {
    onClose: vi.fn(),
    onNewSession: vi.fn(),
    onOpenNewTab: vi.fn(),
    onSessionSaved: vi.fn(),
    onRegister: vi.fn(),
    onSessionRenamed: vi.fn(),
    onWorkerEvent: vi.fn(),
    onLoggingConfigChanged: vi.fn(),
    onSessionAdopted: vi.fn(),
  }
  const merged = {
    sessionId: null,
    tabId: 'tab-1',
    hubReady: true,
    staggerMs: 0,
    loadOnConnect: true,
    isActive: true,
    ...mocks,
    ...props,
  }
  const utils = render(<SessionTab {...merged} />)
  return {
    ...mocks,
    rerender: (next) => utils.rerender(<SessionTab {...{ ...merged, ...next }} />),
  }
}

async function connectWs() {
  await waitFor(() => {
    expect(MockWebSocket.instances.length).toBeGreaterThan(0)
  })
  const ws = MockWebSocket.instances[MockWebSocket.instances.length - 1]
  await act(async () => ws.open())
  return ws
}

// ---------------------------------------------------------------------------
// Setup / teardown
// ---------------------------------------------------------------------------
beforeEach(() => {
  localStorage.clear()
  MockWebSocket.instances = []
  vi.stubGlobal('WebSocket', MockWebSocket)
  vi.stubGlobal('ResizeObserver', MockResizeObserver)
  useWorkspaceStore.getState().reset()
  useStore.getState().reset()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

// ===========================================================================
// Structure: sections, title, close button, loading + empty states
// ===========================================================================
describe('SessionSidebar — structure', () => {
  it(
    'renders the four sections, the title and a close button that calls onClose',
    async () => {
    const { onClose } = renderSidebar()
    await act(async () => {}) // let fetchWorkspaceConfig settle
    expect(screen.getByText('Session Details')).toBeInTheDocument()
    expect(screen.getByRole('complementary')).toBeInTheDocument()
    expect(screen.getAllByRole('heading', { level: 4 })).toHaveLength(4)
    expect(screen.getByRole('heading', { name: 'Permissions' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Active Tools' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Workers' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Containers' })).toBeInTheDocument()
    fireEvent.click(screen.getByTitle('Close panel'))
    expect(onClose).toHaveBeenCalledTimes(1)
  }, 20000)

  it('shows loading placeholders while the workspace config is loading', () => {
    useWorkspaceStore.setState({
      currentWorkspace: { id: 'ws-1', permissions: [], workers: [], containers: [] },
      isLoading: true,
    })
    stubBackend()
    renderSidebar({ config: null, tools: [] })
    expect(screen.getByText('Loading permissions...')).toBeInTheDocument()
    expect(screen.getByText('Loading workers...')).toBeInTheDocument()
    expect(screen.getByText('Session config not loaded yet.')).toBeInTheDocument()
    expect(screen.getByText('No containers for this workspace.')).toBeInTheDocument()
  })

  it('renders every empty-state message when the backend has no data', async () => {
    stubBackend({
      '/api/workspace/list': jsonOk([{ id: 'ws-1', label: 'Blank Workspace', root: '/root' }]),
      '/api/workspace/ws-1/effective_permissions': jsonErr('no perms'),
      '/api/workspace/ws-1/workers': jsonErr('no workers'),
    })
    renderSidebar({ tools: [] })
    await act(async () => {})
    expect(screen.getByText('No permission data for this workspace.')).toBeInTheDocument()
    expect(screen.getByText('No tools enabled for this session.')).toBeInTheDocument()
    expect(screen.getByText('No workers configured for this workspace.')).toBeInTheDocument()
    expect(screen.getByText('No containers for this workspace.')).toBeInTheDocument()
  })

  it('shows "Session config not loaded yet." when the config prop is null', async () => {
    renderSidebar({ config: null })
    await act(async () => {})
    expect(screen.getByText('Session config not loaded yet.')).toBeInTheDocument()
  })
})

// ===========================================================================
// Permissions (read-only ceiling + effective table)
// ===========================================================================
describe('SessionSidebar — permissions (read-only)', () => {
  it('renders the permission table with ceiling + effective columns; container:true → enabled', async () => {
    stubBackend()
    renderSidebar({ tools: [] })
    await act(async () => {})
    expect(screen.getByText('Permission')).toBeInTheDocument()
    expect(screen.getByText('Ceiling (workspace max)')).toBeInTheDocument()
    expect(screen.getByText('Effective (session default)')).toBeInTheDocument()
    expect(screen.getByText('filesystem')).toBeInTheDocument()
    // container is a backend bool → frontend 'enabled'; it appears once as
    // ceiling and once as effective.
    expect(screen.getAllByText('enabled')).toHaveLength(2)
    // network + execution are banned in both columns.
    expect(screen.getAllByText('banned')).toHaveLength(4)
  })

  it('is read-only — no buttons, checkboxes or text inputs inside the section', async () => {
    renderSidebar({ tools: [] })
    await act(async () => {})
    const section = screen.getByRole('heading', { name: 'Permissions' }).closest('section')
    expect(within(section).queryByRole('button')).not.toBeInTheDocument()
    expect(within(section).queryByRole('checkbox')).not.toBeInTheDocument()
    expect(within(section).queryByRole('textbox')).not.toBeInTheDocument()
  })
})

// ===========================================================================
// Active tools: checkboxes, apply_config emission, optimistic override reset
// ===========================================================================
describe('SessionSidebar — active tools', () => {
  it('renders a checked checkbox per enabled tool', async () => {
    renderSidebar()
    await act(async () => {})
    const boxes = screen.getAllByRole('checkbox')
    expect(boxes).toHaveLength(2)
    expect(boxes.every((b) => b.checked)).toBe(true)
  })

  it('toggles a tool off optimistically and sends apply_config with the next tools list', async () => {
    const { sendCommand } = renderSidebar()
    await act(async () => {})
    fireEvent.click(screen.getAllByRole('checkbox')[0]) // uncheck read_file
    expect(sendCommand).toHaveBeenCalledWith('apply_config', {
      config: { mode: 'custom', tools: ['web_search'] },
    })
    const boxes = screen.getAllByRole('checkbox')
    expect(boxes).toHaveLength(1)
    expect(boxes[0].checked).toBe(true)
    expect(screen.queryByLabelText('read_file')).not.toBeInTheDocument()
  })

  it('resets the optimistic override when the tools prop changes (backend confirm)', async () => {
    const { rerender } = renderSidebar({ tools: ['read_file'] })
    await act(async () => {})
    fireEvent.click(screen.getAllByRole('checkbox')[0]) // uncheck → empty override
    expect(screen.getByText('No tools enabled for this session.')).toBeInTheDocument()
    rerender({ tools: ['read_file', 'web_search', 'git'] })
    const boxes = screen.getAllByRole('checkbox')
    expect(boxes).toHaveLength(3)
    expect(boxes.every((b) => b.checked)).toBe(true)
  })
})

// ===========================================================================
// Workers: status dots, Stop action, error surfacing
// ===========================================================================
describe('SessionSidebar — workers', () => {
  it('renders worker names, statuses and a colored dot per worker', async () => {
    stubBackend()
    renderSidebar({ tools: [] })
    await act(async () => {})
    expect(screen.getByText('w1')).toBeInTheDocument()
    expect(screen.getByText('w2')).toBeInTheDocument()
    expect(screen.getByText('ready')).toBeInTheDocument()
    expect(screen.getByText('busy')).toBeInTheDocument()
    expect(document.querySelectorAll('.session-sidebar-dot')).toHaveLength(2)
    expect(screen.getAllByRole('button', { name: 'Stop' })).toHaveLength(2)
  })

  it('stops a worker via POST /api/workspace/{ws}/workers/{name}/stop', async () => {
    const fetchMock = stubBackend()
    renderSidebar({ tools: [] })
    await act(async () => {})
    fireEvent.click(screen.getAllByRole('button', { name: 'Stop' })[0]) // w1
    await act(async () => {})
    const calls = fetchMock.mock.calls.map(([url, opts]) => [String(url), opts])
    expect(
      calls.some(
        ([url, opts]) => url.includes('/api/workspace/ws-1/workers/w1/stop') && opts.method === 'POST'
      )
    ).toBe(true)
  })

  it('surfaces a worker stop error under the Workers section', async () => {
    stubBackend({ '/api/workspace/ws-1/workers/w1/stop': jsonErr({ error: 'worker busy' }) })
    renderSidebar({ tools: [] })
    await act(async () => {})
    fireEvent.click(screen.getAllByRole('button', { name: 'Stop' })[0]) // w1
    expect(await screen.findByText('worker busy')).toBeInTheDocument()
  })
})

// ===========================================================================
// Containers: tm-resource-* filtering, dot colors, Start/Stop, error
// ===========================================================================
describe('SessionSidebar — containers', () => {
  const CONTAINER_ROUTE = {
    '/api/workspace/ws-1/containers': jsonOk({
      containers: [
        { name: 'tm-resource-sys', status: 'running' },
        { name: 'app-1', status: 'running' },
        { name: 'app-2', status: 'stopped' },
      ],
    }),
  }

  // The sidebar's mount effect calls fetchContainers before fetchWorkspaceConfig
  // resolves, so the store must already know the workspace root for the
  // containers fetch to fire.
  function seedWorkspaceRoot() {
    useWorkspaceStore.setState({
      workspaceList: [
        { id: 'ws-1', label: 'Code Development', name: 'Code Development', root: '/root', path: '/root' },
      ],
    })
  }

  it('lists containers, hides tm-resource-* entries, colors dots and shows Start/Stop', async () => {
    seedWorkspaceRoot()
    stubBackend(CONTAINER_ROUTE)
    renderSidebar({ tools: [] })
    await act(async () => {})
    expect(screen.queryByText('tm-resource-sys')).not.toBeInTheDocument()
    expect(screen.getByText('app-1')).toBeInTheDocument()
    expect(screen.getByText('app-2')).toBeInTheDocument()
    const section = screen.getByRole('heading', { name: 'Containers' }).closest('section')
    expect(within(section).getByRole('button', { name: 'Stop' })).toBeInTheDocument() // app-1 running
    expect(within(section).getByRole('button', { name: 'Start' })).toBeInTheDocument() // app-2 stopped
    expect(screen.getByText('app-1').closest('li').querySelector('.session-sidebar-dot-green')).toBeTruthy()
    expect(screen.getByText('app-2').closest('li').querySelector('.session-sidebar-dot-red')).toBeTruthy()
    // Spec asked for a "containers used" counter — the real sidebar has none.
    expect(screen.queryByText(/containers used/i)).not.toBeInTheDocument()
  })

  it('starts a stopped container through store.containerAction (POST .../containers/{name}/start)', async () => {
    seedWorkspaceRoot()
    const fetchMock = stubBackend(CONTAINER_ROUTE)
    renderSidebar({ tools: [] })
    await act(async () => {})
    const section = screen.getByRole('heading', { name: 'Containers' }).closest('section')
    fireEvent.click(within(section).getByRole('button', { name: 'Start' }))
    await act(async () => {})
    const calls = fetchMock.mock.calls.map(([url, opts]) => [String(url), opts])
    expect(
      calls.some(
        ([url, opts]) => url.includes('/api/workspace/ws-1/containers/app-2/start') && opts.method === 'POST'
      )
    ).toBe(true)
  })

  it('surfaces a container action error under the Containers section', async () => {
    seedWorkspaceRoot()
    stubBackend({
      ...CONTAINER_ROUTE,
      '/api/workspace/ws-1/containers/app-2/start': jsonErr('container boom'),
    })
    renderSidebar({ tools: [] })
    await act(async () => {})
    const section = screen.getByRole('heading', { name: 'Containers' }).closest('section')
    fireEvent.click(within(section).getByRole('button', { name: 'Start' }))
    expect(await screen.findByText('container boom')).toBeInTheDocument()
  })
})

// ===========================================================================
// SessionTab wiring: Details button toggles the panel, props come from the
// loaded session config
// ===========================================================================
describe('SessionSidebar — SessionTab wiring', () => {
  it('is closed by default and toggles open via the Details button, receiving the session config', async () => {
    stubBackend()
    renderTab()
    const ws = await connectWs()
    act(() =>
      ws.receive({
        type: 'session_loaded',
        session_id: 'sess-1',
        session_name: 'S1',
        workspace_id: 'ws-1',
        config: { mode: 'custom' },
        tools: ['read_file'], // top-level, like the real backend sends
      })
    )
    expect(await screen.findByText('S1')).toBeInTheDocument()
    expect(screen.queryByRole('complementary')).not.toBeInTheDocument()
    fireEvent.click(screen.getByTitle('Toggle session details panel'))
    const sidebar = await screen.findByRole('complementary')
    expect(within(sidebar).getByText('Session Details')).toBeInTheDocument()
    expect(within(sidebar).getByRole('heading', { name: 'Permissions' })).toBeInTheDocument()
    expect(within(sidebar).getByText('read_file')).toBeInTheDocument()
  })
})
