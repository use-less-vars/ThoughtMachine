// @vitest-environment jsdom
// --- GlobalResources.test.jsx ---
// Shared resource catalog: card rendering, badges, optional fields and the
// disabled Edit action.

import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import GlobalResources from '../GlobalResources'
import useWorkspaceStore from '../../store/workspaceStore'
import useStore from '../../store/useStore'
import useSessionTabsStore from '../../sessionTabsStore'

function jsonOk(data, status = 200) {
  return { ok: true, status, json: async () => data, text: async () => JSON.stringify(data) }
}

const DEFAULT_FALLBACK = { ok: true, status: 200, json: async () => ({}), text: async () => '' }

function stubFetchByUrl(routes, defaultResponse = DEFAULT_FALLBACK) {
  const calls = []
  const fetchMock = vi.fn(async (input, init) => {
    const url = typeof input === 'string' ? input : String(input)
    calls.push(url)
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
  return { fetchMock, calls }
}

const RESOURCE = {
  id: 'npm_cache',
  display_name: 'npm cache',
  description: 'Shared npm cache',
  permission_grain_set: ['read', 'write'],
  default_execution_context: 'container',
  tools: ['npm'],
  dockerfile_reference: 'cache.Dockerfile',
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

describe('GlobalResources', () => {
  it('shows the loading placeholder then renders catalog cards', async () => {
    const { calls } = stubFetchByUrl([
      { match: '/api/resource-catalog', value: jsonOk({ items: [RESOURCE] }) },
    ])
    const { container } = render(<GlobalResources />)
    expect(container.textContent).toContain('Loading resources')
    await screen.findByText('npm cache')
    expect(calls.some((u) => u.includes('/api/resource-catalog'))).toBe(true)

    expect(screen.getByText('Shared npm cache')).toBeInTheDocument()
    expect(screen.getByText('Containerized')).toBeInTheDocument()
    expect(screen.getByText('Context: container')).toBeInTheDocument()
    expect(screen.getByText('Tools: npm')).toBeInTheDocument()
    expect(screen.getByText('read')).toBeInTheDocument()
    expect(screen.getByText('write')).toBeInTheDocument()

    const edit = screen.getByRole('button', { name: 'Edit' })
    expect(edit).toBeDisabled()
    expect(edit).toHaveAttribute('title', 'Coming soon')
  })

  it('accepts a plain array catalog response', async () => {
    stubFetchByUrl([{ match: '/api/resource-catalog', value: jsonOk([RESOURCE]) }])
    render(<GlobalResources />)
    await screen.findByText('npm cache')
  })

  it('shows the empty state when no resources exist', async () => {
    stubFetchByUrl([{ match: '/api/resource-catalog', value: jsonOk([]) }])
    render(<GlobalResources />)
    expect(await screen.findByText('No resources available.')).toBeInTheDocument()
  })

  it('falls back to name and omits optional fields', async () => {
    stubFetchByUrl([
      { match: '/api/resource-catalog', value: jsonOk([{ name: 'plain-tool', description: 'A plain tool' }]) },
    ])
    render(<GlobalResources />)
    await screen.findByText('plain-tool')
    expect(screen.getByText('A plain tool')).toBeInTheDocument()
    expect(screen.queryByText('Containerized')).toBeNull()
    expect(screen.queryByText(/Context:/)).toBeNull()
    expect(screen.queryByText(/Tools:/)).toBeNull()
    expect(screen.queryByText('read')).toBeNull()
  })
})
