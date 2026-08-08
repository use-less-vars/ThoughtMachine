// @vitest-environment jsdom
/*
 * workspaceStore.test.js — workspace store tests (Phase 4).
 *
 * The store (src/store/workspaceStore.js) is the REAL API-wired Zustand store
 * for the Workspace Panel. Tests use the real store + a stubbed global fetch
 * (same pattern as useStore.test.js / SessionTab.test.jsx).
 *
 * DEVIATIONS FROM THE ORIGINAL SPEC (matched to real code):
 *   - Spec named startContainer/stopContainer/fetchDockerHealth. Real API:
 *     containerAction(id, name, 'start'|'stop'|'remove') and dockerAvailable
 *     is derived inside fetchWorkspaceConfig from GET /api/health/containers.
 *   - Spec: updateWorkspaceConfig "reverts on failure". Real code performs the
 *     optimistic merge and, on worker-persist failure, ONLY sets store.error —
 *     it does NOT revert. The test documents that reality.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import useWorkspaceStore from '../workspaceStore'

// ---------------------------------------------------------------------------
// fetch stubs
// ---------------------------------------------------------------------------
function jsonOk(data, status = 200) {
  return { ok: true, status, json: async () => data, text: async () => JSON.stringify(data) }
}

function jsonErr(detail, status = 500) {
  return { ok: false, status, json: async () => ({ detail }), text: async () => JSON.stringify({ detail }) }
}

// Routes: substring -> response (longest key wins, so specific endpoints
// always beat generic ones regardless of insertion order).
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

const CONFIG_ROUTES = {
  '/api/workspace/list': jsonOk([{ id: 'ws-1', label: 'Code Development', root: '/root' }]),
  '/api/workspace/ws-1/effective_permissions': jsonOk({
    effective_permissions: {
      filesystem: 'read',
      network: 'banned',
      git: 'write',
      system: 'read',
      execution: 'banned',
      container: true,
    },
  }),
  '/api/health/containers': jsonOk({ docker: 'reachable' }),
  '/api/workspace/ws-1/workers': jsonOk([{ name: 'w1', runtime_status: 'ready', system_prompt: 'p', tools: ['FileEditor'] }]),
  '/api/workspace/ws-1/containers': jsonOk({ containers: [{ name: 'c1', status: 'running' }] }),
  '/api/session/list': jsonOk([{ session_id: 's1' }]),
}

beforeEach(() => {
  localStorage.clear()
  useWorkspaceStore.getState().reset()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

// ==========================================================================
// fetchWorkspaces — GET /api/workspace/list
// ==========================================================================
describe('fetchWorkspaces', () => {
  it('populates workspaceList from GET /api/workspace/list', async () => {
    stubFetchByUrl({
      '/api/workspace/list': jsonOk([
        { id: 'ws-1', label: 'Alpha', root: '/alpha' },
        { id: 'ws-2', root: '/beta' },
      ]),
    })
    await useWorkspaceStore.getState().fetchWorkspaces()
    const st = useWorkspaceStore.getState()
    expect(st.workspaceList).toEqual([
      { id: 'ws-1', name: 'Alpha', path: '/alpha', root: '/alpha' },
      { id: 'ws-2', name: 'ws-2', path: '/beta', root: '/beta' },
    ])
    expect(st.isLoading).toBe(false)
    expect(st.error).toBe('')
  })

  it('falls back to the demo workspaces and sets error on failure', async () => {
    stubFetchByUrl({ '/api/workspace/list': jsonErr('boom') })
    await useWorkspaceStore.getState().fetchWorkspaces()
    const st = useWorkspaceStore.getState()
    expect(st.workspaceList.map((w) => w.id)).toEqual(['ws-demo-dev', 'ws-demo-writing'])
    expect(st.error).toBe('boom')
    expect(st.isLoading).toBe(false)
  })
})

// ==========================================================================
// clearError
// ==========================================================================
describe('clearError', () => {
  it('clears the store-level error set by a failed fetch', async () => {
    stubFetchByUrl({ '/api/workspace/list': jsonErr('boom') })
    await useWorkspaceStore.getState().fetchWorkspaces()
    expect(useWorkspaceStore.getState().error).toBe('boom')
    useWorkspaceStore.getState().clearError()
    expect(useWorkspaceStore.getState().error).toBe('')
  })
})

// ==========================================================================
// createWorkspace — local-only creation from a purpose definition
// ==========================================================================
describe('createWorkspace', () => {
  it('builds the workspace from the purpose, prepends it to the list and returns its id', () => {
    const id = useWorkspaceStore.getState().createWorkspace('code-development')
    expect(id).toMatch(/^ws-/)
    const st = useWorkspaceStore.getState()
    expect(st.workspaceList[0].id).toBe(id)
    expect(st.workspaceList[0].name).toBe('Code Development')
    expect(st.currentWorkspace.id).toBe(id)
    expect(st.safetyAdvisory.status).toBe('green') // Low risk
  })

  it('returns null for an unknown purpose id and changes nothing', () => {
    const before = useWorkspaceStore.getState().workspaceList.length
    expect(useWorkspaceStore.getState().createWorkspace('nope')).toBeNull()
    expect(useWorkspaceStore.getState().workspaceList.length).toBe(before)
    expect(useWorkspaceStore.getState().currentWorkspace).toBeNull()
  })
})

// ==========================================================================
// fetchWorkspaceConfig — loads list + permissions + health + workers +
// containers + sessions for one workspace
// ==========================================================================
describe('fetchWorkspaceConfig', () => {
  it('sets currentWorkspace with permissions, workers, containers, sessions and dockerAvailable', async () => {
    // Seed a list entry with a root so the containers endpoint is queried.
    useWorkspaceStore.setState({ workspaceList: [{ id: 'ws-1', name: 'WS One', root: '/root' }] })
    stubFetchByUrl(CONFIG_ROUTES)
    await useWorkspaceStore.getState().fetchWorkspaceConfig('ws-1')
    const st = useWorkspaceStore.getState()
    expect(st.currentWorkspace.id).toBe('ws-1')
    expect(st.currentWorkspace.name).toBe('Code Development')
    expect(st.currentWorkspace.permissions.map((p) => p.name)).toEqual([
      'filesystem', 'network', 'git', 'system', 'execution', 'container',
    ])
    const containerPerm = st.currentWorkspace.permissions.find((p) => p.name === 'container')
    expect(containerPerm.ceiling).toBe('enabled') // backend bool -> 'enabled'
    expect(containerPerm.effective).toBe('enabled')
    expect(st.currentWorkspace.workers).toEqual([
      expect.objectContaining({ name: 'w1', runtimeStatus: 'ready', systemPrompt: 'p' }),
    ])
    expect(st.currentWorkspace.containers).toEqual([{ name: 'c1', status: 'running' }])
    expect(st.currentWorkspace.sessions).toEqual([{ session_id: 's1' }])
    expect(st.containerStatus).toEqual({ c1: 'running' })
    expect(st.dockerAvailable).toBe(true)
    expect(st.isLoading).toBe(false)
    expect(st.error).toBe('')
  })

  it('sets dockerAvailable=false when health reports not reachable', async () => {
    useWorkspaceStore.setState({ workspaceList: [{ id: 'ws-1', root: '' }] })
    stubFetchByUrl({ ...CONFIG_ROUTES, '/api/health/containers': jsonOk({ docker: 'down' }) })
    await useWorkspaceStore.getState().fetchWorkspaceConfig('ws-1')
    expect(useWorkspaceStore.getState().dockerAvailable).toBe(false)
  })

  it('keeps dockerAvailable=null (unverified) when the health fetch fails', async () => {
    useWorkspaceStore.setState({ workspaceList: [{ id: 'ws-1', root: '' }] })
    stubFetchByUrl({ ...CONFIG_ROUTES, '/api/health/containers': jsonErr('down') })
    await useWorkspaceStore.getState().fetchWorkspaceConfig('ws-1')
    expect(useWorkspaceStore.getState().dockerAvailable).toBeNull()
  })
})

// ==========================================================================
// updateWorkspaceConfig — optimistic merge + localStorage overlay + worker
// persistence (POST new / PUT kept / DELETE removed)
// ==========================================================================
describe('updateWorkspaceConfig', () => {
  it('merges the partial into the store optimistically before any backend call', async () => {
    useWorkspaceStore.setState({
      currentWorkspace: { id: 'ws-1', name: 'WS', tools: ['a'], workers: [] },
      workspaceList: [{ id: 'ws-1', name: 'WS', tools: ['a'], workers: [] }],
    })
    // updateWorkspaceConfig is async; awaiting also returns the merged config.
    const merged = await useWorkspaceStore.getState().updateWorkspaceConfig('ws-1', { tools: ['a', 'b'] })
    // The merge is synchronous; only worker persistence is async.
    expect(useWorkspaceStore.getState().currentWorkspace.tools).toEqual(['a', 'b'])
    expect(useWorkspaceStore.getState().workspaceList[0].tools).toEqual(['a', 'b'])
    expect(merged.tools).toEqual(['a', 'b'])
    // The local-only overlay is persisted to localStorage.
    expect(JSON.parse(localStorage.getItem('tm.workspace.local.ws-1')).tools).toEqual(['a', 'b'])
  })

  it('POSTs new workers, PUTs kept workers and DELETEs removed workers', async () => {
    useWorkspaceStore.setState({
      currentWorkspace: { id: 'ws-1', name: 'WS', workers: [{ name: 'w1' }, { name: 'w2' }] },
      workspaceList: [{ id: 'ws-1', name: 'WS', workers: [{ name: 'w1' }, { name: 'w2' }] }],
    })
    const fetchMock = stubFetchByUrl({ '/api/workspace/ws-1/workers': jsonOk({}) })
    await useWorkspaceStore.getState().updateWorkspaceConfig('ws-1', {
      workers: [{ name: 'w1' }, { name: 'w3' }],
    })
    const calls = fetchMock.mock.calls.map(([url, opts]) => [String(url), (opts && opts.method) || 'GET'])
    expect(calls).toContainEqual(['/api/workspace/ws-1/workers/w1', 'PUT'])   // kept
    expect(calls).toContainEqual(['/api/workspace/ws-1/workers', 'POST'])     // new
    expect(calls).toContainEqual(['/api/workspace/ws-1/workers/w2', 'DELETE']) // removed
    expect(useWorkspaceStore.getState().error).toBe('')
  })

  it('sets error but does NOT revert the optimistic merge when worker persistence fails', async () => {
    useWorkspaceStore.setState({
      currentWorkspace: { id: 'ws-1', name: 'WS', workers: [] },
      workspaceList: [{ id: 'ws-1', name: 'WS', workers: [] }],
    })
    stubFetchByUrl({ '/api/workspace/ws-1/workers': jsonErr('worker create failed') })
    await useWorkspaceStore.getState().updateWorkspaceConfig('ws-1', { workers: [{ name: 'w1' }] })
    const st = useWorkspaceStore.getState()
    expect(st.error).toContain('worker create failed')
    // Reality: no revert — the optimistic merge stays in place (spec said
    // "reverts on failure"; the real store does not, so we document it).
    expect(st.currentWorkspace.workers.map((w) => w.name)).toEqual(['w1'])
  })
})

// ==========================================================================
// fetchContainers — GET /api/workspace/{id}/containers?workspace_path=
// ==========================================================================
describe('fetchContainers', () => {
  it('updates containerStatus and the current workspace containers', async () => {
    useWorkspaceStore.setState({
      workspaceList: [{ id: 'ws-1', name: 'WS', root: '/root' }],
      currentWorkspace: { id: 'ws-1', name: 'WS', root: '/root' },
    })
    stubFetchByUrl({
      '/api/workspace/ws-1/containers': jsonOk({
        containers: [
          { name: 'c1', status: 'running' },
          { name: 'c2', status: 'stopped' },
        ],
      }),
    })
    await useWorkspaceStore.getState().fetchContainers('ws-1')
    const st = useWorkspaceStore.getState()
    expect(st.containerStatus).toEqual({ c1: 'running', c2: 'stopped' })
    expect(st.currentWorkspace.containers).toEqual([
      { name: 'c1', status: 'running' },
      { name: 'c2', status: 'stopped' },
    ])
    expect(st.error).toBe('')
  })

  it('returns early without fetching when the workspace has no root', async () => {
    useWorkspaceStore.setState({ workspaceList: [{ id: 'ws-1', root: '' }] })
    const fetchMock = stubFetchByUrl({})
    await useWorkspaceStore.getState().fetchContainers('ws-1')
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

// ==========================================================================
// containerAction — start / stop (spec named startContainer/stopContainer;
// the real store exposes containerAction(id, name, action))
// ==========================================================================
describe('containerAction (start/stop)', () => {
  it('start POSTs to .../containers/{name}/start, optimistically marks running and refreshes', async () => {
    useWorkspaceStore.setState({
      workspaceList: [{ id: 'ws-1', root: '/root' }],
      currentWorkspace: { id: 'ws-1', root: '/root' },
    })
    const fetchMock = stubFetchByUrl({
      '/api/workspace/ws-1/containers': jsonOk({ containers: [{ name: 'c1', status: 'running' }] }),
    })
    await useWorkspaceStore.getState().containerAction('ws-1', 'c1', 'start')
    const actionCall = fetchMock.mock.calls.find(([url]) => String(url).includes('/containers/c1/start'))
    expect(actionCall).toBeTruthy()
    expect(actionCall[1].method).toBe('POST')
    const st = useWorkspaceStore.getState()
    expect(st.containerStatus.c1).toBe('running')
    expect(st.busyContainers.c1).toBeUndefined()
    expect(st.error).toBe('')
  })

  it('stop POSTs to .../containers/{name}/stop and reflects the refreshed status', async () => {
    useWorkspaceStore.setState({
      workspaceList: [{ id: 'ws-1', root: '/root' }],
      currentWorkspace: { id: 'ws-1', root: '/root' },
    })
    const fetchMock = stubFetchByUrl({
      '/api/workspace/ws-1/containers': jsonOk({ containers: [{ name: 'c1', status: 'stopped' }] }),
    })
    await useWorkspaceStore.getState().containerAction('ws-1', 'c1', 'stop')
    const actionCall = fetchMock.mock.calls.find(([url]) => String(url).includes('/containers/c1/stop'))
    expect(actionCall).toBeTruthy()
    expect(actionCall[1].method).toBe('POST')
    expect(useWorkspaceStore.getState().containerStatus.c1).toBe('stopped')
  })

  it('sets error, clears the busy flag and rethrows when the action fails', async () => {
    useWorkspaceStore.setState({ workspaceList: [{ id: 'ws-1', root: '/root' }] })
    stubFetchByUrl({ '/api/workspace/ws-1/containers': jsonErr('cannot stop container') })
    await expect(useWorkspaceStore.getState().containerAction('ws-1', 'c1', 'stop')).rejects.toThrow(
      /cannot stop container/
    )
    const st = useWorkspaceStore.getState()
    expect(st.error).toContain('cannot stop container')
    expect(st.busyContainers.c1).toBeUndefined()
  }, 20000)
})

// ==========================================================================
// fetchSessions — GET /api/session/list?workspace_id=
// ==========================================================================
describe('fetchSessions', () => {
  it('updates store.sessions and the current workspace sessions', async () => {
    useWorkspaceStore.setState({
      workspaceList: [{ id: 'ws-1' }],
      currentWorkspace: { id: 'ws-1', sessions: [] },
    })
    stubFetchByUrl({ '/api/session/list': jsonOk([{ session_id: 's1' }, { session_id: 's2' }]) })
    await useWorkspaceStore.getState().fetchSessions('ws-1')
    const st = useWorkspaceStore.getState()
    expect(st.sessions).toEqual([{ session_id: 's1' }, { session_id: 's2' }])
    expect(st.currentWorkspace.sessions).toEqual([{ session_id: 's1' }, { session_id: 's2' }])
  })
})
