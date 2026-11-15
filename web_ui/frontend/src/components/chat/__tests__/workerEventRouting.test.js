/*
 * Regression tests for worker → panel instance-identity routing.
 *
 * Bug: `matchesPanel` compared `event.instance_id === panel.instance_id` as soon
 * as the EVENT carried an instance_id. Panels restored from localStorage written
 * by an older build have no instance_id at all (`JSON.stringify` drops
 * `undefined`), so `undefined === 1` was false and every event was dropped for
 * the restored panel — the panel rendered nothing.
 *
 * Rule now: instance identity is compared only when BOTH sides carry it; a
 * missing side falls through to the next, coarser rule (and panels are
 * backfilled with instance_id 1 on restore — see App.jsx).
 */
import { describe, expect, it } from 'vitest'
import { instanceKeyOf, matchesPanel, routeEventsToPanels } from '../workerEventRouting'

const worker = (extra = {}) => ({ worker_name: 'default', ...extra })

describe('matchesPanel — instance identity', () => {
  it('matches when both sides carry the same instance_id', () => {
    expect(matchesPanel(worker({ instance_id: 1 }), worker({ instance_id: 1 }))).toBe(true)
    expect(matchesPanel(worker({ instance_id: 2 }), worker({ instance_id: 2 }))).toBe(true)
  })

  it('rejects when both sides carry different instance_ids', () => {
    expect(matchesPanel(worker({ instance_id: 1 }), worker({ instance_id: 2 }))).toBe(false)
    expect(matchesPanel(worker({ instance_id: 2 }), worker({ instance_id: 1 }))).toBe(false)
  })

  // The regression: a legacy persisted panel has no instance_id.
  it('matches when the PANEL carries no instance_id (legacy persisted panel)', () => {
    expect(matchesPanel(worker({ instance_id: 1 }), worker())).toBe(true)
  })

  it('matches when the EVENT carries no instance_id', () => {
    expect(matchesPanel(worker(), worker({ instance_id: 1 }))).toBe(true)
  })

  it('compares instance_label only when both sides carry it', () => {
    expect(
      matchesPanel(worker({ instance_label: 'default#2' }), worker({ instance_label: 'default#2' }))
    ).toBe(true)
    expect(
      matchesPanel(worker({ instance_label: 'default#2' }), worker({ instance_label: 'default' }))
    ).toBe(false)
    expect(matchesPanel(worker({ instance_label: 'default#2' }), worker())).toBe(true)
  })

  it('still rejects a different worker name, with or without identity', () => {
    expect(matchesPanel({ worker_name: 'a' }, { worker_name: 'b' })).toBe(false)
    expect(
      matchesPanel({ worker_name: 'a', instance_id: 1 }, { worker_name: 'b', instance_id: 1 })
    ).toBe(false)
    expect(matchesPanel({ instance_id: 1 }, { instance_id: 1 })).toBe(false)
  })

  it('rejects non-objects', () => {
    expect(matchesPanel(null, worker())).toBe(false)
    expect(matchesPanel(worker(), undefined)).toBe(false)
  })
})

describe('routeEventsToPanels', () => {
  it('routes events to a panel that was backfilled with instance_id 1', () => {
    // Shape produced by App.jsx's normalisePersistedPanel for a legacy entry.
    const panel = { worker_name: 'default', instance_id: 1, instance_label: 'default' }
    const events = [
      { type: 'worker:assistant_message', worker_name: 'default', instance_id: 1 },
      { type: 'worker:assistant_message', worker_name: 'other', instance_id: 1 },
    ]
    const routed = routeEventsToPanels(events, [panel])
    expect(Object.keys(routed)).toEqual(['default#1'])
    expect(routed['default#1']).toHaveLength(1)
    expect(routed['default#1'][0].worker_name).toBe('default')
  })

  it('keeps two instances of the same worker apart when both sides carry ids', () => {
    const panels = [
      { worker_name: 'default', instance_id: 1 },
      { worker_name: 'default', instance_id: 2 },
    ]
    const events = [
      { worker_name: 'default', instance_id: 1, type: 'worker:assistant_message' },
      { worker_name: 'default', instance_id: 2, type: 'worker:assistant_message' },
    ]
    const routed = routeEventsToPanels(events, panels)
    expect(routed['default#1']).toHaveLength(1)
    expect(routed['default#2']).toHaveLength(1)
    expect(routed['default#1'][0].instance_id).toBe(1)
    expect(routed['default#2'][0].instance_id).toBe(2)
  })

  it('instanceKeyOf falls back to #0 for a panel with no instance id', () => {
    expect(instanceKeyOf({ worker_name: 'default' })).toBe('default#0')
    expect(instanceKeyOf({ worker_name: 'default', instance_id: 1 })).toBe('default#1')
    expect(instanceKeyOf({ instance_id: 1 })).toBeNull()
  })
})
