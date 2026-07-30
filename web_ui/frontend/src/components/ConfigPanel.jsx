import React, { useState, useEffect, useMemo, useCallback } from 'react';
import ManageProvidersModal from './ManageProvidersModal';
import ContainerPanelContent from './ContainerPanel';
import WorkspacePanel from './WorkspacePanel';
import PromptLibrary from './PromptLibrary';

const BACKEND_PORT = import.meta.env.VITE_BACKEND_PORT || '8000';
const API_BASE = `http://${window.location.hostname}:${BACKEND_PORT}`;

function ConfigPanel({ mode = null, config, sendCommand, providers, availableTools, panelWidth, wsConnected, defaultConfigSaveStatus, defaultConfigSaveMessage, onClearDefaultSaveStatus, workspaceId, sessionId, containerRebuildResult, onClearRebuildResult, selectedWorker, onSelectWorker, isActive, activeSessionId, onClearWorker }) {
  const [defaultSaved, setDefaultSaved] = useState(false);  // false | 'pending' | true | 'error'
  const [showManageProviders, setShowManageProviders] = useState(false);
  const [providerVersion, setProviderVersion] = useState(0);  // incremented when a provider is saved
  const [allTools, setAllTools] = useState([]);
  const normalizeSessionPermissions = (permissions) => {
    const normalized = {
      filesystem: 'read',
      network: 'banned',
      container: false,
      system: 'read',
      git: 'read',
      execution: 'banned',
      ...(permissions ?? {}),
    };

    // Backward compatibility: old configs/sessions stored network as boolean.
    if (typeof normalized.network === 'boolean') {
      normalized.network = normalized.network ? 'write' : 'banned';
    }

    return normalized;
  };

  const getSafeDraft = (cfg) => ({
    mode: cfg?.mode ?? null,
    temperature: cfg?.temperature ?? 0.7,
    max_turns: cfg?.max_turns ?? 10,
    provider: cfg?.provider,
    provider_id: cfg?.provider_id,
    model: cfg?.model,
    system_prompt: cfg?.system_prompt ?? '',
    tools: cfg?.tools ?? [],
    session_permissions: normalizeSessionPermissions(cfg?.session_permissions),

    token_monitor_warning_threshold: cfg?.token_monitor_warning_threshold ?? 35000,
    token_monitor_critical_threshold: cfg?.token_monitor_critical_threshold ?? 50000,
    workspace_path: cfg?.workspace_path ?? '',
    tool_output_token_limit: cfg?.tool_output_token_limit ?? 10000,
  });

  const [activeTab, setActiveTab] = useState('workspace');
  const [draft, setDraft] = useState(getSafeDraft(config));

  // ── Directory browser state ────────────────────────────────────────
  // ── Dirty tracking & apply feedback ────────────────────────────────
  const [lastAppliedConfig, setLastAppliedConfig] = useState(null);
  const [isApplying, setIsApplying] = useState(false);
  const [applyError, setApplyError] = useState(null);

  // ── Sync defaultConfigSaveStatus from backend into local UI state ────
  useEffect(() => {
    if (defaultConfigSaveStatus === 'ok') {
      setDefaultSaved(true);
      const t = setTimeout(() => {
        setDefaultSaved(false);
        onClearDefaultSaveStatus?.();
      }, 2500);
      return () => clearTimeout(t);
    } else if (defaultConfigSaveStatus === 'error') {
      setDefaultSaved('error');
      const t = setTimeout(() => {
        setDefaultSaved(false);
        onClearDefaultSaveStatus?.();
      }, 4000);
      return () => clearTimeout(t);
    }
  }, [defaultConfigSaveStatus, onClearDefaultSaveStatus]);

  useEffect(() => {
    setDraft(getSafeDraft(config));
    setLastAppliedConfig(getSafeDraft(config));
    // Clear applying state when config arrives (apply succeeded)
    if (isApplying) {
      setIsApplying(false);
    }
  }, [config]);

  // Apply error timeout: if no config change after 6s, show error
  const applyTimeoutRef = React.useRef(null);
  useEffect(() => {
    if (isApplying) {
      applyTimeoutRef.current = setTimeout(() => {
        setIsApplying(false);
        setApplyError('Apply timed out — check server connection');
      }, 6000);
    }
    return () => {
      if (applyTimeoutRef.current) {
        clearTimeout(applyTimeoutRef.current);
        applyTimeoutRef.current = null;
      }
    };
  }, [isApplying]);

  // Auto-clear error after 5 seconds
  useEffect(() => {
    if (applyError) {
      const t = setTimeout(() => setApplyError(null), 5000);
      return () => clearTimeout(t);
    }
  }, [applyError]);

  // Fetch the complete list of all available tools from the backend
  useEffect(() => {
    fetch(`${API_BASE}/api/tools`)
      .then(res => res.json())
      .then(data => {
        if (data.tools) setAllTools(data.tools);
      })
      .catch(() => {
        // Ignore — tools tab will just show whatever the session config provides
      });
  }, []);

  // ── Derived: selected provider object ──────────────────────────────
  // Backend sends 'provider' in config_changed; fall back if 'provider_id' not set.
  const activeProviderId = draft.provider_id || draft.provider || '';
  const selectedProvider = providers.find((p) => p.id === activeProviderId);
  const availableModels = useMemo(() => {
    if (!selectedProvider) return []
    const models = selectedProvider.models || []
    const defaultModel = selectedProvider.default_model
    const combined = defaultModel ? [defaultModel, ...models] : models
    return [...new Set(combined)] // deduplicate
  }, [selectedProvider])

  // ── Dirty detection ────────────────────────────────────────────────
  const isDirty = useMemo(() => {
    if (!lastAppliedConfig || !draft) return false;
    return JSON.stringify(draft) !== JSON.stringify(lastAppliedConfig);
  }, [draft, lastAppliedConfig]);

  const handleProviderChange = (e) => {
    const providerId = e.target.value
    const provider = providers.find((p) => p.id === providerId)
    // Reset model when provider changes; prefer default_model
    setDraft({
      ...draft,
      provider_id: providerId,
      model: provider?.default_model || (provider?.models?.[0]) || '',
    })
  }

  const handleModelChange = (e) => {
    setDraft({ ...draft, model: e.target.value })
  }

  // ── Load prompt from library and switch to system_prompt tab ──
  const handleLoadPromptFromLibrary = useCallback(async (promptName) => {
    if (!promptName) return;
    try {
      const res = await fetch(`${API_BASE}/api/prompts/${promptName}`);
      if (!res.ok) return;
      const text = await res.text();
      setDraft(prev => ({ ...prev, system_prompt: text }));
      setActiveTab('system_prompt');
    } catch (e) {
      // silent
    }
  }, []);

  if (!config) {
    return (
      <div style={{ padding: '1rem', fontFamily: 'sans-serif', background: '#313244', color: '#cdd6f4', width: panelWidth || 280, minWidth: 200, maxWidth: 500, flexShrink: 0, overflowY: 'auto', height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        Loading config...
      </div>
    );
  }

  const tabStyle = (tab) => ({
    background: activeTab === tab ? '#45475a' : 'transparent',
    color: activeTab === tab ? '#cdd6f4' : '#6c7086',
    border: 'none',
    borderRadius: '4px',
    padding: '0.3rem 0.6rem',
    cursor: 'pointer',
    fontWeight: activeTab === tab ? 600 : 400,
    fontSize: '0.8rem',
  });

  const inputStyle = {
    width: '100%',
    marginTop: '0.25rem',
    background: '#1e1e2e',
    color: '#cdd6f4',
    border: '1px solid #45475a',
    borderRadius: '4px',
    padding: '0.3rem',
    boxSizing: 'border-box',
  };

  const labelStyle = {
    display: 'block',
    marginBottom: '0.25rem',
    fontSize: '0.85rem',
    color: '#a6adc8',
  };

  // Fallback: use config?.mode when the mode prop is null (e.g. on session load)
  const effectiveMode = mode || config?.mode || null;
  const isModeLocked = effectiveMode && effectiveMode !== 'custom'

  const TAB_KEYS = ['workspace', 'permissions', 'system_prompt', 'general', 'model', 'tools', 'container', 'advanced'];
  const TAB_LABELS = { workspace: 'Workspace', permissions: 'Permissions', system_prompt: 'Prompt', general: 'General', model: 'Model', tools: 'Tools', container: 'Container', advanced: 'Advanced' };

  const modeBadge = effectiveMode === 'agent' ? 'Agent' : effectiveMode === 'engineer' ? 'Engineer' : effectiveMode === 'custom' ? 'Custom' : null
  const modeBadgeColor = effectiveMode === 'agent' ? '#89b4fa' : effectiveMode === 'engineer' ? '#a6e3a1' : effectiveMode === 'custom' ? '#f9e2af' : '#6c7086'

  return (
    <div style={{ padding: '1rem', fontFamily: 'sans-serif', background: '#313244', color: '#cdd6f4', width: panelWidth || 280, minWidth: 200, maxWidth: 500, flexShrink: 0, overflowY: 'auto', height: '100%' }}>
      {/* Mode badge */}
      {modeBadge && (
        <div style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: '0.3rem',
          marginBottom: '0.5rem',
          padding: '0.2rem 0.5rem',
          borderRadius: '4px',
          background: modeBadgeColor + '22',
          border: `1px solid ${modeBadgeColor}`,
          color: modeBadgeColor,
          fontSize: '0.78rem',
          fontWeight: 600,
        }}>
          {modeBadge}
          {isModeLocked && <span style={{ marginLeft: '0.15rem', opacity: 0.7, fontSize: '0.7rem' }}>(locked)</span>}
        </div>
      )}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.75rem' }}>
        <h3 style={{ margin: 0 }}>Config</h3>
        <button
          className="btn btn-accent"
          style={{ fontSize: '0.8rem', padding: '0.25rem 0.75rem' }}
          onClick={() => {
            sendCommand('set_default_config', { config: draft });
            setDefaultSaved('pending');
          }}
        >
          {defaultSaved === 'pending' ? 'Saving…' : defaultSaved === 'error' ? '✗ Save failed' : defaultSaved === true ? '✓ Default saved!' : 'Save as Default'}
        </button>
      </div>

      {/* Tab bar */}
      <div style={{ display: 'flex', gap: '0.25rem', marginBottom: '1rem', borderBottom: '1px solid #45475a', paddingBottom: '0.5rem' }}>
        {TAB_KEYS.map((tab) => (
          <button key={tab} onClick={() => setActiveTab(tab)} style={tabStyle(tab)}>
            {TAB_LABELS[tab]}
          </button>
        ))}
      </div>

      {/* ── Workspace Tab ── */}
      {activeTab === 'workspace' && (
        <div>
          {/* ── Workspace Path (read-only) ── */}
          <div style={{ marginBottom: '1rem' }}>
            <label style={labelStyle}><strong>Workspace Path</strong></label>
            <div style={{
              marginTop: '0.3rem',
              padding: '0.4rem 0.6rem',
              background: '#1e1e2e',
              borderRadius: '4px',
              color: '#cdd6f4',
              fontSize: '0.85rem',
              fontFamily: 'monospace',
              wordBreak: 'break-all',
            }}>
              {draft.workspace_path
                ? draft.workspace_path
                : <span style={{ color: '#f38ba8', fontWeight: 'bold' }}>⚠️ No workspace — session is unbound. Set a workspace.</span>}
            </div>
          </div>

          <WorkspacePanel workspaceId={workspaceId} sessionId={sessionId} selectedWorker={selectedWorker} onSelectWorker={onSelectWorker} isActive={isActive} />
        </div>
      )}

      {/* ── General Tab ──────────────────────────────────────────────── */}
      {activeTab === 'general' && (
        <div>


          <div style={{ marginBottom: '1rem' }}>
            <label style={labelStyle}><strong>Temperature:</strong> {draft.temperature}</label>
            <input
              type="range" min="0" max="2" step="0.1"
              value={draft.temperature}
              onChange={(e) => setDraft({ ...draft, temperature: parseFloat(e.target.value) })}
              style={{ width: '100%' }}
            />
          </div>

          <div style={{ marginBottom: '1rem' }}>
            <label style={labelStyle}><strong>Max Turns</strong></label>
            <input
              type="number" min="1" max="100"
              value={draft.max_turns}
              onChange={(e) => setDraft({ ...draft, max_turns: parseInt(e.target.value, 10) || 1 })}
              style={inputStyle}
            />
          </div>



          <div style={{ marginBottom: '1rem' }}>
            <label style={labelStyle}><strong>Critical Threshold</strong> <span style={{ color: '#6c7086', fontSize: '0.75rem' }}>(tokens, warning is 15k below)</span></label>
            <input
              type="number" min="0"
              value={draft.token_monitor_critical_threshold}
              onChange={(e) => {
                const critical = parseInt(e.target.value, 10) || 0;
                setDraft({
                  ...draft,
                  token_monitor_critical_threshold: critical,
                  token_monitor_warning_threshold: Math.max(critical - 15000, 0),
                });
              }}
              style={inputStyle}
            />
          </div>
        </div>
      )}

      {/* ── Model Tab ────────────────────────────────────────────────── */}
      {activeTab === 'model' && (
        <div>
          <div style={{ marginBottom: '1rem' }}>
            <label style={labelStyle}><strong>Provider</strong></label>
            <select
              value={activeProviderId}
              onChange={handleProviderChange}
              style={inputStyle}
            >
              <option value="" style={{ background: '#1e1e2e', color: '#cdd6f4' }}>-- Select provider --</option>
              {providers.map((p) => (
                <option key={p.id} value={p.id} style={{ background: '#1e1e2e', color: '#cdd6f4' }}>
                  {p.label}
                </option>
              ))}
            </select>
          </div>

          <div style={{ marginBottom: '1rem' }}>
            <label style={labelStyle}><strong>Model</strong></label>
            <select
              value={draft.model || ''}
              onChange={handleModelChange}
              disabled={!selectedProvider}
              style={{
                ...inputStyle,
                opacity: selectedProvider ? 1 : 0.5,
              }}
            >
              <option value="" style={{ background: '#1e1e2e', color: '#cdd6f4' }}>-- Select model --</option>
              {availableModels.map((m) => (
                <option key={m} value={m} style={{ background: '#1e1e2e', color: '#cdd6f4' }}>
                  {m}
                </option>
              ))}
            </select>
          </div>

          {/* Manage Providers button */}
          <div style={{ marginTop: '0.75rem' }}>
            <button
              onClick={() => setShowManageProviders(true)}
              style={{
                background: '#45475a',
                color: '#89b4fa',
                border: '1px solid #89b4fa',
                borderRadius: '4px',
                padding: '0.35rem 0.75rem',
                cursor: 'pointer',
                fontSize: '0.8rem',
                fontWeight: 500,
                width: '100%',
              }}
            >⚙ Manage Providers...</button>
          </div>

          {/* Manage Providers Modal */}
          {showManageProviders && (
            <ManageProvidersModal
              providers={providers}
              sendCommand={sendCommand}
              onClose={() => setShowManageProviders(false)}
              onProviderSaved={() => setProviderVersion(v => v + 1)}
            />
          )}
        </div>
      )}

      {/* ── Tools Tab ────────────────────────────────────────────────── */}
      {activeTab === 'tools' && (
        <div>
          {isModeLocked && (
            <div style={{
              background: 'rgba(249,226,175,0.1)',
              border: '1px solid rgba(249,226,175,0.3)',
              borderRadius: '4px',
              padding: '0.5rem 0.6rem',
              marginBottom: '0.75rem',
              color: '#f9e2af',
              fontSize: '0.8rem',
            }}>
              Tools are locked in {effectiveMode === 'agent' ? 'Agent' : 'Engineer'} mode.
              Switch to Custom mode to enable tool configuration.
            </div>
          )}
          {/* ── Tool Output Token Limit (above tool checkboxes) ───── */}
          <div style={{ marginBottom: '1rem' }}>
            <label style={labelStyle} htmlFor="tool_output_token_limit">
              <strong>Tool Output Token Limit</strong>
            </label>
            <input
              id="tool_output_token_limit"
              type="number"
              min="0"
              step="100"
              disabled={isModeLocked}
              style={{
                ...inputStyle,
                width: '100%',
                marginTop: '0.3rem',
                opacity: isModeLocked ? 0.5 : 1,
              }}
              value={draft.tool_output_token_limit ?? 10000}
              onChange={(e) => {
                const val = e.target.value === '' ? null : parseInt(e.target.value, 10);
                setDraft({ ...draft, tool_output_token_limit: val });
              }}
              placeholder="Default: 10000"
            />
            <small style={{ color: '#6c7086', fontSize: '0.75rem', marginTop: '0.25rem', display: 'block' }}>
              Maximum tokens per tool output. 0 or empty = no limit.
            </small>
          </div>
          <div style={{ borderTop: '1px solid #45475a', paddingTop: '0.75rem' }}>
            <label style={labelStyle}><strong>Tools ({allTools.length} total)</strong></label>
            {allTools.length > 0 ? (
              allTools.map((toolName) => {
                const toolConfig = draft.tools?.find(t => t.name === toolName);
                const enabled = toolConfig ? toolConfig.enabled : false;
                return (
                  <div key={toolName} style={{ marginBottom: '0.35rem' }}>
                    <label style={{ cursor: isModeLocked ? 'default' : 'pointer', display: 'flex', alignItems: 'center', gap: '0.4rem', fontSize: '0.85rem', opacity: isModeLocked ? 0.6 : 1 }}>
                      <input
                        type="checkbox"
                        checked={enabled}
                        disabled={isModeLocked}
                        onChange={(e) => {
                          const updatedTools = draft.tools?.map(t =>
                            t.name === toolName ? { ...t, enabled: e.target.checked } : t
                          ) || [];
                          if (!updatedTools.find(t => t.name === toolName)) {
                            updatedTools.push({ name: toolName, enabled: e.target.checked });
                          }
                          setDraft({ ...draft, tools: updatedTools });
                        }}
                      />
                      {toolName}
                      {isModeLocked && <span style={{ color: '#6c7086', fontSize: '0.7rem', marginLeft: '0.25rem' }}>(read-only)</span>}
                    </label>
                  </div>
                );
              })
            ) : (
              <div style={{ color: '#6c7086', fontSize: '0.8rem', fontStyle: 'italic', padding: '0.5rem 0' }}>
                Loading tool list...
              </div>
            )}
          </div>
        </div>
      )}

      {/* ── Permissions Tab ───────────────────────────────────────────── */}
      {activeTab === 'permissions' && (
        <div>
          <div style={{ marginBottom: '1rem' }}>
            <label style={labelStyle}><strong>Filesystem</strong></label>
            <select
              value={draft.session_permissions?.filesystem ?? 'read'}
              onChange={(e) => setDraft({
                ...draft,
                session_permissions: { ...draft.session_permissions, filesystem: e.target.value }
              })}
              style={inputStyle}
            >
              <option value="full" style={{ background: '#1e1e2e', color: '#cdd6f4' }}>Full</option>
              <option value="write" style={{ background: '#1e1e2e', color: '#cdd6f4' }}>Write</option>
              <option value="read" style={{ background: '#1e1e2e', color: '#cdd6f4' }}>Read</option>
              <option value="ask" style={{ background: '#1e1e2e', color: '#cdd6f4' }}>Ask</option>
              <option value="banned" style={{ background: '#1e1e2e', color: '#cdd6f4' }}>Banned</option>
            </select>
            <small style={{ color: '#6c7086', fontSize: '0.75rem', marginTop: '0.25rem', display: 'block' }}>
              Read/write access to the workspace filesystem. "Ask" prompts for approval on each write.
            </small>
          </div>

          <div style={{ marginBottom: '1rem' }}>
            <label style={labelStyle}><strong>Network</strong></label>
            <select
              value={draft.session_permissions?.network ?? 'banned'}
              onChange={(e) => setDraft({
                ...draft,
                session_permissions: { ...draft.session_permissions, network: e.target.value }
              })}
              style={inputStyle}
            >
              <option value="write" style={{ background: '#1e1e2e', color: '#cdd6f4' }}>Write</option>
              <option value="ask" style={{ background: '#1e1e2e', color: '#cdd6f4' }}>Ask</option>
              <option value="banned" style={{ background: '#1e1e2e', color: '#cdd6f4' }}>Banned</option>
            </select>
            <small style={{ color: '#6c7086', fontSize: '0.75rem', marginTop: '0.25rem', display: 'block' }}>
              Allow the agent to make network requests.
            </small>
          </div>

          <div style={{ marginBottom: '1rem' }}>
            <label style={labelStyle}><strong>Container</strong></label>
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.6rem', marginTop: '0.3rem' }}>
              <label className="toggle-switch">
                <input
                  type="checkbox"
                  checked={draft.session_permissions?.container ?? false}
                  onChange={(e) => setDraft({
                    ...draft,
                    session_permissions: { ...draft.session_permissions, container: e.target.checked }
                  })}
                />
                <span className="toggle-slider"></span>
              </label>
              <span style={{ fontSize: '0.85rem', color: draft.session_permissions?.container ? '#a6e3a1' : '#f38ba8' }}>
                {draft.session_permissions?.container ? 'Enabled' : 'Disabled'}
              </span>
            </div>
            <small style={{ color: '#6c7086', fontSize: '0.75rem', marginTop: '0.25rem', display: 'block' }}>
              Allow container operations.
            </small>
          </div>

          <div style={{ marginBottom: '1rem' }}>
            <label style={labelStyle}><strong>Git</strong></label>
            <select
              value={draft.session_permissions?.git ?? 'read'}
              onChange={(e) => setDraft({
                ...draft,
                session_permissions: { ...draft.session_permissions, git: e.target.value }
              })}
              style={inputStyle}
            >
              <option value="full" style={{ background: '#1e1e2e', color: '#cdd6f4' }}>Full</option>
              <option value="write" style={{ background: '#1e1e2e', color: '#cdd6f4' }}>Write</option>
              <option value="read" style={{ background: '#1e1e2e', color: '#cdd6f4' }}>Read</option>
              <option value="banned" style={{ background: '#1e1e2e', color: '#cdd6f4' }}>Banned</option>
              <option value="ask" style={{ background: '#1e1e2e', color: '#cdd6f4' }}>Ask</option>
            </select>
            <small style={{ color: '#6c7086', fontSize: '0.75rem', marginTop: '0.25rem', display: 'block' }}>
              Access level for Git operations. "Ask" prompts for approval on each write operation (commit, push, pull, etc.).
            </small>
          </div>

          <div style={{ marginBottom: '1rem' }}>
            <label style={labelStyle}><strong>System</strong></label>
            <select
              value={draft.session_permissions?.system ?? 'read'}
              onChange={(e) => setDraft({
                ...draft,
                session_permissions: { ...draft.session_permissions, system: e.target.value }
              })}
              style={inputStyle}
            >
              <option value="full" style={{ background: '#1e1e2e', color: '#cdd6f4' }}>Full</option>
              <option value="write" style={{ background: '#1e1e2e', color: '#cdd6f4' }}>Write</option>
              <option value="read" style={{ background: '#1e1e2e', color: '#cdd6f4' }}>Read</option>
              <option value="ask" style={{ background: '#1e1e2e', color: '#cdd6f4' }}>Ask</option>
              <option value="banned" style={{ background: '#1e1e2e', color: '#cdd6f4' }}>Banned</option>
            </select>
            <small style={{ color: '#6c7086', fontSize: '0.75rem', marginTop: '0.25rem', display: 'block' }}>
              Controls access to system-level operations (environment inspection, process management). "Ask" prompts for approval on each write operation.
            </small>
          </div>

          <div style={{ marginBottom: '1rem' }}>
            <label style={labelStyle}><strong>Worker Access</strong></label>
            <select
              value={draft.session_permissions?.execution ?? 'banned'}
              onChange={(e) => setDraft({
                ...draft,
                session_permissions: { ...draft.session_permissions, execution: e.target.value }
              })}
              style={inputStyle}
            >
              <option value="full" style={{ background: '#1e1e2e', color: '#cdd6f4' }}>Full</option>
              <option value="write" style={{ background: '#1e1e2e', color: '#cdd6f4' }}>Write</option>
              <option value="read" style={{ background: '#1e1e2e', color: '#cdd6f4' }}>Read</option>
              <option value="ask" style={{ background: '#1e1e2e', color: '#cdd6f4' }}>Ask</option>
              <option value="banned" style={{ background: '#1e1e2e', color: '#cdd6f4' }}>Banned</option>
            </select>
            <small style={{ color: '#6c7086', fontSize: '0.75rem', marginTop: '0.25rem', display: 'block' }}>
              Allow the agent to spawn background/child processes (experimental).
            </small>
          </div>

          <p style={{ color: '#6c7086', fontSize: '0.8rem', fontStyle: 'italic', borderTop: '1px solid #45475a', paddingTop: '0.75rem' }}>
            Changes take effect on the next tool call. No restart required.
          </p>
        </div>
      )}

      {/* ── Container Tab ─────────────────────────────────────────────── */}
      {activeTab === 'container' && (
        <ContainerPanelContent
          workspacePath={config?.workspace_path || ''}
          sendCommand={sendCommand}
          containerRebuildResult={containerRebuildResult}
          onClearRebuildResult={onClearRebuildResult}
        />
      )}

      {/* ── System Prompt Tab (merged with library) ───────────────────────────────── */}
      {activeTab === 'system_prompt' && (
        <div>
          {isModeLocked ? (
            /* ── Locked modes: factory prompt preview (read-only) ── */
            <>
              {/* Factory prompt badge */}
              <div style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: '0.3rem',
                marginBottom: '0.5rem',
                padding: '0.2rem 0.5rem',
                borderRadius: '4px',
                background: effectiveMode === 'agent' ? 'rgba(137,180,250,0.15)' : 'rgba(166,227,161,0.15)',
                border: `1px solid ${effectiveMode === 'agent' ? '#89b4fa' : '#a6e3a1'}`,
                color: effectiveMode === 'agent' ? '#89b4fa' : '#a6e3a1',
                fontSize: '0.75rem',
                fontWeight: 600,
              }}>
                {effectiveMode === 'agent' ? 'Agent' : 'Engineer'} Factory Prompt
                <span style={{ marginLeft: '0.15rem', opacity: 0.6, fontSize: '0.7rem' }}>(locked)</span>
              </div>
              {/* Read-only preview */}
              <div style={{ marginBottom: '1rem' }}>
                <label style={labelStyle}><strong>System Prompt</strong> <span style={{ color: '#6c7086', fontSize: '0.7rem' }}>(read-only — factory default for this mode)</span></label>
                <div style={{
                  ...inputStyle,
                  fontFamily: 'monospace',
                  fontSize: '0.8rem',
                  lineHeight: '1.4',
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                  maxHeight: '300px',
                  overflowY: 'auto',
                  padding: '0.5rem',
                  cursor: 'default',
                  opacity: 0.75,
                }}>
                  {draft.system_prompt || <span style={{ color: '#6c7086', fontStyle: 'italic' }}>No factory prompt loaded</span>}
                </div>
              </div>
              {/* Prompt Library — visible but marked read-only */}
              <div style={{ borderTop: '1px solid #45475a', paddingTop: '0.75rem', marginTop: '0.75rem' }}>
                <label style={labelStyle}><strong>Prompt Library</strong></label>
                <p style={{ color: '#6c7086', fontSize: '0.75rem', fontStyle: 'italic', marginTop: '0.25rem', marginBottom: '0.5rem' }}>
                  Browse prompts — switch to Custom mode to apply them.
                </p>
                <div style={{ marginTop: '0.35rem', opacity: 0.6, pointerEvents: 'none' }}>
                  <PromptLibrary onSelectPrompt={handleLoadPromptFromLibrary} />
                </div>
              </div>
            </>
          ) : (
            /* ── Custom mode: editable textarea + library ── */
            <>
              <div style={{ marginBottom: '1rem' }}>
                <label style={labelStyle}><strong>System Prompt</strong></label>
                <textarea
                  rows={6}
                  style={{ ...inputStyle, fontFamily: 'monospace', resize: 'vertical' }}
                  value={draft.system_prompt || ''}
                  onChange={(e) => setDraft({ ...draft, system_prompt: e.target.value })}
                  placeholder="Optional system-level instructions for the agent..."
                />
              </div>
              {/* Prompt Library section — editable in Custom mode */}
              <div style={{ borderTop: '1px solid #45475a', paddingTop: '0.75rem', marginTop: '0.75rem' }}>
                <label style={labelStyle}><strong>Prompt Library</strong></label>
                <div style={{ marginTop: '0.35rem' }}>
                  <PromptLibrary onSelectPrompt={handleLoadPromptFromLibrary} />
                </div>
              </div>
            </>
          )}
        </div>
      )}

      {/* ── Advanced Tab ──────────────────────────────────────────────── */}
      {activeTab === 'advanced' && (
        <div>
          <p style={{ color: '#6c7086', fontSize: '0.85rem', fontStyle: 'italic' }}>No advanced options at this time.</p>
        </div>
      )}

      {/* ── Apply Button ─────────────────────────────────────────────── */}
      <div style={{ marginTop: '1rem', display: 'flex', flexDirection: 'column', gap: '0.4rem' }}>
        <button
          onClick={() => {
            setIsApplying(true);
            setApplyError(null);
            setProviderVersion(0);
            // Just send apply_config — the backend detects workspace_path changes
            // and handles the full project switch internally.
            sendCommand('apply_config', { config: draft });
          }}
          disabled={!wsConnected || isApplying}
          style={{
            background: !wsConnected ? '#585b70' : isApplying ? '#585b70' : (isDirty || providerVersion > 0) ? '#89b4fa' : '#45475a',
            color: !wsConnected || (!isDirty && !isApplying && providerVersion === 0) ? '#6c7086' : '#1e1e2e',
            border: 'none',
            borderRadius: '4px',
            padding: '0.5rem 1.5rem',
            fontWeight: 600,
            cursor: !wsConnected || isApplying ? 'not-allowed' : 'pointer',
            width: '100%',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            gap: '0.4rem',
          }}
        >
          {isApplying && <span className="config-spinner" />}
          {isApplying ? 'Applying…' : 'Apply'}
        </button>
        {!wsConnected && (
          <span style={{ color: '#f9e2af', fontSize: '0.8rem', fontWeight: 500 }}>
            ⚠ Reconnecting...
          </span>
        )}
        {(isDirty || providerVersion > 0) && !isApplying && wsConnected && (
          <span style={{ color: '#f9e2af', fontSize: '0.75rem', fontStyle: 'italic' }}>
            {isDirty ? 'Unsaved changes' : 'Provider credentials updated'}
          </span>
        )}
        {applyError && (
          <span style={{ color: '#f38ba8', fontSize: '0.8rem' }}>
            ⚠ {applyError}
          </span>
        )}
      </div>
    </div>
  );
};

export default React.memo(ConfigPanel)
