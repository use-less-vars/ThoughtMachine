// @vitest-environment jsdom
import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import ConfigPanel from '../ConfigPanel';
import useStore from '../../store/useStore';
import {
  SESSION_RESOURCE_VOCAB,
  PERMISSION_RANK_ORDER,
  PILL_COLORS,
  getPill,
} from '../../data/permissionVocab';

// ── Backend stub (same conventions as ConfigPanelDraft.test.jsx) ─────────────
const jsonOk = (data, status = 200) => ({
  ok: true,
  status,
  json: async () => data,
  text: async () => JSON.stringify(data),
});

const DEFAULT_FALLBACK = {
  ok: true,
  status: 200,
  json: async () => ({ tools: [] }),
  text: async () => '',
};

const DEFAULT_ROUTES = {
  '/api/tools': jsonOk({ tools: [] }),
  '/api/health/containers': jsonOk({ docker: 'reachable' }),
  '/api/workspace/ws-1/workers': jsonOk([]),
  '/api/workspace/ws-1/containers': jsonOk({ containers: [] }),
  '/api/workspace/ws-1/effective_permissions': jsonOk({
    effective_permissions: {
      git: 'read',
      filesystem: 'read',
      container: true,
      network: 'banned',
      mcp: 'banned',
      host_bash: 'banned',
    },
  }),
  '/api/workspace/list': jsonOk([{ id: 'ws-1', label: 'Code Development', root: '/root' }]),
};

const stubBackend = () => {
  const fetchMock = vi.fn(async (url) => {
    const key = Object.keys(DEFAULT_ROUTES)
      .filter((k) => String(url).includes(k))
      .sort((a, b) => b.length - a.length)[0];
    return DEFAULT_ROUTES[key] || DEFAULT_FALLBACK;
  });
  vi.stubGlobal('fetch', fetchMock);
};

const BASE_CONFIG = { mode: 'custom', session_permissions: { network: 'banned' } };

const panelElement = (config, sendCommand) => (
  <ConfigPanel
    config={config}
    sendCommand={sendCommand}
    providers={[]}
    availableTools={[]}
    wsConnected
    workspaceId="ws-1"
    sessionId="s1"
  />
);

const renderPanel = (config = BASE_CONFIG) => {
  const sendCommand = vi.fn();
  const utils = render(panelElement(config, sendCommand));
  return { sendCommand, ...utils };
};

const openPermissionsTab = () => {
  fireEvent.click(screen.getByRole('button', { name: 'Permissions' }));
};

// Permissions-tab select order (DOM order: Git, Filesystem, Network, MCP, Host Bash;
// Container is a checkbox, not a select).
const TAB_RESOURCE_ORDER = ['git', 'filesystem', 'network', 'mcp', 'host_bash'];

beforeEach(() => {
  useStore.getState().reset();
  stubBackend();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  useStore.getState().reset();
});

describe('SESSION_RESOURCE_VOCAB (frontend mirror of backend)', () => {
  it('mirrors the backend permission vocabulary (thoughtmachine/security.py, security/security_gate.py) exactly', () => {
    expect(SESSION_RESOURCE_VOCAB).toEqual({
      git: ['banned', 'ask', 'read', 'write', 'write_on_feature_branch'],
      filesystem: ['banned', 'read', 'write'],
      container: [true, false],
      network: ['banned', 'ask', 'write', 'outbound'],
      mcp: ['banned', 'connect', 'full'],
      host_bash: ['banned', 'ask', 'allow'],
    });
  });

  it('keeps container vocabulary as booleans (not strings)', () => {
    expect(SESSION_RESOURCE_VOCAB.container.every((v) => typeof v === 'boolean')).toBe(true);
    expect(SESSION_RESOURCE_VOCAB.container.map(String)).toEqual(['true', 'false']);
  });
});

describe('PERMISSION_RANK_ORDER (frontend mirror of backend)', () => {
  it('mirrors the backend permission ranks (thoughtmachine/security.py, security/security_gate.py) exactly', () => {
    expect(PERMISSION_RANK_ORDER).toEqual({
      banned: 0,
      none: 1,
      ask: 1.5,
      read: 2,
      outbound: 2.5,
      write: 3,
      write_on_feature_branch: 3,
      full: 4,
    });
  });
});

describe('PILL_COLORS / getPill', () => {
  it('has the exact Catppuccin pill map for every key', () => {
    expect(PILL_COLORS).toEqual({
      full: { bg: '#a6e3a1', fg: '#1e1e2e', label: 'Full' },
      write: { bg: '#a6e3a1', fg: '#1e1e2e', label: 'Write' },
      write_on_feature_branch: { bg: '#a6e3a1', fg: '#1e1e2e', label: 'Feature Branch' },
      read: { bg: '#89b4fa', fg: '#1e1e2e', label: 'Read' },
      ask: { bg: '#f9e2af', fg: '#1e1e2e', label: 'Ask' },
      banned: { bg: '#f38ba8', fg: '#1e1e2e', label: 'Banned' },
      true: { bg: '#a6e3a1', fg: '#1e1e2e', label: 'Enabled' },
      false: { bg: '#f38ba8', fg: '#1e1e2e', label: 'Disabled' },
    });
  });

  it('resolves known keys, boolean keys, and unknown-key fallback', () => {
    expect(getPill('write')).toEqual(PILL_COLORS.write);
    expect(getPill('full')).toEqual(PILL_COLORS.full);
    expect(getPill('write_on_feature_branch')).toEqual(PILL_COLORS.write_on_feature_branch);
    expect(getPill(true)).toEqual(PILL_COLORS.true);
    expect(getPill(false)).toEqual(PILL_COLORS.false);
    expect(getPill('unknown')).toEqual({ bg: '#6c7086', fg: '#cdd6f4', label: 'unknown' });
    // values with no dedicated pill entry fall back with their stringified key
    expect(getPill('outbound')).toEqual({ bg: '#6c7086', fg: '#cdd6f4', label: 'outbound' });
  });
});

describe('ConfigPanel permission option drift guard', () => {
  it('renders exactly 5 permission selects (Git, Filesystem, Network, MCP, Host Bash)', () => {
    renderPanel();
    openPermissionsTab();
    const selects = Array.from(document.querySelectorAll('select'));
    expect(selects).toHaveLength(TAB_RESOURCE_ORDER.length);
  });

  it('renders only option values from the canonical vocab, with none missing', () => {
    renderPanel();
    openPermissionsTab();
    const selects = Array.from(document.querySelectorAll('select'));
    expect(selects).toHaveLength(TAB_RESOURCE_ORDER.length);

    selects.forEach((select, i) => {
      const resource = TAB_RESOURCE_ORDER[i];
      const renderedValues = Array.from(select.options).map((o) => o.value);
      const vocabValues = SESSION_RESOURCE_VOCAB[resource].map(String);
      // every rendered option is a canonical value
      expect(renderedValues.every((v) => vocabValues.includes(v))).toBe(true);
      // and every canonical value is rendered (no drift like a missing 'outbound')
      expect([...renderedValues].sort()).toEqual([...vocabValues].sort());
    });
  });

  it('keeps the intended per-resource option ordering', () => {
    renderPanel();
    openPermissionsTab();
    const selects = Array.from(document.querySelectorAll('select'));
    const optionValues = (i) => Array.from(selects[i].options).map((o) => o.value);

    // permissive-first
    expect(optionValues(0)).toEqual(['write', 'write_on_feature_branch', 'read', 'ask', 'banned']); // git
    expect(optionValues(1)).toEqual(['write', 'read', 'banned']); // filesystem
    expect(optionValues(2)).toEqual(['write', 'outbound', 'ask', 'banned']); // network (includes outbound)
    expect(optionValues(3)).toEqual(['full', 'connect', 'banned']); // mcp
    expect(optionValues(4)).toEqual(['allow', 'ask', 'banned']); // host_bash
  });

  it('capitalises rendered option labels', () => {
    renderPanel();
    openPermissionsTab();
    const selects = Array.from(document.querySelectorAll('select'));
    const labels = Array.from(selects[2].options).map((o) => o.textContent.trim());
    expect(labels).toEqual(['Write', 'Outbound', 'Ask', 'Banned']);
    const gitLabels = Array.from(selects[0].options).map((o) => o.textContent.trim());
    expect(gitLabels).toEqual(['Write', 'Write on feature branches', 'Read', 'Ask', 'Banned']);
  });
});
