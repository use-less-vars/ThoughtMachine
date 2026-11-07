// @vitest-environment jsdom
/**
 * WorkerOutputPanel.test.jsx — R6: worker-event isolation + stale-header fix.
 *
 * 1. ISOLATION — the panel must only render events whose worker_name matches
 *    its workerName prop. Events for OTHER workers, and events without any
 *    worker_name (main-agent events), must be dropped from both the event
 *    stream AND the live workerInfo updates (ctx counter, current_task).
 *
 * 2. STALE HEADER — App renders the panel WITHOUT a sessionId key, so when
 *    two sessions have the same worker selected, switching sessions changes
 *    the sessionId prop without remounting. The panel must reset all worker
 *    state (ctx counter, task, event stream) when sessionId changes.
 *    Regression guard for the reset effect deps
 *    [workspaceId, workerName, sessionId].
 */
import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor, act } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import WorkerOutputPanel from '../WorkerOutputPanel'
import useStore from '../../store/useStore'

class MockResizeObserver {
  constructor(callback) {
    this.callback = callback
  }
  observe() {}
  unobserve() {}
  disconnect() {}
}

function panelProps(overrides = {}) {
  return {
    workspaceId: 'ws-test-1',
    workerName: 'w1',
    sessionId: 'sess-A',
    onClose: vi.fn(),
    incomingEvents: [],
    ...overrides,
  }
}

function renderPanel(props = {}) {
  return render(<WorkerOutputPanel {...panelProps(props)} />)
}

function ctxText() {
  return document.querySelector('.worker-output-header-ctx')?.textContent ?? null
}

function taskInline() {
  return document.querySelector('.worker-output-header-task-inline')?.textContent ?? null
}

function emptyState() {
  return document.querySelector('.worker-output-empty')?.textContent ?? null
}
function dotEl() {
  return document.querySelector('.worker-status-dot')
}

function dotLabel() {
  return document.querySelector('.worker-status-label')?.textContent ?? null
}



// ── Event fixtures (raw WebSocket shapes) ────────────────────────────────
const ctxFor = (worker, length, ts) => ({
  type: 'context_updated',
  worker_name: worker,
  context_length: length,
  critical_threshold: 80000,
  timestamp: ts,
})
const msgFor = (worker, content, ts) => ({
  type: 'worker_message',
  worker_name: worker,
  data: { content },
  timestamp: ts,
})
const statusFor = (worker, status, task, ts) => ({
  type: 'worker_status',
  worker_name: worker,
  data: { runtime_status: status, current_task: task },
  timestamp: ts,
})

const pausedFor = (worker, ts) => ({
  type: 'worker_paused',
  worker_name: worker,
  data: {},
  timestamp: ts,
})
const errorFor = (worker, ts, message = 'worker exploded') => ({
  type: 'worker_error',
  worker_name: worker,
  data: { error: message },
  timestamp: ts,
})
const completedFor = (worker, ts) => ({
  type: 'worker_completed',
  worker_name: worker,
  data: {},
  timestamp: ts,
})

beforeEach(() => {
  localStorage.clear()
  useStore.getState().reset()
  vi.stubGlobal('ResizeObserver', MockResizeObserver)
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('WorkerOutputPanel — worker-event isolation', () => {
  it('renders bubbles for its own worker events', async () => {
    const { rerender } = renderPanel()
    await act(async () => {
      rerender(
        <WorkerOutputPanel
          {...panelProps({ incomingEvents: [msgFor('w1', 'hello from w1', '2026-08-15T00:00:00.000Z')] })}
        />
      )
    })
    await waitFor(() => {
      expect(screen.getByText('hello from w1')).toBeInTheDocument()
    })
  })

  it('drops events from other workers and events without worker_name', async () => {
    const { rerender } = renderPanel()
    await act(async () => {
      rerender(
        <WorkerOutputPanel
          {...panelProps({
            incomingEvents: [
              msgFor('w1', 'hello from w1', '2026-08-15T00:00:00.000Z'),
              msgFor('w2', 'hello from w2', '2026-08-15T00:00:01.000Z'),
              // No worker_name at all → main-agent event, must be dropped
              { type: 'worker_message', data: { content: 'main agent note' }, timestamp: '2026-08-15T00:00:02.000Z' },
            ],
          })}
        />
      )
    })
    await waitFor(() => {
      expect(screen.getByText('hello from w1')).toBeInTheDocument()
    })
    expect(screen.queryByText('hello from w2')).not.toBeInTheDocument()
    expect(screen.queryByText('main agent note')).not.toBeInTheDocument()
  })

  it('ignores live ctx updates from other workers', async () => {
    const { rerender } = renderPanel()
    await act(async () => {
      rerender(
        <WorkerOutputPanel
          {...panelProps({ incomingEvents: [ctxFor('w1', 12345, '2026-08-15T00:00:00.000Z')] })}
        />
      )
    })
    await waitFor(() => {
      expect(ctxText()).toBe('ctx: 12.3K / 80.0K')
    })
    // A second batch adds a context_updated for w2 — it must NOT overwrite
    // w1's counter in the header.
    await act(async () => {
      rerender(
        <WorkerOutputPanel
          {...panelProps({
            incomingEvents: [
              ctxFor('w1', 12345, '2026-08-15T00:00:03.000Z'),
              ctxFor('w2', 99999, '2026-08-15T00:00:04.000Z'),
            ],
          })}
        />
      )
    })
    expect(ctxText()).toBe('ctx: 12.3K / 80.0K')
  })
})

describe('WorkerOutputPanel — stale header on session switch (R6 fix)', () => {
  it('resets ctx counter, task, and event stream when sessionId changes without remount', async () => {
    const { rerender } = renderPanel()

    // Session A: worker w1 reports live state (ctx counter + current task).
    await act(async () => {
      rerender(
        <WorkerOutputPanel
          {...panelProps({
            incomingEvents: [
              ctxFor('w1', 12345, '2026-08-15T00:00:00.000Z'),
              statusFor('w1', 'busy', 'Write the R6 report', '2026-08-15T00:00:01.000Z'),
            ],
          })}
        />
      )
    })
    await waitFor(() => {
      expect(ctxText()).toBe('ctx: 12.3K / 80.0K')
    })
    expect(taskInline()).toContain('Write the R6 report')

    // Switch to session B — same workspace + workerName, new sessionId.
    // App does NOT key the panel by sessionId, so the component stays
    // mounted and only its props change.
    await act(async () => {
      rerender(
        <WorkerOutputPanel {...panelProps({ sessionId: 'sess-B', incomingEvents: [] })} />
      )
    })

    // All worker state from session A must be gone:
    expect(ctxText()).toBe('ctx: —')
    expect(taskInline()).toBeNull()
    await waitFor(() => {
      expect(emptyState()).toContain('Worker output appears here')
    })
  })
})

describe('WorkerOutputPanel — header status dot follows the worker runtime_status (F5)', () => {
  it('defaults to a grey Idle dot while no worker status has been reported', () => {
    renderPanel()
    expect(dotLabel()).toBe('Idle')
    expect(dotEl()).not.toHaveClass('worker-status-dot-busy')
    expect(dotEl().style.background).toBe('rgb(88, 91, 112)')
    expect(dotEl().getAttribute('title')).toBe('Worker is idle')
  })

  it('shows a green Running dot (with pulse) while the worker is busy', async () => {
    const { rerender } = renderPanel()
    await act(async () => {
      rerender(
        <WorkerOutputPanel
          {...panelProps({ incomingEvents: [statusFor('w1', 'busy', 'Write report', '2026-08-15T00:00:01.000Z')] })}
        />
      )
    })
    await waitFor(() => expect(dotLabel()).toBe('Running'))
    expect(dotEl()).toHaveClass('worker-status-dot-busy')
    expect(dotEl().style.background).toBe('rgb(166, 227, 161)')
    expect(dotEl().getAttribute('title')).toBe('Worker is busy')
  })

  it('maps a "running" status to the same green Running dot', async () => {
    const { rerender } = renderPanel()
    await act(async () => {
      rerender(
        <WorkerOutputPanel
          {...panelProps({ incomingEvents: [statusFor('w1', 'running', 'Write report', '2026-08-15T00:00:01.000Z')] })}
        />
      )
    })
    await waitFor(() => expect(dotLabel()).toBe('Running'))
    expect(dotEl()).toHaveClass('worker-status-dot-busy')
    expect(dotEl().style.background).toBe('rgb(166, 227, 161)')
    expect(dotEl().getAttribute('title')).toBe('Worker is running')
  })

  it('is independent of the session isRunning flag: paused worker → amber Paused even while the session runs', async () => {
    // The owning session is running, but the worker itself paused. The dot
    // must follow the worker (amber Paused), NOT the session's isRunning
    // (which is what the pre-F5 code rendered: green Running).
    act(() => {
      useStore.setState({ sessionStates: { 'sess-A': { isRunning: true } } })
    })
    const { rerender } = renderPanel()
    await act(async () => {
      rerender(
        <WorkerOutputPanel
          {...panelProps({ incomingEvents: [pausedFor('w1', '2026-08-15T00:00:01.000Z')] })}
        />
      )
    })
    await waitFor(() => expect(dotLabel()).toBe('Paused'))
    expect(dotLabel()).not.toBe('Running')
    expect(dotEl()).not.toHaveClass('worker-status-dot-busy')
    expect(dotEl().style.background).toBe('rgb(249, 226, 175)')
    expect(dotEl().getAttribute('title')).toBe('Worker is paused')
  })

  it('shows a red Error dot when the worker errors', async () => {
    const { rerender } = renderPanel()
    await act(async () => {
      rerender(
        <WorkerOutputPanel
          {...panelProps({ incomingEvents: [errorFor('w1', '2026-08-15T00:00:01.000Z')] })}
        />
      )
    })
    await waitFor(() => expect(dotLabel()).toBe('Error'))
    expect(dotEl().style.background).toBe('rgb(243, 139, 168)')
    expect(dotEl().getAttribute('title')).toBe('Worker error')
  })

  it('returns to a grey Idle dot once the worker completes', async () => {
    const { rerender } = renderPanel()
    await act(async () => {
      rerender(
        <WorkerOutputPanel
          {...panelProps({ incomingEvents: [statusFor('w1', 'busy', 'Write report', '2026-08-15T00:00:01.000Z')] })}
        />
      )
    })
    await waitFor(() => expect(dotLabel()).toBe('Running'))
    await act(async () => {
      rerender(
        <WorkerOutputPanel
          {...panelProps({ incomingEvents: [completedFor('w1', '2026-08-15T00:00:02.000Z')] })}
        />
      )
    })
    await waitFor(() => expect(dotLabel()).toBe('Idle'))
    expect(dotEl()).not.toHaveClass('worker-status-dot-busy')
    expect(dotEl().style.background).toBe('rgb(88, 91, 112)')
  })
})

