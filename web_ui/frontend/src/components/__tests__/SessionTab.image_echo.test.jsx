// @vitest-environment jsdom
/*
 * SessionTab.image_echo.test.jsx
 *
 * Regression guard for the ORIGINAL pasted-image bug: the backend re-broadcasts
 * the FULL history on every `conversation_version` bump (user_query, tool_call,
 * tool_result, turn, agent_responded), so the same user row carrying
 * `images: [{ id, media_type }]` reaches `SessionTab` several times per turn.
 *
 * Two mechanisms have to hold for the picture to survive:
 *   1. `useStore`'s `mode: 'replace'` merge must not drop a row's image
 *      attachments when a later echo omits them (preserveImageAttachments).
 *   2. The paste-preview registry must not be drained on first use, so the
 *      browser's own copy of the picture keeps painting on every echo
 *      (`remembered`, keyed by server image id) and the durable
 *      `/api/session/<sid>/image/<id>` route is the fallback.
 *
 * NOTE on echo shape: `messagesEqual` in SessionTab (the zustand equality fn)
 * deliberately compares only role/content/flags — NOT the image fields. Two
 * echoes of the SAME length and content are therefore served from the previous
 * array, so the DOM does NOT re-render and a dropped attachment is invisible to
 * a DOM-only assertion. Two consequences for the tests below:
 *
 *   a. The identical-history tests assert the STORE after every echo (see
 *      `expectStoreKeepsImage`): the store array is rewritten on every
 *      delivery, so that is where "this echo was merged and kept the picture"
 *      is observable. Their DOM assertions are a STEADY-STATE guard. They also
 *      re-mount from the state the 3rd echo left behind, which is what proves
 *      the surviving attachments are actually renderable.
 *   b. The growing-history test drives the real echo sequence (user_query →
 *      tool_call → turn): the list LENGTH changes each time, so the selector
 *      cannot mask anything and the store rescue is exercised end-to-end.
 *
 * These tests drive the REAL path — MockWebSocket → `conversation_changed` →
 * `attachLocalImagePreviews` → `receiveConversationChanged` → ChatPanel →
 * MessageBubble — and assert after the 2nd and 3rd delivery that the picture is
 * still rendered and the list has NOT degraded to the placeholder.
 *
 * Harness modelled on SessionTab.messages_equal.test.jsx (MockWebSocket /
 * MockResizeObserver / renderTab + connectWs + mountTab).
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
import {
  registerPastedImagePreviews,
  clearPendingImagePreviews,
} from '../../utils/pastedImagePreviews';

// ──────────────────────────────────────────────────────────────────────────────
// Mock WebSocket (SessionTab uses the global WebSocket.OPEN constant)
// ──────────────────────────────────────────────────────────────────────────────
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

// ──────────────────────────────────────────────────────────────────────────────
// Test helpers
// ──────────────────────────────────────────────────────────────────────────────
const SESSION_ID = 'sess-1';
const IMAGE_ID = 'img-1';
const QUERY = 'look at this picture';
const PREVIEW = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUg==';
const DURABLE_SRC = `/api/session/${SESSION_ID}/image/${IMAGE_ID}`;

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
    sessionId: SESSION_ID,
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

async function mountTab() {
  const utils = renderTab();
  const ws = await connectWs();
  act(() => {
    ws.receive({
      type: 'session_loaded',
      session_id: SESSION_ID,
      session_name: 'My Session',
      config: { mode: 'custom', workspace_path: '/tmp/x' },
    });
  });
  return { ws, ...utils };
}

/* The user row that carries the pasted picture. Fresh object identities on
 * every call — that is what the wire actually delivers. */
function userRow({ withImages = true } = {}) {
  return withImages
    ? { role: 'user', content: QUERY, images: [{ id: IMAGE_ID, media_type: 'image/png' }] }
    : { role: 'user', content: QUERY };
}

/* The backend's actual echo sequence for one turn: the picture arrives with the
 * user_query echo, and the two later echoes re-send the full history — the user
 * row among them, sometimes WITHOUT the image metadata. */
const TOOL_ROW = { role: 'tool_call', content: '{"path":"a.txt"}', tool_call_id: 'tc-1' };
const ANSWER_ROW = { role: 'assistant', content: 'nice picture' };

function echo(n, { withImages = true } = {}) {
  if (n === 1) return [userRow({ withImages })];
  if (n === 2) return [userRow({ withImages }), TOOL_ROW];
  return [userRow({ withImages }), TOOL_ROW, ANSWER_ROW];
}

/* Deliver one `conversation_changed` (the re-broadcast path). */
function deliver(ws, messages) {
  act(() => {
    ws.receive({ type: 'conversation_changed', session_id: SESSION_ID, messages });
  });
}

function bubbles(container) {
  return container.querySelectorAll('.chat-panel .message');
}

function thumbs(container) {
  return container.querySelectorAll('[data-testid="message-image-thumb"]');
}

function placeholders(container) {
  return container.querySelectorAll('[data-testid="message-image-placeholder"]');
}

/* The array the selector would hand ChatPanel if `messagesEqual` did not
 * short-circuit identical content. Reading it directly is how the per-echo
 * merge stays observable when the history does not change shape. */
function storeRows(sessionId = SESSION_ID) {
  return useStore.getState().sessionMessages[sessionId] || [];
}

/* Asserts the STORE still holds the user row's image attachments after an echo.
 * This is the assertion that fails if `preserveImageAttachments` regresses: the
 * DOM can look perfect simply because nothing re-rendered. `preview` defaults
 * to `undefined` — the no-local-preview case, which must NOT grow one. */
function expectStoreKeepsImage({ delivery, preview } = {}) {
  const label = delivery ? ` (delivery ${delivery})` : '';
  const rows = storeRows();
  expect(rows, `store lost the transcript${label}`).toHaveLength(2);
  expect(rows[0].role, `store row 0 is not the user row${label}`).toBe('user');
  expect(
    rows[0].images,
    `store row lost its image metadata${label}`,
  ).toEqual([{ id: IMAGE_ID, media_type: 'image/png' }]);
  if (preview === undefined) {
    expect(
      rows[0].localImagePreviews,
      `store row grew a preview it never had${label}`,
    ).toBeUndefined();
  } else {
    expect(rows[0].localImagePreviews, `store row lost its preview${label}`).toEqual([preview]);
  }
}

/* Asserts the picture is still on screen, from `expectedSrc`, with no
 * placeholder standing in for it and the transcript otherwise intact. */
function expectImageRendered(container, expectedSrc, { delivery, bubbleCount } = {}) {
  const label = delivery ? ` (delivery ${delivery})` : '';
  expect(thumbs(container), `thumbnail missing${label}`).toHaveLength(1);
  expect(thumbs(container)[0].getAttribute('src'), `wrong src${label}`).toBe(expectedSrc);
  expect(
    placeholders(container),
    `degraded to the placeholder${label}`,
  ).toHaveLength(0);
  if (bubbleCount !== undefined) {
    expect(bubbles(container), `transcript changed${label}`).toHaveLength(bubbleCount);
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// Setup / teardown
// ──────────────────────────────────────────────────────────────────────────────
beforeEach(() => {
  MockWebSocket.instances = [];
  localStorage.clear();
  useStore.getState().reset();
  clearPendingImagePreviews();
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
  clearPendingImagePreviews();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ──────────────────────────────────────────────────────────────────────────────
// The second (and third) echo
// ──────────────────────────────────────────────────────────────────────────────
describe('SessionTab — a pasted image survives repeated history echoes', () => {
  it('keeps the local preview after the 2nd and 3rd delivery of the same history', async () => {
    /* The browser's own copy of the just-pasted picture, registered once. */
    registerPastedImagePreviews([{ dataUrl: PREVIEW, mediaType: 'image/png' }], QUERY);

    const { ws, container } = await mountTab();

    let previousRows = storeRows();
    for (let n = 1; n <= 3; n += 1) {
      /* Identical length, identical content, identical order — the backend's
       * re-broadcast shape. `messagesEqual` therefore returns true for echoes 2
       * and 3 and the DOM is NOT re-rendered, which is why the STORE carries the
       * proof: a fresh array identity means the echo really was merged, and the
       * attachments mean the merge kept the picture instead of dropping it. */
      deliver(ws, [userRow(), ANSWER_ROW]);
      expect(storeRows(), `store did not re-merge delivery ${n}`).not.toBe(previousRows);
      previousRows = storeRows();
      expectStoreKeepsImage({ delivery: n, preview: PREVIEW });
      expectImageRendered(container, PREVIEW, { delivery: n, bubbleCount: 2 });
    }

    /* The state the 3rd echo left behind is what a re-mount renders from: it
     * must paint the picture, not merely hold the metadata. */
    cleanup();
    const remounted = await mountTab();
    expectImageRendered(remounted.container, PREVIEW, { bubbleCount: 2 });
  });

  it('keeps rendering from the durable URL when no local preview exists at all', async () => {
    /* Nothing registered: this is the fresh-tab / after-reload case, where the
     * server's image id is the ONLY thing that can carry the picture. */
    const { ws, container } = await mountTab();

    let previousRows = storeRows();
    for (let n = 1; n <= 3; n += 1) {
      deliver(ws, [userRow(), ANSWER_ROW]);
      expect(storeRows(), `store did not re-merge delivery ${n}`).not.toBe(previousRows);
      previousRows = storeRows();
      expectStoreKeepsImage({ delivery: n });
      expectImageRendered(container, DURABLE_SRC, { delivery: n, bubbleCount: 2 });
    }

    /* Re-mount: the image id the 3rd echo left in the store still addresses the
     * durable route, so the thumbnail survives with no browser-side bytes. */
    cleanup();
    const remounted = await mountTab();
    expectImageRendered(remounted.container, DURABLE_SRC, { bubbleCount: 2 });
  });

  it('rescues the attachments on identical-content echoes that arrive WITHOUT images', async () => {
    registerPastedImagePreviews([{ dataUrl: PREVIEW, mediaType: 'image/png' }], QUERY);

    const { ws, container } = await mountTab();

    deliver(ws, [userRow(), ANSWER_ROW]);
    expectStoreKeepsImage({ delivery: 1, preview: PREVIEW });
    expectImageRendered(container, PREVIEW, { delivery: 1, bubbleCount: 2 });

    /* Echoes 2 and 3 re-send the SAME two rows with the SAME text, but with the
     * image metadata stripped. Content is identical, so nothing here can be
     * explained by a re-render — only useStore.preserveImageAttachments can hand
     * the attachments back. */
    let previousRows = storeRows();
    for (let n = 2; n <= 3; n += 1) {
      deliver(ws, [userRow({ withImages: false }), ANSWER_ROW]);
      expect(storeRows(), `store did not re-merge delivery ${n}`).not.toBe(previousRows);
      previousRows = storeRows();
      expectStoreKeepsImage({ delivery: n, preview: PREVIEW });
      expectImageRendered(container, PREVIEW, { delivery: n, bubbleCount: 2 });
    }
  });

  it('keeps the durable URL when the later echoes are bare AND no preview exists', async () => {
    const { ws, container } = await mountTab();

    deliver(ws, [userRow(), ANSWER_ROW]);
    expectImageRendered(container, DURABLE_SRC, { delivery: 1, bubbleCount: 2 });

    for (let n = 2; n <= 3; n += 1) {
      deliver(ws, [userRow({ withImages: false }), ANSWER_ROW]);
      expectStoreKeepsImage({ delivery: n });
      expectImageRendered(container, DURABLE_SRC, { delivery: n, bubbleCount: 2 });
    }
  });

  it('keeps the image when later echoes re-send the row WITHOUT images (growing history)', async () => {
    registerPastedImagePreviews([{ dataUrl: PREVIEW, mediaType: 'image/png' }], QUERY);

    const { ws, container } = await mountTab();

    deliver(ws, echo(1));
    expectImageRendered(container, PREVIEW, { delivery: 1, bubbleCount: 1 });

    /* user_query echo → tool_call echo → turn echo, exactly as the backend
     * bumps conversation_version. The picture arrives only with echo 1; the
     * later two re-send the user row bare. The list LENGTH changes each time,
     * so the store is re-read (no stale-array masking) and
     * useStore.preserveImageAttachments is what hands the attachments back. */
    deliver(ws, echo(2, { withImages: false }));
    expectImageRendered(container, PREVIEW, { delivery: 2, bubbleCount: 2 });

    deliver(ws, echo(3, { withImages: false }));
    expectImageRendered(container, PREVIEW, { delivery: 3, bubbleCount: 3 });
  });

  it('keeps the preview for a re-mounted tab (registry is not drained by the first echo)', async () => {
    registerPastedImagePreviews([{ dataUrl: PREVIEW, mediaType: 'image/png' }], QUERY);

    const first = await mountTab();
    deliver(first.ws, [userRow(), ANSWER_ROW]);
    expectImageRendered(first.container, PREVIEW, { delivery: 1, bubbleCount: 2 });

    /* Tab closed and re-opened (the registry is module state and survives it;
     * the store does not). The preview is still remembered by server image id,
     * so the picture paints instantly again instead of waiting on the durable
     * fetch — that is the whole point of NOT draining on first use.
     *
     * The store is cleared before EVERY delivery so no previous row is around
     * for preserveImageAttachments to rescue the attachments from: the registry
     * is then the only thing that can supply the preview. */
    cleanup();
    useStore.getState().reset();

    const second = await mountTab();
    for (let n = 1; n <= 3; n += 1) {
      act(() => useStore.getState().reset());
      deliver(second.ws, [userRow(), ANSWER_ROW]);
      expectImageRendered(second.container, PREVIEW, { delivery: n, bubbleCount: 2 });
    }
  });

  it('renders from the durable URL after a reload (registry gone, fresh mount)', async () => {
    registerPastedImagePreviews([{ dataUrl: PREVIEW, mediaType: 'image/png' }], QUERY);

    const first = await mountTab();
    deliver(first.ws, [userRow(), ANSWER_ROW]);
    expectImageRendered(first.container, PREVIEW, { delivery: 1, bubbleCount: 2 });

    /* Reload: the page is gone, so is every preview the registry held. A fresh
     * mount can only use the durable route. */
    cleanup();
    clearPendingImagePreviews();
    useStore.getState().reset();

    const second = await mountTab();
    for (let n = 1; n <= 2; n += 1) {
      deliver(second.ws, [userRow(), ANSWER_ROW]);
      expectImageRendered(second.container, DURABLE_SRC, { delivery: n, bubbleCount: 2 });
    }
  });
});
