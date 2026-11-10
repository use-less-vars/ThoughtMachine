/*
 * useStore.test.js — Zustand store tests (Phase 0 Frontend Truthfulness Sprint)
 *
 * GREEN (passes today):
 *   - permissionDefaults: the store mirror of the backend safe defaults
 *     (GET /api/permission-defaults; no hand-copied literal)
 *   - initial state slices
 *   - setSessions / setSessionMode / setTabRunningState / reset
 *   - foreign-session isolation (per-session keyed maps)
 */

import { describe, it, expect, beforeEach } from 'vitest';
import useStore from '../useStore';

beforeEach(() => {
  useStore.getState().reset();
});

// ==========================================================================
// permissionDefaults — store mirror of the backend SINGLE OWNER
// (GET /api/permission-defaults, thoughtmachine.security.SAFE_DEFAULTS).
// The store must NOT carry a hand-copied literal; it starts {} (fail-closed)
// and is filled by setPermissionDefaults / hydratePermissionDefaults.
// ==========================================================================
describe('permissionDefaults', () => {
  it('starts empty (fail-closed) until hydrated from the endpoint', () => {
    expect(useStore.getState().permissionDefaults).toEqual({});
  });

  it('setPermissionDefaults stores the served map verbatim', () => {
    const defaults = {
      git: 'read',
      filesystem: 'read',
      container: false,
      network: 'banned',
      mcp: 'banned',
      host_bash: 'banned',
    };
    useStore.getState().setPermissionDefaults(defaults);
    expect(useStore.getState().permissionDefaults).toEqual(defaults);
  });

  it('setPermissionDefaults falls back to {} for a non-object (fail-closed)', () => {
    useStore.getState().setPermissionDefaults({ git: 'read' });
    expect(useStore.getState().permissionDefaults).toEqual({ git: 'read' });
    useStore.getState().setPermissionDefaults(null);
    expect(useStore.getState().permissionDefaults).toEqual({});
    useStore.getState().setPermissionDefaults([1, 2]);
    expect(useStore.getState().permissionDefaults).toEqual({});
  });

  it('reset clears the mirror back to {}', () => {
    useStore.getState().setPermissionDefaults({ git: 'read' });
    useStore.getState().reset();
    expect(useStore.getState().permissionDefaults).toEqual({});
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
      workspace_path: undefined,
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
      workspace_path: undefined,
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
      workspace_path: undefined,
      isLoaded: true,
    });
  });

  // The server's authoritative `state` string now travels in the session_loaded
  // payload. It must win over the is_running boolean — a PAUSED session reports
  // is_running:false (which would read as IDLE) and a PAUSING session reports
  // is_running:true (which would read as RUNNING).
  it('takes the payload state string over is_running (PAUSED, is_running:false)', () => {
    useStore.getState().receiveSessionLoaded('s1', { state: 'PAUSED', is_running: false });
    expect(useStore.getState().sessionStates.s1.state).toBe('PAUSED');
    expect(useStore.getState().sessionStates.s1.isRunning).toBe(false);
  });

  it('takes the payload state string over is_running (PAUSING, is_running:true)', () => {
    useStore.getState().receiveSessionLoaded('s1', { state: 'PAUSING', is_running: true });
    expect(useStore.getState().sessionStates.s1.state).toBe('PAUSING');
    expect(useStore.getState().sessionStates.s1.isRunning).toBe(true);
  });

  it('falls back to RUNNING when state is null and is_running is true', () => {
    useStore.getState().receiveSessionLoaded('s1', { state: null, is_running: true });
    expect(useStore.getState().sessionStates.s1.state).toBe('RUNNING');
    expect(useStore.getState().sessionStates.s1.isRunning).toBe(true);
  });

  it('falls back to IDLE when the payload has no state key and is_running is false', () => {
    useStore.getState().receiveSessionLoaded('s1', { config: { mode: 'custom' }, is_running: false });
    expect(useStore.getState().sessionStates.s1.state).toBe('IDLE');
    expect(useStore.getState().sessionStates.s1.isRunning).toBe(false);
  });
});

// ==========================================================================
// receiveConfigChanged — authoritative config, PRESERVES omitted providers/tools
// (FIX7: the queued/deferred config_changed payload carries only
//  config/settings/permissions/merged_config, so a full replace would wipe
//  the Tools tab to 0 / 'Loading tool list...')
// ==========================================================================
describe('receiveConfigChanged', () => {
  it('preserves previous providers/tools when the payload omits them', () => {
    useStore.getState().receiveSessionLoaded('s1', { providers: [{ id: 'p1' }], tools: ['read_file'] });
    useStore.getState().receiveConfigChanged('s1', { config: { mode: 'agent' } });
    expect(useStore.getState().sessionConfigs.s1).toEqual({
      config: { mode: 'agent' },
      permissions: null,
      providers: [{ id: 'p1' }],
      tools: ['read_file'],
      workspace_path: undefined,
      isLoaded: true,
    });
  });

  it('replaces providers/tools when the payload DOES carry them', () => {
    useStore.getState().receiveSessionLoaded('s1', { providers: [{ id: 'p1' }], tools: ['read_file'] });
    useStore.getState().receiveConfigChanged('s1', {
      config: { mode: 'agent' },
      providers: [{ id: 'p2' }],
      tools: [{ name: 'glob', enabled: true }],
    });
    expect(useStore.getState().sessionConfigs.s1).toEqual({
      config: { mode: 'agent' },
      permissions: null,
      providers: [{ id: 'p2' }],
      tools: ['glob'],
      workspace_path: undefined,
      isLoaded: true,
    });
  });
});

// ==========================================================================
// receiveConfigQueued / receiveConfigApplyFailed — FIX7 queued-apply state
// ==========================================================================
describe('receiveConfigQueued', () => {
  it('marks the entry as configQueued without clobbering anything', () => {
    useStore.getState().receiveSessionLoaded('s1', { providers: [{ id: 'p1' }], tools: ['read_file'] });
    useStore.getState().receiveConfigQueued('s1');
    const entry = useStore.getState().sessionConfigs.s1;
    expect(entry.configQueued).toBe(true);
    expect(entry.providers).toEqual([{ id: 'p1' }]);
    expect(entry.tools).toEqual(['read_file']);
    expect(entry.config).toBeNull();
  });

  it('creates the entry when missing', () => {
    useStore.getState().receiveConfigQueued('s1');
    expect(useStore.getState().sessionConfigs.s1.configQueued).toBe(true);
  });
});

describe('receiveConfigApplyFailed', () => {
  it('clears configQueued and stores the applyFailed text', () => {
    useStore.getState().receiveConfigQueued('s1');
    useStore.getState().receiveConfigApplyFailed('s1', '⚠ Failed to apply queued config: boom');
    const entry = useStore.getState().sessionConfigs.s1;
    expect(entry.configQueued).toBe(false);
    expect(entry.applyFailed).toBe('⚠ Failed to apply queued config: boom');
  });

  it('defaults the text when missing', () => {
    useStore.getState().receiveConfigApplyFailed('s1');
    expect(useStore.getState().sessionConfigs.s1.applyFailed).toBe('Config apply failed');
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
// receiveConversationChanged — explicit merge mode + client-only notice
// survival (d2-cold-start-status-notice-wiped-by-session-load).
//
// The cold-start CASE-3 `status_message` notice is a CLIENT-ONLY bubble: the
// server never echoes it back in a `conversation_changed` payload. These tests
// pin the merge contract that lets it survive a server-driven replace.
// ==========================================================================
describe('receiveConversationChanged — explicit merge mode', () => {
  it('append mode keeps existing history and adds incoming messages at the tail', () => {
    useStore.getState().receiveConversationChanged('s1', [{ role: 'user', content: 'hist' }]);
    useStore.getState().receiveConversationChanged(
      's1',
      [{ role: 'system', content: 'notice', is_system_notification: true, client_only: true }],
      { mode: 'append' }
    );
    expect(useStore.getState().sessionMessages.s1).toEqual([
      { role: 'user', content: 'hist' },
      { role: 'system', content: 'notice', is_system_notification: true, client_only: true },
    ]);
  });

  it('prepend mode inserts older messages before the existing history', () => {
    useStore.getState().receiveConversationChanged('s1', [{ role: 'assistant', content: 'recent' }]);
    useStore.getState().receiveConversationChanged('s1', [{ role: 'user', content: 'older' }], {
      mode: 'prepend',
    });
    expect(useStore.getState().sessionMessages.s1).toEqual([
      { role: 'user', content: 'older' },
      { role: 'assistant', content: 'recent' },
    ]);
  });

  it('replace mode (default) discards the previous array', () => {
    useStore.getState().receiveConversationChanged('s1', [{ role: 'user', content: 'old' }]);
    useStore.getState().receiveConversationChanged('s1', [{ role: 'assistant', content: 'new' }]);
    expect(useStore.getState().sessionMessages.s1).toEqual([{ role: 'assistant', content: 'new' }]);
  });

  it('replace + preserveClientOnly keeps client-only notices the payload omits', () => {
    useStore.getState().receiveConversationChanged(
      's1',
      [{ role: 'system', content: 'cold-start notice', is_system_notification: true, client_only: true }],
      { mode: 'append' }
    );
    useStore.getState().receiveConversationChanged('s1', [{ role: 'user', content: 'server line' }], {
      mode: 'replace',
      preserveClientOnly: true,
    });
    expect(useStore.getState().sessionMessages.s1).toEqual([
      { role: 'user', content: 'server line' },
      { role: 'system', content: 'cold-start notice', is_system_notification: true, client_only: true },
    ]);
  });

  it('replace WITHOUT preserveClientOnly drops client-only notices (session_cleared semantics)', () => {
    useStore.getState().receiveConversationChanged(
      's1',
      [{ role: 'system', content: 'cold-start notice', is_system_notification: true, client_only: true }],
      { mode: 'append' }
    );
    useStore.getState().receiveConversationChanged('s1', [], { mode: 'replace' });
    expect(useStore.getState().sessionMessages.s1).toEqual([]);
  });

  it('preserveClientOnly does NOT resurrect already-replaced server messages', () => {
    useStore.getState().receiveConversationChanged('s1', [{ role: 'user', content: 'stale' }]);
    useStore.getState().receiveConversationChanged('s1', [{ role: 'assistant', content: 'fresh' }], {
      mode: 'replace',
      preserveClientOnly: true,
    });
    expect(useStore.getState().sessionMessages.s1).toEqual([{ role: 'assistant', content: 'fresh' }]);
  });
});

// ==========================================================================
// receiveConversationChanged — tool call / outcome folding by tool_call_id
// (duplicate-tool-bubble-in-chat).
//
// A tool call and its outcome arrive as two rows sharing one backend
// `tool_call_id`, in ANY order and across ANY of the three merge modes. The
// conversation slice must hold exactly ONE row for that id, at the call's
// position, with `content` = the name/args JSON (what MessageBubble parses) and
// `result` = the outcome text (what MessageBubble renders below it).
// Rows without an id, and rows with DIFFERENT ids (even when name/args are
// byte-identical), must never merge.
// ==========================================================================
describe('receiveConversationChanged — tool call/outcome folding', () => {
  const CALL_ARGS = JSON.stringify({ name: 'read_file', arguments: { path: 'a.txt' } });
  const callRow = (id, extra) => ({ role: 'tool_call', content: CALL_ARGS, tool_call_id: id, ...(extra || {}) });
  const resultRow = (id, text, extra) => ({ role: 'tool_result', content: text, tool_call_id: id, ...(extra || {}) });
  const rows = (sid = 's1') => useStore.getState().sessionMessages[sid] || [];

  it('a: a streamed tool call + the authoritative replace of that call = one entry', () => {
    useStore.getState().receiveConversationChanged('s1', [{ role: 'user', content: 'read a.txt' }], {
      mode: 'append',
    });
    useStore.getState().receiveConversationChanged('s1', [callRow('t1')], { mode: 'append' });
    expect(rows()).toHaveLength(2);

    // Full history replace re-sending the very same call row.
    useStore.getState().receiveConversationChanged(
      's1',
      [{ role: 'user', content: 'read a.txt' }, callRow('t1')],
      { mode: 'replace', preserveClientOnly: true }
    );

    expect(rows()).toHaveLength(2);
    expect(rows().filter((m) => m.tool_call_id === 't1')).toHaveLength(1);
    expect(rows()[1]).toEqual(callRow('t1'));
  });

  it('b: the same tool 3x with identical args but DISTINCT ids stays 3 entries', () => {
    for (const id of ['t1', 't2', 't3']) {
      useStore.getState().receiveConversationChanged('s1', [callRow(id)], { mode: 'append' });
    }
    expect(rows()).toHaveLength(3);
    expect(rows().map((m) => m.tool_call_id)).toEqual(['t1', 't2', 't3']);
    expect(rows().map((m) => m.content)).toEqual([CALL_ARGS, CALL_ARGS, CALL_ARGS]);

    // Distinct ids never merge even inside ONE replace payload.
    useStore.getState().receiveConversationChanged('s1', [callRow('t4'), callRow('t5'), callRow('t6')], {
      mode: 'replace',
    });
    expect(rows()).toHaveLength(3);
    expect(rows().map((m) => m.tool_call_id)).toEqual(['t4', 't5', 't6']);
  });

  it('c: a tool_result folds IN PLACE into its call (1 entry, position kept, outcome in `result`)', () => {
    useStore.getState().receiveConversationChanged('s1', [{ role: 'user', content: 'read a.txt' }], {
      mode: 'append',
    });
    useStore.getState().receiveConversationChanged('s1', [callRow('t1')], { mode: 'append' });
    useStore.getState().receiveConversationChanged('s1', [resultRow('t1', 'file contents: hello')], {
      mode: 'append',
    });

    const msgs = rows();
    expect(msgs).toHaveLength(2);
    expect(msgs[0]).toEqual({ role: 'user', content: 'read a.txt' });
    expect(msgs[1].role).toBe('tool_call');
    expect(msgs[1].tool_call_id).toBe('t1');
    // MessageBubble: ToolCallContent(content) + TruncatableContent(result).
    expect(msgs[1].content).toBe(CALL_ARGS);
    expect(msgs[1].result).toBe('file contents: hello');
  });

  it('c2: one replace payload carrying BOTH phases folds to one bubble, order preserved', () => {
    useStore.getState().receiveConversationChanged(
      's1',
      [
        { role: 'user', content: 'read a.txt' },
        callRow('t1'),
        resultRow('t1', 'file contents: hello'),
        { role: 'assistant', content: 'done' },
      ],
      { mode: 'replace', preserveClientOnly: true }
    );

    const msgs = rows();
    expect(msgs).toHaveLength(3);
    expect(msgs[1].role).toBe('tool_call');
    expect(msgs[1].content).toBe(CALL_ARGS);
    expect(msgs[1].result).toBe('file contents: hello');
    expect(msgs[2]).toEqual({ role: 'assistant', content: 'done' });
  });

  it('d: a tool_result whose id matches nothing stays its own row and corrupts nothing', () => {
    useStore.getState().receiveConversationChanged('s1', [{ role: 'user', content: 'hi' }, callRow('t1')], {
      mode: 'replace',
    });
    useStore.getState().receiveConversationChanged('s1', [resultRow('zzz-orphan', 'orphan outcome')], {
      mode: 'append',
    });

    const msgs = rows();
    expect(msgs).toHaveLength(3);
    expect(msgs[0]).toEqual({ role: 'user', content: 'hi' });
    expect(msgs[1]).toEqual(callRow('t1'));
    expect(msgs[1].result).toBeUndefined();
    expect(msgs[2]).toEqual(resultRow('zzz-orphan', 'orphan outcome'));
  });

  it('e: rows without a tool_call_id behave exactly as before (never merged)', () => {
    useStore.getState().receiveConversationChanged('s1', [{ role: 'user', content: 'a' }], { mode: 'append' });
    useStore.getState().receiveConversationChanged('s1', [{ role: 'user', content: 'a' }], { mode: 'append' });
    expect(rows()).toEqual([
      { role: 'user', content: 'a' },
      { role: 'user', content: 'a' },
    ]);

    // Id-less tool rows are NOT folded either — two identical ones stay two.
    const idless = { role: 'tool_call', content: CALL_ARGS };
    useStore.getState().receiveConversationChanged('s1', [{ ...idless }, { ...idless }], { mode: 'replace' });
    expect(rows()).toHaveLength(2);
    expect(rows().every((m) => m.result === undefined)).toBe(true);
  });

  it('f: prepend puts incoming rows first AND still folds by id (call after result)', () => {
    // Newer page already loaded: the outcome is on screen without its call.
    useStore.getState().receiveConversationChanged(
      's1',
      [resultRow('t1', 'file contents: hello'), { role: 'assistant', content: 'done' }],
      { mode: 'replace' }
    );
    expect(rows()).toHaveLength(2);

    useStore.getState().receiveConversationChanged(
      's1',
      [{ role: 'user', content: 'older line' }, callRow('t1')],
      { mode: 'prepend' }
    );

    const msgs = rows();
    expect(msgs).toHaveLength(3);
    expect(msgs[0]).toEqual({ role: 'user', content: 'older line' });
    expect(msgs[1].role).toBe('tool_call');
    expect(msgs[1].tool_call_id).toBe('t1');
    expect(msgs[1].content).toBe(CALL_ARGS);
    expect(msgs[1].result).toBe('file contents: hello');
    expect(msgs[2]).toEqual({ role: 'assistant', content: 'done' });
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
      workspace_path: undefined,
      isLoaded: true,
    });
    expect(st.sessionMessages.s2).toBeUndefined();
    expect(st.sessionStates.s2).toEqual({ isRunning: false, state: 'IDLE', contextLength: 0, tokensIn: 0, tokensOut: 0 });
  });
});


// ==========================================================================
// session lifecycle — register → load(running) → state → conversation →
// tokens → idle → remove (full purge across all 7 slices)
// ==========================================================================
describe('session lifecycle', () => {
  it('walks a full lifecycle and purges all slices on removeSession', () => {
    // 1. register — empty per-session entries
    useStore.getState().registerSession('s1');

    // 2. session_loaded with is_running: true — authoritative snapshot
    useStore.getState().receiveSessionLoaded('s1', { config: { mode: 'custom' }, is_running: true });
    let st = useStore.getState();
    expect(st.sessionConfigs.s1.isLoaded).toBe(true);
    expect(st.sessionConfigs.s1.config).toEqual({ mode: 'custom' });
    expect(st.sessionStates.s1.state).toBe('RUNNING');
    expect(st.sessionStates.s1.isRunning).toBe(true);

    // 3. state_changed RUNNING
    useStore.getState().receiveStateChanged('s1', 'RUNNING');
    expect(useStore.getState().sessionStates.s1.state).toBe('RUNNING');
    expect(useStore.getState().sessionStates.s1.isRunning).toBe(true);

    // 4. conversation_changed
    useStore.getState().receiveConversationChanged('s1', [{ role: 'user', content: 'hi' }]);
    expect(useStore.getState().sessionMessages.s1).toEqual([{ role: 'user', content: 'hi' }]);

    // 5. tokens_updated — tokens live in sessionStates
    useStore.getState().receiveTokensUpdated('s1', { input: 100, output: 50 });
    st = useStore.getState();
    expect(st.sessionStates.s1.tokensIn).toBe(100);
    expect(st.sessionStates.s1.tokensOut).toBe(50);
    expect(st.sessionStates.s1.state).toBe('RUNNING');

    // 6. state_changed IDLE — session finished
    useStore.getState().receiveStateChanged('s1', 'IDLE');
    st = useStore.getState();
    expect(st.sessionStates.s1.state).toBe('IDLE');
    expect(st.sessionStates.s1.isRunning).toBe(false);

    // 7. removeSession — full purge
    useStore.getState().removeSession('s1');
    st = useStore.getState();
    expect(st.sessionConfigs.s1).toBeUndefined();
    expect(st.sessionMessages.s1).toBeUndefined();
    expect(st.sessionStates.s1).toBeUndefined();
    expect(st.sessionErrors.s1).toBeUndefined();
    expect(st.sessionModes.s1).toBeUndefined();
    expect(st.tabRunningStates.s1).toBeUndefined();
    expect(st.sessions.some((s) => s.session_id === 's1')).toBe(false);

    // Deep-equal against the store's real empty initial slices (getState()
    // includes action fns, so only the 7 data slices are compared).
    const {
      sessions, sessionModes, tabRunningStates,
      sessionConfigs, sessionMessages, sessionStates, sessionErrors,
    } = useStore.getState();
    expect({
      sessions, sessionModes, tabRunningStates,
      sessionConfigs, sessionMessages, sessionStates, sessionErrors,
    }).toEqual({
      sessions: [],
      sessionModes: {},
      tabRunningStates: {},
      sessionConfigs: {},
      sessionMessages: {},
      sessionStates: {},
      sessionErrors: {},
    });
  });
});
