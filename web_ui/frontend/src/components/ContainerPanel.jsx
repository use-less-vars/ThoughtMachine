import React, { useState, useEffect, useCallback } from 'react';

const INTEGRITY_STATUS = {
  ok:        { color: '#a6e3a1', label: 'Container integrity OK' },
  mismatch:  { color: '#f38ba8', label: 'Container integrity mismatch — permissions differ from expected' },
  removed:   { color: '#f38ba8', label: 'Container was removed — re-creation needed' },
  error:     { color: '#f38ba8', label: 'Integrity check error' },
  checking:  { color: '#f9e2af', label: 'Checking integrity…' },
  unknown:    { color: '#6c7086', label: 'Integrity not yet checked' },
};

const STATUS_CONFIG = {
  running:   { color: '#a6e3a1', label: 'Running' },
  stopped:   { color: '#f9e2af', label: 'Stopped' },
  building:  { color: '#f9e2af', label: 'Building' },
  error:     { color: '#f38ba8', label: 'Error' },
  unavailable: { color: '#6c7086', label: 'Container status unavailable' },
};

const ContainerPanelContent = ({ workspacePath = '' }) => {
  const [status, setStatus] = useState('unavailable');
  const [capabilities, setCapabilities] = useState(null);
  const [buildLog, setBuildLog] = useState('');
  const [rebuilding, setRebuilding] = useState(false);
  const [imageTag, setImageTag] = useState('');
  const [integrity, setIntegrity] = useState('unknown');
  const [integrityDetails, setIntegrityDetails] = useState(null);

  const fetchStatus = useCallback(async () => {
    try {
      const params = new URLSearchParams();
      if (workspacePath) params.set('workspace', workspacePath);
      const res = await fetch(`/api/container/status?${params}`);
      if (!res.ok) throw new Error('not available');
      const data = await res.json();
      setStatus(data.status || 'unknown');
      setCapabilities(data.capabilities || null);
      setBuildLog(data.build_log || '');
      setImageTag(data.image || '');
    } catch {
      setStatus('unavailable');
      setCapabilities(null);
      setBuildLog('');
      setImageTag('');
    }
  }, [workspacePath]);

  const fetchIntegrity = useCallback(async () => {
    try {
      const params = new URLSearchParams();
      if (workspacePath) params.set('workspace', workspacePath);
      const res = await fetch(`/api/container/integrity?${params}`);
      if (!res.ok) throw new Error('not available');
      const data = await res.json();
      setIntegrity(data.integrity || 'unknown');
      setIntegrityDetails(data.details || null);
    } catch {
      setIntegrity('unknown');
      setIntegrityDetails(null);
    }
  }, [workspacePath]);

  useEffect(() => {
    fetchStatus();
    fetchIntegrity();
    const interval = setInterval(() => {
      fetchStatus();
      fetchIntegrity();
    }, 5000);
    return () => clearInterval(interval);
  }, [fetchStatus, fetchIntegrity]);

  const handleRebuild = useCallback(async () => {
    setRebuilding(true);
    setBuildLog('');
    setStatus('building');
    try {
      const params = new URLSearchParams();
      if (workspacePath) params.set('workspace', workspacePath);
      const res = await fetch('/api/container/rebuild', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: params.toString(),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setBuildLog(data.build_log || '');
      setStatus(data.status === 'ok' ? 'stopped' : data.status);
      // Refresh full status after rebuild completes
      fetchStatus();
    } catch (err) {
      setBuildLog(`Rebuild failed: ${err.message}`);
      setStatus('error');
    } finally {
      setRebuilding(false);
    }
  }, [workspacePath, fetchStatus]);

  const cfg = STATUS_CONFIG[status] || STATUS_CONFIG.unavailable;
  const intCfg = INTEGRITY_STATUS[integrity] || INTEGRITY_STATUS.unknown;

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

      {/* Integrity status */}
      <div className="config-field" style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
        <span style={{
          display: 'inline-block', width: 10, height: 10, borderRadius: '50%',
          backgroundColor: intCfg.color, flexShrink: 0
        }} />
        <span style={{ fontSize: '0.85rem', color: 'var(--text-muted)' }}>{intCfg.label}</span>
      </div>

      {/* Image tag */}
      <div className="config-field">
        <label>Image</label>
        <span style={{ fontSize: '0.85rem', color: 'var(--text-muted)', fontFamily: 'monospace' }}>
          {imageTag || <em>Not available</em>}
        </span>
      </div>

      {/* Integrity details when mismatch */}
      {integrity === 'mismatch' && integrityDetails && (
        <div className="config-field">
          <label>Mismatch Details</label>
          <pre style={{
            background: 'var(--bg-dark)', color: 'var(--text)',
            padding: '0.5rem', maxHeight: 150, overflow: 'auto',
            fontSize: '0.8rem', borderRadius: 4, margin: 0
          }}>
            {JSON.stringify(integrityDetails, null, 2)}
          </pre>
        </div>
      )}

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
        <button
          className="btn"
          onClick={handleRebuild}
          disabled={rebuilding || status === 'building'}
          title={rebuilding ? 'Rebuilding...' : status === 'unavailable' ? 'Workspace not set' : 'Rebuild Docker image from Dockerfile with --no-cache'}
        >
          {rebuilding || status === 'building' ? 'Rebuilding…' : 'Rebuild Container'}
        </button>
      </div>
    </div>
  );
};

export default React.memo(ContainerPanelContent);
