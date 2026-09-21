// @vitest-environment jsdom
// --- ContainerLogsViewer.test.jsx ---
// Unit tests for the extracted container-logs viewer. The viewer owns the
// fetch, the loading/error/done state and the single long-log affordance
// (a tail-size select clamped to the backend maximum). Props:
// {workspaceId, containerName, tail?}.

import React from 'react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import ContainerLogsViewer, { CONTAINER_LOGS_MAX_TAIL } from './ContainerLogsViewer'

const WORKSPACE_ID = 'ws-1'
const CONTAINER_NAME = 'research-runner'
const LOGS_URL = `/api/workspace/${WORKSPACE_ID}/containers/${CONTAINER_NAME}/logs`

function stubFetch(handler) {
  const fetchMock = vi.fn(handler)
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

// Faithful logs-route stub: the backend returns a JSON body only
// ({container, success, stdout, stderr, duration}). A real HTTP Response
// exposes that same payload through .text() as its serialized form, so .text()
// yields the JSON string (NOT the bare log text). A viewer that silently
// reverts to response.text() therefore renders the JSON envelope blob, which
// the envelope-marker assertion below catches.
function textOk(text = 'boot\nlistening on :8080\n') {
  const envelope = { container: CONTAINER_NAME, success: true, stdout: text, stderr: '', duration: 0.01 }
  return {
    ok: true,
    status: 200,
    json: async () => envelope,
    text: async () => JSON.stringify(envelope),
  }
}

// Faithful error stub: non-2xx responses carry a JSON {detail} body.
function statusErr(status) {
  return {
    ok: false,
    status,
    json: async () => ({ detail: 'no such container' }),
    text: async () => JSON.stringify({ detail: 'no such container' }),
  }
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('ContainerLogsViewer', () => {
  it('fetches logs with the default tail of 200 and renders them in a pre inside a labelled region', async () => {
    const fetchMock = stubFetch(async () => textOk('boot\nlistening on :8080\n'))
    render(<ContainerLogsViewer workspaceId={WORKSPACE_ID} containerName={CONTAINER_NAME} />)

    const region = screen.getByRole('region', { name: 'Container logs' })
    expect(region).toBeInTheDocument()

    const pre = await screen.findByText(/listening on :8080/)
    expect(pre.tagName).toBe('PRE')
    // The viewer must render the decoded stdout, NOT the raw JSON envelope:
    // a response.text() regression would render the blob and leak these keys.
    expect(pre.textContent).toMatch(/listening on :8080/)
    expect(pre.textContent).not.toMatch(/"container"|"success"|"duration"/)

    const urls = fetchMock.mock.calls.map(([u]) => String(u))
    expect(urls).toEqual([`${LOGS_URL}?tail=200`])
  })

  it('surfaces the exact backend error string when logs fail to load', async () => {
    stubFetch(async () => statusErr(404))
    render(<ContainerLogsViewer workspaceId={WORKSPACE_ID} containerName={CONTAINER_NAME} />)

    expect(await screen.findByText('Failed to load logs (404)')).toBeInTheDocument()
  })

  it('exposes a tail-size select that refetches with the chosen tail', async () => {
    const fetchMock = stubFetch(async () => textOk('line\n'))
    render(<ContainerLogsViewer workspaceId={WORKSPACE_ID} containerName={CONTAINER_NAME} />)
    await screen.findByText(/line/)

    const select = screen.getByRole('combobox', { name: 'Log tail size' })
    expect(Array.from(select.options).map((o) => Number(o.value))).toEqual([
      200, 500, 1000, 2000,
    ])

    fireEvent.change(select, { target: { value: '500' } })

    await waitFor(() => {
      const urls = fetchMock.mock.calls.map(([u]) => String(u))
      expect(urls).toContain(`${LOGS_URL}?tail=500`)
    })
  })

  it('clamps an oversized tail prop to the backend maximum', async () => {
    const fetchMock = stubFetch(async () => textOk('line\n'))
    render(
      <ContainerLogsViewer
        workspaceId={WORKSPACE_ID}
        containerName={CONTAINER_NAME}
        tail={999999}
      />
    )
    await screen.findByText(/line/)

    const urls = fetchMock.mock.calls.map(([u]) => String(u))
    expect(urls).toEqual([`${LOGS_URL}?tail=${CONTAINER_LOGS_MAX_TAIL}`])
  })
})
