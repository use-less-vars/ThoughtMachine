// @vitest-environment jsdom
// --- WorkspaceDetailPage.unknownPosture.test.jsx ---
// Contract 5 (docs/CONTRACTS.md): a permission surface the UI could NOT read
// must render as UNKNOWN. It must never be reported as a security claim.
//
// Two ways the surface is unreadable on this page:
//   1. the summary arrives but carries no boolean allow_host_resources (absent
//      or null) -- the page used to fall through to the *forbidden* posture
//      string with the green "safe" colour, i.e. it turned "unknown" into
//      "safe";
//   2. the summary fetch itself fails (HTTP 500) -- the page must show the
//      failure and make NO security claim at all.

import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import WorkspaceDetailPage from './WorkspaceDetailPage'

const WORKSPACE_ID = 'ws-1'

const POSTURE_SAFE =
  'Host resources are forbidden. This workspace can withstand interactions with untrusted content.'
const POSTURE_HOST =
  'Host resources allowed. This workspace is suitable only for trusted, supervised work.'
const POSTURE_UNKNOWN =
  'Workspace permissions could not be read. The security posture of this workspace is unknown; do not treat it as safe.'
const HOST_EXEC_UNKNOWN = 'Host execution state unknown'

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

const DEFAULT_FALLBACK = {
  ok: true,
  status: 200,
  json: async () => ({}),
  text: async () => '',
}

function stubFetchByUrl(routes, defaultResponse = DEFAULT_FALLBACK) {
  const fetchMock = vi.fn(async (url, init) => {
    const key = Object.keys(routes)
      .filter((k) => String(url).includes(k))
      .sort((a, b) => b.length - a.length)[0]
    if (!key) return defaultResponse
    const resp = routes[key]
    return typeof resp === 'function' ? resp(url, init) : resp
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

// Minimal summary payload: only what the Overview tab reads. `allow_host_resources`
// is deliberately NOT part of this fixture -- test 1 adds/omits it explicitly.
function makeSummary(overrides = {}) {
  return {
    workspace_id: WORKSPACE_ID,
    label: 'Research Sandbox',
    root_path: '/home/jojo/workspaces/research',
    permissions: {
      git: 'read',
      filesystem: 'read',
      container: false,
      network: 'ask',
      mcp: 'banned',
      host_bash: 'banned',
    },
    capabilities: ['docker'],
    dockerfile: { path: null, content: '' },
    active_workers: [],
    active_sessions: [],
    containers_in_use: [],
    containers_listed: 0,
    tools: [],
    resource_catalog: [],
    ...overrides,
  }
}

const SUMMARY_KEY = `/api/workspace/${WORKSPACE_ID}/summary`

function routesFor(summary) {
  return {
    [SUMMARY_KEY]: () => jsonOk(summary),
    '/api/tools': jsonOk({ tools: [] }),
  }
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

describe('WorkspaceDetailPage — unreadable permission surface (Contract 5)', () => {
  it('renders UNKNOWN, not the forbidden-string, when allow_host_resources is absent', async () => {
    stubFetchByUrl(routesFor(makeSummary()))
    render(<WorkspaceDetailPage workspaceId={WORKSPACE_ID} />)

    await screen.findByText('Research Sandbox')

    // The lie: an absent field used to render as "forbidden" + green "safe".
    expect(screen.queryByText(POSTURE_SAFE)).toBeNull()
    expect(screen.queryByText(POSTURE_HOST)).toBeNull()
    expect(screen.queryByText('Host execution disabled')).toBeNull()
    expect(screen.queryByText('Host execution enabled')).toBeNull()
    expect(document.querySelector('.wdp-posture-safe')).toBeNull()
    expect(document.querySelector('.wdp-posture-host')).toBeNull()

    const posture = screen.getByText(POSTURE_UNKNOWN)
    expect(posture).toBeInTheDocument()
    expect(posture.className).toContain('wdp-posture-unknown')
    expect(screen.getByText(HOST_EXEC_UNKNOWN)).toBeInTheDocument()
  })

  it('renders UNKNOWN, not the forbidden-string, when allow_host_resources is null', async () => {
    stubFetchByUrl(routesFor(makeSummary({ allow_host_resources: null })))
    render(<WorkspaceDetailPage workspaceId={WORKSPACE_ID} />)

    await screen.findByText('Research Sandbox')

    expect(screen.queryByText(POSTURE_SAFE)).toBeNull()
    expect(screen.queryByText(POSTURE_HOST)).toBeNull()
    expect(screen.getByText(POSTURE_UNKNOWN)).toBeInTheDocument()
  })

  it('makes no security claim at all when the summary fetch fails', async () => {
    stubFetchByUrl({ [SUMMARY_KEY]: jsonErr('summary unavailable', 500) })
    render(<WorkspaceDetailPage workspaceId={WORKSPACE_ID} />)

    await screen.findByText('summary unavailable')

    expect(screen.queryByText(POSTURE_SAFE)).toBeNull()
    expect(screen.queryByText(POSTURE_HOST)).toBeNull()
    expect(document.querySelector('.wdp-posture-safe')).toBeNull()
    expect(document.querySelector('.wdp-posture-host')).toBeNull()
  })
})
