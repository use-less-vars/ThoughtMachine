// @vitest-environment jsdom
// --- RecordContainerPanel.hardening.test.jsx ---
// Additive suite (B4-UI): the record panel must surface a container's
// HARDENING conformance as a THREE-STATE verdict (conformant / drifted /
// unknown) plus a FRESHNESS comparison between the record's recorded hardening
// posture and the LIVE verdict.
//
// The verdict is consumed read-only from the existing container-status route:
//   GET /api/workspace/{workspace_id}/containers/{name}/status
//     -> { ..., "hardening": {"status": "conformant"|"drifted"|"unverified",
//                             "failed": [<axis>, ...]} }
// `unverified` (and every read failure / absent verdict) MUST render as
// "unknown" -- NEVER "conformant" (fail-closed).
//
// This file does NOT modify RecordContainerPanel.test.jsx.

import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor, within } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import RecordContainerPanel from '../RecordContainerPanel'

// Same route-table stub pattern as RecordContainerPanel.test.jsx: unmatched
// URLs resolve to an empty JSON body.
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

//: The record's own container name + workspace scope build the live status URL.
const STATUS_URL = '/api/workspace/ws-a/containers/alpha-ctr/status'

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
const STATUS_ROUTE = (hardening, status = 200) => ({
  match: (url) => url.includes('/containers/alpha-ctr/status'),
  status,
  value: status === 200 ? { hardening } : { error: 'status unavailable' },
})

//: Select the single record and wait for the detail heading to render.
async function openRecord() {
  fireEvent.click(await screen.findByRole('button', { name: /alpha-ctr/ }))
  await screen.findByRole('heading', { level: 4, name: 'alpha-ctr' })
}

//: Wait until a region exists, then return it.
async function region(selector) {
  await waitFor(() => expect(document.querySelector(selector)).not.toBeNull())
  return document.querySelector(selector)
}

describe('RecordContainerPanel \u2014 hardening verdict + freshness', () => {
  it('reads the live hardening verdict from the container-status route', async () => {
    const { calls } = stubFetch([
      LIST_ROUTE([RECORD]),
      EVENTS_ROUTE([]),
      DETAIL_ROUTE(RECORD),
      STATUS_ROUTE({ status: 'conformant', failed: [] }),
    ])
    render(<RecordContainerPanel />)
    await openRecord()

    await waitFor(() => expect(calls).toContain(STATUS_URL))
  })

  it('renders the verdict as conformant when every hardening axis is satisfied', async () => {
    stubFetch([
      LIST_ROUTE([RECORD]),
      EVENTS_ROUTE([]),
      DETAIL_ROUTE(RECORD),
      STATUS_ROUTE({ status: 'conformant', failed: [] }),
    ])
    render(<RecordContainerPanel />)
    await openRecord()

    const el = await region('.rcp-hardening')
    expect(el.textContent).toMatch(/conformant/i)
    expect(el.textContent).not.toMatch(/unknown/i)
  })

  it('renders a drifted verdict PER FAILING AXIS (not one blob)', async () => {
    stubFetch([
      LIST_ROUTE([RECORD]),
      EVENTS_ROUTE([]),
      DETAIL_ROUTE(RECORD),
      STATUS_ROUTE({ status: 'drifted', failed: ['cap_drop', 'security_opt'] }),
    ])
    render(<RecordContainerPanel />)
    await openRecord()

    const el = await region('.rcp-hardening')
    expect(el.textContent).toMatch(/drifted/i)
    //: One list item per failing axis -- a single blob would yield no items.
    const axes = within(el)
      .getAllByRole('listitem')
      .map((li) => li.textContent.trim())
    expect(axes).toEqual(['cap_drop', 'security_opt'])
  })

  it('renders unverified as unknown \u2014 NEVER conformant (fail-closed)', async () => {
    stubFetch([
      LIST_ROUTE([RECORD]),
      EVENTS_ROUTE([]),
      DETAIL_ROUTE(RECORD),
      STATUS_ROUTE({ status: 'unverified', failed: [] }),
    ])
    render(<RecordContainerPanel />)
    await openRecord()

    const el = await region('.rcp-hardening')
    expect(el.textContent).toMatch(/unknown/i)
    expect(el.textContent).not.toMatch(/conformant/i)
  })

  it('renders unknown (never conformant) when the status read fails', async () => {
    stubFetch([
      LIST_ROUTE([RECORD]),
      EVENTS_ROUTE([]),
      DETAIL_ROUTE(RECORD),
      STATUS_ROUTE({ status: 'conformant', failed: [] }, 500),
    ])
    render(<RecordContainerPanel />)
    await openRecord()

    const el = await region('.rcp-hardening')
    expect(el.textContent).toMatch(/unknown/i)
    expect(el.textContent).not.toMatch(/conformant/i)
  })

  it('surfaces a freshness DIFFERENCE when the live verdict differs from the recorded posture', async () => {
    const record = { ...RECORD, intent_snapshot: { hardening: { cap_drop: ['ALL'] } } }
    stubFetch([
      LIST_ROUTE([record]),
      EVENTS_ROUTE([]),
      DETAIL_ROUTE(record),
      STATUS_ROUTE({ status: 'drifted', failed: ['cap_drop'] }),
    ])
    render(<RecordContainerPanel />)
    await openRecord()

    const el = await region('.rcp-freshness')
    expect(el.textContent).toMatch(/differs from the recorded posture/i)
  })

  it('reports freshness as matching when the live verdict agrees with the recorded posture', async () => {
    const record = { ...RECORD, intent_snapshot: { hardening: { cap_drop: ['ALL'] } } }
    stubFetch([
      LIST_ROUTE([record]),
      EVENTS_ROUTE([]),
      DETAIL_ROUTE(record),
      STATUS_ROUTE({ status: 'conformant', failed: [] }),
    ])
    render(<RecordContainerPanel />)
    await openRecord()

    const el = await region('.rcp-freshness')
    expect(el.textContent).toMatch(/matches the recorded posture/i)
    expect(el.textContent).not.toMatch(/differs/i)
  })

  it('reports freshness as cannot-compare when the live verdict is unverified', async () => {
    const record = { ...RECORD, intent_snapshot: { hardening: { cap_drop: ['ALL'] } } }
    stubFetch([
      LIST_ROUTE([record]),
      EVENTS_ROUTE([]),
      DETAIL_ROUTE(record),
      STATUS_ROUTE({ status: 'unverified', failed: [] }),
    ])
    render(<RecordContainerPanel />)
    await openRecord()

    const el = await region('.rcp-freshness')
    expect(el.textContent).toMatch(/cannot compare/i)
  })

  it('renders drift === null (un-inspectable) distinctly from drift === [] (no drift)', async () => {
    const record = { ...RECORD, drift: null }
    stubFetch([LIST_ROUTE([record]), EVENTS_ROUTE([]), DETAIL_ROUTE(record)])
    render(<RecordContainerPanel />)
    await openRecord()

    expect(
      await screen.findByText(
        'Drift unknown \u2014 the live Docker state could not be inspected (Docker unreachable?).'
      )
    ).toBeInTheDocument()
    expect(screen.queryByText('No drift detected.')).not.toBeInTheDocument()
  })

  it('renders drift === [] as the clean (no drift) state', async () => {
    const record = { ...RECORD, drift: [] }
    stubFetch([LIST_ROUTE([record]), EVENTS_ROUTE([]), DETAIL_ROUTE(record)])
    render(<RecordContainerPanel />)
    await openRecord()

    expect(await screen.findByText('No drift detected.')).toBeInTheDocument()
    expect(
      screen.queryByText(
        'Drift unknown \u2014 the live Docker state could not be inspected (Docker unreachable?).'
      )
    ).not.toBeInTheDocument()
  })
})
