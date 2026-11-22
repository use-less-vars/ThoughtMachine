// @vitest-environment jsdom
/*
 * workspaceStore.unknownEffective.test.js — Contract 5 (docs/CONTRACTS.md).
 *
 * When GET /api/workspace/{id}/effective_permissions cannot be read, the store
 * used to fall back to the PURPOSE PRESET and publish it as `effective` (the
 * column the sidebar labels "Effective (what the agent actually has)"), and
 * fabricate a `ceiling` from PERMISSION_CEILINGS. A failed read is not a
 * preset: the honest consequence is NO permission row at all, so the panel's
 * own empty state renders instead of a permission claim.
 *
 * A failed read has two shapes; the fix must cover both:
 *   - an HTTP error status (jsonErr / 500), and
 *   - a throw (network failure).
 *
 * Each case is asserted at the store level and at the rendered level: the store
 * must carry no row with a defined effective/ceiling value, and a panel fed from
 * that store must show neither the preset's value nor a permission claim.
 */

import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import useWorkspaceStore from '../workspaceStore'
import useSessionTabsStore from '../../sessionTabsStore'
import SessionSidebar from '../../components/SessionSidebar'

function jsonOk(data, status = 200) {
  return { ok: true, status, json: async () => data, text: async () => JSON.stringify(data) }
}

function jsonErr(detail, status = 500) {
  return {
    ok: false,
    status,
    json: async () => ({ detail }),
    text: async () => JSON.stringify({ detail }),
  }
}

function stubFetchByUrl(routes) {
  const fetchMock = vi.fn(async (url, options) => {
    const key = Object.keys(routes)
      .filter((k) => String(url).includes(k))
      .sort((a, b) => b.length - a.length)[0]
    if (!key) return jsonErr(`unexpected url: ${url}`)
    const resp = routes[key]
    return typeof resp === 'function' ? resp(url, options) : resp
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

// 'Code Development' is a real purpose definition with a preset permission map,
// so the pre-fix code has a preset it can publish as `effective`.
const ROUTES = (perms) => ({
  '/api/workspace/list': jsonOk([{ id: 'ws-1', label: 'Code Development', root: '/root' }]),
  '/api/workspace/ws-1/effective_permissions': perms,
  '/api/health/containers': jsonOk({ docker: 'reachable' }),
  '/api/workspace/ws-1/workers': jsonOk([]),
  '/api/workspace/ws-1/containers': jsonOk({ containers: [] }),
  '/api/session/list': jsonOk([]),
})

// The Code Development preset's own values. None of them may appear as an
// effective permission or a ceiling while the read has failed.
const PRESET_VALUES = ['write', 'banned', 'ask', 'read']

function expectNoPresetClaimedAsPermission(permissions) {
  expect(Array.isArray(permissions)).toBe(true)
  // The honest unknown: no rows at all. A row could only exist by claiming a
  // value this store never managed to read.
  expect(permissions).toEqual([])
  for (const p of permissions) {
    expect(p.effective).toBeUndefined()
    expect(p.ceiling).toBeUndefined()
  }
}

// SessionSidebar reads currentWorkspace from the store itself, so rendering it
// after the failed fetch shows exactly what the user would see.
function renderSidebarFromStore() {
  render(<SessionSidebar tools={[]} />)
}

beforeEach(() => {
  localStorage.clear()
  useWorkspaceStore.getState().reset()
  useSessionTabsStore.setState({ byWorkspace: {} })
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('fetchWorkspaceConfig — unreadable effective permissions (Contract 5)', () => {
  it('claims nothing when the endpoint returns HTTP 500', async () => {
    stubFetchByUrl(ROUTES(jsonErr('permissions unavailable', 500)))
    await useWorkspaceStore.getState().fetchWorkspaceConfig('ws-1')

    const st = useWorkspaceStore.getState()
    // Sanity: the panel did load, so "no permissions" is not just "no workspace".
    expect(st.currentWorkspace.id).toBe('ws-1')
    expect(st.currentWorkspace.name).toBe('Code Development')
    expectNoPresetClaimedAsPermission(st.currentWorkspace.permissions)

    renderSidebarFromStore()
    expect(screen.getByText('No permission data for this workspace.')).toBeInTheDocument()
    for (const value of PRESET_VALUES) {
      expect(screen.queryByText(value)).toBeNull()
    }
  })

  it('claims nothing when the fetch throws', async () => {
    stubFetchByUrl(
      ROUTES(() => {
        throw new Error('network down')
      })
    )
    await useWorkspaceStore.getState().fetchWorkspaceConfig('ws-1')

    expectNoPresetClaimedAsPermission(useWorkspaceStore.getState().currentWorkspace.permissions)

    renderSidebarFromStore()
    expect(screen.getByText('No permission data for this workspace.')).toBeInTheDocument()
    for (const value of PRESET_VALUES) {
      expect(screen.queryByText(value)).toBeNull()
    }
  })
})
