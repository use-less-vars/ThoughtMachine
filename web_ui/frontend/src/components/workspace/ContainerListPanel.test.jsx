// @vitest-environment jsdom
// --- ContainerListPanel.test.jsx ---
// RED tests for feature C.1 (session-panel container view) -- three-class split.
// Component under test: ./ContainerListPanel.jsx, default export
// `function ContainerListPanel({ workspaceId })`.
//
// Contract pinned by these tests:
//   * on mount inline-fetches GET /api/workspace/${workspaceId}/containers
//   * payload shape: { containers: [entries] } -- a FLAT list (PC3); the legacy
//     `session`/`workspace` keys are GONE. Entries are grouped by `kind`.
//   * entry keys: id, name, kind, state, intent_snapshot, permissions,
//     permission_drift, shared, and (when the backend sends it) note
//   * `kind` is "ephemeral" | "persistent" | "resource" (PC5); the old
//     "runtime" kind is RETIRED.
//   * three group sections: container-group-ephemeral / -persistent / -resource
//   * rows: container-row-{id}; state chip: container-state-{id}
//   * permission badge: permission-badge-{id}
//   * drift indicator: container-drift-{id} (data-drift red|neutral, ABSENT when
//     silent). Precedence: unavailable_reason (red) > non-empty permission_drift
//     (neutral, count) > permission_drift === null (neutral, "not verified") >
//     silent. `driftDecision(entry)` is the pure exported decision function.
//   * shared chip (persistent/resource only): container-shared-{id}
//   * controls: container-action-{id}-stop|start|restart|remove|resources
//   * size-edit input: container-resources-mem-{id}
//   * confirm modal: confirm-dialog / confirm-message / confirm-ok / confirm-cancel
//
// The fetch boundary IS the boundary: we stub global fetch and never mock any
// component internal or helper.

import React from 'react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor, within, act } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import ContainerListPanel, { driftDecision } from './ContainerListPanel'

const WORKSPACE_ID = 'ws-1'

const SHARED_TEXT = 'Stopping affects other sessions in this workspace.'

function ephemeralEntry(overrides = {}) {
  return {
    id: 'eph-1',
    name: 'eph-1',
    kind: 'ephemeral',
    state: 'running',
    intent_snapshot: {},
    permissions: { network: true, filesystem: 'rw', mem_limit: '512m' },
    permission_drift: null,
    shared: false,
    ...overrides,
  }
}

function persistentEntry(overrides = {}) {
  return {
    id: 'pers-1',
    name: 'pers-1',
    kind: 'persistent',
    state: 'running',
    intent_snapshot: {},
    permissions: { network: false, filesystem: 'ro', mem_limit: '1g' },
    permission_drift: null,
    shared: true,
    ...overrides,
  }
}

function resourceEntry(overrides = {}) {
  return {
    id: 'res-1',
    name: 'res-1',
    kind: 'resource',
    state: 'running',
    intent_snapshot: {},
    permissions: { network: false, filesystem: 'ro', mem_limit: '1g' },
    permission_drift: null,
    shared: true,
    ...overrides,
  }
}

function payload({ ephemeral = [], persistent = [], resource = [] } = {}) {
  return { containers: [...ephemeral, ...persistent, ...resource] }
}

function stubFetch(body) {
  const fetchMock = vi.fn(async () => ({ ok: true, status: 200, json: async () => body }))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

// Locate the fetch call whose URL contains `needle` and return url + parsed body.
function actionCall(fetchMock, needle) {
  const call = fetchMock.mock.calls.find(([u]) => String(u).includes(needle))
  expect(call).toBeTruthy()
  const [url, init] = call
  const opts = init || {}
  const body = opts.body ? JSON.parse(opts.body) : undefined
  return { url: String(url), method: opts.method, body }
}

// All fetch calls whose URL contains `needle`, with url/method/headers/body.
function actionCalls(fetchMock, needle) {
  return fetchMock.mock.calls
    .filter(([u]) => String(u).includes(needle))
    .map(([u, init]) => {
      const opts = init || {}
      return {
        url: String(u),
        method: opts.method,
        headers: opts.headers || {},
        body: opts.body ? JSON.parse(opts.body) : undefined,
      }
    })
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('ContainerListPanel (C.1 session-panel container view)', () => {
  // ── T5 ──────────────────────────────────────────────────────────────────
  // T5: all three groups render with the EXACT heading + empty-state copy.
  it('T5 renders the three groups with exact headings and empty-state copy when containers is empty', async () => {
    stubFetch(payload())
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    expect(await screen.findByTestId('container-group-ephemeral')).toBeInTheDocument()
    expect(screen.getByTestId('container-group-persistent')).toBeInTheDocument()
    expect(screen.getByTestId('container-group-resource')).toBeInTheDocument()

    // exact headings (PC6)
    expect(screen.getByText('Ephemeral containers')).toBeInTheDocument()
    expect(screen.getByText('Persistent containers')).toBeInTheDocument()
    expect(screen.getByText('Resource containers')).toBeInTheDocument()

    // exact empty-state copy (PC6)
    expect(screen.getByText('No ephemeral containers.')).toBeInTheDocument()
    expect(screen.getByText('No persistent containers.')).toBeInTheDocument()
    expect(screen.getByText('No resource containers.')).toBeInTheDocument()
  })

  // T5: populated group renders its row and omits that group's empty copy only.
  it('T5 renders a populated ephemeral group row and omits only that group empty-state text', async () => {
    stubFetch(payload({ ephemeral: [ephemeralEntry({ id: 'eph-a' })] }))
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    expect(await screen.findByTestId('container-row-eph-a')).toBeInTheDocument()
    // populated ephemeral group -> its empty copy must be gone
    expect(screen.queryByText('No ephemeral containers.')).toBeNull()
    // the (empty) persistent + resource groups still show their own copy
    expect(screen.getByText('No persistent containers.')).toBeInTheDocument()
    expect(screen.getByText('No resource containers.')).toBeInTheDocument()
  })

  // ── T7 ──────────────────────────────────────────────────────────────────
  // T7: persistent and resource rows land in their OWN group (distinguishable).
  it('T7 persistent and resource rows render in their own distinct groups', async () => {
    stubFetch(
      payload({
        persistent: [persistentEntry({ id: 'pers-1' })],
        resource: [resourceEntry({ id: 'res-1' })],
      })
    )
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    const persGroup = await screen.findByTestId('container-group-persistent')
    const resGroup = screen.getByTestId('container-group-resource')

    expect(within(persGroup).getByTestId('container-row-pers-1')).toBeInTheDocument()
    expect(within(resGroup).getByTestId('container-row-res-1')).toBeInTheDocument()
    // each row belongs to exactly one group
    expect(within(persGroup).queryByTestId('container-row-res-1')).toBeNull()
    expect(within(resGroup).queryByTestId('container-row-pers-1')).toBeNull()
  })

  // ── T9 ──────────────────────────────────────────────────────────────────
  // T9: a row control only dispatches AFTER the blocking confirm is accepted,
  // and it dispatches the correct method/url/JSON body.
  it('T9 stop on an ephemeral row POSTs action=stop after confirm-ok', async () => {
    const fetchMock = stubFetch(payload({ ephemeral: [ephemeralEntry({ id: 'eph-stop', state: 'running' })] }))
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    fireEvent.click(await screen.findByTestId('container-action-eph-stop-stop'))
    expect(await screen.findByTestId('confirm-dialog')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('confirm-ok'))

    await waitFor(() => {
      const { url, method, body } = actionCall(fetchMock, '/containers/eph-stop/action')
      expect(method).toBe('POST')
      expect(url).toBe(`/api/workspace/${WORKSPACE_ID}/containers/eph-stop/action`)
      expect(body).toEqual({ action: 'stop', actor: 'operator' })
    })
  })

  it('T9 remove on an ephemeral row POSTs action=remove after confirm-ok', async () => {
    const fetchMock = stubFetch(payload({ ephemeral: [ephemeralEntry({ id: 'eph-rm', state: 'running' })] }))
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    fireEvent.click(await screen.findByTestId('container-action-eph-rm-remove'))
    fireEvent.click(await screen.findByTestId('confirm-ok'))

    await waitFor(() => {
      const { url, method, body } = actionCall(fetchMock, '/containers/eph-rm/action')
      expect(method).toBe('POST')
      expect(url).toBe(`/api/workspace/${WORKSPACE_ID}/containers/eph-rm/action`)
      expect(body).toEqual({ action: 'remove', actor: 'operator' })
    })
  })

  it('T9 size-edit on a row PATCHes resources with the typed mem_limit after confirm-ok', async () => {
    const fetchMock = stubFetch(payload({ ephemeral: [ephemeralEntry({ id: 'eph-size', state: 'running' })] }))
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    const memInput = await screen.findByTestId('container-resources-mem-eph-size')
    fireEvent.change(memInput, { target: { value: '1g' } })

    fireEvent.click(screen.getByTestId('container-action-eph-size-resources'))
    fireEvent.click(await screen.findByTestId('confirm-ok'))

    await waitFor(() => {
      const { url, method, body } = actionCall(fetchMock, '/containers/eph-size/resources')
      expect(method).toBe('PATCH')
      expect(url).toBe(`/api/workspace/${WORKSPACE_ID}/containers/eph-size/resources`)
      expect(body.mem_limit).toBe('1g')
    })
  })

  // ── T10 ─────────────────────────────────────────────────────────────────
  // T10: drift indicator is 3-state (red | neutral | absent).
  it('T10 non-empty permission_drift renders a neutral indicator naming the drift count', async () => {
    stubFetch(
      payload({
        ephemeral: [
          ephemeralEntry({
            id: 'eph-drift',
            permission_drift: [{ code: 'drift.permission', message: 'shape differs' }],
          }),
        ],
      })
    )
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    const drift = await screen.findByTestId('container-drift-eph-drift')
    expect(drift).toHaveAttribute('data-drift', 'neutral')
    expect(drift).toHaveTextContent(/has drifted from its recorded intent/i)
    expect(drift).toHaveTextContent(/\(1 item\(s\)\)/)
  })

  it('T10 null permission_drift with permissions present renders a neutral not-verified indicator', async () => {
    stubFetch(
      payload({
        ephemeral: [
          ephemeralEntry({
            id: 'eph-silent',
            permissions: { network: true, filesystem: 'rw', mem_limit: '512m' },
            permission_drift: null,
          }),
        ],
      })
    )
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    const drift = await screen.findByTestId('container-drift-eph-silent')
    expect(drift).toHaveAttribute('data-drift', 'neutral')
    expect(drift).toHaveTextContent(/Drift not verified for this container/i)
  })

  it('T10 empty permission_drift array renders NO drift indicator (silent)', async () => {
    stubFetch(
      payload({
        ephemeral: [
          ephemeralEntry({
            id: 'eph-clean',
            permissions: { network: true, filesystem: 'rw', mem_limit: '512m' },
            permission_drift: [],
          }),
        ],
      })
    )
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    await screen.findByTestId('container-row-eph-clean')
    expect(screen.queryByTestId('container-drift-eph-clean')).toBeNull()
  })

  it('T10 unavailable_reason renders a RED drift indicator (outranks permission_drift)', async () => {
    stubFetch(
      payload({
        ephemeral: [
          ephemeralEntry({
            id: 'eph-red',
            permission_drift: [{ code: 'drift.permission', message: 'shape differs' }],
            unavailable_reason: 'container grants network but this session does not.',
          }),
        ],
      })
    )
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    const drift = await screen.findByTestId('container-drift-eph-red')
    expect(drift).toHaveAttribute('data-drift', 'red')
    expect(drift.className).toContain('container-drift--red')
    expect(drift).toHaveTextContent(/container grants network but this session does not\./i)
    expect(drift).toHaveTextContent(/Rebuild to apply the current permissions/i)
  })

  it('T10 null permissions renders a neutral indicator whose tooltip says drift is not wired', async () => {
    stubFetch(
      payload({
        ephemeral: [ephemeralEntry({ id: 'eph-neutral', permissions: null, permission_drift: null })],
      })
    )
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    const drift = await screen.findByTestId('container-drift-eph-neutral')
    expect(drift).toHaveAttribute('data-drift', 'neutral')
    const tooltipText = drift.getAttribute('title') || drift.textContent || ''
    expect(tooltipText).toMatch(/Drift check not yet wired/)
  })

  // ── T11 ─────────────────────────────────────────────────────────────────
  // T11: persistent/resource rows carry a shared chip + shared confirm copy;
  // ephemeral rows do not. `remove` is offered on ephemeral AND persistent
  // rows (Round B); it is never offered on a resource row.
  it('T11 persistent row shows the shared chip and its confirm modal contains the shared copy', async () => {
    stubFetch(payload({ persistent: [persistentEntry({ id: 'pers-shared', state: 'running' })] }))
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    const chip = await screen.findByTestId('container-shared-pers-shared')
    expect(chip).toHaveAttribute('title', SHARED_TEXT)

    fireEvent.click(screen.getByTestId('container-action-pers-shared-stop'))
    const dialog = await screen.findByTestId('confirm-dialog')
    expect(dialog).toHaveTextContent(SHARED_TEXT)
  })

  it('T11 resource row does NOT render a remove control (resource removal is operator-only)', async () => {
    stubFetch(payload({ resource: [resourceEntry({ id: 'res-norm', state: 'running' })] }))
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    await screen.findByTestId('container-row-res-norm')
    expect(screen.queryByTestId('container-action-res-norm-remove')).toBeNull()
    // ... but it still carries the shared chip (resource is shared-class).
    expect(screen.getByTestId('container-shared-res-norm')).toBeInTheDocument()
  })

  it('T11 ephemeral row has no shared chip and its confirm modal lacks the shared copy', async () => {
    stubFetch(payload({ ephemeral: [ephemeralEntry({ id: 'eph-plain', state: 'running' })] }))
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    await screen.findByTestId('container-row-eph-plain')
    expect(screen.queryByTestId('container-shared-eph-plain')).toBeNull()

    fireEvent.click(screen.getByTestId('container-action-eph-plain-stop'))
    const dialog = await screen.findByTestId('confirm-dialog')
    expect(dialog).not.toHaveTextContent(SHARED_TEXT)
  })

  // ── T11b (Round B) ──────────────────────────────────────────────────────
  // T11b: the shared confirm line is VERB-CORRECT. The stop copy stays
  // byte-identical (C.1 pin above); start/restart derive their own gerund and
  // must never claim "Stopping".
  it('T11b shared confirm copy is verb-correct: stop verbatim, start/restart never say "Stopping"', async () => {
    stubFetch(
      payload({
        persistent: [
          persistentEntry({ id: 'pers-stop', state: 'running' }),
          persistentEntry({ id: 'pers-start', state: 'exited' }),
        ],
      })
    )
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    // stop on a shared container -> the verbatim pinned stop line
    fireEvent.click(await screen.findByTestId('container-action-pers-stop-stop'))
    expect(await screen.findByTestId('confirm-dialog')).toHaveTextContent(SHARED_TEXT)
    fireEvent.click(screen.getByTestId('confirm-cancel'))

    // start on an exited shared container -> a "Starting" line, NOT "Stopping"
    fireEvent.click(await screen.findByTestId('container-action-pers-start-start'))
    const startMsg = screen.getByTestId('confirm-message').textContent || ''
    expect(startMsg).not.toMatch(/Stopping/)
    expect(startMsg).toMatch(/Starting/)
    fireEvent.click(screen.getByTestId('confirm-cancel'))

    // restart on a running shared container -> a "Restarting" line, NOT "Stopping"
    fireEvent.click(await screen.findByTestId('container-action-pers-stop-restart'))
    const restartMsg = screen.getByTestId('confirm-message').textContent || ''
    expect(restartMsg).not.toMatch(/Stopping/)
    expect(restartMsg).toMatch(/Restarting/)
  })

  // ── T13 ─────────────────────────────────────────────────────────────────
  // T13: oom state chip is text "oom" and distinct (class) from a running chip.
  it('T13 oom chip reads "oom" with class container-state-chip--oom, distinct from a running chip', async () => {
    stubFetch(
      payload({
        ephemeral: [
          ephemeralEntry({ id: 'eph-oom', state: 'oom' }),
          ephemeralEntry({ id: 'eph-run', state: 'running' }),
        ],
      })
    )
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    const oomChip = await screen.findByTestId('container-state-eph-oom')
    expect(oomChip).toHaveTextContent(/^oom$/)
    expect(oomChip).toHaveClass('container-state-chip--oom')

    const runChip = screen.getByTestId('container-state-eph-run')
    expect(runChip).toHaveTextContent(/^running$/)
    expect(runChip).not.toHaveClass('container-state-chip--oom')
  })

  // ── T14 ─────────────────────────────────────────────────────────────────
  // Live-view hides dead records (no live container) by default; the "Show
  // archived" toggle reveals them. Display-only: nothing is swept or deleted.
  it('T14 hides dead records by default and offers a Show archived toggle', async () => {
    stubFetch(
      payload({
        ephemeral: [
          ephemeralEntry({ id: 'eph-live' }),
          ephemeralEntry({ id: 'eph-dead', live: false }),
        ],
      })
    )
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    expect(await screen.findByTestId('container-row-eph-live')).toBeInTheDocument()
    // the dead record must NOT be visible while the toggle is off
    expect(screen.queryByTestId('container-row-eph-dead')).toBeNull()
    // the toggle itself is present
    expect(screen.getByTestId('container-show-archived')).toBeInTheDocument()
  })

  it('T14 Show archived reveals the dead record, which renders as today (with its no-live-container badge)', async () => {
    stubFetch(
      payload({
        ephemeral: [
          ephemeralEntry({ id: 'eph-live' }),
          ephemeralEntry({ id: 'eph-dead', live: false }),
        ],
      })
    )
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    // dead row hidden until the toggle is flipped
    await screen.findByTestId('container-row-eph-live')
    expect(screen.queryByTestId('container-row-eph-dead')).toBeNull()

    fireEvent.click(screen.getByTestId('container-show-archived'))

    expect(await screen.findByTestId('container-row-eph-dead')).toBeInTheDocument()
    // revealed dead row keeps its existing "no live container" badge
    expect(screen.getByTestId('container-missing-eph-dead')).toBeInTheDocument()
  })

  // ── Fix A: the permission badge describes the RUNNING container ─────────────
  // When the entry carries `live_envelope` (derived server-side from the live
  // Docker inspect), it WINS over the record's frozen creation `permissions`.
  it('badge reflects live_envelope (live) over the recorded permissions', async () => {
    stubFetch(
      payload({
        ephemeral: [
          ephemeralEntry({
            id: 'eph-live-env',
            // the record's frozen envelope says network on / rw ...
            permissions: { network: true, filesystem: 'rw', mem_limit: '512m' },
            // ... but the RUNNING container is network-off / read-only.
            live_envelope: { network: false, filesystem: 'read' },
          }),
        ],
      })
    )
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    const badge = await screen.findByTestId('permission-badge-eph-live-env')
    expect(badge).toHaveTextContent('net off · fs read')
    expect(badge).not.toHaveTextContent('fs rw')
  })

  it('badge falls back to the recorded permissions when no live_envelope is present', async () => {
    stubFetch(
      payload({
        ephemeral: [
          ephemeralEntry({
            id: 'eph-no-env',
            permissions: { network: true, filesystem: 'rw', mem_limit: '512m' },
          }),
        ],
      })
    )
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    const badge = await screen.findByTestId('permission-badge-eph-no-env')
    expect(badge).toHaveTextContent('net on · fs rw · mem 512m')
  })

  // Wave (panel-actions funnel) commit 1: a failed action is visible and is
  // never treated as success. It DOES still re-read server state (see the
  // zombie-row regression test below).
  it('a failed action (401) surfaces the server detail and still refetches the list', async () => {
    const containersBody = payload({ ephemeral: [ephemeralEntry({ id: 'eph-fail', state: 'running' })] })
    const fetchMock = vi.fn(async (url) => {
      const u = String(url)
      if (u.endsWith('/action') || u.endsWith('/resources')) {
        return { ok: false, status: 401, json: async () => ({ detail: 'actor required' }) }
      }
      return { ok: true, status: 200, json: async () => containersBody }
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    fireEvent.click(await screen.findByTestId('container-action-eph-fail-stop'))
    fireEvent.click(await screen.findByTestId('confirm-ok'))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('actor required')

    // the confirm modal is closed and no success state was entered
    expect(screen.queryByTestId('confirm-dialog')).toBeNull()

    // the failure still refreshes: the mount GET plus the post-failure GET
    await waitFor(() => {
      const listGets = fetchMock.mock.calls.filter(
        ([u, init]) => String(u).endsWith('/containers') && (!init || !init.method || init.method === 'GET')
      )
      expect(listGets).toHaveLength(2)
    })
    // ...and the error survives that refetch (refresh() resets `error` on success)
    expect(screen.getByRole('alert')).toHaveTextContent('actor required')
  })

  // Regression: a non-ok action response used to return BEFORE refresh(), so
  // the row that caused the failure (a 404 -- the record is already gone)
  // stayed on screen and 404'd again on every later click: a UI dead end.
  it('a failed remove (404) surfaces the error AND refetches, dropping the stale row', async () => {
    const firstList = payload({ persistent: [persistentEntry({ id: 'pers-zombie', state: 'running' })] })
    const emptyList = payload({})
    let listCalls = 0
    const fetchMock = vi.fn(async (url) => {
      if (String(url).includes('/action')) {
        return { ok: false, status: 404, json: async () => ({ detail: 'container record not found' }) }
      }
      listCalls += 1
      return { ok: true, status: 200, json: async () => (listCalls === 1 ? firstList : emptyList) }
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    await screen.findByTestId('container-row-pers-zombie')
    fireEvent.click(screen.getByTestId('container-action-pers-zombie-remove'))
    fireEvent.click(await screen.findByTestId('confirm-ok'))

    // (1) the server detail is surfaced, exactly as before the fix
    expect(await screen.findByRole('alert')).toHaveTextContent('container record not found')
    // (2) the panel re-read server state, so the zombie row is gone
    await waitFor(() => expect(screen.queryByTestId('container-row-pers-zombie')).toBeNull())
    expect(listCalls).toBe(2)
    // (3) the error is not swallowed by that refetch's `error` reset
    expect(screen.getByRole('alert')).toHaveTextContent('container record not found')
  })

  // Wave commit 2: every panel action attributes the operator.
  it('each panel action request sends actor=operator in the body and the X-Actor header', async () => {
    const fetchMock = stubFetch(payload({ ephemeral: [ephemeralEntry({ id: 'eph-act', state: 'running' })] }))
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    fireEvent.click(await screen.findByTestId('container-action-eph-act-stop'))
    fireEvent.click(await screen.findByTestId('confirm-ok'))
    await waitFor(() =>
      expect(
        actionCalls(fetchMock, '/containers/eph-act/action').some((c) => c.body && c.body.action === 'stop')
      ).toBe(true)
    )

    fireEvent.click(screen.getByTestId('container-action-eph-act-restart'))
    fireEvent.click(await screen.findByTestId('confirm-ok'))
    await waitFor(() =>
      expect(
        actionCalls(fetchMock, '/containers/eph-act/action').some((c) => c.body && c.body.action === 'restart')
      ).toBe(true)
    )

    fireEvent.click(screen.getByTestId('container-action-eph-act-resources'))
    fireEvent.click(await screen.findByTestId('confirm-ok'))
    await waitFor(() =>
      expect(actionCalls(fetchMock, '/containers/eph-act/resources').length).toBeGreaterThan(0)
    )

    const calls = actionCalls(fetchMock, '/containers/eph-act/')
    const stop = calls.find((c) => c.body && c.body.action === 'stop')
    const restart = calls.find((c) => c.body && c.body.action === 'restart')
    const resize = calls.find((c) => c.method === 'PATCH')
    expect(stop && restart && resize).toBeTruthy()
    for (const call of [stop, restart, resize]) {
      expect(call.body.actor).toBe('operator')
      expect(call.headers['X-Actor']).toBe('operator')
    }
    expect(resize.body.mem_limit).toBeDefined()
  })

  it('start on an exited row also attributes the operator', async () => {
    const fetchMock = stubFetch(payload({ ephemeral: [ephemeralEntry({ id: 'eph-start', state: 'exited' })] }))
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    fireEvent.click(await screen.findByTestId('container-action-eph-start-start'))
    fireEvent.click(await screen.findByTestId('confirm-ok'))

    await waitFor(() => {
      const call = actionCalls(fetchMock, '/containers/eph-start/action').find(
        (c) => c.body && c.body.action === 'start'
      )
      expect(call).toBeTruthy()
      expect(call.body.actor).toBe('operator')
      expect(call.headers['X-Actor']).toBe('operator')
    })
  })

  // -- Round B: persistent containers are removable from this panel ---------
  // The backend route treats `remove` as an ALLOW-list: `ephemeral` and
  // `persistent` records may be removed, and every other class -- `resource`
  // (operator-managed outside this panel), `service`, or an unknown one -- is
  // refused with 403 before any Docker verb. The panel only *attributes*
  // `actor=operator` on each request; that token is attribution, not
  // authentication -- the backend gate is what authorises the call. The
  // container's `note` is surfaced in the confirm copy so whoever deletes it can
  // see whose work it is about to delete. Ephemeral behaviour is unchanged.

  it('Round B persistent row renders the Remove action and POSTs action=remove after confirm-ok', async () => {
    const fetchMock = stubFetch(payload({ persistent: [persistentEntry({ id: 'pers-rm', state: 'running' })] }))
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    // wait on the ROW (not the group testid, which can resolve before the
    // stubbed fetch settles -- see the T7 flake note)
    await screen.findByTestId('container-row-pers-rm')
    fireEvent.click(screen.getByTestId('container-action-pers-rm-remove'))
    expect(await screen.findByTestId('confirm-dialog')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('confirm-ok'))

    await waitFor(() => {
      const { url, method, body } = actionCall(fetchMock, '/containers/pers-rm/action')
      expect(method).toBe('POST')
      expect(url).toBe(`/api/workspace/${WORKSPACE_ID}/containers/pers-rm/action`)
      expect(body).toEqual({ action: 'remove', actor: 'operator' })
    })
  })

  it('Round B removing a persistent container succeeds and drops the row on refresh', async () => {
    // Stateful stub: the first list GET sees the persistent container, the GET
    // issued after a successful remove does not -- the panel must re-fetch and
    // drop the row instead of keeping a ghost entry.
    const firstList = payload({ persistent: [persistentEntry({ id: 'pers-gone', state: 'running' })] })
    const emptyList = payload({})
    let listCalls = 0
    const fetchMock = vi.fn(async (url) => {
      if (String(url).includes('/action')) {
        return { ok: true, status: 200, json: async () => ({ id: 'pers-gone', removed: true }) }
      }
      listCalls += 1
      return { ok: true, status: 200, json: async () => (listCalls === 1 ? firstList : emptyList) }
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    await screen.findByTestId('container-row-pers-gone')
    fireEvent.click(screen.getByTestId('container-action-pers-gone-remove'))
    fireEvent.click(await screen.findByTestId('confirm-ok'))

    await waitFor(() => expect(screen.queryByTestId('container-row-pers-gone')).toBeNull())
    // success path: no error surfaced, and the list was re-fetched exactly once.
    expect(screen.queryByRole('alert')).toBeNull()
    expect(listCalls).toBe(2)
  })

  it('Round B persistent row renders its note with the full text in the title attribute', async () => {
    const NOTE = 'nightly build cache - safe to delete after 18:00'
    stubFetch(payload({ persistent: [persistentEntry({ id: 'pers-note', note: NOTE })] }))
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    const note = await screen.findByTestId('container-note-pers-note')
    expect(note).toHaveTextContent(NOTE)
    expect(note).toHaveAttribute('title', NOTE)
  })

  it('Round B remove confirmation for a persistent container shows its note (whose work it is)', async () => {
    const NOTE = 'leaving the db warm for the api tests'
    stubFetch(payload({ persistent: [persistentEntry({ id: 'pers-rmnote', state: 'running', note: NOTE })] }))
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    fireEvent.click(await screen.findByTestId('container-action-pers-rmnote-remove'))
    const message = (await screen.findByTestId('confirm-message')).textContent || ''
    expect(message).toContain(NOTE)
    expect(message).toMatch(/remove pers-1/)
    // honest copy: the route allows `remove` only for the ephemeral and
    // persistent classes (every other class is refused) and the request must
    // pass the workspace write envelope; no sub-grain token is mentioned
    // anywhere in the UI
    expect(message).not.toMatch(/grain/i)
  })

  it('Round B resource row still offers no Remove control while a persistent row in the same payload does', async () => {
    stubFetch(
      payload({
        persistent: [persistentEntry({ id: 'pers-ctl', note: 'mine' })],
        resource: [resourceEntry({ id: 'res-ctl' })],
      })
    )
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    await screen.findByTestId('container-row-res-ctl')
    await screen.findByTestId('container-row-pers-ctl')

    // resource: no remove control, no Remove button in the group at all
    expect(screen.queryByTestId('container-action-res-ctl-remove')).toBeNull()
    const resGroup = screen.getByTestId('container-group-resource')
    expect(within(resGroup).queryByText('Remove')).toBeNull()
    // the same gate DOES allow it one group over -- proves it is per-kind
    expect(screen.getByTestId('container-action-pers-ctl-remove')).toBeInTheDocument()
  })
})

// ── driftDecision (pure) ──────────────────────────────────────────────────
// `driftDecision(entry)` is exported so the precedence rule can be pinned
// without mounting the panel. Contract:
//   1. non-empty string `unavailable_reason`      -> { severity: 'red', body }
//   2. non-empty `permission_drift` array         -> { severity: 'neutral', body }
//   3. `permission_drift === null` (unknown)      -> { severity: 'neutral', body }
//   4. otherwise                                  -> null (row renders nothing)
// `unavailable_reason` OUTRANKS drift: it is the only signal that the running
// container is actively MORE permissive than this session's grant.
describe('driftDecision', () => {
  const PERMS = { network: true, filesystem: 'rw', mem_limit: '512m' }

  it('returns null when permission_drift is an empty array (no drift recorded)', () => {
    expect(driftDecision({ id: 'a', permissions: PERMS, permission_drift: [] })).toBeNull()
  })

  it('returns null when permission_drift is a non-null non-array (shape it cannot read)', () => {
    expect(driftDecision({ id: 'a', permissions: PERMS, permission_drift: 'nope' })).toBeNull()
    expect(driftDecision({ id: 'a', permissions: PERMS, permission_drift: undefined })).toBeNull()
  })

  it('returns null when permission_drift is undefined and no reason is present', () => {
    expect(driftDecision({ id: 'a', permissions: PERMS })).toBeNull()
  })

  it('is neutral (NOT red) for a non-empty permission_drift: drift alone never means red', () => {
    const decision = driftDecision({
      id: 'a',
      permissions: PERMS,
      permission_drift: [{ code: 'drift.permission' }],
    })
    expect(decision).not.toBeNull()
    expect(decision.severity).toBe('neutral')
  })

  it('names the drift count in the neutral body', () => {
    const one = driftDecision({ id: 'a', permissions: PERMS, permission_drift: [{ code: 'x' }] })
    expect(one.body).toMatch(/has drifted from its recorded intent/)
    expect(one.body).toContain('(1 item(s))')
    const three = driftDecision({
      id: 'b',
      permissions: PERMS,
      permission_drift: [{ code: 'x' }, { code: 'y' }, { code: 'z' }],
    })
    expect(three.body).toContain('(3 item(s))')
  })

  it('is neutral "not verified" when permission_drift is null but permissions were recorded', () => {
    const decision = driftDecision({ id: 'a', permissions: PERMS, permission_drift: null })
    expect(decision.severity).toBe('neutral')
    expect(decision.body).toBe('Drift not verified for this container.')
  })

  it('is neutral "not verified" when permission_drift is null and permissions are also null', () => {
    const decision = driftDecision({ id: 'a', permissions: null, permission_drift: null })
    expect(decision.severity).toBe('neutral')
    expect(decision.body).toBe('Drift not verified for this container.')
  })

  it('is red for a non-empty unavailable_reason and appends the rebuild instruction', () => {
    const decision = driftDecision({
      id: 'a',
      permissions: PERMS,
      permission_drift: null,
      unavailable_reason: 'container grants a writable fs but this session does not.',
    })
    expect(decision.severity).toBe('red')
    expect(decision.body).toBe(
      'container grants a writable fs but this session does not. Rebuild to apply the current permissions.'
    )
  })

  it('trims surrounding whitespace off unavailable_reason in the red body', () => {
    const decision = driftDecision({ id: 'a', permission_drift: [], unavailable_reason: '  net mismatch.  ' })
    expect(decision.severity).toBe('red')
    expect(decision.body).toBe('net mismatch. Rebuild to apply the current permissions.')
  })

  it('red OUTRANKS a non-empty permission_drift when both are present', () => {
    const decision = driftDecision({
      id: 'a',
      permissions: PERMS,
      permission_drift: [{ code: 'drift.permission' }],
      unavailable_reason: 'net mismatch.',
    })
    expect(decision.severity).toBe('red')
    expect(decision.body).toMatch(/^net mismatch\./)
  })

  it('ignores an empty or whitespace-only unavailable_reason (falls through to drift)', () => {
    const empty = driftDecision({ id: 'a', permission_drift: [], unavailable_reason: '' })
    expect(empty).toBeNull()
    const blank = driftDecision({ id: 'b', permission_drift: [], unavailable_reason: '   ' })
    expect(blank).toBeNull()
    const nonString = driftDecision({ id: 'c', permission_drift: [], unavailable_reason: 42 })
    expect(nonString).toBeNull()
  })
})


// ── Wave: container sprint close (panel fixes, frontend lane) ───────────────
// Fix 1: the failure body is `{error, code}` (server.py `_json_error`), not
//        `{detail}` -- the panel must show `error` and keep the status code as
//        the fallback only.
// Fix 2: a successful mutating action updates the row without an F5. The
//        response body is preferred (something the operator sees immediately);
//        when it is missing or stale the panel re-reads the list, up to 3 times,
//        500ms apart, until the row agrees with the action. No permanent poller.
// Fix 3: the mem BADGE reads the record's intent (live only drives net/fs), and
//        the input is labelled `New limit (currently X)`.
describe('Wave — container sprint close (panel fixes)', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  // Flush the promise-only part of the dispatch chain (everything before the
  // first backoff timer) and let React commit the state updates it produced.
  const flushMicrotasks = async () => {
    await act(async () => {
      for (let i = 0; i < 12; i += 1) await Promise.resolve()
    })
  }

  // Advance the fake clock by one backoff window, then flush what it wakes.
  const advance = async (ms) => {
    await act(async () => {
      vi.advanceTimersByTime(ms)
      for (let i = 0; i < 12; i += 1) await Promise.resolve()
    })
  }

  const listGets = (fetchMock) =>
    fetchMock.mock.calls.filter(
      ([u, init]) => String(u).endsWith('/containers') && (!init || !init.method || init.method === 'GET')
    )

  // A success body exactly as the routes emit it: a container-view entry rendered
  // WITHOUT a live inspect, i.e. `live: null` and `live_envelope: null`.
  function postActionBody(overrides = {}) {
    return {
      id: 'eph-1',
      name: 'eph-1',
      note: '',
      kind: 'ephemeral',
      state: 'running',
      intent_snapshot: { mem_limit: '512m' },
      permissions: { network: true, filesystem: 'rw', mem_limit: '512m' },
      permission_drift: null,
      shared: false,
      live: null,
      unrecorded: false,
      live_envelope: null,
      ...overrides,
    }
  }

  // ── Fix 1 ─────────────────────────────────────────────────────────────────
  it('Fix 1: a 400 displays the body `error` text, not just the status code', async () => {
    const containersBody = payload({ ephemeral: [ephemeralEntry({ id: 'eph-400', state: 'running' })] })
    const fetchMock = vi.fn(async (url) => {
      if (String(url).endsWith('/resources')) {
        return {
          ok: false,
          status: 400,
          json: async () => ({ error: 'mem_limit must be at least 6MB', code: 'below_floor' }),
        }
      }
      return { ok: true, status: 200, json: async () => containersBody }
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    fireEvent.change(await screen.findByTestId('container-resources-mem-eph-400'), {
      target: { value: '1m' },
    })
    fireEvent.click(screen.getByTestId('container-action-eph-400-resources'))
    fireEvent.click(await screen.findByTestId('confirm-ok'))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('mem_limit must be at least 6MB')
    // the useless generic copy must NOT be what the operator reads
    expect(alert).not.toHaveTextContent('Action failed')
  })

  it('Fix 1: a body with neither `error` nor `detail` still falls back to the status code', async () => {
    const containersBody = payload({ ephemeral: [ephemeralEntry({ id: 'eph-400b', state: 'running' })] })
    const fetchMock = vi.fn(async (url) => {
      if (String(url).endsWith('/resources')) {
        return { ok: false, status: 400, json: async () => ({ code: 'below_floor' }) }
      }
      return { ok: true, status: 200, json: async () => containersBody }
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    fireEvent.click(await screen.findByTestId('container-action-eph-400b-resources'))
    fireEvent.click(await screen.findByTestId('confirm-ok'))

    expect(await screen.findByRole('alert')).toHaveTextContent('Action failed (400)')
  })

  // ── Fix 2 ─────────────────────────────────────────────────────────────────
  it('Fix 2: a success body that carries the new state updates the row in place, with no refetch', async () => {
    const before = payload({
      ephemeral: [
        ephemeralEntry({
          id: 'eph-direct',
          state: 'running',
          live: true,
          permissions: { network: true, filesystem: 'rw', mem_limit: '512m' },
          intent_snapshot: { mem_limit: '512m' },
        }),
      ],
    })
    const fetchMock = vi.fn(async (url) => {
      if (String(url).endsWith('/resources')) {
        return {
          ok: true,
          status: 200,
          json: async () =>
            postActionBody({
              id: 'eph-direct',
              intent_snapshot: { mem_limit: '1g' },
              permissions: { network: true, filesystem: 'rw', mem_limit: '1g' },
            }),
        }
      }
      return { ok: true, status: 200, json: async () => before }
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    fireEvent.change(await screen.findByTestId('container-resources-mem-eph-direct'), {
      target: { value: '1g' },
    })
    fireEvent.click(screen.getByTestId('container-action-eph-direct-resources'))
    fireEvent.click(await screen.findByTestId('confirm-ok'))

    await waitFor(() =>
      expect(screen.getByTestId('permission-badge-eph-direct')).toHaveTextContent('mem 1g')
    )
    // the response was authoritative -> no second list read was needed
    expect(listGets(fetchMock)).toHaveLength(1)
    // ...and its `live: null` did not overwrite the row's live marker
    expect(screen.queryByTestId('container-missing-eph-direct')).toBeNull()
  })

  it('Fix 2: a stale success body is repaired by a 500ms backoff refetch -- no F5', async () => {
    const stale = payload({
      ephemeral: [
        ephemeralEntry({
          id: 'eph-late',
          state: 'running',
          permissions: { network: true, filesystem: 'rw', mem_limit: '512m' },
          intent_snapshot: { mem_limit: '512m' },
        }),
      ],
    })
    const updated = payload({
      ephemeral: [
        ephemeralEntry({
          id: 'eph-late',
          state: 'running',
          permissions: { network: true, filesystem: 'rw', mem_limit: '1g' },
          intent_snapshot: { mem_limit: '1g' },
        }),
      ],
    })
    let listCalls = 0
    const fetchMock = vi.fn(async (url) => {
      if (String(url).endsWith('/resources')) {
        // the route answers BEFORE the record write lands: same degraded entry,
        // old mem_limit, live/live_envelope null
        return { ok: true, status: 200, json: async () => postActionBody({ id: 'eph-late' }) }
      }
      listCalls += 1
      // the first re-read is still stale (the write has not landed yet); the
      // backoff retry is the first read that sees the new record
      return { ok: true, status: 200, json: async () => (listCalls <= 2 ? stale : updated) }
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    await screen.findByTestId('container-row-eph-late')

    vi.useFakeTimers()
    fireEvent.change(screen.getByTestId('container-resources-mem-eph-late'), {
      target: { value: '1g' },
    })
    fireEvent.click(screen.getByTestId('container-action-eph-late-resources'))
    fireEvent.click(screen.getByTestId('confirm-ok'))

    // the immediate re-read happens, but the record is still stale
    await flushMicrotasks()
    expect(listCalls).toBe(2)
    expect(screen.getByTestId('permission-badge-eph-late')).toHaveTextContent('mem 512m')

    // the backoff retry fires one window later and the row catches up on its own
    await advance(500)
    expect(listCalls).toBe(3)
    expect(screen.getByTestId('permission-badge-eph-late')).toHaveTextContent('mem 1g')

    // the loop stops as soon as the row matches: no further reads pile up
    await advance(2000)
    expect(listCalls).toBe(3)
  })

  // ── Fix 3 ─────────────────────────────────────────────────────────────────
  it('Fix 3: the mem badge reads the RECORD intent (500m), never the live envelope (256m)', async () => {
    stubFetch(
      payload({
        ephemeral: [
          ephemeralEntry({
            id: 'eph-mem',
            permissions: { network: true, filesystem: 'rw', mem_limit: '500m' },
            intent_snapshot: { mem_limit: '500m' },
            // mid-recreate: the live container still carries the previous limit
            live_envelope: { network: false, filesystem: 'read', mem_limit: '256m' },
          }),
        ],
      })
    )
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    const badge = await screen.findByTestId('permission-badge-eph-mem')
    expect(badge).toHaveTextContent('mem 500m')
    expect(badge).not.toHaveTextContent('256m')
    // net/fs still describe the running container (unchanged contract)
    expect(badge).toHaveTextContent('net off · fs read')

    // the input is explicitly a NEW limit, named against the record's value
    const input = screen.getByTestId('container-resources-mem-eph-mem')
    expect(input.closest('label')).toHaveTextContent('New limit (currently 500m)')
    expect(input).toHaveValue('500m')
  })

  // ── Fix 4 ────────────────────────────────────────────────────────────────
  // `permissions` on a WIRED record is the six-category GRANT dict and carries no
  // `mem_limit`; the memory figure lives in `intent_snapshot`. Reading `mem` off
  // `permissions` dropped it from every persistent row (and the staged input) even
  // though the payload was complete.
  it('Fix 4: the mem badge reads the INTENT snapshot, not the permissions grant dict', async () => {
    stubFetch(
      payload({
        persistent: [
          persistentEntry({
            id: 'pers-grant',
            // exactly what the backend emits for a wired record: a grant dict
            intent_snapshot: { mem_limit: '500m' },
            permissions: { network: true, git: false, filesystem: 'rw', container: true, mcp: [], host_bash: false },
          }),
        ],
      })
    )
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    const badge = await screen.findByTestId('permission-badge-pers-grant')
    expect(badge).toHaveTextContent('net on · fs rw · mem 500m')

    const input = screen.getByTestId('container-resources-mem-pers-grant')
    expect(input.closest('label')).toHaveTextContent('New limit (currently 500m)')
    expect(input).toHaveValue('500m')
  })

  it('Fix 4: a resource row (permissions === null) shows the numeric INTENT mem and its hint', async () => {
    stubFetch(
      payload({
        resource: [resourceEntry({ id: 'res-mem', intent_snapshot: { mem_limit: 536870912 }, permissions: null })],
      })
    )
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    const badge = await screen.findByTestId('permission-badge-res-mem')
    expect(badge).toHaveTextContent('mem 536870912')

    const input = screen.getByTestId('container-resources-mem-res-mem')
    expect(input.closest('label')).toHaveTextContent('New limit (currently 536870912)')
    expect(input).toHaveValue('536870912')
  })

  it('Fix 4: a row with no memory figure renders `mem —` and the plain label', async () => {
    stubFetch(
      payload({
        persistent: [persistentEntry({ id: 'pers-nomem', intent_snapshot: {}, permissions: null })],
      })
    )
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    const badge = await screen.findByTestId('permission-badge-pers-nomem')
    expect(badge).toHaveTextContent('mem —')

    const input = screen.getByTestId('container-resources-mem-pers-nomem')
    expect(input.closest('label')).toHaveTextContent('New limit')
    expect(input.closest('label')).not.toHaveTextContent('currently')
    expect(input).toHaveValue('')
  })

  // ── Pol 2 ────────────────────────────────────────────────────────────────
  // A mutating action takes seconds and the row used to look inert: the click
  // landed, nothing changed, so the operator clicked again. The row now names
  // the action immediately, marks itself busy (its own controls only), escalates
  // to a neutral spinner when the wait drags on, and clears on completion.
  it('Pol 2: a resize marks its own row busy, escalates to a spinner, then releases', async () => {
    const containersBody = payload({
      ephemeral: [ephemeralEntry({ id: 'eph-busy' }), ephemeralEntry({ id: 'eph-free' })],
    })
    let release
    const fetchMock = vi.fn(async (url) => {
      if (String(url).endsWith('/resources')) {
        // held open so the busy state can be observed
        return new Promise((resolve) => {
          release = () =>
            resolve({
              ok: true,
              status: 200,
              json: async () =>
                postActionBody({ id: 'eph-busy', intent_snapshot: { mem_limit: '1g' } }),
            })
        })
      }
      return { ok: true, status: 200, json: async () => containersBody }
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    fireEvent.change(await screen.findByTestId('container-resources-mem-eph-busy'), {
      target: { value: '1g' },
    })

    vi.useFakeTimers()
    fireEvent.click(screen.getByTestId('container-action-eph-busy-resources'))
    fireEvent.click(screen.getByTestId('confirm-ok'))

    // the click is acknowledged: the verb is named, no spinner yet
    expect(screen.getByTestId('container-inflight-eph-busy')).toHaveTextContent('Resizing')
    expect(screen.queryByTestId('container-spinner-eph-busy')).toBeNull()
    // every control on THAT row is inert ...
    expect(screen.getByTestId('container-action-eph-busy-resources')).toBeDisabled()
    expect(screen.getByTestId('container-action-eph-busy-stop')).toBeDisabled()
    expect(screen.getByTestId('container-action-eph-busy-restart')).toBeDisabled()
    // ... while the neighbouring row stays usable
    expect(screen.getByTestId('container-action-eph-free-stop')).not.toBeDisabled()
    expect(screen.queryByTestId('container-inflight-eph-free')).toBeNull()

    // one window later the wait is no longer plausibly fast: neutral + spinner
    await advance(1600)
    expect(screen.getByTestId('container-inflight-eph-busy')).toHaveTextContent('Working')
    expect(screen.getByTestId('container-spinner-eph-busy')).toBeInTheDocument()

    // the response lands: the row is usable again and the indicator is gone
    await act(async () => {
      release()
      for (let i = 0; i < 12; i += 1) await Promise.resolve()
    })
    await flushMicrotasks()
    expect(screen.queryByTestId('container-inflight-eph-busy')).toBeNull()
    expect(screen.queryByTestId('container-spinner-eph-busy')).toBeNull()
    expect(screen.getByTestId('container-action-eph-busy-resources')).not.toBeDisabled()
    expect(screen.getByTestId('container-action-eph-busy-stop')).not.toBeDisabled()
  })

  it('Pol 2: a failed action still shows the message and releases the row', async () => {
    const containersBody = payload({ ephemeral: [ephemeralEntry({ id: 'eph-busy-400' })] })
    const fetchMock = vi.fn(async (url) => {
      if (String(url).endsWith('/resources')) {
        return {
          ok: false,
          status: 400,
          json: async () => ({ error: 'mem_limit is below the Docker floor', code: 'below_floor' }),
        }
      }
      return { ok: true, status: 200, json: async () => containersBody }
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    fireEvent.change(await screen.findByTestId('container-resources-mem-eph-busy-400'), {
      target: { value: '1m' },
    })

    vi.useFakeTimers()
    fireEvent.click(screen.getByTestId('container-action-eph-busy-400-resources'))
    fireEvent.click(screen.getByTestId('confirm-ok'))

    expect(screen.getByTestId('container-inflight-eph-busy-400')).toBeInTheDocument()

    await flushMicrotasks()

    expect(screen.getByRole('alert')).toHaveTextContent('mem_limit is below the Docker floor')
    expect(screen.queryByTestId('container-inflight-eph-busy-400')).toBeNull()
    expect(screen.getByTestId('container-action-eph-busy-400-resources')).not.toBeDisabled()
  })

  it('Pol 2: two Confirm clicks produce exactly ONE request', async () => {
    const containersBody = payload({ ephemeral: [ephemeralEntry({ id: 'eph-dup' })] })
    let release
    const fetchMock = vi.fn(async (url) => {
      if (String(url).endsWith('/resources')) {
        return new Promise((resolve) => {
          release = () => resolve({ ok: true, status: 200, json: async () => postActionBody({ id: 'eph-dup' }) })
        })
      }
      return { ok: true, status: 200, json: async () => containersBody }
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    fireEvent.change(await screen.findByTestId('container-resources-mem-eph-dup'), {
      target: { value: '1g' },
    })

    vi.useFakeTimers()
    fireEvent.click(screen.getByTestId('container-action-eph-dup-resources'))

    // both clicks land before React re-renders, i.e. with the modal still live
    const ok = screen.getByTestId('confirm-ok')
    await act(async () => {
      ok.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      ok.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })

    expect(actionCalls(fetchMock, '/resources')).toHaveLength(1)
    expect(screen.getByTestId('container-inflight-eph-dup')).toBeInTheDocument()

    // a second attempt at the row's own Resize cannot queue another write either
    fireEvent.click(screen.getByTestId('container-action-eph-dup-resources'))
    await flushMicrotasks()
    expect(actionCalls(fetchMock, '/resources')).toHaveLength(1)

    await act(async () => {
      release()
      for (let i = 0; i < 12; i += 1) await Promise.resolve()
    })
  })
})

