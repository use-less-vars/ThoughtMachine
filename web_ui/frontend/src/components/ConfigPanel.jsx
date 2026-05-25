import React, { useState, useEffect, useMemo, useCallback } from 'react';

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

  const itemStyle = (isDir) => ({
    padding: '0.3rem 0.5rem',
    cursor: isDir ? 'pointer' : 'default',
    borderRadius: '4px',
    display: 'flex',
    alignItems: 'center',
    gap: '0.4rem',
    fontSize: '0.85rem',
    color: isDir ? '#89b4fa' : '#cdd6f4',
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
      <div style={{ marginTop: '0.1rem', marginBottom: '0.4rem', display: 'flex', gap: '0.3rem' }}>
        {path && path !== '/' && (
          <button onClick={goUp} style={{
            background: '#45475a', color: '#cdd6f4', border: '1px solid #585b70',
            borderRadius: '4px', padding: '0.2rem 0.5rem', cursor: 'pointer', fontSize: '0.75rem'
          }}>↑ Parent</button>
        )}
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
            style={itemStyle(true)}
            onClick={() => {
              const newPath = path.replace(/\\/g, '/').replace(/\/$/, '') + '/' + entry.name;
              onNavigate(newPath);
            }}
            onMouseEnter={(e) => { e.target.style.background = '#45475a'; }}
            onMouseLeave={(e) => { e.target.style.background = 'transparent'; }}
          >
            <span>📁</span>
            <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{entry.name}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}


function ConfigPanel({ config, sendCommand, providers, availableTools, panelWidth, wsConnected }) {
  const getSafeDraft = (cfg) => ({
    temperature: cfg?.temperature ?? 0.7,
    max_turns: cfg?.max_turns ?? 10,
    provider: cfg?.provider,
    provider_id: cfg?.provider_id,
    model: cfg?.model,
    system_prompt: cfg?.system_prompt ?? '',
    tools: cfg?.tools ?? [],

    token_monitor_warning_threshold: cfg?.token_monitor_warning_threshold ?? 35000,
    token_monitor_critical_threshold: cfg?.token_monitor_critical_threshold ?? 50000,
    workspace_path: cfg?.workspace_path ?? '',
  });

  const [activeTab, setActiveTab] = useState('general');
  const [draft, setDraft] = useState(getSafeDraft(config));

  // ── Directory browser state ────────────────────────────────────────
  const [browserOpen, setBrowserOpen] = useState(false);
  const [browserPath, setBrowserPath] = useState('');
  const [browserEntries, setBrowserEntries] = useState([]);
  const [browserLoading, setBrowserLoading] = useState(false);
  const [browserError, setBrowserError] = useState('');

  useEffect(() => {
    setDraft(getSafeDraft(config));
  }, [config]);

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

  const TAB_KEYS = ['general', 'model', 'tools', 'system_prompt', 'advanced'];
  const TAB_LABELS = { general: 'General', model: 'Model', tools: 'Tools', system_prompt: 'Prompt', advanced: 'Advanced' };

  return (
    <div style={{ padding: '1rem', fontFamily: 'sans-serif', background: '#313244', color: '#cdd6f4', width: panelWidth || 280, minWidth: 200, maxWidth: 500, flexShrink: 0, overflowY: 'auto', height: '100%' }}>
      <h3 style={{ marginTop: 0, marginBottom: '0.75rem' }}>Config</h3>

      {/* Tab bar */}
      <div style={{ display: 'flex', gap: '0.25rem', marginBottom: '1rem', borderBottom: '1px solid #45475a', paddingBottom: '0.5rem' }}>
        {TAB_KEYS.map((tab) => (
          <button key={tab} onClick={() => setActiveTab(tab)} style={tabStyle(tab)}>
            {TAB_LABELS[tab]}
          </button>
        ))}
      </div>

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
                onClick={() => {
                  setBrowserPath(draft.workspace_path || '');
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
        </div>
      )}

      {/* ── Tools Tab ────────────────────────────────────────────────── */}
      {activeTab === 'tools' && (
        <div>
          <div style={{ marginBottom: '1rem' }}>
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
              onNavigate={(newPath) => {
                setBrowserPath(newPath);
                setBrowserError('');
              }}
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
      <div style={{ marginTop: '1rem', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
        <button
          onClick={() => sendCommand('apply_config', { config: draft })}
          disabled={!wsConnected}
          style={{
            background: wsConnected ? '#89b4fa' : '#585b70',
            color: wsConnected ? '#1e1e2e' : '#a6adc8',
            border: 'none',
            borderRadius: '4px',
            padding: '0.5rem 1.5rem',
            fontWeight: 600,
            cursor: wsConnected ? 'pointer' : 'not-allowed',
            width: '100%',
          }}
        >Apply</button>
        {!wsConnected && (
          <span style={{ color: '#f9e2af', fontSize: '0.8rem', fontWeight: 500 }}>
            ⚠ Reconnecting...
          </span>
        )}
      </div>
    </div>
  );
};

export default React.memo(ConfigPanel)
