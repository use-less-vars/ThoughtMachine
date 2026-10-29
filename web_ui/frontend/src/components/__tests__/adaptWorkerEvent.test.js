import { describe, it, expect } from 'vitest';
import adaptWorkerEvent, { isWorkerEventRenderable, toolCallEventId, mergeToolMessages } from '../chat/adaptWorkerEvent';
import { makeDedupKey, normaliseToolEvent } from '../WorkerOutputPanel';

// ── Constants from the source (must match adaptWorkerEvent.js) ────────────
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

  it('unwraps JSON-wrapped query from delegation system', () => {
    const evt = makeEvent({
      event: 'user_message',
      request: { query: '{"query":"Verify the current state of all changes"}' },
    });
    const result = adaptWorkerEvent(evt);
    expect(result.content).toBe('Verify the current state of all changes');
    expect(result.is_worker_query).toBe(true);
  });

  it('unwraps when JSON has extra whitespace', () => {
    const evt = makeEvent({
      event: 'user_message',
      request: { query: '  { "query": "Run analysis on data" }  ' },
    });
    const result = adaptWorkerEvent(evt);
    expect(result.content).toBe('Run analysis on data');
  });

  it('keeps raw query when JSON has no query key', () => {
    const evt = makeEvent({
      event: 'user_message',
      request: { query: '{"other_key":"hello"}' },
    });
    const result = adaptWorkerEvent(evt);
    // Valid JSON but no 'query' string key — keep as-is
    expect(result.content).toBe('{"other_key":"hello"}');
  });

  it('keeps raw query when query is plain text that starts with {', () => {
    const evt = makeEvent({
      event: 'user_message',
      request: { query: '{just some text that looks like JSON but isnt}' },
    });
    const result = adaptWorkerEvent(evt);
    expect(result.content).toBe('{just some text that looks like JSON but isnt}');
  });

  it('keeps raw query when parsed query field is not a string', () => {
    const evt = makeEvent({
      event: 'user_message',
      request: { query: '{"query":123}' },
    });
    const result = adaptWorkerEvent(evt);
    // 'query' key exists but value is not a string — keep raw
    expect(result.content).toBe('{"query":123}');
  });

  it('keeps raw query when parsed query value is null', () => {
    const evt = makeEvent({
      event: 'user_message',
      request: { query: '{"query":null}' },
    });
    const result = adaptWorkerEvent(evt);
    expect(result.content).toBe('{"query":null}');
  });

  it('keeps plain text queries unchanged', () => {
    const evt = makeEvent({
      event: 'user_message',
      request: { query: 'What files are in the project?' },
    });
    const result = adaptWorkerEvent(evt);
    expect(result.content).toBe('What files are in the project?');
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
// 5b. tool_call_start (pending tool call — mirrors tool_call)
// ==========================================================================
describe('tool_call_start', () => {
  it('returns tool_call msg with JSON content', () => {
    const evt = makeEvent({
      event: 'tool_call_start',
      request: { tool: 'read_file', args: { path: '/tmp/test.txt' }, status: 'running' },
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
      event: 'tool_call_start',
      request: { args: { x: 1 }, status: 'running' },
    });
    const result = adaptWorkerEvent(evt);
    expect(JSON.parse(result.content).name).toBe('unknown');
  });

  it('defaults args to empty object when missing', () => {
    const evt = makeEvent({
      event: 'tool_call_start',
      request: { tool: 'find', status: 'running' },
    });
    const result = adaptWorkerEvent(evt);
    expect(JSON.parse(result.content).arguments).toEqual({});
  });

  it('handles missing request object', () => {
    const evt = makeEvent({ event: 'tool_call_start' });
    delete evt.request;
    const result = adaptWorkerEvent(evt);
    expect(JSON.parse(result.content).name).toBe('unknown');
    expect(JSON.parse(result.content).arguments).toEqual({});
  });

  it('produces a distinct _id from tool_call', () => {
    const start = makeEvent({
      event: 'tool_call_start',
      request: { tool: 'read_file', args: {} },
    });
    const call = makeEvent({
      event: 'tool_call',
      request: { tool: 'read_file', args: {} },
    });
    expect(adaptWorkerEvent(start)._id).not.toBe(adaptWorkerEvent(call)._id);
    expect(adaptWorkerEvent(start)._id.endsWith('tool_call_start')).toBe(true);
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
      expect(result.content).toBe(`Approaching limit (Tokens: 120000)`);
      expect(result.is_system_notification).toBe(true);
    });

    it('handles missing token_count', () => {
      const evt = makeEvent({
        event: 'system_notification',
        response: { type: 'token_warning', message: 'Warning' },
      });
      const result = adaptWorkerEvent(evt);
      expect(result.content).toBe(`Warning`);
    });

    it('handles null token_count', () => {
      const evt = makeEvent({
        event: 'system_notification',
        response: { type: 'token_warning', message: 'Warning', token_count: null },
      });
      const result = adaptWorkerEvent(evt);
      expect(result.content).toBe(`Warning`);
    });

    it('handles empty message', () => {
      const evt = makeEvent({
        event: 'system_notification',
        response: { type: 'token_warning', message: '' },
      });
      const result = adaptWorkerEvent(evt);
      expect(result.content).toBe('');
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
      expect(result.content).toBe(`Turn limit near (Turns: 18)`);
      expect(result.is_system_notification).toBe(true);
    });

    it('handles missing turn_count', () => {
      const evt = makeEvent({
        event: 'system_notification',
        response: { type: 'turn_warning', message: 'Warning' },
      });
      const result = adaptWorkerEvent(evt);
      expect(result.content).toBe(`Warning`);
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
      expect(result.content).toBe(`Time running out (Elapsed: 55s)`);
      expect(result.is_system_notification).toBe(true);
    });

    it('handles missing elapsed_seconds', () => {
      const evt = makeEvent({
        event: 'system_notification',
        response: { type: 'time_warning', message: 'Warning' },
      });
      const result = adaptWorkerEvent(evt);
      expect(result.content).toBe(`Warning`);
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
      expect(result.content).toBe(`Context compressed (Tokens: 5000)`);
      expect(result.is_system_notification).toBe(true);
    });

    it('handles null context_length', () => {
      const evt = makeEvent({
        event: 'system_notification',
        response: { type: 'context_summarized', message: 'Done', context_length: null },
      });
      const result = adaptWorkerEvent(evt);
      expect(result.content).toBe(`Done`);
    });

    it('handles missing context_length', () => {
      const evt = makeEvent({
        event: 'system_notification',
        response: { type: 'context_summarized', message: 'Done' },
      });
      const result = adaptWorkerEvent(evt);
      expect(result.content).toBe(`Done`);
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
      expect(result.content).toBe(`Something happened`);
      expect(result.is_system_notification).toBe(true);
    });

    it('returns fallback msg when message is missing', () => {
      const evt = makeEvent({
        event: 'system_notification',
        response: { type: 'custom_alert' },
      });
      const result = adaptWorkerEvent(evt);
      expect(result.content).toBe(`System notification`);
    });

    it('handles missing response object', () => {
      const evt = makeEvent({ event: 'system_notification' });
      delete evt.response;
      const result = adaptWorkerEvent(evt);
      expect(result.content).toBe(`System notification`);
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
// 17. default (unknown event)
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

// ==========================================================================
// isWorkerEventRenderable (Fix A / Fix B helper — added with panel fixes)
// ==========================================================================
describe('isWorkerEventRenderable', () => {
  it('is false for null/undefined/empty input', () => {
    expect(isWorkerEventRenderable(null)).toBe(false);
    expect(isWorkerEventRenderable(undefined)).toBe(false);
    expect(isWorkerEventRenderable({})).toBe(false);
  });

  it('is false for header-only internal events (context_updated / tokens_updated)', () => {
    expect(isWorkerEventRenderable(makeEvent({ event: 'context_updated', response: {} }))).toBe(false);
    expect(isWorkerEventRenderable(makeEvent({ event: 'tokens_updated', response: {} }))).toBe(false);
  });

  it('is true for lifecycle events (system notifications)', () => {
    expect(isWorkerEventRenderable(makeEvent({ event: 'started' }))).toBe(true);
    expect(isWorkerEventRenderable(makeEvent({ event: 'worker_completed', response: { worker_name: 'w1' } }))).toBe(true);
  });

  it('is false for internal assistant messages with empty content and no reasoning', () => {
    const evt = makeEvent({
      event: 'worker_message',
      response: { content: '', reasoning_content: undefined, response_type: 'answer' },
    });
    expect(isWorkerEventRenderable(evt)).toBe(false);
  });

  it('is true for internal assistant messages with content', () => {
    const evt = makeEvent({ event: 'worker_message', response: { content: 'Hello' } });
    expect(isWorkerEventRenderable(evt)).toBe(true);
  });

  it('is true for internal assistant messages with only reasoning_content', () => {
    const evt = makeEvent({ event: 'assistant_message', response: { reasoning_content: 'thinking...' } });
    expect(isWorkerEventRenderable(evt)).toBe(true);
  });

  it('is false for raw WS header-only events', () => {
    expect(isWorkerEventRenderable({ type: 'worker:context_updated', data: {}, timestamp: 't' })).toBe(false);
    expect(isWorkerEventRenderable({ type: 'worker:tokens_updated', data: {}, timestamp: 't' })).toBe(false);
  });

  it('is false for raw WS empty worker_message (placeholder)', () => {
    expect(isWorkerEventRenderable({ type: 'worker:worker_message', data: { content: '' }, timestamp: 't' })).toBe(false);
    expect(isWorkerEventRenderable({ type: 'worker:assistant_message', data: {}, timestamp: 't' })).toBe(false);
  });

  it('is true for raw WS worker_message with content or reasoning', () => {
    expect(isWorkerEventRenderable({ type: 'worker:worker_message', data: { content: 'Hi' }, timestamp: 't' })).toBe(true);
    expect(isWorkerEventRenderable({ type: 'worker:assistant_message', data: { reasoning_content: 'r' }, timestamp: 't' })).toBe(true);
  });

  it('is true for raw WS lifecycle/warning events', () => {
    expect(isWorkerEventRenderable({ type: 'worker:worker_completed', data: { worker_name: 'w1' }, timestamp: 't' })).toBe(true);
    expect(isWorkerEventRenderable({ type: 'worker:token_warning', data: { token_count: 1 }, timestamp: 't' })).toBe(true);
  });

  it('is true for raw WS user_message with a query', () => {
    expect(isWorkerEventRenderable({ type: 'worker:user_message', data: { query: 'Do it' }, timestamp: 't' })).toBe(true);
  });
});

// ── tool_call_id plumbing (frontend step 1) ────────────────────────────────
// The backend now sends tool_call_id on tool_call_start / tool_call /
// tool_result.  Step 1 only plumbs it through the WS normalisation and both
// dedup keys; eventId() and bubble pairing are deliberately untouched.
describe('tool_call_id plumbing', () => {
  const TCID = 'call_abc123';

  it('carries tool_call_id into request for all three tool events', () => {
    const trio = [
      ['tool_call_start', { tool_name: 'read_file', arguments: '{"path":"/a"}', tool_call_id: TCID }],
      ['tool_call', { tool_name: 'read_file', arguments: '{"path":"/a"}', tool_call_id: TCID }],
      ['tool_result', { tool_name: 'read_file', success: true, result: 'ok', tool_call_id: TCID }],
    ];
    for (const [eventType, data] of trio) {
      const { request } = normaliseToolEvent(eventType, data);
      expect(request.tool_call_id).toBe(TCID);
      // Existing keys are unchanged.
      expect(request.tool).toBe('read_file');
    }
    // status/success keys still behave as before.
    expect(normaliseToolEvent('tool_call_start', trio[0][1]).request.status).toBe('running');
    expect(normaliseToolEvent('tool_call', trio[1][1]).request.args).toEqual({ path: '/a' });
    expect(normaliseToolEvent('tool_result', trio[2][1]).request.success).toBe(true);
    expect(normaliseToolEvent('tool_result', trio[2][1]).response).toEqual({ result: 'ok' });
  });

  it('adds no tool_call_id key when the backend sent none (backward compat)', () => {
    for (const eventType of ['tool_call_start', 'tool_call', 'tool_result']) {
      const { request } = normaliseToolEvent(eventType, { tool_name: 'x', arguments: {}, success: true, result: 'r' });
      expect('tool_call_id' in request).toBe(false);
    }
    // Absent / empty / non-string ids must not change the key at all.
    expect(makeDedupKey('tool_call', 'T')).toBe('tool_call|T');
    expect(makeDedupKey('worker_message', 'T')).toBe('final_response|T');
    expect(makeDedupKey('tool_call', 'T', '')).toBe('tool_call|T');
    expect(makeDedupKey('tool_call', 'T', undefined)).toBe('tool_call|T');
  });

  it('keeps sibling tool events distinct in the dedup key', () => {
    const a = makeDedupKey('tool_call', 'T', 'call_1');
    const b = makeDedupKey('tool_call', 'T', 'call_2');
    expect(a).toBe('tool_call|T|tccall_1');
    expect(b).toBe('tool_call|T|tccall_2');
    expect(a).not.toBe(b);
    // Reproduce the panel's dedup filter (key + Set) to prove the second
    // same-type/same-timestamp event is no longer swallowed.
    const seen = new Set([a]);
    expect(seen.has(b)).toBe(false);
    // Control: without ids the old behaviour (collapse) still holds.
    expect(makeDedupKey('tool_call', 'T')).toBe(makeDedupKey('tool_call', 'T'));
  });
});

// ── Tool-call phase identity + merge (frontend step 2) ─────────────────────
// One backend tool call arrives as three events (start / call / result) at
// three different timestamps with three different event names.  They must
// share ONE message identity so a later merge step can show a single bubble.
// Without a backend id the historical behaviour (three distinct rows) is kept.

// Build a phase event with the essentials; `tcId` omitted => no id at all.
function toolPhase(event, tcId, opts = {}) {
  const {
    timestamp = '2026-08-01T10:00:00.000Z',
    tool = 'read_file',
    args = { path: '/a' },
    result,
    success,
  } = opts;
  const request = { tool, args };
  if (tcId) request.tool_call_id = tcId;
  if (success !== undefined) request.success = success;
  const response = result !== undefined ? { result } : {};
  return makeEvent({ session_id: 'sess1', worker_name: 'w1', event, timestamp, request, response });
}

describe('tool-call phase identity (step 2)', () => {
  it('gives all three phases of one call the same _id', () => {
    const start = adaptWorkerEvent(toolPhase('tool_call_start', 'call_1'));
    const call = adaptWorkerEvent(toolPhase('tool_call', 'call_1'));
    const result = adaptWorkerEvent(toolPhase('tool_result', 'call_1', { result: 'ok' }));
    expect(call._id).toBe(start._id);
    expect(result._id).toBe(start._id);
    // Sanity: the shared id is built from the backend id, not the event name.
    expect(start._id).toBe(toolCallEventId(toolPhase('tool_call_start', 'call_1')));
    expect(start._id).toContain('tccall_1');
    // A paired result routes into the call bubble.
    expect(result.role).toBe('tool_call');
    expect(result.result).toBe('ok');
    expect(result.tool_call_id).toBe('call_1');
  });

  it('never collides two different calls in the same session', () => {
    const a = adaptWorkerEvent(toolPhase('tool_call', 'call_1'))._id;
    const b = adaptWorkerEvent(toolPhase('tool_call', 'call_2'))._id;
    expect(a).not.toBe(b);
    expect(new Set([a, b]).size).toBe(2);
    // Each result lines up with ITS OWN call, not with the sibling.
    expect(adaptWorkerEvent(toolPhase('tool_result', 'call_2', { result: 'second' }))._id).toBe(b);
    expect(adaptWorkerEvent(toolPhase('tool_result', 'call_1', { result: 'first' }))._id).toBe(a);
  });

  it('falls back to three distinct ids and a standalone tool_result without an id', () => {
    const start = adaptWorkerEvent(toolPhase('tool_call_start'));
    const call = adaptWorkerEvent(toolPhase('tool_call'));
    const result = adaptWorkerEvent(toolPhase('tool_result', undefined, { result: 'ok' }));
    expect(new Set([start._id, call._id, result._id]).size).toBe(3);
    expect(toolCallEventId(toolPhase('tool_call'))).toBeNull();
    // Unpaired results keep today's role + content exactly.
    expect(result.role).toBe('tool_result');
    expect(result.content).toBe('ok');
    expect(result.result).toBe('ok');
    expect('tool_call_id' in result).toBe(false);
    expect('tool_call_id' in call).toBe(false);
  });

  it('keeps eventId() output byte-identical to the pre-refactor literals', () => {
    // Full identity: session_id + worker_name + i<id> + l<label> + ts + event.
    expect(adaptWorkerEvent(makeEvent({
      session_id: 'sess1',
      worker_name: 'w1',
      instance_id: 0,
      instance_label: 'L',
      timestamp: '2026-08-01T10:00:00.000Z',
      event: 'tool_call',
      request: { tool: 't', args: {} },
    }))._id).toBe('sess1_w1_i0_lL_2026-08-01T10:00:00.000Z_tool_call');
    // Sparse variants — the join('_') shape must not shift.
    expect(adaptWorkerEvent({ event: 'tool_call' })._id).toBe('tool_call');
    expect(adaptWorkerEvent({ event: 'tool_call', timestamp: 'T' })._id).toBe('T_tool_call');
    expect(adaptWorkerEvent({ session_id: 's', event: 'tool_call', timestamp: 'T' })._id).toBe('s_T_tool_call');
    expect(adaptWorkerEvent({ event: 'tool_call', instance_id: 0 })._id).toBe('i0_tool_call');
    expect(adaptWorkerEvent({ event: 'tool_call', instance_label: 'L2' })._id).toBe('lL2_tool_call');
  });
});

describe('mergeToolMessages (step 2)', () => {
  it('collapses start + call + result into ONE entry carrying the result', () => {
    const start = adaptWorkerEvent(toolPhase('tool_call_start', 'call_1'));
    const call = adaptWorkerEvent(toolPhase('tool_call', 'call_1'));
    const done = adaptWorkerEvent(toolPhase('tool_result', 'call_1', { result: 'file contents' }));
    let list = mergeToolMessages([], start, 1);
    list = mergeToolMessages(list, call, 2);
    list = mergeToolMessages(list, done, 3);
    expect(list).toHaveLength(1);
    expect(list[0]._seq).toBe(1);          // first-insert sequence kept
    expect(list[0]._id).toBe(start._id);   // first-insert identity kept
    expect(list[0].role).toBe('tool_call');
    expect(list[0].result).toBe('file contents');
    expect(list[0].tool_name).toBe('read_file');
    expect(list[0].arguments).toEqual({ path: '/a' });
    // Round-B rule: content is FIRST-INSERT authoritative — the result phase
    // must NOT overwrite the name/args JSON with the result text.
    expect(list[0].content).toBe(start.content);
    expect(list[0].content).not.toBe('file contents');
    expect(JSON.parse(list[0].content)).toEqual({ name: 'read_file', arguments: { path: '/a' } });
  });

  it('orders two calls by first insert and attributes reversed results correctly', () => {
    let list = mergeToolMessages([], adaptWorkerEvent(toolPhase('tool_call_start', 'call_1', { tool: 'read_file' })), 1);
    list = mergeToolMessages(list, adaptWorkerEvent(toolPhase('tool_call_start', 'call_2', { tool: 'write_file' })), 2);
    // Results arrive in REVERSE order: call_2 finishes first.
    list = mergeToolMessages(list, adaptWorkerEvent(toolPhase('tool_result', 'call_2', { tool: 'write_file', result: 'wrote B' })), 3);
    list = mergeToolMessages(list, adaptWorkerEvent(toolPhase('tool_result', 'call_1', { tool: 'read_file', result: 'read A' })), 4);
    expect(list).toHaveLength(2);
    expect(list.map(m => m.tool_call_id)).toEqual(['call_1', 'call_2']);
    expect(list.map(m => m._seq)).toEqual([1, 2]);
    // Attribution proven on the result text, not just on the count.
    expect(list[0].result).toBe('read A');
    expect(list[1].result).toBe('wrote B');
    expect(list[0].tool_name).toBe('read_file');
    expect(list[1].tool_name).toBe('write_file');
  });

  it('does not mutate prev (array or entry)', () => {
    const first = mergeToolMessages([], adaptWorkerEvent(toolPhase('tool_call_start', 'call_1')), 7);
    const snapshot = JSON.parse(JSON.stringify(first));
    const next = mergeToolMessages(first, adaptWorkerEvent(toolPhase('tool_result', 'call_1', { result: 'done' })), 8);
    expect(first).toHaveLength(1);
    expect(JSON.parse(JSON.stringify(first))).toEqual(snapshot);
    expect(first[0].result).toBeUndefined();
    expect(next).not.toBe(first);
    expect(next[0]).not.toBe(first[0]);
    expect(next[0].result).toBe('done');
  });

  it('appends messages that carry no tool_call_id', () => {
    const a = mergeToolMessages([], adaptWorkerEvent(toolPhase('tool_call')), 1);
    const b = mergeToolMessages(a, adaptWorkerEvent(toolPhase('tool_call')), 2);
    expect(a).toHaveLength(1);
    expect(b).toHaveLength(2);
    expect(b.map(m => m._seq)).toEqual([1, 2]);
    // A third id-less phase still appends — the legacy three-row layout.
    const c = mergeToolMessages(b, adaptWorkerEvent(toolPhase('tool_result', undefined, { result: 'r' })), 3);
    expect(c).toHaveLength(3);
  });

  it('never erases an earlier field with undefined', () => {
    const base = { _id: 'x', tool_call_id: 'call_9', error: 'boom', content: 'c', _seq: 1 };
    const list = mergeToolMessages([base], { _id: 'x', tool_call_id: 'call_9', result: 'late', content: 'c2' }, 2);
    expect(list[0].error).toBe('boom');
    expect(list[0].result).toBe('late');
    expect(list[0]._seq).toBe(1);
    expect(list[0].content).toBe('c');   // content is never overlaid
  });
});

// ── ACCEPTANCE: the whole real-world sequence ───────────────────────────────
// Two tool calls emitted start→call each, then the two results arriving in
// REVERSE order (call 2 first).  This is the panel's actual fold.
describe('ACCEPTANCE — two interleaved tool calls, reversed results (step 2)', () => {
  function foldFetch(messages) {
    let msgs = [];
    let seq = 0;
    for (const msg of messages) {
      seq += 1;
      msgs = mergeToolMessages(msgs, msg, seq);
    }
    return msgs;
  }

  it('pairs each result to its own call and orders by call start', () => {
    const messages = [
      adaptWorkerEvent(toolPhase('tool_call_start', 'call_1', { tool: 'read_file' })),
      adaptWorkerEvent(toolPhase('tool_call_start', 'call_2', { tool: 'write_file' })),
      adaptWorkerEvent(toolPhase('tool_call', 'call_1', { tool: 'read_file' })),
      adaptWorkerEvent(toolPhase('tool_call', 'call_2', { tool: 'write_file' })),
      // Results in REVERSE order: call 2 finishes first.
      adaptWorkerEvent(toolPhase('tool_result', 'call_2', { tool: 'write_file', result: 'wrote B' })),
      adaptWorkerEvent(toolPhase('tool_result', 'call_1', { tool: 'read_file', result: 'read A' })),
    ];
    const msgs = foldFetch(messages);
    expect(msgs).toHaveLength(2);
    expect(new Set(msgs.map(m => m._id)).size).toBe(2);
    // Ordered by call START, not by result arrival.
    expect(msgs.map(m => m.tool_call_id)).toEqual(['call_1', 'call_2']);
    expect(msgs.map(m => m._seq)).toEqual([1, 2]);
    // Attribution proven on the result TEXT, not on the count.
    expect(msgs[0].result).toBe('read A');
    expect(msgs[1].result).toBe('wrote B');
    // Round-B fix: content is still the name/args JSON the bubble parses.
    expect(JSON.parse(msgs[0].content)).toEqual({ name: 'read_file', arguments: { path: '/a' } });
    expect(JSON.parse(msgs[1].content)).toEqual({ name: 'write_file', arguments: { path: '/a' } });
    expect(msgs[0].content).not.toBe('read A');
    expect(msgs[1].content).not.toBe('wrote B');
    expect(msgs.map(m => m.role)).toEqual(['tool_call', 'tool_call']);
  });

  it('keeps the old three-row layout for id-less (replayed) phases', () => {
    const msgs = foldFetch([
      adaptWorkerEvent(toolPhase('tool_call_start')),
      adaptWorkerEvent(toolPhase('tool_call')),
      adaptWorkerEvent(toolPhase('tool_result', undefined, { result: 'legacy ok' })),
    ]);
    expect(msgs).toHaveLength(3);
    expect(msgs.map(m => m.role)).toEqual(['tool_call', 'tool_call', 'tool_result']);
    expect(msgs[2].content).toBe('legacy ok');
    expect(new Set(msgs.map(m => m._id)).size).toBe(3);
  });
});

