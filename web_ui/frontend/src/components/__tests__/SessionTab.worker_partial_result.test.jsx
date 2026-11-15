// @vitest-environment jsdom
/*
 * SessionTab.worker_partial_result.test.jsx
 *
 * `worker:worker_partial_result` is published on the global bus by
 * worker_thread.py (running path :4158, terminal path :5156) and forwarded to
 * the browser by bridge.py as `worker:worker_partial_result`. SessionTab's
 * handleEvent switch is an explicit allow-list of `worker:*` types, so a
 * missing arm sends the event to `default:` -> console.warn + DROP.
 *
 * Rendered against the REAL SessionTab so the assertion lands at the real
 * WebSocket boundary: ws.receive(dict) -> handleEvent -> onWorkerEvent.
 * The fake-WebSocket setup mirrors SessionTab.addendum_event.test.jsx,
 * SessionTab.test.jsx and SessionTab.stop_resume.test.jsx.
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
  cleanup,
  act,
  waitFor,
} from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import SessionTab from '../SessionTab';
import useStore from '../../store/useStore';

// ─── Mock WebSocket (SessionTab uses the global WebSocket.OPEN constant) ───
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

// ─── Mock ResizeObserver (jsdom lacks it) ───
class MockResizeObserver {
  constructor(callback) {
    this.callback = callback;
  }
  observe() {}
  unobserve() {}
  disconnect() {}
}

// ─── Test helpers ───
function lastWs() {
  return MockWebSocket.instances[MockWebSocket.instances.length - 1];
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
    onRegister: vi.fn(),
    onSessionRenamed: vi.fn(),
    onWorkerEvent: vi.fn(),
    onLoggingConfigChanged: vi.fn(),
    onSessionAdopted: vi.fn(),
    ...props,
  };
  render(<SessionTab {...mergedProps} />);
}

// ─── Setup / teardown ───
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

// ─── worker:worker_partial_result ───
describe('SessionTab — worker:worker_partial_result routing', () => {
  it('forwards worker:worker_partial_result to onWorkerEvent instead of dropping it', async () => {
    const onWorkerEvent = vi.fn();
    renderTab({ sessionId: 'sess-1', onWorkerEvent });
    const ws = await connectWs();

    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});

    const msg = {
      type: 'worker:worker_partial_result',
      worker_name: 'w1',
      instance_id: 1,
      instance_label: 'w1',
      session_id: 'sess-1',
      timestamp: '2026-01-01T00:00:00+00:00',
      data: { status: 'running', reason: 'partial', content: 'hello partial' },
    };
    act(() => ws.receive(msg));

    expect(onWorkerEvent).toHaveBeenCalledTimes(1);
    expect(onWorkerEvent.mock.calls[0][0]).toBe('sess-1');
    expect(onWorkerEvent.mock.calls[0][1].type).toBe('worker:worker_partial_result');
    expect(
      warn.mock.calls.some((c) => String(c[0]).includes('Unknown event type')),
    ).toBe(false);
  });
});
