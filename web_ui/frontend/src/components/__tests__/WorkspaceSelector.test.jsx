// @vitest-environment jsdom
/*
 * WorkspaceSelector.test.jsx — landing selector tests (Phase 1).
 *
 * Tests the REAL WorkspaceSelector (src/components/WorkspaceSelector.jsx)
 * against the real workspaceStore. Navigation goes through the hash router
 * (../router.js), so after create/click the hash is `#/workspace/<id>`.
 * The selector does NOT fetch on mount — only the Refresh button does.
 */

import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor, within } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import WorkspaceSelector from '../WorkspaceSelector'
import useWorkspaceStore from '../../store/workspaceStore'
import purposeDefinitions from '../../data/purposeDefinitions.json'

// ---------------------------------------------------------------------------
// fetch stub (only used by the Refresh test)
// ---------------------------------------------------------------------------
function jsonOk(data, status = 200) {
  return { ok: true, status, json: async () => data, text: async () => JSON.stringify(data) }
}

// ---------------------------------------------------------------------------
// Setup / teardown
// ---------------------------------------------------------------------------
beforeEach(() => {
  localStorage.clear()
  window.location.hash = ''
  useWorkspaceStore.getState().reset()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

// ===========================================================================
// Structure
// ===========================================================================
describe('WorkspaceSelector — structure', () => {
  it('renders the sidebar title and the main heading', () => {
    render(<WorkspaceSelector />)
    expect(screen.getByRole('heading', { name: 'Workspaces' })).toBeInTheDocument()
    expect(
      screen.getByRole('heading', { name: 'What kind of work do you want to do today?' })
    ).toBeInTheDocument()
  }, 20000)

  it('renders a purpose card for every entry in purposeDefinitions', () => {
    render(<WorkspaceSelector />)
    purposeDefinitions.forEach((p) => {
      expect(screen.getByRole('heading', { level: 4, name: p.label })).toBeInTheDocument()
    })
    expect(screen.getAllByRole('heading', { level: 4 })).toHaveLength(purposeDefinitions.length)
  })

  it('renders full card contents: risk, icon, description, settings, docker requirement', () => {
    render(<WorkspaceSelector />)
    purposeDefinitions.forEach((p) => {
      const card = screen.getByRole('heading', { level: 4, name: p.label }).closest('.ws-card')
      expect(within(card).getByText(p.risk)).toBeInTheDocument()
      expect(within(card).getByRole('img', { name: p.label })).toBeInTheDocument()
      expect(within(card).getByText(p.description)).toBeInTheDocument()
      expect(within(card).getByText(p.recommendedSettings)).toBeInTheDocument()
      expect(
        within(card).getByText(`Requires Docker: ${p.requiresDocker ? 'yes' : 'no'}`)
      ).toBeInTheDocument()
    })
  })

  it('shows the correct Requires Docker yes/no counts across cards', () => {
    render(<WorkspaceSelector />)
    const yes = purposeDefinitions.filter((p) => p.requiresDocker).length
    const no = purposeDefinitions.length - yes
    expect(screen.getAllByText('Requires Docker: yes')).toHaveLength(yes)
    expect(screen.getAllByText('Requires Docker: no')).toHaveLength(no)
  })
})

// ===========================================================================
// Purpose card selection (create + navigate)
// ===========================================================================
describe('WorkspaceSelector — purpose selection', () => {
  it('creates a workspace and navigates when a card is clicked', () => {
    render(<WorkspaceSelector />)
    fireEvent.click(screen.getByRole('heading', { level: 4, name: 'Code Development' }).closest('.ws-card'))
    const list = useWorkspaceStore.getState().workspaceList
    const created = list.find((w) => w.purposeId === 'code-development')
    expect(created).toBeTruthy()
    expect(window.location.hash).toBe(`#/workspace/${created.id}`)
  })

  it('creates a workspace on Enter keydown', () => {
    render(<WorkspaceSelector />)
    fireEvent.keyDown(
      screen.getByRole('heading', { level: 4, name: 'Writing & Research' }).closest('.ws-card'),
      { key: 'Enter' }
    )
    const created = useWorkspaceStore.getState().workspaceList.find(
      (w) => w.purposeId === 'writing-research'
    )
    expect(created).toBeTruthy()
    expect(window.location.hash).toBe(`#/workspace/${created.id}`)
  })

  it('creates a workspace on Space keydown', () => {
    render(<WorkspaceSelector />)
    fireEvent.keyDown(
      screen.getByRole('heading', { level: 4, name: 'Blank Workspace' }).closest('.ws-card'),
      { key: ' ' }
    )
    const created = useWorkspaceStore.getState().workspaceList.find((w) => w.purposeId === 'blank')
    expect(created).toBeTruthy()
    expect(window.location.hash).toBe(`#/workspace/${created.id}`)
  })
})

// ===========================================================================
// Sidebar workspace list
// ===========================================================================
describe('WorkspaceSelector — sidebar', () => {
  it('renders seeded workspaces with name, risk and path, and navigates on click', () => {
    useWorkspaceStore.setState({
      workspaceList: [
        { id: 'ws-a', name: 'Alpha Workspace', risk: 'Low', path: '/alpha' },
        { id: 'ws-b', name: 'Beta Workspace', risk: 'Critical', path: '/beta' },
      ],
    })
    render(<WorkspaceSelector />)
    expect(screen.getByText('Alpha Workspace')).toBeInTheDocument()
    expect(screen.getByText('Beta Workspace')).toBeInTheDocument()
    expect(screen.getByText('/alpha')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Alpha Workspace/ }))
    expect(window.location.hash).toBe('#/workspace/ws-a')
  }, 20000)

  it('applies the low/high risk classes in the sidebar', () => {
    useWorkspaceStore.setState({
      workspaceList: [
        { id: 'ws-a', name: 'Alpha Workspace', risk: 'Low', path: '' },
        { id: 'ws-b', name: 'Beta Workspace', risk: 'High', path: '' },
      ],
    })
    const { container } = render(<WorkspaceSelector />)
    expect(container.querySelector('.ws-sidebar .ws-risk.low')).toHaveTextContent('Low')
    expect(container.querySelector('.ws-sidebar .ws-risk.high')).toHaveTextContent('High')
  })

  it('applies the critical risk class to the Security Research purpose card', () => {
    const { container } = render(<WorkspaceSelector />)
    const critical = [...container.querySelectorAll('.ws-card .ws-risk.critical')]
    expect(critical).toHaveLength(1)
    expect(critical[0]).toHaveTextContent('Critical')
  })

  it('shows the empty message when no workspaces exist', () => {
    useWorkspaceStore.setState({ workspaceList: [] })
    render(<WorkspaceSelector />)
    expect(screen.getByText('No workspaces yet. Create one to get started.')).toBeInTheDocument()
  })

  it('does not fetch on mount', () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    render(<WorkspaceSelector />)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('refreshes the workspace list from the backend when Refresh is clicked', async () => {
    const fetchMock = vi.fn(async () => jsonOk([{ id: 'ws-x', label: 'Fresh Workspace', root: '/fresh' }]))
    vi.stubGlobal('fetch', fetchMock)
    render(<WorkspaceSelector />)
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(screen.getByText('Fresh Workspace')).toBeInTheDocument())
    expect(fetchMock).toHaveBeenCalledWith('/api/workspace/list')
  })
})
