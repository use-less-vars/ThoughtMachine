// @vitest-environment jsdom
/*
 * ConfigPanel.addendum.test.jsx — W1.5 addendum panel surface
 *
 * jsdom + @testing-library/react, per-file environment (ConfigPanel has no
 * `test` config in vite.config.js).  Covers the operator-visible half of the
 * addendum REST surface (GET/POST/DELETE /api/session/{id}/addendum):
 *   - the list and the totals line render what the SERVER returned
 *   - a write adopts the server's resulting entry set (never a local append)
 *   - the 409 refusal channel renders the boundary's own sentence verbatim,
 *     leaves the stored list untouched, and keeps the typed text for editing
 *   - the refusal banner does not survive a later accepted write
 *   - "clear all" is two-step: the first click only arms, the second fires
 *     DELETE
 *   - the store echo (receiveAddendumUpdated) merges by entry id — it neither
 *     duplicates nor wipes the rendered rows
 *
 * The panel is a mirror, not an owner: it must never invent a char count, a
 * budget, or an entry.  These tests therefore assert against the payloads the
 * mocked server hands back, not against panel-side arithmetic.
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, waitFor, act } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import ConfigPanel from '../ConfigPanel';
import useStore from '../../store/useStore';

// The store's permissionDefaults slice is hydrated from the backend single
// owner (GET /api/permission-defaults) and starts {} (fail-closed); the panel's
// permissions tab reads it, so seed it here like ConfigPanel.test.jsx does.
const TEST_PERMISSION_DEFAULTS = {
  git: 'read',
  filesystem: 'read',
  container: false,
  network: 'banned',
  mcp: 'banned',
  host_bash: 'banned',
};

const baseConfig = {
  mode: 'custom',
  temperature: 0.7,
  max_turns: 10,
  provider: 'openai',
  provider_id: 'p1',
  model: 'gpt-4o',
  system_prompt: 'Be nice',
  tools: [{ name: 'read_file', enabled: true }],
  session_permissions: { filesystem: 'write' },
  workspace_path: '/home/jojo/work',
};

const SESSION = 'sess-1';
const BUDGET = 100;
const MAX_ENTRIES = 64;

const makeEntry = (id, text) => ({
  id,
  ts: '2026-01-01T00:00:00+00:00',
  source: 'operator',
  text,
  pinned: false,
});

const E1 = makeEntry('a-000000000001', 'always answer in metric units');
const E2 = makeEntry('a-000000000002', 'never truncate tool output');
const E3 = makeEntry('a-000000000003', 'prefer boring solutions');

// GET /api/session/{id}/addendum payload shape (backend addendum_routes).
const addendumBody = (entries, total_chars) => ({
  entries,
  total_chars,
  budget_chars: BUDGET,
  max_entries: MAX_ENTRIES,
  session_id: SESSION,
});

const clearedBody = () => ({
  status: 'cleared',
  entries: [],
  total_chars: 0,
  budget_chars: BUDGET,
  max_entries: MAX_ENTRIES,
  session_id: SESSION,
});

const response = (status, body) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
  text: async () => JSON.stringify(body),
});

/*
 * URL-routing fetch stub.  ConfigPanel issues three fetches on mount
 * (/api/tools, /api/session/{id}/permissions and /api/session/{id}/addendum),
 * so a blanket mock would let the addendum GET answer the tool-list call.  The
 * router keys on the URL and on init.method and records per-verb call counts.
 */
function installFetch(handlers) {
  const calls = { get: 0, post: 0, delete: 0, postBodies: [] };
  const fetchMock = vi.fn(async (url, init = {}) => {
    const target = String(url);
    const method = String(init.method || 'GET').toUpperCase();
    if (target.includes('/addendum')) {
      if (method === 'POST') {
        calls.post += 1;
        calls.postBodies.push(init.body ? JSON.parse(init.body) : null);
        return handlers.post(calls.post);
      }
      if (method === 'DELETE') {
        calls.delete += 1;
        return handlers.delete ? handlers.delete(calls.delete) : response(200, clearedBody());
      }
      calls.get += 1;
      return handlers.get(calls.get);
    }
    if (target.includes('/permissions')) return response(200, { raw: {} });
    if (target.includes('/api/tools')) return response(200, { tools: [] });
    return response(200, {});
  });
  vi.stubGlobal('fetch', fetchMock);
  return { fetchMock, calls };
}

function renderPanel(props = {}) {
  const sendCommand = vi.fn();
  const utils = render(
    <ConfigPanel
      config={baseConfig}
      sendCommand={sendCommand}
      providers={[]}
      availableTools={[]}
      wsConnected
      defaultConfigSaveStatus={null}
      onClearDefaultSaveStatus={vi.fn()}
      sessionId={SESSION}
      {...props}
    />
  );
  return { sendCommand, ...utils };
}

const totals = () => screen.getByTestId('addendum-totals');
const rows = () => screen.getByTestId('addendum-list').querySelectorAll('li');
const textarea = () => screen.getByLabelText('Addendum entry text');

const submitText = (text) => {
  fireEvent.change(textarea(), { target: { value: text } });
  fireEvent.click(screen.getByRole('button', { name: 'Add entry' }));
};

beforeEach(() => {
  useStore.getState().reset();
  useStore.setState({ permissionDefaults: { ...TEST_PERMISSION_DEFAULTS } });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ==========================================================================
// Render + write chain
// ==========================================================================
describe('ConfigPanel addendum', () => {
  it('renders the loaded list, adopts the server set after a write, and never duplicates on the store echo', async () => {
    const { calls } = installFetch({
      get: () => response(200, addendumBody([E1, E2], 53)),
      post: () =>
        response(200, { status: 'accepted', entry: E3, ...addendumBody([E1, E2, E3], 82) }),
    });
    renderPanel();

    // The rendered list IS the server's answer.
    await waitFor(() =>
      expect(screen.getByTestId(`addendum-entry-${E1.id}`)).toBeInTheDocument()
    );
    expect(screen.getByTestId(`addendum-entry-${E2.id}`)).toBeInTheDocument();
    expect(totals().textContent).toBe('53 / 100 chars · 2 / 64 entries');
    expect(rows()).toHaveLength(2);
    // Per-entry char count is computed from the text (the payload carries none).
    expect(screen.getByTestId(`addendum-entry-${E1.id}`).textContent).toContain(
      `${E1.text.length} chars`
    );

    submitText(E3.text);
    await waitFor(() =>
      expect(screen.getByTestId(`addendum-entry-${E3.id}`)).toBeInTheDocument()
    );
    expect(calls.post).toBe(1);
    // The text goes out verbatim — trimming here would misreport the budget.
    expect(calls.postBodies[0]).toEqual({ text: E3.text });
    expect(totals().textContent).toBe('82 / 100 chars · 3 / 64 entries');
    expect(rows()).toHaveLength(3);
    // Accepted write clears the composer.
    expect(textarea().value).toBe('');

    // The store echo (the panel's own coherence call, replayed) merges by id:
    // same keys, no duplicate row, nothing wiped.
    act(() => {
      useStore.getState().receiveAddendumUpdated(SESSION, { entries: [E1, E2, E3] });
    });
    expect(Object.keys(useStore.getState().addendumEntries[SESSION]).sort()).toEqual(
      [E1.id, E2.id, E3.id].sort()
    );
    expect(rows()).toHaveLength(3);
    expect(screen.getByTestId(`addendum-entry-${E3.id}`)).toBeInTheDocument();
  });

  // ========================================================================
  // Refusal channel + recovery
  // ========================================================================
  it('shows the boundary refusal verbatim, leaves the list untouched, then recovers after a clear', async () => {
    installFetch({
      get: () => response(200, addendumBody([E1], 90)),
      post: (n) => {
        if (n === 1) {
          return response(409, {
            status: 'refused',
            reason: 'budget_exceeded',
            message: 'addendum budget exceeded: 120 > 100 chars',
            entries: [E1],
            total_chars: 90,
            budget_chars: BUDGET,
            max_entries: MAX_ENTRIES,
          });
        }
        return response(200, { status: 'accepted', entry: E3, ...addendumBody([E3], E3.text.length) });
      },
    });
    renderPanel();

    await waitFor(() => expect(screen.getByTestId(`addendum-entry-${E1.id}`)).toBeInTheDocument());
    expect(totals().textContent).toBe('90 / 100 chars · 1 / 64 entries');

    submitText('x'.repeat(30));
    await waitFor(() => expect(screen.getByTestId('addendum-refusal')).toBeInTheDocument());
    const banner = screen.getByTestId('addendum-refusal');
    expect(banner).toHaveTextContent('addendum budget exceeded: 120 > 100 chars');
    expect(banner).toHaveTextContent('budget_exceeded');
    // Refused, never truncated: the stored set is exactly what it was.
    expect(rows()).toHaveLength(1);
    expect(totals().textContent).toBe('90 / 100 chars · 1 / 64 entries');
    expect(screen.queryByTestId('addendum-error')).not.toBeInTheDocument();

    // Recovery: the clear action is reachable from the panel itself.
    fireEvent.click(screen.getByRole('button', { name: 'Clear all' }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm clear' }));
    await waitFor(() => expect(screen.queryByTestId('addendum-list')).not.toBeInTheDocument());
    expect(screen.getByText('No addendum entries.')).toBeInTheDocument();
    expect(totals().textContent).toBe('0 / 100 chars · 0 / 64 entries');
    expect(screen.queryByTestId('addendum-refusal')).not.toBeInTheDocument();

    // The same write now fits.
    submitText(E3.text);
    await waitFor(() => expect(screen.getByTestId(`addendum-entry-${E3.id}`)).toBeInTheDocument());
    expect(screen.queryByTestId('addendum-refusal')).not.toBeInTheDocument();
  });

  it('drops a stale refusal banner once a later write is accepted', async () => {
    installFetch({
      get: () => response(200, addendumBody([], 0)),
      post: (n) => {
        if (n === 1) {
          return response(409, {
            status: 'refused',
            reason: 'budget_exceeded',
            message: 'addendum budget exceeded: 120 > 100 chars',
            entries: [],
            total_chars: 0,
            budget_chars: BUDGET,
            max_entries: MAX_ENTRIES,
          });
        }
        return response(200, { status: 'accepted', entry: E3, ...addendumBody([E3], E3.text.length) });
      },
    });
    renderPanel();

    await waitFor(() => expect(totals().textContent).toBe('0 / 100 chars · 0 / 64 entries'));
    expect(screen.getByText('No addendum entries.')).toBeInTheDocument();

    submitText('x'.repeat(30));
    await waitFor(() => expect(screen.getByTestId('addendum-refusal')).toBeInTheDocument());

    submitText(E3.text);
    await waitFor(() => expect(screen.getByTestId(`addendum-entry-${E3.id}`)).toBeInTheDocument());
    expect(screen.queryByTestId('addendum-refusal')).not.toBeInTheDocument();
  });

  it('keeps the typed text when the write is refused', async () => {
    const typed = 'a much longer sentence that will not fit the remaining budget';
    installFetch({
      get: () => response(200, addendumBody([E1], 90)),
      post: () =>
        response(409, {
          status: 'refused',
          reason: 'budget_exceeded',
          message: `addendum budget exceeded: ${90 + typed.length} > 100 chars`,
          entries: [E1],
          total_chars: 90,
          budget_chars: BUDGET,
          max_entries: MAX_ENTRIES,
        }),
    });
    renderPanel();

    await waitFor(() => expect(screen.getByTestId(`addendum-entry-${E1.id}`)).toBeInTheDocument());

    submitText(typed);
    await waitFor(() => expect(screen.getByTestId('addendum-refusal')).toBeInTheDocument());
    // Nothing was persisted, so the operator must be able to edit it down.
    expect(textarea().value).toBe(typed);
  });

  // ========================================================================
  // Two-step clear
  // ========================================================================
  it('arms on the first click and fires DELETE only on the confirm click', async () => {
    const { calls } = installFetch({
      get: () => response(200, addendumBody([E1], E1.text.length)),
    });
    renderPanel();

    await waitFor(() => expect(screen.getByTestId(`addendum-entry-${E1.id}`)).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: 'Clear all' }));
    // Armed only: the destructive call has NOT been made and nothing vanished.
    expect(screen.getByRole('button', { name: 'Confirm clear' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument();
    expect(calls.delete).toBe(0);
    expect(screen.getByTestId(`addendum-entry-${E1.id}`)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Confirm clear' }));
    await waitFor(() => expect(calls.delete).toBe(1));
    await waitFor(() => expect(screen.queryByTestId('addendum-list')).not.toBeInTheDocument());
    expect(screen.getByText('No addendum entries.')).toBeInTheDocument();
  });
});
