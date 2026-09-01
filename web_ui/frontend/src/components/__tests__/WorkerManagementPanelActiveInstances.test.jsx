// @vitest-environment jsdom
/*
 * WorkerManagementPanelActiveInstances.test.jsx — direct-render tests for the
 * ACTIVE-INSTANCE running list of WorkerManagementPanel (controlled component).
 *
 * Mocking mirrors SessionTabsIntegration / SessionWorkerIsolation: a fetch
 * stub that matches registered route keys by URL substring (longest key wins),
 * installed via vi.stubGlobal('fetch') and torn down in afterEach.
 *
 * Contract under test:
 *   - the running list is fed ONLY by GET /api/workspace/{ws}/workers/active
 *     (with ?session_id= when a session is given) — never a bare /workers GET
 *   - templates come ONLY from the GLOBAL /api/workspace/templates endpoint
 *     into the "From Template" creation modal; no template rows in the list
 *   - rows render normalized instance data (labels, runtime status, elapsed,
 *     tools count, ctx tokens, heartbeat info, pause/resume/stop actions)
 *   - exact empty-state copy
 *   - click-to-select contract: onSelectWorker(name, workspaceId, instance_id,
 *     instance_label), plus the auto-open effect firing once for a newly
 *     appeared active worker
 *
 * NOTE: container_id is normalized into each row's data but the component does
 * NOT render it anywhere, so no container_id display assertion is possible.
 */
import React from 'react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import WorkerManagementPanel from '../WorkerManagementPanel'

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

const WORKSPACE_ID = 'ws-test-1'
const SESSION_ID = 'sess-1'
const ACTIVE_URL = `/api/workspace/${WORKSPACE_ID}/workers/active`
const TEMPLATES_URL = '/api/workspace/templates'

const BUSY_WORKER = {
  worker_name: 'coder',
  instance_id: 1,
  instance_label: 'coder',
  status: 'busy',
  elapsed: 130,
  tools: ['bash', 'file'],
  current_context_tokens: 12400,
  max_context_tokens: 64000,
  time_since_last_query: 12,
  container_id: 'abc123',
}

const READY_WORKER = {
  worker_name: 'coder',
  instance_id: 2,
  instance_label: 'coder#2',
  status: 'ready',
  elapsed: 0,
  tools: [],
  time_since_last_query: null,
  last_heartbeat: null,
  paused_manually: true,
}

function renderPanel({ workers = [], onSelectWorker = vi.fn(), selectedWorker = null } = {}) {
  const fetchMock = stubFetchByUrl({
    [ACTIVE_URL]: jsonOk(workers),
    [TEMPLATES_URL]: jsonOk([]),
  })
  render(
    <WorkerManagementPanel
      workspaceId={WORKSPACE_ID}
      sessionId={SESSION_ID}
      onSelectWorker={onSelectWorker}
      selectedWorker={selectedWorker}
      isActive
    />
  )
  return { fetchMock, onSelectWorker }
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('WorkerManagementPanel — active instances', () => {
  it('loads the running list from /workers/active?session_id= and renders normalized rows', async () => {
    const { fetchMock } = renderPanel({ workers: [BUSY_WORKER, READY_WORKER] })

    // Running list comes ONLY from the session-scoped active endpoint.
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(`${ACTIVE_URL}?session_id=${SESSION_ID}`)
    )
    const urls = fetchMock.mock.calls.map(([u]) => String(u))
    expect(urls.filter((u) => u.includes('/workers')).every((u) => u.includes('/workers/active'))).toBe(
      true
    )

    // Rows: instance labels (coder / coder#2), runtime status, elapsed.
    expect(await screen.findByText('coder')).toBeInTheDocument()
    expect(screen.getByText('coder#2')).toBeInTheDocument()
    expect(screen.getByText('busy')).toBeInTheDocument()
    expect(screen.getByText('ready')).toBeInTheDocument()
    expect(screen.getByText('2m 10s')).toBeInTheDocument()
    expect(screen.getByText('0s')).toBeInTheDocument()

    // Tools count + compact token indicator.
    expect(screen.getByText('2')).toBeInTheDocument()
    expect(screen.getByText('0')).toBeInTheDocument()
    expect(screen.getByTitle('Context tokens').textContent).toContain('12.4k')

    // Pause + Stop actions for running workers; manual-pause badge.
    expect(screen.getAllByRole('button', { name: '⏸ Pause' })).toHaveLength(2)
    expect(screen.getAllByRole('button', { name: 'Stop' })).toHaveLength(2)
    expect(screen.getByText('manual pause')).toBeInTheDocument()

    // Templates fetched on mount from the GLOBAL endpoint (modal source only).
    expect(fetchMock).toHaveBeenCalledWith(TEMPLATES_URL)
  })

  it('shows the exact empty-state copy when no active instances are running', async () => {
    const { fetchMock } = renderPanel({ workers: [] })

    expect(
      await screen.findByText('No workers configured. Create one now, or start from a template.')
    ).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: '+ New Worker' })).toHaveLength(2)
    expect(screen.getAllByRole('button', { name: 'From Template' })).toHaveLength(2)
    expect(fetchMock).toHaveBeenCalledWith(`${ACTIVE_URL}?session_id=${SESSION_ID}`)
  })

  it('auto-opens a newly appeared active worker; row click re-selects via onSelectWorker(name, workspaceId, instance_id, instance_label)', async () => {
    const worker = { ...BUSY_WORKER, instance_id: 2, instance_label: 'coder#2' }
    const onSelectWorker = vi.fn()
    renderPanel({ workers: [worker], onSelectWorker })

    // Auto-open: first fetch of an active instance fires onSelectWorker once.
    await waitFor(() => expect(onSelectWorker).toHaveBeenCalledTimes(1))
    expect(onSelectWorker).toHaveBeenCalledWith('coder', WORKSPACE_ID, 2, 'coder#2')

    // Row click adds a second call carrying the instance identity.
    fireEvent.click(await screen.findByText('coder#2'))
    await waitFor(() => expect(onSelectWorker).toHaveBeenCalledTimes(2))
    expect(onSelectWorker.mock.calls[1]).toEqual(['coder', WORKSPACE_ID, 2, 'coder#2'])
  })

  it('Stop POSTs to /workers/{name}/stop?instance_id= and flips the row to stopped', async () => {
    const stopRoute = `/api/workspace/${WORKSPACE_ID}/workers/coder/stop`
    const fetchMock = stubFetchByUrl({
      [ACTIVE_URL]: jsonOk([BUSY_WORKER]),
      [TEMPLATES_URL]: jsonOk([]),
      [stopRoute]: jsonOk({ ok: true }),
    })
    render(
      <WorkerManagementPanel
        workspaceId={WORKSPACE_ID}
        sessionId={SESSION_ID}
        onSelectWorker={vi.fn()}
        selectedWorker={null}
        isActive
      />
    )

    fireEvent.click(await screen.findByRole('button', { name: 'Stop' }))
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(`${stopRoute}?instance_id=1`, { method: 'POST' })
    )
    expect(await screen.findByText('stopped')).toBeInTheDocument()
  })

  it('Pause POSTs to /workers/{name}/pause?instance_id= and shows Pausing…', async () => {
    const pauseRoute = `/api/workspace/${WORKSPACE_ID}/workers/coder/pause`
    const fetchMock = stubFetchByUrl({
      [ACTIVE_URL]: jsonOk([BUSY_WORKER]),
      [TEMPLATES_URL]: jsonOk([]),
      [pauseRoute]: jsonOk({ ok: true }),
    })
    render(
      <WorkerManagementPanel
        workspaceId={WORKSPACE_ID}
        sessionId={SESSION_ID}
        onSelectWorker={vi.fn()}
        selectedWorker={null}
        isActive
      />
    )

    fireEvent.click(await screen.findByRole('button', { name: '⏸ Pause' }))
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(`${pauseRoute}?instance_id=1`, { method: 'POST' })
    )
    expect((await screen.findAllByText('Pausing…')).length).toBeGreaterThan(0)
  })

  it('Resume POSTs to /workers/{name}/resume?instance_id= for a paused worker and flips to ready', async () => {
    const paused = { ...BUSY_WORKER, status: 'paused', elapsed: null, time_since_last_query: null }
    const resumeRoute = `/api/workspace/${WORKSPACE_ID}/workers/coder/resume`
    const fetchMock = stubFetchByUrl({
      [ACTIVE_URL]: jsonOk([paused]),
      [TEMPLATES_URL]: jsonOk([]),
      [resumeRoute]: jsonOk({ ok: true }),
    })
    render(
      <WorkerManagementPanel
        workspaceId={WORKSPACE_ID}
        sessionId={SESSION_ID}
        onSelectWorker={vi.fn()}
        selectedWorker={null}
        isActive
      />
    )

    expect(await screen.findByText('Paused')).toBeInTheDocument()
    // A paused worker cannot be stopped.
    expect(screen.queryByRole('button', { name: 'Stop' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '▶ Resume' }))
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(`${resumeRoute}?instance_id=1`, { method: 'POST' })
    )
    expect(await screen.findByText('ready')).toBeInTheDocument()
  })

  it('handles a null heartbeat without crashing and renders time-since-last-query when present', async () => {
    const noHeartbeat = { ...BUSY_WORKER, time_since_last_query: null, last_heartbeat: null }
    const withHeartbeat = {
      worker_name: 'doc',
      instance_id: 1,
      instance_label: 'doc',
      status: 'ready',
      last_heartbeat: new Date().toISOString(),
    }
    stubFetchByUrl({
      [ACTIVE_URL]: jsonOk([noHeartbeat, withHeartbeat]),
      [TEMPLATES_URL]: jsonOk([]),
    })
    render(
      <WorkerManagementPanel
        workspaceId={WORKSPACE_ID}
        sessionId={SESSION_ID}
        onSelectWorker={vi.fn()}
        selectedWorker={null}
        isActive
      />
    )

    expect(await screen.findByText('coder')).toBeInTheDocument()
    expect(screen.getByText('doc')).toBeInTheDocument()
    // The only "Time since last query" span is the one with a heartbeat.
    expect(screen.getByTitle('Time since last query').textContent).toBe('just now')
  })

  it('From Template opens the creation modal fed by the GLOBAL templates endpoint', async () => {
    const templates = [{ name: 'tmpl-a', description: 'Template A', tools: ['bash'] }]
    const fetchMock = stubFetchByUrl({
      [ACTIVE_URL]: jsonOk([]),
      [TEMPLATES_URL]: jsonOk(templates),
    })
    render(
      <WorkerManagementPanel
        workspaceId={WORKSPACE_ID}
        sessionId={SESSION_ID}
        onSelectWorker={vi.fn()}
        selectedWorker={null}
        isActive
      />
    )

    await screen.findByText('No workers configured. Create one now, or start from a template.')
    fireEvent.click(screen.getAllByRole('button', { name: 'From Template' })[0])

    expect(await screen.findByText('New Worker from Template')).toBeInTheDocument()
    expect(screen.getByText('tmpl-a')).toBeInTheDocument()

    // Templates only ever come from the global endpoint — never workspace-scoped.
    const templateCalls = fetchMock.mock.calls
      .map(([u]) => String(u))
      .filter((u) => u.includes('templates'))
    expect(templateCalls.length).toBeGreaterThan(0)
    expect(templateCalls.every((u) => u === TEMPLATES_URL)).toBe(true)

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByText('New Worker from Template')).not.toBeInTheDocument()
  })
})
