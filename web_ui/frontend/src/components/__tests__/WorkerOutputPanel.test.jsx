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
