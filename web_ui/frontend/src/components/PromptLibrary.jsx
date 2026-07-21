import React, { useState, useEffect, useCallback } from 'react';

const API_BASE = `http://${window.location.hostname}:${import.meta.env.VITE_BACKEND_PORT || '8000'}`;

const FACTORY_NAMES = ['agent', 'engineer'];

export default function PromptLibrary({ onSelectPrompt }) {
  // ── All hooks at top level, unconditionally ──
  const [prompts, setPrompts] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [editingPrompt, setEditingPrompt] = useState(null);
  const [isCreating, setIsCreating] = useState(false);
  const [editName, setEditName] = useState('');
  const [editContent, setEditContent] = useState('');
  const [saving, setSaving] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState(null);

  // ── Fetch prompts ──
  const fetchPrompts = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/api/prompts`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setPrompts(Array.isArray(data) ? data : (Array.isArray(data.prompts) ? data.prompts : []));
    } catch (e) {
      setError(e.message || 'Failed to fetch prompts');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchPrompts(); }, [fetchPrompts]);

  // ── Create prompt ──
  const handleCreate = useCallback(async () => {
    const name = editName.trim();
    const content = editContent.trim();
    if (!name || !content) {
      setError('Name and text are required.');
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/api/prompts/${encodeURIComponent(name)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setIsCreating(false);
      setEditName('');
      setEditContent('');
      setEditingPrompt(null);
      await fetchPrompts();
    } catch (e) {
      setError(e.message || 'Failed to create prompt');
    } finally {
      setSaving(false);
    }
  }, [editName, editContent, fetchPrompts]);

  // ── Save edit ──
  const handleSaveEdit = useCallback(async () => {
    if (!editingPrompt) return;
    const content = editContent.trim();
    if (!content) {
      setError('Prompt text is required.');
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/api/prompts/${encodeURIComponent(editingPrompt.name)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setEditingPrompt(null);
      setEditName('');
      setEditContent('');
      await fetchPrompts();
    } catch (e) {
      setError(e.message || 'Failed to save prompt');
    } finally {
      setSaving(false);
    }
  }, [editingPrompt, editContent, fetchPrompts]);

  // ── Delete prompt ──
  const handleDelete = useCallback(async () => {
    if (!deleteTarget) return;
    setSaving(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/api/prompts/${encodeURIComponent(deleteTarget)}`, {
        method: 'DELETE',
      });
      if (res.status === 403) {
        setError('Factory prompts cannot be deleted.');
        setDeleteTarget(null);
        return;
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setDeleteTarget(null);
      await fetchPrompts();
    } catch (e) {
      setError(e.message || 'Failed to delete prompt');
      setDeleteTarget(null);
    } finally {
      setSaving(false);
    }
  }, [deleteTarget, fetchPrompts]);

  // ── Open edit form ──
  const startEdit = useCallback(async (name) => {
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/api/prompts/${encodeURIComponent(name)}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setEditingPrompt({ name, text: data.content });
      setEditName(name);
      setEditContent(data.content);
      setIsCreating(false);
    } catch (e) {
      setError(e.message || 'Failed to load prompt');
    }
  }, []);

  // ── Cancel edit/create ──
  const cancelForm = useCallback(() => {
    setEditingPrompt(null);
    setIsCreating(false);
    setEditName('');
    setEditContent('');
    setError(null);
  }, []);

  // ── Select prompt (calls parent callback) ──
  const handleSelect = useCallback((name) => {
    onSelectPrompt?.(name);
  }, [onSelectPrompt]);

  // ── Start creating ──
  const startCreating = useCallback(() => {
    setIsCreating(true);
    setEditingPrompt(null);
    setEditName('');
    setEditContent('');
    setError(null);
  }, []);

  // ── Helper: is factory prompt ──
  const isFactory = (name) => FACTORY_NAMES.includes(name);

  // ── Styles ──
  const containerStyle = {
    fontFamily: 'sans-serif',
    fontSize: '0.85rem',
    color: '#cdd6f4',
  };
  const inputStyle = {
    width: '100%',
    marginTop: '0.25rem',
    background: '#45475a',
    color: '#cdd6f4',
    border: '1px solid #585b70',
    borderRadius: '4px',
    padding: '0.4rem 0.6rem',
    boxSizing: 'border-box',
    fontSize: '0.85rem',
    outline: 'none',
  };
  const textareaStyle = {
    ...inputStyle,
    fontFamily: 'monospace',
    resize: 'vertical',
    minHeight: '120px',
  };
  const listItemStyle = {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    padding: '0.5rem 0.6rem',
    background: '#45475a',
    borderRadius: '4px',
    marginBottom: '0.35rem',
    cursor: 'pointer',
    transition: 'background 0.15s',
  };
  const factoryBadgeStyle = {
    background: 'rgba(249,226,175,0.15)',
    color: '#f9e2af',
    fontSize: '0.7rem',
    padding: '0.15rem 0.4rem',
    borderRadius: '3px',
    fontWeight: 600,
    marginLeft: '0.4rem',
  };
  const btnAccentStyle = {
    background: '#89b4fa',
    color: '#1e1e2e',
    border: 'none',
    borderRadius: '4px',
    padding: '0.35rem 0.75rem',
    cursor: 'pointer',
    fontSize: '0.8rem',
    fontWeight: 600,
  };
  const btnDangerStyle = {
    background: '#f38ba8',
    color: '#1e1e2e',
    border: 'none',
    borderRadius: '4px',
    padding: '0.35rem 0.75rem',
    cursor: 'pointer',
    fontSize: '0.8rem',
    fontWeight: 600,
  };
  const btnSecondaryStyle = {
    background: 'transparent',
    color: '#a6adc8',
    border: '1px solid #585b70',
    borderRadius: '4px',
    padding: '0.35rem 0.75rem',
    cursor: 'pointer',
    fontSize: '0.8rem',
  };
  const disabledBtnStyle = {
    ...btnAccentStyle,
    opacity: 0.5,
    cursor: 'not-allowed',
  };

  // ── Render: Edit form ──
  if (editingPrompt) {
    return (
      <div style={containerStyle}>
        <h4 style={{ margin: '0 0 0.75rem 0', fontSize: '0.9rem', color: '#cdd6f4' }}>
          Edit Prompt: {editingPrompt.name}
        </h4>
        <div style={{ marginBottom: '0.75rem' }}>
          <label style={{ display: 'block', marginBottom: '0.25rem', color: '#a6adc8', fontSize: '0.8rem' }}>
            <strong>Name</strong>
          </label>
          <input
            type="text"
            style={{ ...inputStyle, opacity: 0.6 }}
            value={editingPrompt.name}
            disabled
          />
        </div>
        <div style={{ marginBottom: '0.75rem' }}>
          <label style={{ display: 'block', marginBottom: '0.25rem', color: '#a6adc8', fontSize: '0.8rem' }}>
            <strong>Prompt Text</strong>
          </label>
          <textarea
            rows={6}
            style={textareaStyle}
            value={editContent}
            onChange={(e) => setEditContent(e.target.value)}
          />
        </div>
        <div style={{ display: 'flex', gap: '0.4rem' }}>
          <button
            style={saving ? disabledBtnStyle : btnAccentStyle}
            onClick={handleSaveEdit}
            disabled={saving}
          >
            {saving ? 'Saving...' : 'Save'}
          </button>
          <button style={btnSecondaryStyle} onClick={cancelForm} disabled={saving}>
            Cancel
          </button>
        </div>
        {error && <p style={{ color: '#f38ba8', fontSize: '0.8rem', marginTop: '0.5rem' }}>{error}</p>}
      </div>
    );
  }

  // ── Render: Create form ──
  if (isCreating) {
    return (
      <div style={containerStyle}>
        <h4 style={{ margin: '0 0 0.75rem 0', fontSize: '0.9rem', color: '#cdd6f4' }}>
          New Prompt
        </h4>
        <div style={{ marginBottom: '0.75rem' }}>
          <label style={{ display: 'block', marginBottom: '0.25rem', color: '#a6adc8', fontSize: '0.8rem' }}>
            <strong>Name</strong>
          </label>
          <input
            type="text"
            style={inputStyle}
            value={editName}
            onChange={(e) => setEditName(e.target.value)}
            placeholder="e.g., my-custom-prompt"
            autoFocus
          />
        </div>
        <div style={{ marginBottom: '0.75rem' }}>
          <label style={{ display: 'block', marginBottom: '0.25rem', color: '#a6adc8', fontSize: '0.8rem' }}>
            <strong>Prompt Text</strong>
          </label>
          <textarea
            rows={6}
            style={textareaStyle}
            value={editContent}
            onChange={(e) => setEditContent(e.target.value)}
            placeholder="Enter prompt content..."
          />
        </div>
        <div style={{ display: 'flex', gap: '0.4rem' }}>
          <button
            style={saving ? disabledBtnStyle : btnAccentStyle}
            onClick={handleCreate}
            disabled={saving}
          >
            {saving ? 'Saving...' : 'Save'}
          </button>
          <button style={btnSecondaryStyle} onClick={cancelForm} disabled={saving}>
            Cancel
          </button>
        </div>
        {error && <p style={{ color: '#f38ba8', fontSize: '0.8rem', marginTop: '0.5rem' }}>{error}</p>}
      </div>
    );
  }

  // ── Render: List view (default) ──
  return (
    <div style={containerStyle}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.75rem' }}>
        <h4 style={{ margin: 0, fontSize: '0.9rem', color: '#cdd6f4' }}>Prompt Library</h4>
        <button style={btnAccentStyle} onClick={startCreating}>
          + New Prompt
        </button>
      </div>

      {/* Error message */}
      {error && <p style={{ color: '#f38ba8', fontSize: '0.8rem', marginBottom: '0.5rem' }}>{error}</p>}

      {/* Loading state */}
      {loading ? (
        <p style={{ color: '#6c7086', fontStyle: 'italic', textAlign: 'center', padding: '1rem 0' }}>
          Loading prompts...
        </p>
      ) : prompts.length === 0 ? (
        <p style={{ color: '#6c7086', fontStyle: 'italic', textAlign: 'center', padding: '1rem 0' }}>
          No prompts yet. Create one to get started.
        </p>
      ) : (
        prompts.map((prompt) => {
          const name = prompt.name || prompt;
          return (
            <div
              key={name}
              style={listItemStyle}
              onClick={() => handleSelect(name)}
              onMouseEnter={(e) => { e.currentTarget.style.background = '#585b70'; }}
              onMouseLeave={(e) => { e.currentTarget.style.background = '#45475a'; }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: '0.3rem', overflow: 'hidden' }}>
                <span style={{ fontWeight: 500, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                  {name}
                </span>
                {isFactory(name) && <span style={factoryBadgeStyle}>🔒 Factory</span>}
              </div>
              <div style={{ display: 'flex', gap: '0.3rem', flexShrink: 0 }} onClick={(e) => e.stopPropagation()}>
                {!isFactory(name) && (
                  <>
                    <button
                      style={{ background: 'none', border: 'none', cursor: 'pointer', color: '#89b4fa', fontSize: '0.8rem', padding: '0.15rem 0.3rem' }}
                      title="Edit prompt"
                      onClick={() => startEdit(name)}
                    >
                      ✏️
                    </button>
                    <button
                      style={{ background: 'none', border: 'none', cursor: 'pointer', color: '#f38ba8', fontSize: '0.8rem', padding: '0.15rem 0.3rem' }}
                      title="Delete prompt"
                      onClick={() => setDeleteTarget(name)}
                    >
                      🗑️
                    </button>
                  </>
                )}
              </div>
            </div>
          );
        })
      )}

      {/* Delete confirmation */}
      {deleteTarget && (
        <div style={{
          marginTop: '0.75rem',
          padding: '0.6rem',
          border: '1px solid #f38ba8',
          borderRadius: '4px',
          background: 'rgba(243,139,168,0.08)',
        }}>
          <p style={{ margin: '0 0 0.5rem 0', fontSize: '0.85rem', color: '#cdd6f4' }}>
            Delete prompt <strong>'{deleteTarget}'</strong>?
          </p>
          <div style={{ display: 'flex', gap: '0.4rem' }}>
            <button style={saving ? { ...btnDangerStyle, opacity: 0.5, cursor: 'not-allowed' } : btnDangerStyle} onClick={handleDelete} disabled={saving}>
              {saving ? 'Deleting...' : 'Delete'}
            </button>
            <button style={btnSecondaryStyle} onClick={() => setDeleteTarget(null)} disabled={saving}>
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
