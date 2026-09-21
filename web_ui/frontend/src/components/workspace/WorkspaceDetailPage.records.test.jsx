// @vitest-environment jsdom
// --- WorkspaceDetailPage.records.test.jsx ---
// Additive suite: the workspace detail page must expose the record-aware
// container panel (RecordContainerPanel) in the WORKSPACE context via a new
// "Records" tab, SCOPED to the workspace -- records are filtered by their own
// `workspace_id` field. The Global mount (WorkspaceSelector) is untouched.
//
// This file does NOT modify WorkspaceDetailPage.test.jsx; it re-renders the
// same component with the container-records route stubbed to return records
// from TWO workspaces so the workspace scoping can be observed.

import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import WorkspaceDetailPage from './WorkspaceDetailPage'

const WORKSPACE_ID = 'ws-1'
const OTHER_WORKSPACE_ID = 'ws-2'

function jsonOk(data, status = 200) {
  return { ok: true, status, json: async () => data, text: async () => JSON.stringify(data) }
}

// Stub fetch by substring route (longest key wins) -- the same pattern used by
// the other workspace suites.
function stubFetchByUrl(routes, defaultResponse = jsonOk({})) {
  const fetchMock = vi.fn(async (url) => {
    const key = Object.keys(routes)
      .filter((k) => String(url).includes(k))
      .sort((a, b) => b.length - a.length)[0]
    if (!key) return defaultResponse
    const resp = routes[key]
    return typeof resp === 'function' ? resp(url) : resp
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function makeSummary(overrides = {}) {
  return {
    workspace_id: WORKSPACE_ID,
    label: 'Research Sandbox',
    root_path: '/home/jojo/workspaces/research',
    allow_host_resources: false,
    permissions: {},
    dockerfile: { path: '/home/jojo/workspaces/research/Dockerfile', content: 'FROM python:3.11\n' },
    worker_templates: [],
    active_workers: [],
    active_sessions: [],
    active_containers: [],
    tools: [],
    resource_catalog: [],
    ...overrides,
  }
}

// Two records, one per workspace, both returned by the single list endpoint's
// payload (the unscoped panel lists every workspace's records).
const RECORDS = [
  {
    container_id: 'rec-ws1',
    id: 'rec-ws1',
    name: 'alpha-ctr',
    workspace_id: WORKSPACE_ID,
    state: 'running',
    lifecycle_class: 'ephemeral',
    drift: [],
  },
  {
    container_id: 'rec-ws2',
    id: 'rec-ws2',
    name: 'beta-ctr',
    workspace_id: OTHER_WORKSPACE_ID,
    state: 'running',
    lifecycle_class: 'ephemeral',
    drift: [],
  },
]

function routesFor(summary) {
  return {
    [`/api/workspace/${WORKSPACE_ID}/summary`]: () => jsonOk(summary),
    '/api/container-records': () => jsonOk({ records: RECORDS, count: RECORDS.length }),
  }
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

describe('WorkspaceDetailPage \u2014 Records tab (record panel in workspace context)', () => {
  it('exposes the record-aware container panel as a dedicated workspace tab', async () => {
    stubFetchByUrl(routesFor(makeSummary()))
    render(<WorkspaceDetailPage workspaceId={WORKSPACE_ID} />)
    await screen.findByText('Research Sandbox')

    // A dedicated "Records" tab exists (the Containers tab stays read-only).
    const recordsTab = screen.getByRole('tab', { name: 'Records' })
    fireEvent.click(recordsTab)

    // The panel is reachable: its heading and refresh control render.
    expect(await screen.findByText('Container Records')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Refresh Records' })).toBeInTheDocument()
  })

  it("scopes the rendered records to the workspace via each record's own workspace_id", async () => {
    stubFetchByUrl(routesFor(makeSummary()))
    render(<WorkspaceDetailPage workspaceId={WORKSPACE_ID} />)
    await screen.findByText('Research Sandbox')
    fireEvent.click(screen.getByRole('tab', { name: 'Records' }))

    // The workspace's OWN record renders...
    expect(await screen.findByText('alpha-ctr')).toBeInTheDocument()
    // ...and another workspace's record does NOT.
    expect(screen.queryByText('beta-ctr')).toBeNull()
  })
})
