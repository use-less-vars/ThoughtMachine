/*
 * sessionLookup.test.js — unit coverage for the tri-state deep-link
 * membership probe.
 *
 * The contract that matters to callers: `true` / `false` are only ever
 * returned when the backend actually answered with a session ARRAY. Every other
 * outcome (missing args, transport throw, non-OK status, a non-array payload) is
 * `null` = "undecidable", because App fails OPEN on null and must never turn an
 * inconclusive probe into a refusal.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { isSessionInWorkspace } from '../sessionLookup'

const WS = 'ws-test-1'
const LIST_URL = `/api/session/list?workspace_id=${WS}`

function jsonOk(data, status = 200) {
  return { ok: true, status, json: async () => data, text: async () => JSON.stringify(data) }
}

function jsonNotOk(status = 500) {
  return { ok: false, status, json: async () => ({}), text: async () => '' }
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('isSessionInWorkspace', () => {
  it('returns true when the session is listed in that workspace', async () => {
    const fetchMock = vi.fn(async () => jsonOk([{ session_id: 'sess-1' }, { session_id: 'sess-2' }]))
    vi.stubGlobal('fetch', fetchMock)
    await expect(isSessionInWorkspace(WS, 'sess-2')).resolves.toBe(true)
    // The scope is the workspace, not the bare endpoint.
    expect(fetchMock).toHaveBeenCalledWith(LIST_URL)
  })

  it('returns false when the listing answered and the session is not in it', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonOk([{ session_id: 'sess-2' }])))
    await expect(isSessionInWorkspace(WS, 'sess-1')).resolves.toBe(false)
  })

  it('returns false (not null) for an empty listing — that is an answer, not a non-answer', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonOk([])))
    await expect(isSessionInWorkspace(WS, 'sess-1')).resolves.toBe(false)
  })

  it('returns null when the response is not ok', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonNotOk(503)))
    await expect(isSessionInWorkspace(WS, 'sess-1')).resolves.toBeNull()
  })

  it('returns null when fetch rejects (network failure) instead of throwing', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new Error('network down')
    }))
    await expect(isSessionInWorkspace(WS, 'sess-1')).resolves.toBeNull()
  })

  it('returns null when the payload is not a session array', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonOk({ sessions: [{ session_id: 'sess-1' }] })))
    await expect(isSessionInWorkspace(WS, 'sess-1')).resolves.toBeNull()
  })

  it('returns null without calling fetch when either argument is missing', async () => {
    const fetchMock = vi.fn(async () => jsonOk([{ session_id: 'sess-1' }]))
    vi.stubGlobal('fetch', fetchMock)
    await expect(isSessionInWorkspace('', 'sess-1')).resolves.toBeNull()
    await expect(isSessionInWorkspace(null, 'sess-1')).resolves.toBeNull()
    await expect(isSessionInWorkspace(WS, '')).resolves.toBeNull()
    await expect(isSessionInWorkspace(WS, undefined)).resolves.toBeNull()
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
