// @vitest-environment jsdom
// --- GlobalSessions.test.jsx ---
// Active sessions grouped by workspace; rows navigate to the session view via
// the hash router.

import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import GlobalSessions from '../GlobalSessions'
import useWorkspaceStore from '../../store/workspaceStore'
import useStore from '../../store/useStore'
import useSessionTabsStore from '../../sessionTabsStore'

const WORKSPACES = [
  { id: 'ws-a', label: 'Alpha Workspace' },
  { id: 'ws-b', label: 'Beta Workspace' },
]

const SESSIONS = [
  {
    session_id: 'sess-1',
    workspace_id: 'ws-a',
    name: 'Refactor Session',
    mode: 'engineer',
    worker_count: 2,
    started_at: '2026-08-30T12:00:00Z',
  },
  {
    session_id: 'sess-2',
    workspace_id: 'ws-b',
    name: 'Research Session',
    mode: 'research',
    worker_count: 1,
    started_at: '2026-08-29T08:00:00Z',
  },
  {
    session_id: 'sess-3',
    workspace_id: 'ws-a',
    name: 'Orphaned Session',
    mode: 'architect',
    worker_count: 0,
    started_at: '',
  },
]

beforeEach(() => {
  localStorage.clear()
  window.location.hash = ''
  useWorkspaceStore.getState().reset()
  useStore.getState().reset()
  useSessionTabsStore.getState().reset()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('GlobalSessions', () => {
  it('shows the empty state when there are no sessions', () => {
    render(<GlobalSessions />)
    expect(screen.getByText('No active sessions.')).toBeInTheDocument()
  })

  it('groups sessions by workspace and uses workspace labels', () => {
    render(<GlobalSessions sessions={SESSIONS} workspaces={WORKSPACES} />)
    expect(screen.getByText('Alpha Workspace')).toBeInTheDocument()
    expect(screen.getByText('Beta Workspace')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /Session/ }).length).toBe(3)
    expect(screen.getByText('engineer')).toBeInTheDocument()
    expect(screen.getByText('2 workers')).toBeInTheDocument()
    expect(screen.getByText('0 workers')).toBeInTheDocument()
  })

  it('navigates to the session view when a row is clicked', () => {
    render(<GlobalSessions sessions={SESSIONS} workspaces={WORKSPACES} />)
    fireEvent.click(screen.getByRole('button', { name: /Refactor Session/ }))
    expect(window.location.hash).toBe('#/session/sess-1')
  })

  it('encodes session ids in the hash URL', () => {
    render(
      <GlobalSessions
        sessions={[
          {
            session_id: 'a/b c',
            workspace_id: 'ws-a',
            name: 'Odd Session',
            mode: 'research',
            worker_count: 1,
            started_at: null,
          },
        ]}
        workspaces={WORKSPACES}
      />
    )
    fireEvent.click(screen.getByRole('button', { name: /Odd Session/ }))
    expect(window.location.hash).toBe('#/session/a%2Fb%20c')
  })

  it('falls back to the workspace id when no label exists', () => {
    render(
      <GlobalSessions
        sessions={[
          {
            session_id: 's-y',
            workspace_id: 'ws-z',
            name: 'Zed Session',
            mode: 'research',
            worker_count: 1,
            started_at: '2026-08-30T12:00:00Z',
          },
        ]}
        workspaces={[]}
      />
    )
    expect(screen.getByText('ws-z')).toBeInTheDocument()
  })

  it('falls back to "Unknown workspace" when the workspace id is missing', () => {
    render(
      <GlobalSessions
        sessions={[
          {
            session_id: 's-x',
            workspace_id: null,
            name: 'No Workspace',
            mode: 'research',
            worker_count: 1,
            started_at: '2026-08-30T12:00:00Z',
          },
        ]}
        workspaces={[]}
      />
    )
    expect(screen.getByText('Unknown workspace')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /No Workspace/ }))
    expect(window.location.hash).toBe('#/session/s-x')
  })

  it('shows Untitled Session and tolerates missing fields', () => {
    render(
      <GlobalSessions
        sessions={[{ session_id: 's-u', workspace_id: 'ws-a', mode: 'research', started_at: 'not-a-date' }]}
        workspaces={WORKSPACES}
      />
    )
    expect(screen.getByText('Untitled Session')).toBeInTheDocument()
    expect(screen.getByText('0 workers')).toBeInTheDocument()
    expect(screen.getByText('Alpha Workspace')).toBeInTheDocument()
  })

  it('renders a non-empty started-time span for valid dates', () => {
    const { container } = render(
      <GlobalSessions
        sessions={[
          {
            session_id: 's-t',
            workspace_id: 'ws-a',
            name: 'Timed Session',
            mode: 'engineer',
            worker_count: 1,
            started_at: '2026-08-30T12:00:00Z',
          },
        ]}
        workspaces={WORKSPACES}
      />
    )
    const started = container.querySelector('.gms-session-started')
    expect(started).not.toBeNull()
    expect(started.textContent.length).toBeGreaterThan(0)
  })
})
