// @vitest-environment jsdom
/*
 * SessionTab.addendum_event.test.jsx
 *
 * W1 Slice 4 — the `addendum_updated` / `runtime_notice` runtime-channel arms.
 *
 * Rendered against the REAL SessionTab so the assertions land at the real
 * WebSocket boundary: ws.receive(dict) -> handleEvent -> useStore. Neither the
 * bridge arm nor the store action is mocked, so if any link is missing the
 * store never sees the event and these tests fail.
 *
 * What is pinned (design doc §7.3 + ruling D-6):
 *   1. addendum_updated for the current session reaches the store, keyed by
 *      entry_id, so the notification is self-describing rather than a diff;
 *   2. addendum_updated for a DIFFERENT session is ignored (per-session guard,
 *      same shape as state_changed / tokens_updated / assistant_delta);
 *   3. duplicate and out-of-order arrival converge: the store merges by
 *      entry_id and never appends, so a late-arriving OLDER event cannot
 *      duplicate an entry or delete the entries only the newer event knew
 *      about. (Field-level regression is not possible here because
 *      session/addendum.py exposes only write_addendum_entry — entries are
 *      append-only and immutable once written.)
 *   4. runtime_notice (a rejected write) reaches the store without throwing and
 *      without touching the entry set; a wrong-session notice is ignored.
 *
 * The fake-WebSocket setup mirrors SessionTab.test.jsx and
 * SessionTab.stop_resume.test.jsx (static instances, per-instance `sent`,
 * vi.stubGlobal('WebSocket', ...)).
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

// ─────────────────────────────────────────────────────────────────────────
// Mock WebSocket (SessionTab uses the global WebSocket.OPEN constant)
// ─────────────────────────────────────────────────────────────────────────
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

// ─── Mock ResizeObserver (jsdom lacks it; ChatPanel constructs one on mount) ───
class MockResizeObserver {
  constructor(callback) {
    this.callback = callback;
  }
  observe() {}
  unobserve() {}
  disconnect() {}
}

// ─────────────────────────────────────────────────────────────────────────
// Test helpers
// ─────────────────────────────────────────────────────────────────────────
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

// The store's two addendum slices for the session under test.
function entries(sessionId = 'sess-1') {
  return useStore.getState().addendumEntries[sessionId] || {};
}
function entryIds(sessionId = 'sess-1') {
  return Object.keys(entries(sessionId)).sort();
}
function notices(sessionId = 'sess-1') {
  return useStore.getState().runtimeNotices[sessionId] || [];
}

// Entries exactly as session/addendum.py writes them: { id, ts, source, text,
// pinned }.
const E1 = {
  id: 'a-000000000001',
  ts: '2026-01-01T00:00:00+00:00',
  source: 'operator',
  text: 'always answer in metric units',
  pinned: false,
};
const E2 = {
  id: 'a-000000000002',
  ts: '2026-01-01T00:05:00+00:00',
  source: 'config',
  text: 'the staging cluster is read-only',
  pinned: true,
};

// The `addendum_updated` payload exactly as _updated_payload() builds it: the
// FULL resulting entry set plus entry_id/ts/source for the entry just written.
function updatedEvent(list, entryId, sessionId = 'sess-1') {
  const entry = list.find((e) => e.id === entryId);
  return {
    type: 'addendum_updated',
    session_id: sessionId,
    entry_id: entry.id,
    ts: entry.ts,
    source: entry.source,
    entries: list.map((e) => ({ ...e })),
  };
}

function noticeEvent(sessionId = 'sess-1') {
  return {
    type: 'runtime_notice',
    session_id: sessionId,
    level: 'warning',
    code: 'empty_text',
    message: 'addendum text is empty',
  };
}

// ─────────────────────────────────────────────────────────────────────────
// Setup / teardown
// ─────────────────────────────────────────────────────────────────────────
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

// ─────────────────────────────────────────────────────────────────────────
// addendum_updated
// ─────────────────────────────────────────────────────────────────────────
describe('SessionTab — addendum_updated runtime event', () => {
  it('writes the entry set into the store keyed by entry_id', async () => {
    renderTab({ sessionId: 'sess-1' });
    const ws = await connectWs();

    act(() => ws.receive(updatedEvent([E1], E1.id)));

    expect(entryIds()).toEqual([E1.id]);
    expect(entries()[E1.id]).toEqual(E1);
  });

  it('ignores addendum_updated for a different session', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    renderTab({ sessionId: 'sess-1' });
    const ws = await connectWs();

    act(() => ws.receive(updatedEvent([E1], E1.id, 'other-session')));

    expect(entries()).toEqual({});
    expect(warn.mock.calls.some((c) => String(c[0]).includes('addendum_updated'))).toBe(true);
  });

  it('converges on duplicate and out-of-order arrival (merge by entry_id, never append)', async () => {
    renderTab({ sessionId: 'sess-1' });
    const ws = await connectWs();

    const first = updatedEvent([E1], E1.id);
    const second = updatedEvent([E1, E2], E2.id);

    // Duplicate delivery of the same event must not duplicate the entry.
    act(() => {
      ws.receive(first);
      ws.receive(first);
    });
    expect(entryIds()).toEqual([E1.id]);

    // The newer event carries the full resulting set.
    act(() => ws.receive(second));
    expect(entryIds()).toEqual([E1.id, E2.id]);

    // Out of order: the OLDER event arriving late must neither duplicate E1 nor
    // drop E2, which only the newer event knew about. An append-shaped store
    // fails this; a merge keyed by entry_id cannot.
    act(() => ws.receive(first));
    expect(entryIds()).toEqual([E1.id, E2.id]);
    expect(entries()[E2.id]).toEqual(E2);
  });
});

// ─────────────────────────────────────────────────────────────────────────
// runtime_notice
// ─────────────────────────────────────────────────────────────────────────
describe('SessionTab — runtime_notice runtime event', () => {
  it('stores a rejected-write notice without throwing or touching the entry set', async () => {
    renderTab({ sessionId: 'sess-1' });
    const ws = await connectWs();

    act(() => ws.receive(updatedEvent([E1], E1.id)));
    expect(() => act(() => ws.receive(noticeEvent()))).not.toThrow();

    expect(notices()).toEqual([
      { level: 'warning', code: 'empty_text', message: 'addendum text is empty' },
    ]);
    expect(entryIds()).toEqual([E1.id]);

    // Consecutive rejections stay visible rather than overwriting each other.
    act(() => ws.receive({ ...noticeEvent(), code: 'invalid_source', message: 'unknown source' }));
    expect(notices().map((n) => n.code)).toEqual(['empty_text', 'invalid_source']);
  });

  it('ignores runtime_notice for a different session', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    renderTab({ sessionId: 'sess-1' });
    const ws = await connectWs();

    act(() => ws.receive(noticeEvent('other-session')));

    expect(notices()).toEqual([]);
    expect(warn.mock.calls.some((c) => String(c[0]).includes('runtime_notice'))).toBe(true);
  });
});
