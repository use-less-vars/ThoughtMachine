import React, { useState, useEffect, useMemo, useCallback } from 'react';
import ManageProvidersModal from './ManageProvidersModal';
import ContainerPanelContent from './ContainerPanel';
import WorkspacePanel from './WorkspacePanel';

// ── Directory Browser sub-component ──────────────────────────────────────
function DirectoryBrowser({ path, entries, loading, error, onNavigate, onSelect, setLoading, setEntries, setError }) {
  const fetchDir = useCallback(async (dirPath) => {
    setLoading(true);
    setError('');
    try {
      const url = `http://${window.location.hostname}:8000/api/browse?path=${encodeURIComponent(dirPath || '')}`;
      const res = await fetch(url);
      const data = await res.json();
      if (data.success) {
        setEntries(data.entries || []);
      } else {
        setError(data.error || 'Failed to list directory');
        setEntries([]);
      }
    } catch (err) {
      setError('Network error: ' + err.message);
      setEntries([]);
    } finally {
      setLoading(false);
    }
  }, [setLoading, setEntries, setError]);

  const [newFolderName, setNewFolderName] = useState('');
  const [hoveredFolder, setHoveredFolder] = useState(null);
  const [creating, setCreating] = useState(false);

  const createFolder = useCallback(async () => {
    const name = newFolderName.trim();
    if (!name) return;
    setCreating(true);
    try {
      const url = `http://${window.location.hostname}:8000/api/browse/create`;
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ parent_path: path, name }),
      });
      const data = await res.json();
      if (data.success) {
        setNewFolderName('');
        fetchDir(path);
      } else {
        setError(data.error || 'Failed to create directory');
      }
    } catch (err) {
      setError('Network error: ' + err.message);
    } finally {
      setCreating(false);
    }
  }, [newFolderName, path, fetchDir, setError]);

  useEffect(() => {
    fetchDir(path);
  }, [path, fetchDir]);

  const goUp = () => {
    const parts = path.replace(/\\/g, '/').replace(/\/$/, '').split('/');
    parts.pop();
    const parent = parts.join('/') || '/';
    onNavigate(parent);
  };

  const listStyle = {
    listStyle: 'none',
    margin: 0,
    padding: 0,
    overflowY: 'auto',
    flex: 1,
    minHeight: 0,
  };

  const itemStyle = (isDir, isHovered) => ({
    padding: '0.3rem 0.5rem',
    cursor: isDir ? 'pointer' : 'default',
    borderRadius: '4px',
    display: 'flex',
    alignItems: 'center',
    gap: '0.4rem',
    fontSize: '0.85rem',
    color: isDir ? '#89b4fa' : '#cdd6f4',
    background: isHovered ? '#45475a' : 'transparent',
    transition: 'background 0.15s',
  });

  if (loading) {
    return <div style={{ ...listStyle, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#a6adc8' }}>Loading...</div>;
  }

  if (error) {
    return (
      <div style={{ ...listStyle, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: '0.5rem' }}>
        <span style={{ color: '#f38ba8', fontSize: '0.85rem' }}>{error}</span>
        <button onClick={() => fetchDir(path)} style={{
          background: '#45475a', color: '#cdd6f4', border: '1px solid #585b70',
          borderRadius: '4px', padding: '0.3rem 0.8rem', cursor: 'pointer', fontSize: '0.8rem'
        }}>Retry</button>
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
      <div style={{ marginTop: '0.1rem', marginBottom: '0.4rem', display: 'flex', gap: '0.3rem', flexWrap: 'wrap' }}>
        {path && path !== '/' && (
          <button onClick={goUp} style={{
            background: '#45475a', color: '#cdd6f4', border: '1px solid #585b70',
            borderRadius: '4px', padding: '0.2rem 0.5rem', cursor: 'pointer', fontSize: '0.75rem'
          }}>↑ Parent</button>
        )}
        <div style={{ display: 'flex', gap: '0.3rem', alignItems: 'center' }}>
          <input
            type="text"
            value={newFolderName}
            onChange={(e) => setNewFolderName(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') createFolder(); }}
            placeholder="new folder"
            style={{
              background: '#1e1e2e', color: '#cdd6f4', border: '1px solid #585b70',
              borderRadius: '4px', padding: '0.2rem 0.4rem', fontSize: '0.75rem',
              width: '90px', outline: 'none',
            }}
          />
          <button onClick={createFolder} disabled={creating || !newFolderName.trim()} style={{
            background: creating ? '#585b70' : '#45475a',
            color: creating || !newFolderName.trim() ? '#6c7086' : '#a6e3a1',
            border: '1px solid #585b70',
            borderRadius: '4px', padding: '0.2rem 0.4rem',
            cursor: creating || !newFolderName.trim() ? 'not-allowed' : 'pointer',
            fontSize: '0.75rem', whiteSpace: 'nowrap',
          }}>{creating ? '…' : '+ Folder'}</button>
        </div>
        <button onClick={() => onSelect(path)} style={{
          background: '#89b4fa', color: '#1e1e2e', border: 'none',
          borderRadius: '4px', padding: '0.2rem 0.5rem', cursor: 'pointer', fontWeight: 600, fontSize: '0.75rem',
          marginLeft: 'auto',
        }}>Select This Folder</button>
      </div>
      <ul style={listStyle}>
        {entries.filter(e => e.is_dir).length === 0 && (
          <li style={{ padding: '0.5rem', color: '#6c7086', fontSize: '0.8rem', textAlign: 'center' }}>
            (no subdirectories)
          </li>
        )}
        {entries.filter(e => e.is_dir).map((entry) => (
          <li
            key={entry.name}
            onClick={() => {
              const newPath = path.replace(/\\/g, '/').replace(/\/$/, '') + '/' + entry.name;
              onNavigate(newPath);
            }}
            onMouseEnter={() => setHoveredFolder(entry.name)}
            onMouseLeave={() => setHoveredFolder(null)}
            style={itemStyle(true, hoveredFolder === entry.name)}
          >
            <span>📁</span>
            <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{entry.name}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}


function ConfigPanel({ config, sendCommand, providers, availableTools, panelWidth, wsConnected, defaultConfigSaveStatus, defaultConfigSaveMessage, onClearDefaultSaveStatus, workspaceId, sessionId, containerRebuildResult, onClearRebuildResult }) {
  const [defaultSaved, setDefaultSaved] = useState(false);  // false | 'pending' | true | 'error'
  const [showManageProviders, setShowManageProviders] = useState(false);
  const [providerVersion, setProviderVersion] = useState(0);  // incremented when a provider is saved
  const normalizeSessionPermissions = (permissions) => {
    const normalized = {
      filesystem: 'read',
      network: 'banned',
      container: false,
      security: 'read',
      git: 'write',
      execution: 'banned',
      system: true,
      ...(permissions ?? {}),
    };

    // Backward compatibility: old configs/sessions stored network as boolean.
    if (typeof normalized.network === 'boolean') {
      normalized.network = normalized.network ? 'write' : 'banned';
    }

    return normalized;
  };

  const getSafeDraft = (cfg) => ({
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

  const [activeTab, setActiveTab] = useState('general');
  const [draft, setDraft] = useState(getSafeDraft(config));

  // ── Directory browser state ────────────────────────────────────────
  // ── Dirty tracking & apply feedback ────────────────────────────────
  const [lastAppliedConfig, setLastAppliedConfig] = useState(null);
  const [isApplying, setIsApplying] = useState(false);
  const [applyError, setApplyError] = useState(null);

  const [browserOpen, setBrowserOpen] = useState(false);
  const [browserPath, setBrowserPath] = useState('');
  const [browserEntries, setBrowserEntries] = useState([]);
  const [browserLoading, setBrowserLoading] = useState(false);
  const [browserError, setBrowserError] = useState('');
  const handleBrowseNavigate = useCallback((newPath) => {
    setBrowserPath(newPath);
    setBrowserError('');
  }, []);

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

  const TAB_KEYS = ['general', 'model', 'tools', 'permissions', 'container', 'workspace', 'system_prompt', 'advanced'];  // container tab placeholder
  const TAB_LABELS = { workspace: 'Workspace', general: 'General', model: 'Model', tools: 'Tools', permissions: 'Permissions', container: 'Container', system_prompt: 'Prompt', advanced: 'Advanced' };

  return (
    <div style={{ padding: '1rem', fontFamily: 'sans-serif', background: '#313244', color: '#cdd6f4', width: panelWidth || 280, minWidth: 200, maxWidth: 500, flexShrink: 0, overflowY: 'auto', height: '100%' }}>
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

      {/* ── Workspace Tab ──────────────────────────────────────────── */}
      {activeTab === 'workspace' && (
        <WorkspacePanel workspaceId={workspaceId} sessionId={sessionId} />
      )}

      {/* ── General Tab ──────────────────────────────────────────────── */}
      {activeTab === 'general' && (
        <div>
          {/* ── Workspace (first) ── */}
          <div style={{ marginBottom: '1rem' }}>
            <label style={labelStyle}><strong>Workspace</strong></label>
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginTop: '0.3rem' }}>
              {draft.workspace_path ? (
                <span style={{ color: '#cdd6f4', fontSize: '0.9rem' }}>
                  📁 {draft.workspace_path.split('/').filter(Boolean).pop() || '/'}
                </span>
              ) : (
                <span style={{ color: '#6c7086', fontSize: '0.85rem', fontStyle: 'italic' }}>
                  No workspace selected
                </span>
              )}
              <button
                onClick={async () => {
                  let initialPath = draft.workspace_path;
                  if (!initialPath) {
                    try {
                      const res = await fetch(`http://${window.location.hostname}:8000/api/browse?path=`);
                      const data = await res.json();
                      if (data.success && data.current_path) {
                        initialPath = data.current_path;
                      }
                    } catch (_) {}
                  }
                  setBrowserPath(initialPath || '/');
                  setBrowserOpen(true);
                  setBrowserError('');
                }}
                style={{
                  background: '#45475a',
                  color: '#cdd6f4',
                  border: '1px solid #585b70',
                  borderRadius: '4px',
                  padding: '0.3rem 0.6rem',
                  cursor: 'pointer',
                  fontWeight: 600,
                  fontSize: '0.8rem',
                  whiteSpace: 'nowrap',
                  marginLeft: 'auto',
                }}
              >Browse</button>
            </div>
          </div>

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
              style={{
                ...inputStyle,
                width: '100%',
                marginTop: '0.3rem',
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
            <label style={labelStyle}><strong>Tools</strong></label>
            {availableTools.map((tool) => {
              const toolName = tool.name || tool;
              const toolConfig = draft.tools?.find(t => t.name === toolName);
              const enabled = toolConfig ? toolConfig.enabled : false;
              return (
                <div key={toolName} style={{ marginBottom: '0.35rem' }}>
                  <label style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: '0.4rem', fontSize: '0.85rem' }}>
                    <input
                      type="checkbox"
                      checked={enabled}
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
                  </label>
                </div>
              );
            })}
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
              Allow container execution of code.
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
            <label style={labelStyle}><strong>Execution</strong></label>
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

      {/* ── System Prompt Tab ──────────────────────────────────────────── */}
      {activeTab === 'system_prompt' && (
        <div>
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
        </div>
      )}

      {/* ── Advanced Tab ──────────────────────────────────────────────── */}
      {activeTab === 'advanced' && (
        <div>
          <p style={{ color: '#6c7086', fontSize: '0.85rem', fontStyle: 'italic' }}>No advanced options at this time.</p>
        </div>
      )}

      {/* ── Directory Browser Overlay ────────────────────────────────── */}
      {browserOpen && (
        <div style={{
          position: 'fixed',
          top: 0, left: 0, right: 0, bottom: 0,
          background: 'rgba(0,0,0,0.6)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          zIndex: 1000,
        }} onClick={() => setBrowserOpen(false)}>
          <div style={{
            background: '#313244',
            border: '1px solid #585b70',
            borderRadius: '8px',
            padding: '1rem',
            width: '400px',
            maxHeight: '60vh',
            display: 'flex',
            flexDirection: 'column',
            boxShadow: '0 4px 20px rgba(0,0,0,0.4)',
          }} onClick={(e) => e.stopPropagation()}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.5rem' }}>
              <strong style={{ fontSize: '0.9rem' }}>Select Directory</strong>
              <button onClick={() => setBrowserOpen(false)} style={{
                background: 'none', border: 'none', color: '#a6adc8', cursor: 'pointer', fontSize: '1.2rem'
              }}>✕</button>
            </div>
            <div style={{
              background: '#1e1e2e',
              borderRadius: '4px',
              padding: '0.65rem 0.7rem',
              marginBottom: '0.75rem',
              fontSize: '0.9rem',
              color: '#cdd6f4',
              fontFamily: 'monospace',
              lineHeight: '1.5',
              flexShrink: 0,
              overflowX: 'auto',
              whiteSpace: 'pre',
              wordBreak: 'keep-all',
            }}>{browserPath || '~'}</div>
            <DirectoryBrowser
              path={browserPath}
              entries={browserEntries}
              loading={browserLoading}
              error={browserError}
              onNavigate={handleBrowseNavigate}
              onSelect={(selectedPath) => {
                setDraft({ ...draft, workspace_path: selectedPath });
                setBrowserOpen(false);
              }}
              setLoading={setBrowserLoading}
              setEntries={setBrowserEntries}
              setError={setBrowserError}
            />
          </div>
        </div>
      )}

      {/* ── Apply Button ─────────────────────────────────────────────── */}
      <div style={{ marginTop: '1rem', display: 'flex', flexDirection: 'column', gap: '0.4rem' }}>
        <button
          onClick={() => {
            setIsApplying(true);
            setApplyError(null);
            setProviderVersion(0);
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
