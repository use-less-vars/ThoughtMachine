import React, { useState, useEffect, useCallback } from 'react';

const STATUS_CONFIG = {
  running:   { color: '#a6e3a1', label: 'Running' },
  stopped:   { color: '#f9e2af', label: 'Stopped' },
  building:  { color: '#f9e2af', label: 'Building' },
  error:     { color: '#f38ba8', label: 'Error' },
  unavailable: { color: '#6c7086', label: 'Container status unavailable' },
};

const ContainerPanelContent = () => {
  const [status, setStatus] = useState('unavailable');
  const [capabilities, setCapabilities] = useState(null);
  const [buildLog, setBuildLog] = useState('');

  const fetchStatus = useCallback(async () => {
    try {
      const res = await fetch('/api/container/status');
      if (!res.ok) throw new Error('not available');
      const data = await res.json();
      setStatus(data.status || 'unknown');
      setCapabilities(data.capabilities || null);
      setBuildLog(data.build_log || '');
    } catch {
      setStatus('unavailable');
      setCapabilities(null);
      setBuildLog('');
    }
  }, []);

  useEffect(() => {
    fetchStatus();
    const interval = setInterval(fetchStatus, 5000);
    return () => clearInterval(interval);
  }, [fetchStatus]);

  const cfg = STATUS_CONFIG[status] || STATUS_CONFIG.unavailable;

  return (
    <div className="config-section">
      <h3 style={{ marginTop: 0 }}>Container Sandbox</h3>

      {/* Status */}
      <div className="config-field" style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
        <span style={{
          display: 'inline-block', width: 12, height: 12, borderRadius: '50%',
          backgroundColor: cfg.color, flexShrink: 0
        }} />
        <span>{cfg.label}</span>
      </div>

      {/* Capabilities */}
      <div className="config-field">
        <label>Capabilities</label>
        <div style={{ fontSize: '0.85rem', color: 'var(--text-muted)' }}>
          {capabilities
            ? <pre style={{ margin: 0, whiteSpace: 'pre-wrap' }}>{JSON.stringify(capabilities, null, 2)}</pre>
            : <em>Coming soon…</em>
          }
        </div>
      </div>

      {/* Build log */}
      <div className="config-field">
        <label>Build Log</label>
        <pre style={{
          background: 'var(--bg-dark)', color: 'var(--text)',
          padding: '0.5rem', maxHeight: 200, overflow: 'auto',
          fontSize: '0.8rem', borderRadius: 4, margin: 0
        }}>
          {buildLog || 'No build log available.'}
        </pre>
      </div>

      {/* Rebuild button */}
      <div className="config-field">
        <button className="btn" disabled title="Rebuild available in Phase 2">
          Rebuild Container
        </button>
      </div>
    </div>
  );
};

export default React.memo(ContainerPanelContent);
