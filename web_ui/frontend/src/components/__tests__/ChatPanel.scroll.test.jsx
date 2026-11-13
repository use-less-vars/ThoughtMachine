// @vitest-environment jsdom
/*
 * ChatPanel.scroll.test.jsx
 *
 * D2 — auto-scroll must follow the THINKING phase, not just the answer body.
 *
 * `ChatPanel` treats "the count is unchanged but the last message's text grew"
 * as an append (a streaming delta), so a reader parked at the bottom keeps
 * following the stream. That in-place-growth case looked at `content` only,
 * while the thinking phase grows `reasoning_content` and leaves `content`
 * empty — so every reasoning frame looked like "nothing changed" and the effect
 * took its restore-scrollTop branch instead: the panel silently froze for the
 * whole reasoning phase and jumped only when the first answer chunk landed.
 *
 * The cases themselves now live in the SHARED suite (scrollBehaviour.suite.jsx)
 * so ChatPanel and WorkerOutputPanel are held to the same behaviour by the same
 * code, and the reasoning-phase fixtures below are what ChatPanel feeds it.
 */
import React from 'react'
import ChatPanel from '../ChatPanel'
import { registerScrollBehaviour } from './scrollBehaviour.suite.jsx'

const USER = { role: 'user', content: 'why is the sky blue' }

function assistant(reasoningContent) {
  return {
    role: 'assistant',
    content: '',
    reasoning_content: reasoningContent,
    is_streaming: true,
  }
}

// Step 1 differs from step 0 ONLY by the reasoning text: same message count,
// same empty `content`. The array identities are memoised because ChatPanel's
// own append detector is keyed on the `messages` reference.
const MESSAGES = [
  [USER, assistant('step one')],
  [USER, assistant('step one, step two')],
]

registerScrollBehaviour({
  name: 'ChatPanel',
  element: (step) => <ChatPanel messages={MESSAGES[step]} />,
  // memo(ChatPanel) shallow-compares: a fresh-but-equal array forces the
  // post-stub commit the baseline measurement needs.
  bumpElement: (step) => <ChatPanel messages={[...MESSAGES[step]]} />,
  container: () => document.querySelector('.chat-panel'),
  // Growth stays 0 on purpose: the panel-side reasoning detector is what these
  // cases exist to protect, and a grown box would let the hook's per-commit
  // glue hide a broken detector.
  growthDelta: 0,
})
