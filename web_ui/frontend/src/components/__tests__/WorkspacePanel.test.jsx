// @vitest-environment jsdom
/*
 * WorkspacePanel.test.jsx — workspace panel tests (Phase 3).
 *
 * Tests the REAL WorkspacePanel (src/components/WorkspacePanel.jsx) against a
 * stubbed global fetch. The child editors (DockerfileEditor,
 * DomainAllowlistEditor, WorkerManagementPanel) all fetch on mount, so every
 * render stubs their routes. Effective-permissions pills read from useStore
 * (sessionConfigs[sessionId].permissions ?? PERMISSION_DEFAULTS).
 */

import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, act, waitFor, within } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import WorkspacePanel from '../WorkspacePanel'
import useStore from '../../store/useStore'
// --- Phase 4 tabbed panel (src/components/workspace/) ---
import TabbedWorkspacePanel from '../workspace/WorkspacePanel'
import useWorkspaceStore from '../../store/workspaceStore'
import ResourcesTab from '../workspace/tabs/ResourcesTab'
import PermissionsTab from '../workspace/tabs/PermissionsTab'
import ToolsTab from '../workspace/tabs/ToolsTab'
import CredentialsTab from '../workspace/tabs/CredentialsTab'
import ContainersTab from '../workspace/tabs/ContainersTab'
import WorkersTab from '../workspace/tabs/WorkersTab'
import SessionDefaultsTab from '../workspace/tabs/SessionDefaultsTab'

// ---------------------------------------------------------------------------
// fetch stubs — substring routes, longest key wins
// ---------------------------------------------------------------------------
function jsonOk(data, status = 200) {
  return { ok: true, status, json: async () => data, text: async () => JSON.stringify(data) }
}

function jsonErr(detail, status = 500) {
  return { ok: false, status, json: async () => ({ detail }), text: async () => JSON.stringify({ detail }) }
}

function textOk(text) {
  return { ok: true, status: 200, text: async () => text, json: async () => ({}) }
}

const DEFAULT_FALLBACK = {
  ok: true,
  status: 200,
  json: async () => ({}),
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

const DEFAULT_ROUTES = {
  '/api/workspace/ws-1/dockerfile': textOk('FROM node:20\nWORKDIR /app'),
  '/api/workspace/ws-1/domain_allowlist': jsonOk(['*.github.com', 'example.com']),
  '/api/workspace/ws-1/workers': jsonOk([]),
  '/api/workspace/templates': jsonOk([]),
}

function stubBackend(extra = {}) {
  return stubFetchByUrl({ ...DEFAULT_ROUTES, ...extra })
}

// ---------------------------------------------------------------------------
// Render helpers
// ---------------------------------------------------------------------------
function renderPanel(props = {}) {
  const merged = {
    workspaceId: 'ws-1',
    sessionId: 'sess-1',
    onSelectWorker: vi.fn(),
    selectedWorker: null,
    isActive: true,
    ...props,
  }
  return render(<WorkspacePanel {...merged} />)
}

function dockerfileRoot() {
  return screen.getByPlaceholderText(/# Paste or edit/).closest('div')
}

function allowlistRoot() {
  return screen.getByPlaceholderText('e.g. *.github.com').closest('div').parentElement
}

// ---------------------------------------------------------------------------
// Setup / teardown
// ---------------------------------------------------------------------------
beforeEach(() => {
  localStorage.clear()
  useStore.getState().reset()
  useWorkspaceStore.getState().reset()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

// ===========================================================================
// Structure
// ===========================================================================
describe('WorkspacePanel — structure', () => {
  it('renders "No workspace loaded." when no workspaceId is given', () => {
    stubBackend()
    renderPanel({ workspaceId: null })
    expect(screen.getByText('No workspace loaded.')).toBeInTheDocument()
  })

  it('renders the four section labels and the two helper texts', async () => {
    stubBackend()
    renderPanel()
    await act(async () => {})
    expect(screen.getByText('Dockerfile')).toBeInTheDocument()
    expect(screen.getByText('Domain Allowlist')).toBeInTheDocument()
    expect(screen.getByText('Workers')).toBeInTheDocument()
    expect(screen.getByText('Effective Permissions')).toBeInTheDocument()
    expect(
      screen.getByText('One domain per line. Wildcards supported (e.g. *.example.com).')
    ).toBeInTheDocument()
    expect(screen.getByText('Merged session + workspace capabilities.')).toBeInTheDocument()
  })
})

// ===========================================================================
// Dockerfile editor
// ===========================================================================
describe('Dockerfile section', () => {
  it('shows the loading state while the dockerfile fetch is pending', () => {
    stubBackend({
      '/api/workspace/ws-1/dockerfile': () => new Promise(() => {}),
    })
    renderPanel()
    expect(screen.getByText('Loading Dockerfile…')).toBeInTheDocument()
  })

  it('loads the dockerfile into the textarea and keeps Save disabled until edited', async () => {
    stubBackend()
    renderPanel()
    await waitFor(() => {
      expect(screen.getByPlaceholderText(/# Paste or edit/)).toHaveValue('FROM node:20\nWORKDIR /app')
    })
    expect(within(dockerfileRoot()).getByRole('button', { name: 'Save' })).toBeDisabled()
    expect(screen.queryByText(/You've changed the Dockerfile/)).not.toBeInTheDocument()
  })

  it('warns on unsaved changes and PUTs the content on save', async () => {
    const fetchMock = stubBackend()
    renderPanel()
    await waitFor(() => {
      expect(screen.getByPlaceholderText(/# Paste or edit/)).toHaveValue('FROM node:20\nWORKDIR /app')
    })
    fireEvent.change(screen.getByPlaceholderText(/# Paste or edit/), { target: { value: 'FROM node:22' } })
    expect(
      screen.getByText(/You've changed the Dockerfile\. Rebuild the container for changes to take effect\./)
    ).toBeInTheDocument()
    fireEvent.click(within(dockerfileRoot()).getByRole('button', { name: 'Save' }))
    await waitFor(() => {
      const put = fetchMock.mock.calls.find(
        ([u, o]) => String(u).includes('/dockerfile') && o?.method === 'PUT'
      )
      expect(put).toBeTruthy()
      expect(put[1].body).toBe('FROM node:22')
    })
    expect(screen.queryByText(/You've changed the Dockerfile/)).not.toBeInTheDocument()
    expect(screen.getByText(/Last saved:/)).toBeInTheDocument()
  })

  it('starts with an empty editor when the backend returns 404', async () => {
    stubBackend({ '/api/workspace/ws-1/dockerfile': jsonErr('not found', 404) })
    renderPanel()
    await waitFor(() => {
      expect(screen.getByPlaceholderText(/# Paste or edit/)).toHaveValue('')
    })
    expect(screen.queryByText(/Error: HTTP 404/)).not.toBeInTheDocument()
  })

  it('shows "Error: HTTP <status>" with a working Retry button on fetch failure', async () => {
    const routes = { '/api/workspace/ws-1/dockerfile': jsonErr('boom', 500) }
    stubFetchByUrl(routes)
    renderPanel()
    await waitFor(() => expect(screen.getByText('Error: HTTP 500')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(screen.getByText('Error: HTTP 500')).toBeInTheDocument())
    // Backend recovers — Retry re-fetches and shows the editor
    routes['/api/workspace/ws-1/dockerfile'] = textOk('FROM node:24')
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => {
      expect(screen.getByPlaceholderText(/# Paste or edit/)).toHaveValue('FROM node:24')
    })
  })
})

// ===========================================================================
// Domain allowlist editor
// ===========================================================================
describe('Domain Allowlist section', () => {
  it('shows the loading state while the allowlist fetch is pending', () => {
    stubBackend({
      '/api/workspace/ws-1/domain_allowlist': () => new Promise(() => {}),
    })
    renderPanel()
    expect(screen.getByText('Loading domain allowlist…')).toBeInTheDocument()
  })

  it('renders the domains with a count and no unsaved indicator', async () => {
    stubBackend()
    renderPanel()
    await waitFor(() => expect(screen.getByText('*.github.com')).toBeInTheDocument())
    expect(screen.getByText('example.com')).toBeInTheDocument()
    expect(screen.getByText('2 domains')).toBeInTheDocument()
    expect(screen.queryByText('Unsaved changes')).not.toBeInTheDocument()
  })

  it('shows the empty state when the allowlist is empty', async () => {
    stubBackend({ '/api/workspace/ws-1/domain_allowlist': jsonOk([]) })
    renderPanel()
    await waitFor(() =>
      expect(screen.getByText('No domains in allowlist. Add one below.')).toBeInTheDocument()
    )
    expect(screen.getByText('0 domains')).toBeInTheDocument()
  })

  it('adds a domain via the button, shows unsaved changes, then saves via PUT', async () => {
    const fetchMock = stubBackend()
    renderPanel()
    await waitFor(() => expect(screen.getByText('*.github.com')).toBeInTheDocument())
    const input = screen.getByPlaceholderText('e.g. *.github.com')
    expect(within(allowlistRoot()).getByRole('button', { name: 'Add' })).toBeDisabled()
    fireEvent.change(input, { target: { value: 'api.stripe.com' } })
    expect(within(allowlistRoot()).getByRole('button', { name: 'Add' })).toBeEnabled()
    fireEvent.click(within(allowlistRoot()).getByRole('button', { name: 'Add' }))
    await waitFor(() => expect(screen.getByText('api.stripe.com')).toBeInTheDocument())
    expect(screen.getByText('3 domains')).toBeInTheDocument()
    expect(screen.getByText('Unsaved changes')).toBeInTheDocument()
    fireEvent.click(within(allowlistRoot()).getByRole('button', { name: 'Save' }))
    await waitFor(() => {
      const put = fetchMock.mock.calls.find(
        ([u, o]) => String(u).includes('/domain_allowlist') && o?.method === 'PUT'
      )
      expect(put).toBeTruthy()
      expect(JSON.parse(put[1].body).domains).toEqual(['*.github.com', 'example.com', 'api.stripe.com'])
    })
    expect(screen.queryByText('Unsaved changes')).not.toBeInTheDocument()
  })

  it('rejects an empty domain on Enter with a validation message', async () => {
    stubBackend()
    renderPanel()
    await waitFor(() => expect(screen.getByText('*.github.com')).toBeInTheDocument())
    const input = screen.getByPlaceholderText('e.g. *.github.com')
    fireEvent.change(input, { target: { value: '   ' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(screen.getByText('Domain cannot be empty.')).toBeInTheDocument()
  })

  it('rejects duplicate domains (case-insensitive)', async () => {
    stubBackend()
    renderPanel()
    await waitFor(() => expect(screen.getByText('*.github.com')).toBeInTheDocument())
    const input = screen.getByPlaceholderText('e.g. *.github.com')
    fireEvent.change(input, { target: { value: 'Example.COM' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(screen.getByText('Domain already in the allowlist.')).toBeInTheDocument()
  })

  it('removes a domain with the ✕ button', async () => {
    stubBackend()
    renderPanel()
    await waitFor(() => expect(screen.getByText('example.com')).toBeInTheDocument())
    fireEvent.click(screen.getByTitle('Remove example.com'))
    await waitFor(() => expect(screen.queryByText('example.com')).not.toBeInTheDocument())
    expect(screen.getByText('1 domain')).toBeInTheDocument()
    expect(screen.getByText('Unsaved changes')).toBeInTheDocument()
  })
})

// ===========================================================================
// Workers section
// ===========================================================================
describe('Workers section', () => {
  it('shows the loading state, then the empty state, and fetches templates', async () => {
    const fetchMock = stubBackend()
    renderPanel()
    expect(screen.getByText('Loading workers…')).toBeInTheDocument()
    await waitFor(() =>
      expect(
        screen.getByText('No workers configured. Create one now, or start from a template.')
      ).toBeInTheDocument()
    )
    // Both the panel toolbar and the empty state render these buttons (2 each).
    expect(screen.getAllByRole('button', { name: '+ New Worker' })).toHaveLength(2)
    expect(screen.getAllByRole('button', { name: 'From Template' })).toHaveLength(2)
    expect(fetchMock.mock.calls.some(([u]) => String(u).includes('/api/workspace/templates'))).toBe(true)
  })

  it('renders worker rows and auto-opens the output panel for a ready worker', async () => {
    const onSelectWorker = vi.fn()
    stubBackend({
      '/api/workspace/ws-1/workers': jsonOk([
        {
          name: 'w1',
          description: 'first worker',
          tools: ['FileEditor', 'GitTool'],
          runtime_status: 'ready',
        },
      ]),
    })
    renderPanel({ onSelectWorker })
    await waitFor(() => expect(screen.getByText('w1')).toBeInTheDocument())
    expect(screen.getByText('first worker')).toBeInTheDocument()
    expect(screen.getByText('ready')).toBeInTheDocument()
    expect(screen.getByText('2')).toBeInTheDocument() // tools count
    expect(onSelectWorker).toHaveBeenCalledWith('w1', 'ws-1')
  })

  it('POSTs to the stop endpoint when stopping a running worker', async () => {
    const fetchMock = stubBackend({
      '/api/workspace/ws-1/workers': jsonOk([
        { name: 'w1', description: '', tools: [], runtime_status: 'ready' },
      ]),
    })
    renderPanel({ onSelectWorker: vi.fn() })
    await waitFor(() => expect(screen.getByText('w1')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
    await waitFor(() => {
      const post = fetchMock.mock.calls.find(
        ([u, o]) => String(u).includes('/workers/w1/stop') && o?.method === 'POST'
      )
      expect(post).toBeTruthy()
    })
  })
})

// ===========================================================================
// Effective permissions
// ===========================================================================
describe('Effective Permissions section', () => {
  it('renders the five default permission pills when the session has none', async () => {
    stubBackend()
    renderPanel()
    await act(async () => {})
    expect(screen.getByText('Filesystem: Read')).toBeInTheDocument()
    expect(screen.getByText('Network: Banned')).toBeInTheDocument()
    expect(screen.getByText('Git: Read')).toBeInTheDocument()
    expect(screen.getByText('System: Read')).toBeInTheDocument()
    expect(screen.getByText('Container: Disabled')).toBeInTheDocument()
  })

  it('renders pills from the seeded session config and maps boolean container values', async () => {
    stubBackend()
    useStore.setState({
      sessionConfigs: {
        'sess-1': {
          permissions: {
            filesystem: 'full',
            network: 'ask',
            git: 'write',
            system: 'read',
            container: true,
          },
        },
      },
    })
    renderPanel()
    await act(async () => {})
    expect(screen.getByText('Filesystem: Full')).toBeInTheDocument()
    expect(screen.getByText('Network: Ask')).toBeInTheDocument()
    expect(screen.getByText('Git: Write')).toBeInTheDocument()
    expect(screen.getByText('System: Read')).toBeInTheDocument()
    expect(screen.getByText('Container: Enabled')).toBeInTheDocument()
    expect(screen.getByTitle('Container: true')).toBeInTheDocument()
    expect(screen.getByTitle('Filesystem: full')).toBeInTheDocument()
  })
})

// ===========================================================================
// Phase 4 tabbed panel — direct coverage of the tab components
// (src/components/workspace/tabs/*) and the tabbed WorkspacePanel shell
// (src/components/workspace/WorkspacePanel.jsx) with the REAL hash router and
// the REAL workspaceStore behind a stubbed global fetch.
// ===========================================================================

function makeWorkspace(overrides = {}) {
  return {
    id: 'ws-tabs',
    name: 'Tab Workspace',
    path: '~/workspaces/tabs',
    root: '~/workspaces/tabs',
    risk: 'Low',
    purposeId: 'code-development',
    resources: [
      { name: 'git', icon: '🌿', description: 'Version control access', containerized: true, risk: 'Low', enabled: true },
      { name: 'serial', icon: '🔌', description: 'Serial / device access', containerized: false, risk: 'Low', enabled: false },
    ],
    permissions: [
      { name: 'filesystem', ceiling: 'write', effective: 'write' },
      { name: 'network', ceiling: 'read', effective: 'read' },
    ],
    tools: [
      { name: 'FileEditor', resource: 'filesystem', permission: 'write', enabled: true, defaultOn: true },
      { name: 'GitTool', resource: 'git', permission: 'read', enabled: false, defaultOn: false },
    ],
    credentials: [{ name: 'openai-key', type: 'api_key', placeholder: 'sk-…', assigned: true }],
    containers: [{ name: 'app-1', status: 'running', uptime_seconds: 90, note: 'api' }],
    workers: [
      {
        name: 'coder',
        description: 'Writes code',
        systemPrompt: 'You write code.',
        tools: ['FileEditor'],
        workerPermissions: ['filesystem'],
        tokenLimit: 8000,
        runtimeStatus: 'ready',
      },
    ],
    sessionDefaults: {
      systemPrompt: 'You are a helpful assistant.',
      tokenLimit: 8000,
      temperature: 0.7,
      maxTurns: 20,
      toolOutputTokenLimit: 2000,
      allowedProviders: ['OpenAI'],
      defaultPreset: 'balanced',
    },
    ...overrides,
  }
}

const TABS_ENTRY = { id: 'ws-tabs', label: 'Code Development', root: '~/workspaces/tabs' }

function stubTabsBackend(extra = {}) {
  return stubFetchByUrl({
    '/api/workspace/list': jsonOk([TABS_ENTRY]),
    '/api/workspace/ws-tabs/effective_permissions': jsonOk({
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
    '/api/workspace/ws-tabs/workers': jsonOk([]),
    '/api/workspace/ws-tabs/containers': jsonOk({ containers: [] }),
    '/api/session/list?workspace_id=ws-tabs': jsonOk([]),
    ...extra,
  })
}

async function renderTabbedPanel() {
  window.location.hash = '#/workspace/ws-tabs'
  useWorkspaceStore.setState({ workspaceList: [{ ...TABS_ENTRY }] })
  stubTabsBackend()
  render(<TabbedWorkspacePanel />)
  await screen.findByRole('heading', { name: 'Code Development' })
}

// ---------------------------------------------------------------------------
// Tab components (unit level)
// ---------------------------------------------------------------------------
describe('Phase 4 tabs — ResourcesTab', () => {
  it('renders resource cards with risk, badges and toggles', () => {
    render(<ResourcesTab workspace={makeWorkspace()} update={vi.fn()} />)
    expect(screen.getByRole('heading', { name: 'Resources' })).toBeInTheDocument()
    expect(screen.getByText('git')).toBeInTheDocument()
    expect(screen.getByText('Serial / device access')).toBeInTheDocument()
    expect(screen.getByText('✓ Containerized')).toBeInTheDocument()
    expect(screen.getByText('⚠ Host-only')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add Resource' })).toBeInTheDocument()
  })

  it('toggles a resource through update()', () => {
    const update = vi.fn()
    render(<ResourcesTab workspace={makeWorkspace()} update={update} />)
    fireEvent.click(screen.getByLabelText('Enabled'))
    expect(update).toHaveBeenCalledWith(
      'resources',
      expect.arrayContaining([expect.objectContaining({ name: 'git', enabled: false })])
    )
  })
})

describe('Phase 4 tabs — PermissionsTab', () => {
  it('renders a table with ceiling dropdowns and effective values', () => {
    render(<PermissionsTab workspace={makeWorkspace()} update={vi.fn()} />)
    expect(screen.getByRole('heading', { name: 'Permissions' })).toBeInTheDocument()
    expect(screen.getByText('filesystem')).toBeInTheDocument()
    expect(screen.getByText('network')).toBeInTheDocument()
    expect(screen.getAllByRole('combobox')).toHaveLength(2)
    expect(screen.getAllByText('write').length).toBeGreaterThanOrEqual(1)
  })

  it('lowers a ceiling through update()', () => {
    const update = vi.fn()
    render(<PermissionsTab workspace={makeWorkspace()} update={update} />)
    const row = screen.getByText('network').closest('tr')
    fireEvent.change(within(row).getByRole('combobox'), { target: { value: 'banned' } })
    expect(update).toHaveBeenCalledWith(
      'permissions',
      expect.arrayContaining([expect.objectContaining({ name: 'network', ceiling: 'banned', effective: 'banned' })])
    )
  })
})

describe('Phase 4 tabs — ToolsTab', () => {
  it('groups tools by resource and renders both toggles', () => {
    render(<ToolsTab workspace={makeWorkspace()} update={vi.fn()} />)
    expect(screen.getByRole('heading', { name: 'Tools' })).toBeInTheDocument()
    expect(screen.getByText('filesystem')).toBeInTheDocument()
    expect(screen.getByText('git')).toBeInTheDocument()
    expect(screen.getByText('FileEditor')).toBeInTheDocument()
    expect(screen.getByText('GitTool')).toBeInTheDocument()
  })

  it('flips the Default ON toggle through update()', () => {
    const update = vi.fn()
    render(<ToolsTab workspace={makeWorkspace()} update={update} />)
    const row = screen.getByText('FileEditor').closest('.wp-tool-row')
    fireEvent.click(within(row).getByLabelText('Default ON'))
    expect(update).toHaveBeenCalledWith(
      'tools',
      expect.arrayContaining([expect.objectContaining({ name: 'FileEditor', defaultOn: false })])
    )
  })
})

describe('Phase 4 tabs — CredentialsTab', () => {
  it('renders assigned credentials with type and placeholder', () => {
    render(<CredentialsTab workspace={makeWorkspace()} update={vi.fn()} />)
    expect(screen.getByRole('heading', { name: 'Credentials' })).toBeInTheDocument()
    expect(screen.getByText('openai-key')).toBeInTheDocument()
    expect(screen.getByText('api_key')).toBeInTheDocument()
    expect(screen.getByText('sk-…')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add Credential' })).toBeInTheDocument()
  })

  it('unassigns a credential through update()', () => {
    const update = vi.fn()
    render(<CredentialsTab workspace={makeWorkspace()} update={update} />)
    fireEvent.click(screen.getByLabelText('Assigned'))
    expect(update).toHaveBeenCalledWith(
      'credentials',
      expect.arrayContaining([expect.objectContaining({ name: 'openai-key', assigned: false })])
    )
  })
})

describe('Phase 4 tabs — ContainersTab', () => {
  it('renders the container table with the limit counter', () => {
    render(
      <ContainersTab
        workspace={makeWorkspace()}
        dockerAvailable={true}
        containerStatus={{}}
        busyContainers={{}}
        onRefresh={vi.fn()}
        onAction={vi.fn(() => Promise.resolve())}
        onError={vi.fn()}
      />
    )
    expect(screen.getByRole('heading', { name: 'Containers' })).toBeInTheDocument()
    expect(screen.getByText('1 of 4 containers used')).toBeInTheDocument()
    expect(screen.getByText('app-1')).toBeInTheDocument()
    expect(screen.getByText('running')).toBeInTheDocument()
    expect(screen.getByText('1m 30s')).toBeInTheDocument()
    expect(screen.getByText('api')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Stop' })).toBeInTheDocument()
  })

  it('calls onAction with the container name and action', () => {
    const onAction = vi.fn(() => Promise.resolve())
    render(
      <ContainersTab
        workspace={makeWorkspace()}
        dockerAvailable={true}
        containerStatus={{}}
        busyContainers={{}}
        onRefresh={vi.fn()}
        onAction={onAction}
        onError={vi.fn()}
      />
    )
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
    expect(onAction).toHaveBeenCalledWith('app-1', 'stop')
  })
})

describe('Phase 4 tabs — WorkersTab', () => {
  it('renders worker cards with meta and edit controls', () => {
    render(<WorkersTab workspace={makeWorkspace()} update={vi.fn()} />)
    expect(screen.getByRole('heading', { name: 'Workers' })).toBeInTheDocument()
    expect(screen.getByText('coder')).toBeInTheDocument()
    expect(screen.getByText('You write code.')).toBeInTheDocument()
    expect(screen.getByText('1 tools')).toBeInTheDocument()
    expect(screen.getByText('1 permissions')).toBeInTheDocument()
    expect(screen.getByText('8000 tokens')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add Preset' })).toBeInTheDocument()
  })
})

describe('Phase 4 tabs — SessionDefaultsTab', () => {
  it('renders the defaults form and commits edits on blur', () => {
    const update = vi.fn()
    render(<SessionDefaultsTab workspace={makeWorkspace()} update={update} />)
    expect(screen.getByRole('heading', { name: 'Session Defaults' })).toBeInTheDocument()
    expect(screen.getByText('System prompt')).toBeInTheDocument()
    const prompt = screen.getByLabelText('System prompt')
    expect(prompt).toHaveValue('You are a helpful assistant.')
    fireEvent.change(prompt, { target: { value: 'Be concise.' } })
    fireEvent.blur(prompt)
    expect(update).toHaveBeenCalledWith(
      'sessionDefaults',
      expect.objectContaining({ systemPrompt: 'Be concise.' })
    )
  })
})

// ---------------------------------------------------------------------------
// Tabbed WorkspacePanel shell (integration at unit-file level)
// ---------------------------------------------------------------------------
describe('Phase 4 tabbed panel — shell wiring', () => {
  it('defaults to Session Defaults and switches tabs', async () => {
    await renderTabbedPanel()
    expect(screen.getByRole('tab', { name: 'Session Defaults' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('heading', { name: 'Session Defaults' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: 'Resources' }))
    expect(screen.getByRole('tab', { name: 'Resources' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('heading', { name: 'Resources' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: 'Tools' }))
    expect(screen.getByRole('heading', { name: 'Tools' })).toBeInTheDocument()
    expect(screen.getByText('FileEditor')).toBeInTheDocument()
  })

  it('opens the New Session modal from the header', async () => {
    await renderTabbedPanel()
    fireEvent.click(screen.getByRole('button', { name: 'New Session' }))
    expect(await screen.findByRole('dialog', { name: 'New session' })).toBeInTheDocument()
  })
})

describe('Phase 4 tabbed panel — safety advisory', () => {
  it('shows the green advisory for the low-risk code-development fixture', async () => {
    await renderTabbedPanel()
    expect(screen.getByRole('img', { name: 'green' })).toBeInTheDocument()
    expect(screen.getByText('Low risk — standard guardrails apply.')).toBeInTheDocument()
    expect(screen.getByText('All guardrails active — no action needed.')).toBeInTheDocument()
  })

  it('shows the amber advisory when a host-only resource is enabled and disables it on demand', async () => {
    localStorage.setItem(
      'tm.workspace.local.ws-tabs',
      JSON.stringify({
        resources: [
          { name: 'serial', icon: '🔌', description: 'Serial / device access', containerized: false, risk: 'Low', enabled: true },
        ],
      })
    )
    await renderTabbedPanel()
    expect(screen.getByRole('img', { name: 'amber' })).toBeInTheDocument()
    expect(screen.getByText('Host-only resources enabled — containerize or disable them.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Disable serial' }))
    await waitFor(() => {
      const res = useWorkspaceStore.getState().currentWorkspace.resources.find((r) => r.name === 'serial')
      expect(res.enabled).toBe(false)
    })
  })

  it('shows the red advisory and Install Docker hint when Docker is down', async () => {
    localStorage.setItem(
      'tm.workspace.local.ws-tabs',
      JSON.stringify({
        resources: [
          { name: 'serial', icon: '🔌', description: 'Serial / device access', containerized: false, risk: 'Low', enabled: true },
        ],
      })
    )
    stubTabsBackend({ '/api/health/containers': jsonOk({ docker: 'down' }) })
    window.location.hash = '#/workspace/ws-tabs'
    useWorkspaceStore.setState({ workspaceList: [{ ...TABS_ENTRY }] })
    render(<TabbedWorkspacePanel />)
    await screen.findByRole('heading', { name: 'Code Development' })
    expect(screen.getByRole('img', { name: 'red' })).toBeInTheDocument()
    expect(screen.getByText('Host-only resources enabled and Docker is unavailable.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Install Docker' }))
    expect(
      screen.getByText('Install Docker and restart the server to enable containerized execution.')
    ).toBeInTheDocument()
  })

  it('warns when the network permission exceeds the purpose default and lowers it', async () => {
    stubTabsBackend({
      '/api/workspace/ws-tabs/effective_permissions': jsonOk({
        effective_permissions: {
          filesystem: 'write',
          network: 'write',
          git: 'write',
          system: 'read',
          execution: 'banned',
          container: true,
        },
      }),
    })
    window.location.hash = '#/workspace/ws-tabs'
    useWorkspaceStore.setState({ workspaceList: [{ ...TABS_ENTRY }] })
    render(<TabbedWorkspacePanel />)
    await screen.findByRole('heading', { name: 'Code Development' })
    expect(screen.getByRole('img', { name: 'amber' })).toBeInTheDocument()
    expect(screen.getByText('Network permission is higher than this purpose requires.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Lower network to read' }))
    await waitFor(() => {
      const p = useWorkspaceStore.getState().currentWorkspace.permissions.find((x) => x.name === 'network')
      expect(p.effective).toBe('read')
    })
  })
})
