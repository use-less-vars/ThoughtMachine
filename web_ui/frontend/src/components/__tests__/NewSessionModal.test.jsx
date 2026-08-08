// @vitest-environment jsdom
/*
 * NewSessionModal.test.jsx — session create/list/delete modal (Phase 3).
 *
 * Renders the REAL NewSessionModal (src/components/workspace/modals/
 * NewSessionModal.jsx) with the REAL workspace store and a stubbed global
 * fetch. The modal lists the workspace's sessions on mount
 * (GET /api/session/list), creates sessions (POST /api/session/create) and
 * deletes them (DELETE /api/session/{id}) through the workspaceStore actions.
 * Mode is chosen via the Agent / Engineer / Custom buttons (the default comes
 * from localStorage 'thoughtmachine_last_mode', falling back to 'engineer').
 */

import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import NewSessionModal from '../workspace/modals/NewSessionModal'
import useWorkspaceStore from '../../store/workspaceStore'

// ---------------------------------------------------------------------------
// fetch stubs — substring routes, longest key wins
// ---------------------------------------------------------------------------
function jsonOk(data, status = 200) {
  return { ok: true, status, json: async () => data, text: async () => JSON.stringify(data) }
}

function jsonErr(detail, status = 500) {
  return { ok: false, status, json: async () => ({ detail }), text: async () => JSON.stringify({ detail }) }
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

const WORKSPACE = {
  id: 'ws-1',
  name: 'Demo Dev Workspace',
  path: '~/workspaces/demo-dev',
  root: '',
  sessions: [],
}

function stubBackend(extra = {}) {
  return stubFetchByUrl({
    '/api/session/list?workspace_id=ws-1': jsonOk([]),
    '/api/session/create': jsonOk({ session_id: 's-new', mode: 'engineer', name: 'Fix bug' }),
    ...extra,
  })
}

// ---------------------------------------------------------------------------
// Setup / teardown
// ---------------------------------------------------------------------------
beforeEach(() => {
  localStorage.clear()
  useWorkspaceStore.getState().reset()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function renderModal(props = {}) {
  const onClose = props.onClose || vi.fn()
  return {
    onClose,
    ...render(<NewSessionModal workspace={props.workspace || WORKSPACE} onClose={onClose} />),
  }
}

function activeMode() {
  return document.querySelector('.wp-mode-option-active')
}

// ===========================================================================
// Structure / mode selection
// ===========================================================================
describe('NewSessionModal — structure and modes', () => {
  it('renders the title, path line, name input and the three mode options', async () => {
    stubBackend()
    renderModal()
    expect(screen.getByRole('heading', { name: 'New Session' })).toBeInTheDocument()
    expect(screen.getByText('Demo Dev Workspace — ~/workspaces/demo-dev')).toBeInTheDocument()
    expect(screen.getByText('Sessions in this workspace')).toBeInTheDocument()
    expect(screen.getByPlaceholderText('e.g. Refactor auth module')).toBeInTheDocument()

    expect(screen.getByText('Agent')).toBeInTheDocument()
    expect(screen.getByText('Full tools, no worker')).toBeInTheDocument()
    expect(screen.getByText('Engineer')).toBeInTheDocument()
    expect(screen.getByText('Delegation only')).toBeInTheDocument()
    expect(screen.getByText('Custom')).toBeInTheDocument()
    expect(screen.getByText('Your tools, your prompt')).toBeInTheDocument()

    // Engineer is the default mode (no localStorage value set)
    expect(activeMode().textContent).toContain('Engineer')
    await waitFor(() => expect(screen.getByText('No sessions yet.')).toBeInTheDocument())
  }, 20000)

  it('defaults to the mode persisted in thoughtmachine_last_mode', async () => {
    localStorage.setItem('thoughtmachine_last_mode', 'agent')
    stubBackend()
    renderModal()
    expect(activeMode().textContent).toContain('Agent')
    await waitFor(() => expect(screen.getByText('No sessions yet.')).toBeInTheDocument())
  }, 20000)

  it('switches the active mode when another mode button is clicked', async () => {
    stubBackend()
    renderModal()
    fireEvent.click(screen.getByText('Custom').closest('button'))
    expect(activeMode().textContent).toContain('Custom')
    expect(activeMode().textContent).not.toContain('Engineer')
    await waitFor(() => expect(screen.getByText('No sessions yet.')).toBeInTheDocument())
  }, 20000)
})

// ===========================================================================
// Closing behaviour
// ===========================================================================
describe('NewSessionModal — closing', () => {
  it('calls onClose when Cancel is clicked', async () => {
    stubBackend()
    const { onClose } = renderModal()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onClose).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(screen.getByText('No sessions yet.')).toBeInTheDocument())
  }, 20000)

  it('calls onClose on overlay click but not on inner dialog clicks', async () => {
    stubBackend()
    const { onClose } = renderModal()
    const dialog = screen.getByRole('dialog', { name: 'New session' })
    fireEvent.click(dialog)
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.click(dialog.parentElement) // the overlay
    expect(onClose).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(screen.getByText('No sessions yet.')).toBeInTheDocument())
  })
})

// ===========================================================================
// Sessions list
// ===========================================================================
describe('NewSessionModal — sessions list', () => {
  it('shows the loading state while sessions are being fetched', () => {
    stubBackend({ '/api/session/list?workspace_id=ws-1': () => new Promise(() => {}) })
    renderModal()
    expect(screen.getByText('Loading sessions…')).toBeInTheDocument()
  })

  it('renders fetched sessions with name, mode badge, Open and Delete buttons', async () => {
    stubBackend({
      '/api/session/list?workspace_id=ws-1': jsonOk([
        { session_id: 's-1', name: 'Auth refactor', mode: 'engineer', updated_at: '2026-01-01T00:00:00Z' },
        { session_id: 's-2', name: '', mode: 'agent', updated_at: null },
      ]),
    })
    renderModal()
    await waitFor(() => expect(screen.getByText('Auth refactor')).toBeInTheDocument())
    expect(screen.getByText('Untitled')).toBeInTheDocument()
    expect(screen.getByText('engineer')).toBeInTheDocument()
    expect(screen.getByText('agent')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Open' })).toHaveLength(2)
    expect(screen.getAllByRole('button', { name: 'Delete' })).toHaveLength(2)
  }, 20000)

  it('shows the empty state when the workspace has no sessions', async () => {
    stubBackend()
    renderModal()
    await waitFor(() => expect(screen.getByText('No sessions yet.')).toBeInTheDocument())
  })

  it('opens a session from the row Open button', async () => {
    stubBackend({
      '/api/session/list?workspace_id=ws-1': jsonOk([
        { session_id: 's-1', name: 'Auth refactor', mode: 'engineer', updated_at: null },
      ]),
    })
    renderModal()
    await waitFor(() => expect(screen.getByText('Auth refactor')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Open' }))
    expect(localStorage.getItem('activeSessionId')).toBe('s-1')
    expect(window.location.hash).toBe('#/')
  })
})

// ===========================================================================
// Deletion
// ===========================================================================
describe('NewSessionModal — deletion', () => {
  it('deletes a session after confirmation and refreshes the list', async () => {
    let sessions = [{ session_id: 's-1', name: 'Auth refactor', mode: 'engineer', updated_at: null }]
    let deleted = false
    const fetchMock = stubBackend({
      '/api/session/list?workspace_id=ws-1': () => jsonOk(deleted ? [] : sessions),
      '/api/session/s-1?workspace_id=ws-1': () => {
        deleted = true
        return jsonOk({ ok: true })
      },
    })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderModal()
    await waitFor(() => expect(screen.getByText('Auth refactor')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    await waitFor(() => {
      const del = fetchMock.mock.calls.find(
        ([u, o]) => String(u).includes('/api/session/s-1') && o?.method === 'DELETE'
      )
      expect(del).toBeTruthy()
      expect(String(del[0])).toContain('workspace_id=ws-1')
    })
    expect(window.confirm).toHaveBeenCalledWith('Delete session "Auth refactor"?')
    // Refetch after delete → the list is now empty
    await waitFor(() => expect(screen.getByText('No sessions yet.')).toBeInTheDocument())
  }, 20000)

  it('does not delete when the confirmation is cancelled', async () => {
    const fetchMock = stubBackend({
      '/api/session/list?workspace_id=ws-1': jsonOk([
        { session_id: 's-1', name: '', mode: 'agent', updated_at: null },
      ]),
      '/api/session/s-1?workspace_id=ws-1': jsonOk({ ok: true }),
    })
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderModal()
    await waitFor(() => expect(screen.getByText('Untitled')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    expect(window.confirm).toHaveBeenCalledWith('Delete session "Untitled"?')
    await new Promise((r) => setTimeout(r, 0))
    const del = fetchMock.mock.calls.find(
      ([u, o]) => String(u).includes('/api/session/s-1') && o?.method === 'DELETE'
    )
    expect(del).toBeFalsy()
  }, 20000)
})

// ===========================================================================
// Creation
// ===========================================================================
describe('NewSessionModal — creation', () => {
  it('creates a session, persists the mode and shows the success view', async () => {
    const fetchMock = stubBackend()
    renderModal()
    await waitFor(() => expect(screen.getByText('No sessions yet.')).toBeInTheDocument())
    fireEvent.change(screen.getByPlaceholderText('e.g. Refactor auth module'), {
      target: { value: 'Fix bug' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Create Session' }))
    await waitFor(() => {
      const post = fetchMock.mock.calls.find(
        ([u, o]) => String(u).includes('/api/session/create') && o?.method === 'POST'
      )
      expect(post).toBeTruthy()
      expect(JSON.parse(post[1].body)).toEqual({
        mode: 'engineer',
        name: 'Fix bug',
        workspace_id: 'ws-1',
      })
    })
    await waitFor(() => expect(localStorage.getItem('thoughtmachine_last_mode')).toBe('engineer'))
    await waitFor(() => expect(screen.getByText('Session created.')).toBeInTheDocument())
    expect(screen.getByText('Session ID:')).toBeInTheDocument()
    expect(screen.getByText('s-new')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Close' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Open Session' })).toBeInTheDocument()
  })

  it('creates with the selected Agent mode', async () => {
    const fetchMock = stubBackend()
    renderModal()
    await waitFor(() => expect(screen.getByText('No sessions yet.')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Agent').closest('button'))
    fireEvent.change(screen.getByPlaceholderText('e.g. Refactor auth module'), {
      target: { value: 'Scout' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Create Session' }))
    await waitFor(() => {
      const post = fetchMock.mock.calls.find(
        ([u, o]) => String(u).includes('/api/session/create') && o?.method === 'POST'
      )
      expect(post).toBeTruthy()
      expect(JSON.parse(post[1].body).mode).toBe('agent')
    })
    await waitFor(() => expect(localStorage.getItem('thoughtmachine_last_mode')).toBe('agent'))
    await waitFor(() => expect(screen.getByText('Session created.')).toBeInTheDocument())
  })

  it('creates a session via Enter in the name field', async () => {
    const fetchMock = stubBackend()
    renderModal()
    await waitFor(() => expect(screen.getByText('No sessions yet.')).toBeInTheDocument())
    fireEvent.change(screen.getByPlaceholderText('e.g. Refactor auth module'), {
      target: { value: 'Quick fix' },
    })
    fireEvent.keyDown(screen.getByPlaceholderText('e.g. Refactor auth module'), { key: 'Enter' })
    await waitFor(() => {
      const post = fetchMock.mock.calls.find(
        ([u, o]) => String(u).includes('/api/session/create') && o?.method === 'POST'
      )
      expect(post).toBeTruthy()
      expect(JSON.parse(post[1].body).name).toBe('Quick fix')
    })
    await waitFor(() => expect(screen.getByText('Session created.')).toBeInTheDocument())
  })

  it('shows the server error message when creation fails', async () => {
    stubBackend({ '/api/session/create': jsonErr('Name too long', 400) })
    renderModal()
    await waitFor(() => expect(screen.getByText('No sessions yet.')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Create Session' }))
    await waitFor(() => expect(screen.getByText('Name too long')).toBeInTheDocument())
    expect(screen.queryByText('Session created.')).not.toBeInTheDocument()
  })

  it('opens the created session via the Open Session button', async () => {
    stubBackend()
    renderModal()
    await waitFor(() => expect(screen.getByText('No sessions yet.')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Create Session' }))
    await waitFor(() => expect(screen.getByText('Session created.')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Open Session' }))
    expect(localStorage.getItem('activeSessionId')).toBe('s-new')
    expect(window.location.hash).toBe('#/')
  })
})
