// @vitest-environment jsdom
/*
 * AppHealthBanner.test.jsx — App-level integration for the health banners
 * driven by the two-step backend probe (Step 1 GET /api/health, Step 2
 * GET /api/health/containers):
 *   - /api/health unreachable          → "Backend not running" banner
 *   - /api/health OK + containers degraded with hint → hint shown verbatim
 *   - /api/health OK + containers degraded without hint → generic actionable line
 *
 * Renders the REAL App with stubbed global WebSocket / ResizeObserver / fetch
 * (same harness as SessionTabsIntegration.test.jsx).
 */
import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import App from '../../App'
import useStore from '../../store/useStore'
import useWorkspaceStore from '../../store/workspaceStore'
import useSessionTabsStore from '../../sessionTabsStore'

// Mock WebSocket (App connects the hub WS on mount; we never open it here).
class MockWebSocket {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3
  static instances = []

  constructor(url) {
    this.url = url
    this.readyState = MockWebSocket.CONNECTING
    this.sent = []
    this.onopen = null
    this.onmessage = null
    this.onclose = null
    this.onerror = null
    MockWebSocket.instances.push(this)
  }

  send(data) {
    this.sent.push(data)
  }

  close(code = 1001) {
    this.readyState = MockWebSocket.CLOSED
    this.onclose?.({ code })
  }
}

class MockResizeObserver {
  constructor(callback) {
    this.callback = callback
  }
  observe() {}
  unobserve() {}
  disconnect() {}
}

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

const ENTRY = { id: 'ws-test-1', label: 'Code Development', root: '~/workspaces/ws-test-1' }

function stubBackend(entry = ENTRY, extra = {}) {
  const id = entry.id
  return stubFetchByUrl({
    '/api/workspace/list': jsonOk([entry]),
    [`/api/workspace/${id}/effective_permissions`]: jsonOk({
      effective_permissions: {
        filesystem: 'write',
        network: 'read',
        git: 'write',
        system: 'read',
        execution: 'banned',
        container: true,
      },
    }),
    '/api/health/containers': jsonOk({ docker: 'reachable' }),
    [`/api/workspace/${id}/workers`]: jsonOk([]),
    [`/api/workspace/${id}/containers`]: jsonOk({ containers: [] }),
    [`/api/session/list?workspace_id=${id}`]: jsonOk([]),
    '/api/session/create': jsonOk({ session_id: 's-1', mode: 'engineer', name: 'Fix bug' }),
    '/api/resource-catalog': jsonOk({ items: [] }),
    ...extra,
  })
}

const HEALTH_OK = { status: 'ok', service: 'thoughtmachine-web-ui' }

beforeEach(() => {
  localStorage.clear()
  window.location.hash = `#/workspace/${ENTRY.id}`
  useStore.getState().reset()
  useWorkspaceStore.getState().reset()
  useWorkspaceStore.setState({ workspaceList: [{ ...ENTRY }] })
  useSessionTabsStore.getState().reset()
  MockWebSocket.instances = []
  vi.stubGlobal('WebSocket', MockWebSocket)
  vi.stubGlobal('ResizeObserver', MockResizeObserver)
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('App health banners', () => {
  it('shows the backend-down banner when GET /api/health fails', async () => {
    // Step 1 (liveness) fails → the whole probe is aborted, Step 2 never runs.
    stubBackend(ENTRY, {
      '/api/health': () => {
        throw new Error('ECONNREFUSED')
      },
    })
    render(<App />)

    await waitFor(() => {
      expect(
        screen.getByText(/Backend not running — check logs\/backend_startup\.log/)
      ).toBeInTheDocument()
    })
    // The degraded-Docker banner must NOT appear alongside it.
    expect(screen.queryByText(/Docker unavailable/)).not.toBeInTheDocument()
  })

  it('shows the actionable docker hint when the backend is up but docker is degraded', async () => {
    stubBackend(ENTRY, {
      '/api/health': jsonOk(HEALTH_OK),
      '/api/health/containers': jsonOk({
        status: 'degraded',
        docker: { available: false, reason: 'daemon_down', hint: 'sudo systemctl enable --now docker' },
      }),
    })
    render(<App />)

    await waitFor(() => {
      expect(screen.getByText(/sudo systemctl enable --now docker/)).toBeInTheDocument()
    })
    // No backend-down banner, and raw error text (the reason) is never shown.
    expect(screen.queryByText(/Backend not running/)).not.toBeInTheDocument()
    expect(screen.queryByText(/daemon_down/)).not.toBeInTheDocument()
  })

  it('falls back to a generic actionable line when the hint is missing', async () => {
    stubBackend(ENTRY, {
      '/api/health': jsonOk(HEALTH_OK),
      '/api/health/containers': jsonOk({
        status: 'degraded',
        docker: { available: false, reason: 'daemon_down', hint: null },
      }),
    })
    render(<App />)

    await waitFor(() => {
      expect(
        screen.getByText(/Docker unavailable — see the backend startup log for details/)
      ).toBeInTheDocument()
    })
  })
})
