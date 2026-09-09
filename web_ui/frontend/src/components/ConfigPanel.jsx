import React, { useState, useEffect, useMemo, useCallback } from 'react';
import ManageProvidersModal from './ManageProvidersModal';
import ContainerPanelContent from './ContainerPanel';
import WorkspacePanel from './WorkspacePanel';
import PromptLibrary from './PromptLibrary';
import useStore from '../store/useStore';

// Canonical-only option lists, permissive-first; no rank map needed — some
// canonical levels share a rank (write_on_feature_branch is a write-tier grant).
// Container is a boolean toggle, so it has no option list here.
const SESSION_PERMISSION_OPTION_ORDER = {
  git: ['write', 'write_on_feature_branch', 'read', 'ask', 'banned'],
  filesystem: ['write', 'read', 'banned'],
  network: ['write', 'outbound', 'ask', 'banned'],
  mcp: ['full', 'connect', 'banned'],
  host_bash: ['allow', 'ask', 'banned'],
}
// Mirrors backend SAFE_DEFAULTS for the canonical session resources. useStore's
// PERMISSION_DEFAULTS carries the same six canonical keys, so either may seed
// the session-permissions tab.
const CANONICAL_PERMISSION_DEFAULTS = {
  git: 'read',
  filesystem: 'read',
  container: false,
  network: 'banned',
  mcp: 'banned',
  host_bash: 'banned',
}
// Only these keys may be PUT to /api/session/{id}/permissions (mirrors the
// backend session-permission store schema).
const CANONICAL_SESSION_PERMISSION_KEYS = ['git', 'filesystem', 'container', 'network', 'mcp', 'host_bash']
const permissionOptionStyle = { background: '#1e1e2e', color: '#cdd6f4' }
const permissionOptionLabel = (value) => value === 'write_on_feature_branch'
  ? 'Write on feature branches'
  : String(value).charAt(0).toUpperCase() + String(value).slice(1)

// Flat-map deep equality for the permissions raw maps (string/bool values).
const isEqualRaw = (a, b) => {
  if (a === b) return true;
  if (!a || !b || typeof a !== 'object' || typeof b !== 'object') return false;
  const keysA = Object.keys(a);
  const keysB = Object.keys(b);
  if (keysA.length !== keysB.length) return false;
  return keysA.every((k) => a[k] === b[k]);
}

const BACKEND_PORT = import.meta.env.VITE_BACKEND_PORT || '8000';
const API_BASE = `http://${window.location.hostname}:${BACKEND_PORT}`;

function ConfigPanel({ mode = null, config, sendCommand, providers, availableTools, panelWidth, wsConnected, defaultConfigSaveStatus, onClearDefaultSaveStatus, workspaceId, sessionId, containerRebuildResult, onClearRebuildResult, selectedWorker, onSelectWorker, isActive, configQueued = false, applyFailed = null }) {
  const [defaultSaved, setDefaultSaved] = useState(false);  // false | 'pending' | true | 'error'
  const [showManageProviders, setShowManageProviders] = useState(false);
  const [providerVersion, setProviderVersion] = useState(0);  // incremented when a provider is saved
  const [allTools, setAllTools] = useState([]);
  const normalizeSessionPermissions = (permissions) => {
    const normalized = {
      ...CANONICAL_PERMISSION_DEFAULTS,
      ...(permissions ?? {}),
    };

    // Backward compatibility: old configs/sessions stored network as boolean.
    if (typeof normalized.network === 'boolean') {
      normalized.network = normalized.network ? 'write' : 'banned';
    }

    return normalized;
  };

  const getSafeDraft = (cfg) => ({
    mode: cfg?.mode ?? null,
    temperature: cfg?.temperature ?? 0.7,
    max_turns: cfg?.max_turns ?? 10,
    provider: cfg?.provider,
    provider_id: cfg?.provider_id,
    model: cfg?.model,
    system_prompt: cfg?.system_prompt ?? '',
    tools: cfg?.tools ?? [],
    // NOTE: session_permissions deliberately NOT included — permissions now live
    // in the disk-pure session endpoint (/api/session/{id}/permissions) and the
    // draft / apply_config payload must never carry them again.

    token_monitor_warning_threshold: cfg?.token_monitor_warning_threshold ?? 65000,
    token_monitor_critical_threshold: cfg?.token_monitor_critical_threshold ?? 80000,
    workspace_path: cfg?.workspace_path ?? '',
    tool_output_token_limit: cfg?.tool_output_token_limit ?? 10000,
  });

  const [activeTab, setActiveTab] = useState('workspace');
  // Draft persistence: the draft lives in the Zustand store (sessionDrafts)
  // while sessionId is known, so unsaved edits survive tab switches that
  // unmount this panel. localDraft is a fallback for the brief window where
  // sessionId is still null (fresh tab, pre-load) but config already exists.
  const storeDraft = useStore((s) => (sessionId ? s.sessionDrafts[sessionId] : undefined))
  const setSessionDraft = useStore((s) => s.setSessionDraft)
  const clearSessionDraft = useStore((s) => s.clearSessionDraft)
  const [localDraft, setLocalDraft] = useState(null)
  const draft = storeDraft ?? localDraft ?? getSafeDraft(config)
  const updateDraft = (next) => {
    if (sessionId) setSessionDraft(sessionId, next)
    else setLocalDraft(next)
  }

  // ── Directory browser state ────────────────────────────────────────
  // ── Dirty tracking & apply feedback ────────────────────────────────
  const [lastAppliedConfig, setLastAppliedConfig] = useState(null);
  const [isApplying, setIsApplying] = useState(false);
  const [applyError, setApplyError] = useState(null);

  // ── Sync defaultConfigSaveStatus from backend into local UI state ────
  useEffect(() => {
    if (defaultConfigSaveStatus === 'ok') {
      setDefaultSaved(true);
      const t = setTimeout(() => {
        setDefaultSaved(false);
        onClearDefaultSaveStatus?.();
      }, 2500);
      return () => clearTimeout(t);
    } else if (defaultConfigSaveStatus === 'error') {
      setDefaultSaved('error');
      const t = setTimeout(() => {
        setDefaultSaved(false);
        onClearDefaultSaveStatus?.();
      }, 4000);
      return () => clearTimeout(t);
    }
  }, [defaultConfigSaveStatus, onClearDefaultSaveStatus]);

  useEffect(() => {
    const seeded = getSafeDraft(config)
    setLastAppliedConfig(seeded)
    // When an apply is in flight, the incoming config is the applied truth:
    // drop any pending draft so the panel re-seeds from it. Otherwise a
    // store-backed draft is the user's unsaved work and must NOT be clobbered
    // by this config update — it only falls back to getSafeDraft(config) when
    // no draft is pending.
    if (isApplying) {
      if (sessionId) clearSessionDraft(sessionId)
      setLocalDraft(null)
      setIsApplying(false)
    }
    setApplyError(null);
  }, [config]);

  // Apply error timeout: if no config change after 6s, show error.
  // Disarmed while the apply is QUEUED (controller busy — the config_changed
  // arrives later via the deferred apply, so no false 'timed out' error).
  const applyTimeoutRef = React.useRef(null);
  useEffect(() => {
    if (isApplying && !configQueued) {
      applyTimeoutRef.current = setTimeout(() => {
        setIsApplying(false);
        setApplyError('Apply timed out — check server connection');
      }, 6000);
    }
    return () => {
      if (applyTimeoutRef.current) {
        clearTimeout(applyTimeoutRef.current);
        applyTimeoutRef.current = null;
      }
    };
  }, [isApplying, configQueued]);

  // Server-reported apply failure (config_apply_failed) — surface the real
  // error immediately instead of waiting for the 6s timeout.
  useEffect(() => {
    if (applyFailed) {
      setIsApplying(false);
      setApplyError(applyFailed);
    }
  }, [applyFailed]);

  // Auto-clear error after 5 seconds
  useEffect(() => {
    if (applyError) {
      const t = setTimeout(() => setApplyError(null), 5000);
      return () => clearTimeout(t);
    }
  }, [applyError]);

  // Fetch the complete list of all available tools from the backend
  useEffect(() => {
    fetch(`${API_BASE}/api/tools`)
      .then(res => res.json())
      .then(data => {
        if (data.tools) setAllTools(data.tools);
      })
      .catch(() => {
        // Ignore — tools tab will just show whatever the session config provides
      });
  }, []);

  // ── Permissions tab: disk-pure REST state ──────────────────────────
  // Tab-LOCAL: raw (editable grant map), effective (read-only enforced
  // profile) and resolved_at come from GET/PUT /api/session/{id}/permissions.
  // Permission edits NEVER enter the draft.
  const [sessionPerms, setSessionPerms] = useState(null);    // { raw, effective, resolved_at } | null
  const [lastAppliedRaw, setLastAppliedRaw] = useState(null); // raw from the last GET/PUT response
  const [permsLoadError, setPermsLoadError] = useState(null); // muted tab note when GET fails

  // Load the session's permission profile whenever the session changes.
  useEffect(() => {
    let cancelled = false;
    setPermsLoadError(null);
    if (!sessionId) {
      setSessionPerms(null);
      setLastAppliedRaw(null);
      return () => { cancelled = true; };
    }
    fetch(`${API_BASE}/api/session/${encodeURIComponent(sessionId)}/permissions`)
      .then(async (res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (cancelled) return;
        const raw = data && typeof data.raw === 'object' && data.raw !== null ? data.raw : {};
        setSessionPerms({
          raw,
          effective: data && typeof data.effective === 'object' && data.effective !== null ? data.effective : null,
          resolved_at: data?.resolved_at ?? null,
        });
        setLastAppliedRaw(raw);
      })
      .catch(() => {
        if (cancelled) return;
        // No permission source for this session (or server unreachable): the
        // tab falls back to display-only values; Apply skips the PUT.
        setSessionPerms(null);
        setLastAppliedRaw(null);
        setPermsLoadError('Session permissions unavailable — showing stored values');
      });
    return () => { cancelled = true; };
  }, [sessionId]);

  // Display map for the Permissions tab: REST raw once loaded, otherwise a
  // normalized seed from config.session_permissions (DISPLAY ONLY).
  const permsRaw = sessionPerms?.raw ?? normalizeSessionPermissions(config?.session_permissions);

  // ── Derived: selected provider object ──────────────────────────────
  // Backend sends 'provider' in config_changed; fall back if 'provider_id' not set.
  // Try matching by UUID id first, then by legacy provider_type string.
  const activeProviderId = (() => {
    if (draft.provider_id) {
      const match = providers.find(p => p.id === draft.provider_id);
      if (match) return draft.provider_id;
    }
    if (draft.provider) {
      const match = providers.find(p => p.provider_type === draft.provider);
      if (match) return match.id;
    }
    return '';
  })();
  const selectedProvider = providers.find((p) => p.id === activeProviderId);
  const availableModels = useMemo(() => {
    if (!selectedProvider) return []
    const models = selectedProvider.models || []
    const defaultModel = selectedProvider.default_model
    const combined = defaultModel ? [defaultModel, ...models] : models
    return [...new Set(combined)] // deduplicate
  }, [selectedProvider])

  // ── Dirty detection ────────────────────────────────────────────────
  const isDirty = useMemo(() => {
    if (!lastAppliedConfig || !draft) return false;
    return JSON.stringify(draft) !== JSON.stringify(lastAppliedConfig);
  }, [draft, lastAppliedConfig]);

  // Permissions-tab dirty: JSON-diff of the tab raw edits vs the raw returned
  // by the last GET/PUT response. Only meaningful while a REST source is loaded.
  const permsDirty = useMemo(() => {
    if (!sessionPerms || !lastAppliedRaw) return false;
    return !isEqualRaw(permsRaw, lastAppliedRaw);
  }, [sessionPerms, lastAppliedRaw, permsRaw]);

  const handleProviderChange = (e) => {
    const providerId = e.target.value
    const provider = providers.find((p) => p.id === providerId)
    // Reset model when provider changes; prefer default_model
    updateDraft({
      ...draft,
      provider_id: providerId,
      model: provider?.default_model || (provider?.models?.[0]) || '',
    })
  }

  const handleModelChange = (e) => {
    updateDraft({ ...draft, model: e.target.value })
  }

  // ── Permissions tab: raw-map edits (tab-local state only) ──────────
  const handlePermissionChange = (key, value) => {
    if (!sessionPerms) return; // display-only fallback (no session REST source)
    setSessionPerms({ ...sessionPerms, raw: { ...(sessionPerms.raw ?? {}), [key]: value } });
  }

  // ── Apply: PUT permissions first (when a session REST source is loaded),
  // then apply_config for the rest of the draft (never carrying permissions).
  const handleApply = async () => {
    setIsApplying(true);
    setApplyError(null);
    setProviderVersion(0);

    // Permission edits are persisted by the REST PUT below — immediate and
    // independent of the WebSocket apply_config pipeline (which may be queued
    // while the controller is busy).  When the ONLY pending change is the
    // permissions tab, finish right after the PUT and skip apply_config so the
    // save is never deferred behind the busy-queue ('Queued — applying…').
    const permissionsOnlyApply = Boolean(sessionPerms) && permsDirty && !isDirty && providerVersion === 0;

    if (sessionId && sessionPerms) {
      try {
        // PUT only canonical session-permission keys — the backend schema rejects
        // any other key (legacy system/execution or gate grains).
        const rawPerms = sessionPerms.raw ?? {};
        const payload = Object.fromEntries(
          CANONICAL_SESSION_PERMISSION_KEYS.filter((k) => k in rawPerms).map((k) => [k, rawPerms[k]])
        );
        const res = await fetch(`${API_BASE}/api/session/${encodeURIComponent(sessionId)}/permissions`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        if (!res.ok) {
          let message = `HTTP ${res.status}`;
          try {
            const errData = await res.json();
            if (errData?.detail?.errors?.length) message = errData.detail.errors.join('; ');
            else if (typeof errData?.detail === 'string') message = errData.detail;
            else if (errData?.detail) message = JSON.stringify(errData.detail);
          } catch { /* non-JSON error body */ }
          throw new Error(message);
        }
        const data = await res.json();
        // Applied baseline = exactly what we PUT (canonical values the backend
        // persists unchanged). Do NOT copy the echo over sessionPerms.raw: the
        // user may have edited the tab again while the PUT was in flight, and
        // those newer edits must survive (stay dirty until the next Apply)
        // instead of being silently swallowed by echo adoption.
        setSessionPerms((prev) => ({
          ...prev,
          effective: data && typeof data.effective === 'object' && data.effective !== null ? data.effective : (prev?.effective ?? null),
          resolved_at: data?.resolved_at ?? null,
        }));
        setLastAppliedRaw(payload);
      } catch (err) {
        // PUT failed: surface the error, re-enable Apply, DO NOT touch apply_config.
        setIsApplying(false);
        setApplyError(`Failed to save permissions: ${err.message}`);
        return;
      }
    }

    if (permissionsOnlyApply) {
      // The PUT already applied the permission edits and refreshed
      // lastAppliedRaw (clearing permsDirty).  Nothing config-level changed, so
      // there is nothing to queue behind the controller — re-enable Apply now.
      setIsApplying(false);
      return;
    }

    // Strip session_permissions defensively (stale store drafts may still hold
    // the legacy key); the apply_config payload must never carry permissions.
    const { session_permissions, ...configPayload } = draft;
    sendCommand('apply_config', { config: configPayload });
  }

  // ── Load prompt from library and switch to system_prompt tab ──
  const handleLoadPromptFromLibrary = useCallback(async (promptName) => {
    if (!promptName) return;
    try {
      const res = await fetch(`${API_BASE}/api/prompts/${promptName}`);
      if (!res.ok) return;
      const text = await res.text();
      const base = useStore.getState().sessionDrafts[sessionId] ?? getSafeDraft(config);
      if (sessionId) setSessionDraft(sessionId, { ...base, system_prompt: text });
      else setLocalDraft({ ...base, system_prompt: text });
      setActiveTab('system_prompt');
    } catch (e) {
      // silent
    }
  }, [sessionId, config]);

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

  // Fallback: use config?.mode when the mode prop is null (e.g. on session load)
  const effectiveMode = mode || config?.mode || null;
  const isModeLocked = effectiveMode && effectiveMode !== 'custom'

  const TAB_KEYS = ['workspace', 'permissions', 'system_prompt', 'general', 'model', 'tools', 'container', 'advanced'];
  const TAB_LABELS = { workspace: 'Workspace', permissions: 'Permissions', system_prompt: 'Prompt', general: 'General', model: 'Model', tools: 'Tools', container: 'Container', advanced: 'Advanced' };

  const modeBadge = effectiveMode === 'agent' ? 'Agent' : effectiveMode === 'engineer' ? 'Engineer' : effectiveMode === 'custom' ? 'Custom' : null
  const modeBadgeColor = effectiveMode === 'agent' ? '#89b4fa' : effectiveMode === 'engineer' ? '#a6e3a1' : effectiveMode === 'custom' ? '#f9e2af' : '#6c7086'

  return (
    <div style={{ padding: '1rem', fontFamily: 'sans-serif', background: '#313244', color: '#cdd6f4', width: panelWidth || 280, minWidth: 200, maxWidth: 500, flexShrink: 0, overflowY: 'auto', height: '100%' }}>
      {/* Mode badge */}
      {modeBadge && (
        <div style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: '0.3rem',
          marginBottom: '0.5rem',
          padding: '0.2rem 0.5rem',
          borderRadius: '4px',
          background: modeBadgeColor + '22',
          border: `1px solid ${modeBadgeColor}`,
          color: modeBadgeColor,
          fontSize: '0.78rem',
          fontWeight: 600,
        }}>
          {modeBadge}
          {isModeLocked && <span style={{ marginLeft: '0.15rem', opacity: 0.7, fontSize: '0.7rem' }}>(locked)</span>}
        </div>
      )}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.75rem' }}>
        <h3 style={{ margin: 0 }}>Config</h3>
        <button
          className="btn btn-accent"
          style={{ fontSize: '0.8rem', padding: '0.25rem 0.75rem' }}
          onClick={() => {
            // Save-as-Default never carries session_permissions (disk-pure split).
            const { session_permissions, ...defaultsPayload } = draft;
            sendCommand('set_default_config', { config: defaultsPayload });
            setDefaultSaved('pending');
          }}
        >
          {defaultSaved === 'pending' ? 'Saving…' : defaultSaved === 'error' ? '✗ Save failed' : defaultSaved === true ? '✓ Default saved!' : 'Save as Default'}
        </button>
      </div>

      {/* Tab bar */}
      <div style={{ display: 'flex', gap: '0.25rem', marginBottom: '1rem', borderBottom: '1px solid #45475a', paddingBottom: '0.5rem' }}>
        {TAB_KEYS.map((tab) => (
          <button key={tab} onClick={() => setActiveTab(tab)} style={tabStyle(tab)}>
            {TAB_LABELS[tab]}
          </button>
        ))}
      </div>

      {/* ── Workspace Tab ── */}
      {activeTab === 'workspace' && (
        <div>
          {/* ── Workspace Path (read-only) ── */}
          <div style={{ marginBottom: '1rem' }}>
            <label style={labelStyle}><strong>Workspace Path</strong></label>
            <div style={{
              marginTop: '0.3rem',
              padding: '0.4rem 0.6rem',
              background: '#1e1e2e',
              borderRadius: '4px',
              color: '#cdd6f4',
              fontSize: '0.85rem',
              fontFamily: 'monospace',
              wordBreak: 'break-all',
            }}>
              {draft.workspace_path
                ? draft.workspace_path
                : <span style={{ color: '#f38ba8', fontWeight: 'bold' }}>⚠️ No workspace — session is unbound. Set a workspace.</span>}
            </div>
          </div>

          <WorkspacePanel workspaceId={workspaceId} sessionId={sessionId} selectedWorker={selectedWorker} onSelectWorker={onSelectWorker} isActive={isActive} effectivePermissions={sessionPerms?.effective ?? null} />
        </div>
      )}

      {/* ── General Tab ──────────────────────────────────────────────── */}
      {activeTab === 'general' && (
        <div>


          <div style={{ marginBottom: '1rem' }}>
            <label style={labelStyle}><strong>Temperature:</strong> {draft.temperature}</label>
            <input
              type="range" min="0" max="2" step="0.1"
              value={draft.temperature}
              onChange={(e) => updateDraft({ ...draft, temperature: parseFloat(e.target.value) })}
              style={{ width: '100%' }}
            />
          </div>

          <div style={{ marginBottom: '1rem' }}>
            <label style={labelStyle}><strong>Max Turns</strong></label>
            <input
              type="number" min="1" max="150"
              value={draft.max_turns}
              onChange={(e) => updateDraft({ ...draft, max_turns: parseInt(e.target.value, 10) || 1 })}
              style={inputStyle}
            />
          </div>



          <div style={{ marginBottom: '1rem' }}>
            <label style={labelStyle}><strong>Critical Threshold</strong> <span style={{ color: '#6c7086', fontSize: '0.75rem' }}>(tokens, warning is 15k below)</span></label>
            <input
              type="number" min="0"
              value={draft.token_monitor_critical_threshold}
              onChange={(e) => {
                const critical = parseInt(e.target.value, 10) || 0;
                updateDraft({
                  ...draft,
                  token_monitor_critical_threshold: critical,
                  token_monitor_warning_threshold: Math.max(critical - 15000, 0),
                });
              }}
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

          {/* Manage Providers button */}
          <div style={{ marginTop: '0.75rem' }}>
            <button
              onClick={() => setShowManageProviders(true)}
              style={{
                background: '#45475a',
                color: '#89b4fa',
                border: '1px solid #89b4fa',
                borderRadius: '4px',
                padding: '0.35rem 0.75rem',
                cursor: 'pointer',
                fontSize: '0.8rem',
                fontWeight: 500,
                width: '100%',
              }}
            >⚙ Manage Providers...</button>
          </div>

          {/* Manage Providers Modal */}
          {showManageProviders && (
            <ManageProvidersModal
              providers={providers}
              sendCommand={sendCommand}
              onClose={() => setShowManageProviders(false)}
              onProviderSaved={() => setProviderVersion(v => v + 1)}
            />
          )}
        </div>
      )}

      {/* ── Tools Tab ────────────────────────────────────────────────── */}
      {activeTab === 'tools' && (
        <div>
          {isModeLocked && (
            <div style={{
              background: 'rgba(249,226,175,0.1)',
              border: '1px solid rgba(249,226,175,0.3)',
              borderRadius: '4px',
              padding: '0.5rem 0.6rem',
              marginBottom: '0.75rem',
              color: '#f9e2af',
              fontSize: '0.8rem',
            }}>
              Tools are locked in {effectiveMode === 'agent' ? 'Agent' : 'Engineer'} mode.
              Switch to Custom mode to enable tool configuration.
            </div>
          )}
          {/* ── Tool Output Token Limit (above tool checkboxes) ───── */}
          <div style={{ marginBottom: '1rem' }}>
            <label style={labelStyle} htmlFor="tool_output_token_limit">
              <strong>Tool Output Token Limit</strong>
            </label>
            <input
              id="tool_output_token_limit"
              type="number"
              min="0"
              step="100"
              disabled={isModeLocked}
              style={{
                ...inputStyle,
                width: '100%',
                marginTop: '0.3rem',
                opacity: isModeLocked ? 0.5 : 1,
              }}
              value={draft.tool_output_token_limit ?? 10000}
              onChange={(e) => {
                const val = e.target.value === '' ? null : parseInt(e.target.value, 10);
                updateDraft({ ...draft, tool_output_token_limit: val });
              }}
              placeholder="Default: 10000"
            />
            <small style={{ color: '#6c7086', fontSize: '0.75rem', marginTop: '0.25rem', display: 'block' }}>
              Maximum tokens per tool output. 0 or empty = no limit.
            </small>
          </div>
          <div style={{ borderTop: '1px solid #45475a', paddingTop: '0.75rem' }}>
            {(() => {
              /* Locked modes show allTools (mode presets); custom shows availableTools */
              const displayTools = isModeLocked ? allTools : availableTools;
              return (
                <>
                  <label style={labelStyle}><strong>Tools ({displayTools.length} total)</strong></label>
                  {displayTools.length > 0 ? (
                    displayTools.map((tool) => {
                      const toolName = typeof tool === 'string' ? tool : tool?.name;
                      const toolConfig = draft.tools?.find(t => t.name === toolName);
                      const enabled = toolConfig ? toolConfig.enabled : false;
                return (
                  <div key={toolName} style={{ marginBottom: '0.35rem' }}>
                    <label style={{ cursor: isModeLocked ? 'default' : 'pointer', display: 'flex', alignItems: 'center', gap: '0.4rem', fontSize: '0.85rem', opacity: isModeLocked ? 0.6 : 1 }}>
                      <input
                        type="checkbox"
                        checked={enabled}
                        disabled={isModeLocked}
                        onChange={(e) => {
                          const updatedTools = draft.tools?.map(t =>
                            t.name === toolName ? { ...t, enabled: e.target.checked } : t
                          ) || [];
                          if (!updatedTools.find(t => t.name === toolName)) {
                            updatedTools.push({ name: toolName, enabled: e.target.checked });
                          }
                          updateDraft({ ...draft, tools: updatedTools });
                        }}
                      />
                      {toolName}
                      {isModeLocked && <span style={{ color: '#6c7086', fontSize: '0.7rem', marginLeft: '0.25rem' }}>(read-only)</span>}
                    </label>
                  </div>
                );
              })
            ) : (
              <div style={{ color: '#6c7086', fontSize: '0.8rem', fontStyle: 'italic', padding: '0.5rem 0' }}>
                Loading tool list...
              </div>
            )}
                </>
              );
            })()}
          </div>
        </div>
      )}

      {/* ── Permissions Tab ───────────────────────────────────────────── */}
      {activeTab === 'permissions' && (
        <div>
          {permsLoadError && (
            <div style={{ color: '#f9e2af', fontSize: '0.75rem', fontStyle: 'italic', marginBottom: '0.5rem' }}>
              ⚠ {permsLoadError}
            </div>
          )}
          {sessionPerms?.effective && (
            <div style={{ marginBottom: '0.75rem', padding: '0.4rem 0.6rem', background: '#1e1e2e', border: '1px solid #45475a', borderRadius: '4px', fontSize: '0.75rem', color: '#a6adc8' }}>
              <strong>Effective profile (enforced):</strong>{' '}
              {(() => {
                const e = sessionPerms.effective;
                const pairs = [
                  ['git', e.git],
                  ['filesystem', e.filesystem],
                  ['container', e.container],
                  ['network', e.network],
                  ['mcp', e.mcp],
                  ['host_bash', e.host_bash],
                ];
                return pairs
                  .filter(([, v]) => v !== undefined && v !== null)
                  .map(([k, v]) => `${k}: ${typeof v === 'boolean' ? (v ? 'Enabled' : 'Disabled') : permissionOptionLabel(String(v))}`)
                  .join(' · ');
              })()}
            </div>
          )}
          <div style={{ marginBottom: '1rem' }}>
            <label style={labelStyle}><strong>Git</strong></label>
            <select
              value={permsRaw.git ?? CANONICAL_PERMISSION_DEFAULTS.git}
              onChange={(e) => handlePermissionChange('git', e.target.value)}
              style={inputStyle}
            >
              {SESSION_PERMISSION_OPTION_ORDER.git.map((value) => (
                <option key={value} value={value} style={permissionOptionStyle}>{permissionOptionLabel(value)}</option>
              ))}
            </select>
            <small style={{ color: '#6c7086', fontSize: '0.75rem', marginTop: '0.25rem', display: 'block' }}>
              Access level for Git operations. "Ask" prompts for approval on each write operation (commit, push, pull, etc.); "Write on feature branches" restricts writes to feature branches.
            </small>
          </div>

          <div style={{ marginBottom: '1rem' }}>
            <label style={labelStyle}><strong>Filesystem</strong></label>
            <select
              value={permsRaw.filesystem ?? CANONICAL_PERMISSION_DEFAULTS.filesystem}
              onChange={(e) => handlePermissionChange('filesystem', e.target.value)}
              style={inputStyle}
            >
              {SESSION_PERMISSION_OPTION_ORDER.filesystem.map((value) => (
                <option key={value} value={value} style={permissionOptionStyle}>{permissionOptionLabel(value)}</option>
              ))}
            </select>
            <small style={{ color: '#6c7086', fontSize: '0.75rem', marginTop: '0.25rem', display: 'block' }}>
              Read/write access to the workspace filesystem.
            </small>
          </div>

          <div style={{ marginBottom: '1rem' }}>
            <label style={labelStyle}><strong>Container</strong></label>
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.6rem', marginTop: '0.3rem' }}>
              <label className="toggle-switch">
                <input
                  type="checkbox"
                  checked={permsRaw.container ?? CANONICAL_PERMISSION_DEFAULTS.container}
                  onChange={(e) => handlePermissionChange('container', e.target.checked)}
                />
                <span className="toggle-slider"></span>
              </label>
              <span style={{ fontSize: '0.85rem', color: permsRaw.container ? '#a6e3a1' : '#f38ba8' }}>
                {permsRaw.container ? 'Enabled' : 'Disabled'}
              </span>
            </div>
            <small style={{ color: '#6c7086', fontSize: '0.75rem', marginTop: '0.25rem', display: 'block' }}>
              Allow container operations.
            </small>
          </div>

          <div style={{ marginBottom: '1rem' }}>
            <label style={labelStyle}><strong>Network</strong></label>
            <select
              value={permsRaw.network ?? CANONICAL_PERMISSION_DEFAULTS.network}
              onChange={(e) => handlePermissionChange('network', e.target.value)}
              style={inputStyle}
            >
              {SESSION_PERMISSION_OPTION_ORDER.network.map((value) => (
                <option key={value} value={value} style={permissionOptionStyle}>{permissionOptionLabel(value)}</option>
              ))}
            </select>
            <small style={{ color: '#6c7086', fontSize: '0.75rem', marginTop: '0.25rem', display: 'block' }}>
              Allow the agent to make network requests.
            </small>
          </div>

          <div style={{ marginBottom: '1rem' }}>
            <label style={labelStyle}><strong>MCP</strong></label>
            <select
              value={permsRaw.mcp ?? CANONICAL_PERMISSION_DEFAULTS.mcp}
              onChange={(e) => handlePermissionChange('mcp', e.target.value)}
              style={inputStyle}
            >
              {SESSION_PERMISSION_OPTION_ORDER.mcp.map((value) => (
                <option key={value} value={value} style={permissionOptionStyle}>{permissionOptionLabel(value)}</option>
              ))}
            </select>
            <small style={{ color: '#6c7086', fontSize: '0.75rem', marginTop: '0.25rem', display: 'block' }}>
              Allow the agent to connect to external MCP servers.
            </small>
          </div>

          <div style={{ marginBottom: '1rem' }}>
            <label style={labelStyle}><strong>Host Bash</strong></label>
            <select
              value={permsRaw.host_bash ?? CANONICAL_PERMISSION_DEFAULTS.host_bash}
              onChange={(e) => handlePermissionChange('host_bash', e.target.value)}
              style={inputStyle}
            >
              {SESSION_PERMISSION_OPTION_ORDER.host_bash.map((value) => (
                <option key={value} value={value} style={permissionOptionStyle}>{permissionOptionLabel(value)}</option>
              ))}
            </select>
            <small style={{ color: '#6c7086', fontSize: '0.75rem', marginTop: '0.25rem', display: 'block' }}>
              Supervised host-shell access outside the sandbox. "Ask" prompts for approval before each command.
            </small>
          </div>

          <p style={{ color: '#6c7086', fontSize: '0.8rem', fontStyle: 'italic', borderTop: '1px solid #45475a', paddingTop: '0.75rem' }}>
            Changes take effect on the next tool call.
          </p>
        </div>
      )}

      {/* ── Container Tab ─────────────────────────────────────────────── */}
      {activeTab === 'container' && (
        <ContainerPanelContent
          workspacePath={config?.workspace_path || ''}
          sendCommand={sendCommand}
          containerRebuildResult={containerRebuildResult}
          onClearRebuildResult={onClearRebuildResult}
        />
      )}

      {/* ── System Prompt Tab (merged with library) ───────────────────────────────── */}
      {activeTab === 'system_prompt' && (
        <div>
          {isModeLocked ? (
            /* ── Locked modes: factory prompt preview (read-only) ── */
            <>
              {/* Factory prompt badge */}
              <div style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: '0.3rem',
                marginBottom: '0.5rem',
                padding: '0.2rem 0.5rem',
                borderRadius: '4px',
                background: effectiveMode === 'agent' ? 'rgba(137,180,250,0.15)' : 'rgba(166,227,161,0.15)',
                border: `1px solid ${effectiveMode === 'agent' ? '#89b4fa' : '#a6e3a1'}`,
                color: effectiveMode === 'agent' ? '#89b4fa' : '#a6e3a1',
                fontSize: '0.75rem',
                fontWeight: 600,
              }}>
                {effectiveMode === 'agent' ? 'Agent' : 'Engineer'} Factory Prompt
                <span style={{ marginLeft: '0.15rem', opacity: 0.6, fontSize: '0.7rem' }}>(locked)</span>
              </div>
              {/* Read-only preview */}
              <div style={{ marginBottom: '1rem' }}>
                <label style={labelStyle}><strong>System Prompt</strong> <span style={{ color: '#6c7086', fontSize: '0.7rem' }}>(read-only — factory default for this mode)</span></label>
                <div style={{
                  ...inputStyle,
                  fontFamily: 'monospace',
                  fontSize: '0.8rem',
                  lineHeight: '1.4',
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                  maxHeight: '300px',
                  overflowY: 'auto',
                  padding: '0.5rem',
                  cursor: 'default',
                  opacity: 0.75,
                }}>
                  {draft.system_prompt || <span style={{ color: '#6c7086', fontStyle: 'italic' }}>No factory prompt loaded</span>}
                </div>
              </div>
              {/* Prompt Library — visible but marked read-only */}
              <div style={{ borderTop: '1px solid #45475a', paddingTop: '0.75rem', marginTop: '0.75rem' }}>
                <label style={labelStyle}><strong>Prompt Library</strong></label>
                <p style={{ color: '#6c7086', fontSize: '0.75rem', fontStyle: 'italic', marginTop: '0.25rem', marginBottom: '0.5rem' }}>
                  Browse prompts — switch to Custom mode to apply them.
                </p>
                <div style={{ marginTop: '0.35rem', opacity: 0.6, pointerEvents: 'none' }}>
                  <PromptLibrary onSelectPrompt={handleLoadPromptFromLibrary} />
                </div>
              </div>
            </>
          ) : (
            /* ── Custom mode: editable textarea + library ── */
            <>
              <div style={{ marginBottom: '1rem' }}>
                <label style={labelStyle}><strong>System Prompt</strong></label>
                <textarea
                  rows={6}
                  style={{ ...inputStyle, fontFamily: 'monospace', resize: 'vertical' }}
                  value={draft.system_prompt || ''}
                  onChange={(e) => updateDraft({ ...draft, system_prompt: e.target.value })}
                  placeholder="Optional system-level instructions for the agent..."
                />
              </div>
              {/* Prompt Library section — editable in Custom mode */}
              <div style={{ borderTop: '1px solid #45475a', paddingTop: '0.75rem', marginTop: '0.75rem' }}>
                <label style={labelStyle}><strong>Prompt Library</strong></label>
                <div style={{ marginTop: '0.35rem' }}>
                  <PromptLibrary onSelectPrompt={handleLoadPromptFromLibrary} />
                </div>
              </div>
            </>
          )}
        </div>
      )}

      {/* ── Advanced Tab ──────────────────────────────────────────────── */}
      {activeTab === 'advanced' && (
        <div>
          <p style={{ color: '#6c7086', fontSize: '0.85rem', fontStyle: 'italic' }}>No advanced options at this time.</p>
        </div>
      )}

      {/* ── Apply Button ─────────────────────────────────────────────── */}
      <div style={{ marginTop: '1rem', display: 'flex', flexDirection: 'column', gap: '0.4rem' }}>
        <button
          onClick={() => {
            handleApply();
          }}
          disabled={!wsConnected || isApplying}
          style={{
            background: !wsConnected ? '#585b70' : isApplying ? '#585b70' : (isDirty || permsDirty || providerVersion > 0) ? '#89b4fa' : '#45475a',
            color: !wsConnected || (!isDirty && !permsDirty && !isApplying && providerVersion === 0) ? '#6c7086' : '#1e1e2e',
            border: 'none',
            borderRadius: '4px',
            padding: '0.5rem 1.5rem',
            fontWeight: 600,
            cursor: !wsConnected || isApplying ? 'not-allowed' : 'pointer',
            width: '100%',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            gap: '0.4rem',
          }}
        >
          {isApplying && <span className="config-spinner" />}
          {isApplying ? (configQueued ? 'Queued — applying when idle…' : 'Applying…') : 'Apply'}
        </button>
        {!wsConnected && (
          <span style={{ color: '#f9e2af', fontSize: '0.8rem', fontWeight: 500 }}>
            ⚠ Reconnecting...
          </span>
        )}
        {(isDirty || permsDirty || providerVersion > 0) && !isApplying && wsConnected && (
          <span style={{ color: '#f9e2af', fontSize: '0.75rem', fontStyle: 'italic' }}>
            {isDirty ? 'Unsaved changes' : permsDirty ? 'Unsaved permission changes' : 'Provider credentials updated'}
          </span>
        )}
        {applyError && (
          <span style={{ color: '#f38ba8', fontSize: '0.8rem' }}>
            ⚠ {applyError}
          </span>
        )}
      </div>
    </div>
  );
};

export default React.memo(ConfigPanel)
