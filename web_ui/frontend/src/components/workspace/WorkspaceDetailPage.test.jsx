// @vitest-environment jsdom
// --- WorkspaceDetailPage.test.jsx ---
// Tests for the workspace detail page (Layer 2 core): summary rendering,
// exact security-posture strings, the host-execution toggle + confirm modal,
// permission-ceiling editing, containers / workers / tools tabs, the
// placeholder tabs and the no-dummy-data guarantee.
//
// Fetch is stubbed by substring route (longest key wins) — the same pattern
// used by the other component suites. The summary route returns a fresh copy
// per call so a refetch after Apply reflects server-persisted mutations.

import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, act, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import WorkspaceDetailPage from './WorkspaceDetailPage'

const WORKSPACE_ID = 'ws-1'

const POSTURE_SAFE =
  'Host resources are forbidden. This workspace can withstand interactions with untrusted content.'
const POSTURE_HOST =
  'Host resources allowed. This workspace is suitable only for trusted, supervised work.'
const HOST_ENABLE_WARNING =
  'This allows resources to run on the host. Only enable for trusted, supervised workspaces.'

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

// Faithful logs-route stub: GET .../containers/{name}/logs returns a JSON body
// only ({container, success, stdout, stderr, duration}). A real HTTP Response
// exposes that payload through .text() as its serialized form, so .text()
// yields the JSON string, NOT the bare log text. A viewer that reverts to
// response.text() renders the envelope blob and the marker assertions fail.
function logsOk(stdout, container = 'research-runner') {
  const envelope = { container, success: true, stdout, stderr: '', duration: 0.01 }
  return {
    ok: true,
    status: 200,
    json: async () => envelope,
    text: async () => JSON.stringify(envelope),
  }
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

// Realistic summary fixture matching the backend GET /api/workspace/{id}/summary
// shape: {workspace_id, label, root_path, allow_host_resources, permissions,
// capabilities, dockerfile, worker_templates, active_workers, active_sessions,
// active_containers, tools, resource_catalog}.
function makeSummary(overrides = {}) {
  return {
    workspace_id: WORKSPACE_ID,
    label: 'Research Sandbox',
    root_path: '/home/jojo/workspaces/research',
    allow_host_resources: false,
    permissions: {
      git: 'read',
      filesystem: 'read',
      container: false,
      network: 'ask',
      mcp: 'banned',
      host_bash: 'banned',
    },
    capabilities: ['docker', 'network'],
    dockerfile: {
      path: '/home/jojo/workspaces/research/Dockerfile',
      content: 'FROM python:3.11\n',
    },
    worker_templates: [
      {
        name: 'default-worker',
        description: 'Default worker template',
        tool_classes: ['file_editor', 'docker_code_runner'],
      },
    ],
    active_workers: [
      { worker_name: 'worker-1', instance_id: 7, status: 'running', elapsed: 125.4 },
    ],
    active_sessions: [
      {
        session_id: 'sess-1',
        workspace_id: WORKSPACE_ID,
        name: 'Main',
        mode: 'sandbox',
        started_at: '2026-08-31T10:00:00Z',
      },
    ],
    active_containers: [
      {
        id: 'abc123',
        name: 'research-runner',
        type: 'resource',
        workspace_id: WORKSPACE_ID,
        status: 'running',
      },
    ],
    tools: ['git', 'file_editor'],
    resource_catalog: [
      {
        name: 'git',
        display_name: 'Git',
        description: 'Version control access to repositories.',
        permission_grain_set: ['banned', 'read', 'ask', 'write'],
        execution_mode: 'container',
        container_image: null,
        dockerfile_reference: null,
        tools: ['git_read', 'git_write'],
      },
      {
        name: 'filesystem',
        display_name: 'Filesystem',
        description: 'Read and write access to workspace files.',
        permission_grain_set: ['banned', 'read', 'ask', 'write'],
        execution_mode: 'container',
        container_image: null,
        dockerfile_reference: null,
        tools: ['read_file', 'file_editor', 'apply_edits'],
      },
      {
        name: 'host_bash',
        display_name: 'Host shell',
        description: 'Execute commands directly on the host machine.',
        permission_grain_set: ['banned', 'read', 'ask', 'write'],
        execution_mode: 'host',
        container_image: null,
        dockerfile_reference: null,
        tools: ['host_bash'],
      },
    ],
    ...overrides,
  }
}

// Fresh copy per call so a refetch after Apply returns the mutated state.
function summaryRoute(summary) {
  return () => jsonOk({ ...summary, permissions: { ...summary.permissions } })
}

function routesFor(summary, extra = {}) {
  return {
    [`/api/workspace/${WORKSPACE_ID}/summary`]: summaryRoute(summary),
    '/api/tools': jsonOk({ tools: [] }),
    ...extra,
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

describe('WorkspaceDetailPage', () => {
  it('renders the summary truth: label, workspace id, root path and the safe posture string', async () => {
    stubFetchByUrl(routesFor(makeSummary()))
    render(<WorkspaceDetailPage workspaceId={WORKSPACE_ID} />)

    expect(await screen.findByText('Research Sandbox')).toBeInTheDocument()
    expect(screen.getByText(WORKSPACE_ID)).toBeInTheDocument()
    expect(screen.getByText('/home/jojo/workspaces/research')).toBeInTheDocument()
    expect(screen.getByText(POSTURE_SAFE)).toBeInTheDocument()
    expect(screen.queryByText(POSTURE_HOST)).toBeNull()
    expect(screen.getByText('Host execution disabled')).toBeInTheDocument()
  })

  it('shows the host-allowed posture string when allow_host_resources is true', async () => {
    stubFetchByUrl(routesFor(makeSummary({ allow_host_resources: true })))
    render(<WorkspaceDetailPage workspaceId={WORKSPACE_ID} />)

    await screen.findByText('Research Sandbox')
    expect(screen.getByText(POSTURE_HOST)).toBeInTheDocument()
    expect(screen.queryByText(POSTURE_SAFE)).toBeNull()
    expect(screen.getByText('Host execution enabled')).toBeInTheDocument()
  })

  it('shows a loading state before the summary resolves, then the content', async () => {
    let release
    const gate = new Promise((resolve) => {
      release = resolve
    })
    const summary = makeSummary()
    stubFetchByUrl({
      [`/api/workspace/${WORKSPACE_ID}/summary`]: {
        ok: true,
        status: 200,
        json: async () => {
          await gate
          return summary
        },
        text: async () => JSON.stringify(summary),
      },
    })

    render(<WorkspaceDetailPage workspaceId={WORKSPACE_ID} />)
    expect(screen.getByLabelText('Loading workspace')).toBeInTheDocument()
    expect(screen.queryByText('Research Sandbox')).toBeNull()

    await act(async () => {
      release()
    })
    expect(await screen.findByText('Research Sandbox')).toBeInTheDocument()
    expect(screen.queryByLabelText('Loading workspace')).toBeNull()
  })

  it('shows an error with a Retry button on summary fetch failure and recovers on retry', async () => {
    const routes = {
      [`/api/workspace/${WORKSPACE_ID}/summary`]: jsonErr('Backend exploded', 500),
    }
    const fetchMock = stubFetchByUrl(routes)
    render(<WorkspaceDetailPage workspaceId={WORKSPACE_ID} />)

    expect(await screen.findByText('Backend exploded')).toBeInTheDocument()
    const retry = screen.getByRole('button', { name: 'Retry' })

    routes['/api/workspace/' + WORKSPACE_ID + '/summary'] = jsonOk(makeSummary())
    fireEvent.click(retry)

    expect(await screen.findByText('Research Sandbox')).toBeInTheDocument()
    const summaryCalls = fetchMock.mock.calls.filter(([url]) =>
      String(url).includes('/api/workspace/' + WORKSPACE_ID + '/summary')
    )
    expect(summaryCalls.length).toBeGreaterThanOrEqual(2)
  })

  it('toggles host execution through the confirm modal and PUTs allow_host_resources', async () => {
    const summary = makeSummary()
    const putBodies = []
    stubFetchByUrl(
      routesFor(summary, {
        [`/api/workspace/${WORKSPACE_ID}/permissions`]: (url, init) => {
          putBodies.push(JSON.parse(init.body))
          return jsonOk({ ok: true })
        },
      })
    )
    render(<WorkspaceDetailPage workspaceId={WORKSPACE_ID} />)
    await screen.findByText('Research Sandbox')

    const toggle = screen.getByRole('switch', { name: 'Toggle host resource execution' })
    expect(toggle).toHaveAttribute('aria-checked', 'false')

    fireEvent.click(toggle)
    expect(screen.getByText('Enable host resource execution?')).toBeInTheDocument()
    expect(screen.getByText(HOST_ENABLE_WARNING)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    expect(screen.queryByText('Enable host resource execution?')).toBeNull()
    expect(screen.getByRole('switch', { name: 'Toggle host resource execution' })).toHaveAttribute(
      'aria-checked',
      'true'
    )
    expect(screen.getByText('Unsaved changes')).toBeInTheDocument()

    // The server persisted the change; the refetch must reflect it.
    summary.allow_host_resources = true
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))
    await waitFor(() => expect(putBodies.length).toBe(1))
    expect(putBodies[0]).toEqual({
      permissions: summary.permissions,
      allow_host_resources: true,
    })

    expect(await screen.findByText(POSTURE_HOST)).toBeInTheDocument()
    expect(screen.getByRole('switch', { name: 'Toggle host resource execution' })).toHaveAttribute(
      'aria-checked',
      'true'
    )
    expect(screen.getByText('Host execution enabled')).toBeInTheDocument()
    expect(screen.queryByText('Unsaved changes')).toBeNull()
  })

  it('keeps the host toggle pending and shows an inline error when the permission PUT fails', async () => {
    stubFetchByUrl(
      routesFor(makeSummary(), {
        [`/api/workspace/${WORKSPACE_ID}/permissions`]: jsonErr('Permission denied', 403),
      })
    )
    render(<WorkspaceDetailPage workspaceId={WORKSPACE_ID} />)
    await screen.findByText('Research Sandbox')

    fireEvent.click(screen.getByRole('switch', { name: 'Toggle host resource execution' }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))

    expect(await screen.findByText('Permission denied')).toBeInTheDocument()
    // The toggle keeps the pending value (On) even though the summary still says false.
    expect(screen.getByRole('switch', { name: 'Toggle host resource execution' })).toHaveAttribute(
      'aria-checked',
      'true'
    )
    expect(screen.getByText(POSTURE_SAFE)).toBeInTheDocument()
    expect(screen.getByText('Unsaved changes')).toBeInTheDocument()
  })

  it('renders a resource card per catalog entry and applies a changed permission ceiling', async () => {
    const summary = makeSummary()
    const putBodies = []
    stubFetchByUrl(
      routesFor(summary, {
        [`/api/workspace/${WORKSPACE_ID}/permissions`]: (url, init) => {
          putBodies.push(JSON.parse(init.body))
          return jsonOk({ ok: true })
        },
      })
    )
    render(<WorkspaceDetailPage workspaceId={WORKSPACE_ID} />)
    await screen.findByText('Research Sandbox')
    fireEvent.click(screen.getByRole('tab', { name: 'Permissions & Resources' }))

    // All 6 canonical ceiling keys render as editor cards (validator order),
    // not just the 3 catalog rows: catalog entries supply the display
    // name/description/tools/context badge, the remaining keys (network, mcp,
    // container) fall back to fixed display-name metadata.
    expect(screen.getByText('Git')).toBeInTheDocument()
    expect(screen.getByText('Version control access to repositories.')).toBeInTheDocument()
    expect(screen.getByText('Filesystem')).toBeInTheDocument()
    expect(screen.getByText('Read and write access to workspace files.')).toBeInTheDocument()
    expect(screen.getByText('Host shell')).toBeInTheDocument()
    expect(screen.getByText('Network')).toBeInTheDocument()
    expect(screen.getByText('MCP')).toBeInTheDocument()
    expect(screen.getByText('Container')).toBeInTheDocument()
    expect(document.querySelectorAll('.wdp-resource-card').length).toBe(6)
    // Execution-context badges exist only on catalog rows (here: 3).
    expect(screen.getAllByText('container')).toHaveLength(2)
    // Tools render as individual chips (not a comma-joined string).
    expect(screen.getByText('git_read')).toBeInTheDocument()
    expect(screen.getByText('git_write')).toBeInTheDocument()
    expect(screen.getByText('read_file')).toBeInTheDocument()
    expect(screen.getByText('file_editor')).toBeInTheDocument()
    expect(screen.getByText('apply_edits')).toBeInTheDocument()
    // Enabled = level !== banned (container false maps OFF), so git,
    // filesystem and network are Enabled; host_bash, mcp and container are
    // Disabled.
    expect(screen.getAllByText('Enabled')).toHaveLength(3)
    expect(screen.getAllByText('Disabled')).toHaveLength(3)

    // Five ceiling selects (git, filesystem, network, mcp, host_bash in
    // ceiling order); container has NO dropdown — it is a real boolean switch
    // (Off for false). Each select offers that key's full canonical ceiling
    // vocabulary:
    //   git            banned|ask|read|write_on_feature_branch|write
    //   filesystem     banned|ask|read|write
    //   network        banned|ask|write|outbound
    //   mcp            banned|connect|full
    //   host_bash      banned|ask|allow
    const selects = screen.getAllByRole('combobox')
    expect(selects).toHaveLength(5)
    const optionValues = (index) =>
      Array.from(selects[index].options).map((o) => o.value)
    expect(optionValues(0)).toEqual([
      'banned',
      'ask',
      'read',
      'write_on_feature_branch',
      'write',
    ])
    const generic = ['banned', 'ask', 'read', 'write']
    expect(optionValues(1)).toEqual(generic)
    expect(optionValues(2)).toEqual(['banned', 'ask', 'write', 'outbound'])
    expect(optionValues(3)).toEqual(['banned', 'connect', 'full'])
    expect(optionValues(4)).toEqual(['banned', 'ask', 'allow'])
    const containerToggle = screen.getByRole('switch', { name: 'Toggle Container' })
    expect(containerToggle).toHaveAttribute('aria-checked', 'false')

    // Changing a dropdown marks pending state.
    fireEvent.change(selects[0], { target: { value: 'write' } })
    expect(screen.getByText('Unsaved changes')).toBeInTheDocument()

    const applyButton = screen.getByRole('button', { name: 'Apply Permissions' })
    expect(applyButton).not.toBeDisabled()
    summary.permissions.git = 'write'
    fireEvent.click(applyButton)

    await waitFor(() => expect(putBodies.length).toBe(1))
    // allow_host_resources is sent ONLY when the host toggle changed; a pure
    // permission edit omits it (the backend treats an absent key as unchanged).
    expect(putBodies[0]).toEqual({
      permissions: {
        git: 'write',
        filesystem: 'read',
        container: false,
        network: 'ask',
        mcp: 'banned',
        host_bash: 'banned',
      },
    })

    // After the refetch the dropdown reflects the applied ceiling.
    await waitFor(() => expect(screen.getAllByRole('combobox')[0].value).toBe('write'))
    expect(screen.queryByText('Unsaved changes')).toBeNull()
  })

  it('renders worker templates and active workers in separated sections', async () => {
    stubFetchByUrl(routesFor(makeSummary()))
    render(<WorkspaceDetailPage workspaceId={WORKSPACE_ID} />)
    await screen.findByText('Research Sandbox')
    fireEvent.click(screen.getByRole('tab', { name: 'Workers' }))

    expect(screen.getByText('Worker Templates')).toBeInTheDocument()
    expect(screen.getByText('Active Workers')).toBeInTheDocument()

    expect(screen.getByText('default-worker')).toBeInTheDocument()
    expect(screen.getByText('Default worker template')).toBeInTheDocument()
    expect(screen.getByText('file_editor, docker_code_runner')).toBeInTheDocument()

    expect(screen.getByText('worker-1')).toBeInTheDocument()
    expect(screen.getByText('#7')).toBeInTheDocument()
    expect(screen.getByText('running')).toBeInTheDocument()
    expect(screen.getByText('2m 5s')).toBeInTheDocument()
  })

  it('renders tools from /api/tools with disabled reasons and no false empty string', async () => {
    stubFetchByUrl(
      routesFor(makeSummary(), {
        '/api/tools': jsonOk({
          tools: [
            {
              name: 'host_bash',
              enabled: false,
              disabled_reason: 'requires allow_host_resources: true',
              permission_level: 'banned',
            },
            { name: 'git', enabled: true, disabled_reason: null, permission_level: 'write' },
          ],
        }),
      })
    )
    render(<WorkspaceDetailPage workspaceId={WORKSPACE_ID} />)
    await screen.findByText('Research Sandbox')
    fireEvent.click(screen.getByRole('tab', { name: 'Tools' }))

    expect(await screen.findByText('host_bash')).toBeInTheDocument()
    expect(screen.getByText('git')).toBeInTheDocument()
    expect(screen.getByText('requires allow_host_resources: true')).toBeInTheDocument()
    expect(screen.getByText('permission: banned')).toBeInTheDocument()
    // Both fixture tools carry a permission_level, so the ceiling note appears twice.
    expect(screen.getAllByText('Controlled by permission ceiling')).toHaveLength(2)
    expect(screen.getByText('Disabled')).toBeInTheDocument()
    expect(screen.getByText('Enabled')).toBeInTheDocument()
    // CRITICAL: no false empty-state string while tools exist.
    expect(screen.queryByText('No tools configured')).toBeNull()
    expect(screen.queryByText('No tools available.')).toBeNull()
  })

  it('shows an inline error and Retry in the tools tab when /api/tools fails', async () => {
    const routes = routesFor(makeSummary(), {
      '/api/tools': jsonErr('Tools service down', 503),
    })
    stubFetchByUrl(routes)
    render(<WorkspaceDetailPage workspaceId={WORKSPACE_ID} />)
    await screen.findByText('Research Sandbox')
    fireEvent.click(screen.getByRole('tab', { name: 'Tools' }))

    expect(await screen.findByText('Tools service down')).toBeInTheDocument()
    const retry = screen.getByRole('button', { name: 'Retry' })

    routes['/api/tools'] = jsonOk({
      tools: [{ name: 'git', enabled: true, disabled_reason: null, permission_level: null }],
    })
    fireEvent.click(retry)

    expect(await screen.findByText('git')).toBeInTheDocument()
    // The page did not crash: the summary header is still rendered.
    expect(screen.getByText('Research Sandbox')).toBeInTheDocument()
  })

  it('renders active container rows and the dockerfile path', async () => {
    stubFetchByUrl(routesFor(makeSummary()))
    render(<WorkspaceDetailPage workspaceId={WORKSPACE_ID} />)
    await screen.findByText('Research Sandbox')
    fireEvent.click(screen.getByRole('tab', { name: 'Containers' }))

    expect(screen.getByText('/home/jojo/workspaces/research/Dockerfile')).toBeInTheDocument()
    expect(screen.getByText('Dockerfile content available')).toBeInTheDocument()
    expect(screen.getByText('research-runner')).toBeInTheDocument()
    expect(screen.getByText('resource')).toBeInTheDocument()
    expect(screen.getByText('running')).toBeInTheDocument()
    expect(screen.getByText('abc123')).toBeInTheDocument()
    expect(screen.getAllByText(WORKSPACE_ID).length).toBeGreaterThanOrEqual(2)
  })

  it('shows the empty containers state and the null-dockerfile note', async () => {
    stubFetchByUrl(
      routesFor(
        makeSummary({
          active_containers: [],
          dockerfile: { path: '/home/jojo/workspaces/research/Dockerfile', content: null },
        })
      )
    )
    render(<WorkspaceDetailPage workspaceId={WORKSPACE_ID} />)
    await screen.findByText('Research Sandbox')
    fireEvent.click(screen.getByRole('tab', { name: 'Containers' }))

    expect(screen.getByText('No active containers.')).toBeInTheDocument()
    expect(screen.getByText('No Dockerfile content recorded')).toBeInTheDocument()
    expect(screen.queryByText('Dockerfile content available')).toBeNull()
  })

  it('never renders hardcoded risk-score dummy data', async () => {
    stubFetchByUrl(routesFor(makeSummary()))
    render(<WorkspaceDetailPage workspaceId={WORKSPACE_ID} />)
    await screen.findByText('Research Sandbox')

    expect(screen.queryByText(/risk score/i)).toBeNull()
    expect(screen.queryByText(/critical/i)).toBeNull()
    // The detail page now carries its own back-to-workspaces nav link.
    expect(screen.getByRole('link', { name: '← Back to workspaces' })).toHaveAttribute('href', '#/workspaces')
  })

  it('shows the exact placeholder strings in the Session Defaults and Credentials tabs', async () => {
    stubFetchByUrl(routesFor(makeSummary()))
    render(<WorkspaceDetailPage workspaceId={WORKSPACE_ID} />)
    await screen.findByText('Research Sandbox')

    fireEvent.click(screen.getByRole('tab', { name: 'Session Defaults' }))
    expect(screen.getByText('Session defaults will be configurable here soon.')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: 'Credentials' }))
    expect(screen.getByText('Workspace credential attachments coming soon.')).toBeInTheDocument()
  })

  it('renders the new detail page, not the old WorkspacePanel', async () => {
    stubFetchByUrl(routesFor(makeSummary()))
    render(<WorkspaceDetailPage workspaceId={WORKSPACE_ID} />)
    await screen.findByText('Research Sandbox')

    // The new page's key headings are present...
    expect(screen.getByText('Root path')).toBeInTheDocument()
    expect(screen.getByText('Security posture')).toBeInTheDocument()
    expect(screen.getAllByText('Host execution').length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText('Active sessions')).toBeInTheDocument()
    expect(screen.getByText('Active containers')).toBeInTheDocument()
    // ...and the legacy panel's error chrome is not; the new page's own back
    // link IS present (three-layer nav: workspaces → workspace → session).
    expect(screen.getByRole('link', { name: '← Back to workspaces' })).toHaveAttribute('href', '#/workspaces')
    expect(screen.queryByText('Workspace not found.')).toBeNull()
  })

  it('opens the New Session modal from the header button', async () => {
    stubFetchByUrl(
      routesFor(makeSummary(), {
        '/api/session/list?workspace_id=ws-1': jsonOk([]),
      })
    )
    render(<WorkspaceDetailPage workspaceId={WORKSPACE_ID} />)
    await screen.findByText('Research Sandbox')

    const newSessionBtn = screen.getByRole('button', { name: '+ New Session' })
    expect(newSessionBtn).toBeInTheDocument()
    expect(screen.queryByRole('dialog', { name: /^New session$/ })).toBeNull()

    fireEvent.click(newSessionBtn)
    expect(screen.getByRole('dialog', { name: /^New session$/ })).toBeInTheDocument()
    expect(screen.getByText('Sessions in this workspace')).toBeInTheDocument()
  })

  it('creates a session sending the absolute root path as workspace_path', async () => {
    const createBodies = []
    stubFetchByUrl(
      routesFor(makeSummary(), {
        '/api/session/list?workspace_id=ws-1': jsonOk([]),
        '/api/session/create': (url, init) => {
          createBodies.push(JSON.parse(init.body))
          return jsonOk({ session_id: 's-new-1', mode: 'engineer' })
        },
      })
    )
    render(<WorkspaceDetailPage workspaceId={WORKSPACE_ID} />)
    await screen.findByText('Research Sandbox')

    fireEvent.click(screen.getByRole('button', { name: '+ New Session' }))
    await screen.findByRole('dialog', { name: /^New session$/ })
    fireEvent.click(screen.getByRole('button', { name: 'Create Session' }))

    await waitFor(() => expect(createBodies.length).toBe(1))
    // The store has no list entry for ws-1, so the summary's absolute
    // root_path must be forwarded verbatim as workspace_path.
    expect(createBodies[0]).toEqual({
      mode: 'engineer',
      workspace_id: WORKSPACE_ID,
      workspace_path: '/home/jojo/workspaces/research',
    })
    expect(await screen.findByText('Session created.')).toBeInTheDocument()
  })

  it('closes the New Session modal via Cancel without creating a session', async () => {
    const createBodies = []
    stubFetchByUrl(
      routesFor(makeSummary(), {
        '/api/session/list?workspace_id=ws-1': jsonOk([]),
        '/api/session/create': (url, init) => {
          createBodies.push(JSON.parse(init.body))
          return jsonOk({ session_id: 's-cancel-1', mode: 'engineer' })
        },
      })
    )
    render(<WorkspaceDetailPage workspaceId={WORKSPACE_ID} />)
    await screen.findByText('Research Sandbox')

    fireEvent.click(screen.getByRole('button', { name: '+ New Session' }))
    expect(screen.getByRole('dialog', { name: /^New session$/ })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog', { name: /^New session$/ })).toBeNull()
    expect(createBodies.length).toBe(0)
  })

  // --- Containers tab: live read-only status view -------------------------
  // The tab must present the summary's live container state as a read-only
  // table, reveal a detail region on row selection, and lazily fetch
  // container logs from the read-only logs route. No lifecycle controls.

  it('renders a read-only container status table with column headers', async () => {
    stubFetchByUrl(routesFor(makeSummary()))
    render(<WorkspaceDetailPage workspaceId={WORKSPACE_ID} />)
    await screen.findByText('Research Sandbox')
    fireEvent.click(screen.getByRole('tab', { name: 'Containers' }))

    expect(screen.getByRole('columnheader', { name: 'Name' })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'State' })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'Type' })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'ID' })).toBeInTheDocument()
  })

  it('reveals a container detail region when a row is selected', async () => {
    stubFetchByUrl(routesFor(makeSummary()))
    render(<WorkspaceDetailPage workspaceId={WORKSPACE_ID} />)
    await screen.findByText('Research Sandbox')
    fireEvent.click(screen.getByRole('tab', { name: 'Containers' }))

    expect(screen.queryByText('Container detail')).toBeNull()
    fireEvent.click(screen.getByText('research-runner'))
    expect(screen.getByText('Container detail')).toBeInTheDocument()
    expect(screen.getByText('Workspace ID')).toBeInTheDocument()
  })

  it('loads container logs on demand into a pre element with the read-only route', async () => {
    const logText = 'boot\nlistening on :8080\n'
    const fetchMock = stubFetchByUrl(
      routesFor(makeSummary(), {
        '/api/workspace/ws-1/containers/research-runner/logs': logsOk(logText),
      })
    )
    render(<WorkspaceDetailPage workspaceId={WORKSPACE_ID} />)
    await screen.findByText('Research Sandbox')
    fireEvent.click(screen.getByRole('tab', { name: 'Containers' }))

    fireEvent.click(screen.getByRole('button', { name: 'Logs' }))

    const pre = await screen.findByText(/listening on :8080/)
    expect(pre.tagName).toBe('PRE')
    // Rendered output is the decoded stdout, not the JSON envelope blob.
    expect(pre.textContent).toMatch(/listening on :8080/)
    expect(pre.textContent).not.toMatch(/"container"|"success"|"duration"/)
    const urls = fetchMock.mock.calls.map(([u]) => String(u))
    expect(
      urls.some((u) =>
        u.includes('/api/workspace/ws-1/containers/research-runner/logs?tail=200')
      )
    ).toBe(true)
  })

  it('shows an inline error when container logs fail to load', async () => {
    stubFetchByUrl(
      routesFor(makeSummary(), {
        '/api/workspace/ws-1/containers/research-runner/logs': jsonErr('no such container', 404),
      })
    )
    render(<WorkspaceDetailPage workspaceId={WORKSPACE_ID} />)
    await screen.findByText('Research Sandbox')
    fireEvent.click(screen.getByRole('tab', { name: 'Containers' }))

    fireEvent.click(screen.getByRole('button', { name: 'Logs' }))
    expect(await screen.findByText(/Failed to load logs/)).toBeInTheDocument()
  })

  it('exposes no container lifecycle controls in the read-only tab', async () => {
    stubFetchByUrl(routesFor(makeSummary()))
    render(<WorkspaceDetailPage workspaceId={WORKSPACE_ID} />)
    await screen.findByText('Research Sandbox')
    fireEvent.click(screen.getByRole('tab', { name: 'Containers' }))

    expect(
      screen.queryByRole('button', { name: /start|stop|remove|restart|delete|kill|recreate/i })
    ).toBeNull()
    // The only control in the tab is the read-only logs viewer toggle.
    expect(screen.getByRole('button', { name: 'Logs' })).toBeInTheDocument()
  })
})

// --- Container logs viewer extraction (integration) -------------------------
// The inline logs viewer was extracted into ContainerLogsViewer; the tab now
// delegates to it. These tests pin the accessible contract the tab exposes:
// a labelled logs region, the single long-log affordance (a tail-size select),
// a logs toggle whose accessible name still contains "Logs", and the exact
// backend error string.

describe('WorkspaceDetailPage \u2014 container logs viewer', () => {
  it('exposes an accessible logs region and a tail-size select after opening logs', async () => {
    stubFetchByUrl(
      routesFor(makeSummary(), {
        '/api/workspace/ws-1/containers/research-runner/logs': logsOk('boot\nlistening on :8080\n'),
      })
    )
    render(<WorkspaceDetailPage workspaceId={WORKSPACE_ID} />)
    await screen.findByText('Research Sandbox')
    fireEvent.click(screen.getByRole('tab', { name: 'Containers' }))

    // The logs toggle's accessible name still contains "Logs".
    fireEvent.click(screen.getByRole('button', { name: /Logs/ }))

    // A labelled region wraps the log output.
    const region = await screen.findByRole('region', { name: 'Container logs' })
    expect(region).toBeInTheDocument()
    const pre = screen.getByText(/listening on :8080/)
    expect(pre.tagName).toBe('PRE')

    // The single long-log affordance is the tail-size select.
    const select = screen.getByRole('combobox', { name: 'Log tail size' })
    expect(Array.from(select.options).map((o) => Number(o.value))).toEqual([
      200, 500, 1000, 2000,
    ])
  })

  it('refetches container logs with the newly chosen tail', async () => {
    const fetchMock = stubFetchByUrl(
      routesFor(makeSummary(), {
        '/api/workspace/ws-1/containers/research-runner/logs': logsOk('boot\nlistening on :8080\n'),
      })
    )
    render(<WorkspaceDetailPage workspaceId={WORKSPACE_ID} />)
    await screen.findByText('Research Sandbox')
    fireEvent.click(screen.getByRole('tab', { name: 'Containers' }))
    fireEvent.click(screen.getByRole('button', { name: /Logs/ }))

    const select = await screen.findByRole('combobox', { name: 'Log tail size' })
    fireEvent.change(select, { target: { value: '500' } })

    await waitFor(() => {
      const urls = fetchMock.mock.calls.map(([u]) => String(u))
      expect(
        urls.some((u) =>
          u.includes('/api/workspace/ws-1/containers/research-runner/logs?tail=500')
        )
      ).toBe(true)
    })
  })
})

