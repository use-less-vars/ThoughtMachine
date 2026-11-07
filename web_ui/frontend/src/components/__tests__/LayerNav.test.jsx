// @vitest-environment jsdom
/*
 * LayerNav.test.jsx — WAVE 4b PHASE 1: shared top-left breadcrumb (LayerNav).
 *
 * Unit tests: render <LayerNav> DIRECTLY (no App mount) and assert the
 * three-layer breadcrumb contract (workspaces → workspace → session):
 *   - Case A (workspace): 1 link (#/workspaces) + current span (aria-current).
 *   - Case B (session + workspaceId): 2 links (#/workspaces, #/workspace/:id)
 *     + current span.
 *   - Case C (session, legacy, no workspaceId): 1 link (#/workspaces)
 *     + current span, NO workspace crumb.
 *   - selector / falsy route: renders nothing (container.firstChild === null).
 *
 * Ancestor crumbs are REAL anchors with a literal href; the current crumb is a
 * plain <span aria-current="page"> (never a link).
 */
import React from 'react'
import { describe, it, expect, afterEach } from 'vitest'
import { render, cleanup } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import LayerNav from '../LayerNav'

afterEach(() => cleanup())

const navOf = (container) => container.querySelector('[data-testid="layer-nav"]')

describe('LayerNav — Case A: workspace view', () => {
  it('renders 1 ancestor link (#/workspaces) + current workspace crumb', () => {
    const { container } = render(
      <LayerNav route={{ view: 'workspace', id: 'ws1' }} workspaceLabel="Alpha" sessionLabel={null} />
    )
    const nav = navOf(container)
    expect(nav).not.toBeNull()
    const links = nav.querySelectorAll('a')
    expect(links).toHaveLength(1)
    expect(links[0]).toHaveAttribute('href', '#/workspaces')
    const current = nav.querySelector('[aria-current="page"]')
    expect(current).not.toBeNull()
    expect(current.tagName).toBe('SPAN')
    expect(current.textContent).toBe('Alpha')
  })

  it('falls back to "Workspace" when workspaceLabel is null', () => {
    const { container } = render(
      <LayerNav route={{ view: 'workspace', id: 'ws1' }} workspaceLabel={null} sessionLabel={null} />
    )
    const nav = navOf(container)
    expect(nav.querySelector('[aria-current="page"]').textContent).toBe('Workspace')
    const links = nav.querySelectorAll('a')
    expect(links).toHaveLength(1)
    expect(links[0]).toHaveAttribute('href', '#/workspaces')
  })
})

describe('LayerNav — Case B: session view with workspaceId', () => {
  it('renders 2 ancestor links + current session crumb', () => {
    const { container } = render(
      <LayerNav route={{ view: 'session', id: 's1', workspaceId: 'ws1' }} workspaceLabel="Alpha" sessionLabel="Chat" />
    )
    const nav = navOf(container)
    const links = nav.querySelectorAll('a')
    expect(links).toHaveLength(2)
    expect(links[0]).toHaveAttribute('href', '#/workspaces')
    expect(links[1]).toHaveAttribute('href', '#/workspace/ws1')
    const current = nav.querySelector('[aria-current="page"]')
    expect(current).not.toBeNull()
    expect(current.tagName).toBe('SPAN')
    expect(current.textContent).toBe('Chat')
  })

  it('encodes the workspaceId in the workspace crumb href', () => {
    const { container } = render(
      <LayerNav route={{ view: 'session', id: 's1', workspaceId: 'ws 1' }} workspaceLabel="Alpha" sessionLabel="Chat" />
    )
    const links = navOf(container).querySelectorAll('a')
    expect(links).toHaveLength(2)
    expect(links[0]).toHaveAttribute('href', '#/workspaces')
    expect(links[1]).toHaveAttribute('href', '#/workspace/ws%201')
  })
})

describe('LayerNav — Case C: legacy session view (no workspaceId)', () => {
  it('renders 1 ancestor link + current session crumb, no workspace crumb', () => {
    const { container } = render(
      <LayerNav route={{ view: 'session', id: 's1' }} workspaceLabel={null} sessionLabel="Chat" />
    )
    const nav = navOf(container)
    const links = nav.querySelectorAll('a')
    expect(links).toHaveLength(1)
    expect(links[0]).toHaveAttribute('href', '#/workspaces')
    const current = nav.querySelector('[aria-current="page"]')
    expect(current).not.toBeNull()
    expect(current.tagName).toBe('SPAN')
    expect(current.textContent).toBe('Chat')
  })

  it('falls back to "Session" when sessionLabel is null', () => {
    const { container } = render(
      <LayerNav route={{ view: 'session', id: 's1' }} workspaceLabel={null} sessionLabel={null} />
    )
    expect(navOf(container).querySelector('[aria-current="page"]').textContent).toBe('Session')
  })
})

describe('LayerNav — renders nothing', () => {
  it('selector view renders nothing', () => {
    const { container } = render(
      <LayerNav route={{ view: 'selector' }} workspaceLabel={null} sessionLabel={null} />
    )
    expect(container.firstChild).toBeNull()
  })

  it('falsy route renders nothing', () => {
    const { container } = render(<LayerNav route={null} workspaceLabel={null} sessionLabel={null} />)
    expect(container.firstChild).toBeNull()
  })
})
