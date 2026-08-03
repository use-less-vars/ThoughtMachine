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
  render(
    <SessionTab
      sessionId={null}
      tabId="tab-1"
      hubReady
      staggerMs={0}
      loadOnConnect
      isActive
      onClose={onClose}
      onNewSession={onNewSession}
      onOpenNewTab={onOpenNewTab}
      onSessionSaved={onSessionSaved}
      onRegister={onRegister}
      onSessionRenamed={onSessionRenamed}
      onWorkerEvent={onWorkerEvent}
      onLoggingConfigChanged={onLoggingConfigChanged}
      {...props}
    />
  );
  return {
    onClose,
    onNewSession,
    onOpenNewTab,
    onSessionSaved,
    onRegister,
    onSessionRenamed,
    onWorkerEvent,
    onLoggingConfigChanged,
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

  it('inactive tab with existing session shows deferred placeholder instead of loading', async () => {
    renderTab({ sessionId: 'sess-1', loadOnConnect: false });
    await connectWs();
    expect(screen.getByText('Click tab to load conversation')).toBeInTheDocument();
    const ws = lastWs();
    const commands = sentCommands(ws).map((c) => c.command);
    expect(commands).not.toContain('load_session');
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

  it('session_loaded on a fresh tab registers the new session and shows its name', async () => {
    const mocks = renderTab();
    const ws = await connectWs();
    act(() =>
      ws.receive({
        type: 'session_loaded',
        session_id: 'sess-new',
        session_name: 'My Session',
      })
    );
    expect(await screen.findByText('My Session')).toBeInTheDocument();
    await waitFor(() => {
      expect(mocks.onNewSession).toHaveBeenCalledWith('sess-new', 'My Session');
    });
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
