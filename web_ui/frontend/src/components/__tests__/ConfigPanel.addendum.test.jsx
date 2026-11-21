// @vitest-environment jsdom
/*
 * ConfigPanel.addendum.test.jsx — W1.5 addendum panel surface
 *
 * jsdom + @testing-library/react, per-file environment (ConfigPanel has no
 * `test` config in vite.config.js).  Covers the operator-visible half of the
 * addendum REST surface — ONE read plus the writes:
 *   GET    /api/session/{id}/addendum/effective
 *   POST   /api/session/{id}/addendum
 *   DELETE /api/session/{id}/addendum
 *   PATCH  /api/session/{id}/addendum/entries/{entry_id}
 *   DELETE /api/session/{id}/addendum/entries/{entry_id}
 *
 * The contract these tests pin:
 *   - ONE read paints the whole section.  GET .../addendum/effective answers
 *     with the entry set AND the block the next turn will carry, resolved once
 *     by the one resolver, so the rows you see and the preview you see are two
 *     views of a single payload and cannot disagree.  There is no second list
 *     request, and the seam test below asserts the panel makes exactly one.
 *   - A write only echoes the server's resulting set into the session store and
 *     bumps an epoch; what ends up on screen always comes from the re-read, never
 *     from a local append and never from panel-side arithmetic.  The mocked
 *     server here is therefore STATEFUL: the GET answers with whatever the last
 *     write left behind, exactly like the real read whose whole job is to be the
 *     authority.
 *   - The 409 refusal channel renders the boundary's own sentence verbatim,
 *     leaves the stored list untouched, and keeps the typed text for editing.
 *   - The refusal banner does not survive a later accepted write.
 *   - "clear all" is two-step: the first click only arms, the second fires
 *     DELETE, and the post-clear re-read is what removes the rows.
 *   - The store echo (receiveAddendumUpdated) merges by entry id for WS/event
 *     traffic, while the REST echoes use mode:'replace' — the only shape that can
 *     represent a clear or a removal.
 *
 * The panel is a mirror, not an owner: it must never invent a char count, a
 * budget, an entry, or a rendered block.  These tests therefore assert against
 * the payloads the mocked server hands back, not against panel-side arithmetic.
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, waitFor, act, within } from '@testing-library/react';
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

// The backend measures the text as stored; the panel only echoes the number, so
// every expectation below is derived from the payload rather than hard-coded.
const textLen = (entries) => entries.reduce((n, e) => n + (e.text || '').length, 0);
const totalsText = (entries) =>
  `${textLen(entries)} / ${BUDGET} chars · ${entries.length} / ${MAX_ENTRIES} entries`;

// The block the resolver would render for a set: the framing line plus one line
// per entry, and NO block at all when there is nothing to render.
const renderBlock = (entries) =>
  entries.length === 0 ? '' : `[addendum]\n${entries.map((e) => e.text).join('\n')}`;

// WRITE-response shape only.  POST/PATCH/DELETE answer with the resulting entry
// set (plus `status`/`entry`), which the panel echoes into the store; the panel
// never renders from these, so they carry no `rendered_block`.
const addendumBody = (entries) => ({
  entries,
  total_chars: textLen(entries),
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

// THE read: GET /api/session/{id}/addendum/effective.  One payload carries the
// entry set AND the block the next turn receives, so the list and the preview
// are two views of one resolution.
const effectiveBody = (entries, renderedBlock) => ({
  status: 'ok',
  session_id: SESSION,
  entries,
  total_chars: textLen(entries),
  budget_chars: BUDGET,
  max_entries: MAX_ENTRIES,
  rendered_block: renderedBlock,
  block_chars: renderedBlock.length,
  applied: 'next_query',
  resolved_via: 'live',
  diagnostics: {
    live_bridges: 1,
    agent_bearing_bridges: 1,
    session_object_id: 'obj-1',
    agent_session_object_id: 'obj-1',
    same_object: true,
  },
});

const response = (status, body) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
  text: async () => JSON.stringify(body),
});

/*
 * A stateful stand-in for the real read: the GET always answers with the set the
 * last successful write left behind.  That is what makes "the list is re-read
 * from the authority" testable — a panel that patched itself from a write
 * response would still pass, but a panel that kept its own stale copy would not.
 */
function statefulServer(initialEntries, renderedBlock) {
  let entries = initialEntries;
  let block = renderedBlock === undefined ? renderBlock(initialEntries) : renderedBlock;
  return {
    get: () => response(200, effectiveBody(entries, block)),
    set(nextEntries, nextBlock) {
      entries = nextEntries;
      block = nextBlock === undefined ? renderBlock(nextEntries) : nextBlock;
    },
  };
}

/*
 * URL-routing fetch stub.  ConfigPanel issues two fetches on mount
 * (/api/session/{id}/permissions and /api/session/{id}/addendum/effective, plus
 * /api/tools), so a blanket mock would let the addendum GET answer the tool-list
 * call.  The router keys on the URL and on init.method, records per-verb call
 * counts, and records every addendum GET URL so a test can prove WHICH read the
 * panel made (there must be exactly one, and it must be the /effective one).
 */
function installFetch(handlers) {
  const calls = {
    get: 0,
    post: 0,
    patch: 0,
    delete: 0,
    getUrls: [],
    postBodies: [],
    patchBodies: [],
  };
  const fetchMock = vi.fn(async (url, init = {}) => {
    const target = String(url);
    const method = String(init.method || 'GET').toUpperCase();
    if (target.includes('/addendum')) {
      if (method === 'POST') {
        calls.post += 1;
        calls.postBodies.push(init.body ? JSON.parse(init.body) : null);
        return handlers.post(calls.post, target);
      }
      if (method === 'PATCH') {
        calls.patch += 1;
        calls.patchBodies.push(init.body ? JSON.parse(init.body) : null);
        return handlers.patch(calls.patch, target);
      }
      if (method === 'DELETE') {
        calls.delete += 1;
        return handlers.delete ? handlers.delete(calls.delete, target) : response(200, clearedBody());
      }
      calls.get += 1;
      calls.getUrls.push(target);
      return handlers.get(calls.get, target);
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

describe('ConfigPanel addendum surface', () => {
  // ========================================================================
  // The loaded set, a write, and the store echo
  // ========================================================================
  it('renders the loaded list, adopts the server set after a write, and never duplicates on the store echo', async () => {
    const server = statefulServer([E1, E2]);
    const { calls } = installFetch({
      get: server.get,
      post: () => {
        // The write itself only reports the resulting set; the RENDER comes from
        // the re-read below.
        server.set([E1, E2, E3]);
        return response(200, { status: 'accepted', entry: E3, ...addendumBody([E1, E2, E3]) });
      },
    });
    renderPanel();

    // The rendered list IS the server's answer...
    await waitFor(() =>
      expect(screen.getByTestId(`addendum-entry-${E1.id}`)).toBeInTheDocument()
    );
    // ...and it came from exactly one request on mount, the /effective one.
    expect(calls.get).toBe(1);
    expect(calls.getUrls).toHaveLength(1);
    expect(calls.getUrls[0]).toContain('/addendum/effective');
    expect(screen.getByTestId(`addendum-entry-${E2.id}`)).toBeInTheDocument();
    expect(totals().textContent).toBe(totalsText([E1, E2]));
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
    // The write bumped the epoch, so the section was re-read from the authority.
    expect(calls.get).toBe(2);
    expect(totals().textContent).toBe(totalsText([E1, E2, E3]));
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
  // The seam: one read, one resolution
  // ========================================================================
  it('paints the list AND the preview from a single read, so the two cannot disagree', async () => {
    const BLOCK = '[addendum]\nalways answer in metric units\nnever truncate tool output';
    const server = statefulServer([E1, E2], BLOCK);
    const { calls } = installFetch({ get: server.get });
    renderPanel();

    await waitFor(() =>
      expect(screen.getByTestId('addendum-effective-block')).toBeInTheDocument()
    );

    // THE point of this test: one request on mount, and it is the /effective one.
    // A second list GET would be a second resolution of the same session state and
    // the two answers could eventually differ — which is the bug this seam removed.
    expect(calls.get).toBe(1);
    expect(calls.getUrls).toHaveLength(1);
    expect(calls.getUrls[0]).toContain(`/api/session/${SESSION}/addendum/effective`);

    // The rows ARE that payload's entries, in the payload's order...
    expect(rows()).toHaveLength(2);
    expect(Array.from(rows()).map((li) => li.getAttribute('data-testid'))).toEqual([
      `addendum-entry-${E1.id}`,
      `addendum-entry-${E2.id}`,
    ]);
    expect(totals().textContent).toBe(totalsText([E1, E2]));
    // ...and the preview IS that same payload's rendered_block, verbatim: the
    // panel never re-derives framing, so what it shows is what the turn receives.
    expect(screen.getByTestId('addendum-effective-block').textContent).toBe(BLOCK);
    // Even the preview's own counts come from the payload, not from the rows.
    expect(screen.getByTestId('addendum-effective-meta').textContent).toContain(
      '2 / 64 entries'
    );
  });

  // ========================================================================
  // Refusal channel + recovery
  // ========================================================================
  it('shows the boundary refusal verbatim, leaves the list untouched, then recovers after a clear', async () => {
    const server = statefulServer([E1]);
    const { calls } = installFetch({
      get: server.get,
      post: (n) => {
        if (n === 1) {
          return response(409, {
            status: 'refused',
            reason: 'budget_exceeded',
            message: 'addendum budget exceeded: 120 > 100 chars',
            ...addendumBody([E1]),
          });
        }
        server.set([E3]);
        return response(200, { status: 'accepted', entry: E3, ...addendumBody([E3]) });
      },
      delete: () => {
        server.set([], '');
        return response(200, clearedBody());
      },
    });
    renderPanel();

    await waitFor(() => expect(screen.getByTestId(`addendum-entry-${E1.id}`)).toBeInTheDocument());
    expect(totals().textContent).toBe(totalsText([E1]));

    submitText('x'.repeat(30));
    await waitFor(() => expect(screen.getByTestId('addendum-refusal')).toBeInTheDocument());
    const banner = screen.getByTestId('addendum-refusal');
    expect(banner).toHaveTextContent('addendum budget exceeded: 120 > 100 chars');
    expect(banner).toHaveTextContent('budget_exceeded');
    // Refused, never truncated: the stored set is exactly what it was — confirmed
    // against a re-read, since a refusal also bumps the epoch.
    await waitFor(() => expect(calls.get).toBe(2));
    expect(rows()).toHaveLength(1);
    expect(totals().textContent).toBe(totalsText([E1]));
    expect(screen.queryByTestId('addendum-error')).not.toBeInTheDocument();

    // Recovery: the clear action is reachable from the panel itself.
    fireEvent.click(screen.getByRole('button', { name: 'Clear all' }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm clear' }));
    await waitFor(() => expect(screen.queryByTestId('addendum-list')).not.toBeInTheDocument());
    expect(screen.getByText('No addendum entries.')).toBeInTheDocument();
    expect(totals().textContent).toBe(totalsText([]));
    expect(screen.queryByTestId('addendum-refusal')).not.toBeInTheDocument();

    // The same write now fits.
    submitText(E3.text);
    await waitFor(() => expect(screen.getByTestId(`addendum-entry-${E3.id}`)).toBeInTheDocument());
    expect(screen.queryByTestId('addendum-refusal')).not.toBeInTheDocument();
  });

  it('drops a stale refusal banner once a later write is accepted', async () => {
    const server = statefulServer([]);
    installFetch({
      get: server.get,
      post: (n) => {
        if (n === 1) {
          return response(409, {
            status: 'refused',
            reason: 'budget_exceeded',
            message: 'addendum budget exceeded: 120 > 100 chars',
            ...addendumBody([]),
          });
        }
        server.set([E3]);
        return response(200, { status: 'accepted', entry: E3, ...addendumBody([E3]) });
      },
    });
    renderPanel();

    await waitFor(() => expect(totals().textContent).toBe(totalsText([])));
    expect(screen.getByText('No addendum entries.')).toBeInTheDocument();

    submitText('x'.repeat(30));
    await waitFor(() => expect(screen.getByTestId('addendum-refusal')).toBeInTheDocument());

    submitText(E3.text);
    await waitFor(() => expect(screen.getByTestId(`addendum-entry-${E3.id}`)).toBeInTheDocument());
    expect(screen.queryByTestId('addendum-refusal')).not.toBeInTheDocument();
  });

  it('keeps the typed text when the write is refused', async () => {
    const typed = 'a much longer sentence that will not fit the remaining budget';
    const server = statefulServer([E1]);
    installFetch({
      get: server.get,
      post: () =>
        response(409, {
          status: 'refused',
          reason: 'budget_exceeded',
          message: `addendum budget exceeded: ${textLen([E1]) + typed.length} > 100 chars`,
          ...addendumBody([E1]),
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
    const server = statefulServer([E1]);
    const { calls } = installFetch({
      get: server.get,
      delete: () => {
        server.set([], '');
        return response(200, clearedBody());
      },
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
    // The rows disappear because the post-clear re-read says the set is empty.
    await waitFor(() => expect(calls.get).toBe(2));
    await waitFor(() => expect(screen.queryByTestId('addendum-list')).not.toBeInTheDocument());
    expect(screen.getByText('No addendum entries.')).toBeInTheDocument();
    expect(totals().textContent).toBe(totalsText([]));
  });

  // ========================================================================
  // Effective block preview
  // ========================================================================
  it('renders the block the backend RESOLVED for the next turn, and refetches it after a write', async () => {
    const BLOCK_BEFORE = '[addendum]\nalways answer in metric units\nnever truncate tool output';
    const BLOCK_AFTER = `${BLOCK_BEFORE}\nprefer boring solutions`;
    const server = statefulServer([E1, E2], BLOCK_BEFORE);
    const { calls } = installFetch({
      get: server.get,
      post: () => {
        server.set([E1, E2, E3], BLOCK_AFTER);
        return response(200, { status: 'accepted', entry: E3, ...addendumBody([E1, E2, E3]) });
      },
    });
    renderPanel();

    // The panel shows the resolved text verbatim: it never re-derives framing.
    await waitFor(() => expect(screen.getByTestId('addendum-effective-block')).toBeInTheDocument());
    expect(screen.getByTestId('addendum-effective-block').textContent).toBe(BLOCK_BEFORE);
    const meta = screen.getByTestId('addendum-effective-meta').textContent;
    // The route's `applied: 'next_query'` is reported in operator terms: the block
    // lands in the system prompt of the very next message, not after a restart.
    expect(meta).toContain('applies to your next message');
    expect(meta).toContain('resolved via live');
    // ...and the panel says out loud that nothing here is waiting for Apply (the
    // panel-wide Apply button is correctly grey on this tab: no draft state).
    expect(screen.getByTestId('addendum-persist-note').textContent).toContain('no Apply needed');
    expect(meta).toContain('2 / 64 entries');
    expect(screen.getByTestId('addendum-effective-diagnostics').textContent).toContain(
      'same session object: yes'
    );
    // One read painted the section, preview included.
    expect(calls.get).toBe(1);

    submitText(E3.text);
    await waitFor(() => expect(screen.getByTestId(`addendum-entry-${E3.id}`)).toBeInTheDocument());
    // A REST write lands in the session STORE, not in a running agent, so the
    // preview is re-fetched (never patched from the list).
    await waitFor(() => expect(calls.get).toBe(2));
    await waitFor(() =>
      expect(screen.getByTestId('addendum-effective-block').textContent).toBe(BLOCK_AFTER)
    );
  });

  // ========================================================================
  // Per-entry edit + remove
  // ========================================================================
  it('edits one entry in place with PATCH and adopts the server set', async () => {
    const edited = { ...E1, text: 'always answer in SI units' };
    const server = statefulServer([E1, E2]);
    const { calls } = installFetch({
      get: server.get,
      patch: (n, url) => {
        expect(String(url)).toContain(`/addendum/entries/${E1.id}`);
        server.set([edited, E2]);
        return response(200, {
          status: 'updated',
          entry: edited,
          ...addendumBody([edited, E2]),
        });
      },
    });
    renderPanel();

    await waitFor(() => expect(screen.getByTestId(`addendum-entry-${E1.id}`)).toBeInTheDocument());
    const row = screen.getByTestId(`addendum-entry-${E1.id}`);
    fireEvent.click(within(row).getByRole('button', { name: 'Edit' }));

    const editBox = screen.getByLabelText(`Edit addendum entry ${E1.id}`);
    // Seeded from the entry's text so an edit starts from what was stored.
    expect(editBox.value).toBe(E1.text);
    fireEvent.change(editBox, { target: { value: edited.text } });
    fireEvent.click(screen.getByRole('button', { name: 'Save entry' }));

    await waitFor(() => expect(calls.patch).toBe(1));
    // Text goes out verbatim, exactly like an append.
    expect(calls.patchBodies[0]).toEqual({ text: edited.text });
    // The edited text on screen is the re-read set's, and the preview was re-read
    // from the same resolution.
    await waitFor(() => expect(calls.get).toBe(2));
    await waitFor(() =>
      expect(screen.getByTestId(`addendum-entry-${E1.id}`).textContent).toContain(edited.text)
    );
    expect(totals().textContent).toBe(totalsText([edited, E2]));
    expect(screen.getByTestId('addendum-effective-block').textContent).toBe(
      renderBlock([edited, E2])
    );
    // The editor collapses once the server accepted the new text.
    expect(screen.queryByLabelText(`Edit addendum entry ${E1.id}`)).not.toBeInTheDocument();
    expect(screen.getByTestId(`addendum-entry-${E2.id}`)).toBeInTheDocument();
  });

  it('removes one entry on its own URL and drops the row (replace echo)', async () => {
    let removed = 0;
    const server = statefulServer([E1, E2]);
    installFetch({
      get: server.get,
      delete: (n, url) => {
        removed += 1;
        expect(String(url)).toContain(`/addendum/entries/${E1.id}`);
        server.set([E2]);
        return response(200, {
          status: 'removed',
          removed_id: E1.id,
          ...addendumBody([E2]),
        });
      },
    });
    renderPanel();

    await waitFor(() => expect(screen.getByTestId(`addendum-entry-${E1.id}`)).toBeInTheDocument());
    fireEvent.click(
      within(screen.getByTestId(`addendum-entry-${E1.id}`)).getByRole('button', { name: 'Remove' })
    );

    await waitFor(() => expect(removed).toBe(1));
    await waitFor(() => expect(screen.queryByTestId(`addendum-entry-${E1.id}`)).not.toBeInTheDocument());
    expect(rows()).toHaveLength(1);
    expect(totals().textContent).toBe(totalsText([E2]));
    // The preview follows the same re-read, so it cannot still advertise the
    // removed line.
    expect(screen.getByTestId('addendum-effective-block').textContent).toBe(renderBlock([E2]));
    // A removal is not representable by a merge: the echo replaces the slice.
    expect(Object.keys(useStore.getState().addendumEntries[SESSION])).toEqual([E2.id]);
  });

  it('makes a clear representable in the store slice (replace, not merge)', async () => {
    const server = statefulServer([E1]);
    installFetch({
      get: server.get,
      delete: () => {
        server.set([], '');
        return response(200, clearedBody());
      },
    });
    renderPanel();

    await waitFor(() => expect(screen.getByTestId(`addendum-entry-${E1.id}`)).toBeInTheDocument());
    // Seed the store slice the way a live event would, so the clear has
    // something to retract.
    act(() => {
      useStore.getState().receiveAddendumUpdated(SESSION, { entries: [E1] });
    });
    expect(Object.keys(useStore.getState().addendumEntries[SESSION])).toEqual([E1.id]);

    fireEvent.click(screen.getByRole('button', { name: 'Clear all' }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm clear' }));

    await waitFor(() => expect(screen.getByText('No addendum entries.')).toBeInTheDocument());
    expect(useStore.getState().addendumEntries[SESSION]).toEqual({});
  });
});
