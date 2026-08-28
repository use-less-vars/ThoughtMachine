// @vitest-environment jsdom
/*
 * OnboardingWizard.test.jsx — first-run wizard (Phase 3b).
 *
 * App-level integration (real <App />, stubbed WS / fetch / ResizeObserver):
 *   - onboarding status false  → wizard shown (Welcome + Skip)
 *   - onboarding status true   → wizard NOT shown
 *   - full flow: Get Started → Save & Continue → Create Workspace → Finish
 *     → POST /api/onboarding/complete called + wizard hidden
 *   - Skip → POST /api/onboarding/complete called + wizard hidden
 *
 * Component-level (standalone <OnboardingWizard />):
 *   - renders screen 1
 *   - Test connection success shows the inline ok message
 *   - Skip → onFinished called
 *   - Save & Continue → sendCommand('save_provider', …) with the full profile
 *
 * NOTE: the App-level flow test must OPEN the hub WebSocket
 * (readyState = OPEN + onopen()) because App's wizardSend returns false unless
 * the hub WS is open — otherwise 'Save & Continue' would surface the
 * "backend not ready" error and the flow could not proceed.
 */
import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor, fireEvent } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import App from '../../App'
import OnboardingWizard from '../OnboardingWizard'
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

function openHubWs() {
  // The hub WS is the first socket App creates. Simulate the server accepting
  // the connection so App's wizardSend (readyState === OPEN) succeeds.
  const hub = MockWebSocket.instances[0]
  hub.readyState = MockWebSocket.OPEN
  hub.onopen?.()
}

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

describe('Onboarding wizard — App integration', () => {
  it('shows the wizard when onboarding is incomplete', async () => {
    stubBackend(ENTRY, {
      '/api/health': jsonOk(HEALTH_OK),
      '/api/onboarding/status': jsonOk({ onboarding_complete: false }),
    })
    render(<App />)

    await waitFor(() => {
      expect(screen.getByText('Welcome to ThoughtMachine')).toBeInTheDocument()
    })
    expect(screen.getByRole('button', { name: 'Skip onboarding' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Get started' })).toBeInTheDocument()
  })

  it('hides the wizard when onboarding is already complete', async () => {
    const fetchMock = stubBackend(ENTRY, {
      '/api/health': jsonOk(HEALTH_OK),
      '/api/onboarding/status': jsonOk({ onboarding_complete: true }),
    })
    render(<App />)

    // Wait for the status probe to have completed before asserting absence.
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([url]) => String(url).includes('/api/onboarding/status'))
      ).toBe(true)
    })
    expect(screen.queryByText('Welcome to ThoughtMachine')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Skip onboarding' })).not.toBeInTheDocument()
  })

  it('runs the full flow to Finish, calls /api/onboarding/complete and hides the wizard', async () => {
    const fetchMock = stubBackend(ENTRY, {
      '/api/health': jsonOk(HEALTH_OK),
      '/api/onboarding/status': jsonOk({ onboarding_complete: false }),
      '/api/onboarding/test-connection': jsonOk({ ok: true }),
      '/api/browse/create': jsonOk({ success: true, path: '/home/user/workspaces/my-project' }),
      '/api/workspace/resolve': jsonOk({ workspace_id: 'ws-new', root: '~/workspaces/my-project' }),
      '/api/onboarding/complete': jsonOk({ onboarding_complete: true }),
    })
    render(<App />)

    await waitFor(() => {
      expect(screen.getByText('Welcome to ThoughtMachine')).toBeInTheDocument()
    })

    // wizardSend requires the hub WS to be open — simulate the connection.
    openHubWs()

    // Screen 2 — provider (default: openai_compatible, base URL required).
    fireEvent.click(screen.getByRole('button', { name: 'Get started' }))
    fireEvent.change(screen.getByLabelText('Base URL'), { target: { value: 'https://example.com/v1' } })
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'sk-test-123' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save provider and continue' }))

    // Screen 3 — workspace.
    await waitFor(() => {
      expect(screen.getByText('Create your first workspace')).toBeInTheDocument()
    })
    fireEvent.change(screen.getByLabelText('Workspace name'), { target: { value: 'My Project' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create workspace' }))

    // Screen 4 — summary → Finish.
    await waitFor(() => {
      expect(screen.getByText("You're all set")).toBeInTheDocument()
    })
    fireEvent.click(screen.getByRole('button', { name: 'Finish onboarding' }))

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([url, opts]) =>
          String(url).includes('/api/onboarding/complete') && (opts?.method || 'GET') === 'POST')
      ).toBe(true)
    })
    await waitFor(() => {
      expect(screen.queryByText('Welcome to ThoughtMachine')).not.toBeInTheDocument()
    })
  })

  it('hides the wizard when Skip is clicked', async () => {
    const fetchMock = stubBackend(ENTRY, {
      '/api/health': jsonOk(HEALTH_OK),
      '/api/onboarding/status': jsonOk({ onboarding_complete: false }),
      '/api/onboarding/complete': jsonOk({ onboarding_complete: true }),
    })
    render(<App />)

    await waitFor(() => {
      expect(screen.getByText('Welcome to ThoughtMachine')).toBeInTheDocument()
    })
    fireEvent.click(screen.getByRole('button', { name: 'Skip onboarding' }))

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([url, opts]) =>
          String(url).includes('/api/onboarding/complete') && (opts?.method || 'GET') === 'POST')
      ).toBe(true)
    })
    await waitFor(() => {
      expect(screen.queryByText('Welcome to ThoughtMachine')).not.toBeInTheDocument()
    })
  })
})

describe('Onboarding wizard — component', () => {
  it('renders the welcome screen with the three layer cards', () => {
    stubFetchByUrl({})
    render(<OnboardingWizard onFinished={vi.fn()} sendCommand={vi.fn(() => true)} />)

    expect(screen.getByText('Welcome to ThoughtMachine')).toBeInTheDocument()
    expect(screen.getByText('Landing')).toBeInTheDocument()
    expect(screen.getByText('Workspace')).toBeInTheDocument()
    expect(screen.getByText('Session')).toBeInTheDocument()
  })

  it('shows the inline ok message when the test connection succeeds', async () => {
    stubFetchByUrl({
      '/api/onboarding/test-connection': jsonOk({ ok: true }),
    })
    render(<OnboardingWizard onFinished={vi.fn()} sendCommand={vi.fn(() => true)} />)

    fireEvent.click(screen.getByRole('button', { name: 'Get started' }))
    fireEvent.change(screen.getByLabelText('Base URL'), { target: { value: 'https://example.com/v1' } })
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'sk-test-123' } })
    fireEvent.click(screen.getByRole('button', { name: 'Test connection' }))

    await waitFor(() => {
      expect(screen.getByText('Connection successful')).toBeInTheDocument()
    })
  })

  it('calls POST /api/onboarding/complete and onFinished when skipped', async () => {
    const fetchMock = stubFetchByUrl({
      '/api/onboarding/complete': jsonOk({ onboarding_complete: true }),
    })
    const onFinished = vi.fn()
    render(<OnboardingWizard onFinished={onFinished} sendCommand={vi.fn(() => true)} />)

    fireEvent.click(screen.getByRole('button', { name: 'Skip onboarding' }))

    await waitFor(() => {
      expect(onFinished).toHaveBeenCalledTimes(1)
    })
    const completeCall = fetchMock.mock.calls.find(([url, opts]) =>
      String(url).includes('/api/onboarding/complete'))
    expect(completeCall).toBeTruthy()
    expect(completeCall[1].method).toBe('POST')
  })

  it('sends the save_provider WS command with the full profile on Save & Continue', async () => {
    stubFetchByUrl({})
    const sendCommand = vi.fn(() => true)
    render(<OnboardingWizard onFinished={vi.fn()} sendCommand={sendCommand} />)

    fireEvent.click(screen.getByRole('button', { name: 'Get started' }))
    fireEvent.change(screen.getByLabelText('Base URL'), { target: { value: 'https://example.com/v1' } })
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'sk-test-123' } })
    fireEvent.change(screen.getByLabelText('Default model'), { target: { value: 'gpt-4o' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save provider and continue' }))

    expect(sendCommand).toHaveBeenCalledWith('save_provider', {
      provider: {
        id: 'openai_compatible',
        label: 'OpenAI Compatible',
        provider_type: 'openai_compatible',
        base_url: 'https://example.com/v1',
        api_key: 'sk-test-123',
        default_model: 'gpt-4o',
        models: [],
        timeout: 120,
      },
    })
    // Advanced to the workspace screen.
    expect(screen.getByText('Create your first workspace')).toBeInTheDocument()
  })
})
