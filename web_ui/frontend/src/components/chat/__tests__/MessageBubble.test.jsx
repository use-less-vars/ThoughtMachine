// @vitest-environment jsdom
/*
 * MessageBubble.test.jsx
 *
 * Streaming render fast path. While an assistant message is still arriving its
 * text changes every animation frame, and ReactMarkdown re-parses + re-renders
 * the whole document per frame — the dominant cost of a live turn (and the
 * markdown of a half-written document is wrong anyway: an unterminated `**`
 * flickers between states). So `msg.is_streaming` selects plain pre-wrapped
 * text, and only a final message goes through ReactMarkdown.
 *
 * The comparator cases below are regression guards for the hand-written
 * `React.memo` comparator in MessageBubble.jsx: without an `is_streaming` term
 * the flip from plain text to markdown is swallowed by memo and the message
 * stays literal forever.
 */

import React from 'react';
import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { MessageBubble } from '../MessageBubble';

afterEach(cleanup);

describe('MessageBubble — streaming render fast path', () => {
  it('renders streaming text literally (no markdown parse)', () => {
    render(
      <MessageBubble
        msg={{ role: 'assistant', content: 'a **bold** word', is_streaming: true }}
        index={0}
      />
    );

    // The raw markdown survives verbatim into the DOM.
    expect(document.body.textContent).toContain('a **bold** word');
    // ...and nothing was parsed into markdown nodes.
    expect(document.querySelector('strong')).toBeNull();
  });

  it('renders markdown once the message is final', () => {
    render(
      <MessageBubble msg={{ role: 'assistant', content: 'a **bold** word' }} index={0} />
    );

    const strong = document.querySelector('strong');
    expect(strong).not.toBeNull();
    expect(strong.textContent).toBe('bold');
    // The literal source is gone: it was consumed by the parser.
    expect(document.body.textContent).not.toContain('**bold**');
  });

  it('re-renders when is_streaming flips on the same message (memo comparator)', () => {
    const { rerender } = render(
      <MessageBubble
        msg={{ role: 'assistant', content: 'a **bold** word', is_streaming: true }}
        index={0}
      />
    );
    expect(document.querySelector('strong')).toBeNull();

    // Same element instance, same index, same content — ONLY the streaming flag
    // changes. Without `prev.msg.is_streaming === next.msg.is_streaming` in the
    // comparator, React.memo bails out and this stays literal text forever.
    rerender(<MessageBubble msg={{ role: 'assistant', content: 'a **bold** word' }} index={0} />);

    const strong = document.querySelector('strong');
    expect(strong).not.toBeNull();
    expect(strong.textContent).toBe('bold');
  });

  it('renders streaming reasoning as plain text inside the thinking block', () => {
    render(
      <MessageBubble
        msg={{ role: 'assistant', content: '', reasoning_content: 'ponder **ing**', is_streaming: true }}
        index={0}
      />
    );

    const block = document.querySelector('.reasoning-block');
    expect(block).not.toBeNull();
    expect(block.textContent).toContain('ponder **ing**');
    expect(document.querySelector('strong')).toBeNull();
  });

  it('renders final reasoning as markdown inside the thinking block', () => {
    render(
      <MessageBubble
        msg={{ role: 'assistant', content: '', reasoning_content: 'ponder **ing**' }}
        index={0}
      />
    );

    const block = document.querySelector('.reasoning-block');
    expect(block).not.toBeNull();
    const strong = block.querySelector('strong');
    expect(strong).not.toBeNull();
    expect(strong.textContent).toBe('ing');
    expect(block.textContent).not.toContain('**ing**');
  });
});

/*
 * D1 — the thinking block is collapsible.
 *
 * `open` used to be hardcoded on the <details>, so a user who collapsed the
 * Thinking block had it snap back open on the very next delta (the message
 * re-renders on every streamed frame). The block now owns its open state, but
 * it must NOT leak that state onto a different message: ChatPanel keys bubbles
 * by array index (`messages.map((msg, i) => <MessageBubble key={i} .../>)`), so
 * a load-more prepend or a whole-list `conversation_changed` replace hands the
 * same instance a different message, and messages carry no id.
 */
describe('MessageBubble — collapsible thinking block', () => {
  const block = () => document.querySelector('details.reasoning-block');

  // React 18 attaches `toggle` as a NON-delegated listener directly on the
  // <details> node (jsdom dispatches no toggle event of its own), so the event
  // is fired at the element itself rather than at a container.
  function setOpen(details, open) {
    details.open = open;
    fireEvent(details, new Event('toggle'));
  }

  it('renders the thinking block open by default', () => {
    render(
      <MessageBubble msg={{ role: 'assistant', content: '', reasoning_content: 'step one' }} index={0} />
    );

    expect(block()).not.toBeNull();
    expect(block().open).toBe(true);
  });

  it('keeps a user-collapsed thinking block collapsed while the reasoning streams on', () => {
    const { rerender } = render(
      <MessageBubble
        msg={{ role: 'assistant', content: '', reasoning_content: 'step one', is_streaming: true }}
        index={0}
      />
    );
    expect(block().open).toBe(true);

    setOpen(block(), false);
    expect(block().open).toBe(false);

    // Append-only growth of the SAME message — a live continuation.
    rerender(
      <MessageBubble
        msg={{ role: 'assistant', content: '', reasoning_content: 'step one, step two', is_streaming: true }}
        index={0}
      />
    );

    expect(block().open).toBe(false);
    expect(block().textContent).toContain('step one, step two');
  });

  it('stays open again after the user re-expands it', () => {
    const { rerender } = render(
      <MessageBubble
        msg={{ role: 'assistant', content: '', reasoning_content: 'step one', is_streaming: true }}
        index={0}
      />
    );

    setOpen(block(), false);
    expect(block().open).toBe(false);

    setOpen(block(), true);
    expect(block().open).toBe(true);

    // ...and the re-opened state survives further streaming.
    rerender(
      <MessageBubble
        msg={{ role: 'assistant', content: '', reasoning_content: 'step one, step two', is_streaming: true }}
        index={0}
      />
    );
    expect(block().open).toBe(true);
  });

  it('does not carry a collapsed state onto a different message in the same slot', () => {
    const { rerender } = render(
      <MessageBubble msg={{ role: 'assistant', content: '', reasoning_content: 'first reasoning' }} index={0} />
    );

    setOpen(block(), false);
    expect(block().open).toBe(false);

    // Index keying re-uses this very instance for a different message.
    rerender(
      <MessageBubble msg={{ role: 'assistant', content: '', reasoning_content: 'unrelated reasoning' }} index={0} />
    );

    expect(block().open).toBe(true);
    expect(block().textContent).toContain('unrelated reasoning');
  });
});
