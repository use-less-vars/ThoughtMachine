// @vitest-environment jsdom
/*
 * ConfigPanel.test.jsx — Phase 0 Frontend Truthfulness Sprint
 *
 * jsdom + @testing-library/react.  Covers:
 *   - loading placeholder (config === null)
 *   - header / save-as-default button
 *   - all 8 tab buttons
 *   - /api/tools fetch on mount (global fetch is mocked)
 *   - Permissions tab: 5 selects in order, PERMISSION_DEFAULTS values when
 *     session_permissions is missing, container checkbox, legacy boolean
 *     network normalization (true → 'write', false → 'banned')
 *   - set_default_config flow + pending/saved button states
 *   - apply_config flow + disabled state when ws is disconnected
 *   - mode badge with (locked) hint
 *   - Tools tab: tool count, checkbox checked state, empty list placeholder
 *
 * Note: ConfigPanel has no `test` config in vite.config.js, so the jsdom
 * environment is declared per-file via the docblock above; RTL cleanup and
 * jest-dom matchers are imported explicitly (no globals).
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, waitFor, act } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import ConfigPanel from '../ConfigPanel';
import { PERMISSION_DEFAULTS } from '../../store/useStore';

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

function renderPanel(props = {}) {
  const sendCommand = vi.fn();
  const onClearDefaultSaveStatus = vi.fn();
  const utils = render(
    <ConfigPanel
      config={baseConfig}
      sendCommand={sendCommand}
      providers={[]}
      availableTools={[]}
      wsConnected
      defaultConfigSaveStatus={null}
      onClearDefaultSaveStatus={onClearDefaultSaveStatus}
      {...props}
    />
  );
  return { sendCommand, onClearDefaultSaveStatus, ...utils };
}

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({
      ok: true,
      json: async () => ({ tools: [] }),
      text: async () => '',
    }))
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ==========================================================================
// Loading placeholder
// ==========================================================================
describe('loading placeholder', () => {
  it('renders "Loading config..." when config is null', () => {
    render(
      <ConfigPanel
        config={null}
        sendCommand={vi.fn()}
        providers={[]}
        availableTools={[]}
        wsConnected
        defaultConfigSaveStatus={null}
      />
    );
    expect(screen.getByText('Loading config...')).toBeInTheDocument();
  });
});

// ==========================================================================
// Header & save-as-default
// ==========================================================================
describe('header', () => {
  it('renders the Config heading and Save as Default button', () => {
    renderPanel();
    expect(screen.getByRole('heading', { name: 'Config' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save as Default' })).toBeInTheDocument();
  }, 20000);

  it('renders all 8 tab buttons', () => {
    renderPanel();
    const labels = ['Workspace', 'Permissions', 'Prompt', 'General', 'Model', 'Tools', 'Container', 'Advanced'];
    for (const label of labels) {
      expect(screen.getByRole('button', { name: label })).toBeInTheDocument();
    }
  }, 20000);
});

// ==========================================================================
// Backend tool fetch
// ==========================================================================
describe('backend tool fetch', () => {
  it('fetches the tool list from /api/tools on mount', async () => {
    renderPanel();
    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalled();
    });
    const url = String(globalThis.fetch.mock.calls[0][0]);
    expect(url).toEqual(expect.stringContaining('/api/tools'));
  });
});

// ==========================================================================
// Permissions tab
// ==========================================================================
describe('Permissions tab', () => {
  it('shows PERMISSION_DEFAULTS values when session_permissions is missing', () => {
    const { container } = renderPanel({ config: { ...baseConfig, session_permissions: undefined } });
    fireEvent.click(screen.getByRole('button', { name: 'Permissions' }));
    const selects = container.querySelectorAll('select');
    expect(selects).toHaveLength(5);
    // Order must be: Filesystem, Network, Git, System, Execution
    expect(selects[0].value).toBe(PERMISSION_DEFAULTS.filesystem); // 'read'
    expect(selects[1].value).toBe(PERMISSION_DEFAULTS.network);   // 'banned'
    expect(selects[2].value).toBe(PERMISSION_DEFAULTS.git);       // 'read'
    expect(selects[3].value).toBe(PERMISSION_DEFAULTS.system);    // 'read'
    expect(selects[4].value).toBe(PERMISSION_DEFAULTS.execution); // 'banned'
    const checkbox = container.querySelector('input[type="checkbox"]');
    expect(checkbox).not.toBeChecked();
  });

  it('keeps explicit session_permissions values (filesystem write)', () => {
    const { container } = renderPanel();
    fireEvent.click(screen.getByRole('button', { name: 'Permissions' }));
    const selects = container.querySelectorAll('select');
    expect(selects[0].value).toBe('write');
  });

  it('normalizes legacy boolean network permission true → write', () => {
    const { container } = renderPanel({ config: { ...baseConfig, session_permissions: { network: true } } });
    fireEvent.click(screen.getByRole('button', { name: 'Permissions' }));
    const selects = container.querySelectorAll('select');
    expect(selects[1].value).toBe('write');
  });

  it('normalizes legacy boolean network permission false → banned', () => {
    const { container } = renderPanel({ config: { ...baseConfig, session_permissions: { network: false } } });
    fireEvent.click(screen.getByRole('button', { name: 'Permissions' }));
    const selects = container.querySelectorAll('select');
    expect(selects[1].value).toBe('banned');
  });
});

// ==========================================================================
// Save as Default flow
// ==========================================================================
describe('Save as Default', () => {
  it('sends set_default_config and shows the pending state', () => {
    const { sendCommand } = renderPanel();
    fireEvent.click(screen.getByRole('button', { name: 'Save as Default' }));
    expect(sendCommand).toHaveBeenCalledWith(
      'set_default_config',
      expect.objectContaining({ config: expect.any(Object) })
    );
    expect(screen.getByRole('button', { name: 'Saving…' })).toBeInTheDocument();
  });

  it('shows the success state when the backend confirms', () => {
    renderPanel({ defaultConfigSaveStatus: 'ok' });
    expect(screen.getByRole('button', { name: '✓ Default saved!' })).toBeInTheDocument();
  });

  it('shows the error state when the backend reports an error', () => {
    renderPanel({ defaultConfigSaveStatus: 'error' });
    expect(screen.getByRole('button', { name: '✗ Save failed' })).toBeInTheDocument();
  });
});

// ==========================================================================
// Apply button
// ==========================================================================
describe('Apply button', () => {
  it('is disabled with a reconnecting hint when the ws is disconnected', () => {
    renderPanel({ wsConnected: false });
    expect(screen.getByRole('button', { name: 'Apply' })).toBeDisabled();
    expect(screen.getByText(/Reconnecting/)).toBeInTheDocument();
  });

  it('sends apply_config and shows the applying state when connected', () => {
    const { sendCommand } = renderPanel();
    const applyButton = screen.getByRole('button', { name: 'Apply' });
    expect(applyButton).toBeEnabled();
    fireEvent.click(applyButton);
    expect(sendCommand).toHaveBeenCalledWith(
      'apply_config',
      expect.objectContaining({ config: expect.any(Object) })
    );
    expect(screen.getByRole('button', { name: 'Applying…' })).toBeInTheDocument();
  });

  it('shows the queued label and disarms the 6s timeout while the apply is queued', () => {
    vi.useFakeTimers();
    try {
      const utils = renderPanel();
      fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
      expect(utils.sendCommand).toHaveBeenCalledWith('apply_config', expect.anything());
      // Backend ACKs config_queued → queued label, timeout effect disarmed.
      utils.rerender(
        <ConfigPanel
          config={baseConfig}
          sendCommand={utils.sendCommand}
          providers={[]}
          availableTools={[]}
          wsConnected
          defaultConfigSaveStatus={null}
          onClearDefaultSaveStatus={utils.onClearDefaultSaveStatus}
          configQueued
        />
      );
      expect(
        screen.getByRole('button', { name: 'Queued — applying when idle…' })
      ).toBeInTheDocument();
      // 7s pass — NO false 'Apply timed out' error while queued.
      act(() => {
        vi.advanceTimersByTime(7000);
      });
      expect(screen.queryByText(/Apply timed out/)).not.toBeInTheDocument();
      expect(
        screen.getByRole('button', { name: 'Queued — applying when idle…' })
      ).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it('surfaces a server-reported apply failure immediately', () => {
    renderPanel({ applyFailed: '⚠ Failed to apply config: boom' });
    expect(screen.getByText(/Failed to apply config: boom/)).toBeInTheDocument();
    // Applying state is cleared → the button is enabled again.
    expect(screen.getByRole('button', { name: 'Apply' })).toBeEnabled();
  });
});

// ==========================================================================
// Mode badge
// ==========================================================================
describe('mode badge', () => {
  it('shows the mode badge with the locked hint for agent mode', () => {
    renderPanel({ mode: 'agent', config: { ...baseConfig, mode: 'agent' } });
    expect(screen.getByText('Agent')).toBeInTheDocument();
    expect(screen.getByText('(locked)')).toBeInTheDocument();
  });

  it('shows the mode badge without the locked hint for custom mode', () => {
    renderPanel();
    expect(screen.getByText('Custom')).toBeInTheDocument();
    expect(screen.queryByText('(locked)')).not.toBeInTheDocument();
  });
});

// ==========================================================================
// Tools tab
// ==========================================================================
describe('Tools tab', () => {
  it('renders available tools with the correct checked state', () => {
    renderPanel({ availableTools: ['read_file', 'write_file'] });
    fireEvent.click(screen.getByRole('button', { name: 'Tools' }));
    expect(screen.getByText('Tools (2 total)')).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: 'read_file' })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'write_file' })).not.toBeChecked();
  });

  it('shows the loading placeholder when no tools are available', () => {
    renderPanel({ availableTools: [] });
    fireEvent.click(screen.getByRole('button', { name: 'Tools' }));
    expect(screen.getByText('Loading tool list...')).toBeInTheDocument();
  });
});
