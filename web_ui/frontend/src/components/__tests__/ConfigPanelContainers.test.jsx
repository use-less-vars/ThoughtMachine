// @vitest-environment jsdom
/*
 * ConfigPanelContainers.test.jsx — the Live View tab hosts the container list.
 *
 * ConfigPanel's default tab is 'live_view'; it renders SessionWorkspaceTab,
 * which mounts WorkerManagementPanel (workers) and ContainerListPanel
 * (containers) for the bound workspace. This suite pins that wiring:
 *
 *   * the Containers section renders on the DEFAULT tab (no click needed),
 *   * the list is fetched from /api/workspace/{id}/containers,
 *   * an ephemeral row exposes the shared chip (title = SHARED_TITLE, the
 *     C.1 contract string), and
 *   * an unbound panel (no workspaceId) never mounts the list at all.
 *
 * Renders the REAL ConfigPanel with a stubbed global fetch, following the
 * ConfigPanelDraft.test.jsx / ConfigPanel.test.jsx convention.
 */

import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import ConfigPanel from '../ConfigPanel'
import useStore from '../../store/useStore'

// Copy pinned by the C.1 contract (mirrors ContainerListPanel's SHARED_TITLE).
const SHARED_TITLE = 'Stopping affects other sessions in this workspace.'

function jsonOk(data, status = 200) {
  return { ok: true, status, json: async () => data, text: async () => JSON.stringify(data) }
}

const DEFAULT_FALLBACK = {
  ok: true,
  status: 200,
  json: async () => ({ tools: [] }),
  text: async () => '',
}

// One live, SHARED ephemeral container with a permission snapshot.
const EPHEMERAL = {
  id: 'c-eph',
  name: 'session-s1-eph',
  kind: 'ephemeral',
  state: 'running',
  live: true,
  shared: true,
  permissions: { network: false, filesystem: 'read', mem_limit: '512m' },
}

// Stub the routes ConfigPanel + its Live View tab touch on mount. Longest
// matching key wins, so '/api/workspace/ws-1/containers' is never swallowed by
// a shorter prefix.
function stubBackend(containers = []) {
  const routes = {
    '/api/tools': jsonOk({ tools: [] }),
    '/api/health/containers': jsonOk({ docker: 'reachable' }),
    '/api/workspace/ws-1/workers': jsonOk([]),
    '/api/workspace/ws-1/containers': jsonOk({ containers }),
    '/api/workspace/ws-1/effective_permissions': jsonOk({ effective_permissions: {} }),
    '/api/workspace/list': jsonOk([{ id: 'ws-1', label: 'Code Development', root: '/root' }]),
  }
  const fetchMock = vi.fn(async (url) => {
    const s = String(url)
    const key = Object.keys(routes)
      .filter((k) => s.includes(k))
      .sort((a, b) => b.length - a.length)[0]
    return routes[key] || DEFAULT_FALLBACK
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

const CONFIG = { mode: 'custom', session_permissions: { network: 'banned' } }

function renderPanel(props = {}) {
  const sendCommand = vi.fn()
  const utils = render(
    <ConfigPanel
      config={CONFIG}
      sendCommand={sendCommand}
      providers={[]}
      availableTools={[]}
      wsConnected
      workspaceId="ws-1"
      sessionId="s1"
      {...props}
    />
  )
  return { sendCommand, ...utils }
}

beforeEach(() => {
  useStore.getState().reset()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  useStore.getState().reset()
})

describe('ConfigPanel Live View tab — container list wiring', () => {
  it('defaults to the Live View tab and mounts the Containers section with all three groups', async () => {
    stubBackend([])
    renderPanel()

    // Live View is the DEFAULT tab — no click required.
    expect(screen.getByRole('button', { name: 'Live View' })).toBeInTheDocument()
    // The Containers section header is present on the default tab.
    expect(screen.getByText('Containers')).toBeInTheDocument()

    // All three groups exist and the pinned empty-state copy renders.
    expect(screen.getByTestId('container-group-ephemeral')).toBeInTheDocument()
    expect(screen.getByTestId('container-group-persistent')).toBeInTheDocument()
    expect(screen.getByTestId('container-group-resource')).toBeInTheDocument()
    expect(await screen.findByText('No ephemeral containers.')).toBeInTheDocument()
    expect(screen.getByText('No persistent containers.')).toBeInTheDocument()
    expect(screen.getByText('No resource containers.')).toBeInTheDocument()

    // ...and the list was fetched from the workspace-scoped endpoint.
    await waitFor(() =>
      expect(globalThis.fetch).toHaveBeenCalledWith('/api/workspace/ws-1/containers')
    )
  })

  it('renders an ephemeral container row with its shared chip', async () => {
    stubBackend([EPHEMERAL])
    renderPanel()

    const row = await screen.findByTestId('container-row-c-eph')
    expect(row).toHaveTextContent('session-s1-eph')
    // The empty state is replaced by the row.
    expect(screen.queryByText('No ephemeral containers.')).not.toBeInTheDocument()

    const chip = screen.getByTestId('container-shared-c-eph')
    expect(chip).toHaveTextContent('shared')
    expect(chip).toHaveAttribute('title', SHARED_TITLE)
  })

  it('does not mount the container list when the session is unbound (no workspaceId)', async () => {
    stubBackend([])
    renderPanel({ workspaceId: null })

    expect(await screen.findByText('No workspace loaded.')).toBeInTheDocument()
    expect(screen.queryByTestId('container-group-ephemeral')).toBeNull()
    expect(globalThis.fetch).not.toHaveBeenCalledWith('/api/workspace/ws-1/containers')
  })
})
