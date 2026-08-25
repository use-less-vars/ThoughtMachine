// @vitest-environment jsdom
/**
 * workerEventRouting.test.jsx — event → panel routing for flexible worker panels.
 *
 * Covers the required EVENT ROUTING scenario:
 *   - two panels with the same worker_name but different instance_id: an event
 *     carrying instance_id routes ONLY to the matching key (no cross-instance
 *     leakage);
 *   - instance_label fallback when instance_id is absent on the event;
 *   - bare worker_name fallback (matches EVERY panel with that name);
 *   - events with no worker_name never match anything;
 *   - instanceKeyOf key construction and null safety.
 */
import { describe, it, expect } from 'vitest'
import { instanceKeyOf, matchesPanel, routeEventsToPanels } from '../chat/workerEventRouting'

describe('instanceKeyOf', () => {
  it('builds the worker_name#instance_id key (missing instance id → 0)', () => {
    expect(instanceKeyOf({ worker_name: 'w1', instance_id: 2 })).toBe('w1#2')
    expect(instanceKeyOf({ worker_name: 'w1' })).toBe('w1#0')
    expect(instanceKeyOf({ worker_name: 'w1', instance_id: 0 })).toBe('w1#0')
  })

  it('returns null for entries without a worker_name or for non-objects', () => {
    expect(instanceKeyOf({})).toBeNull()
    expect(instanceKeyOf(null)).toBeNull()
    expect(instanceKeyOf(undefined)).toBeNull()
    expect(instanceKeyOf('w1')).toBeNull()
    expect(instanceKeyOf(42)).toBeNull()
  })
})

describe('matchesPanel', () => {
  it('matches by instance_id first (strict equality)', () => {
    const panel = { worker_name: 'w1', instance_id: 2, instance_label: 'L1' }
    expect(matchesPanel({ worker_name: 'w1', instance_id: 2 }, panel)).toBe(true)
    expect(matchesPanel({ worker_name: 'w1', instance_id: 3 }, panel)).toBe(false)
    // Event has an instance_id but the panel has none → no match
    expect(matchesPanel({ worker_name: 'w1', instance_id: 2 }, { worker_name: 'w1' })).toBe(false)
  })

  it('falls back to instance_label when the event has no instance_id', () => {
    const panel = { worker_name: 'w1', instance_label: 'L1' }
    expect(matchesPanel({ worker_name: 'w1', instance_label: 'L1' }, panel)).toBe(true)
    expect(matchesPanel({ worker_name: 'w1', instance_label: 'L2' }, panel)).toBe(false)
    // instance_id wins over a mismatched label
    expect(matchesPanel({ worker_name: 'w1', instance_id: 2, instance_label: 'L9' }, panel)).toBe(false)
  })

  it('falls back to bare worker_name equality when the event has no instance identity', () => {
    // Bare event matches EVERY panel with that worker_name (multi-match)
    expect(matchesPanel({ worker_name: 'w1' }, { worker_name: 'w1', instance_id: 2 })).toBe(true)
    expect(matchesPanel({ worker_name: 'w1' }, { worker_name: 'w1', instance_label: 'L9' })).toBe(true)
    expect(matchesPanel({ worker_name: 'w1' }, { worker_name: 'w2' })).toBe(false)
  })

  it('never matches events without worker_name, or null/non-object inputs', () => {
    expect(matchesPanel({}, { worker_name: 'w1' })).toBe(false)
    expect(matchesPanel({ type: 'unrelated' }, { worker_name: 'w1' })).toBe(false)
    expect(matchesPanel(null, { worker_name: 'w1' })).toBe(false)
    expect(matchesPanel({ worker_name: 'w1' }, null)).toBe(false)
    expect(matchesPanel('str', { worker_name: 'w1' })).toBe(false)
  })
})

describe('routeEventsToPanels', () => {
  it('routes each event only to matching panels, with no cross-instance leakage', () => {
    const panels = [
      { worker_name: 'w1', instance_id: 1 },
      { worker_name: 'w1', instance_id: 2 },
      { worker_name: 'w2', instance_id: 1 },
    ]
    const e1 = { worker_name: 'w1', instance_id: 1 }
    const e2 = { worker_name: 'w1', instance_id: 2 }
    const e3 = { worker_name: 'w1' } // bare → matches BOTH w1 panels
    const e4 = { worker_name: 'w2', instance_id: 1 }
    const e5 = { type: 'unrelated' } // no worker_name → matches nothing

    const routed = routeEventsToPanels([e1, e2, e3, e4, e5], panels)

    expect(Object.keys(routed).sort()).toEqual(['w1#1', 'w1#2', 'w2#1'])
    expect(routed['w1#1']).toEqual([e1, e3])
    expect(routed['w1#2']).toEqual([e2, e3])
    expect(routed['w2#1']).toEqual([e4])
  })

  it('routes by instance_label when events and panels carry labels instead of ids', () => {
    const panels = [{ worker_name: 'w1', instance_label: 'Label A' }]
    const e1 = { worker_name: 'w1', instance_label: 'Label A' }
    const e2 = { worker_name: 'w1', instance_label: 'Label B' }
    const routed = routeEventsToPanels([e1, e2], panels)
    expect(routed['w1#0']).toEqual([e1])
  })

  it('handles empty and non-array inputs', () => {
    expect(routeEventsToPanels([], [])).toEqual({})
    expect(routeEventsToPanels(null, null)).toEqual({})
    expect(routeEventsToPanels(undefined, [])).toEqual({})
    // Non-array events → every panel gets an empty list
    expect(routeEventsToPanels('nope', [{ worker_name: 'w1' }])).toEqual({ 'w1#0': [] })
    expect(routeEventsToPanels([], [{ worker_name: 'w1' }])).toEqual({ 'w1#0': [] })
  })
})
