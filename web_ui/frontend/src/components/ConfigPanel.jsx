import React, { useState, useEffect, useMemo } from 'react';

const ConfigPanel = ({ config, sendCommand, providers, availableTools, panelWidth }) => {
  const getSafeDraft = (cfg) => ({
    temperature: cfg?.temperature ?? 0.7,
    max_turns: cfg?.max_turns ?? 20,
    provider: cfg?.provider ?? 'openai',
    provider_id: cfg?.provider_id ?? null,
    model: cfg?.model ?? null,
    system_prompt: cfg?.system_prompt ?? '',
    tools: cfg?.tools ?? [],
  });

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

  return (
    <div style={{ padding: '1rem', fontFamily: 'sans-serif', background: '#313244', color: '#cdd6f4', width: panelWidth || 280, minWidth: 200, maxWidth: 500, flexShrink: 0, overflowY: 'auto', height: '100%' }}>
      <h3>Config</h3>

      {/* Provider */}
      <div style={{ marginBottom: '1rem' }}>
        <label>
          <strong>Provider:</strong>
        </label>
        <select
          value={activeProviderId}
          onChange={handleProviderChange}
          style={{ width: '100%', marginTop: '0.25rem', background: '#1e1e2e', color: '#cdd6f4', border: '1px solid #45475a', borderRadius: '4px', padding: '0.3rem' }}
        >
          <option value="" style={{ background: '#1e1e2e', color: '#cdd6f4' }}>-- Select provider --</option>
          {providers.map((p) => (
            <option key={p.id} value={p.id}>
              {p.label}
            </option>
          ))}
        </select>
      </div>

      {/* Model */}
      <div style={{ marginBottom: '1rem' }}>
        <label>
          <strong>Model:</strong>
        </label>
        <select
          value={draft.model || ''}
          onChange={handleModelChange}
          disabled={!selectedProvider}
          style={{ width: '100%', marginTop: '0.25rem', background: '#1e1e2e', color: '#cdd6f4', border: '1px solid #45475a', borderRadius: '4px', padding: '0.3rem' }}
        >
          <option value="" style={{ background: '#1e1e2e', color: '#cdd6f4' }}>-- Select model --</option>
          {availableModels.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
      </div>

      <div style={{ marginBottom: '1rem' }}>
        <label>
          <strong>Temperature:</strong> {draft.temperature}
        </label>
        <input
          type="range"
          min="0"
          max="2"
          step="0.1"
          value={draft.temperature}
          onChange={(e) => setDraft({ ...draft, temperature: parseFloat(e.target.value) })}
          style={{ width: '100%' }}
        />
      </div>

      <div style={{ marginBottom: '1rem' }}>
        <label>
          <strong>Max turns:</strong>
        </label>
        <input
          type="number"
          min="1"
          max="100"
          value={draft.max_turns}
          onChange={(e) => setDraft({ ...draft, max_turns: parseInt(e.target.value, 10) || 1 })}
          style={{ width: '80px', marginLeft: '0.5rem', background: '#1e1e2e', color: '#cdd6f4', border: '1px solid #45475a', borderRadius: '4px', padding: '0.3rem' }}
        />
      </div>

      {/* Tools */}
      <div style={{ marginBottom: '1rem' }}>
        <strong>Tools:</strong>
        {availableTools.map((tool) => {
          const toolName = tool.name || tool;
          const toolConfig = draft.tools?.find(t => t.name === toolName);
          const enabled = toolConfig ? toolConfig.enabled : false;
          return (
            <div key={toolName}>
              <label>
                <input
                  type="checkbox"
                  checked={enabled}
                  onChange={(e) => {
                    const updatedTools = draft.tools?.map(t =>
                      t.name === toolName ? { ...t, enabled: e.target.checked } : t
                    ) || [];
                    // If the tool wasn't in the list at all, add it
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

      {/* System Prompt */}
      <div style={{ marginBottom: '1rem' }}>
        <strong>System Prompt:</strong>
        <textarea
          rows={3}
          style={{ width: '100%', marginTop: '0.25rem', background: '#1e1e2e', color: '#cdd6f4', border: '1px solid #45475a', borderRadius: '4px', padding: '0.3rem', fontFamily: 'monospace' }}
          value={draft.system_prompt || ''}
          onChange={(e) => setDraft({ ...draft, system_prompt: e.target.value })}
          placeholder="Optional system-level instructions for the agent..."
        />
      </div>

      <div>
        <button onClick={() => sendCommand('apply_config', { config: draft })}
          style={{ background: '#89b4fa', color: '#1e1e2e', border: 'none', borderRadius: '4px', padding: '0.5rem 1.5rem', fontWeight: 600, cursor: 'pointer' }}
        >Apply</button>
      </div>
    </div>
  );
};

export default ConfigPanel;
