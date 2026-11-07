// @vitest-environment jsdom
/*
 * workspaceStore.fetchContainers.test.js
 *
 * Regression test for bug/session-workspace-tab-missing-containers.
 *
 * fetchContainers must issue `GET /api/workspace/{id}/containers` even when the
 * store's entry for `id` has no `root` (and no usable `path`). The backend
 * resolves the workspace root from the registry by id — `workspace_path` is
 * optional (server.py::_resolve_workspace_path falls back to
 * WorkspaceRegistry.get_default().get_workspace(id).root_path) — so a missing
 * store root must NOT suppress the request. The issued URL must carry NO
 * `workspace_path` query param.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import useWorkspaceStore from '../workspaceStore'

function jsonOk(data) {
  return { ok: true, status: 200, json: async () => data, text: async () => JSON.stringify(data) }
}

beforeEach(() => {
  localStorage.clear()
  useWorkspaceStore.getState().reset()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('fetchContainers without a store root', () => {
  it('issues GET /api/workspace/{id}/containers with no workspace_path when the entry has no root', async () => {
    // Entry has an id but NO `root` and NO `path` — exactly the state that
    // made the SessionSidebar containers tab come up empty.
    useWorkspaceStore.setState({
      workspaceList: [{ id: 'ws-1', name: 'WS One' }],
      currentWorkspace: null,
    })

    const fetchMock = vi.fn(async () => jsonOk({ containers: [{ name: 'c1', state: 'running' }] }))
    vi.stubGlobal('fetch', fetchMock)

    await useWorkspaceStore.getState().fetchContainers('ws-1')

    const containerCalls = fetchMock.mock.calls.filter(([url]) =>
      String(url).includes('/api/workspace/ws-1/containers')
    )
    // The request MUST be issued despite the missing root.
    expect(containerCalls.length).toBeGreaterThan(0)

    const url = String(containerCalls[0][0])
    expect(url).toBe('/api/workspace/ws-1/containers')
    expect(url).not.toContain('workspace_path')

    expect(useWorkspaceStore.getState().containerStatus).toEqual({ c1: 'running' })
    expect(useWorkspaceStore.getState().error).toBe('')
  })
})
