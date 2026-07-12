import { describe, it, expect } from 'vitest';
import adaptWorkerEvent from '../chat/adaptWorkerEvent';

// ── Constants from the source (must match adaptWorkerEvent.js) ────────────
const SYSTEM_NOTIFICATION_EMOJI = '⚠️';
const WORKER_STARTED_TEXT = '⬤ Worker started';
const WORKER_COMPLETED_TEXT = '■ Worker completed';
const WORKER_STOPPED_TEXT = '⏹ Worker stopped';
const UNKNOWN_EVENT_TEXT = 'Unknown event: ';

// ── Helper: build an event with just the essentials ────────────────────────
function makeEvent(overrides = {}) {
  return {
    event: 'started',
    timestamp: '2026-07-09T12:00:00.000Z',
    request: {},
    response: {},
    ...overrides,
  };
}

function eventId(evt) {
  const parts = [];
  if (evt.session_id) parts.push(evt.session_id);
  if (evt.worker_name) parts.push(evt.worker_name);
  if (evt.timestamp) parts.push(evt.timestamp);
  parts.push(evt.event || '');
  return parts.join('_');
}

// ==========================================================================
// 1. Guard: null / undefined
// ==========================================================================
describe('guard clause', () => {
  it('returns null for null input', () => {
    expect(adaptWorkerEvent(null)).toBeNull();
  });

  it('returns null for undefined input', () => {
    expect(adaptWorkerEvent(undefined)).toBeNull();
  });

  it('returns null for empty object (no event field)', () => {
    expect(adaptWorkerEvent({})).toBeNull();
  });

  it('returns null for object with only timestamp', () => {
    expect(adaptWorkerEvent({ timestamp: '2026-01-01T00:00:00.000Z' })).toBeNull();
  });
});

// ==========================================================================
// 2. user_message
// ==========================================================================
describe('user_message', () => {
  it('returns msg with query from request', () => {
    const evt = makeEvent({ event: 'user_message', request: { query: 'Hello world' } });
    const result = adaptWorkerEvent(evt);
    expect(result).not.toBeNull();
    expect(result._id).toBe(eventId(evt));
    expect(result.role).toBe('user');
    expect(result.content).toBe('Hello world');
    expect(result.is_worker_query).toBe(true);
  });

  it('returns fallback content when query is empty', () => {
    const evt = makeEvent({ event: 'user_message', request: { query: '' } });
    const result = adaptWorkerEvent(evt);
    expect(result.content).toBe('(empty query)');
  });

  it('returns fallback content when request is missing', () => {
    const evt = makeEvent({ event: 'user_message' });
    delete evt.request;
    const result = adaptWorkerEvent(evt);
    expect(result.content).toBe('(empty query)');
  });

  it('returns fallback content when request.query is missing', () => {
    const evt = makeEvent({ event: 'user_message', request: {} });
    const result = adaptWorkerEvent(evt);
    expect(result.content).toBe('(empty query)');
  });
});

// ==========================================================================
// 3. query (legacy) — always suppressed
// ==========================================================================
describe('query', () => {
  it('returns null', () => {
    const evt = makeEvent({ event: 'query', request: { query: 'anything' } });
    expect(adaptWorkerEvent(evt)).toBeNull();
  });
});

// ==========================================================================
// 4. final_response
// ==========================================================================
describe('final_response', () => {
  it('returns assistant msg with content and reasoning', () => {
    const evt = makeEvent({
      event: 'final_response',
      response: {
        content: 'The answer is 42.',
        reasoning_content: 'Step 1: 1+1=2',
        response_type: 'answer',
      },
    });
    const result = adaptWorkerEvent(evt);
    expect(result._id).toBe(eventId(evt));
    expect(result.role).toBe('assistant');
    expect(result.content).toBe('The answer is 42.');
    expect(result.reasoning_content).toBe('Step 1: 1+1=2');
    expect(result.is_final).toBe(true);
    expect(result.response_type).toBe('answer');
  });

  it('handles missing response object', () => {
    const evt = makeEvent({ event: 'final_response' });
    delete evt.response;
    const result = adaptWorkerEvent(evt);
    expect(result.content).toBe('');
    expect(result.reasoning_content).toBeUndefined();
    expect(result.is_final).toBe(false);
    expect(result.response_type).toBe('answer');
  });

  it('handles missing reasoning_content', () => {
    const evt = makeEvent({
      event: 'final_response',
      response: { content: 'Hello', response_type: 'answer' },
    });
    const result = adaptWorkerEvent(evt);
    expect(result.content).toBe('Hello');
    expect(result.reasoning_content).toBeUndefined();
  });

  it('handles missing response_type', () => {
    const evt = makeEvent({
      event: 'final_response',
      response: { content: 'Hello' },
    });
    const result = adaptWorkerEvent(evt);
    expect(result.response_type).toBe('answer');
  });

  it('preserves custom response_type', () => {
    const evt = makeEvent({
      event: 'final_response',
      response: { content: 'Hello', response_type: 'error' },
    });
    const result = adaptWorkerEvent(evt);
    expect(result.response_type).toBe('error');
  });
});

// ==========================================================================
// 4b. worker_message
// ==========================================================================
describe('worker_message', () => {
  it('sets is_final: true when response_type is present (final answer)', () => {
    const evt = makeEvent({
      event: 'worker_message',
      response: { content: 'Final result', reasoning_content: 'thinking...', response_type: 'answer' },
    });
    const result = adaptWorkerEvent(evt);
    expect(result.role).toBe('assistant');
    expect(result.content).toBe('Final result');
    expect(result.reasoning_content).toBe('thinking...');
    expect(result.is_final).toBe(true);
    expect(result.response_type).toBe('answer');
  });

  it('sets is_final: false when response_type is missing (intermediate message)', () => {
    const evt = makeEvent({
      event: 'worker_message',
      response: { content: 'Intermediate update' },
    });
    const result = adaptWorkerEvent(evt);
    expect(result.role).toBe('assistant');
    expect(result.content).toBe('Intermediate update');
    expect(result.reasoning_content).toBeUndefined();
    expect(result.is_final).toBe(false);
    expect(result.response_type).toBe('answer');
  });

  it('sets is_final: false when response is missing', () => {
    const evt = makeEvent({ event: 'worker_message' });
    delete evt.response;
    const result = adaptWorkerEvent(evt);
    expect(result.content).toBe('');
    expect(result.is_final).toBe(false);
  });
});

// ==========================================================================
// 4c. assistant_message
// ==========================================================================
describe('assistant_message', () => {
  it('sets is_final: true when response_type is present (final answer)', () => {
    const evt = makeEvent({
      event: 'assistant_message',
      response: { content: 'Final answer', reasoning_content: 'Step by step...', response_type: 'answer' },
    });
    const result = adaptWorkerEvent(evt);
    expect(result.role).toBe('assistant');
    expect(result.content).toBe('Final answer');
    expect(result.reasoning_content).toBe('Step by step...');
    expect(result.is_final).toBe(true);
    expect(result.response_type).toBe('answer');
  });

  it('sets is_final: false when response_type is missing (intermediate message)', () => {
    const evt = makeEvent({
      event: 'assistant_message',
      response: { content: 'Working on it...' },
    });
    const result = adaptWorkerEvent(evt);
    expect(result.role).toBe('assistant');
    expect(result.content).toBe('Working on it...');
    expect(result.reasoning_content).toBeUndefined();
    expect(result.is_final).toBe(false);
    expect(result.response_type).toBe('answer');
  });

  it('sets is_final: false when response is missing', () => {
    const evt = makeEvent({ event: 'assistant_message' });
    delete evt.response;
    const result = adaptWorkerEvent(evt);
    expect(result.content).toBe('');
    expect(result.is_final).toBe(false);
  });
});

// ==========================================================================
// 5. tool_call
// ==========================================================================
describe('tool_call', () => {
  it('returns tool_call msg with JSON content', () => {
    const evt = makeEvent({
      event: 'tool_call',
      request: { tool: 'read_file', args: { path: '/tmp/test.txt' } },
    });
    const result = adaptWorkerEvent(evt);
    expect(result._id).toBe(eventId(evt));
    expect(result.role).toBe('tool_call');
    expect(JSON.parse(result.content)).toEqual({
      name: 'read_file',
      arguments: { path: '/tmp/test.txt' },
    });
  });

  it('defaults tool name to unknown when missing', () => {
    const evt = makeEvent({
      event: 'tool_call',
      request: { args: { x: 1 } },
    });
    const result = adaptWorkerEvent(evt);
    expect(JSON.parse(result.content).name).toBe('unknown');
  });

  it('defaults args to empty object when missing', () => {
    const evt = makeEvent({
      event: 'tool_call',
      request: { tool: 'find' },
    });
    const result = adaptWorkerEvent(evt);
    expect(JSON.parse(result.content).arguments).toEqual({});
  });

  it('handles missing request object', () => {
    const evt = makeEvent({ event: 'tool_call' });
    delete evt.request;
    const result = adaptWorkerEvent(evt);
    expect(JSON.parse(result.content).name).toBe('unknown');
    expect(JSON.parse(result.content).arguments).toEqual({});
  });
});

// ==========================================================================
// 6. tool_result
// ==========================================================================
describe('tool_result', () => {
  it('returns success result', () => {
    const evt = makeEvent({
      event: 'tool_result',
      request: { tool: 'glob', success: true },
      response: { result: 'Found 3 files' },
    });
    const result = adaptWorkerEvent(evt);
    expect(result._id).toBe(eventId(evt));
    expect(result.role).toBe('tool_result');
    expect(result.content).toBe('Found 3 files');
    expect(result.is_summary).toBe(false);
  });

  it('returns error message on failure', () => {
    const evt = makeEvent({
      event: 'tool_result',
      request: { tool: 'glob', success: false, error: 'Permission denied' },
      response: { result: '' },
    });
    const result = adaptWorkerEvent(evt);
    expect(result.content).toBe('Permission denied');
  });

  it('falls back to response.result on failure when request.error is missing', () => {
    const evt = makeEvent({
      event: 'tool_result',
      request: { tool: 'glob', success: false },
      response: { result: 'Something broke' },
    });
    const result = adaptWorkerEvent(evt);
    expect(result.content).toBe('Something broke');
  });

  it('uses fallback string on failure when both error and result are missing', () => {
    const evt = makeEvent({
      event: 'tool_result',
      request: { tool: 'glob', success: false },
      response: {},
    });
    const result = adaptWorkerEvent(evt);
    expect(result.content).toBe('Unknown error');
  });

  it('sets is_summary for SummarizeTool', () => {
    const evt = makeEvent({
      event: 'tool_result',
      request: { tool: 'SummarizeTool', success: true },
      response: { result: 'Summary made' },
    });
    const result = adaptWorkerEvent(evt);
    expect(result.is_summary).toBe(true);
    expect(result.content).toBe('Summary made');
  });

  it('defaults success to true when missing', () => {
    const evt = makeEvent({
      event: 'tool_result',
      request: { tool: 'find' },
      response: { result: 'data' },
    });
    const result = adaptWorkerEvent(evt);
    expect(result.content).toBe('data');
  });

  it('wraps content in String() for non-string results', () => {
    const evt = makeEvent({
      event: 'tool_result',
      request: { tool: 'calc', success: true },
      response: { result: 42 },
    });
    const result = adaptWorkerEvent(evt);
    expect(result.content).toBe('42');
  });

  it('handles missing request object', () => {
    const evt = makeEvent({ event: 'tool_result' });
    delete evt.request;
    const result = adaptWorkerEvent(evt);
    expect(result.content).toBe('(empty)');
  });

  it('handles missing response object', () => {
    const evt = makeEvent({
      event: 'tool_result',
      request: { tool: 'find', success: true },
    });
    delete evt.response;
    const result = adaptWorkerEvent(evt);
    expect(result.content).toBe('(empty)');
  });
});

// ==========================================================================
// 7. system_notification
// ==========================================================================
describe('system_notification', () => {
  // ── token_warning ──
  describe('token_warning type', () => {
    it('returns msg with token count', () => {
      const evt = makeEvent({
        event: 'system_notification',
        response: { type: 'token_warning', message: 'Approaching limit', token_count: 120000 },
      });
      const result = adaptWorkerEvent(evt);
      expect(result.role).toBe('user');
      expect(result.content).toBe(`${SYSTEM_NOTIFICATION_EMOJI} Approaching limit (Tokens: 120000)`);
      expect(result.is_system_notification).toBe(true);
    });

    it('handles missing token_count', () => {
      const evt = makeEvent({
        event: 'system_notification',
        response: { type: 'token_warning', message: 'Warning' },
      });
      const result = adaptWorkerEvent(evt);
      expect(result.content).toBe(`${SYSTEM_NOTIFICATION_EMOJI} Warning`);
    });

    it('handles null token_count', () => {
      const evt = makeEvent({
        event: 'system_notification',
        response: { type: 'token_warning', message: 'Warning', token_count: null },
      });
      const result = adaptWorkerEvent(evt);
      expect(result.content).toBe(`${SYSTEM_NOTIFICATION_EMOJI} Warning`);
    });

    it('handles empty message', () => {
      const evt = makeEvent({
        event: 'system_notification',
        response: { type: 'token_warning', message: '' },
      });
      const result = adaptWorkerEvent(evt);
      expect(result.content).toBe(`${SYSTEM_NOTIFICATION_EMOJI}`);
    });
  });

  // ── turn_warning ──
  describe('turn_warning type', () => {
    it('returns msg with turn count', () => {
      const evt = makeEvent({
        event: 'system_notification',
        response: { type: 'turn_warning', message: 'Turn limit near', turn_count: 18 },
      });
      const result = adaptWorkerEvent(evt);
      expect(result.role).toBe('user');
      expect(result.content).toBe(`${SYSTEM_NOTIFICATION_EMOJI} Turn limit near (Turns: 18)`);
      expect(result.is_system_notification).toBe(true);
    });

    it('handles missing turn_count', () => {
      const evt = makeEvent({
        event: 'system_notification',
        response: { type: 'turn_warning', message: 'Warning' },
      });
      const result = adaptWorkerEvent(evt);
      expect(result.content).toBe(`${SYSTEM_NOTIFICATION_EMOJI} Warning`);
    });
  });

  // ── time_warning ──
  describe('time_warning type', () => {
    it('returns msg with elapsed seconds', () => {
      const evt = makeEvent({
        event: 'system_notification',
        response: { type: 'time_warning', message: 'Time running out', elapsed_seconds: 55 },
      });
      const result = adaptWorkerEvent(evt);
      expect(result.role).toBe('user');
      expect(result.content).toBe(`${SYSTEM_NOTIFICATION_EMOJI} Time running out (Elapsed: 55s)`);
      expect(result.is_system_notification).toBe(true);
    });

    it('handles missing elapsed_seconds', () => {
      const evt = makeEvent({
        event: 'system_notification',
        response: { type: 'time_warning', message: 'Warning' },
      });
      const result = adaptWorkerEvent(evt);
      expect(result.content).toBe(`${SYSTEM_NOTIFICATION_EMOJI} Warning`);
    });
  });

  // ── context_summarized ──
  describe('context_summarized type', () => {
    it('returns msg with token count', () => {
      const evt = makeEvent({
        event: 'system_notification',
        response: { type: 'context_summarized', message: 'Context compressed', context_length: 5000 },
      });
      const result = adaptWorkerEvent(evt);
      expect(result.role).toBe('user');
      expect(result.content).toBe(`${SYSTEM_NOTIFICATION_EMOJI} Context compressed (Tokens: 5000)`);
      expect(result.is_system_notification).toBe(true);
    });

    it('handles null context_length', () => {
      const evt = makeEvent({
        event: 'system_notification',
        response: { type: 'context_summarized', message: 'Done', context_length: null },
      });
      const result = adaptWorkerEvent(evt);
      expect(result.content).toBe(`${SYSTEM_NOTIFICATION_EMOJI} Done`);
    });

    it('handles missing context_length', () => {
      const evt = makeEvent({
        event: 'system_notification',
        response: { type: 'context_summarized', message: 'Done' },
      });
      const result = adaptWorkerEvent(evt);
      expect(result.content).toBe(`${SYSTEM_NOTIFICATION_EMOJI} Done`);
    });
  });

  // ── unknown / fallback ──
  describe('unknown notification type', () => {
    it('returns fallback msg for unrecognized type', () => {
      const evt = makeEvent({
        event: 'system_notification',
        response: { type: 'custom_alert', message: 'Something happened' },
      });
      const result = adaptWorkerEvent(evt);
      expect(result.role).toBe('user');
      expect(result.content).toBe(`${SYSTEM_NOTIFICATION_EMOJI} Something happened`);
      expect(result.is_system_notification).toBe(true);
    });

    it('returns fallback msg when message is missing', () => {
      const evt = makeEvent({
        event: 'system_notification',
        response: { type: 'custom_alert' },
      });
      const result = adaptWorkerEvent(evt);
      expect(result.content).toBe(`${SYSTEM_NOTIFICATION_EMOJI} System notification`);
    });

    it('handles missing response object', () => {
      const evt = makeEvent({ event: 'system_notification' });
      delete evt.response;
      const result = adaptWorkerEvent(evt);
      expect(result.content).toBe(`${SYSTEM_NOTIFICATION_EMOJI} System notification`);
    });
  });
});

// ==========================================================================
// 8. started
// ==========================================================================
describe('started', () => {
  it('returns system notification', () => {
    const evt = makeEvent({ event: 'started' });
    const result = adaptWorkerEvent(evt);
    expect(result._id).toBe(eventId(evt));
    expect(result.role).toBe('system');
    expect(result.content).toBe(WORKER_STARTED_TEXT);
    expect(result.is_system_notification).toBe(true);
  });
});

// ==========================================================================
// 9. completed
// ==========================================================================
describe('completed', () => {
  it('returns system notification', () => {
    const evt = makeEvent({ event: 'completed' });
    const result = adaptWorkerEvent(evt);
    expect(result._id).toBe(eventId(evt));
    expect(result.role).toBe('system');
    expect(result.content).toBe(WORKER_COMPLETED_TEXT);
    expect(result.is_system_notification).toBe(true);
  });
});

// ==========================================================================
// 10. stopped
// ==========================================================================
describe('stopped', () => {
  it('returns system notification', () => {
    const evt = makeEvent({ event: 'stopped' });
    const result = adaptWorkerEvent(evt);
    expect(result._id).toBe(eventId(evt));
    expect(result.role).toBe('system');
    expect(result.content).toBe(WORKER_STOPPED_TEXT);
    expect(result.is_system_notification).toBe(true);
  });
});

// ==========================================================================
// 11. error
// ==========================================================================
describe('error', () => {
  it('uses response.error when available', () => {
    const evt = makeEvent({
      event: 'error',
      response: { error: 'Server error' },
      request: { error: 'Request error' },
    });
    const result = adaptWorkerEvent(evt);
    expect(result.role).toBe('user');
    expect(result.content).toBe('❌ Server error');
    expect(result.is_system_notification).toBe(true);
  });

  it('falls back to request.error when response.error missing', () => {
    const evt = makeEvent({
      event: 'error',
      response: {},
      request: { error: 'Request error' },
    });
    const result = adaptWorkerEvent(evt);
    expect(result.content).toBe('❌ Request error');
  });

  it('uses default message when both errors missing', () => {
    const evt = makeEvent({ event: 'error' });
    const result = adaptWorkerEvent(evt);
    expect(result.content).toBe('❌ Unknown error');
  });

  it('handles missing request and response', () => {
    const evt = makeEvent({ event: 'error' });
    delete evt.request;
    delete evt.response;
    const result = adaptWorkerEvent(evt);
    expect(result.content).toBe('❌ Unknown error');
  });
});

// ==========================================================================
// 12. worker_spawned
// ==========================================================================
describe('worker_spawned', () => {
  it('uses worker_name from response', () => {
    const evt = makeEvent({
      event: 'worker_spawned',
      response: { worker_name: 'my-coder' },
    });
    const result = adaptWorkerEvent(evt);
    expect(result._id).toBe(eventId(evt));
    expect(result.role).toBe('system');
    expect(result.content).toBe('🟢 Worker spawned: my-coder');
    expect(result.is_system_notification).toBe(true);
    expect(result.is_worker_event).toBe(true);
  });

  it('falls back to worker_name from request', () => {
    const evt = makeEvent({
      event: 'worker_spawned',
      request: { worker_name: 'fallback-worker' },
    });
    const result = adaptWorkerEvent(evt);
    expect(result.content).toBe('🟢 Worker spawned: fallback-worker');
  });

  it('defaults to "default" when worker_name is missing', () => {
    const evt = makeEvent({ event: 'worker_spawned' });
    const result = adaptWorkerEvent(evt);
    expect(result.content).toBe('🟢 Worker spawned: default');
  });

  it('handles missing request and response', () => {
    const evt = makeEvent({ event: 'worker_spawned' });
    delete evt.request;
    delete evt.response;
    const result = adaptWorkerEvent(evt);
    expect(result.content).toBe('🟢 Worker spawned: default');
  });
});

// ==========================================================================
// 13. worker_status
// ==========================================================================
describe('worker_status', () => {
  it('includes message when present', () => {
    const evt = makeEvent({
      event: 'worker_status',
      response: { worker_name: 'my-coder', status: 'busy', message: 'Processing' },
    });
    const result = adaptWorkerEvent(evt);
    expect(result._id).toBe(eventId(evt));
    expect(result.role).toBe('system');
    expect(result.content).toBe('⏳ Worker status: busy — Processing');
    expect(result.is_system_notification).toBe(true);
    expect(result.is_worker_event).toBe(true);
  });

  it('omits message dash when message is empty', () => {
    const evt = makeEvent({
      event: 'worker_status',
      response: { worker_name: 'my-coder', status: 'idle', message: '' },
    });
    const result = adaptWorkerEvent(evt);
    expect(result.content).toBe('⏳ Worker status: idle');
  });

  it('defaults status to running', () => {
    const evt = makeEvent({ event: 'worker_status' });
    const result = adaptWorkerEvent(evt);
    expect(result.content).toBe('⏳ Worker status: running');
  });

  it('reads status from request if response missing', () => {
    const evt = makeEvent({
      event: 'worker_status',
      request: { status: 'error' },
    });
    const result = adaptWorkerEvent(evt);
    expect(result.content).toBe('⏳ Worker status: error');
  });
});

// ==========================================================================
// 14. worker_completed
// ==========================================================================
describe('worker_completed', () => {
  it('uses worker_name from response', () => {
    const evt = makeEvent({
      event: 'worker_completed',
      response: { worker_name: 'my-coder' },
    });
    const result = adaptWorkerEvent(evt);
    expect(result._id).toBe(eventId(evt));
    expect(result.role).toBe('system');
    expect(result.content).toBe('✅ Worker completed: my-coder');
    expect(result.is_system_notification).toBe(true);
    expect(result.is_worker_event).toBe(true);
  });

  it('falls back to request', () => {
    const evt = makeEvent({
      event: 'worker_completed',
      request: { worker_name: 'fallback' },
    });
    const result = adaptWorkerEvent(evt);
    expect(result.content).toBe('✅ Worker completed: fallback');
  });

  it('defaults to "default"', () => {
    const evt = makeEvent({ event: 'worker_completed' });
    const result = adaptWorkerEvent(evt);
    expect(result.content).toBe('✅ Worker completed: default');
  });
});

// ==========================================================================
// 15. worker_error
// ==========================================================================
describe('worker_error', () => {
  it('uses worker_name and error from response', () => {
    const evt = makeEvent({
      event: 'worker_error',
      response: { worker_name: 'my-coder', error: 'Timeout' },
    });
    const result = adaptWorkerEvent(evt);
    expect(result._id).toBe(eventId(evt));
    expect(result.role).toBe('user');
    expect(result.content).toBe('🔴 Worker error (my-coder): Timeout');
    expect(result.is_system_notification).toBe(true);
    expect(result.is_worker_event).toBe(true);
  });

  it('falls back to request fields', () => {
    const evt = makeEvent({
      event: 'worker_error',
      request: { worker_name: 'fallback', error: 'Crash' },
    });
    const result = adaptWorkerEvent(evt);
    expect(result.content).toBe('🔴 Worker error (fallback): Crash');
  });

  it('uses default worker name and error message', () => {
    const evt = makeEvent({ event: 'worker_error' });
    const result = adaptWorkerEvent(evt);
    expect(result.content).toBe('🔴 Worker error (default): Unknown worker error');
  });
});

// ==========================================================================
// 16. default (unknown event)
// ==========================================================================
describe('unknown event type', () => {
  it('returns fallback msg for unrecognized event', () => {
    const evt = makeEvent({ event: 'some_new_event' });
    const result = adaptWorkerEvent(evt);
    expect(result._id).toBe(eventId(evt));
    expect(result.role).toBe('system');
    expect(result.content).toBe(`${UNKNOWN_EVENT_TEXT}some_new_event`);
    expect(result.is_system_notification).toBe(true);
  });
});

// ==========================================================================
// Edge cases: _id generation
// ==========================================================================
describe('_id generation', () => {
  it('concatenates timestamp and event name with underscore separator', () => {
    const evt = makeEvent({ event: 'started' });
    const result = adaptWorkerEvent(evt);
    expect(result._id).toBe('2026-07-09T12:00:00.000Z_started');
  });

  it('handles missing timestamp', () => {
    const evt = makeEvent({ event: 'started' });
    delete evt.timestamp;
    const result = adaptWorkerEvent(evt);
    expect(result._id).toBe('started');
  });

  it('handles empty string timestamp', () => {
    const evt = makeEvent({ event: 'started', timestamp: '' });
    const result = adaptWorkerEvent(evt);
    expect(result._id).toBe('started');
  });

  it('includes session_id and worker_name when present', () => {
    const evt = makeEvent({
      event: 'worker_message',
      session_id: 'sess_abc123',
      worker_name: 'my-worker',
      response: { content: 'Hello' },
    });
    const result = adaptWorkerEvent(evt);
    expect(result._id).toBe('sess_abc123_my-worker_2026-07-09T12:00:00.000Z_worker_message');
  });

  it('includes only session_id when worker_name is absent', () => {
    const evt = makeEvent({
      event: 'tool_call',
      session_id: 'sess_xyz',
      request: { tool: 'read' },
    });
    const result = adaptWorkerEvent(evt);
    expect(result._id).toBe('sess_xyz_2026-07-09T12:00:00.000Z_tool_call');
  });
});
