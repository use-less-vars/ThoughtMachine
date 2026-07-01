import React, { useState, useEffect, useCallback } from 'react';

// ── Reusable inline styles (matching WorkspacePanel's Catppuccin palette) ──
const inputStyle = {
  background: '#1e1e2e',
  color: '#cdd6f4',
  border: '1px solid #585b70',
  borderRadius: '4px',
  padding: '0.4rem 0.5rem',
  fontSize: '0.85rem',
  width: '100%',
  boxSizing: 'border-box',
  outline: 'none',
};

// ── DockerfileEditor Component ────────────────────────────────────────────
export default function DockerfileEditor({ workspaceId }) {
  const [content, setContent] = useState('');
  const [lastSaved, setLastSaved] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState('');
  const [lastSavedTime, setLastSavedTime] = useState('');

  // Fetch Dockerfile on mount / workspaceId change
  const fetchDockerfile = useCallback(() => {
    if (!workspaceId) return;
    setLoading(true);
    setError('');
    fetch(`/api/workspace/${workspaceId}/dockerfile`)
      .then(async (res) => {
        if (!res.ok) {
          if (res.status === 404) {
            // No custom Dockerfile yet — start with empty editor
            setContent('');
            setLastSaved('');
            return;
          }
          throw new Error(`HTTP ${res.status}`);
        }
        const text = await res.text();
        setContent(text);
        setLastSaved(text);
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [workspaceId]);

  useEffect(() => {
    fetchDockerfile();
  }, [fetchDockerfile]);

  // Whether content differs from last saved state
  const hasUnsavedChanges = content !== lastSaved;

  // Handle save
  const handleSave = useCallback(async () => {
    setSaveError('');
    setSaving(true);
    try {
      const res = await fetch(`/api/workspace/${workspaceId}/dockerfile`, {
        method: 'PUT',
        headers: { 'Content-Type': 'text/plain' },
        body: content,
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setLastSaved(content);
      setLastSavedTime(new Date().toLocaleString());
      setSaveError('');
    } catch (err) {
      setSaveError(err.message);
    } finally {
      setSaving(false);
    }
  }, [workspaceId, content]);

  // ── Loading state ─────────────────────────────────────────────────────
  if (loading) {
    return (
      <div style={{ color: '#6c7086', fontSize: '0.85rem' }}>
        Loading Dockerfile…
      </div>
    );
  }

  // ── Fetch error state ─────────────────────────────────────────────────
  if (error) {
    return (
      <div>
        <div style={{ color: '#f38ba8', fontSize: '0.85rem', marginBottom: '0.4rem' }}>
          Error: {error}
        </div>
        <button
          onClick={fetchDockerfile}
          style={{
            background: '#89b4fa',
            color: '#1e1e2e',
            border: 'none',
            borderRadius: '4px',
            padding: '0.3rem 0.75rem',
            cursor: 'pointer',
            fontWeight: 600,
            fontSize: '0.8rem',
          }}
        >
          Retry
        </button>
      </div>
    );
  }

  // ── Normal state ──────────────────────────────────────────────────────
  return (
    <div>
      {/* Warning banner for unsaved changes */}
      {hasUnsavedChanges && (
        <div
          style={{
            background: 'rgba(249, 226, 175, 0.15)',
            border: '1px solid #f9e2af',
            color: '#f9e2af',
            borderRadius: '4px',
            padding: '0.4rem 0.6rem',
            fontSize: '0.8rem',
            marginBottom: '0.5rem',
            display: 'flex',
            alignItems: 'center',
            gap: '0.4rem',
          }}
        >
          <span style={{ fontWeight: 600 }}>⚠</span>
          <span>
            You've changed the Dockerfile. Rebuild the container for changes to take effect.
          </span>
        </div>
      )}

      {/* Textarea */}
      <textarea
        value={content}
        onChange={(e) => setContent(e.target.value)}
        rows={20}
        style={{
          ...inputStyle,
          fontFamily: "'JetBrains Mono', 'Fira Code', monospace",
          fontSize: '0.75rem',
          lineHeight: '1.4',
          resize: 'vertical',
          marginBottom: '0.4rem',
        }}
        placeholder="# Paste or edit your Dockerfile here&#10;# Leave empty for the default Dockerfile"
        spellCheck={false}
      />

      {/* Save error */}
      {saveError && (
        <div style={{ color: '#f38ba8', fontSize: '0.8rem', marginBottom: '0.4rem' }}>
          Error: {saveError}
        </div>
      )}

      {/* Bottom bar: save button + timestamp */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
        <button
          onClick={handleSave}
          disabled={saving || !hasUnsavedChanges}
          style={{
            background: !hasUnsavedChanges
              ? '#585b70'
              : saving
                ? '#585b70'
                : '#89b4fa',
            color: !hasUnsavedChanges ? '#6c7086' : '#1e1e2e',
            border: 'none',
            borderRadius: '4px',
            padding: '0.3rem 0.75rem',
            cursor: saving || !hasUnsavedChanges ? 'default' : 'pointer',
            fontWeight: 600,
            fontSize: '0.8rem',
            transition: 'background 0.15s',
          }}
        >
          {saving ? 'Saving…' : 'Save'}
        </button>
        {lastSavedTime && (
          <small style={{ color: '#6c7086', fontSize: '0.75rem' }}>
            Last saved: {lastSavedTime}
          </small>
        )}
      </div>
    </div>
  );
}
