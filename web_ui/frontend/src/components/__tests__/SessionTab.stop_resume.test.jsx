// @vitest-environment jsdom
/*
 * SessionTab.stop_resume.test.jsx
 *
 * Capability strand — Stop / Resume controls on the query bar.
 *
 * Rendered against the REAL SessionTab so the assertions land at the real
 * WebSocket boundary: QueryBar -> SessionTab.sendCommand -> ws.send(JSON).
 * The sendCommand prop is never mocked here.
 *
 * STOP   : drive status RUNNING, click Stop,   expect {command:'stop_session'} on the socket.
 * RESUME : drive status PAUSED,  click Resume, expect {command:'resume_session'} on the socket.
 *
 * The fake-WebSocket setup mirrors SessionTab.test.jsx (static instances,
 * per-instance `sent`, vi.stubGlobal('WebSocket', ...)).
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

// ─────────────────────────────────────────────────────────────────────────────
// Mock WebSocket (SessionTab uses the global WebSocket.OPEN constant)
// ─────────────────────────────────────────────────────────────────────────────
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
    this.closed = false;
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
    this.closed = true;
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

// ─── Mock ResizeObserver (jsdom lacks it; ChatPanel constructs one on mount) ──
class MockResizeObserver {
  constructor(callback) {
    this.callback = callback;
  }
  observe() {}
  unobserve() {}
  disconnect() {}
}

// ─────────────────────────────────────────────────────────────────────────────
// Test helpers (identical idiom to SessionTab.test.jsx)
// ─────────────────────────────────────────────────────────────────────────────
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
  const onRegister = vi.fn();
  const mergedProps = {
    sessionId: null,
    tabId: 'tab-1',
    hubReady: true,
    staggerMs: 0,
    loadOnConnect: true,
    isActive: true,
    onClose: vi.fn(),
    onNewSession: vi.fn(),
    onOpenNewTab: vi.fn(),
    onSessionSaved: vi.fn(),
    onRegister,
    onSessionRenamed: vi.fn(),
    onWorkerEvent: vi.fn(),
    onLoggingConfigChanged: vi.fn(),
    onSessionAdopted: vi.fn(),
    ...props,
  };
  render(<SessionTab {...mergedProps} />);
  return { onRegister };
}

// ─────────────────────────────────────────────────────────────────────────────
// Setup / teardown
// ─────────────────────────────────────────────────────────────────────────────
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

// ─────────────────────────────────────────────────────────────────────────────
// Stop / Resume over the real WebSocket boundary
// ─────────────────────────────────────────────────────────────────────────────
describe('QueryBar Stop/Resume controls (real WebSocket boundary)', () => {
  it('clicking Stop dispatches a stop_session frame', async () => {
    renderTab({ sessionId: 'sess-1' });
    const ws = await connectWs();
    // Drive the session to RUNNING the same way the existing suite does.
    act(() => ws.receive({ type: 'state_changed', state: 'RUNNING', is_running: true }));

    const stopBtn = screen.queryByRole('button', { name: /stop/i });
    expect(stopBtn).not.toBeNull();
    fireEvent.click(stopBtn);
    expect(sentCommands(ws).some((c) => c.command === 'stop_session')).toBe(true);
  });

  it('clicking Resume dispatches a resume_session frame', async () => {
    renderTab({ sessionId: 'sess-1' });
    const ws = await connectWs();
    // Drive the session to PAUSED.
    act(() => ws.receive({ type: 'state_changed', state: 'PAUSED', is_running: false }));

    const resumeBtn = screen.queryByRole('button', { name: /resume/i });
    expect(resumeBtn).not.toBeNull();
    fireEvent.click(resumeBtn);
    expect(sentCommands(ws).some((c) => c.command === 'resume_session')).toBe(true);
  });
});
