// @vitest-environment jsdom
/*
 * WorkspaceIntegration.test.jsx — the tabbed workspace panel end-to-end.
 *
 * Renders the REAL WorkspacePanel (src/components/workspace/WorkspacePanel.jsx)
 * with the REAL hash router, the REAL workspace store and a stubbed global
 * fetch. The store fans out in parallel to /api/workspace/list,
 * /api/workspace/{id}/effective_permissions, /api/health/containers,
 * /api/workspace/{id}/workers, /api/workspace/{id}/containers and
 * /api/session/list, then assembles the workspace. These tests walk the full
 * path: config load → header/advisory → tab navigation → store updates →
 * New Session modal flow.
 */
import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor, within } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import WorkspacePanel from '../workspace/WorkspacePanel'
import useWorkspaceStore from '../../store/workspaceStore'
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
        filesystem: 'write',
        network: 'read',
        git: 'write',
        system: 'read',
        execution: 'banned',
        container: true,
      },
    }),
    '/api/health/containers': jsonOk({ docker: 'reachable' }),
    [`/api/workspace/${id}/workers`]: jsonOk([
      {
        name: 'coder',
        description: 'Writes code',
        system_prompt: 'You write code.',
        tools: ['FileEditor'],
        permission_footprint: { filesystem: 'read' },
        runtime_status: 'ready',
        warning_threshold_tokens: 8000,
      },
    ]),
    [`/api/workspace/${id}/containers`]: jsonOk({
      containers: [{ name: 'app-1', status: 'running', uptime_seconds: 90, note: 'api' }],
    }),
    [`/api/session/list?workspace_id=${id}`]: jsonOk([]),
    '/api/session/create': jsonOk({ session_id: 's-1', mode: 'engineer', name: 'Fix bug' }),
    '/api/resource-catalog': jsonOk({ items: [{ name: 'workspace_files', description: 'Workspace file access' }] }),
    [`/api/workspace/${id}/workers/coder`]: jsonOk({ ok: true }),
    ...extra,
  })
}
beforeEach(() => {
  localStorage.clear()
  window.location.hash = `#/workspace/${ENTRY.id}`
  useWorkspaceStore.getState().reset()
  useWorkspaceStore.setState({ workspaceList: [{ ...ENTRY }] })
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})
function renderPanel() {
  return render(<WorkspacePanel />)
}
async function loadPanel(entry = ENTRY) {
  stubBackend(entry)
  renderPanel()
  await screen.findByRole('heading', { name: entry.label || entry.name })
}
describe('WorkspacePanel — loading and header', () => {
  it('loads the workspace from the backend and renders the header', async () => {
    await loadPanel()
    expect(screen.getByText('Low')).toBeInTheDocument()
    expect(screen.getByText('~/workspaces/ws-test-1')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'New Session' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '← Back to workspaces' })).toBeInTheDocument()
  })
  it('shows the loading state while the config fetch is pending', () => {
    stubBackend(ENTRY, { '/api/workspace/list': () => new Promise(() => {}) })
    renderPanel()
    expect(screen.getByText('Loading workspace…')).toBeInTheDocument()
    expect(screen.getByLabelText('Loading workspace')).toBeInTheDocument()
  })
  it('shows "No workspace selected." when the route has no workspace id', () => {
    stubBackend()
    window.location.hash = '#/workspaces'
    renderPanel()
    expect(screen.getByText('No workspace selected.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '← Back to workspaces' })).toBeInTheDocument()
  })
  it('warns when Docker health could not be verified', async () => {
    stubBackend(ENTRY, {
      '/api/health/containers': () => {
        throw new Error('network down')
      },
    })
    renderPanel()
    await screen.findByRole('heading', { name: 'Code Development' })
    expect(
      screen.getByText('Could not verify Docker status — some features may be unavailable.')
    ).toBeInTheDocument()
  })
})
describe('WorkspacePanel — tab navigation', () => {
  it('defaults to Session Defaults and renders every tab', async () => {
    await loadPanel()
    expect(screen.getByRole('heading', { name: 'Session Defaults' })).toBeInTheDocument()
    expect(screen.getByText('System prompt')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: 'Resources' }))
    expect(screen.getByRole('heading', { name: 'Resources' })).toBeInTheDocument()
    expect(screen.getAllByText('✓ Containerized').length).toBeGreaterThanOrEqual(4)
    expect(screen.getByRole('button', { name: 'Add Resource' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: 'Permissions' }))
    expect(screen.getByRole('heading', { name: 'Permissions' })).toBeInTheDocument()
    expect(screen.getByText('execution')).toBeInTheDocument()
    expect(screen.getAllByText('banned').length).toBeGreaterThanOrEqual(1)
    fireEvent.click(screen.getByRole('tab', { name: 'Tools' }))
    expect(screen.getByRole('heading', { name: 'Tools' })).toBeInTheDocument()
    expect(screen.getByText('FileEditor')).toBeInTheDocument()
    expect(screen.getByText('DockerTool')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: 'Credentials' }))
    expect(screen.getByText('No credentials assigned to this workspace.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: 'Containers' }))
    expect(screen.getByRole('heading', { name: 'Containers' })).toBeInTheDocument()
    expect(screen.getByText('1 of 4 containers used')).toBeInTheDocument()
    expect(screen.getByText('app-1')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: 'Workers' }))
    expect(screen.getByRole('heading', { name: 'Workers' })).toBeInTheDocument()
    expect(screen.getByText('coder')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: 'Session Defaults' }))
    expect(screen.getByText('System prompt')).toBeInTheDocument()
  })
})
describe('WorkspacePanel — edits reach the store', () => {
  it('toggles a resource and persists the change locally', async () => {
    await loadPanel()
    fireEvent.click(screen.getByRole('tab', { name: 'Resources' }))
    const gitCard = screen.getByText('git').closest('.wp-resource-card')
    fireEvent.click(within(gitCard).getByLabelText('Enabled'))
    await waitFor(() => {
      const res = useWorkspaceStore.getState().currentWorkspace.resources.find((r) => r.name === 'git')
      expect(res.enabled).toBe(false)
    })
    const overlay = JSON.parse(localStorage.getItem('tm.workspace.local.ws-test-1'))
    expect(overlay.resources.find((r) => r.name === 'git').enabled).toBe(false)
  })
  it('changes a permission ceiling from the Permissions tab', async () => {
    await loadPanel()
    fireEvent.click(screen.getByRole('tab', { name: 'Permissions' }))
    const row = screen.getByText('network').closest('tr')
    fireEvent.change(within(row).getByRole('combobox'), { target: { value: 'banned' } })
    await waitFor(() => {
      const p = useWorkspaceStore.getState().currentWorkspace.permissions.find((x) => x.name === 'network')
      expect(p.ceiling).toBe('banned')
      expect(p.effective).toBe('banned')
    })
  })
  it('toggles a tool default from the Tools tab', async () => {
    await loadPanel()
    fireEvent.click(screen.getByRole('tab', { name: 'Tools' }))
    const row = screen.getByText('GitTool').closest('.wp-tool-row')
    fireEvent.click(within(row).getByLabelText('Default ON'))
    await waitFor(() => {
      const t = useWorkspaceStore.getState().currentWorkspace.tools.find((x) => x.name === 'GitTool')
      expect(t.defaultOn).toBe(false)
    })
  })
})
describe('WorkspacePanel — New Session flow', () => {
  it('creates a session through the New Session modal', async () => {
    const fetchMock = stubBackend()
    renderPanel()
    await screen.findByRole('heading', { name: 'Code Development' })
    fireEvent.click(screen.getByRole('button', { name: 'New Session' }))
    await screen.findByRole('dialog', { name: 'New session' })
    fireEvent.change(screen.getByPlaceholderText('e.g. Refactor auth module'), {
      target: { value: 'Fix bug' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Create Session' }))
    await waitFor(() => {
      const post = fetchMock.mock.calls.find(
        ([u, o]) => String(u).includes('/api/session/create') && o?.method === 'POST'
      )
      expect(post).toBeTruthy()
      const body = JSON.parse(post[1].body)
      expect(body.workspace_id).toBe('ws-test-1')
      expect(body.workspace_path).toBe('~/workspaces/ws-test-1')
      expect(body.name).toBe('Fix bug')
      expect(body.mode).toBe('engineer')
    })
    await waitFor(() => expect(screen.getByText('Session created.')).toBeInTheDocument())
  })
})
describe('WorkspacePanel — safety advisory', () => {
  it('shows the green advisory for a low-risk workspace with no warnings', async () => {
    await loadPanel()
    expect(screen.getByRole('img', { name: 'green' })).toBeInTheDocument()
    expect(screen.getByText('Low risk — standard guardrails apply.')).toBeInTheDocument()
    expect(screen.getByText('All guardrails active — no action needed.')).toBeInTheDocument()
  })
  it('shows the amber advisory for a medium-risk purpose', async () => {
    const entry = { id: 'ws-med-1', label: 'Social Media Bot', root: '~/workspaces/ws-med-1' }
    stubBackend(entry)
    useWorkspaceStore.setState({ workspaceList: [{ ...entry }] })
    window.location.hash = '#/workspace/ws-med-1'
    renderPanel()
    await screen.findByRole('heading', { name: 'Social Media Bot' })
    expect(screen.getByRole('img', { name: 'amber' })).toBeInTheDocument()
    expect(screen.getByText('Medium risk — extra review recommended.')).toBeInTheDocument()
  })
  it('shows the red advisory for a high-risk purpose', async () => {
    const entry = { id: 'ws-hw-1', label: 'Hardware Hacking', root: '~/workspaces/ws-hw-1' }
    stubBackend(entry)
    useWorkspaceStore.setState({ workspaceList: [{ ...entry }] })
    window.location.hash = '#/workspace/ws-hw-1'
    renderPanel()
    await screen.findByRole('heading', { name: 'Hardware Hacking' })
    expect(screen.getByRole('img', { name: 'red' })).toBeInTheDocument()
    expect(screen.getByText('High risk — restricted environment required.')).toBeInTheDocument()
  })
  it('warns about host-only resources and disables one on demand', async () => {
    stubBackend()
    localStorage.setItem(
      'tm.workspace.local.ws-test-1',
      JSON.stringify({
        resources: [
          { name: 'serial', icon: '🔌', description: 'Serial / device access', containerized: false, risk: 'Low', enabled: true },
        ],
      })
    )
    renderPanel()
    await screen.findByRole('heading', { name: 'Code Development' })
    expect(
      screen.getByText('Host-only resources enabled — containerize or disable them.')
    ).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Disable serial' }))
    await waitFor(() => {
      const res = useWorkspaceStore.getState().currentWorkspace.resources.find((r) => r.name === 'serial')
      expect(res.enabled).toBe(false)
    })
  })
  it('shows the red advisory and Install Docker hint when Docker is down', async () => {
    stubBackend(ENTRY, { '/api/health/containers': jsonOk({ docker: 'down' }) })
    localStorage.setItem(
      'tm.workspace.local.ws-test-1',
      JSON.stringify({
        resources: [
          { name: 'serial', icon: '🔌', description: 'Serial / device access', containerized: false, risk: 'Low', enabled: true },
        ],
      })
    )
    renderPanel()
    await screen.findByRole('heading', { name: 'Code Development' })
    expect(screen.getByText('Host-only resources enabled and Docker is unavailable.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Install Docker' }))
    expect(
      screen.getByText('Install Docker and restart the server to enable containerized execution.')
    ).toBeInTheDocument()
  })
  it('warns when the network permission exceeds the purpose default and lowers it', async () => {
    stubBackend(ENTRY, {
      '/api/workspace/ws-test-1/effective_permissions': jsonOk({
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
    renderPanel()
    await screen.findByRole('heading', { name: 'Code Development' })
    expect(
      screen.getByText('Network permission is higher than this purpose requires.')
    ).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Lower network to read' }))
    await waitFor(() => {
      const p = useWorkspaceStore.getState().currentWorkspace.permissions.find((x) => x.name === 'network')
      expect(p.effective).toBe('read')
    })
  })
})
