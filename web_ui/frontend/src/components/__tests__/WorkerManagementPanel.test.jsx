// @vitest-environment jsdom
/*
 * WorkerManagementPanel.test.jsx — deliverable 1: left worker tray.
 *
 * Covers:
 *  1. instance_label is displayed as the row label
 *  2. compact token indicator (current / max context tokens, '12.4k / 80k')
 *  3. 'manual pause' badge on/off driven by payload.paused_manually
 *  4. time_since_last_query formatted ('2m 10s') with fallback to
 *     relativeTime(last_heartbeat) when null
 *  5. real runtime_status surfaced in the status text ('Paused')
 */

import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor, fireEvent } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import WorkerManagementPanel from '../WorkerManagementPanel'

class MockResizeObserver {
  constructor(callback) {
    this.callback = callback
  }
  observe() {}
  unobserve() {}
  disconnect() {}
}

// ── Worker payload fixture (real API shape) ────────────────────────────────
function makeWorker(overrides = {}) {
  return {
    name: 'docgen',
    instance_id: 2,
    instance_label: 'docgen#2',
    runtime_status: 'paused',
    paused_manually: true,
    current_context_tokens: 12400,
    max_context_tokens: 80000,
    time_since_last_query: 130,
    last_heartbeat: new Date(Date.now() - 10 * 60000).toISOString(),
    description: '',
    tools: [],
    permission_footprint: {},
    ...overrides,
  }
}

let workersPayload

function renderPanel(workerOverrides = {}) {
  workersPayload = [makeWorker(workerOverrides)]
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url) => {
      if (String(url).includes('/workers')) {
        return { ok: true, status: 200, json: async () => workersPayload }
      }
      return { ok: true, status: 200, json: async () => [] }
    })
  )
  return render(
    <WorkerManagementPanel
      workspaceId="ws-1"
      sessionId="sess-1"
      onSelectWorker={vi.fn()}
      selectedWorker={null}
      isActive
    />
  )
}

async function waitForRow() {
  await waitFor(() => expect(screen.getByText('docgen#2')).toBeInTheDocument())
}

describe('WorkerManagementPanel — deliverable 1 tray', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.stubGlobal('ResizeObserver', MockResizeObserver)
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('renders the instance_label as the worker row label', async () => {
    renderPanel()
    await waitForRow()
    expect(screen.getByText('docgen#2')).toBeInTheDocument()
  })

  it('shows a compact token indicator (current / max)', async () => {
    renderPanel()
    await waitForRow()
    const tokens = screen.getByTitle('Context tokens')
    expect(tokens.textContent).toMatch(/12\.4k\s*\/\s*80k/)
  })

  it('shows only the current token count when max_context_tokens is missing', async () => {
    renderPanel({ max_context_tokens: null })
    await waitForRow()
    const tokens = screen.getByTitle('Context tokens')
    expect(tokens.textContent).toContain('ctx: 12.4k')
    expect(tokens.textContent).not.toContain('/')
  })

  it('omits the token indicator when current_context_tokens is null', async () => {
    renderPanel({ current_context_tokens: null })
    await waitForRow()
    expect(screen.queryByTitle('Context tokens')).not.toBeInTheDocument()
  })

  it('shows the manual pause badge when paused_manually is true', async () => {
    renderPanel({ paused_manually: true })
    await waitForRow()
    expect(screen.getByText('manual pause')).toBeInTheDocument()
  })

  it('omits the manual pause badge when paused_manually is falsy', async () => {
    renderPanel({ paused_manually: false })
    await waitForRow()
    expect(screen.queryByText('manual pause')).not.toBeInTheDocument()
  })

  it('renders time_since_last_query in human-readable form', async () => {
    renderPanel({ time_since_last_query: 130 })
    await waitForRow()
    expect(screen.getByText('2m 10s')).toBeInTheDocument()
  })

  it('falls back to heartbeat-relative time when time_since_last_query is null', async () => {
    renderPanel({ time_since_last_query: null })
    await waitForRow()
    expect(screen.getByText('10m ago')).toBeInTheDocument()
  })

  it('surfaces the real runtime_status in the status text', async () => {
    renderPanel({ runtime_status: 'paused' })
    await waitForRow()
    expect(screen.getByText('Paused')).toBeInTheDocument()
  })
})

// ── Stop All Workers button ───────────────────────────────────────────────
// Promotes the backend POST /api/workspace/{ws_id}/workers/stop_all route
// into a single destructive "Stop All Workers" action guarded by a
// confirmation dialog.
function renderWithFetch(stopAllResponse) {
  const calls = []
  workersPayload = [makeWorker()]
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url, opts = {}) => {
      const u = String(url)
      calls.push({ url: u, method: (opts.method || 'GET').toUpperCase() })
      if (u.includes('/workers/stop_all')) {
        return stopAllResponse
      }
      if (u.includes('/workers')) {
        return { ok: true, status: 200, json: async () => workersPayload }
      }
      return { ok: true, status: 200, json: async () => [] }
    })
  )
  render(
    <WorkerManagementPanel
      workspaceId="ws-1"
      sessionId="sess-1"
      onSelectWorker={vi.fn()}
      selectedWorker={null}
      isActive
    />
  )
  return calls
}

describe('WorkerManagementPanel — Stop All Workers', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.stubGlobal('ResizeObserver', MockResizeObserver)
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('renders a single "Stop All Workers" button', async () => {
    renderWithFetch({ ok: true, status: 200, json: async () => [] })
    await waitForRow()
    expect(
      screen.getByRole('button', { name: 'Stop All Workers' })
    ).toBeInTheDocument()
  })

  it('shows a confirmation dialog and sends no request until confirmed', async () => {
    const calls = renderWithFetch({ ok: true, status: 200, json: async () => [] })
    await waitForRow()

    fireEvent.click(screen.getByRole('button', { name: 'Stop All Workers' }))

    expect(
      screen.getByRole('heading', { name: 'Stop All Workers' })
    ).toBeInTheDocument()
    expect(calls.filter((c) => c.url.includes('/workers/stop_all'))).toHaveLength(0)
  })

  it('POSTs exactly once to the stop_all endpoint after confirming', async () => {
    const calls = renderWithFetch({ ok: true, status: 200, json: async () => [] })
    await waitForRow()

    fireEvent.click(screen.getByRole('button', { name: 'Stop All Workers' }))
    fireEvent.click(screen.getByRole('button', { name: 'Stop All' }))

    await waitFor(() => {
      expect(calls.filter((c) => c.url.includes('/workers/stop_all'))).toHaveLength(1)
    })
    const call = calls.find((c) => c.url.includes('/workers/stop_all'))
    expect(call.url).toBe('/api/workspace/ws-1/workers/stop_all')
    expect(call.method).toBe('POST')
    expect(calls.filter((c) => c.url.includes('/workers/stop_all'))).toHaveLength(1)
  })

  it('surfaces a failed stop_all response via an alert role', async () => {
    renderWithFetch({
      ok: false,
      status: 500,
      json: async () => ({ detail: { error: 'stop_all exploded' } }),
    })
    await waitForRow()

    fireEvent.click(screen.getByRole('button', { name: 'Stop All Workers' }))
    fireEvent.click(screen.getByRole('button', { name: 'Stop All' }))

    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent('stop_all exploded')
    )
  })
})
