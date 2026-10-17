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
//   * drift indicator: container-drift-{id} (data-drift amber|neutral, ABSENT when silent)
//   * shared chip (persistent/resource only): container-shared-{id}
//   * controls: container-action-{id}-stop|start|restart|remove|resources
//   * size-edit input: container-resources-mem-{id}
//   * confirm modal: confirm-dialog / confirm-message / confirm-ok / confirm-cancel
//
// The fetch boundary IS the boundary: we stub global fetch and never mock any
// component internal or helper.

import React from 'react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor, within } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import ContainerListPanel from './ContainerListPanel'

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
  // T10: drift indicator is 3-state.
  it('T10 non-empty permission_drift renders an amber indicator matching /older permissions/i', async () => {
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
    expect(drift).toHaveAttribute('data-drift', 'amber')
    expect(drift).toHaveTextContent(/older permissions/i)
  })

  it('T10 null permission_drift with permissions present renders NO drift indicator (silent)', async () => {
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

    await screen.findByTestId('container-row-eph-silent')
    expect(screen.queryByTestId('container-drift-eph-silent')).toBeNull()
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
  // never treated as success.
  it('a failed action (401) surfaces the server detail and does not refetch as if it succeeded', async () => {
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

    // the failure returned BEFORE refresh(): exactly one containers GET happened
    const listGets = fetchMock.mock.calls.filter(
      ([u, init]) => String(u).endsWith('/containers') && (!init || !init.method || init.method === 'GET')
    )
    expect(listGets).toHaveLength(1)
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

  // -- Round B: trust-by-note for persistent containers ---------------------
  // A persistent container is workspace property: any session whose envelope
  // passes may remove one. Remove is therefore offered on persistent rows too,
  // and the container's `note` is the coordination mechanism that lets the
  // acting session see whose work it is about to delete. Resource rows stay
  // operator-only and never offer Remove. Ephemeral behaviour is unchanged.

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
    // honest copy: removal is gated by the permissions envelope; no sub-grain
    // token is mentioned anywhere in the UI
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
