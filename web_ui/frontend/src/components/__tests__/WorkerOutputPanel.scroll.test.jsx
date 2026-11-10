// @vitest-environment jsdom
/*
 * WorkerOutputPanel.scroll.test.jsx
 *
 * The worker panel is the second consumer of `hooks/useStickToBottom` and the
 * one that historically drifted: it carried its own copy of the stick-to-bottom
 * logic, never received the ChatPanel fixes, and a worker stream was left
 * behind whenever the reader was parked at the bottom. It now runs the SAME
 * shared suite as ChatPanel (scrollBehaviour.suite.jsx), so that drift cannot
 * come back silently.
 *
 * Harness notes:
 *  - The scroll region only renders when `workerName` is truthy (the
 *    `worker-output-no-selection` early return), which is exactly the
 *    `enabled: !!workerName` gate the hook is given.
 *  - "Growth" here is an APPENDED event, not an in-place edit: the panel dedups
 *    incoming events by `makeDedupKey(type, timestamp, tool_call_id)`, so a
 *    second event with the same timestamp is dropped rather than updating the
 *    first.
 *  - `growthDelta` is +400 because the panel has no append-side
 *    `scrollToBottom()` of its own — the hook's per-commit glue is its only
 *    writer, so the stubbed box has to actually grow for the case to mean
 *    anything.
 */
import React from 'react'
import { vi } from 'vitest'
import WorkerOutputPanel from '../WorkerOutputPanel'
import useStore from '../../store/useStore'
import { registerScrollBehaviour } from './scrollBehaviour.suite.jsx'

const WORKER = 'w-scroll'
const PROPS = {
  workspaceId: 'ws-scroll-1',
  workerName: WORKER,
  instanceId: 'inst-scroll-1',
  instanceLabel: 'scroll-worker',
  sessionId: 'sess-scroll',
  onClose: () => {},
}

const T0 = '2026-08-15T00:00:00.000Z'
const T1 = '2026-08-15T00:00:01.000Z'

const ev = (content, timestamp) => ({
  type: 'worker_message',
  worker_name: WORKER,
  data: { content },
  timestamp,
})

const EVENTS = [
  [ev('first chunk', T0)],
  [ev('first chunk', T0), ev('second chunk', T1)],
]

registerScrollBehaviour({
  name: 'WorkerOutputPanel',
  element: (step) => <WorkerOutputPanel {...PROPS} incomingEvents={EVENTS[step]} />,
  // The memo comparator treats same-length arrays of identical elements as
  // equal (it compares by reference, then length, then element identity), so
  // the bump has to move a prop it actually compares: `size`.
  bumpElement: (step, n) => (
    <WorkerOutputPanel {...PROPS} size={n % 2 === 1 ? 360 : 350} incomingEvents={EVENTS[step]} />
  ),
  container: () => document.querySelector('.worker-output-scroll'),
  growthDelta: 400,
  beforeEach: () => {
    localStorage.clear()
    useStore.getState().reset()
  },
})
