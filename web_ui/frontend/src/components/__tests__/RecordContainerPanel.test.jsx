// @vitest-environment jsdom
// --- RecordContainerPanel.test.jsx ---
// Read-only container-record panel: list/poll, record selection, detail view
// (fields + drift) and the selected record's merged event log. Every
// read-failure path (permission denied / actor missing / record gone / Docker
// unreachable / backend unreachable) gets its own message.

import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor, act } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import RecordContainerPanel from '../RecordContainerPanel'

// Route table: each entry is `{ match, value, status? }`. `match` is either a
// string (substring test) or a predicate; `value` is the JSON body (or a
// function of the url). Anything unmatched resolves to an empty body, which
// the panel reads as "no records".
function stubFetch(routes) {
  const calls = []
  const requests = []
  const fetchMock = vi.fn(async (input, options) => {
    const url = typeof input === 'string' ? input : String(input)
    calls.push(url)
    requests.push({ url, options })
    const route = routes.find((r) =>
      typeof r.match === 'function' ? r.match(url) : url.includes(r.match)
    )
    if (!route) return { ok: true, status: 200, json: async () => ({}) }
    const status = route.status || 200
    const body = typeof route.value === 'function' ? route.value(url) : route.value
    return { ok: status >= 200 && status < 300, status, json: async () => body }
  })
  vi.stubGlobal('fetch', fetchMock)
  return { fetchMock, calls, requests }
}

beforeEach(() => {
  localStorage.clear()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.useRealTimers()
})

const RECORD = {
  id: 'rec-1',
  container_id: 'rec-1',
  name: 'alpha-ctr',
  workspace_id: 'ws-a',
  state: 'running',
  lifecycle_class: 'ephemeral',
  updated_at: '2026-09-10T10:00:00Z',
  drift: [],
}

const LIST_ROUTE = (records) => ({
  match: (url) => url === '/api/container-records',
  value: { records, count: records.length },
})
const EVENTS_ROUTE = (events) => ({
  match: (url) => url.includes('/events'),
  value: { record_id: 'rec-1', events, count: events.length },
})
const DETAIL_ROUTE = (record, status = 200) => ({
  match: (url) => url.includes('/api/container-records/rec-1?'),
  status,
  value: status === 200 ? record : { error: 'not found' },
})

describe('RecordContainerPanel', () => {
  it('shows the empty state when there are no records', async () => {
    stubFetch([LIST_ROUTE([])])
    render(<RecordContainerPanel />)
    expect(
      await screen.findByText('No container records yet. Start a container to create one.')
    ).toBeInTheDocument()
  })

  it('renders a record row with name, state, lifecycle class and drift status', async () => {
    stubFetch([LIST_ROUTE([RECORD])])
    const { container } = render(<RecordContainerPanel />)

    expect(await screen.findByText('alpha-ctr')).toBeInTheDocument()
    expect(screen.getByText('running')).toBeInTheDocument()
    expect(screen.getByText('ephemeral')).toBeInTheDocument()
    expect(screen.getByText('drift: clean')).toBeInTheDocument()
    expect(screen.getByText('2026-09-10T10:00:00Z')).toBeInTheDocument()
    expect(
      container.querySelector('.rcp-drift-indicator.rcp-drift-clean')
    ).toBeInTheDocument()
  })

  it('shows drift findings with a truncated signature and expandable raw JSON', async () => {
    const finding = {
      drift_class: 'image_mismatch',
      event_type: 'drift_check',
      expected: 'img:a',
      actual: 'img:b',
      signature: 'abcdef0123456789',
    }
    const record = { ...RECORD, drift: [finding] }
    stubFetch([LIST_ROUTE([record]), EVENTS_ROUTE([]), DETAIL_ROUTE(record)])
    const { container } = render(<RecordContainerPanel />)

    fireEvent.click(await screen.findByRole('button', { name: /alpha-ctr/ }))

    expect(await screen.findByText('Drift detected — 1 finding.')).toBeInTheDocument()
    expect(screen.getByText('abcdef012345…')).toBeInTheDocument()

    const toggle = screen.getByRole('button', { name: 'Show raw JSON for image_mismatch' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(toggle)

    const raw = container.querySelector('.rcp-drift-raw')
    expect(raw).toBeInTheDocument()
    expect(raw.textContent).toContain('"drift_class": "image_mismatch"')
  })

  it('selecting a record fetches detail (with workspace_id) and the event log', async () => {
    const events = [
      {
        timestamp: '2026-09-10T09:00:00Z',
        event_type: 'created',
        actor: 'operator',
        payload: { name: 'alpha-ctr' },
      },
    ]
    const { calls } = stubFetch([
      LIST_ROUTE([RECORD]),
      EVENTS_ROUTE(events),
      DETAIL_ROUTE({ ...RECORD, owner: 'operator', purpose: 'smoke', docker_id: 'deadbeef' }),
    ])

    render(<RecordContainerPanel />)
    fireEvent.click(await screen.findByRole('button', { name: /alpha-ctr/ }))

    expect(
      await screen.findByRole('heading', { level: 4, name: 'alpha-ctr' })
    ).toBeInTheDocument()
    expect(await screen.findByText('actor: operator')).toBeInTheDocument()

    await waitFor(() =>
      expect(calls).toContain('/api/container-records/rec-1?workspace_id=ws-a')
    )
    expect(calls).toContain('/api/container-records/rec-1/events?workspace_id=ws-a')
  })

  it('shows a "record gone" message when the detail read returns 404', async () => {
    stubFetch([LIST_ROUTE([RECORD]), EVENTS_ROUTE([]), DETAIL_ROUTE(RECORD, 404)])
    render(<RecordContainerPanel />)

    fireEvent.click(await screen.findByRole('button', { name: /alpha-ctr/ }))

    expect(
      await screen.findByText('Record no longer exists — it may have been pruned or replaced.')
    ).toBeInTheDocument()
  })

  it('surfaces a permission-denied list failure', async () => {
    stubFetch([
      {
        match: (url) => url === '/api/container-records',
        status: 403,
        value: { error: 'nope', code: 'permission_denied' },
      },
    ])
    render(<RecordContainerPanel />)

    expect(await screen.findByText('Permission denied: nope')).toBeInTheDocument()
  })

  it('surfaces a missing-actor list failure', async () => {
    stubFetch([
      {
        match: (url) => url === '/api/container-records',
        status: 400,
        value: { error: 'actor required', code: 'actor_required' },
      },
    ])
    render(<RecordContainerPanel />)

    expect(
      await screen.findByText('Actor context missing: actor required')
    ).toBeInTheDocument()
  })

  it('treats a null drift as unknown on both list and detail', async () => {
    const record = { ...RECORD, drift: null }
    stubFetch([LIST_ROUTE([record]), EVENTS_ROUTE([]), DETAIL_ROUTE(record)])
    const { container } = render(<RecordContainerPanel />)

    expect(await screen.findByText('drift: unknown')).toBeInTheDocument()
    expect(
      container.querySelector('.rcp-drift-indicator.rcp-drift-unknown')
    ).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /alpha-ctr/ }))

    expect(
      await screen.findByText(
        'Drift unknown — the live Docker state could not be inspected (Docker unreachable?).'
      )
    ).toBeInTheDocument()
  })

  it('surfaces a Docker-unreachable list failure', async () => {
    stubFetch([
      {
        match: (url) => url === '/api/container-records',
        status: 500,
        value: { error: 'cannot connect to the Docker daemon' },
      },
    ])
    render(<RecordContainerPanel />)

    expect(
      await screen.findByText('Docker unreachable: cannot connect to the Docker daemon')
    ).toBeInTheDocument()
  })

  it('surfaces a backend-unreachable error when fetch rejects', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new Error('boom')
    }))
    render(<RecordContainerPanel />)

    expect(await screen.findByText('Backend unreachable — boom')).toBeInTheDocument()
  })

  it('polls the list every 10 seconds', async () => {
    const spy = vi.spyOn(globalThis, 'setInterval')
    stubFetch([LIST_ROUTE([])])
    render(<RecordContainerPanel />)

    await waitFor(() => expect(spy).toHaveBeenCalled())
    expect(spy).toHaveBeenCalledWith(expect.any(Function), 10000)
  })

  it('refetches the list when Refresh Records is clicked', async () => {
    const { calls } = stubFetch([LIST_ROUTE([])])
    render(<RecordContainerPanel />)

    await waitFor(() =>
      expect(calls.filter((u) => u === '/api/container-records').length).toBe(1)
    )

    fireEvent.click(screen.getByRole('button', { name: 'Refresh Records' }))

    await waitFor(() =>
      expect(calls.filter((u) => u === '/api/container-records').length).toBe(2)
    )
  })

  // ── Pass 2b: write actions ─────────────────────────────────────────────

  const WRITE_KILL = '/api/container-records/rec-1/kill?workspace_id=ws-a'
  const WRITE_RESTART = '/api/container-records/rec-1/restart?workspace_id=ws-a'
  const WRITE_RECREATE = '/api/container-records/rec-1/recreate?workspace_id=ws-a'

  //: Select the (single) record row so the detail + action bar render.
  async function selectRecord(name = /alpha-ctr/) {
    fireEvent.click(await screen.findByRole('button', { name }))
    await screen.findByText('Actor: operator')
  }

  it('renders the write controls with a fixed actor and an enabled reason input', async () => {
    stubFetch([LIST_ROUTE([RECORD]), EVENTS_ROUTE([]), DETAIL_ROUTE(RECORD)])
    render(<RecordContainerPanel />)
    await selectRecord()

    expect(screen.getByText('Actor: operator')).toBeInTheDocument()
    expect(
      screen.getByPlaceholderText('optional note for write actions')
    ).toBeEnabled()
    for (const label of ['Kill', 'Restart', 'Recreate']) {
      expect(screen.getByRole('button', { name: label })).toBeEnabled()
    }
  })

  it('posts kill to the record-keyed route with {actor} and omits an empty reason', async () => {
    const { calls, requests } = stubFetch([
      LIST_ROUTE([RECORD]),
      EVENTS_ROUTE([]),
      DETAIL_ROUTE(RECORD),
    ])
    render(<RecordContainerPanel />)
    await selectRecord()

    fireEvent.click(screen.getByRole('button', { name: 'Kill' }))

    await waitFor(() =>
      expect(calls.filter((u) => u === WRITE_KILL).length).toBe(1)
    )
    const killReq = requests.find((r) => r.url === WRITE_KILL)
    expect(killReq.options.method).toBe('POST')
    expect(killReq.options.headers).toEqual({ 'Content-Type': 'application/json' })
    // `reason` key is absent entirely when the box is empty.
    expect(JSON.parse(killReq.options.body)).toEqual({ actor: 'operator' })

    // Success refetches the list (initial load + post-action refetch).
    await waitFor(() =>
      expect(calls.filter((u) => u === '/api/container-records').length).toBe(2)
    )

    // No name-keyed legacy route is ever called.
    for (const url of calls) {
      expect(url).not.toMatch(/\/(start|stop|delete)(\?|$)/)
    }
  })

  it('trims and sends the reason when one is provided', async () => {
    const { requests } = stubFetch([
      LIST_ROUTE([RECORD]),
      EVENTS_ROUTE([]),
      DETAIL_ROUTE(RECORD),
    ])
    render(<RecordContainerPanel />)
    await selectRecord()

    fireEvent.change(
      screen.getByPlaceholderText('optional note for write actions'),
      { target: { value: '  rotating credentials  ' } }
    )
    fireEvent.click(screen.getByRole('button', { name: 'Kill' }))

    await waitFor(() =>
      expect(requests.some((r) => r.url === WRITE_KILL)).toBe(true)
    )
    const killReq = requests.find((r) => r.url === WRITE_KILL)
    expect(JSON.parse(killReq.options.body)).toEqual({
      actor: 'operator',
      reason: 'rotating credentials',
    })
  })

  it('surfaces a permission-denied action failure', async () => {
    stubFetch([
      LIST_ROUTE([RECORD]),
      EVENTS_ROUTE([]),
      DETAIL_ROUTE(RECORD),
      {
        match: (url) => url.includes('/kill'),
        status: 403,
        value: { error: 'nope', code: 'permission_denied' },
      },
    ])
    render(<RecordContainerPanel />)
    await selectRecord()

    fireEvent.click(screen.getByRole('button', { name: 'Kill' }))

    expect(await screen.findByText('Permission denied: nope')).toBeInTheDocument()
  })

  it('surfaces a missing-actor action failure (401)', async () => {
    stubFetch([
      LIST_ROUTE([RECORD]),
      EVENTS_ROUTE([]),
      DETAIL_ROUTE(RECORD),
      { match: (url) => url.includes('/kill'), status: 401, value: { error: 'actor required' } },
    ])
    render(<RecordContainerPanel />)
    await selectRecord()

    fireEvent.click(screen.getByRole('button', { name: 'Kill' }))

    expect(
      await screen.findByText('Actor context missing: actor required')
    ).toBeInTheDocument()
  })

  it('surfaces a record-gone action failure (404)', async () => {
    stubFetch([
      LIST_ROUTE([RECORD]),
      EVENTS_ROUTE([]),
      DETAIL_ROUTE(RECORD),
      { match: (url) => url.includes('/kill'), status: 404, value: { error: 'record not found' } },
    ])
    render(<RecordContainerPanel />)
    await selectRecord()

    fireEvent.click(screen.getByRole('button', { name: 'Kill' }))

    expect(
      await screen.findByText('Record no longer exists \u2014 it may have been pruned or replaced.')
    ).toBeInTheDocument()
  })

  it('surfaces a Docker-unreachable action failure (503)', async () => {
    stubFetch([
      LIST_ROUTE([RECORD]),
      EVENTS_ROUTE([]),
      DETAIL_ROUTE(RECORD),
      {
        match: (url) => url.includes('/kill'),
        status: 503,
        value: { error: 'cannot connect to the Docker daemon' },
      },
    ])
    render(<RecordContainerPanel />)
    await selectRecord()

    fireEvent.click(screen.getByRole('button', { name: 'Kill' }))

    expect(
      await screen.findByText('Docker unreachable: cannot connect to the Docker daemon')
    ).toBeInTheDocument()
  })

  it('surfaces an action conflict (409)', async () => {
    stubFetch([
      LIST_ROUTE([RECORD]),
      EVENTS_ROUTE([]),
      DETAIL_ROUTE(RECORD),
      { match: (url) => url.includes('/kill'), status: 409, value: { error: 'container not found' } },
    ])
    render(<RecordContainerPanel />)
    await selectRecord()

    fireEvent.click(screen.getByRole('button', { name: 'Kill' }))

    expect(
      await screen.findByText('Action conflict: container not found')
    ).toBeInTheDocument()
  })

  // SYNTHETIC body: the real server's malformed-actor 400 contains the word 'actor'
  // and routes to the actor_missing branch; this guards the classifier, not a real server path.
  it('surfaces a malformed-actor action failure as a bad request (400 without an actor code)', async () => {
    stubFetch([
      LIST_ROUTE([RECORD]),
      EVENTS_ROUTE([]),
      DETAIL_ROUTE(RECORD),
      //: A malformed actor token is rejected with a 400 carrying an `error`
      //: (and no `code`). NB: the message text must avoid the word "actor" —
      //: classifyFailure's /\bactor\b/i guard (RecordContainerPanel.jsx) would
      //: otherwise route it to the actor_missing branch, not the 400 branch.
      {
        match: (url) => url.includes('/kill'),
        status: 400,
        value: { error: 'malformed request' },
      },
    ])
    render(<RecordContainerPanel />)
    await selectRecord()

    fireEvent.click(screen.getByRole('button', { name: 'Kill' }))

    expect(
      await screen.findByText('Bad request: malformed request')
    ).toBeInTheDocument()
  })

  it('surfaces a permission-denied action failure without refetching the record (403)', async () => {
    const { calls } = stubFetch([
      LIST_ROUTE([RECORD]),
      EVENTS_ROUTE([]),
      DETAIL_ROUTE(RECORD),
      {
        match: (url) => url.includes('/kill'),
        status: 403,
        value: { error: 'nope', code: 'permission_denied' },
      },
    ])
    render(<RecordContainerPanel />)
    await selectRecord()

    fireEvent.click(screen.getByRole('button', { name: 'Kill' }))

    expect(await screen.findByText('Permission denied: nope')).toBeInTheDocument()
    //: Probe finding (4b): the non-ok guard returns before the success-only
    //: refetch block, so a failed write (incl. 403) never re-reads the list or
    //: the record detail — the write path does not refetch on permission denial.
    expect(calls.filter((u) => u === '/api/container-records').length).toBe(1)
    expect(
      calls.filter((u) => u === '/api/container-records/rec-1?workspace_id=ws-a').length
    ).toBe(1)
  })

  it('shows an in-flight pending state, then a success notice', async () => {
    let resolveWrite
    const writePromise = new Promise((resolve) => {
      resolveWrite = resolve
    })
    const fetchMock = vi.fn(async (input) => {
      const url = typeof input === 'string' ? input : String(input)
      if (url.includes('/kill')) return writePromise
      if (url.includes('/events'))
        return { ok: true, status: 200, json: async () => ({ events: [], count: 0 }) }
      if (url.includes('/api/container-records/rec-1?'))
        return { ok: true, status: 200, json: async () => RECORD }
      if (url === '/api/container-records')
        return { ok: true, status: 200, json: async () => ({ records: [RECORD], count: 1 }) }
      return { ok: true, status: 200, json: async () => ({}) }
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<RecordContainerPanel />)
    await selectRecord()

    fireEvent.click(screen.getByRole('button', { name: 'Kill' }))

    // Visible in-flight state: button label swaps + a status line appears.
    expect(
      await screen.findByText('Killing\u2026 (waiting for the backend\u2026)')
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Killing\u2026' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Restart' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Recreate' })).toBeDisabled()

    await act(async () => {
      resolveWrite({ ok: true, status: 200, json: async () => RECORD })
    })

    expect(await screen.findByText('Kill applied to rec-1.')).toBeInTheDocument()
  })

  it('disables Kill for an exited record but keeps Restart and Recreate enabled', async () => {
    const record = { ...RECORD, state: 'exited' }
    stubFetch([LIST_ROUTE([record]), EVENTS_ROUTE([]), DETAIL_ROUTE(record)])
    render(<RecordContainerPanel />)
    await selectRecord()

    expect(screen.getByRole('button', { name: 'Kill' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Restart' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Recreate' })).toBeEnabled()
  })

  it('disables every action when the record has no name', async () => {
    const record = { ...RECORD, name: '', state: 'creating' }
    stubFetch([LIST_ROUTE([record]), EVENTS_ROUTE([]), DETAIL_ROUTE(record)])
    render(<RecordContainerPanel />)
    await selectRecord(/rec-1/)

    for (const label of ['Kill', 'Restart', 'Recreate']) {
      expect(screen.getByRole('button', { name: label })).toBeDisabled()
    }
  })

  it('posts restart and recreate to their record-keyed routes', async () => {
    const { requests } = stubFetch([
      LIST_ROUTE([RECORD]),
      EVENTS_ROUTE([]),
      DETAIL_ROUTE(RECORD),
    ])
    render(<RecordContainerPanel />)
    await selectRecord()

    fireEvent.click(screen.getByRole('button', { name: 'Restart' }))
    expect(await screen.findByText('Restart applied to rec-1.')).toBeInTheDocument()
    expect(requests.some((r) => r.url === WRITE_RESTART)).toBe(true)
    expect(
      requests.find((r) => r.url === WRITE_RESTART).options.method
    ).toBe('POST')

    fireEvent.click(screen.getByRole('button', { name: 'Recreate' }))
    expect(await screen.findByText('Recreate applied to rec-1.')).toBeInTheDocument()
    expect(requests.some((r) => r.url === WRITE_RECREATE)).toBe(true)
    expect(
      requests.find((r) => r.url === WRITE_RECREATE).options.method
    ).toBe('POST')
  })

  // ── Pass 3a: read-path coverage gaps ──────────────────────────────────────────

  it('shows the list loading interim before the records resolve', async () => {
    let resolveList
    const listPromise = new Promise((resolve) => {
      resolveList = resolve
    })
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input) => {
        const url = typeof input === 'string' ? input : String(input)
        if (url === '/api/container-records') return listPromise
        return { ok: true, status: 200, json: async () => ({}) }
      })
    )

    render(<RecordContainerPanel />)

    // Synchronous first paint: the effect has fired but the list is unresolved.
    expect(screen.getByText('Loading records…')).toBeInTheDocument()

    await act(async () => {
      resolveList({ ok: true, status: 200, json: async () => ({ records: [], count: 0 }) })
    })
    expect(screen.queryByText('Loading records…')).not.toBeInTheDocument()
  })

  it('shows the detail loading interim while the detail request is in flight', async () => {
    let resolveDetail
    const detailPromise = new Promise((resolve) => {
      resolveDetail = resolve
    })
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input) => {
        const url = typeof input === 'string' ? input : String(input)
        if (url.includes('/events'))
          return { ok: true, status: 200, json: async () => ({ events: [], count: 0 }) }
        if (url.includes('/api/container-records/rec-1?')) return detailPromise
        if (url === '/api/container-records')
          return { ok: true, status: 200, json: async () => ({ records: [RECORD], count: 1 }) }
        return { ok: true, status: 200, json: async () => ({}) }
      })
    )

    render(<RecordContainerPanel />)
    fireEvent.click(await screen.findByRole('button', { name: /alpha-ctr/ }))

    expect(await screen.findByText('Loading record…')).toBeInTheDocument()

    await act(async () => {
      resolveDetail({ ok: true, status: 200, json: async () => RECORD })
    })
    // Detail resolved: the pending interim is replaced by the loaded detail.
    expect(await screen.findByText('No drift detected.')).toBeInTheDocument()
    expect(screen.queryByText('Loading record…')).not.toBeInTheDocument()
  })

  it('renders the clean-drift message on the detail when drift is an empty list', async () => {
    stubFetch([LIST_ROUTE([RECORD]), EVENTS_ROUTE([]), DETAIL_ROUTE({ ...RECORD, drift: [] })])
    render(<RecordContainerPanel />)
    await selectRecord()

    expect(await screen.findByText('No drift detected.')).toBeInTheDocument()
  })

  it('renders the empty event-log message when there are no events', async () => {
    stubFetch([LIST_ROUTE([RECORD]), EVENTS_ROUTE([]), DETAIL_ROUTE(RECORD)])
    render(<RecordContainerPanel />)
    await selectRecord()

    expect(await screen.findByText('No events recorded for this record.')).toBeInTheDocument()
  })

  it('renders the empty intent-snapshot message when none is recorded', async () => {
    stubFetch([LIST_ROUTE([RECORD]), EVENTS_ROUTE([]), DETAIL_ROUTE(RECORD)])
    render(<RecordContainerPanel />)
    await selectRecord()

    expect(await screen.findByText('No intent snapshot recorded.')).toBeInTheDocument()
  })

  it('renders the notes field when the detail carries notes', async () => {
    stubFetch([
      LIST_ROUTE([RECORD]),
      EVENTS_ROUTE([]),
      DETAIL_ROUTE({ ...RECORD, notes: 'handle with care' }),
    ])
    render(<RecordContainerPanel />)
    await selectRecord()

    expect(await screen.findByText('Notes: handle with care')).toBeInTheDocument()
  })

  it('surfaces a generic bad-request list failure (400 without an actor code)', async () => {
    stubFetch([
      {
        match: (url) => url === '/api/container-records',
        status: 400,
        value: { error: 'missing workspace_id' },
      },
    ])
    render(<RecordContainerPanel />)

    expect(await screen.findByText('Bad request: missing workspace_id')).toBeInTheDocument()
  })

  it('surfaces an events-fetch failure alongside the detail', async () => {
    stubFetch([
      LIST_ROUTE([RECORD]),
      { match: (url) => url.includes('/events'), status: 500, value: { error: 'events exploded' } },
      DETAIL_ROUTE(RECORD),
    ])
    render(<RecordContainerPanel />)
    await selectRecord()

    expect(await screen.findByText('events exploded')).toBeInTheDocument()
    expect(screen.queryByText('No events recorded for this record.')).not.toBeInTheDocument()
  })

  it('closes the detail view when Close detail is clicked', async () => {
    stubFetch([LIST_ROUTE([RECORD]), EVENTS_ROUTE([]), DETAIL_ROUTE(RECORD)])
    render(<RecordContainerPanel />)
    await selectRecord()

    expect(
      screen.getByRole('heading', { level: 4, name: 'alpha-ctr' })
    ).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Close detail' }))

    expect(
      screen.queryByRole('heading', { level: 4, name: 'alpha-ctr' })
    ).not.toBeInTheDocument()
    expect(
      screen.getByText('Select a record to see its details and event log.')
    ).toBeInTheDocument()
  })
})
