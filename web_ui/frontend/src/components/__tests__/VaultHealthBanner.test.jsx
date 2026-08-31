// @vitest-environment jsdom
// --- VaultHealthBanner.test.jsx ---
// Vault integrity banner: fetches /api/vault/status on mount, polls every 30s
// and renders healthy / degraded / error states.

import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import VaultHealthBanner from '../VaultHealthBanner'
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
  vi.useRealTimers()
})

describe('VaultHealthBanner', () => {
  it('renders nothing until the first status check completes', () => {
    const { fetchMock } = stubFetchByUrl([{ match: '/api/vault/status', value: jsonOk({ ok: true }) }])
    const { container } = render(<VaultHealthBanner />)
    expect(container.querySelector('.vault-health-banner')).toBeNull()
    expect(fetchMock).toHaveBeenCalled()
  })

  it('renders the healthy banner when status.ok is true', async () => {
    stubFetchByUrl([{ match: '/api/vault/status', value: jsonOk({ ok: true }) }])
    render(<VaultHealthBanner />)
    const banner = await screen.findByText('\u2713 Vault healthy')
    expect(banner).toBeInTheDocument()
    expect(banner.closest('.vault-health-banner')).toHaveClass('vault-health-ok')
  })

  it('renders the degraded banner when the status cannot be fetched', async () => {
    stubFetchByUrl([
      {
        match: '/api/vault/status',
        value: () => {
          throw new Error('network down')
        },
      },
    ])
    render(<VaultHealthBanner />)
    const banner = await screen.findByText(/Vault status unavailable/)
    expect(banner).toHaveClass('vault-health-degraded')
  })

  it('renders the issues list when status.ok is false', async () => {
    stubFetchByUrl([
      {
        match: '/api/vault/status',
        value: jsonOk({
          ok: false,
          issues: [
            {
              severity: 'critical',
              file: '/vault/config.json',
              message: 'checksum mismatch',
              action: 're-run integrity check',
            },
            { severity: 'odd', message: 'only message' },
            { file: '/vault/missing.json' },
          ],
        }),
      },
    ])
    render(<VaultHealthBanner />)
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveClass('vault-health-error')
    expect(screen.getByText('\u26a0 Vault integrity issues detected')).toBeInTheDocument()
    expect(screen.getByText('critical')).toBeInTheDocument()
    expect(screen.getByText('/vault/config.json')).toBeInTheDocument()
    expect(screen.getByText('checksum mismatch')).toBeInTheDocument()
    expect(screen.getByText('\u2192 re-run integrity check')).toBeInTheDocument()
    // Unknown severity falls back to the medium class.
    expect(alert.querySelector('.vault-health-severity.medium')).toBeInTheDocument()
    // Missing file falls back to "unknown file".
    expect(screen.getByText('unknown file')).toBeInTheDocument()
  })

  it('shows the empty-issues message when unhealthy without issues', async () => {
    stubFetchByUrl([{ match: '/api/vault/status', value: jsonOk({ ok: false }) }])
    render(<VaultHealthBanner />)
    await screen.findByRole('alert')
    expect(screen.getByText('Vault reported unhealthy but no issues were listed.')).toBeInTheDocument()
  })

  it('schedules a 30-second polling interval and clears it on unmount', () => {
    const setIntervalSpy = vi.spyOn(window, 'setInterval')
    const clearIntervalSpy = vi.spyOn(window, 'clearInterval')
    const { unmount } = render(<VaultHealthBanner />)
    expect(setIntervalSpy).toHaveBeenCalled()
    expect(setIntervalSpy.mock.calls[0][1]).toBe(30000)
    unmount()
    expect(clearIntervalSpy).toHaveBeenCalled()
  })
})
