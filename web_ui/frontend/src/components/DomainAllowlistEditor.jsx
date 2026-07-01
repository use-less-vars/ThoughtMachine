import React, { useState, useEffect, useCallback, useRef } from 'react';

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

// ── DomainAllowlistEditor Component ──────────────────────────────────────
export default function DomainAllowlistEditor({ workspaceId }) {
  const [domains, setDomains] = useState([]);
  const [lastSaved, setLastSaved] = useState([]);
  const [newDomain, setNewDomain] = useState('');
  const [addError, setAddError] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState('');
  const inputRef = useRef(null);

  // ── Fetch on mount / workspaceId change ──────────────────────────────
  const fetchAllowlist = useCallback(() => {
    if (!workspaceId) return;
    setLoading(true);
    setError('');
    fetch(`/api/workspace/${workspaceId}/domain_allowlist`)
      .then(async (res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        const list = Array.isArray(data) ? data : [];
        setDomains(list);
        setLastSaved(list);
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [workspaceId]);

  useEffect(() => {
    fetchAllowlist();
  }, [fetchAllowlist]);

  // ── Derive unsaved state ─────────────────────────────────────────────
  const hasUnsavedChanges =
    domains.length !== lastSaved.length ||
    domains.some((d, i) => d !== lastSaved[i]);

  // ── Add domain ───────────────────────────────────────────────────────
  const handleAdd = useCallback(() => {
    const trimmed = newDomain.trim();
    setAddError('');

    if (!trimmed) {
      setAddError('Domain cannot be empty.');
      return;
    }

    if (domains.some((d) => d.toLowerCase() === trimmed.toLowerCase())) {
      setAddError('Domain already in the allowlist.');
      return;
    }

    setDomains((prev) => [...prev, trimmed]);
    setNewDomain('');
    // Refocus the input after adding
    inputRef.current?.focus();
  }, [newDomain, domains]);

  // ── Remove domain ────────────────────────────────────────────────────
  const handleRemove = useCallback((index) => {
    setDomains((prev) => prev.filter((_, i) => i !== index));
  }, []);

  // ── Handle Enter key in add input ────────────────────────────────────
  const handleKeyDown = useCallback(
    (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        handleAdd();
      }
    },
    [handleAdd],
  );

  // ── Save ─────────────────────────────────────────────────────────────
  const handleSave = useCallback(async () => {
    setSaveError('');
    setSaving(true);
    try {
      const res = await fetch(
        `/api/workspace/${workspaceId}/domain_allowlist`,
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ domains }),
        },
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setLastSaved([...domains]);
      setSaveError('');
    } catch (err) {
      setSaveError(err.message);
    } finally {
      setSaving(false);
    }
  }, [workspaceId, domains]);

  // ── Loading state ────────────────────────────────────────────────────
  if (loading) {
    return (
      <div style={{ color: '#6c7086', fontSize: '0.85rem' }}>
        Loading domain allowlist…
      </div>
    );
  }

  // ── Fetch error state ────────────────────────────────────────────────
  if (error) {
    return (
      <div>
        <div style={{ color: '#f38ba8', fontSize: '0.85rem', marginBottom: '0.4rem' }}>
          Error: {error}
        </div>
        <button
          onClick={fetchAllowlist}
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

  // ── Normal state ─────────────────────────────────────────────────────
  return (
    <div>
      {/* Unsaved changes indicator */}
      {hasUnsavedChanges && (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '0.4rem',
            marginBottom: '0.5rem',
            fontSize: '0.8rem',
            color: '#f9e2af',
          }}
        >
          <span style={{ fontSize: '0.7rem' }}>●</span>
          <span>Unsaved changes</span>
        </div>
      )}

      {/* Domain list */}
      {domains.length === 0 ? (
        <div
          style={{
            color: '#6c7086',
            fontSize: '0.85rem',
            fontStyle: 'italic',
            marginBottom: '0.5rem',
          }}
        >
          No domains in allowlist. Add one below.
        </div>
      ) : (
        <div style={{ marginBottom: '0.5rem' }}>
          {domains.map((domain, i) => (
            <div
              key={`${domain}-${i}`}
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                padding: '0.3rem 0.5rem',
                borderRadius: '4px',
                fontSize: '0.85rem',
                fontFamily: "'JetBrains Mono', 'Fira Code', monospace",
                background: i % 2 === 0 ? 'rgba(69, 71, 90, 0.2)' : 'transparent',
                color: '#cdd6f4',
              }}
            >
              <span style={{ wordBreak: 'break-all', flex: 1 }}>{domain}</span>
              <button
                onClick={() => handleRemove(i)}
                title={`Remove ${domain}`}
                style={{
                  background: 'transparent',
                  border: 'none',
                  color: '#f38ba8',
                  cursor: 'pointer',
                  fontSize: '0.85rem',
                  padding: '0.1rem 0.3rem',
                  marginLeft: '0.5rem',
                  flexShrink: 0,
                  lineHeight: 1,
                  borderRadius: '3px',
                }}
                onMouseEnter={(e) => (e.currentTarget.style.background = 'rgba(243, 139, 168, 0.15)')}
                onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
              >
                ✕
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Add domain input + button */}
      <div style={{ display: 'flex', gap: '0.4rem', marginBottom: '0.5rem' }}>
        <input
          ref={inputRef}
          type="text"
          value={newDomain}
          onChange={(e) => {
            setNewDomain(e.target.value);
            if (addError) setAddError('');
          }}
          onKeyDown={handleKeyDown}
          placeholder="e.g. *.github.com"
          style={{
            ...inputStyle,
            flex: 1,
            fontFamily: "'JetBrains Mono', 'Fira Code', monospace",
            fontSize: '0.8rem',
          }}
        />
        <button
          onClick={handleAdd}
          disabled={!newDomain.trim()}
          style={{
            background: newDomain.trim() ? '#89b4fa' : '#585b70',
            color: newDomain.trim() ? '#1e1e2e' : '#6c7086',
            border: 'none',
            borderRadius: '4px',
            padding: '0.3rem 0.75rem',
            cursor: newDomain.trim() ? 'pointer' : 'default',
            fontWeight: 600,
            fontSize: '0.8rem',
            whiteSpace: 'nowrap',
          }}
        >
          Add
        </button>
      </div>

      {/* Add validation error */}
      {addError && (
        <div style={{ color: '#f9e2af', fontSize: '0.8rem', marginBottom: '0.4rem' }}>
          {addError}
        </div>
      )}

      {/* Save error */}
      {saveError && (
        <div style={{ color: '#f38ba8', fontSize: '0.8rem', marginBottom: '0.4rem' }}>
          Error: {saveError}
        </div>
      )}

      {/* Bottom bar: save button + domain count */}
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
        <small style={{ color: '#6c7086', fontSize: '0.75rem' }}>
          {domains.length} domain{domains.length !== 1 ? 's' : ''}
        </small>
      </div>
    </div>
  );
}
