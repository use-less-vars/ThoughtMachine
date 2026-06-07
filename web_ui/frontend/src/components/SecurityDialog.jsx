/*
 * SecurityDialog.jsx
 *
 * Modal dialog that prompts the user to approve or deny a tool execution
 * request from the AI agent.  Shown automatically when a SECURITY_PROMPT
 * event arrives over the WebSocket.
 *
 * Props:
 *   prompt      — object with { request_id, tool_name, capabilities, arguments, description }
 *   sendCommand — WS sendCommand function
 *   onDismiss   — called when the dialog is resolved or cancelled
 */
import React, { useCallback, useState } from 'react';

// ── Shared style constants (Catppuccin Mocha) ───────────────────────────────
const BACKDROP = {
  position: 'fixed',
  top: 0, left: 0, right: 0, bottom: 0,
  background: 'rgba(0,0,0,0.6)',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  zIndex: 1060, // above provider modals (1050)
};

const CARD = {
  background: '#313244',
  border: '1px solid #585b70',
  borderRadius: '8px',
  padding: '1.25rem',
  width: '520px',
  maxWidth: '90vw',
  boxShadow: '0 4px 20px rgba(0,0,0,0.4)',
  display: 'flex',
  flexDirection: 'column',
  gap: '0.75rem',
};

const BADGE_STYLE = {
  display: 'inline-block',
  background: '#45475a',
  color: '#89b4fa',
  padding: '0.2rem 0.5rem',
  borderRadius: '4px',
  fontSize: '0.75rem',
  fontWeight: 600,
  fontFamily: 'monospace',
};

const CAP_BADGE = {
  display: 'inline-block',
  background: '#1e1e2e',
  color: '#a6e3a1',
  padding: '0.15rem 0.45rem',
  borderRadius: '3px',
  fontSize: '0.72rem',
  fontFamily: 'monospace',
  margin: '0.15rem 0.25rem 0.15rem 0',
};

function SecurityDialog({ prompt, sendCommand, onDismiss }) {
  const [remember, setRemember] = useState(false);

  const handleApprove = useCallback(() => {
    sendCommand('security_response', {
      request_id: prompt.request_id,
      approved: true,
      remember,
    });
    onDismiss();
  }, [prompt.request_id, remember, sendCommand, onDismiss]);

  const handleDeny = useCallback(() => {
    sendCommand('security_response', {
      request_id: prompt.request_id,
      approved: false,
      remember,
    });
    onDismiss();
  }, [prompt.request_id, remember, sendCommand, onDismiss]);

  const handleBackdropClick = useCallback((e) => {
    // Closing the dialog via backdrop cancels (denies) the prompt
    if (e.target === e.currentTarget) {
      sendCommand('security_response', {
        request_id: prompt.request_id,
        approved: false,
        remember: false,
      });
      onDismiss();
    }
  }, [prompt.request_id, sendCommand, onDismiss]);

  return (
    <div style={BACKDROP} onClick={handleBackdropClick}>
      <div style={CARD} onClick={(e) => e.stopPropagation()}>
        {/* ── Header ────────────────────────────────────────────── */}
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
            <span style={{ fontSize: '1.1rem' }}>🔒</span>
            <strong style={{ fontSize: '0.95rem', color: '#f9e2af' }}>
              Security Prompt
            </strong>
          </div>
          {prompt.tool_name && (
            <span style={BADGE_STYLE}>{prompt.tool_name}</span>
          )}
        </div>

        {/* ── Description ───────────────────────────────────────── */}
        <p style={{ fontSize: '0.85rem', color: '#cdd6f4', margin: 0, lineHeight: 1.5 }}>
          {prompt.description || `Tool '${prompt.tool_name || 'unknown'}' requires your approval to proceed.`}
        </p>

        {/* ── Capabilities ──────────────────────────────────────── */}
        {prompt.capabilities && prompt.capabilities.length > 0 && (
          <div>
            <div style={{ fontSize: '0.78rem', color: '#a6adc8', marginBottom: '0.25rem', fontWeight: 600 }}>
              Required Capabilities:
            </div>
            <div>
              {prompt.capabilities.map((cap, i) => (
                <span key={i} style={CAP_BADGE}>{cap}</span>
              ))}
            </div>
          </div>
        )}

        {/* ── Arguments (collapsed preview) ─────────────────────── */}
        {prompt.arguments && Object.keys(prompt.arguments).length > 0 && (
          <div>
            <div style={{ fontSize: '0.78rem', color: '#a6adc8', marginBottom: '0.25rem', fontWeight: 600 }}>
              Arguments:
            </div>
            <pre style={{
              background: '#181825',
              color: '#bac2de',
              padding: '0.5rem 0.65rem',
              borderRadius: '4px',
              fontSize: '0.78rem',
              fontFamily: 'monospace',
              overflow: 'auto',
              maxHeight: '120px',
              margin: 0,
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-all',
            }}>
              {JSON.stringify(prompt.arguments, null, 2)}
            </pre>
          </div>
        )}

        {/* ── Remember checkbox ─────────────────────────────────── */}
        <label style={{
          display: 'flex',
          alignItems: 'center',
          gap: '0.4rem',
          fontSize: '0.82rem',
          color: '#a6adc8',
          cursor: 'pointer',
          userSelect: 'none',
        }}>
          <input
            type="checkbox"
            checked={remember}
            onChange={(e) => setRemember(e.target.checked)}
            style={{ accentColor: '#89b4fa', cursor: 'pointer' }}
          />
          Remember this decision for the current session
        </label>

        {/* ── Action buttons ────────────────────────────────────── */}
        <div style={{ display: 'flex', gap: '0.5rem', justifyContent: 'flex-end', marginTop: '0.25rem' }}>
          <button
            onClick={handleDeny}
            style={{
              background: '#45475a',
              color: '#f38ba8',
              border: '1px solid #f38ba8',
              borderRadius: '4px',
              padding: '0.45rem 1.25rem',
              cursor: 'pointer',
              fontWeight: 600,
              fontSize: '0.85rem',
              transition: 'background 0.15s',
            }}
            onMouseEnter={(e) => { e.currentTarget.style.background = '#585b70' }}
            onMouseLeave={(e) => { e.currentTarget.style.background = '#45475a' }}
          >
            Deny
          </button>
          <button
            onClick={handleApprove}
            style={{
              background: '#a6e3a1',
              color: '#1e1e2e',
              border: 'none',
              borderRadius: '4px',
              padding: '0.45rem 1.25rem',
              cursor: 'pointer',
              fontWeight: 600,
              fontSize: '0.85rem',
              transition: 'background 0.15s',
            }}
            onMouseEnter={(e) => { e.currentTarget.style.background = '#94e2d5' }}
            onMouseLeave={(e) => { e.currentTarget.style.background = '#a6e3a1' }}
          >
            Approve
          </button>
        </div>
      </div>
    </div>
  );
}

export default React.memo(SecurityDialog);
