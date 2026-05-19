import React, { useState, useEffect, useMemo } from 'react';

const ConfigPanel = ({ config, sendCommand, providers, availableTools, panelWidth, wsConnected }) => {
  const getSafeDraft = (cfg) => ({
    temperature: cfg?.temperature,
    max_turns: cfg?.max_turns,
    provider: cfg?.provider,
    provider_id: cfg?.provider_id,
    model: cfg?.model,
    system_prompt: cfg?.system_prompt ?? '',
    tools: cfg?.tools ?? [],
    max_tokens: cfg?.max_tokens,
    context_length: cfg?.context_length,
  });

  const [activeTab, setActiveTab] = useState('general');

  const [draft, setDraft] = useState(getSafeDraft(config));

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
            <label style={labelStyle}><strong>Max Tokens</strong> <span style={{ color: '#6c7086', fontSize: '0.75rem' }}>(leave empty for model default)</span></label>
            <input
              type="number" min="1"
              placeholder="Unlimited"
              value={draft.max_tokens ?? ''}
              onChange={(e) => setDraft({ ...draft, max_tokens: e.target.value === '' ? undefined : parseInt(e.target.value, 10) })}
              style={inputStyle}
            />
          </div>

          <div style={{ marginBottom: '1rem' }}>
            <label style={labelStyle}><strong>Context Length</strong> <span style={{ color: '#6c7086', fontSize: '0.75rem' }}>(leave empty for model default)</span></label>
            <input
              type="number" min="1"
              placeholder="Default"
              value={draft.context_length ?? ''}
              onChange={(e) => setDraft({ ...draft, context_length: e.target.value === '' ? undefined : parseInt(e.target.value, 10) })}
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
          <p style={{ color: '#6c7086', fontStyle: 'italic', fontSize: '0.85rem' }}>
            Advanced settings coming soon.
          </p>
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

export default ConfigPanel;
