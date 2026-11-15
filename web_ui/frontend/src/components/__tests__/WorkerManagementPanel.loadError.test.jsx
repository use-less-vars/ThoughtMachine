// @vitest-environment jsdom
/*
 * WorkerManagementPanel.loadError.test.jsx — the panel must be HONEST about a
 * failed poll.
 *
 * Before this spec the fetch handler ended in `.catch(() => setWorkers([]))`,
 * so every failure — HTTP 500, a dropped connection, an exception thrown
 * mid-map — rendered as "No workers configured…", i.e. as the empty state.
 * A real backend lock stall therefore looked like "there are no workers".
 *
 * Contract under test (NOT more than this):
 *   (i)  a rejected FIRST fetch yields an explicit error state naming the
 *        failure, and never the empty-state copy
 *   (ii) a rejected REFRESH keeps the previously loaded rows on screen and
 *        reports the failure as a non-destructive notice
 *
 * Mocking mirrors WorkerManagementPanelActiveInstances: a fetch stub that
 * matches registered route keys by URL substring (longest key wins), installed
 * via vi.stubGlobal('fetch') and torn down in afterEach.
 *
 * Test (ii) drives the 3s poll with fake timers: the interval is real (3000 ms)
 * and waiting it out with real timers would sit on the default per-test
 * timeout.
 */
import React from 'react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup, act } from '@testing-library/react'
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
const EMPTY_COPY = 'No workers configured. Create one now, or start from a template.'

const BUSY_WORKER = {
  worker_name: 'coder',
  instance_id: 1,
  instance_label: 'coder',
  status: 'busy',
  elapsed: 130,
  tools: ['bash', 'file'],
  time_since_last_query: 12,
  container_id: 'abc123',
}

function renderPanel() {
  render(
    <WorkerManagementPanel
      workspaceId={WORKSPACE_ID}
      sessionId={SESSION_ID}
      onSelectWorker={vi.fn()}
      selectedWorker={null}
      isActive
    />
  )
}

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('WorkerManagementPanel — failed polls are visible, never silent', () => {
  it('(i) a rejected fetch renders an error state quoting the failure, not the empty list', async () => {
    stubFetchByUrl({
      [ACTIVE_URL]: () => Promise.reject(new Error('HTTP 500')),
      [TEMPLATES_URL]: jsonOk([]),
    })
    renderPanel()

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('Could not load workers')
    expect(alert.textContent).toContain('HTTP 500')
    // The lie this replaces: a failure must never render as "no workers".
    expect(screen.queryByText(EMPTY_COPY)).not.toBeInTheDocument()
  })

  it('(ii) a rejected refresh keeps the previously loaded workers and adds a non-destructive notice', async () => {
    vi.useFakeTimers()
    let failPolls = false
    stubFetchByUrl({
      [ACTIVE_URL]: () =>
        failPolls ? Promise.reject(new Error('backend lock stall')) : jsonOk([BUSY_WORKER]),
      [TEMPLATES_URL]: jsonOk([]),
    })
    renderPanel()

    // First poll succeeds: the row is on screen.
    await act(async () => {})
    expect(screen.getByText('coder')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()

    // Next poll (the 3s interval) fails: the loaded row must survive it.
    failPolls = true
    await act(async () => {
      vi.advanceTimersByTime(3000)
    })
    await act(async () => {})

    expect(screen.getByText('coder')).toBeInTheDocument()
    const alert = screen.getByRole('alert')
    expect(alert.textContent).toContain('Refresh failed')
    expect(alert.textContent).toContain('backend lock stall')
    expect(screen.queryByText(EMPTY_COPY)).not.toBeInTheDocument()
  })
})
