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
//     permission_drift, shared
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
      expect(body).toEqual({ action: 'stop' })
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
      expect(body).toEqual({ action: 'remove' })
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
  // ephemeral rows do not, and only ephemeral rows offer `remove`.
  it('T11 persistent row shows the shared chip and its confirm modal contains the shared copy', async () => {
    stubFetch(payload({ persistent: [persistentEntry({ id: 'pers-shared', state: 'running' })] }))
    render(<ContainerListPanel workspaceId={WORKSPACE_ID} />)

    const chip = await screen.findByTestId('container-shared-pers-shared')
    expect(chip).toHaveAttribute('title', SHARED_TEXT)

    fireEvent.click(screen.getByTestId('container-action-pers-shared-stop'))
    const dialog = await screen.findByTestId('confirm-dialog')
    expect(dialog).toHaveTextContent(SHARED_TEXT)
  })

  it('T11 resource row does NOT render a remove control (server refuses non-ephemeral removal)', async () => {
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
})
