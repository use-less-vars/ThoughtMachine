/*
 * adaptWorkerEvent.js
 *
 * Pure function that transforms a worker event object (from events.jsonl)
 * into a msg object suitable for MessageBubble (or any of the shared
 * message components: MessageContent, CopyButton, etc.).
 *
 * Exported as default: adaptWorkerEvent(evt)
 *
 * Returns a msg object with at minimum { _id, role, content } plus
 * optional flags (is_final, is_system_notification, reasoning_content,
 * is_summary, response_type) — or null if the event should be suppressed
 * (user_message / query are already shown in the main ChatPanel).
 */

const SYSTEM_NOTIFICATION_EMOJI = '⚠️'
const WORKER_STARTED_TEXT = '⬤ Worker started'
const WORKER_COMPLETED_TEXT = '■ Worker completed'
const WORKER_STOPPED_TEXT = '⏹ Worker stopped'
const UNKNOWN_EVENT_TEXT = 'Unknown event: '

/**
 * Build a unique ID for deduplication, matching the pattern used in
 * WorkerOutputPanel's fetchEvents (timestamp + event).
 */
function eventId(evt) {
  return (evt.timestamp || '') + (evt.event || '')
}

/**
 * Adapt a token-warning system_notification into a msg object.
 */
function tokenWarningMsg(evt) {
  const resp = evt.response || {}
  const message = resp.message || ''
  const tokenCount = resp.token_count
  let content = `${SYSTEM_NOTIFICATION_EMOJI} ${message}`
  if (tokenCount !== undefined) {
    content += ` (Tokens: ${tokenCount})`
  }
  return {
    _id: eventId(evt),
    role: 'user',
    content: content.trim(),
    is_system_notification: true,
  }
}

/**
 * Adapt a turn-warning system_notification into a msg object.
 */
function turnWarningMsg(evt) {
  const resp = evt.response || {}
  const message = resp.message || ''
  const turnCount = resp.turn_count
  let content = `${SYSTEM_NOTIFICATION_EMOJI} ${message}`
  if (turnCount !== undefined) {
    content += ` (Turns: ${turnCount})`
  }
  return {
    _id: eventId(evt),
    role: 'user',
    content: content.trim(),
    is_system_notification: true,
  }
}

/**
 * Adapt a time-warning system_notification into a msg object.
 */
function timeWarningMsg(evt) {
  const resp = evt.response || {}
  const message = resp.message || ''
  const elapsed = resp.elapsed_seconds
  let content = `${SYSTEM_NOTIFICATION_EMOJI} ${message}`
  if (elapsed !== undefined) {
    content += ` (Elapsed: ${elapsed}s)`
  }
  return {
    _id: eventId(evt),
    role: 'user',
    content: content.trim(),
    is_system_notification: true,
  }
}

/**
 * Main adapter function.
 *
 * @param {object} evt — A worker event object from events.jsonl
 * @returns {object|null} — A msg object for MessageBubble, or null to suppress
 */
export default function adaptWorkerEvent(evt) {
  // Guard against null/undefined
  if (!evt || !evt.event) return null

  switch (evt.event) {
    // ── Query (user's request to the worker) ─────────────────────
    case 'user_message': {
      const req = evt.request || {}
      return {
        _id: eventId(evt),
        role: 'user',
        content: req.query || '(empty query)',
        is_worker_query: true,
      }
    }
    // Legacy query event (same format)
    case 'query':
      return null

    // ── Final response (assistant answer) ──────────────────────────
    case 'final_response': {
      const resp = evt.response || {}
      return {
        _id: eventId(evt),
        role: 'assistant',
        content: resp.content || '',
        reasoning_content: resp.reasoning_content || undefined,
        is_final: true,
        response_type: resp.response_type || 'answer',
      }
    }

    // ── Tool call ─────────────────────────────────────────────────
    case 'tool_call': {
      const req = evt.request || {}
      const jsonContent = JSON.stringify({
        name: req.tool || 'unknown',
        arguments: req.args || {},
      })
      return {
        _id: eventId(evt),
        role: 'tool_call',
        content: jsonContent,
      }
    }

    // ── Tool result ───────────────────────────────────────────────
    case 'tool_result': {
      const req = evt.request || {}
      const resp = evt.response || {}
      const success = req.success !== false
      const content = success
        ? (resp.result || '(empty)')
        : (req.error || resp.result || 'Unknown error')
      const isSummary = (req.tool === 'SummarizeTool')
      return {
        _id: eventId(evt),
        role: 'tool_result',
        content: String(content),
        is_summary: isSummary,
      }
    }

    // ── System notifications (warnings) ──────────────────────────
    case 'system_notification': {
      const resp = evt.response || {}
      const type = resp.type || ''
      if (type === 'token_warning') return tokenWarningMsg(evt)
      if (type === 'turn_warning') return turnWarningMsg(evt)
      if (type === 'time_warning') return timeWarningMsg(evt)
      if (type === 'context_summarized') {
        const ctxLen = resp.context_length
        let content = `${SYSTEM_NOTIFICATION_EMOJI} ${resp.message || 'Context summarized'}`
        if (ctxLen !== undefined && ctxLen !== null) {
          content += ` (Tokens: ${ctxLen})`
        }
        return {
          _id: eventId(evt),
          role: 'user',
          content: content.trim(),
          is_system_notification: true,
        }
      }
      // Fallback for system_notification without a recognized type
      return {
        _id: eventId(evt),
        role: 'user',
        content: `${SYSTEM_NOTIFICATION_EMOJI} ${resp.message || 'System notification'}`,
        is_system_notification: true,
      }
    }

    // ── Lifecycle events ──────────────────────────────────────────
    case 'started':
      return {
        _id: eventId(evt),
        role: 'system',
        content: WORKER_STARTED_TEXT,
        is_system_notification: true,
      }

    case 'completed':
      return {
        _id: eventId(evt),
        role: 'system',
        content: WORKER_COMPLETED_TEXT,
        is_system_notification: true,
      }

    case 'stopped':
      return {
        _id: eventId(evt),
        role: 'system',
        content: WORKER_STOPPED_TEXT,
        is_system_notification: true,
      }

    // ── Error ─────────────────────────────────────────────────────
    case 'error': {
      const resp = evt.response || {}
      const req = evt.request || {}
      const message = resp.error || req.error || 'Unknown error'
      return {
        _id: eventId(evt),
        role: 'user',
        content: `❌ ${message}`,
        is_system_notification: true,
      }
    }

    // ── Fallback: unknown event type ──────────────────────────────
    default:
      return {
        _id: eventId(evt),
        role: 'system',
        content: `${UNKNOWN_EVENT_TEXT}${evt.event}`,
        is_system_notification: true,
      }
  }
}


/* ===================================================================
 * Example mappings (for reference / testing):
 *
 * const event1 = {
 *   event: 'tool_call',
 *   timestamp: '2026-07-01T12:00:00.000Z',
 *   request: { tool: 'read_file', args: { path: '/tmp/test.txt' } },
 *   response: {},
 * };
 * // adaptWorkerEvent(event1)
 * // → {
 * //     _id: '2026-07-01T12:00:00.000Ztool_call',
 * //     role: 'tool_call',
 * //     content: '{"name":"read_file","arguments":{"path":"/tmp/test.txt"}}',
 * //   }
 *
 * const event2 = {
 *   event: 'final_response',
 *   timestamp: '2026-07-01T12:01:00.000Z',
 *   response: {
 *     content: 'The answer is 42.',
 *     reasoning_content: 'Let me think about this...\n\n1 + 1 = 2...\nSo the answer is 42.',
 *     response_type: 'answer',
 *   },
 * };
 * // adaptWorkerEvent(event2)
 * // → {
 * //     _id: '2026-07-01T12:01:00.000Zfinal_response',
 * //     role: 'assistant',
 * //     content: 'The answer is 42.',
 * //     reasoning_content: 'Let me think about this...\n\n1 + 1 = 2...\nSo the answer is 42.',
 * //     is_final: true,
 * //     response_type: 'answer',
 * //   }
 *
 * const event3 = {
 *   event: 'system_notification',
 *   timestamp: '2026-07-01T12:02:00.000Z',
 *   response: {
 *     type: 'token_warning',
 *     message: 'Approaching context limit',
 *     token_count: 120000,
 *   },
 * };
 * // adaptWorkerEvent(event3)
 * // → {
 * //     _id: '2026-07-01T12:02:00.000Zsystem_notification',
 * //     role: 'user',
 * //     content: '⚠️ Approaching context limit (Tokens: 120000)',
 * //     is_system_notification: true,
 * //   }
 *
 * const event4 = {
 *   event: 'tool_result',
 *   timestamp: '2026-07-01T12:03:00.000Z',
 *   request: { tool: 'glob', success: true },
 *   response: { result: 'Found 3 files: a.txt, b.txt, c.txt' },
 * };
 * // adaptWorkerEvent(event4)
 * // → {
 * //     _id: '2026-07-01T12:03:00.000Ztool_result',
 * //     role: 'tool_result',
 * //     content: 'Found 3 files: a.txt, b.txt, c.txt',
 * //   }
 *
 * const event5 = { event: 'user_message', timestamp: '...' };
 * // adaptWorkerEvent(event5) → null  (suppressed)
 * =================================================================== */
