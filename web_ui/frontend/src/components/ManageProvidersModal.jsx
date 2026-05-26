/*
 * ManageProvidersModal.jsx
 *
 * Modal that lists all provider profiles with Add / Edit / Delete controls.
 * Props:
 *   providers   — array of provider objects (from backend)
 *   sendCommand — WS sendCommand function
 *   onClose     — called when modal is dismissed
 */
import React, { useState, useCallback } from 'react';
import ProviderEditModal from './ProviderEditModal';

const MODAL_BACKDROP = {
  position: 'fixed',
  top: 0, left: 0, right: 0, bottom: 0,
  background: 'rgba(0,0,0,0.6)',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  zIndex: 1050,
};

const MODAL_STYLE = {
  background: '#313244',
  border: '1px solid #585b70',
  borderRadius: '8px',
  padding: '1.25rem',
  width: '600px',
  maxHeight: '75vh',
  display: 'flex',
  flexDirection: 'column',
  boxShadow: '0 4px 20px rgba(0,0,0,0.4)',
};

const TABLE_HEADER_STYLE = {
  textAlign: 'left',
  padding: '0.5rem 0.6rem',
  fontSize: '0.8rem',
  color: '#a6adc8',
  borderBottom: '1px solid #585b70',
  fontWeight: 600,
  whiteSpace: 'nowrap',
};

const TABLE_CELL_STYLE = {
  padding: '0.45rem 0.6rem',
  fontSize: '0.82rem',
  color: '#cdd6f4',
  borderBottom: '1px solid #45475a',
  verticalAlign: 'middle',
};

function ManageProvidersModal({ providers, sendCommand, onClose }) {
  const [editProvider, setEditProvider] = useState(null);  // null = list view, object = editing
  const [confirmDeleteId, setConfirmDeleteId] = useState(null);

  const handleAdd = useCallback(() => {
    setEditProvider({});  // empty object triggers "new" mode
  }, []);

  const handleEdit = useCallback((provider) => {
    setEditProvider(provider);
  }, []);

  const handleSave = useCallback((providerData) => {
    sendCommand('save_provider', { provider: providerData });
    setEditProvider(null);
  }, [sendCommand]);

  const handleCancelEdit = useCallback(() => {
    setEditProvider(null);
  }, []);

  const handleDeleteClick = useCallback((providerId) => {
    setConfirmDeleteId(providerId);
  }, []);

  const handleConfirmDelete = useCallback(() => {
    if (confirmDeleteId) {
      sendCommand('delete_provider', { provider_id: confirmDeleteId });
      setConfirmDeleteId(null);
    }
  }, [confirmDeleteId, sendCommand]);

  const handleCancelDelete = useCallback(() => {
    setConfirmDeleteId(null);
  }, []);

  // ── If editing, show the edit modal instead ──
  if (editProvider !== null) {
    return (
      <ProviderEditModal
        provider={editProvider.id ? editProvider : null}
        onSave={handleSave}
        onCancel={handleCancelEdit}
      />
    );
  }

  // ── Confirm delete overlay ──
  if (confirmDeleteId) {
    const p = providers.find((pr) => pr.id === confirmDeleteId);
    return (
      <div style={MODAL_BACKDROP} onClick={handleCancelDelete}>
        <div style={{ ...MODAL_STYLE, width: '400px' }} onClick={(e) => e.stopPropagation()}>
          <div style={{ marginBottom: '1rem', fontSize: '0.95rem', fontWeight: 600 }}>
            Delete Provider
          </div>
          <p style={{ fontSize: '0.85rem', color: '#cdd6f4', marginBottom: '0.5rem' }}>
            Are you sure you want to delete <strong>{p?.label || confirmDeleteId}</strong>?
          </p>
          <p style={{ fontSize: '0.8rem', color: '#f9e2af', marginBottom: '1rem' }}>
            This action cannot be undone.
          </p>
          <div style={{ display: 'flex', gap: '0.5rem', justifyContent: 'flex-end' }}>
            <button
              onClick={handleCancelDelete}
              style={{
                background: '#45475a', color: '#cdd6f4', border: 'none',
                borderRadius: '4px', padding: '0.45rem 1rem', cursor: 'pointer', fontSize: '0.85rem',
              }}
            >Cancel</button>
            <button
              onClick={handleConfirmDelete}
              style={{
                background: '#f38ba8', color: '#1e1e2e', border: 'none',
                borderRadius: '4px', padding: '0.45rem 1rem', cursor: 'pointer', fontWeight: 600, fontSize: '0.85rem',
              }}
            >Delete</button>
          </div>
        </div>
      </div>
    );
  }

  // ── Provider list view ──
  return (
    <div style={MODAL_BACKDROP} onClick={onClose}>
      <div style={MODAL_STYLE} onClick={(e) => e.stopPropagation()}>
        {/* Header */}
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.75rem' }}>
          <strong style={{ fontSize: '0.95rem' }}>Manage Providers</strong>
          <button onClick={onClose} style={{
            background: 'none', border: 'none', color: '#a6adc8', cursor: 'pointer', fontSize: '1.2rem'
          }}>✕</button>
        </div>

        {/* Table */}
        <div style={{ flex: 1, overflowY: 'auto', marginBottom: '0.75rem' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr>
                <th style={TABLE_HEADER_STYLE}>Label</th>
                <th style={TABLE_HEADER_STYLE}>ID</th>
                <th style={TABLE_HEADER_STYLE}>Type</th>
                <th style={TABLE_HEADER_STYLE}>Base URL</th>
                <th style={TABLE_HEADER_STYLE}>Default Model</th>
                <th style={{ ...TABLE_HEADER_STYLE, width: '100px', textAlign: 'center' }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {providers.length === 0 ? (
                <tr>
                  <td colSpan={6} style={{ ...TABLE_CELL_STYLE, textAlign: 'center', color: '#6c7086', fontStyle: 'italic' }}>
                    No providers configured yet. Click "Add Provider" to get started.
                  </td>
                </tr>
              ) : (
                providers.map((p) => (
                  <tr key={p.id}>
                    <td style={TABLE_CELL_STYLE}>{p.label}</td>
                    <td style={{ ...TABLE_CELL_STYLE, fontFamily: 'monospace', fontSize: '0.78rem' }}>{p.id}</td>
                    <td style={TABLE_CELL_STYLE}>{p.provider_type}</td>
                    <td style={{ ...TABLE_CELL_STYLE, maxWidth: '180px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {p.base_url}
                    </td>
                    <td style={TABLE_CELL_STYLE}>{p.default_model || '—'}</td>
                    <td style={{ ...TABLE_CELL_STYLE, textAlign: 'center' }}>
                      <div style={{ display: 'flex', gap: '0.3rem', justifyContent: 'center' }}>
                        <button
                          onClick={() => handleEdit(p)}
                          style={{
                            background: '#89b4fa', color: '#1e1e2e', border: 'none',
                            borderRadius: '3px', padding: '0.25rem 0.5rem', cursor: 'pointer',
                            fontSize: '0.75rem', fontWeight: 600,
                          }}
                          title="Edit provider"
                        >Edit</button>
                        <button
                          onClick={() => handleDeleteClick(p.id)}
                          style={{
                            background: '#45475a', color: '#f38ba8', border: '1px solid #f38ba8',
                            borderRadius: '3px', padding: '0.25rem 0.5rem', cursor: 'pointer',
                            fontSize: '0.75rem',
                          }}
                          title="Delete provider"
                        >Delete</button>
                      </div>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

        {/* Add button */}
        <div style={{ display: 'flex', gap: '0.5rem', justifyContent: 'flex-end' }}>
          <button
            onClick={onClose}
            style={{
              background: '#45475a', color: '#cdd6f4', border: 'none',
              borderRadius: '4px', padding: '0.45rem 1rem', cursor: 'pointer', fontSize: '0.85rem',
            }}
          >Close</button>
          <button
            onClick={handleAdd}
            style={{
              background: '#a6e3a1', color: '#1e1e2e', border: 'none',
              borderRadius: '4px', padding: '0.45rem 1rem', cursor: 'pointer', fontWeight: 600, fontSize: '0.85rem',
            }}
          >+ Add Provider</button>
        </div>
      </div>
    </div>
  );
}

export default React.memo(ManageProvidersModal);
