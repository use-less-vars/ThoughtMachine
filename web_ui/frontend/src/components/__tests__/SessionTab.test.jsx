// @vitest-environment jsdom
/*
 * SessionTab.test.jsx
 *
 * Phase 0 — Frontend Truthfulness Sprint.
 * Tests the self-contained session tab: WebSocket lifecycle, command
 * emission (new_session / load_session / security_response), event
 * handling (state_changed, tokens_updated, conversation_changed,
 * config_changed, session_loaded, security_prompt, worker:* forwarding),
 * and auto-reconnect after an unexpected close.
 *
 * NOTE: These tests were written from static analysis of SessionTab.jsx
 * (and its children) and have NOT yet been executed in this environment
 * (DockerCodeRunner broken this session; node_modules not installed).
 * Outcomes are PREDICTED — run `cd web_ui/frontend && npm install && npm test`
 * to verify.
 */

import React from 'react';
import {
  describe,
  it,
  expect,
  vi,
  beforeEach,
  afterEach,
} from 'vitest';
import {
  render,
  screen,
  fireEvent,
  cleanup,
  act,
  waitFor,
} from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import SessionTab from '../SessionTab';
import useStore from '../../store/useStore';

// ────────────────────────────────────────────────────────────────────────────
// Mock WebSocket (SessionTab uses the global WebSocket.OPEN constant)
// ────────────────────────────────────────────────────────────────────────────
class MockWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  static instances = [];

  constructor(url) {
    this.url = url;
    this.readyState = MockWebSocket.CONNECTING;
    this.sent = [];
    this.onopen = null;
    this.onmessage = null;
    this.onclose = null;
    this.onerror = null;
    MockWebSocket.instances.push(this);
  }

  send(data) {
    this.sent.push(data);
  }

  close(code = 1001) {
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.({ code });
  }

  open() {
    this.readyState = MockWebSocket.OPEN;
    this.onopen?.({});
  }

  receive(msg) {
    this.onmessage?.({ data: JSON.stringify(msg) });
  }
}

// ─── Mock ResizeObserver (jsdom lacks it; ChatPanel constructs one on mount) ───
class MockResizeObserver {
  constructor(callback) {
    this.callback = callback;
  }
  observe() {}
  unobserve() {}
  disconnect() {}
}

// ────────────────────────────────────────────────────────────────────────────
// Test helpers
// ────────────────────────────────────────────────────────────────────────────
function lastWs() {
  return MockWebSocket.instances[MockWebSocket.instances.length - 1];
}

function sentCommands(ws) {
  return ws.sent.map((data) => JSON.parse(data));
}

async function connectWs() {
  await waitFor(() => {
    expect(MockWebSocket.instances.length).toBeGreaterThan(0);
  });
  const ws = lastWs();
  await act(async () => ws.open());
  return ws;
}

function renderTab(props = {}) {
  const onClose = vi.fn();
  const onNewSession = vi.fn();
  const onOpenNewTab = vi.fn();
  const onSessionSaved = vi.fn();
  const onRegister = vi.fn();
  const onSessionRenamed = vi.fn();
  const onWorkerEvent = vi.fn();
  const onLoggingConfigChanged = vi.fn();
  const onSessionAdopted = vi.fn();
  const mergedProps = {
    sessionId: null,
    tabId: 'tab-1',
    hubReady: true,
    staggerMs: 0,
    loadOnConnect: true,
    isActive: true,
    onClose,
    onNewSession,
    onOpenNewTab,
    onSessionSaved,
    onRegister,
    onSessionRenamed,
    onWorkerEvent,
    onLoggingConfigChanged,
    onSessionAdopted,
    ...props,
  };
  const utils = render(<SessionTab {...mergedProps} />);
  return {
    onClose,
    onNewSession,
    onOpenNewTab,
    onSessionSaved,
    onRegister,
    onSessionRenamed,
    onWorkerEvent,
    onLoggingConfigChanged,
    onSessionAdopted,
    rerender: (nextProps) =>
      utils.rerender(<SessionTab {...{ ...mergedProps, ...nextProps }} />),
  };
}

// ────────────────────────────────────────────────────────────────────────────
// Setup / teardown
// ────────────────────────────────────────────────────────────────────────────
beforeEach(() => {
  MockWebSocket.instances = [];
  vi.stubGlobal('WebSocket', MockWebSocket);
  vi.stubGlobal('ResizeObserver', MockResizeObserver);
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true,
    json: async () => ({ tools: [] }),
    text: async () => '',
  })));
  useStore.getState().reset();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ────────────────────────────────────────────────────────────────────────────
// WebSocket lifecycle
// ────────────────────────────────────────────────────────────────────────────
describe('SessionTab — WebSocket lifecycle', () => {
  it('opens a WebSocket to the backend /ws endpoint', async () => {
    renderTab();
    const ws = await connectWs();
    expect(ws.url).toMatch(/^ws:\/\//);
    expect(ws.url.endsWith('/ws')).toBe(true);
  });

  it('fresh tab sends new_session (mode custom), get_providers and get_available_tools on open', async () => {
    renderTab();
    const ws = await connectWs();
    const commands = sentCommands(ws).map((c) => c.command);
    expect(commands).toContain('new_session');
    expect(commands).toContain('get_providers');
    expect(commands).toContain('get_available_tools');
    const newSession = sentCommands(ws).find((c) => c.command === 'new_session');
    expect(newSession.mode).toBe('custom');
  });

  it('existing session sends load_session with its session_id', async () => {
    renderTab({ sessionId: 'sess-1' });
    const ws = await connectWs();
    // load_session is deliberately deferred by one tick on open (SessionTab.jsx:
    // "Defer load_session by one tick so the parent's synchronous setup
    // (handlers, etc.) completes first"), so it arrives in a later macrotask
    // than the synchronous new_session/get_providers sends — poll for it.
    await waitFor(() => {
      const load = sentCommands(ws).find((c) => c.command === 'load_session');
      expect(load).toBeTruthy();
      expect(load.session_id).toBe('sess-1');
    });
  });

  it('does not send load_session twice on first connect', async () => {
    renderTab({ sessionId: 'sess-1' });
    const ws = await connectWs();
    // Duplicate onopen on the same socket (StrictMode / backend race) must
    // not double-send: loadSentRef dedupes per connection (Fix 4b).
    act(() => ws.open());
    act(() => ws.open());
    await waitFor(() => {
      const loads = sentCommands(ws).filter((c) => c.command === 'load_session');
      expect(loads).toHaveLength(1);
      expect(loads[0].session_id).toBe('sess-1');
    });
  });

  it('inactive tab with existing session still sends load_session but shows deferred placeholder', async () => {
    renderTab({ sessionId: 'sess-1', loadOnConnect: false });
    await connectWs();
    // Fix 4b: inactive tabs still send load_session on connect (session state
    // may have changed while disconnected), but keep the placeholder until
    // the data arrives (or the tab is activated).
    expect(screen.getByText('Click tab to load conversation')).toBeInTheDocument();
    const ws = lastWs();
    await waitFor(() => {
      const loads = sentCommands(ws).filter((c) => c.command === 'load_session');
      expect(loads).toHaveLength(1);
      expect(loads[0].session_id).toBe('sess-1');
    });
  });

  it('keeps the deferred placeholder on activation and clears it when the load response arrives', async () => {
    const { rerender } = renderTab({ sessionId: 'sess-1', loadOnConnect: false, isActive: false });
    const ws = await connectWs();
    // Fix 4b: inactive tabs still send load_session on connect.
    await waitFor(() => {
      const loads = sentCommands(ws).filter((c) => c.command === 'load_session');
      expect(loads).toHaveLength(1);
      expect(loads[0].session_id).toBe('sess-1');
    });
    expect(screen.getByText('Click tab to load conversation')).toBeInTheDocument();
    // Activating the tab with the socket still OPEN neither reconnects nor
    // resends load_session nor clears the placeholder (the deferred-clear
    // effect is driven by the server response, not by isActive alone).
    rerender({ isActive: true });
    expect(screen.getByText('Click tab to load conversation')).toBeInTheDocument();
    expect(sentCommands(ws).filter((c) => c.command === 'load_session')).toHaveLength(1);
    // The placeholder clears only once the backend responds with the session.
    act(() =>
      ws.receive({
        type: 'session_loaded',
        session_id: 'sess-1',
        session_name: 'My Session',
        // Fix 4a: backend embeds config in session_loaded so the UI renders immediately
        config: { mode: 'custom', workspace_path: '/tmp/x' },
      })
    );
    expect(await screen.findByText('My Session')).toBeInTheDocument();
    expect(screen.queryByText('Click tab to load conversation')).not.toBeInTheDocument();
  });

  it('reconnects after an unexpected close (code != 1001)', async () => {
    vi.useFakeTimers();
    try {
      renderTab();
      // The mount effect always defers the first connection via
      // setTimeout(connectSessionWs, staggerMs) — even with staggerMs=0 —
      // so flush the timer before grabbing the instance (fake timers).
      act(() => vi.advanceTimersByTime(0));
      const ws = lastWs();
      expect(ws).toBeTruthy();
      act(() => ws.open());
      const before = MockWebSocket.instances.length;
      act(() => ws.close(1006));
      act(() => vi.advanceTimersByTime(5000));
      expect(MockWebSocket.instances.length).toBeGreaterThan(before);
    } finally {
      vi.useRealTimers();
    }
  });

  it('resends load_session after an unexpected close (forced reload on reconnect)', async () => {
    vi.useFakeTimers();
    try {
      renderTab({ sessionId: 'sess-1' });
      // Flush the mount-effect stagger timer (see reconnect test above).
      act(() => vi.advanceTimersByTime(0));
      const ws1 = lastWs();
      act(() => ws1.open());
      // Flush the one-tick load_session deferral inside onopen.
      act(() => vi.advanceTimersByTime(0));
      expect(sentCommands(ws1).filter((c) => c.command === 'load_session')).toHaveLength(1);

      const before = MockWebSocket.instances.length;
      act(() => ws1.close(1006));
      act(() => vi.advanceTimersByTime(5000));
      expect(MockWebSocket.instances.length).toBeGreaterThan(before);

      // Fix 4b: onclose resets loadSentRef, so the reconnected socket must
      // send load_session again (forced reload on reconnect).
      const ws2 = lastWs();
      act(() => ws2.open());
      act(() => vi.advanceTimersByTime(0));
      const loads2 = sentCommands(ws2).filter((c) => c.command === 'load_session');
      expect(loads2).toHaveLength(1);
      expect(loads2[0].session_id).toBe('sess-1');
    } finally {
      vi.useRealTimers();
    }
  });
});

// ────────────────────────────────────────────────────────────────────────────
// Event handling
// ────────────────────────────────────────────────────────────────────────────
describe('SessionTab — event handling', () => {
  it('renders initial Idle status', async () => {
    renderTab();
    await connectWs();
    expect(screen.getByText('● Idle')).toBeInTheDocument();
  });

  it('state_changed updates the status indicator', async () => {
    renderTab();
    const ws = await connectWs();
    act(() => ws.receive({ type: 'state_changed', state: 'RUNNING', is_running: true }));
    expect(await screen.findByText('● Running')).toBeInTheDocument();
  });

  it('ignores state_changed for a different session', async () => {
    renderTab({ sessionId: 'sess-1' });
    const ws = await connectWs();
    act(() => ws.receive({ type: 'state_changed', session_id: 'other-session', state: 'RUNNING', is_running: true }));
    expect(screen.getByText('● Idle')).toBeInTheDocument();
  });

  it('conversation_changed renders user and assistant messages', async () => {
    renderTab();
    const ws = await connectWs();
    act(() =>
      ws.receive({
        type: 'conversation_changed',
        messages: [
          { role: 'user', content: 'Hello there' },
          { role: 'assistant', content: 'Hi!' },
        ],
      })
    );
    expect(await screen.findByText('Hello there')).toBeInTheDocument();
    expect(screen.getByText('Hi!')).toBeInTheDocument();
  });

  it('tokens_updated updates the token counter', async () => {
    renderTab();
    const ws = await connectWs();
    act(() => ws.receive({ type: 'tokens_updated', input: 100, output: 50 }));
    expect(await screen.findByText('Tokens: In=100 / Out=50')).toBeInTheDocument();
  });

  it('tokens_updated with source=worker is forwarded to onWorkerEvent', async () => {
    const mocks = renderTab({ sessionId: 'sess-1' });
    const ws = await connectWs();
    act(() => ws.receive({ type: 'tokens_updated', source: 'worker', input: 7, output: 3 }));
    await waitFor(() => {
      expect(mocks.onWorkerEvent).toHaveBeenCalledWith(
        'sess-1',
        expect.objectContaining({ type: 'tokens_updated', source: 'worker' })
      );
    });
  });

  it('worker:worker_message is forwarded to onWorkerEvent', async () => {
    const mocks = renderTab({ sessionId: 'sess-1' });
    const ws = await connectWs();
    act(() =>
      ws.receive({
        type: 'worker:worker_message',
        session_id: 'sess-1',
        data: { content: 'worker says hi' },
      })
    );
    await waitFor(() => {
      expect(mocks.onWorkerEvent).toHaveBeenCalledWith(
        'sess-1',
        expect.objectContaining({ type: 'worker:worker_message' })
      );
    });
  });

  it('does NOT forward worker events for a different session', async () => {
    const mocks = renderTab({ sessionId: 'sess-1' });
    const ws = await connectWs();
    act(() =>
      ws.receive({
        type: 'worker:worker_message',
        session_id: 'other-session',
        data: { content: 'not mine' },
      })
    );
    // Give the handler a tick to (wrongly) forward — it must not.
    await new Promise((r) => setTimeout(r, 10));
    expect(mocks.onWorkerEvent).not.toHaveBeenCalled();
  });

  it('config_changed renders ConfigPanel with heading and no loading placeholder', async () => {
    renderTab();
    const ws = await connectWs();
    act(() =>
      ws.receive({
        type: 'config_changed',
        config: { mode: 'custom', provider: 'openai', model: 'gpt-4o' },
      })
    );
    expect(await screen.findByText('Config')).toBeInTheDocument();
    expect(screen.queryByText('Loading config...')).not.toBeInTheDocument();
  });

  it('config_queued marks the session as queued for ConfigPanel (FIX7)', async () => {
    renderTab({ sessionId: 'sess-1' });
    const ws = await connectWs();
    act(() => ws.receive({ type: 'config_queued', status: 'queued', session_id: 'sess-1' }));
    await waitFor(() => {
      expect(useStore.getState().sessionConfigs['sess-1'].configQueued).toBe(true);
    });
  });

  it('config_apply_failed stores the error and clears the queued state (FIX7)', async () => {
    renderTab({ sessionId: 'sess-1' });
    const ws = await connectWs();
    act(() => ws.receive({ type: 'config_queued', status: 'queued', session_id: 'sess-1' }));
    act(() =>
      ws.receive({
        type: 'config_apply_failed',
        session_id: 'sess-1',
        text: '⚠ Failed to apply queued config: boom',
      })
    );
    await waitFor(() => {
      const entry = useStore.getState().sessionConfigs['sess-1'];
      expect(entry.configQueued).toBe(false);
      expect(entry.applyFailed).toBe('⚠ Failed to apply queued config: boom');
    });
  });

  it('session_loaded on a fresh tab registers the new session and shows its name', async () => {
    const mocks = renderTab();
    const ws = await connectWs();
    act(() =>
      ws.receive({
        type: 'session_loaded',
        session_id: 'sess-new',
        session_name: 'My Session',
        // Fix 4a: backend now embeds config in session_loaded so the chat UI renders immediately
        config: { mode: 'custom', workspace_path: '/tmp/x' },
      })
    );
    expect(await screen.findByText('My Session')).toBeInTheDocument();
    await waitFor(() => {
      expect(mocks.onNewSession).toHaveBeenCalledWith('sess-new', 'My Session');
    });
  });

  // Fix 3b: after a backend restart, load_session on a dead id makes the
  // backend create a REPLACEMENT session and reply session_loaded with a
  // DIFFERENT session_id. The tab must NOT silently rebind — it shows a
  // recovery banner and blocks further commands until 'Start New Session'.
  it(
    'session_loaded with a DIFFERENT session id shows the stale-session recovery banner',
    async () => {
    const mocks = renderTab({ sessionId: 'sess-1' });
    const ws = await connectWs();
    // load_session for sess-1 is deferred by one tick on open — wait for it so
    // the tab is in the "loaded session" state before the stale reply arrives.
    await waitFor(() => {
      const load = sentCommands(ws).find((c) => c.command === 'load_session');
      expect(load).toBeTruthy();
    });
    // Seed store slices for the dead session so the Fix 4c purge is observable.
    act(() => {
      useStore.getState().setSessions([{ session_id: 'sess-1', name: 'Dead Session' }]);
      useStore.getState().setSessionMode('sess-1', 'agent');
      useStore.getState().setTabRunningState('sess-1', 'RUNNING');
      useStore.getState().registerSession('sess-1');
    });
    act(() =>
      ws.receive({
        type: 'session_loaded',
        session_id: 'replacement-sess',
        session_name: 'Replacement Session',
        workspace_id: 'ws-2',
        // Fix 4a: config field is inert for the stale-branch path
        config: { mode: 'custom', workspace_path: '/tmp/x' },
      })
    );
    // Recovery banner with the stale-session message + Start New Session action.
    expect(await screen.findByText(/no longer available/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Start New Session' })).toBeInTheDocument();
    // Fix 4c: the dead session's store slices are purged so nothing can leak
    // into the replacement session.
    expect(useStore.getState().sessionModes['sess-1']).toBeUndefined();
    expect(useStore.getState().tabRunningStates['sess-1']).toBeUndefined();
    expect(useStore.getState().sessionConfigs['sess-1']).toBeUndefined();
    expect(useStore.getState().sessions.some((s) => s.session_id === 'sess-1')).toBe(false);
    // The tab was NOT silently rebound to the replacement session.
    expect(screen.queryByText('Replacement Session')).not.toBeInTheDocument();
    // sendCommand is gated: further commands are blocked (nothing sent on the WS).
    const registered = mocks.onRegister.mock.calls[0][0];
    act(() => registered.sendCommand('start_session', { query: 'ignored' }));
    expect(sentCommands(ws).some((c) => c.command === 'start_session')).toBe(false);
    // 'Start New Session' adopts the stashed replacement through onNewSession.
    fireEvent.click(screen.getByRole('button', { name: 'Start New Session' }));
    await waitFor(() => {
      expect(mocks.onNewSession).toHaveBeenCalledWith('replacement-sess', 'Replacement Session');
    });
    expect(screen.queryByText(/no longer available/i)).not.toBeInTheDocument();
  }, 20000);

  // Intentional replacement (workspace switch via apply_config): the backend
  // flags the new session_loaded with `replacement: true`. The tab must adopt
  // it SILENTLY — no stale banner, no purge of the old session's store slices
  // (the old session still exists server-side), and App is notified via
  // onSessionAdopted so the tab entry tracks the new session id.
  it('session_loaded with replacement:true silently adopts the new session (no stale banner)', async () => {
    const mocks = renderTab({ sessionId: 'sess-1' });
    const ws = await connectWs();
    await waitFor(() => {
      const load = sentCommands(ws).find((c) => c.command === 'load_session');
      expect(load).toBeTruthy();
    });
    // Seed store slices for the old session — they must SURVIVE the adoption
    // (the backend-restart stale path purges them; intentional replacement does not).
    act(() => {
      useStore.getState().setSessions([{ session_id: 'sess-1', name: 'Old Session' }]);
      useStore.getState().setSessionMode('sess-1', 'agent');
      useStore.getState().setTabRunningState('sess-1', 'RUNNING');
      useStore.getState().registerSession('sess-1');
    });
    act(() =>
      ws.receive({
        type: 'session_loaded',
        session_id: 'replacement-sess',
        session_name: 'Replacement Session',
        workspace_id: 'ws-2',
        replacement: true,
        config: { mode: 'custom', workspace_path: '/tmp/x' },
      })
    );
    // No stale-session banner and no recovery action.
    expect(screen.queryByText(/no longer available/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Start New Session' })).not.toBeInTheDocument();
    // Old session slices are NOT purged (the old session still exists server-side).
    expect(useStore.getState().sessionModes['sess-1']).toBe('agent');
    expect(useStore.getState().tabRunningStates['sess-1']).toBe('RUNNING');
    expect(useStore.getState().sessions.some((s) => s.session_id === 'sess-1')).toBe(true);
    // The replacement session is adopted through the store...
    await waitFor(() => {
      expect(useStore.getState().sessionConfigs['replacement-sess']).toBeDefined();
      expect(useStore.getState().sessionConfigs['replacement-sess'].isLoaded).toBe(true);
    });
    // ...the tab shows the new session name...
    expect(await screen.findByText('Replacement Session')).toBeInTheDocument();
    // ...and App is notified so the tab entry follows the new session id.
    await waitFor(() => {
      expect(mocks.onSessionAdopted).toHaveBeenCalledWith('replacement-sess');
    });
    // This is a rebind, not a new-tab creation — onNewSession must NOT fire.
    expect(mocks.onNewSession).not.toHaveBeenCalled();
  });

  // Fix 4d: a dead session id (backend restart) now makes the backend reply
  // session_loaded with load_error: true and the SAME session_id the tab asked
  // for. The tab must show the recovery banner and must NOT register the
  // phantom session in the store (no config, no sessions-list entry, no
  // onSessionAdopted / onNewSession notifications).
  it('session_loaded with load_error renders the load-failed state and does not register the session', async () => {
    const mocks = renderTab({ sessionId: 'sess-1' });
    const ws = await connectWs();
    // load_session for sess-1 is deferred by one tick on open — wait for it so
    // the tab is in the "loaded session" state before the load_error reply.
    await waitFor(() => {
      const load = sentCommands(ws).find((c) => c.command === 'load_session');
      expect(load).toBeTruthy();
    });
    act(() =>
      ws.receive({
        type: 'session_loaded',
        session_id: 'sess-1',
        load_error: true,
        workspace_id: 'ws-1',
      })
    );
    // Recovery banner: load-failed message + Start New Session action.
    expect(await screen.findByText(/could not be loaded/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Start New Session' })).toBeInTheDocument();
    // The phantom session is NOT registered in the store (no config snapshot,
    // no sessions-list entry).
    expect(useStore.getState().sessionConfigs['sess-1']).toBeUndefined();
    expect(useStore.getState().sessions.some((s) => s.session_id === 'sess-1')).toBe(false);
    // No adoption, no rebind, no new-session notification.
    expect(mocks.onSessionAdopted).not.toHaveBeenCalled();
    expect(mocks.onNewSession).not.toHaveBeenCalled();
  });

  // Fix 4d: 'Start New Session' after load_error has no stashed replacement —
  // it asks the backend for a brand new session and accepts the reply through
  // the normal path (expectingNewSessionRef bypasses stale detection for the
  // dead id, and wasExpectingNew routes onNewSession to App).
  it('Start New Session after load_error sends new_session and accepts the reply', async () => {
    const mocks = renderTab({ sessionId: 'sess-1' });
    const ws = await connectWs();
    await waitFor(() => {
      const load = sentCommands(ws).find((c) => c.command === 'load_session');
      expect(load).toBeTruthy();
    });
    act(() =>
      ws.receive({
        type: 'session_loaded',
        session_id: 'sess-1',
        load_error: true,
      })
    );
    fireEvent.click(await screen.findByRole('button', { name: 'Start New Session' }));
    // The tab asks the backend for a brand new session (custom mode), exactly
    // like a fresh tab does on open.
    await waitFor(() => {
      const ns = sentCommands(ws).find((c) => c.command === 'new_session');
      expect(ns).toBeTruthy();
      expect(ns.mode).toBe('custom');
    });
    act(() =>
      ws.receive({
        type: 'session_loaded',
        session_id: 'sess-new',
        session_name: 'Fresh Session',
        config: { mode: 'custom', workspace_path: '/tmp/x' },
      })
    );
    // The new session is adopted and rendered; the recovery banner is gone.
    expect(await screen.findByText('Fresh Session')).toBeInTheDocument();
    expect(screen.queryByText(/could not be loaded/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/no longer available/i)).not.toBeInTheDocument();
    await waitFor(() => {
      expect(mocks.onNewSession).toHaveBeenCalledWith('sess-new', 'Fresh Session');
    });
    expect(useStore.getState().sessionConfigs['sess-new'].isLoaded).toBe(true);
  });
});

// ────────────────────────────────────────────────────────────────────────────
// Security prompt
// ────────────────────────────────────────────────────────────────────────────
describe('SessionTab — security prompt', () => {
  it('renders SecurityDialog and Approve sends security_response', async () => {
    renderTab();
    const ws = await connectWs();
    act(() =>
      ws.receive({
        type: 'security_prompt',
        request_id: 'req-1',
        tool_name: 'read_file',
        capabilities: ['read'],
        arguments: { path: '/tmp/x' },
        description: 'Tool requires your approval.',
      })
    );
    expect(await screen.findByText('Security Prompt')).toBeInTheDocument();
    expect(screen.getByText('read_file')).toBeInTheDocument();
    const approve = screen.getByText('Approve');
    fireEvent.click(approve);
    const response = sentCommands(ws).find((c) => c.command === 'security_response');
    expect(response).toBeTruthy();
    expect(response.request_id).toBe('req-1');
    expect(response.approved).toBe(true);
  });
});
