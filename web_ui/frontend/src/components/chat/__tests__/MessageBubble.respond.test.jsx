// @vitest-environment jsdom
/*
 * MessageBubble.respond.test.jsx
 *
 * A `Respond` tool call IS the assistant's answer: its arguments carry a
 * markdown `content` document plus an optional report attachment. It used to be
 * rendered by ToolCallContent as pretty-printed JSON, and when the backend's
 * own `json.loads` of the provider's `arguments` failed it kept that string
 * verbatim (session_manager.py:686) and re-encoded it with json.dumps — so the
 * operator saw escaped markdown source (`\"content\":`, `\n`, `\u2014`) instead
 * of their message.
 *
 * These cases pin three things: the markdown pipeline is used, a STRING
 * `arguments` payload renders identically to an object one (the regression),
 * and a malformed payload still shows the raw text instead of blanking.
 */

import React from 'react';
import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { MessageBubble } from '../MessageBubble';

afterEach(cleanup);

const CONTENT_MARKDOWN = [
  '# Quarterly Report',
  '',
  'Revenue rose — sharply — this quarter.',
  '',
  '| Metric | Value |',
  '| ------ | ----- |',
  '| ARR    | 42    |',
].join('\n');

const ARG_OBJECT = {
  content: CONTENT_MARKDOWN,
  report_title: 'Q4 attachment',
  report_body: '# Attachment\n\nBODY-MARKER-TEXT',
};

/* The healthy shape: the backend parsed `arguments` itself, so the envelope
 * carries a real object. */
function envelopeWithObjectArguments(args) {
  return JSON.stringify({ name: 'Respond', arguments: args });
}

/* The failing shape: the backend kept `arguments` as the raw string it could
 * not parse, then json.dumps re-escaped it. This is what produced the bug. */
function envelopeWithStringArguments(args) {
  return JSON.stringify({ name: 'Respond', arguments: JSON.stringify(args) });
}

function renderToolCall(content) {
  return render(<MessageBubble msg={{ role: 'tool_call', content }} index={0} />);
}

describe('MessageBubble — Respond tool call renders as an assistant message', () => {
  it('renders object arguments through the markdown pipeline', () => {
    renderToolCall(envelopeWithObjectArguments(ARG_OBJECT));

    const block = document.querySelector('.assistant-markdown');
    expect(block).not.toBeNull();

    const heading = block.querySelector('h1');
    expect(heading).not.toBeNull();
    expect(heading.textContent).toBe('Quarterly Report');

    // A table cell only exists if remark-gfm ran over real markdown.
    const cell = block.querySelector('td');
    expect(cell).not.toBeNull();
    expect(cell.textContent).toBe('ARR');

    // The regression: JSON-escaped source must never reach the rendered message.
    expect(block.textContent).not.toContain('\\"content\\":');
    expect(block.textContent).not.toContain('\\n');
  });

  it('renders a STRING arguments payload identically to an object one', () => {
    const { unmount } = renderToolCall(envelopeWithObjectArguments(ARG_OBJECT));
    const fromObject = document.querySelector('.assistant-markdown').innerHTML;
    unmount();

    renderToolCall(envelopeWithStringArguments(ARG_OBJECT));
    const fromString = document.querySelector('.assistant-markdown').innerHTML;

    // Same DOM, byte for byte: the double-encoded payload is decoded, not shown.
    expect(fromString).toBe(fromObject);
    expect(document.querySelector('.assistant-markdown h1').textContent).toBe('Quarterly Report');
    expect(document.body.textContent).not.toContain('\\"content\\":');
  });

  it('falls back to the raw text when arguments are malformed (and never throws)', () => {
    // Truncated JSON — exactly what a bad round trip leaves behind.
    const content = JSON.stringify({ name: 'Respond', arguments: '{"content": "# Broken' });

    expect(() => renderToolCall(content)).not.toThrow();

    const raw = document.querySelector('.tool-call-raw');
    expect(raw).not.toBeNull();
    expect(raw.textContent).toBe(content);
    // Nothing was invented in place of the unparseable payload.
    expect(document.querySelector('.assistant-markdown')).toBeNull();
  });

  it('leaves non-Respond tool calls on the untouched JSON display', () => {
    renderToolCall(JSON.stringify({ name: 'Bash', arguments: { command: 'ls' } }));

    expect(document.querySelector('.tool-call-args').textContent).toContain('"command": "ls"');
    expect(document.querySelector('.assistant-markdown')).toBeNull();
  });
});

describe('MessageBubble — Respond report attachment', () => {
  it('shows the report as a collapsed chip and keeps the body out of the DOM until expanded', () => {
    renderToolCall(envelopeWithStringArguments(ARG_OBJECT));

    const attachment = document.querySelector('details.respond-attachment');
    expect(attachment).not.toBeNull();
    expect(attachment.open).toBe(false);
    expect(attachment.querySelector('summary').textContent).toContain('Q4 attachment');

    // The body is not mounted at all while collapsed — it is an attachment,
    // not inline text.
    expect(document.querySelector('.respond-attachment-body')).toBeNull();
    expect(attachment.textContent).not.toContain('BODY-MARKER-TEXT');
  });

  it('renders the report body as markdown once expanded', () => {
    renderToolCall(envelopeWithStringArguments(ARG_OBJECT));

    const attachment = document.querySelector('details.respond-attachment');
    // React 18 attaches `toggle` directly to the <details> node and jsdom
    // dispatches none of its own, so fire it at the element.
    attachment.open = true;
    fireEvent(attachment, new Event('toggle'));

    const body = document.querySelector('.respond-attachment-body');
    expect(body).not.toBeNull();
    expect(body.querySelector('h1').textContent).toBe('Attachment');
    expect(body.textContent).toContain('BODY-MARKER-TEXT');
  });

  it('keeps the raw JSON available but collapsed by default', () => {
    renderToolCall(envelopeWithStringArguments(ARG_OBJECT));

    const raw = document.querySelector('details.respond-raw');
    expect(raw).not.toBeNull();
    expect(raw.open).toBe(false);
    expect(raw.textContent).toContain('Respond');
  });
});
