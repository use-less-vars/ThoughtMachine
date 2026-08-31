// @vitest-environment jsdom
// --- GlobalCredentials.test.jsx ---
// Vault credential management: list (names + masks, never secrets), inline
// two-step delete, and the add-credential modal.

import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import GlobalCredentials from '../GlobalCredentials'
import useWorkspaceStore from '../../store/workspaceStore'
import useStore from '../../store/useStore'
import useSessionTabsStore from '../../sessionTabsStore'

function jsonOk(data, status = 200) {
  return { ok: true, status, json: async () => data, text: async () => JSON.stringify(data) }
}

function jsonNotOk(data, status = 500) {
  return { ok: false, status, json: async () => data, text: async () => JSON.stringify(data) }
}

const DEFAULT_FALLBACK = { ok: true, status: 200, json: async () => ({}), text: async () => '' }

function stubFetchByUrl(routes, defaultResponse = DEFAULT_FALLBACK) {
  const calls = []
  const requests = []
  const fetchMock = vi.fn(async (input, init) => {
    const url = typeof input === 'string' ? input : String(input)
    calls.push(url)
    requests.push({ url, init: init || {} })
    const func = routes.find((r) => typeof r.match === 'function' && r.match(url, init))
    const str = routes
      .filter((r) => typeof r.match === 'string' && url.includes(r.match))
      .sort((a, b) => b.match.length - a.match.length)[0]
    const route = func || str
    if (!route) return defaultResponse
    const value = typeof route.value === 'function' ? route.value(url, init) : route.value
    return value
  })
  vi.stubGlobal('fetch', fetchMock)
  return { fetchMock, calls, requests }
}

const CREDS = [{ name: 'github_token' }, { name: 'openai_api_key' }]

function isGet(url, init) {
  return url === '/api/credentials' && (!init || !init.method || init.method === 'GET')
}

beforeEach(() => {
  localStorage.clear()
  window.location.hash = ''
  useWorkspaceStore.getState().reset()
  useStore.getState().reset()
  useSessionTabsStore.getState().reset()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('GlobalCredentials', () => {
  it('shows the loading placeholder then fetches credentials', async () => {
    const { calls } = stubFetchByUrl([
      { match: (url, init) => isGet(url, init), value: jsonOk({ credentials: CREDS }) },
    ])
    const { container } = render(<GlobalCredentials />)
    expect(container.textContent).toContain('Loading credentials')
    await screen.findByText('github_token')
    expect(calls.some((u) => u.includes('/api/credentials'))).toBe(true)
  })

  it('renders names with masked secrets and never plaintext', async () => {
    stubFetchByUrl([{ match: (url, init) => isGet(url, init), value: jsonOk({ credentials: CREDS }) }])
    render(<GlobalCredentials />)
    await screen.findByText('github_token')
    expect(screen.getByText('openai_api_key')).toBeInTheDocument()
    expect(screen.getAllByText('\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022').length).toBe(2)
    expect(screen.queryByText(/sk-secret|abc123/)).toBeNull()
  })

  it('shows the empty state when no credentials exist', async () => {
    stubFetchByUrl([{ match: (url, init) => isGet(url, init), value: jsonOk([]) }])
    render(<GlobalCredentials />)
    expect(await screen.findByText('No credentials stored in the vault.')).toBeInTheDocument()
  })

  it('shows the empty state when the API returns no credential list', async () => {
    stubFetchByUrl([{ match: (url, init) => isGet(url, init), value: jsonOk({}) }])
    render(<GlobalCredentials />)
    expect(await screen.findByText('No credentials stored in the vault.')).toBeInTheDocument()
  })

  it('deletes a credential after inline confirmation', async () => {
    let remaining = CREDS
    const { requests } = stubFetchByUrl([
      { match: (url, init) => isGet(url, init), value: () => jsonOk({ credentials: remaining }) },
      {
        match: (url, init) => url === '/api/credentials/github_token' && init && init.method === 'DELETE',
        value: () => {
          remaining = remaining.filter((c) => c.name !== 'github_token')
          return jsonOk({ ok: true })
        },
      },
    ])
    render(<GlobalCredentials />)
    await screen.findByText('github_token')

    fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[0])
    expect(screen.getByRole('button', { name: 'Confirm' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    await waitFor(() => expect(screen.queryByText('github_token')).toBeNull())
    expect(
      requests.some((r) => r.url === '/api/credentials/github_token' && r.init.method === 'DELETE')
    ).toBe(true)
    expect(screen.getByText('openai_api_key')).toBeInTheDocument()
  })

  it('Cancel aborts the delete flow', async () => {
    stubFetchByUrl([{ match: (url, init) => isGet(url, init), value: jsonOk({ credentials: CREDS }) }])
    render(<GlobalCredentials />)
    await screen.findByText('github_token')

    fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[0])
    expect(screen.getByRole('button', { name: 'Confirm' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('button', { name: 'Confirm' })).toBeNull()
    expect(screen.getByText('github_token')).toBeInTheDocument()
  })

  it('resets the confirm flow and keeps the row when deletion fails', async () => {
    const { requests } = stubFetchByUrl([
      { match: (url, init) => isGet(url, init), value: jsonOk({ credentials: CREDS }) },
      {
        match: (url, init) => url === '/api/credentials/github_token' && init && init.method === 'DELETE',
        value: jsonNotOk({ ok: false }),
      },
    ])
    render(<GlobalCredentials />)
    await screen.findByText('github_token')

    fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[0])
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Confirm' })).toBeNull())
    expect(
      requests.some((r) => r.url === '/api/credentials/github_token' && r.init.method === 'DELETE')
    ).toBe(true)
    // The row remains after the failed delete.
    expect(screen.getByText('github_token')).toBeInTheDocument()
  })

  it('requires name and secret before saving', async () => {
    stubFetchByUrl([{ match: (url, init) => isGet(url, init), value: jsonOk({ credentials: CREDS }) }])
    render(<GlobalCredentials />)
    await screen.findByText('github_token')

    fireEvent.click(screen.getByRole('button', { name: '+ Add Credential' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText('Name and secret are required.')).toBeInTheDocument()
    expect(screen.getByRole('dialog', { name: 'Add credential' })).toBeInTheDocument()
  })

  it('adds a credential via the modal', async () => {
    const created = []
    stubFetchByUrl([
      {
        match: (url, init) => isGet(url, init),
        value: () => jsonOk({ credentials: CREDS.concat(created) }),
      },
      {
        match: (url, init) => url === '/api/credentials' && init && init.method === 'POST',
        value: (url, init) => {
          created.push(JSON.parse(init.body))
          return jsonOk({ ok: true })
        },
      },
    ])
    render(<GlobalCredentials />)
    await screen.findByText('github_token')

    fireEvent.click(screen.getByRole('button', { name: '+ Add Credential' }))
    expect(screen.getByRole('dialog', { name: 'Add credential' })).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'stripe_key' } })
    fireEvent.change(screen.getByLabelText('Secret'), { target: { value: 'sk-live-xyz' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Add credential' })).toBeNull())
    await screen.findByText('stripe_key')
    // Locks the backend contract: POST /api/credentials requires {name, value}.
    expect(created).toEqual([{ name: 'stripe_key', value: 'sk-live-xyz' }])
    // The secret value never appears as text.
    expect(screen.queryByText(/sk-live-xyz/)).toBeNull()
  })

  it('shows an error when saving fails and keeps the modal open', async () => {
    stubFetchByUrl([
      { match: (url, init) => isGet(url, init), value: jsonOk({ credentials: CREDS }) },
      {
        match: (url, init) => url === '/api/credentials' && init && init.method === 'POST',
        value: jsonNotOk({ ok: false }),
      },
    ])
    render(<GlobalCredentials />)
    await screen.findByText('github_token')

    fireEvent.click(screen.getByRole('button', { name: '+ Add Credential' }))
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'stripe_key' } })
    fireEvent.change(screen.getByLabelText('Secret'), { target: { value: 'sk-live-xyz' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText('Failed to save credential.')).toBeInTheDocument()
    expect(screen.getByRole('dialog', { name: 'Add credential' })).toBeInTheDocument()
  })

  it('closes the add modal with Cancel', async () => {
    stubFetchByUrl([{ match: (url, init) => isGet(url, init), value: jsonOk({ credentials: CREDS }) }])
    render(<GlobalCredentials />)
    await screen.findByText('github_token')

    fireEvent.click(screen.getByRole('button', { name: '+ Add Credential' }))
    expect(screen.getByRole('dialog', { name: 'Add credential' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog', { name: 'Add credential' })).toBeNull()
  })
})
