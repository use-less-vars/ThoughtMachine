// @vitest-environment jsdom
/*
 * router.test.js — parseHash route parsing.
 *   - nested route #/workspace/:wsId/session/:sid carries workspaceId
 *   - legacy #/session/:id parses with workspaceId null
 *   - workspace / selector / unknown hashes
 */
import { describe, it, expect } from 'vitest'
import { parseHash } from '../../router'

describe('parseHash', () => {
  it('returns the selector view for empty / root / workspaces hashes', () => {
    expect(parseHash('')).toEqual({ view: 'selector' })
    expect(parseHash('#')).toEqual({ view: 'selector' })
    expect(parseHash('/')).toEqual({ view: 'selector' })
    expect(parseHash('#/workspaces')).toEqual({ view: 'selector' })
  })

  it('parses the workspace route', () => {
    expect(parseHash('#/workspace/ws-1')).toEqual({ view: 'workspace', id: 'ws-1' })
  })

  it('parses the nested session route with an explicit workspace', () => {
    expect(parseHash('#/workspace/ws-1/session/s-1')).toEqual({
      view: 'session',
      id: 's-1',
      workspaceId: 'ws-1',
    })
  })

  it('parses the legacy session route with workspaceId null', () => {
    expect(parseHash('#/session/s-1')).toEqual({ view: 'session', id: 's-1', workspaceId: null })
  })

  it('decodes URL-encoded ids in nested session routes', () => {
    expect(parseHash('#/workspace/my%20ws/session/my%20session')).toEqual({
      view: 'session',
      id: 'my session',
      workspaceId: 'my ws',
    })
  })

  it('returns null for unknown hashes', () => {
    expect(parseHash('#/garbage')).toBeNull()
    expect(parseHash('#/workspace/ws-1/session')).toBeNull()
    expect(parseHash('#/workspace/ws-1/session/')).toBeNull()
  })
})
