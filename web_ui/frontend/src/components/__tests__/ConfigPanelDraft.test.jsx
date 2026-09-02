// @vitest-environment jsdom
/*
 * ConfigPanelDraft.test.jsx — ConfigPanel draft persistence across
 * unmount/remount (tab-switch permission draft loss fix).
 *
 * The draft now lives in the Zustand store (sessionDrafts) so unsaved edits
 * survive tab switches that unmount the panel, and are cleared when an apply
 * succeeds. Renders the REAL ConfigPanel with a stubbed global fetch (same
 * pattern as SessionSidebar.test.jsx).
 */

import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, act } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import ConfigPanel from '../ConfigPanel'
import useStore from '../../store/useStore'

function jsonOk(data, status = 200) {
  return { ok: true, status, json: async () => data, text: async () => JSON.stringify(data) }
}

const DEFAULT_FALLBACK = {
  ok: true,
  status: 200,
  json: async () => ({ tools: [] }),
  text: async () => '',
}

// Routes needed by ConfigPanel's mount fetch (/api/tools) and the WorkspacePanel
// it mounts by default (workers/containers/effective_permissions).
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

function stubBackend() {
  const fetchMock = vi.fn(async (url) => {
    const key = Object.keys(DEFAULT_ROUTES)
      .filter((k) => String(url).includes(k))
      .sort((a, b) => b.length - a.length)[0]
    return DEFAULT_ROUTES[key] || DEFAULT_FALLBACK
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

const BASE_CONFIG = { mode: 'custom', session_permissions: { network: 'banned' } }
const APPLIED_CONFIG = { ...BASE_CONFIG, session_permissions: { ...BASE_CONFIG.session_permissions, network: 'ask' } }

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

beforeEach(() => {
  useStore.getState().reset()
  stubBackend()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  useStore.getState().reset()
})

describe('ConfigPanel draft persistence (tab-switch permission draft loss fix)', () => {
  it('writes edits to the store draft and marks the panel dirty', () => {
    renderPanel()
    const select = openPermissionsTab()
    expect(select.value).toBe('banned')
    fireEvent.change(select, { target: { value: 'ask' } })
    expect(select.value).toBe('ask')
    expect(screen.getByText('Unsaved changes')).toBeInTheDocument()
    expect(useStore.getState().sessionDrafts['s1'].session_permissions.network).toBe('ask')
  })

  it('keeps the unsaved draft after unmount/remount (tab switch)', () => {
    renderPanel()
    const select = openPermissionsTab()
    fireEvent.change(select, { target: { value: 'ask' } })
    cleanup()
    renderPanel() // remount — store draft restored
    const select2 = openPermissionsTab()
    expect(select2.value).toBe('ask')
    expect(screen.getByText('Unsaved changes')).toBeInTheDocument()
  })

  it('sends the draft on Apply and clears it when the apply succeeds', () => {
    const { sendCommand, rerender } = renderPanel()
    const select = openPermissionsTab()
    fireEvent.change(select, { target: { value: 'ask' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))
    expect(sendCommand).toHaveBeenCalledWith('apply_config', {
      config: expect.objectContaining({
        session_permissions: expect.objectContaining({ network: 'ask' }),
      }),
    })
    // Backend echoes the applied config; SessionTab then re-renders ConfigPanel
    // with the new config prop.
    act(() => {
      useStore.getState().receiveConfigChanged('s1', {
        config: APPLIED_CONFIG,
        permissions: { network: 'ask' },
      })
    })
    rerender(panelElement(APPLIED_CONFIG, sendCommand))
    expect(useStore.getState().sessionDrafts['s1']).toBeUndefined()
    expect(screen.queryByText('Unsaved changes')).not.toBeInTheDocument()
  })

  it('store actions round-trip session drafts (setSessionDraft/clearSessionDraft)', () => {
    const draft = { mode: 'custom', session_permissions: { network: 'ask' } }
    act(() => { useStore.getState().setSessionDraft('x', draft) })
    expect(useStore.getState().sessionDrafts['x']).toEqual(draft)
    act(() => { useStore.getState().clearSessionDraft('x') })
    expect(useStore.getState().sessionDrafts['x']).toBeUndefined()
  })
})
