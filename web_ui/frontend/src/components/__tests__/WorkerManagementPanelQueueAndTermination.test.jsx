// @vitest-environment jsdom
/*
 * WorkerManagementPanelQueueAndTermination.test.jsx — the PHASE 2.1a additions
 * to the worker row: the status vocabulary the BACKEND actually emits, the
 * per-instance query QUEUE, and the Stop-vs-Kill split.
 *
 * Backend contract being surfaced (not invented here):
 *   - `_worker_active_entry` reports `status` verbatim from
 *     WorkerThread.status (web_ui/backend/workspace_routes.py:579) plus
 *     `queued_query_count` / `queued_query_ids` from
 *     `_worker_queued_queries(thread)` (:585, count/ids at :534, FIFO order).
 *   - stop DETACHES (runtime_status 'detached', worker_thread.py:1494):
 *     recoverable, queue preserved → `POST /workers/{name}/stop`.
 *   - kill TERMINATES (runtime_status 'killed', worker_thread.py:1514) and
 *     rejects every queued query (reject_queued_queries, :1533) → destructive,
 *     so it goes through a confirmation dialog → `POST /workers/{name}/kill`.
 *   - resume RUNS the queued work in order, nothing is discarded →
 *     `POST /workers/{name}/resume`.
 *
 * Mocking mirrors WorkerManagementPanelActiveInstances: a fetch stub that
 * matches registered route keys by URL substring (longest key wins), installed
 * via vi.stubGlobal('fetch') and torn down in afterEach.
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

const WORKSPACE_ID = 'ws'
const SESSION_ID = 'sess-1'
const ACTIVE_URL = `/api/workspace/${WORKSPACE_ID}/workers/active`
const TEMPLATES_URL = '/api/workspace/templates'

function makeWorker(overrides = {}) {
  return {
    worker_name: 'coder',
    instance_id: 3,
    instance_label: 'coder#3',
    status: 'busy',
    elapsed: 130,
    tools: ['bash'],
    time_since_last_query: 12,
    ...overrides,
  }
}

function renderPanel({ workers = [], routes = {} } = {}) {
  const fetchMock = stubFetchByUrl({
    [ACTIVE_URL]: jsonOk(workers),
    [TEMPLATES_URL]: jsonOk([]),
    ...routes,
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
  return fetchMock
}

function callsTo(fetchMock, fragment) {
  return fetchMock.mock.calls.filter(([u]) => String(u).includes(fragment))
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('WorkerManagementPanel — status vocabulary', () => {
  it('renders detached and killed distinctly (label and colour)', async () => {
    renderPanel({
      workers: [
        makeWorker({ worker_name: 'parked', instance_id: 1, instance_label: 'parked', status: 'detached' }),
        makeWorker({ worker_name: 'dead', instance_id: 2, instance_label: 'dead', status: 'killed' }),
      ],
    })

    const detached = await screen.findByText('Detached')
    const killed = screen.getByText('Killed')

    // Two different states must never collapse into one indistinguishable row:
    // detached is mauve (parked but ALIVE), killed is red (terminal).
    expect(detached).not.toBe(killed)
    expect(detached).toHaveStyle({ color: '#cba6f7' })
    expect(killed).toHaveStyle({ color: '#f38ba8' })
  })

  it('renders an unrecognized status visibly degraded instead of silently', async () => {
    renderPanel({ workers: [makeWorker({ status: 'zombie' })] })

    const label = await screen.findByText('zombie (unrecognized)')
    expect(label).toBeInTheDocument()
    // Amber: a status this panel cannot explain must not look healthy/normal.
    expect(label).toHaveStyle({ color: '#f9e2af' })
  })
})

describe('WorkerManagementPanel — queued queries', () => {
  it('renders the queued count and exposes the FIFO ids in the title', async () => {
    renderPanel({
      workers: [makeWorker({ queued_query_count: 2, queued_query_ids: ['q-a', 'q-b'] })],
    })

    expect(await screen.findByText('⧗ 2 queued')).toBeInTheDocument()
    expect(screen.getByTitle('Queued queries (FIFO): q-a, q-b')).toBeInTheDocument()
  })

  it('renders "none queued" when the payload omits the queue fields', async () => {
    // Older/otherwise-shaped payloads omit queued_query_count/-_ids entirely;
    // that must read as "nothing queued", never crash and never invent ids.
    renderPanel({ workers: [makeWorker({ status: 'ready' })] })

    expect(await screen.findByText('none queued')).toBeInTheDocument()
    expect(screen.getByTitle('No queued queries')).toBeInTheDocument()
  })
})

describe('WorkerManagementPanel — Stop vs Kill', () => {
  it('offers stop (detach) and kill (terminate) as clearly different actions', async () => {
    renderPanel({ workers: [makeWorker({ status: 'busy' })] })

    const stop = await screen.findByRole('button', { name: 'Stop' })
    const kill = screen.getByRole('button', { name: 'Kill' })

    expect(stop).not.toBe(kill)
    expect(stop.title).toContain('detach')
    expect(stop.title).toContain('recoverable')
    expect(stop.title).toContain('queued queries are kept')
    expect(kill.title).toContain('terminate')
    expect(kill.title).toContain('unrecoverable')

    // Only the unrecoverable action carries the solid danger colour; stop is an
    // outline so the two are not confusable at a glance.
    expect(kill).toHaveStyle({ background: '#f38ba8' })
    expect(stop).not.toHaveStyle({ background: '#f38ba8' })
  })

  it('sends no kill request until the confirmation is accepted', async () => {
    const killRoute = `/api/workspace/${WORKSPACE_ID}/workers/coder/kill`
    const fetchMock = renderPanel({
      workers: [makeWorker({ status: 'busy', queued_query_count: 2, queued_query_ids: ['q-a', 'q-b'] })],
      routes: { [killRoute]: jsonOk({ status: 'ok', name: 'coder' }) },
    })

    fireEvent.click(await screen.findByRole('button', { name: 'Kill' }))

    expect(screen.getByRole('heading', { name: 'Kill Worker' })).toBeInTheDocument()
    expect(callsTo(fetchMock, '/kill')).toHaveLength(0)

    fireEvent.click(screen.getByRole('button', { name: 'Kill now' }))

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(`${killRoute}?instance_id=3`, { method: 'POST' })
    )
    expect(callsTo(fetchMock, '/kill')).toHaveLength(1)

    // Dialog closes; the row reflects the terminal state and the queue that the
    // backend just rejected (reject_queued_queries, worker_thread.py:1533).
    expect(screen.queryByRole('heading', { name: 'Kill Worker' })).not.toBeInTheDocument()
    expect(await screen.findByText('Killed')).toBeInTheDocument()
    expect(screen.getByText('none queued')).toBeInTheDocument()
  })

  it('does not offer kill for a status it cannot vouch for', async () => {
    renderPanel({ workers: [makeWorker({ status: 'completed' })] })

    expect(await screen.findByText('completed')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Kill' })).not.toBeInTheDocument()
  })
})

describe('WorkerManagementPanel — resuming a detached worker', () => {
  it('disables resume when nothing is queued and says why', async () => {
    const resumeRoute = `/api/workspace/${WORKSPACE_ID}/workers/coder/resume`
    const fetchMock = renderPanel({
      workers: [makeWorker({ status: 'detached', queued_query_count: 0, queued_query_ids: [] })],
      routes: { [resumeRoute]: jsonOk({ status: 'resumed' }) },
    })

    const resume = await screen.findByRole('button', { name: '▶ Resume' })
    expect(resume).toBeDisabled()
    expect(resume.title).toContain('Nothing queued')

    fireEvent.click(resume)
    expect(callsTo(fetchMock, '/resume')).toHaveLength(0)
  })

  it('resumes a detached worker that has queued work and flips the row to ready', async () => {
    const resumeRoute = `/api/workspace/${WORKSPACE_ID}/workers/coder/resume`
    const fetchMock = renderPanel({
      workers: [makeWorker({ status: 'detached', queued_query_count: 1, queued_query_ids: ['q-a'] })],
      routes: { [resumeRoute]: jsonOk({ status: 'resumed' }) },
    })

    const resume = await screen.findByRole('button', { name: '▶ Resume' })
    expect(resume).toBeEnabled()
    expect(resume.title).toContain('runs its 1 queued query in order (nothing is discarded)')

    fireEvent.click(resume)

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(`${resumeRoute}?instance_id=3`, { method: 'POST' })
    )
    expect(await screen.findByText('ready')).toBeInTheDocument()
  })
})
