// @vitest-environment jsdom
/*
 * WorkspaceDetailPagePermissions.test.jsx — the Permissions & Resources tab of
 * the WorkspaceDetailPage (Layer 2) against a stubbed backend.
 *
 * Stubs GET /api/workspace/{id}/summary, PUT /api/workspace/{id}/permissions
 * and /api/vault/status (WorkspaceDetailPage mounts VaultHealthBanner) and
 * exercises the resource cards, dirty tracking, tool chips and the apply flow.
 */
import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor, within } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import WorkspaceDetailPage from '../workspace/WorkspaceDetailPage'

function jsonOk(data, status = 200) {
  return { ok: true, status, json: async () => data, text: async () => JSON.stringify(data) }
}
function jsonErr(data, status = 422) {
  return { ok: false, status, json: async () => data, text: async () => JSON.stringify(data) }
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

const CATALOG = [
  {
    name: 'git',
    display_name: 'Git',
    description: 'Version control access',
    permission_grain_set: ['banned', 'read', 'ask', 'write'],
    default_execution_context: 'containerized',
    container_image: null,
    dockerfile_reference: null,
    tools: ['git_read', 'git_write'],
  },
  {
    name: 'filesystem',
    display_name: 'Filesystem',
    description: 'Filesystem access',
    permission_grain_set: ['banned', 'read', 'ask', 'write'],
    default_execution_context: 'containerized',
    container_image: null,
    dockerfile_reference: null,
    tools: ['read_file', 'file_editor', 'apply_edits', 'file_search', 'glob'],
  },
  {
    name: 'docker',
    display_name: 'Docker',
    description: 'Docker container execution',
    permission_grain_set: ['banned', 'read', 'ask', 'write'],
    default_execution_context: 'containerized',
    container_image: null,
    dockerfile_reference: null,
    tools: ['docker_code_runner', 'container_control'],
  },
  {
    name: 'host_bash',
    display_name: 'Host bash',
    description: 'Host shell execution',
    permission_grain_set: ['banned', 'read', 'ask', 'write'],
    default_execution_context: 'containerized',
    container_image: null,
    dockerfile_reference: null,
    tools: ['host_bash'],
  },
  {
    name: 'tty',
    display_name: 'TTY',
    description: 'Terminal device access',
    permission_grain_set: ['banned', 'read', 'ask', 'write'],
    default_execution_context: 'containerized',
    container_image: null,
    dockerfile_reference: null,
    tools: [],
  },
  {
    name: 'jtag',
    display_name: 'JTAG',
    description: 'Hardware debug access',
    permission_grain_set: ['banned', 'read', 'ask', 'write'],
    default_execution_context: 'containerized',
    container_image: null,
    dockerfile_reference: null,
    tools: [],
  },
]

function makeSummary(overrides = {}) {
  return {
    workspace_id: 'ws-test-1',
    label: 'Code Development',
    root_path: '~/workspaces/ws-test-1',
    allow_host_resources: false,
    permissions: {
      git: 'read',
      filesystem: 'read',
      docker: 'banned',
      host_bash: 'banned',
      tty: 'read',
      jtag: 'banned',
    },
    resource_catalog: CATALOG,
    active_sessions: [],
    active_workers: [],
    active_containers: [],
    dockerfile: null,
    worker_templates: [],
    tools: [],
    ...overrides,
  }
}

// Stub the summary + vault status and record PUT /permissions bodies.
function stubBackend(entry = makeSummary(), extra = {}) {
  const id = entry.workspace_id
  const putCalls = []
  const fetchMock = stubFetchByUrl({
    [`/api/workspace/${id}/summary`]: () => jsonOk({ ...entry, permissions: { ...entry.permissions } }),
    '/api/vault/status': () => jsonOk({ ok: true }),
    [`/api/workspace/${id}/permissions`]: (url, options) => {
      putCalls.push(options && options.body ? JSON.parse(options.body) : null)
      return jsonOk({ ok: true })
    },
    ...extra,
  })
  return { fetchMock, putCalls }
}

async function renderPermissionsTab() {
  const stub = stubBackend()
  render(<WorkspaceDetailPage workspaceId="ws-test-1" />)
  await screen.findByText('Code Development')
  fireEvent.click(screen.getByRole('tab', { name: 'Permissions & Resources' }))
  await screen.findByText('Resource permissions')
  return stub
}

function cardFor(displayName) {
  return within(screen.getByText(displayName).closest('.wdp-resource-card'))
}

beforeEach(() => {
  localStorage.clear()
  window.location.hash = ''
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('WorkspaceDetailPage \u2014 Permissions & Resources', () => {
  it('renders every resource card with its current permission level', async () => {
    await renderPermissionsTab()
    expect(document.querySelectorAll('.wdp-resource-card').length).toBe(6)
    expect(cardFor('Git').getByRole('combobox')).toHaveValue('read')
    expect(cardFor('Filesystem').getByRole('combobox')).toHaveValue('read')
    expect(cardFor('Docker').getByRole('combobox')).toHaveValue('banned')
    expect(cardFor('Host bash').getByRole('combobox')).toHaveValue('banned')
    expect(cardFor('TTY').getByRole('combobox')).toHaveValue('read')
    expect(cardFor('JTAG').getByRole('combobox')).toHaveValue('banned')
    // Banned resources render as Disabled with an Off switch.
    expect(cardFor('Docker').getByText('Disabled')).toBeInTheDocument()
    expect(cardFor('Docker').getByText('Off')).toBeInTheDocument()
    expect(cardFor('Git').getByText('Enabled')).toBeInTheDocument()
  })

  it('renders tools as chips and none for empty tool lists', async () => {
    await renderPermissionsTab()
    const gitCard = cardFor('Git')
    expect(gitCard.getByText('Tools:')).toBeInTheDocument()
    expect(gitCard.getByText('git_read')).toBeInTheDocument()
    expect(gitCard.getByText('git_write')).toBeInTheDocument()
    expect(cardFor('TTY').getByText('none')).toBeInTheDocument()
  })

  it('marks changes dirty and enables Apply when a permission changes', async () => {
    await renderPermissionsTab()
    const applyHeader = screen.getByRole('button', { name: 'Apply' })
    const applyTab = screen.getByRole('button', { name: 'Apply Permissions' })
    expect(applyHeader).toBeDisabled()
    expect(applyTab).toBeDisabled()
    fireEvent.change(cardFor('Git').getByRole('combobox'), { target: { value: 'write' } })
    expect(screen.getByText('Unsaved changes')).toBeInTheDocument()
    expect(applyHeader).toBeEnabled()
    expect(applyTab).toBeEnabled()
  })

  it('applies the full permission dict and shows a success message', async () => {
    const { putCalls } = await renderPermissionsTab()
    fireEvent.change(cardFor('Git').getByRole('combobox'), { target: { value: 'write' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply Permissions' }))
    await screen.findAllByText('Permissions updated')
    expect(putCalls.length).toBe(1)
    // Host toggle untouched \u2192 allow_host_resources must be OMITTED.
    expect(putCalls[0]).toEqual({
      permissions: { ...makeSummary().permissions, git: 'write' },
    })
    // Draft is discarded once the refetch lands.
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Apply' })).toBeDisabled()
    })
  })

  it('sends allow_host_resources only when the host toggle changed', async () => {
    const { putCalls } = await renderPermissionsTab()
    fireEvent.click(screen.getByRole('switch', { name: 'Toggle host resource execution' }))
    await screen.findByText('Enable host resource execution?')
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))
    await screen.findAllByText('Permissions updated')
    expect(putCalls.length).toBe(1)
    expect(putCalls[0].allow_host_resources).toBe(true)
    expect(putCalls[0].permissions).toEqual(makeSummary().permissions)
  })

  it('keeps the draft and shows the backend error when apply fails', async () => {
    const summary = makeSummary()
    stubFetchByUrl({
      [`/api/workspace/${summary.workspace_id}/summary`]: jsonOk(summary),
      '/api/vault/status': jsonOk({ ok: true }),
      [`/api/workspace/${summary.workspace_id}/permissions`]: jsonErr({
        detail: { errors: ['invalid permission level'] },
      }),
    })
    render(<WorkspaceDetailPage workspaceId={summary.workspace_id} />)
    await screen.findByText('Code Development')
    fireEvent.click(screen.getByRole('tab', { name: 'Permissions & Resources' }))
    await screen.findByText('Resource permissions')
    const gitSelect = cardFor('Git').getByRole('combobox')
    fireEvent.change(gitSelect, { target: { value: 'write' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply Permissions' }))
    await screen.findAllByText(/invalid permission level/)
    // Draft preserved on failure \u2014 the editor must not be reset.
    expect(cardFor('Git').getByRole('combobox')).toHaveValue('write')
    expect(screen.getByRole('button', { name: 'Apply Permissions' })).toBeEnabled()
  })
})
