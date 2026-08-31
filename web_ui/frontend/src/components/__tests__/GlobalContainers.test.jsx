// @vitest-environment jsdom
// --- GlobalContainers.test.jsx ---
// Read-only list of active containers with type badges.

import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import GlobalContainers from '../GlobalContainers'
import useWorkspaceStore from '../../store/workspaceStore'
import useStore from '../../store/useStore'
import useSessionTabsStore from '../../sessionTabsStore'

const CONTAINERS = [
  { name: 'alpha-ctr', type: 'free_use', workspace_id: 'ws-a', status: 'running' },
  { name: 'beta-ctr', type: 'resource', workspace_id: 'ws-b', status: 'stopped' },
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

describe('GlobalContainers', () => {
  it('shows the empty state when there are no containers', () => {
    render(<GlobalContainers />)
    expect(screen.getByText('No active containers.')).toBeInTheDocument()
  })

  it('renders container rows with type badges', () => {
    const { container } = render(<GlobalContainers containers={CONTAINERS} />)
    expect(screen.getByText('alpha-ctr')).toBeInTheDocument()
    expect(screen.getByText('beta-ctr')).toBeInTheDocument()
    expect(screen.getByText('free_use')).toBeInTheDocument()
    expect(screen.getByText('resource')).toBeInTheDocument()
    expect(screen.getByText('ws-a')).toBeInTheDocument()
    expect(screen.getByText('ws-b')).toBeInTheDocument()
    expect(screen.getByText('running')).toBeInTheDocument()
    expect(screen.getByText('stopped')).toBeInTheDocument()

    expect(container.querySelector('.gms-type-free')).toBeInTheDocument()
    expect(container.querySelector('.gms-type-resource')).toBeInTheDocument()
    expect(container.querySelectorAll('.gms-container-row').length).toBe(2)
  })

  it('falls back to free_use badge for unknown container types', () => {
    const { container } = render(
      <GlobalContainers containers={[{ name: 'c1', workspace_id: 'ws-a', status: 'running' }]} />
    )
    expect(screen.getByText('free_use')).toBeInTheDocument()
    expect(container.querySelector('.gms-type-free')).toBeInTheDocument()
  })
})
