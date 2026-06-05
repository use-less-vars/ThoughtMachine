/*
 * ProviderEditModal.jsx
 *
 * Modal form for adding/editing a single provider profile.
 * Props:
 *   provider  — existing provider object (null for "add new")
 *   onSave    — called with the provider data object
 *   onCancel  — called when modal is dismissed
 */
import React, { useState, useEffect } from 'react';

const MODAL_BACKDROP = {
  position: 'fixed',
  top: 0, left: 0, right: 0, bottom: 0,
  background: 'rgba(0,0,0,0.6)',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  zIndex: 1100,
};

const MODAL_STYLE = {
  background: '#313244',
  border: '1px solid #585b70',
  borderRadius: '8px',
  padding: '1.25rem',
  width: '480px',
  maxHeight: '80vh',
  display: 'flex',
  flexDirection: 'column',
  boxShadow: '0 4px 20px rgba(0,0,0,0.4)',
  overflowY: 'auto',
};

const LABEL_STYLE = {
  display: 'block',
  fontSize: '0.8rem',
  color: '#a6adc8',
  marginBottom: '0.25rem',
  fontWeight: 500,
};

const INPUT_STYLE = {
  width: '100%',
  padding: '0.45rem 0.55rem',
  borderRadius: '4px',
  border: '1px solid #585b70',
  background: '#1e1e2e',
  color: '#cdd6f4',
  fontSize: '0.85rem',
  boxSizing: 'border-box',
};

const FIELD_GAP = { marginBottom: '0.75rem' };

function ProviderEditModal({ provider, onSave, onCancel }) {
  const isNew = !provider;
  const [id, setId] = useState('');
  const [label, setLabel] = useState('');
  const [providerType, setProviderType] = useState('openai_compatible');
  const [baseUrl, setBaseUrl] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [defaultModel, setDefaultModel] = useState('');
  const [modelsText, setModelsText] = useState('');
  const [timeout, setTimeout_] = useState(120);
  const [error, setError] = useState('');
  const [showApiKey, setShowApiKey] = useState(false);

  useEffect(() => {
    if (provider) {
      setId(provider.id || '');
      setLabel(provider.label || '');
      setProviderType(provider.provider_type || 'openai_compatible');
      setBaseUrl(provider.base_url || '');
      setApiKey(provider.api_key || '');
      setDefaultModel(provider.default_model || '');
      setModelsText((provider.models || []).join('\n'));
      setTimeout_(provider.timeout ?? 120);
    } else {
      setId('');
      setLabel('');
      setProviderType('openai_compatible');
      setBaseUrl('');
      setApiKey('');
      setDefaultModel('');
      setModelsText('');
      setTimeout_(120);
    }
    setError('');
  }, [provider]);

  const handleSubmit = () => {
    // Validate
    if (!id.trim()) {
      setError('Provider ID is required');
      return;
    }
    if (!label.trim()) {
      setError('Label is required');
      return;
    }
    if (!baseUrl.trim()) {
      setError('Base URL is required');
      return;
    }

    const models = modelsText
      .split('\n')
      .map((m) => m.trim())
      .filter((m) => m.length > 0);

    onSave({
      id: id.trim(),
      label: label.trim(),
      provider_type: providerType,
      base_url: baseUrl.trim(),
      api_key: apiKey,
      default_model: defaultModel.trim(),
      models,
      timeout: parseInt(timeout, 10) || 120,
    });
  };

  return (
    <div style={MODAL_BACKDROP} onClick={onCancel}>
      <div style={MODAL_STYLE} onClick={(e) => e.stopPropagation()}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem' }}>
          <strong style={{ fontSize: '0.95rem' }}>{isNew ? 'Add Provider' : 'Edit Provider'}</strong>
          <button onClick={onCancel} style={{
            background: 'none', border: 'none', color: '#a6adc8', cursor: 'pointer', fontSize: '1.2rem'
          }}>✕</button>
        </div>

        {error && (
          <div style={{
            background: '#f38ba8', color: '#1e1e2e', padding: '0.4rem 0.6rem',
            borderRadius: '4px', fontSize: '0.8rem', marginBottom: '0.75rem'
          }}>{error}</div>
        )}

        {/* ID (only editable for new providers) */}
        <div style={FIELD_GAP}>
          <label style={LABEL_STYLE}>ID</label>
          <input
            style={{ ...INPUT_STYLE, opacity: isNew ? 1 : 0.6 }}
            value={id}
            onChange={(e) => setId(e.target.value)}
            placeholder="e.g., openai, my-custom-vllm"
            disabled={!isNew}
          />
        </div>

        {/* Label */}
        <div style={FIELD_GAP}>
          <label style={LABEL_STYLE}>Label</label>
          <input
            style={INPUT_STYLE}
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder="e.g., OpenAI GPT-4, My Local vLLM"
          />
        </div>

        {/* Provider Type */}
        <div style={FIELD_GAP}>
          <label style={LABEL_STYLE}>Type</label>
          <select
            style={INPUT_STYLE}
            value={providerType}
            onChange={(e) => setProviderType(e.target.value)}
          >
            <option value="openai_compatible">OpenAI Compatible</option>
            <option value="openai">OpenAI</option>
            <option value="anthropic">Anthropic</option>
            <option value="google">Google</option>
            <option value="custom">Custom</option>
          </select>
        </div>

        {/* Base URL */}
        <div style={FIELD_GAP}>
          <label style={LABEL_STYLE}>Base URL</label>
          <input
            style={INPUT_STYLE}
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder="https://api.openai.com/v1"
          />
        </div>

        {/* API Key */}
        <div style={FIELD_GAP}>
          <label style={LABEL_STYLE}>API Key</label>
          <div style={{ display: 'flex', gap: '0.25rem' }}>
            <input
              type={showApiKey ? 'text' : 'password'}
              style={{ ...INPUT_STYLE, flex: 1 }}
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="sk-..."
            />
            <button
              type="button"
              onClick={() => setShowApiKey((s) => !s)}
              title={showApiKey ? 'Hide API key' : 'Show API key'}
              style={{
                background: '#45475a',
                border: '1px solid #585b70',
                borderRadius: '4px',
                color: '#a6adc8',
                cursor: 'pointer',
                fontSize: '0.9rem',
                padding: '0.45rem 0.6rem',
                lineHeight: 1,
              }}
            >
              {showApiKey ? (
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/>
                  <path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/>
                  <line x1="1" y1="1" x2="23" y2="23"/>
                </svg>
              ) : (
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
                  <circle cx="12" cy="12" r="3"/>
                </svg>
              )}
            </button>
          </div>
        </div>

        {/* Default Model */}
        <div style={FIELD_GAP}>
          <label style={LABEL_STYLE}>Default Model</label>
          <input
            style={INPUT_STYLE}
            value={defaultModel}
            onChange={(e) => setDefaultModel(e.target.value)}
            placeholder="gpt-4o"
          />
        </div>

        {/* Models */}
        <div style={FIELD_GAP}>
          <label style={LABEL_STYLE}>Models <span style={{ color: '#6c7086', fontSize: '0.7rem' }}>(one per line)</span></label>
          <textarea
            rows={4}
            style={{ ...INPUT_STYLE, fontFamily: 'monospace', resize: 'vertical' }}
            value={modelsText}
            onChange={(e) => setModelsText(e.target.value)}
            placeholder="gpt-4o&#10;gpt-4o-mini"
          />
        </div>

        {/* Timeout */}
        <div style={FIELD_GAP}>
          <label style={LABEL_STYLE}>Timeout (seconds)</label>
          <input
            type="number"
            min="10"
            max="600"
            style={INPUT_STYLE}
            value={timeout}
            onChange={(e) => setTimeout_(parseInt(e.target.value, 10) || 120)}
          />
        </div>

        {/* Buttons */}
        <div style={{ display: 'flex', gap: '0.5rem', justifyContent: 'flex-end', marginTop: '0.5rem' }}>
          <button
            onClick={onCancel}
            style={{
              background: '#45475a',
              color: '#cdd6f4',
              border: 'none',
              borderRadius: '4px',
              padding: '0.45rem 1rem',
              cursor: 'pointer',
              fontSize: '0.85rem',
            }}
          >Cancel</button>
          <button
            onClick={handleSubmit}
            style={{
              background: '#89b4fa',
              color: '#1e1e2e',
              border: 'none',
              borderRadius: '4px',
              padding: '0.45rem 1rem',
              cursor: 'pointer',
              fontWeight: 600,
              fontSize: '0.85rem',
            }}
          >{isNew ? 'Add Provider' : 'Save Changes'}</button>
        </div>
      </div>
    </div>
  );
}

export default React.memo(ProviderEditModal);
