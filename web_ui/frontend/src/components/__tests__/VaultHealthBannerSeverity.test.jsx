// @vitest-environment jsdom
/*
 * VaultHealthBannerSeverity.test.jsx — severity badge mapping (legacy
 * error/warning labels map onto the existing critical/medium classes) and the
 * empty-issues fallback copy. Complements VaultHealthBanner.test.jsx.
 */
import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import VaultHealthBanner from '../VaultHealthBanner'

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

beforeEach(() => {
  localStorage.clear()
  window.location.hash = ''
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('VaultHealthBanner \u2014 severity mapping and fallback', () => {
  it('maps error severity onto the red critical class', async () => {
    stubFetchByUrl({
      '/api/vault/status': jsonOk({
        ok: false,
        issues: [
          {
            severity: 'error',
            file: 'config.json',
            message: 'corrupt',
            action: 're-run integrity check',
          },
        ],
      }),
    })
    render(<VaultHealthBanner />)
    const alert = await screen.findByRole('alert')
    expect(alert.querySelector('.vault-health-severity.critical')).toBeInTheDocument()
    expect(alert.querySelector('.vault-health-severity.error')).not.toBeInTheDocument()
  })

  it('maps warning severity onto the amber medium class', async () => {
    stubFetchByUrl({
      '/api/vault/status': jsonOk({
        ok: false,
        issues: [{ severity: 'warning', file: 'config.json', message: 'stale' }],
      }),
    })
    render(<VaultHealthBanner />)
    const alert = await screen.findByRole('alert')
    expect(alert.querySelector('.vault-health-severity.medium')).toBeInTheDocument()
    expect(alert.querySelector('.vault-health-severity.warning')).not.toBeInTheDocument()
  })

  it('shows the see-logs fallback plus the legacy message when issues are missing', async () => {
    stubFetchByUrl({ '/api/vault/status': jsonOk({ ok: false }) })
    render(<VaultHealthBanner />)
    await screen.findByRole('alert')
    expect(screen.getByText('Vault unhealthy \u2014 see logs.')).toBeInTheDocument()
    expect(
      screen.getByText('Vault reported unhealthy but no issues were listed.')
    ).toBeInTheDocument()
  })
})
