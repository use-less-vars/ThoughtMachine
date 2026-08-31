// @vitest-environment jsdom
// --- WorkspaceSelector.test.jsx ---
// Global Management landing view: summary-driven sections, host risk tags,
// session navigation, vault health banner, credentials/resources rendering,
// refresh and the custom-workspace modal flow.

import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import WorkspaceSelector from '../WorkspaceSelector'
import useWorkspaceStore from '../../store/workspaceStore'

function jsonOk(data, status = 200) {
  return { ok: true, status, json: async () => data, text: async () => JSON.stringify(data) }
}

function stubFetchByUrl(routes) {
  const calls = []
  const fetchMock = vi.fn(async (input, init) => {
    const url = typeof input === 'string' ? input : String(input)
    calls.push(url)
    const func = routes.find((r) => typeof r.match === 'function' && r.match(url, init))
    const str = routes
      .filter((r) => typeof r.match === 'string' && url.includes(r.match))
      .sort((a, b) => b.match.length - a.match.length)[0]
    const route = func || str
    if (!route) return { ok: true, status: 200, json: async () => ({}), text: async () => '' }
    const value = typeof route.value === 'function' ? route.value(url, init) : route.value
    return value
  })
  vi.stubGlobal('fetch', fetchMock)
  return { fetchMock, calls }
}

const SUMMARY = {
  workspaces: [
    {
      id: 'ws-a',
      label: 'Alpha Workspace',
      active_sessions_count: 2,
      total_workers: 3,
      last_active: '2026-08-01T10:00:00Z',
      status: 'running',
      allow_host_resources: true,
    },
    {
      id: 'ws-b',
      label: 'Beta Workspace',
      active_sessions_count: 0,
      total_workers: 0,
      last_active: null,
      status: 'idle',
      allow_host_resources: false,
    },
  ],
  active_sessions: [
    {
      session_id: 'sess-1',
      workspace_id: 'ws-a',
      name: 'Refactor Session',
      mode: 'engineer',
      worker_count: 2,
      started_at: '2026-08-30T12:00:00Z',
    },
    {
      session_id: 'sess-2',
      workspace_id: 'ws-b',
      name: 'Research Session',
      mode: 'research',
      worker_count: 1,
      started_at: '2026-08-29T08:00:00Z',
    },
  ],
  active_containers: [
    { name: 'alpha-ctr', type: 'free_use', workspace_id: 'ws-a', status: 'running' },
    { name: 'beta-ctr', type: 'resource', workspace_id: 'ws-b', status: 'stopped' },
  ],
  providers: [],
}

const RESOURCE = {
  id: 'npm_cache',
  display_name: 'npm cache',
  description: 'Shared npm cache',
  permission_grain_set: ['read', 'write'],
  default_execution_context: 'container',
  tools: ['npm'],
  dockerfile_reference: 'cache.Dockerfile',
}

const ROUTES = [
  { match: '/api/global/summary', value: jsonOk(SUMMARY) },
  { match: '/api/vault/status', value: jsonOk({ ok: true }) },
  { match: '/api/credentials', value: jsonOk({ credentials: [{ name: 'github_token' }, { name: 'openai_api_key' }] }) },
  { match: '/api/resource-catalog', value: jsonOk({ items: [RESOURCE] }) },
  { match: '/api/prompts', value: jsonOk([]) },
]

const MODAL_ROUTES = [
  ...ROUTES,
  { match: '/api/user-home', value: jsonOk({ home: '/home/test' }) },
  {
    match: (url) => url.startsWith('/api/browse'),
    value: (url) => {
      const p = new URL(url, 'http://x').searchParams.get('path')
      return jsonOk({ success: true, current_path: p, entries: p === '/home/test' ? [{ name: 'project-a', is_dir: true }] : [] })
    },
  },
  { match: '/api/workspace/resolve', value: jsonOk({ workspace_id: 'ws-custom', root: '/home/test/project-a' }) },
]

beforeEach(() => {
  localStorage.clear()
  window.location.hash = ''
  useWorkspaceStore.getState().reset()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('WorkspaceSelector', () => {
  it('renders all global management sections and fetches the summary', async () => {
    const { calls } = stubFetchByUrl(ROUTES)
    render(<WorkspaceSelector />)

    await screen.findByRole('heading', { name: 'Alpha Workspace' })

    expect(screen.getByRole('heading', { name: 'Workspaces' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Active Sessions' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Active Containers' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Global Resources' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Global Credentials' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Global Sysprompts' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Providers' })).toBeInTheDocument()

    expect(calls.some((u) => u.includes('/api/global/summary'))).toBe(true)
  })

  it('shows host allow/forbid tags', async () => {
    stubFetchByUrl(ROUTES)
    const { container } = render(<WorkspaceSelector />)

    await screen.findByRole('heading', { name: 'Alpha Workspace' })

    expect(screen.getByText('Host: allowed')).toBeInTheDocument()
    expect(screen.getByText('Host: forbidden')).toBeInTheDocument()
    expect(container.querySelector('.ws-card .ws-risk.allow').textContent).toBe('Host: allowed')
    expect(container.querySelector('.ws-card .ws-risk.forbid').textContent).toBe('Host: forbidden')
  })

  it('navigates to a workspace when a card is clicked', async () => {
    stubFetchByUrl(ROUTES)
    render(<WorkspaceSelector />)

    await screen.findByRole('heading', { name: 'Alpha Workspace' })

    fireEvent.click(screen.getByRole('heading', { level: 4, name: 'Alpha Workspace' }).closest('.ws-card'))
    expect(window.location.hash).toBe('#/workspace/ws-a')
  })

  it('navigates to a session when a session row is clicked', async () => {
    stubFetchByUrl(ROUTES)
    render(<WorkspaceSelector />)

    await screen.findByText('Refactor Session')

    fireEvent.click(screen.getByRole('button', { name: /Refactor Session/ }))
    expect(window.location.hash).toBe('#/session/sess-1')
  })

  it('shows vault healthy banner', async () => {
    stubFetchByUrl(ROUTES)
    render(<WorkspaceSelector />)

    await screen.findByText('✓ Vault healthy')
  })

  it('masks credentials and renders resources', async () => {
    stubFetchByUrl(ROUTES)
    render(<WorkspaceSelector />)

    await screen.findByText('github_token')
    expect(screen.getByText('openai_api_key')).toBeInTheDocument()
    expect(screen.getAllByText('••••••••').length).toBe(2)
    expect(screen.queryByText(/sk-secret|abc123/)).toBeNull()

    await screen.findByText('npm cache')
    expect(screen.getByText('Shared npm cache')).toBeInTheDocument()
    expect(screen.getByText('Containerized')).toBeInTheDocument()
    expect(screen.getByText('Context: container')).toBeInTheDocument()
  })

  it('Refresh refetches the summary', async () => {
    const { calls } = stubFetchByUrl(ROUTES)
    render(<WorkspaceSelector />)

    await screen.findByRole('heading', { name: 'Alpha Workspace' })

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(calls.filter((u) => u.includes('/api/global/summary')).length).toBe(2))
  })

  it('opens the custom workspace modal', async () => {
    stubFetchByUrl(MODAL_ROUTES)
    render(<WorkspaceSelector />)

    await screen.findByRole('heading', { name: 'Alpha Workspace' })

    fireEvent.click(screen.getByRole('button', { name: '+ New Workspace' }))
    expect(screen.getByRole('dialog', { name: 'Create custom workspace' })).toBeInTheDocument()
  })

  it('custom modal resolves a folder and navigates', async () => {
    stubFetchByUrl(MODAL_ROUTES)
    render(<WorkspaceSelector />)

    await screen.findByRole('heading', { name: 'Alpha Workspace' })

    fireEvent.click(screen.getByRole('button', { name: '+ New Workspace' }))
    await screen.findByText('Select This Folder')

    fireEvent.click(screen.getByText(/project-a/))
    await waitFor(() => expect(screen.getByText('Select This Folder')).toBeInTheDocument())

    fireEvent.click(screen.getByText('Select This Folder'))
    await waitFor(() => expect(window.location.hash).toBe('#/workspace/ws-custom'))
    expect(localStorage.getItem('thoughtmachine_last_workspace')).toBe('/home/test/project-a')
  })
})
