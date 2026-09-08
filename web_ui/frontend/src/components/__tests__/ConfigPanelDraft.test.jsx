// @vitest-environment jsdom
/*
 * ConfigPanelDraft.test.jsx — ConfigPanel draft persistence against the
 * disk-pure permissions flow.
 *
 * Permissions no longer live in the draft: the Permissions tab is REST-driven
 * (GET/PUT /api/session/{id}/permissions, see the session-permission route in
 * stubBackend) and edits stay TAB-LOCAL. The store draft (sessionDrafts) only
 * carries the 13 non-permission config keys, so it survives tab switches that
 * unmount the panel and is cleared when an apply succeeds. Renders the REAL
 * ConfigPanel with a stubbed global fetch (same pattern as
 * ConfigPanel.test.jsx / SessionSidebar.test.jsx).
 */

import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, act, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import ConfigPanel from '../ConfigPanel'
import useStore from '../../store/useStore'

function jsonOk(data, status = 200) {
  return { ok: true, status, json: async () => data, text: async () => JSON.stringify(data) }
}

function jsonError(status, detail) {
  return { ok: false, status, json: async () => ({ detail }), text: async () => '' }
}

const DEFAULT_FALLBACK = {
  ok: true,
  status: 200,
  json: async () => ({ tools: [] }),
  text: async () => '',
}

// Routes needed by ConfigPanel's mount fetch (/api/tools), the WorkspacePanel
// it mounts by default (workers/containers/effective_permissions) and the
// workspace list.
const DEFAULT_ROUTES = {
  '/api/tools': jsonOk({ tools: [] }),
  '/api/health/containers': jsonOk({ docker: 'reachable' }),
  '/api/workspace/ws-1/workers': jsonOk([]),
  '/api/workspace/ws-1/containers': jsonOk({ containers: [] }),
  '/api/workspace/ws-1/effective_permissions': jsonOk({
    effective_permissions: { filesystem: 'read', network: 'banned', git: 'read', system: 'read', execution: 'banned', container: true },
  }),
  '/api/workspace/list': jsonOk([{ id: 'ws-1', label: 'Code Development', root: '/root' }]),
}

// Raw grant map the backend stores for session s1. Canonical safe-default grant
// map — useStore's legacy PERMISSION_DEFAULTS still carries system/execution and
// lacks mcp/host_bash, so it cannot describe the session profile.
const RAW_PROFILE = {
  filesystem: 'read',
  network: 'banned',
  container: false,
  git: 'read',
  mcp: 'banned',
  host_bash: 'banned',
}

// Error body the stub returns for refused permission PUTs (mirrors the real
// backend envelope: { detail: { errors: [...] } } — joined with '; ' by
// handleApply before it surfaces as 'Failed to save permissions: <msg>').
const PUT_422_ERRORS = [
  'container permission rejected by workspace policy',
  'execution grant locked to read-only',
]

const PERMISSIONS_RESPONSE = (raw = RAW_PROFILE) => ({
  raw,
  effective: { ...raw },
  resolved_at: '2025-06-01T12:00:00Z',
})

function stubBackend() {
  const fetchMock = vi.fn(async (url, options) => {
    const s = String(url)
    // Disk-pure session permission endpoint: GET loads the profile,
    // PUT persists the edited raw map and echoes it back.
    if (s.includes('/api/session/s1/permissions')) {
      if (options && options.method === 'PUT') {
        const sent = JSON.parse(options.body)
        // Deterministic refusal sentinel: the container grant is locked by
        // workspace policy, so PUTs carrying container:true fail with HTTP
        // 422. The default profile (RAW_PROFILE) and test 4's successful PUT
        // both carry container:false, so only this test hits the refusal.
        if (sent.container === true) {
          return jsonError(422, { errors: PUT_422_ERRORS })
        }
        return jsonOk(PERMISSIONS_RESPONSE(sent))
      }
      return jsonOk(PERMISSIONS_RESPONSE())
    }
    const key = Object.keys(DEFAULT_ROUTES)
      .filter((k) => s.includes(k))
      .sort((a, b) => b.length - a.length)[0]
    return DEFAULT_ROUTES[key] || DEFAULT_FALLBACK
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

// The display-only fallback (network 'write', from config.session_permissions)
// intentionally differs from the REST profile the GET returns (network
// 'banned'), so tests can wait for the REST load to land before editing.
const BASE_CONFIG = { mode: 'custom', session_permissions: { network: 'write' } }
const APPLIED_CONFIG = { mode: 'custom', session_permissions: { network: 'ask' } }

function panelElement(config, sendCommand) {
  return (
    <ConfigPanel
      config={config}
      sendCommand={sendCommand}
      providers={[]}
      availableTools={[]}
      wsConnected
      workspaceId="ws-1"
      sessionId="s1"
    />
  )
}

function renderPanel(config = BASE_CONFIG) {
  const sendCommand = vi.fn()
  const utils = render(panelElement(config, sendCommand))
  return { sendCommand, ...utils }
}

// Click the Permissions tab; return the Network <select> (the label's wrapping
// div contains exactly one select).
function openPermissionsTab() {
  fireEvent.click(screen.getByRole('button', { name: 'Permissions' }))
  const networkLabel = screen.getByText('Network')
  return networkLabel.closest('div').querySelector('select')
}

// Open the Permissions tab and wait until the session GET has resolved:
// permission edits are a no-op until a REST source is loaded, and the loaded
// select shows RAW_PROFILE.network ('banned'), not the 'write' fallback.
async function openPermissionsTabLoaded() {
  const select = openPermissionsTab()
  await waitFor(() => expect(select.value).toBe(RAW_PROFILE.network))
  return select
}

beforeEach(() => {
  useStore.getState().reset()
  stubBackend()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  useStore.getState().reset()
})

describe('ConfigPanel draft persistence (disk-pure permissions flow)', () => {
  it('keeps permission edits tab-local, marks the panel dirty, and never writes them to the store draft', async () => {
    renderPanel()
    const select = await openPermissionsTabLoaded()
    fireEvent.change(select, { target: { value: 'ask' } })
    expect(select.value).toBe('ask')
    // Permissions-tab dirty label (the draft itself is untouched, so the
    // generic "Unsaved changes" label must NOT appear).
    expect(screen.getByText('Unsaved permission changes')).toBeInTheDocument()
    expect(screen.queryByText('Unsaved changes')).not.toBeInTheDocument()
    // Permission edits never enter the store draft.
    expect(useStore.getState().sessionDrafts['s1']).toBeUndefined()
  })

  it('keeps draft (non-permission) edits in the store across unmount/remount', () => {
    const { container } = renderPanel()
    fireEvent.click(screen.getByRole('button', { name: 'General' }))
    const temperature = container.querySelector('input[type="range"]')
    expect(temperature.value).toBe('0.7')
    fireEvent.change(temperature, { target: { value: '0.9' } })
    expect(screen.getByText('Unsaved changes')).toBeInTheDocument()
    const stored = useStore.getState().sessionDrafts['s1']
    expect(stored.temperature).toBe(0.9)
    // Drafts are permission-free by construction.
    expect(stored.session_permissions).toBeUndefined()

    cleanup()
    const { container: container2 } = renderPanel()
    fireEvent.click(screen.getByRole('button', { name: 'General' }))
    const temperature2 = container2.querySelector('input[type="range"]')
    expect(temperature2.value).toBe('0.9')
    expect(screen.getByText('Unsaved changes')).toBeInTheDocument()
  })

  it('drops tab-local permission edits on unmount — the remounted panel re-fetches the persisted profile', async () => {
    renderPanel()
    const select = await openPermissionsTabLoaded()
    fireEvent.change(select, { target: { value: 'ask' } })
    expect(screen.getByText('Unsaved permission changes')).toBeInTheDocument()

    cleanup()
    renderPanel()
    const select2 = await openPermissionsTabLoaded()
    // The server profile (network 'banned') is re-fetched from the GET; the
    // lost tab-local 'ask' never entered the store draft.
    expect(select2.value).toBe(RAW_PROFILE.network)
    expect(screen.queryByText('Unsaved permission changes')).not.toBeInTheDocument()
    expect(screen.queryByText('Unsaved changes')).not.toBeInTheDocument()
  })

  it('applies a permission-only change immediately via the session PUT — no apply_config, never queued', async () => {
    const fetchMock = globalThis.fetch
    const { sendCommand } = renderPanel()
    const select = await openPermissionsTabLoaded()
    fireEvent.change(select, { target: { value: 'ask' } })
    expect(screen.getByText('Unsaved permission changes')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))

    // The permission path must never surface the queued state: no apply_config
    // is sent, so no config_queued ACK can ever arrive — assert the absence of
    // the 'Queued — applying when idle…' indicator explicitly (requirement b).
    expect(screen.queryByText('Queued — applying when idle…')).not.toBeInTheDocument()

    // The permission-only apply completes on the PUT success ALONE: Apply
    // re-enables without any config_changed echo (and thus without ever being
    // deferred as 'Queued — applying when idle…' behind a busy controller).
    await waitFor(() => expect(screen.getByRole('button', { name: 'Apply' })).toBeEnabled())

    // Immediate-success feedback — Apply re-enabled on PUT success alone,
    // nothing queued, no stuck 'Applying…' state lingers.
    expect(screen.queryByText('Queued — applying when idle…')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Applying…' })).not.toBeInTheDocument()

    // The PUT carried the full edited raw map...
    const putCall = fetchMock.mock.calls.find(
      ([url, opts]) => String(url).includes('/api/session/s1/permissions') && opts && opts.method === 'PUT'
    )
    expect(putCall).toBeTruthy()
    expect(JSON.parse(putCall[1].body)).toEqual({ ...RAW_PROFILE, network: 'ask' })

    // ...and that is the WHOLE apply: no config-level change is pending, so
    // apply_config is never sent — the save is immediate (REST) and can never
    // enter the apply_config busy-queue.
    expect(sendCommand).not.toHaveBeenCalled()
    expect(sendCommand).not.toHaveBeenCalledWith('apply_config', expect.anything())

    // The PUT echo became the applied baseline: dirty labels cleared, Network
    // shows the persisted value, no draft was created (permissions never live
    // in the draft).
    expect(screen.queryByText('Unsaved permission changes')).not.toBeInTheDocument()
    expect(screen.queryByText('Unsaved changes')).not.toBeInTheDocument()
    expect(select.value).toBe('ask')
    expect(useStore.getState().sessionDrafts['s1']).toBeUndefined()
  })

  it('applies rapid successive permission changes sequentially via the session PUT — no dropped edits', async () => {
    const { sendCommand } = renderPanel()
    const select = await openPermissionsTabLoaded()
    // Deferred PUT stub: every session-permission PUT resolves only when this
    // test resolves it, so request order/bodies are asserted exactly.
    const pendingPuts = []
    const deferredFetch = vi.fn(async (url, options) => {
      const s = String(url)
      if (s.includes('/api/session/s1/permissions') && options && options.method === 'PUT') {
        return new Promise((resolve) => pendingPuts.push({ resolve, body: JSON.parse(options.body) }))
      }
      const key = Object.keys(DEFAULT_ROUTES).filter((k) => s.includes(k)).sort((a, b) => b.length - a.length)[0]
      return DEFAULT_ROUTES[key] || DEFAULT_FALLBACK
    })
    vi.stubGlobal('fetch', deferredFetch)
    // Edit 1 (banned → ask) + Apply → PUT #1 is in flight with the FIRST value.
    fireEvent.change(select, { target: { value: 'ask' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))
    await waitFor(() => expect(pendingPuts).toHaveLength(1))
    expect(pendingPuts[0].body).toEqual({ ...RAW_PROFILE, network: 'ask' })
    // Mid-flight the panel shows the plain applying state — never the queued
    // indicator (no apply_config was sent, so no config_queued ACK can come).
    expect(screen.getByRole('button', { name: 'Applying…' })).toBeDisabled()
    expect(screen.queryByText('Queued — applying when idle…')).not.toBeInTheDocument()
    // Edit 2 lands while PUT #1 is still pending (permission selects stay
    // editable mid-flight). 'outbound' is a valid network level distinct from
    // the persisted 'ask' (and from the 'write' display-only fallback).
    fireEvent.change(select, { target: { value: 'outbound' } })
    expect(select.value).toBe('outbound')
    // Resolve PUT #1 (server persisted 'ask') — the newer 'outbound' edit must
    // NOT be swallowed by the echo: the tab stays on 'outbound' and dirty.
    await act(async () => {
      pendingPuts[0].resolve(jsonOk(PERMISSIONS_RESPONSE({ ...RAW_PROFILE, network: 'ask' })))
    })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Apply' })).toBeEnabled())
    expect(select.value).toBe('outbound')
    expect(screen.getByText('Unsaved permission changes')).toBeInTheDocument()
    expect(screen.queryByText('Queued — applying when idle…')).not.toBeInTheDocument()
    // Clicking Apply again sends PUT #2 with the SECOND value (full map).
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))
    await waitFor(() => expect(pendingPuts).toHaveLength(2))
    expect(pendingPuts[1].body).toEqual({ ...RAW_PROFILE, network: 'outbound' })
    // Resolving PUT #2 leaves the panel clean and matching the last edit.
    await act(async () => {
      pendingPuts[1].resolve(jsonOk(PERMISSIONS_RESPONSE({ ...RAW_PROFILE, network: 'outbound' })))
    })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Apply' })).toBeEnabled())
    expect(select.value).toBe('outbound')
    expect(screen.queryByText('Unsaved permission changes')).not.toBeInTheDocument()
    // Sequential REST applies only — nothing ever routed to the busy-queue.
    expect(sendCommand).not.toHaveBeenCalled()
    expect(sendCommand).not.toHaveBeenCalledWith('apply_config', expect.anything())
  })

  it('PUTs permission edits immediately AND sends a permission-free apply_config when config edits are also pending', async () => {
    const fetchMock = globalThis.fetch
    const { sendCommand, container, rerender } = renderPanel()
    // Permission edit — Permissions tab.
    const select = await openPermissionsTabLoaded()
    fireEvent.change(select, { target: { value: 'ask' } })
    expect(screen.getByText('Unsaved permission changes')).toBeInTheDocument()

    // Config edit — General tab (draft in the store; permissions NOT in it).
    fireEvent.click(screen.getByRole('button', { name: 'General' }))
    const temperature = container.querySelector('input[type="range"]')
    expect(temperature.value).toBe('0.7')
    fireEvent.change(temperature, { target: { value: '0.9' } })
    expect(screen.getByText('Unsaved changes')).toBeInTheDocument()
    expect(useStore.getState().sessionDrafts['s1'].temperature).toBe(0.9)
    expect(useStore.getState().sessionDrafts['s1'].session_permissions).toBeUndefined()

    // With config edits pending the apply still routes through apply_config
    // (only a PERMISSION-ONLY apply skips it) — but the permission PUT fires
    // first and lands immediately regardless of the controller's busy state.
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))
    await waitFor(() => {
      expect(sendCommand).toHaveBeenCalledWith('apply_config', expect.anything())
    })

    const putCall = fetchMock.mock.calls.find(
      ([url, opts]) => String(url).includes('/api/session/s1/permissions') && opts && opts.method === 'PUT'
    )
    expect(putCall).toBeTruthy()
    expect(JSON.parse(putCall[1].body)).toEqual({ ...RAW_PROFILE, network: 'ask' })

    // apply_config carries the config draft only — never session_permissions.
    expect(sendCommand).toHaveBeenCalledWith('apply_config', {
      config: expect.not.objectContaining({ session_permissions: expect.anything() }),
    })
    const applyCall = sendCommand.mock.calls.find(([cmd]) => cmd === 'apply_config')
    expect(applyCall[1].config.mode).toBe('custom')
    expect(applyCall[1].config.temperature).toBe(0.9)

    // Backend echoes the applied config; SessionTab then re-renders ConfigPanel
    // with the new config prop — the pending draft is dropped on success.
    act(() => {
      useStore.getState().receiveConfigChanged('s1', {
        config: APPLIED_CONFIG,
        permissions: { network: 'ask' },
      })
    })
    rerender(panelElement(APPLIED_CONFIG, sendCommand))
    await waitFor(() => {
      expect(useStore.getState().sessionDrafts['s1']).toBeUndefined()
    })
    expect(screen.queryByText('Unsaved changes')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Apply' })).toBeEnabled()

    // The permission PUT echo became the applied baseline for the tab.
    fireEvent.click(screen.getByRole('button', { name: 'Permissions' }))
    const selectAfter = screen.getByText('Network').closest('div').querySelector('select')
    await waitFor(() => expect(selectAfter.value).toBe('ask'))
    expect(screen.queryByText('Unsaved permission changes')).not.toBeInTheDocument()
  })

  it('surfaces a failed permissions PUT (422) and aborts apply_config — Apply re-enabled, no draft write, edit stays dirty', async () => {
    const fetchMock = globalThis.fetch
    const { sendCommand, container } = renderPanel()
    await openPermissionsTabLoaded()
    // Flip the container grant on (RAW_PROFILE.container is false): the stub
    // refuses any PUT carrying container:true with 422, so this edit can never
    // be persisted and handleApply must abort before apply_config.
    const checkbox = container.querySelector('input[type="checkbox"]')
    expect(checkbox.checked).toBe(false)
    fireEvent.click(checkbox)
    expect(checkbox.checked).toBe(true)
    expect(screen.getByText('Unsaved permission changes')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))

    // (1) The PUT failure surfaces with the server's joined detail.errors text.
    const joinedErrors = PUT_422_ERRORS.join('; ')
    let errorSpan
    await waitFor(() => {
      errorSpan = screen.getByText((content, node) => node.tagName === 'SPAN' && content.includes('Failed to save permissions: '))
      expect(errorSpan).toHaveTextContent(`Failed to save permissions: ${joinedErrors}`)
    })
    // The rendered span prefixes the message with the ⚠ icon.
    expect(errorSpan.textContent.replace(/\s+/g, ' ').trim()).toMatch(/^⚠ Failed to save permissions: /)

    // The refusal reached the stub with the edited raw map (container:true).
    const putCall = fetchMock.mock.calls.find(
      ([url, opts]) => String(url).includes('/api/session/s1/permissions') && opts && opts.method === 'PUT'
    )
    expect(putCall).toBeTruthy()
    expect(JSON.parse(putCall[1].body).container).toBe(true)

    // (2) apply_config was never sent — the PUT failure aborts the apply.
    expect(sendCommand).not.toHaveBeenCalledWith('apply_config', expect.anything())
    expect(sendCommand).not.toHaveBeenCalled()

    // (3) Apply is re-enabled after the failure (isApplying cleared).
    expect(screen.getByRole('button', { name: 'Apply' })).toBeEnabled()

    // (4) The failed apply left no draft behind.
    expect(useStore.getState().sessionDrafts['s1']).toBeUndefined()

    // (5) The edit is still tab-local and unpersisted → the dirty label stays.
    expect(screen.getByText('Unsaved permission changes')).toBeInTheDocument()
    expect(container.querySelector('input[type="checkbox"]').checked).toBe(true)
  })

  it('store actions round-trip session drafts (setSessionDraft/clearSessionDraft)', () => {
    // Drafts carry config keys only — never session_permissions.
    const draft = { mode: 'custom', temperature: 0.9, system_prompt: 'Be nice' }
    act(() => { useStore.getState().setSessionDraft('x', draft) })
    expect(useStore.getState().sessionDrafts['x']).toEqual(draft)
    act(() => { useStore.getState().clearSessionDraft('x') })
    expect(useStore.getState().sessionDrafts['x']).toBeUndefined()
  })
})
