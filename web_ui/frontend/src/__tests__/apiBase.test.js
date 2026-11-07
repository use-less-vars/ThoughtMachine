// @vitest-environment jsdom
/*
 * apiBase.test.js — Wave 3a: ONE shared relative-path helper for API/WS URLs.
 *
 * Several components previously built absolute origins inline
 *   http://${window.location.hostname}:${port}    and
 *   ws://${window.location.hostname}:${port}/ws
 * apiBase.js collapses every one of those into a single relative, same-origin
 * path:
 *   - apiUrl(path) -> the path unchanged (no scheme, no //host) so requests hit
 *                     the page's own origin; the Vite dev proxy and the
 *                     production reverse proxy both forward /api and /ws
 *                     (see vite.config.js server.proxy).
 *   - wsUrl()      -> ws://<window.location.host>/ws  (wss: under https).
 *
 * RED first: this file imports ./apiBase, which does not exist yet, so the run
 * fails on import — the intended first (red) failure.
 *
 * NOTE: vite.config.js has NO `test` block (no test.include / environment);
 * vitest uses its default include (**\/*.test.*) and the jsdom environment is
 * selected per-file via the `// @vitest-environment jsdom` docblock above,
 * matching every other test in this repo.
 */
import { describe, it, expect } from 'vitest'
import { apiUrl, wsUrl } from '../apiBase'

describe('apiBase — shared relative-path helper', () => {
  it('apiUrl(path) returns a same-origin relative path (no scheme, no host)', () => {
    const url = apiUrl('/api/x')
    expect(url).toBe('/api/x')
    expect(url).not.toMatch(/^[a-z]+:\/\//i) // no http:// / ws:// scheme
    expect(url).not.toContain('//') // no //hostname
    expect(url).not.toContain('localhost')
  })

  it('apiUrl leaves nested paths + query strings untouched', () => {
    expect(apiUrl('/api/session/abc/permissions')).toBe('/api/session/abc/permissions')
    expect(apiUrl('/api/logging/config?x=1')).toBe('/api/logging/config?x=1')
  })

  it('wsUrl() targets window.location.host with the ws scheme (jsdom default is http:)', () => {
    expect(wsUrl()).toBe(`ws://${window.location.host}/ws`)
  })

  it('wsUrl() uses wss: when the page is served over https', () => {
    const original = window.location
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { protocol: 'https:', host: 'app.example.com' },
    })
    try {
      expect(wsUrl()).toBe('wss://app.example.com/ws')
    } finally {
      Object.defineProperty(window, 'location', { configurable: true, value: original })
    }
  })
})
