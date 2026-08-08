// @vitest-environment jsdom
/*
 * WorkspaceSessionList.test.jsx — session list tests (Phase 1).
 *
 * Tests the REAL SessionList (src/components/WorkspaceSessionList.jsx)
 * against stubbed fetch responses. Session list is fetched from
 * `/api/session/list?workspace_id=<id>`; deletes go to `/api/session/<id>`.
 */

import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import SessionList from '../WorkspaceSessionList'

// ---------------------------------------------------------------------------
// fetch stubs
// ---------------------------------------------------------------------------
function jsonOk(data, status = 200) {
  return { ok: true, status, json: async () => data, text: async () => JSON.stringify(data) }
}
function jsonErr(msg = 'error', status = 500) {
  return {
    ok: false,
    status,
    json: async () => {
      throw new Error(msg)
    },
    text: async () => msg,
  }
}

const DEFAULT_FALLBACK = { ok: true, status: 200, json: async () => [], text: async () => '[]' }

function stubFetchByUrl(routes) {
  const fetchMock = vi.fn(async (url, options) => {
    const sorted = Object.entries(routes).sort((a, b) => b[0].length - a[0].length)
    for (const [key, value] of sorted) {
      if (String(url).includes(key)) return typeof value === 'function' ? value(url, options) : value
    }
    return DEFAULT_FALLBACK
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

const NOW = Date.now()
function session(id, over = {}) {
  const ts = new Date(NOW - 3600 * 1000).toISOString()
  return { session_id: id, name: `Session ${id}`, mode: 'agent', created_at: ts, updated_at: ts, ...over }
}

function renderList(props = {}) {
  const defaults = { workspaceId: 'ws-1', onOpen: vi.fn(), onNewSession: vi.fn(), onBack: vi.fn() }
  const merged = { ...defaults, ...props }
  const utils = render(<SessionList {...merged} />)
  return { ...utils, onOpen: merged.onOpen, onNewSession: merged.onNewSession, onBack: merged.onBack }
}

// ---------------------------------------------------------------------------
// Setup / teardown
// ---------------------------------------------------------------------------
beforeEach(() => {
  localStorage.clear()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

// ===========================================================================
// Empty states
// ===========================================================================
describe('SessionList — empty states', () => {
  it('shows the placeholder when no workspace is selected', () => {
    renderList({ workspaceId: null })
    expect(screen.getByText('Sessions')).toBeInTheDocument()
    expect(screen.getByText('No workspace selected.')).toBeInTheDocument()
  })

  it('shows the loading indicator while the list is pending', () => {
    stubFetchByUrl({
      '/api/session/list?workspace_id=ws-1': () => new Promise(() => {}),
    })
    renderList()
    expect(screen.getByText(/Loading sessions/)).toBeInTheDocument()
  })

  it('shows the empty message when the backend returns no sessions', async () => {
    stubFetchByUrl({ '/api/session/list?workspace_id=ws-1': jsonOk([]) })
    renderList()
    await waitFor(() =>
      expect(screen.getByText(/No sessions yet in this workspace/)).toBeInTheDocument()
    )
    // header button + the <strong> hint both say "+ New Session"
    expect(screen.getAllByText('+ New Session')).toHaveLength(2)
  })

  it('shows the server error when the list request fails', async () => {
    stubFetchByUrl({ '/api/session/list?workspace_id=ws-1': jsonErr('boom', 500) })
    renderList()
    await waitFor(() => expect(screen.getByText(/Server returned 500/)).toBeInTheDocument())
  })
})

// ===========================================================================
// Rendering
// ===========================================================================
describe('SessionList — rendering', () => {
  it('renders session cards sorted most recent first', async () => {
    stubFetchByUrl({
      '/api/session/list?workspace_id=ws-1': jsonOk([
        session('old', { name: 'Older', updated_at: new Date(NOW - 86400000).toISOString() }),
        session('new', { name: 'Newer', updated_at: new Date(NOW - 3600000).toISOString() }),
      ]),
    })
    renderList()
    const older = await screen.findByText('Older')
    const newer = screen.getByText('Newer')
    expect(newer.compareDocumentPosition(older) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  }, 20000)

  it('falls back to Untitled Session when name is missing', async () => {
    stubFetchByUrl({ '/api/session/list?workspace_id=ws-1': jsonOk([session('s1', { name: '' })]) })
    renderList()
    expect(await screen.findByText('Untitled Session')).toBeInTheDocument()
  }, 20000)

  it('labels modes: Agent, Engineer, Custom, and raw fallback', async () => {
    stubFetchByUrl({
      '/api/session/list?workspace_id=ws-1': jsonOk([
        session('a', { mode: 'agent' }),
        session('b', { mode: 'engineer' }),
        session('c', { mode: 'custom' }),
        session('d', { mode: 'boss' }),
        session('e', { mode: undefined }),
      ]),
    })
    renderList()
    await screen.findByText('Engineer')
    // 'a' (agent) and 'e' (undefined -> Agent) both show 'Agent'
    expect(screen.getAllByText('Agent')).toHaveLength(2)
    expect(screen.getByText('Engineer')).toBeInTheDocument()
    expect(screen.getByText('Custom')).toBeInTheDocument()
    expect(screen.getByText('boss')).toBeInTheDocument()
  }, 20000)

  it('renders the preview text when present', async () => {
    stubFetchByUrl({
      '/api/session/list?workspace_id=ws-1': jsonOk([session('s1', { preview: 'wip: dockerfile' })]),
    })
    renderList()
    expect(await screen.findByText('wip: dockerfile')).toBeInTheDocument()
  })

  it('formats relative timestamps (just now / m ago)', async () => {
    stubFetchByUrl({
      '/api/session/list?workspace_id=ws-1': jsonOk([
        session('a', { updated_at: new Date(NOW - 30 * 1000).toISOString() }),
        session('b', { updated_at: new Date(NOW - 5 * 60000).toISOString() }),
      ]),
    })
    renderList()
    await screen.findByText(/just now/)
    expect(screen.getByText(/5m ago/)).toBeInTheDocument()
  }, 20000)
})

// ===========================================================================
// Interactions
// ===========================================================================
describe('SessionList — interactions', () => {
  it('opens a session when its card is clicked', async () => {
    stubFetchByUrl({ '/api/session/list?workspace_id=ws-1': jsonOk([session('s9')]) })
    const { onOpen } = renderList()
    fireEvent.click(await screen.findByText('Session s9'))
    expect(onOpen).toHaveBeenCalledTimes(1)
    expect(onOpen).toHaveBeenCalledWith('s9')
  })

  it('opens a session via the Open button without double-firing', async () => {
    stubFetchByUrl({ '/api/session/list?workspace_id=ws-1': jsonOk([session('s9')]) })
    const { onOpen } = renderList()
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    expect(onOpen).toHaveBeenCalledTimes(1)
    expect(onOpen).toHaveBeenCalledWith('s9')
  })

  it('fires onBack and onNewSession from the header buttons', async () => {
    stubFetchByUrl({ '/api/session/list?workspace_id=ws-1': jsonOk([session('s1')]) })
    const { onBack, onNewSession } = renderList()
    await screen.findByText('Session s1')
    fireEvent.click(screen.getByRole('button', { name: '← Back' }))
    fireEvent.click(screen.getByRole('button', { name: '+ New Session' }))
    expect(onBack).toHaveBeenCalledTimes(1)
    expect(onNewSession).toHaveBeenCalledTimes(1)
  })
})

// ===========================================================================
// Deletion
// ===========================================================================
describe('SessionList — deletion', () => {
  it('deletes a session after confirm and removes it from the list', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const fetchMock = stubFetchByUrl({
      '/api/session/list?workspace_id=ws-1': jsonOk([session('s1', { name: 'Doomed' })]),
      '/api/session/s1': jsonOk({ ok: true }),
    })
    renderList()
    await screen.findByText('Doomed')
    fireEvent.click(screen.getByRole('button', { name: '✕' }))
    expect(confirmSpy).toHaveBeenCalledWith('Delete this session? This cannot be undone.')
    await waitFor(() => expect(screen.queryByText('Doomed')).not.toBeInTheDocument())
    const deleteCalls = fetchMock.mock.calls.filter(([, o]) => o && o.method === 'DELETE')
    expect(deleteCalls).toHaveLength(1)
    expect(String(deleteCalls[0][0])).toBe('/api/session/s1')
  })

  it('keeps the session when confirm is cancelled', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    const fetchMock = stubFetchByUrl({
      '/api/session/list?workspace_id=ws-1': jsonOk([session('s1', { name: 'Doomed' })]),
      '/api/session/s1': jsonOk({ ok: true }),
    })
    renderList()
    await screen.findByText('Doomed')
    fireEvent.click(screen.getByRole('button', { name: '✕' }))
    expect(screen.getByText('Doomed')).toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([, o]) => o && o.method === 'DELETE')).toBe(false)
  })

  it('shows an error and hides the session list when delete fails', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    stubFetchByUrl({
      '/api/session/list?workspace_id=ws-1': jsonOk([session('s1', { name: 'Doomed' })]),
      '/api/session/s1': jsonErr('nope', 500),
    })
    renderList()
    await screen.findByText('Doomed')
    fireEvent.click(screen.getByRole('button', { name: '✕' }))
    await waitFor(() => expect(screen.getByText(/Failed to delete/)).toBeInTheDocument())
    // The error replaces the list entirely (!loading && !error guard in the component).
    expect(screen.queryByText('Doomed')).not.toBeInTheDocument()
  })
})
