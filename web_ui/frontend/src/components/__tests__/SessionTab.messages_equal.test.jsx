// @vitest-environment jsdom
/*
 * SessionTab.messages_equal.test.jsx
 *
 * Regression guard for the `messagesEqual` store-selector comparator in
 * SessionTab.jsx (~lines 41-76).
 *
 * The previous comparator only looked at the length plus the FIRST and LAST
 * message (role + content). A change in the MIDDLE of the list — a replaced
 * message body, or a flag flip on a middle bubble — left length, first and
 * last untouched, so the equality fn returned true, zustand handed back the
 * stale array, and ChatPanel never re-rendered: the DOM kept showing the old
 * transcript. These tests pin the full walk: both cases below mutate ONLY a
 * middle element of a same-length list whose first and last elements are the
 * very same object instances, and then assert on the rendered DOM.
 *
 * Harness modelled on SessionTab.test.jsx (MockWebSocket / MockResizeObserver /
 * renderTab + connectWs): the tab is rendered directly and its messages are
 * driven through the real store (`receiveConversationChanged`), which is the
 * exact path the selector + comparator guard.
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
import { render, act, cleanup, waitFor } from '@testing-library/react';
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

// ─── Mock ResizeObserver (jsdom lacks it; ChatPanel constructs one) ───
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
    sessionId: 'sess-1',
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
  return render(<SessionTab {...mergedProps} />);
}

/* Mount on an ACTIVE tab so the deferred placeholder never replaces the
 * transcript, connect the socket and confirm the session. */
async function mountTab() {
  const utils = renderTab();
  const ws = await connectWs();
  act(() => {
    ws.receive({
      type: 'session_loaded',
      session_id: 'sess-1',
      session_name: 'My Session',
      config: { mode: 'custom', workspace_path: '/tmp/x' },
    });
  });
  return { ws, ...utils };
}

/* Drive the messages slice exactly the way SessionTab's conversation_changed
 * handler does (replace mode). */
function setMessages(messages) {
  act(() => {
    useStore.getState().receiveConversationChanged('sess-1', messages);
  });
}

function bubbles(container) {
  return container.querySelectorAll('.chat-panel .message');
}

// ────────────────────────────────────────────────────────────────────────────
// Setup / teardown
// ────────────────────────────────────────────────────────────────────────────
beforeEach(() => {
  MockWebSocket.instances = [];
  localStorage.clear();
  useStore.getState().reset();
  vi.stubGlobal('WebSocket', MockWebSocket);
  vi.stubGlobal('ResizeObserver', MockResizeObserver);
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true,
    json: async () => ({ tools: [] }),
    text: async () => '',
  })));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ────────────────────────────────────────────────────────────────────────────
// Middle-of-list changes
// ────────────────────────────────────────────────────────────────────────────
describe('SessionTab — messagesEqual catches MID-LIST changes', () => {
  it('re-renders when a middle message content changes (same length, same last)', async () => {
    const { container } = await mountTab();

    const first = { role: 'user', content: 'first question' };
    const last = { role: 'assistant', content: 'last answer' };

    setMessages([first, { role: 'assistant', content: 'middle v1' }, last]);
    expect(container.textContent).toContain('middle v1');
    expect(bubbles(container)).toHaveLength(3);

    /* Same length, and the FIRST and LAST entries are the SAME object
     * instances — only the middle element's content differs. A first/last-only
     * comparator reports "equal" here and the DOM would keep 'middle v1'. */
    setMessages([first, { role: 'assistant', content: 'middle v2' }, last]);

    expect(container.textContent).toContain('middle v2');
    expect(container.textContent).not.toContain('middle v1');
    expect(container.textContent).toContain('first question');
    expect(container.textContent).toContain('last answer');
    expect(bubbles(container)).toHaveLength(3);
  });

  it('re-renders when a middle message differs only by is_final (same length, same last)', async () => {
    const { container } = await mountTab();

    const first = { role: 'user', content: 'first question' };
    const last = { role: 'assistant', content: 'last answer' };

    setMessages([first, { role: 'assistant', content: 'middle answer' }, last]);

    /* Non-final assistant bubble: the plain assistant markdown path. */
    expect(container.querySelectorAll('.chat-panel .message-final')).toHaveLength(0);
    expect(container.querySelectorAll('.chat-panel .final-markdown')).toHaveLength(0);

    /* Identical length, identical first and last element, identical middle
     * content/role — ONLY the is_final flag (which messagesEqual inspects)
     * flips to true, so the bubble must switch to the final rendering. */
    setMessages([
      first,
      { role: 'assistant', content: 'middle answer', is_final: true, response_type: 'answer' },
      last,
    ]);

    const finalBubbles = container.querySelectorAll('.chat-panel .message-final');
    expect(finalBubbles).toHaveLength(1);
    expect(finalBubbles[0].textContent).toContain('middle answer');
    expect(container.querySelectorAll('.chat-panel .final-markdown')).toHaveLength(1);
    expect(bubbles(container)).toHaveLength(3);
  });
});
