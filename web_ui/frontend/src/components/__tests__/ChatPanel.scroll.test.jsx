// @vitest-environment jsdom
/*
 * ChatPanel.scroll.test.jsx
 *
 * D2 — auto-scroll must follow the THINKING phase, not just the answer body.
 *
 * `ChatPanel` treats "the count is unchanged but the last message's text grew"
 * as an append (a streaming delta), so a reader parked at the bottom keeps
 * following the stream. That in-place-growth test looked at `content` only,
 * while the thinking phase grows `reasoning_content` and leaves `content`
 * empty — so every reasoning frame looked like "nothing changed" and the effect
 * took its restore-scrollTop branch instead: the panel silently froze for the
 * whole reasoning phase and jumped only when the first answer chunk landed.
 *
 * jsdom performs no layout, so the container's scroll metrics are stubbed
 * (scrollHeight 1000 / clientHeight 300 / writable scrollTop) and rAF is
 * replaced by a 0ms timer to make the component's double-rAF deterministic.
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, act } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import ChatPanel from '../ChatPanel';

const SCROLL_HEIGHT = 1000;
const CLIENT_HEIGHT = 300;

// jsdom lacks ResizeObserver; ChatPanel constructs one on mount.
class MockResizeObserver {
  constructor(callback) {
    this.callback = callback;
  }
  observe() {}
  unobserve() {}
  disconnect() {}
}

/* Give the (layout-less) scroll container real-looking metrics. */
function stubScrollMetrics(el) {
  let scrollTop = 0;
  Object.defineProperty(el, 'scrollHeight', { configurable: true, value: SCROLL_HEIGHT });
  Object.defineProperty(el, 'clientHeight', { configurable: true, value: CLIENT_HEIGHT });
  Object.defineProperty(el, 'scrollTop', {
    configurable: true,
    get: () => scrollTop,
    set: (value) => {
      scrollTop = value;
    },
  });
  el.__getScrollTop = () => scrollTop;
}

/* Flush the component's nested requestAnimationFrame callbacks. */
async function flushFrames() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 20));
  });
}

function panel() {
  return document.querySelector('.chat-panel');
}

const USER = { role: 'user', content: 'why is the sky blue' };

function assistant(reasoningContent) {
  return {
    role: 'assistant',
    content: '',
    reasoning_content: reasoningContent,
    is_streaming: true,
  };
}

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', MockResizeObserver);
  // Deterministic double-rAF: each frame is a 0ms macrotask.
  vi.stubGlobal('requestAnimationFrame', (cb) => setTimeout(() => cb(0), 0));
  vi.stubGlobal('cancelAnimationFrame', (id) => clearTimeout(id));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('ChatPanel — auto-scroll during the reasoning phase', () => {
  it('keeps the view pinned to the bottom while only reasoning_content grows', async () => {
    const { rerender } = render(<ChatPanel messages={[USER, assistant('step one')]} />);
    stubScrollMetrics(panel());

    // Initial mount: the reader is at the bottom, so the panel pins itself there.
    await flushFrames();
    expect(panel().__getScrollTop()).toBe(SCROLL_HEIGHT);

    // Same message count, same `content` (still empty) — only the reasoning
    // text grew. This is an append in every way that matters to the reader.
    rerender(<ChatPanel messages={[USER, assistant('step one, step two')]} />);
    await flushFrames();

    // Pre-fix this fell into the restore-scrollTop branch and reset to 0.
    expect(panel().__getScrollTop()).toBe(SCROLL_HEIGHT);
  });

  it('still restores a scrolled-up reader position when only reasoning grows', async () => {
    const { rerender } = render(<ChatPanel messages={[USER, assistant('step one')]} />);
    const el = panel();
    stubScrollMetrics(el);
    await flushFrames();

    // The user scrolls up to read earlier content.
    act(() => {
      el.scrollTop = 120;
      el.dispatchEvent(new Event('scroll'));
    });

    rerender(<ChatPanel messages={[USER, assistant('step one, step two')]} />);
    await flushFrames();

    // Not pinned to the bottom, and not yanked back to the top either.
    expect(el.__getScrollTop()).toBe(120);
  });
});
