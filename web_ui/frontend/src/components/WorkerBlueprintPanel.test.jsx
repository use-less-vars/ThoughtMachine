// @vitest-environment jsdom
// --- WorkerBlueprintPanel.test.jsx ---
// RED tests for feature C.2 (workspace worker-blueprint editor).
// Component under test: ./WorkerBlueprintPanel.jsx (default export).
//
// Contract pinned by these tests:
//   * on mount inline-fetches GET /api/workspace/${workspaceId}/workers/blueprints
//   * blueprint object keys mirror the backend WorkerDefinition schema
//     (name, description, system_prompt, tools, permission_footprint,
//      warning_threshold_tokens, critical_threshold_tokens, ...)
//   * root: worker-blueprint-panel; list: blueprint-list;
//     row: blueprint-row-{name}; empty state: blueprint-empty
//   * selecting a row opens blueprint-edit-form with per-field inputs
//     field-{field_name} (name, description, system_prompt,
//     warning_threshold_tokens, critical_threshold_tokens, ...)
//   * save control: blueprint-save -> PATCH
//     /api/workspace/${workspaceId}/workers/blueprints/{name}
//   * validation/API failure -> blueprint-error (visible, non-empty)
//   * restart-to-apply-hint explains edits apply to FUTURE worker spawns
//
// The fetch boundary IS the boundary: we stub global fetch and never mock any
// component internal or helper.

import React from 'react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import WorkerBlueprintPanel from './WorkerBlueprintPanel'

const WORKSPACE_ID = 'ws-1'
const LIST_URL = `/api/workspace/${WORKSPACE_ID}/workers/blueprints`

const ALPHA = {
  name: 'alpha',
  description: 'Alpha worker blueprint',
  system_prompt: 'You are the alpha worker.',
  tools: [],
  permission_footprint: {},
  warning_threshold_tokens: 65000,
  critical_threshold_tokens: 80000,
}

const BETA = {
  name: 'beta',
  description: 'Beta worker blueprint',
  system_prompt: 'You are the beta worker.',
  tools: [],
  permission_footprint: {},
  warning_threshold_tokens: 11111,
  critical_threshold_tokens: 22222,
}

function jsonResponse(status, payload) {
  return { ok: status < 400, status, json: async () => payload }
}

// Stub global fetch with a handler: ({ url, method, body }) -> response.
function stubFetch(handler) {
  const fetchMock = vi.fn(async (url, init = {}) => {
    const method = String(init.method || 'GET').toUpperCase()
    const body = init.body ? JSON.parse(init.body) : undefined
    return handler({ url: String(url), method, body })
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('WorkerBlueprintPanel (C.2 workspace worker-blueprint editor)', () => {
  // ── T6 ──────────────────────────────────────────────────────────────────
  it('T6 renders blueprint rows with threshold summary and an empty state', async () => {
    stubFetch(({ method, url }) => {
      if (method === 'GET' && url.includes(LIST_URL)) {
        return jsonResponse(200, [ALPHA, BETA])
      }
      return jsonResponse(200, {})
    })

    const { unmount } = render(<WorkerBlueprintPanel workspaceId={WORKSPACE_ID} />)

    expect(await screen.findByTestId('worker-blueprint-panel')).toBeInTheDocument()
    expect(await screen.findByTestId('blueprint-list')).toBeInTheDocument()
    expect(await screen.findByTestId('blueprint-row-alpha')).toBeInTheDocument()
    expect(screen.getByTestId('blueprint-row-beta')).toBeInTheDocument()

    // Threshold summary visible on each row.
    expect(screen.getByTestId('blueprint-row-alpha')).toHaveTextContent('65000')
    expect(screen.getByTestId('blueprint-row-beta')).toHaveTextContent('11111')

    unmount()

    // Empty workspace -> empty state instead of rows.
    stubFetch(({ method, url }) => {
      if (method === 'GET' && url.includes(LIST_URL)) {
        return jsonResponse(200, [])
      }
      return jsonResponse(200, {})
    })
    render(<WorkerBlueprintPanel workspaceId={WORKSPACE_ID} />)

    expect(await screen.findByTestId('blueprint-empty')).toBeInTheDocument()
    expect(screen.queryByTestId('blueprint-row-alpha')).toBeNull()
  })

  // ── T7 ──────────────────────────────────────────────────────────────────
  it('T7 loads values, PATCHes edited values on save, and surfaces a validation error', async () => {
    const calls = []
    stubFetch(({ url, method, body }) => {
      calls.push({ url, method, body })
      if (method === 'PATCH') {
        return jsonResponse(200, { ...ALPHA, ...body })
      }
      if (method === 'GET' && url.includes(LIST_URL)) {
        return jsonResponse(200, [ALPHA, BETA])
      }
      return jsonResponse(200, {})
    })

    const { unmount } = render(<WorkerBlueprintPanel workspaceId={WORKSPACE_ID} />)
    fireEvent.click(await screen.findByTestId('blueprint-row-alpha'))

    expect(await screen.findByTestId('blueprint-edit-form')).toBeInTheDocument()
    expect(screen.getByTestId('field-name')).toHaveValue(ALPHA.name)
    expect(screen.getByTestId('field-description')).toHaveValue(ALPHA.description)
    expect(screen.getByTestId('field-system_prompt')).toHaveValue(ALPHA.system_prompt)

    fireEvent.change(screen.getByTestId('field-warning_threshold_tokens'), {
      target: { value: '12345' },
    })
    fireEvent.click(screen.getByTestId('blueprint-save'))

    await waitFor(() => {
      expect(calls.find((c) => c.method === 'PATCH')).toBeTruthy()
    })
    const patch = calls.find((c) => c.method === 'PATCH')
    expect(patch.url).toContain(`/workers/blueprints/alpha`)
    expect(patch.body.warning_threshold_tokens).toBe(12345)
    unmount()

    // A 422 from the API must surface a visible, non-empty error.
    stubFetch(({ url, method }) => {
      if (method === 'PATCH') {
        return jsonResponse(422, { detail: 'warning_threshold_tokens must be >= 0' })
      }
      if (method === 'GET' && url.includes(LIST_URL)) {
        return jsonResponse(200, [ALPHA])
      }
      return jsonResponse(200, {})
    })

    render(<WorkerBlueprintPanel workspaceId={WORKSPACE_ID} />)
    fireEvent.click(await screen.findByTestId('blueprint-row-alpha'))
    fireEvent.change(await screen.findByTestId('field-warning_threshold_tokens'), {
      target: { value: '-5' },
    })
    fireEvent.click(screen.getByTestId('blueprint-save'))

    const err = await screen.findByTestId('blueprint-error')
    expect(err).toBeInTheDocument()
    expect(err.textContent.trim()).not.toBe('')
  })

  // ── T8 ──────────────────────────────────────────────────────────────────
  it('T8 shows a restart-to-apply hint describing future worker spawns', async () => {
    stubFetch(({ url, method }) => {
      if (method === 'GET' && url.includes(LIST_URL)) {
        return jsonResponse(200, [ALPHA])
      }
      return jsonResponse(200, {})
    })

    render(<WorkerBlueprintPanel workspaceId={WORKSPACE_ID} />)
    fireEvent.click(await screen.findByTestId('blueprint-row-alpha'))

    const hint = await screen.findByTestId('restart-to-apply-hint')
    expect(hint).toBeInTheDocument()
    expect(hint).toHaveTextContent(/future/i)
    expect(hint).toHaveTextContent(/worker/i)
  })
})
