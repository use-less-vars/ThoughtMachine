// @vitest-environment jsdom
// --- MessageBubble.test.jsx ---
// Component under test: ../chat/MessageBubble (default export + named exports).
//
// Pins the TWO system-bubble families and their visual separation:
//
//   * persisted notice — role 'system' (or role 'user' + is_system_notification)
//     as delivered by the backend. Rendered with the historic, enclosed
//     .message-system-as-user bubble and WITHOUT .message-ephemeral. The
//     backend's transport tag ('[SYSTEM NOTIFICATION]', see
//     agent/core/message.py:SYSTEM_NOTIFICATION_PREFIX) is not part of the
//     text the operator wrote, so it is stripped from the visible content and
//     kept verbatim in the element's `title`.
//   * ephemeral notice — the same role/flags plus client_only === true
//     (status_message acks, worker paused/stopped/resumed). Those frames are
//     never echoed back by the server and vanish on the next
//     conversation_changed replace, so they additionally carry the
//     .message-ephemeral modifier (muted, dashed).
//
// The wrapper stripping must NOT touch a real user message that happens to
// start with the tag. The backend recognises notices by content
// (agent/core/message.py:is_system_notice_text) as well as through the
// explicit is_system_notification field, but the renderer still only strips
// once a message already renders as system -- so a genuine user message
// keeps its text verbatim.

import React from 'react'
import { describe, it, expect, afterEach } from 'vitest'
import { render, cleanup } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import MessageBubble, { stripSystemNoticeWrapper } from '../chat/MessageBubble'

afterEach(cleanup)

// ── Helpers ────────────────────────────────────────────────────────────────
function renderBubble(msg, index = 0) {
  const { container } = render(<MessageBubble msg={msg} index={index} />)
  const bubble = container.querySelector('.message')
  const paragraph = container.querySelector('.system-text, .message-text')
  return { bubble, paragraph }
}

// ==========================================================================
// 1. stripSystemNoticeWrapper
// ==========================================================================
describe('stripSystemNoticeWrapper', () => {
  it('removes the leading [SYSTEM NOTIFICATION] tag', () => {
    expect(stripSystemNoticeWrapper('[SYSTEM NOTIFICATION] Heads up')).toBe('Heads up')
  })

  it('removes the tag when it is followed by a colon', () => {
    expect(stripSystemNoticeWrapper('[SYSTEM NOTIFICATION]: Heads up')).toBe('Heads up')
  })

  it('matches the tag case-insensitively', () => {
    expect(stripSystemNoticeWrapper('[system notification] heads up')).toBe('heads up')
  })

  it('removes the [System Notice: wrapper', () => {
    expect(stripSystemNoticeWrapper('[System Notice: heads up')).toBe('heads up')
  })

  it('strips the closing bracket of the [System Notice: ...] family', () => {
    expect(stripSystemNoticeWrapper('[System Notice: You were paused by the user. Do not call tools.]'))
      .toBe('You were paused by the user. Do not call tools.')
  })

  it('strips the closing bracket after trailing whitespace, exactly once', () => {
    expect(stripSystemNoticeWrapper('[System Notice: heads up]  ')).toBe('heads up')
  })

  it('returns the original when the [System Notice: ...] body is empty', () => {
    expect(stripSystemNoticeWrapper('[System Notice: ]')).toBe('[System Notice: ]')
  })

  it('preserves a legacy body that ends in its own bracket', () => {
    expect(stripSystemNoticeWrapper('[SYSTEM NOTIFICATION] see [docs]')).toBe('see [docs]')
  })

  it('strips the legacy tag and leaves the body verbatim', () => {
    expect(stripSystemNoticeWrapper('[SYSTEM NOTIFICATION] plain text')).toBe('plain text')
  })

  it('leaves unwrapped text untouched', () => {
    expect(stripSystemNoticeWrapper('Session started.')).toBe('Session started.')
  })

  it('leaves a tag that is not a prefix untouched', () => {
    expect(stripSystemNoticeWrapper('Heads up [SYSTEM NOTIFICATION]')).toBe('Heads up [SYSTEM NOTIFICATION]')
  })

  it('returns the original text when stripping would leave nothing', () => {
    expect(stripSystemNoticeWrapper('[SYSTEM NOTIFICATION]')).toBe('[SYSTEM NOTIFICATION]')
  })

  it('is defensive about null / undefined / non-strings', () => {
    expect(stripSystemNoticeWrapper(null)).toBe('')
    expect(stripSystemNoticeWrapper(undefined)).toBe('')
    expect(stripSystemNoticeWrapper(42)).toBe('42')
  })
})

// ==========================================================================
// 2. Persisted system notice (unchanged bubble, tag hidden)
// ==========================================================================
describe('persisted system notice', () => {
  it('keeps the enclosed system-as-user bubble and adds no ephemeral modifier', () => {
    const { bubble } = renderBubble({ role: 'system', content: 'Heads up' })
    expect(bubble.classList.contains('message-system-as-user')).toBe(true)
    expect(bubble.classList.contains('message-ephemeral')).toBe(false)
  })

  it('hides the transport tag from the visible text and keeps it in title', () => {
    const raw = '[SYSTEM NOTIFICATION] Agent restart required'
    const { paragraph } = renderBubble({ role: 'system', content: raw })
    expect(paragraph).toHaveTextContent('Agent restart required')
    expect(paragraph.textContent).not.toContain('[SYSTEM NOTIFICATION]')
    expect(paragraph.getAttribute('title')).toBe(raw)
  })

  it('leaves title unset for content that carries no tag', () => {
    const { paragraph } = renderBubble({ role: 'system', content: 'Token usage has returned to safe levels.' })
    expect(paragraph.getAttribute('title')).toBeNull()
    expect(paragraph).toHaveTextContent('Token usage has returned to safe levels.')
  })

  it('renders role user + is_system_notification through the same path', () => {
    const raw = '[SYSTEM NOTIFICATION] Turn limit near (Turns: 18)'
    const { bubble, paragraph } = renderBubble({ role: 'user', content: raw, is_system_notification: true })
    expect(bubble.classList.contains('message-system-as-user')).toBe(true)
    expect(bubble.classList.contains('message-ephemeral')).toBe(false)
    expect(paragraph.textContent).toBe('Turn limit near (Turns: 18)')
  })
})

// ==========================================================================
// 3. Ephemeral (client-only) notice
// ==========================================================================
describe('ephemeral client-only notice', () => {
  it('adds the message-ephemeral modifier alongside the system role class', () => {
    const { bubble } = renderBubble({ role: 'system', content: '⏸ Pausing…', client_only: true })
    expect(bubble.classList.contains('message-system-as-user')).toBe(true)
    expect(bubble.classList.contains('message-ephemeral')).toBe(true)
  })

  it('treats only client_only === true as ephemeral', () => {
    const { bubble } = renderBubble({ role: 'system', content: '⏸ Pausing…', client_only: 'yes' })
    expect(bubble.classList.contains('message-ephemeral')).toBe(false)
  })

  it('never marks an ordinary assistant message ephemeral', () => {
    const { bubble } = renderBubble({ role: 'assistant', content: 'plain answer' })
    expect(bubble.classList.contains('message-ephemeral')).toBe(false)
  })
})

// ==========================================================================
// 4. Tag stripping must not leak onto real user content
// ==========================================================================
describe('user message starting with the tag', () => {
  it('is rendered verbatim (no flag, no strip)', () => {
    const raw = '[SYSTEM NOTIFICATION] this is literally what I typed'
    const { bubble, paragraph } = renderBubble({ role: 'user', content: raw })
    expect(bubble.classList.contains('message-user')).toBe(true)
    expect(bubble.classList.contains('message-ephemeral')).toBe(false)
    expect(paragraph.textContent).toBe(raw)
    expect(paragraph.getAttribute('title')).toBeNull()
  })
})
