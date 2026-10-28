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
import { render, cleanup } from '@testing-library/react';
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
