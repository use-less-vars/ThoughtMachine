/*
 * useStore.test.js — Zustand store tests (Phase 0 Frontend Truthfulness Sprint)
 *
 * GREEN (passes today):
 *   - PERMISSION_DEFAULTS: the 6 documented defaults the Permissions tab relies on
 *   - initial state slices
 *   - setSessions / setSessionMode / setTabRunningState / removeSessionState / reset
 *   - foreign-session isolation (per-session keyed maps)
 *
 * RED (intentionally failing until Phase 1 — do NOT delete):
 *   - workerEvents slice contract (per-session event log, dedup, 500 cap,
 *     clearWorkerEvents, removal on removeSessionState).  The backend already
 *     emits worker:* events and SessionTab forwards them via onWorkerEvent;
 *     Phase 1 adds this slice to the store.  These tests drive that work.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import useStore, { PERMISSION_DEFAULTS } from '../useStore';

beforeEach(() => {
  useStore.getState().reset();
});

// ==========================================================================
// PERMISSION_DEFAULTS — truthfulness contract for the Permissions tab
// ==========================================================================
describe('PERMISSION_DEFAULTS', () => {
  it('exposes exactly the 6 documented permission keys', () => {
    expect(Object.keys(PERMISSION_DEFAULTS).sort()).toEqual([
      'container',
      'execution',
      'filesystem',
      'git',
      'network',
      'system',
    ]);
  });

  it('filesystem defaults to read', () => {
    expect(PERMISSION_DEFAULTS.filesystem).toBe('read');
  });

  it('network defaults to banned', () => {
    expect(PERMISSION_DEFAULTS.network).toBe('banned');
  });

  it('container defaults to false', () => {
    expect(PERMISSION_DEFAULTS.container).toBe(false);
  });

  it('system defaults to read', () => {
    expect(PERMISSION_DEFAULTS.system).toBe('read');
  });

  it('git defaults to read', () => {
    expect(PERMISSION_DEFAULTS.git).toBe('read');
  });

  it('execution defaults to banned', () => {
    expect(PERMISSION_DEFAULTS.execution).toBe('banned');
  });
});

// ==========================================================================
// Initial state
// ==========================================================================
describe('initial state', () => {
  it('starts with an empty sessions list', () => {
    expect(useStore.getState().sessions).toEqual([]);
  });

  it('starts with empty sessionModes', () => {
    expect(useStore.getState().sessionModes).toEqual({});
  });

  it('starts with empty tabRunningStates', () => {
    expect(useStore.getState().tabRunningStates).toEqual({});
  });
});

// ==========================================================================
// setSessions
// ==========================================================================
describe('setSessions', () => {
  it('sets the sessions list', () => {
    const sessions = [{ session_id: 's1', name: 'One' }];
    useStore.getState().setSessions(sessions);
    expect(useStore.getState().sessions).toBe(sessions);
  });

  it('replaces the previous list (does not merge)', () => {
    useStore.getState().setSessions([{ session_id: 's1' }]);
    useStore.getState().setSessions([{ session_id: 's2' }]);
    expect(useStore.getState().sessions).toEqual([{ session_id: 's2' }]);
  });
});

// ==========================================================================
// setSessionMode — keyed merge + foreign-session isolation
// ==========================================================================
describe('setSessionMode', () => {
  it('sets the mode for a session', () => {
    useStore.getState().setSessionMode('s1', 'agent');
    expect(useStore.getState().sessionModes.s1).toBe('agent');
  });

  it('merges into existing modes without clobbering other sessions', () => {
    useStore.getState().setSessionMode('s1', 'agent');
    useStore.getState().setSessionMode('s2', 'engineer');
    expect(useStore.getState().sessionModes).toEqual({ s1: 'agent', s2: 'engineer' });
  });

  it('overrides the mode for an existing key', () => {
    useStore.getState().setSessionMode('s1', 'agent');
    useStore.getState().setSessionMode('s1', 'custom');
    expect(useStore.getState().sessionModes.s1).toBe('custom');
  });

  it('does not touch tabRunningStates', () => {
    useStore.getState().setTabRunningState('s1', 'RUNNING');
    useStore.getState().setSessionMode('s1', 'agent');
    expect(useStore.getState().tabRunningStates.s1).toBe('RUNNING');
  });
});

// ==========================================================================
// setTabRunningState — keyed merge
// ==========================================================================
describe('setTabRunningState', () => {
  it('sets the running state for a session', () => {
    useStore.getState().setTabRunningState('s1', 'RUNNING');
    expect(useStore.getState().tabRunningStates.s1).toBe('RUNNING');
  });

  it('merges per-session states independently', () => {
    useStore.getState().setTabRunningState('s1', 'RUNNING');
    useStore.getState().setTabRunningState('s2', 'PAUSED');
    expect(useStore.getState().tabRunningStates).toEqual({ s1: 'RUNNING', s2: 'PAUSED' });
  });
});

// ==========================================================================
// removeSessionState — drops both slices for one session, keeps others
// ==========================================================================
describe('removeSessionState', () => {
  it('drops sessionModes and tabRunningStates for the removed session only', () => {
    useStore.getState().setSessionMode('s1', 'agent');
    useStore.getState().setSessionMode('s2', 'custom');
    useStore.getState().setTabRunningState('s1', 'RUNNING');
    useStore.getState().setTabRunningState('s2', 'IDLE');
    useStore.getState().removeSessionState('s1');
    expect(useStore.getState().sessionModes).toEqual({ s2: 'custom' });
    expect(useStore.getState().tabRunningStates).toEqual({ s2: 'IDLE' });
  });

  it('keeps the sessions list untouched', () => {
    useStore.getState().setSessions([{ session_id: 's1' }]);
    useStore.getState().setSessionMode('s1', 'agent');
    useStore.getState().removeSessionState('s1');
    expect(useStore.getState().sessions).toEqual([{ session_id: 's1' }]);
  });
});

// ==========================================================================
// reset
// ==========================================================================
describe('reset', () => {
  it('restores the initial state', () => {
    useStore.getState().setSessions([{ session_id: 's1' }]);
    useStore.getState().setSessionMode('s1', 'agent');
    useStore.getState().setTabRunningState('s1', 'RUNNING');
    useStore.getState().reset();
    expect(useStore.getState().sessions).toEqual([]);
    expect(useStore.getState().sessionModes).toEqual({});
    expect(useStore.getState().tabRunningStates).toEqual({});
  });
});

// ==========================================================================
// workerEvents (Phase 1 contract — intentionally failing until Phase 1)
// ==========================================================================
// The backend already emits worker:* events, and SessionTab forwards them to
// the parent via onWorkerEvent.  Phase 1 will add a per-session workerEvents
// slice to this store with canonical-type dedup and a 500-event cap.  These
// tests document that contract and FAIL (RED) until Phase 1 lands — they are
// the acceptance tests for that work, do not delete them.
describe('workerEvents (Phase 1 contract — intentionally failing until Phase 1)', () => {
  it('defines workerEvents in the initial state', () => {
    expect(useStore.getState().workerEvents).toBeDefined();
  });

  it('defines addWorkerEvent as a function', () => {
    expect(typeof useStore.getState().addWorkerEvent).toBe('function');
  });

  it('defines clearWorkerEvents as a function', () => {
    expect(typeof useStore.getState().clearWorkerEvents).toBe('function');
  });

  it('appends events per session (isolation)', () => {
    useStore.getState().addWorkerEvent('s1', { type: 'worker_message', timestamp: 'T1' });
    useStore.getState().addWorkerEvent('s1', { type: 'worker_message', timestamp: 'T2' });
    useStore.getState().addWorkerEvent('s2', { type: 'worker_message', timestamp: 'T3' });
    expect(useStore.getState().workerEvents.s1).toHaveLength(2);
    expect(useStore.getState().workerEvents.s2).toHaveLength(1);
  });

  it('dedups canonical event types sharing a timestamp', () => {
    // worker_message / final_response / assistant_message all canonicalize to 'final_response'
    useStore.getState().addWorkerEvent('s1', { type: 'final_response', timestamp: 'T' });
    useStore.getState().addWorkerEvent('s1', { type: 'worker_message', timestamp: 'T' });
    useStore.getState().addWorkerEvent('s1', { type: 'assistant_message', timestamp: 'T' });
    expect(useStore.getState().workerEvents.s1).toHaveLength(1);
  });

  it('caps each session at 500 events (oldest dropped)', () => {
    for (let i = 0; i < 501; i++) {
      useStore.getState().addWorkerEvent('s1', { type: 'worker_message', timestamp: `T${i}` });
    }
    expect(useStore.getState().workerEvents.s1).toHaveLength(500);
    expect(useStore.getState().workerEvents.s1[0].timestamp).not.toBe('T0');
    expect(useStore.getState().workerEvents.s1[499].timestamp).toBe('T500');
  });

  it('clearWorkerEvents empties a session events', () => {
    useStore.getState().addWorkerEvent('s1', { type: 'worker_message', timestamp: 'T1' });
    useStore.getState().clearWorkerEvents('s1');
    expect(useStore.getState().workerEvents.s1).toEqual([]);
  });

  it('removeSessionState also drops workerEvents for that session only', () => {
    useStore.getState().addWorkerEvent('s1', { type: 'worker_message', timestamp: 'T1' });
    useStore.getState().addWorkerEvent('s2', { type: 'worker_message', timestamp: 'T2' });
    useStore.getState().removeSessionState('s1');
    expect(useStore.getState().workerEvents.s1).toBeUndefined();
    expect(useStore.getState().workerEvents.s2).toHaveLength(1);
  });
});
