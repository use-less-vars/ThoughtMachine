/*
 * useStore.test.js — Zustand store tests (Phase 0 Frontend Truthfulness Sprint)
 *
 * GREEN (passes today):
 *   - PERMISSION_DEFAULTS: the 6 documented defaults the Permissions tab relies on
 *   - initial state slices
 *   - setSessions / setSessionMode / setTabRunningState / reset
 *   - foreign-session isolation (per-session keyed maps)
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
// registerSession — per-session slice initialization (no overwrite)
// ==========================================================================
describe('registerSession', () => {
  it('creates empty per-session entries in all three slices', () => {
    useStore.getState().registerSession('s1');
    const st = useStore.getState();
    expect(st.sessionConfigs.s1).toEqual({ config: null, permissions: null, providers: [], tools: [], isLoaded: false });
    expect(st.sessionMessages.s1).toEqual([]);
    expect(st.sessionStates.s1).toEqual({ isRunning: false, state: null, contextLength: 0, tokensIn: 0, tokensOut: 0 });
  });

  it('does NOT overwrite an existing session entry', () => {
    useStore.getState().receiveSessionLoaded('s1', { config: { mode: 'custom' } });
    useStore.getState().registerSession('s1');
    const st = useStore.getState();
    expect(st.sessionConfigs.s1).toEqual({
      config: { mode: 'custom' },
      permissions: null,
      providers: [],
      tools: [],
      isLoaded: true,
    });
  });
});

// ==========================================================================
// removeSession — drops a session from all seven slices, keeps others
// ==========================================================================
describe('removeSession', () => {
  it('removes a session from all seven slices and leaves other sessions intact', () => {
    useStore.getState().registerSession('s1');
    useStore.getState().registerSession('s2');
    useStore.getState().receiveSessionLoaded('s1', {});
    useStore.getState().receiveConversationChanged('s1', [{ role: 'user', content: 'hi' }]);
    useStore.getState().receiveStateChanged('s1', 'RUNNING');
    useStore.getState().setSessionError('s1', 'boom');
    useStore.getState().setSessionMode('s1', 'agent');
    useStore.getState().setTabRunningState('s1', 'RUNNING');
    useStore.getState().setSessions([
      { session_id: 's1', name: 'One' },
      { session_id: 's2', name: 'Two' },
    ]);
    useStore.getState().receiveSessionLoaded('s2', {});
    useStore.getState().removeSession('s1');
    const st = useStore.getState();
    expect(st.sessionConfigs.s1).toBeUndefined();
    expect(st.sessionMessages.s1).toBeUndefined();
    expect(st.sessionStates.s1).toBeUndefined();
    expect(st.sessionErrors.s1).toBeUndefined();
    expect(st.sessionModes.s1).toBeUndefined();
    expect(st.tabRunningStates.s1).toBeUndefined();
    expect(st.sessions.map((s) => s.session_id)).toEqual(['s2']);
    expect(st.sessionConfigs.s2).toBeDefined();
    expect(st.sessionMessages.s2).toEqual([]);
  });
});

// ==========================================================================
// receiveSessionLoaded — snapshot from the session_loaded event
// ==========================================================================
describe('receiveSessionLoaded', () => {
  it('stores config/permissions/providers/tools and marks isLoaded', () => {
    useStore.getState().receiveSessionLoaded('s1', {
      type: 'session_loaded',
      session_id: 's1',
      config: { mode: 'custom' },
      permissions: { network: 'banned' },
      providers: [{ id: 'p1' }],
      tools: [{ name: 'read_file' }, 'glob'],
    });
    expect(useStore.getState().sessionConfigs.s1).toEqual({
      config: { mode: 'custom' },
      permissions: { network: 'banned' },
      providers: [{ id: 'p1' }],
      tools: ['read_file', 'glob'],
      isLoaded: true,
    });
  });

  it('normalizes object tools to names and tolerates missing fields', () => {
    useStore.getState().receiveSessionLoaded('s1', { type: 'session_loaded' });
    expect(useStore.getState().sessionConfigs.s1).toEqual({
      config: null,
      permissions: null,
      providers: [],
      tools: [],
      isLoaded: true,
    });
  });
});

// ==========================================================================
// receiveConfigChanged — REPLACES, does not merge
// ==========================================================================
describe('receiveConfigChanged', () => {
  it('replaces the whole entry (no merge with previous providers/tools)', () => {
    useStore.getState().receiveSessionLoaded('s1', { providers: [{ id: 'p1' }], tools: ['read_file'] });
    useStore.getState().receiveConfigChanged('s1', { config: { mode: 'agent' } });
    expect(useStore.getState().sessionConfigs.s1).toEqual({
      config: { mode: 'agent' },
      permissions: null,
      providers: [],
      tools: [],
      isLoaded: true,
    });
  });
});

// ==========================================================================
// receiveProvidersList — providers array without clobbering the entry
// ==========================================================================
describe('receiveProvidersList', () => {
  it('stores the providers array without clobbering config', () => {
    useStore.getState().receiveSessionLoaded('s1', { config: { mode: 'custom' } });
    useStore.getState().receiveProvidersList('s1', [{ id: 'p1' }, { id: 'p2' }]);
    const entry = useStore.getState().sessionConfigs.s1;
    expect(entry.providers).toEqual([{ id: 'p1' }, { id: 'p2' }]);
    expect(entry.config).toEqual({ mode: 'custom' });
  });

  it('creates the entry when missing', () => {
    useStore.getState().receiveProvidersList('s1', [{ id: 'p1' }]);
    expect(useStore.getState().sessionConfigs.s1.providers).toEqual([{ id: 'p1' }]);
  });
});

// ==========================================================================
// receiveToolsList — normalization of string/object entries
// ==========================================================================
describe('receiveToolsList', () => {
  it('normalizes mixed string/object tools and preserves the rest of the entry', () => {
    useStore.getState().receiveSessionLoaded('s1', { config: { mode: 'custom' }, providers: [{ id: 'p1' }] });
    useStore.getState().receiveToolsList('s1', [{ name: 'read_file', enabled: true }, 'glob']);
    const entry = useStore.getState().sessionConfigs.s1;
    expect(entry.tools).toEqual(['read_file', 'glob']);
    expect(entry.config).toEqual({ mode: 'custom' });
    expect(entry.providers).toEqual([{ id: 'p1' }]);
  });
});

// ==========================================================================
// receiveConversationChanged — replaces the messages array
// ==========================================================================
describe('receiveConversationChanged', () => {
  it('replaces the messages array', () => {
    useStore.getState().receiveConversationChanged('s1', [{ role: 'user', content: 'a' }]);
    useStore.getState().receiveConversationChanged('s1', [
      { role: 'user', content: 'b' },
      { role: 'assistant', content: 'c' },
    ]);
    expect(useStore.getState().sessionMessages.s1).toEqual([
      { role: 'user', content: 'b' },
      { role: 'assistant', content: 'c' },
    ]);
  });
});

// ==========================================================================
// receiveStateChanged — derives isRunning, merges into session state
// ==========================================================================
describe('receiveStateChanged', () => {
  it('derives isRunning from the state string', () => {
    useStore.getState().receiveStateChanged('s1', 'RUNNING');
    expect(useStore.getState().sessionStates.s1.isRunning).toBe(true);
    useStore.getState().receiveStateChanged('s1', 'PAUSED');
    expect(useStore.getState().sessionStates.s1.isRunning).toBe(false);
    useStore.getState().receiveStateChanged('s1', 'WAITING_FOR_USER');
    expect(useStore.getState().sessionStates.s1.isRunning).toBe(false);
  });

  it('preserves contextLength/tokens on subsequent state changes', () => {
    useStore.getState().updateContextLength('s1', 120);
    useStore.getState().receiveTokensUpdated('s1', { input: 100, output: 50 });
    useStore.getState().receiveStateChanged('s1', 'RUNNING');
    expect(useStore.getState().sessionStates.s1).toEqual({
      isRunning: true,
      state: 'RUNNING',
      contextLength: 120,
      tokensIn: 100,
      tokensOut: 50,
    });
  });
});

// ==========================================================================
// updateContextLength — merges contextLength into session state
// ==========================================================================
describe('updateContextLength', () => {
  it('merges contextLength into the session state', () => {
    useStore.getState().receiveStateChanged('s1', 'RUNNING');
    useStore.getState().updateContextLength('s1', 500);
    const st = useStore.getState().sessionStates.s1;
    expect(st.contextLength).toBe(500);
    expect(st.isRunning).toBe(true);
  });

  it('creates the entry when missing', () => {
    useStore.getState().updateContextLength('s2', 300);
    expect(useStore.getState().sessionStates.s2.contextLength).toBe(300);
  });
});

// ==========================================================================
// receiveTokensUpdated — flagged extension (tokens live in sessionStates)
// ==========================================================================
describe('receiveTokensUpdated', () => {
  it('stores input/output tokens and preserves other session state', () => {
    useStore.getState().receiveStateChanged('s1', 'RUNNING');
    useStore.getState().receiveTokensUpdated('s1', { type: 'tokens_updated', input: 100, output: 50 });
    const st = useStore.getState().sessionStates.s1;
    expect(st.tokensIn).toBe(100);
    expect(st.tokensOut).toBe(50);
    expect(st.isRunning).toBe(true);
  });
});

// ==========================================================================
// New-slice cross-session isolation
// ==========================================================================
describe('new slice cross-session isolation', () => {
  it('keeps s2 data untouched when s1 is registered/removed', () => {
    useStore.getState().receiveSessionLoaded('s1', { config: { mode: 'custom' } });
    useStore.getState().receiveSessionLoaded('s2', { config: { mode: 'engineer' } });
    useStore.getState().receiveConversationChanged('s1', [{ role: 'user', content: 'x' }]);
    useStore.getState().receiveStateChanged('s1', 'RUNNING');
    useStore.getState().removeSession('s1');
    const st = useStore.getState();
    expect(st.sessionConfigs.s2).toEqual({
      config: { mode: 'engineer' },
      permissions: null,
      providers: [],
      tools: [],
      isLoaded: true,
    });
    expect(st.sessionMessages.s2).toBeUndefined();
    expect(st.sessionStates.s2).toEqual({ isRunning: false, state: 'IDLE', contextLength: 0, tokensIn: 0, tokensOut: 0 });
  });
});

