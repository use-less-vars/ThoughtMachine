/*
 * OnboardingWizard.jsx
 *
 * First-run setup wizard (Phase 3b). Mounted by App.jsx when
 * GET /api/onboarding/status reports onboarding_complete === false.
 *
 * Props:
 *   onFinished  — called once the wizard is done (skip counts as done)
 *   sendCommand — WS sender (command, payload) used ONLY for save_provider;
 *                 returns false when the hub WS is not open.
 *
 * Screens:
 *   1 Welcome  — three layer cards (Landing / Workspace / Session)
 *   2 Provider — provider type + base URL + API key + model;
 *                "Test connection" → POST /api/onboarding/test-connection;
 *                "Save & Continue" → WS save_provider → screen 3
 *   3 Workspace— name → slug → ~/workspaces/<slug>; description and
 *                host-resources are FRONTEND-ONLY state (the backend has no
 *                fields for them yet); creates the folder via
 *                POST /api/browse/create (ensuring ~/workspaces exists first,
 *                because browse/create requires an existing parent) and
 *                registers it via POST /api/workspace/resolve → screen 4
 *   4 Summary  — provider + workspace recap; Finish → POST /api/onboarding/complete
 *
 * Skip (every screen) calls POST /api/onboarding/complete then onFinished().
 * A failed skip POST is ignored so the user is never trapped in the wizard.
 */
import React, { useState } from 'react'

const BACKDROP_STYLE = {
  position: 'fixed',
  top: 0,
  left: 0,
  right: 0,
  bottom: 0,
  background: 'rgba(0, 0, 0, 0.6)',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  zIndex: 1200,
}

const CARD_STYLE = {
  background: '#313244',
  border: '1px solid #585b70',
  borderRadius: '8px',
  padding: '1.5rem',
  width: '560px',
  maxHeight: '88vh',
  display: 'flex',
  flexDirection: 'column',
  boxShadow: '0 4px 20px rgba(0, 0, 0, 0.4)',
  overflowY: 'auto',
}

const TITLE_STYLE = { margin: 0, fontSize: '1.25rem', color: '#cdd6f4', fontWeight: 600 }
const SUBTITLE_STYLE = { margin: '0.25rem 0 1rem', fontSize: '0.85rem', color: '#a6adc8' }
const LABEL_STYLE = { display: 'block', fontSize: '0.8rem', color: '#a6adc8', marginBottom: '0.25rem', fontWeight: 500 }
const INPUT_STYLE = {
  width: '100%',
  padding: '0.45rem 0.55rem',
  borderRadius: '4px',
  border: '1px solid #585b70',
  background: '#1e1e2e',
  color: '#cdd6f4',
  fontSize: '0.85rem',
  boxSizing: 'border-box',
}
const PRIMARY_BUTTON = {
  background: '#89b4fa',
  color: '#1e1e2e',
  border: 'none',
  borderRadius: '4px',
  padding: '0.5rem 0.9rem',
  fontSize: '0.85rem',
  fontWeight: 600,
  cursor: 'pointer',
}
const SECONDARY_BUTTON = {
  background: '#45475a',
  color: '#cdd6f4',
  border: '1px solid #585b70',
  borderRadius: '4px',
  padding: '0.5rem 0.9rem',
  fontSize: '0.85rem',
  cursor: 'pointer',
}
const GHOST_BUTTON = {
  background: 'transparent',
  color: '#a6adc8',
  border: 'none',
  borderRadius: '4px',
  padding: '0.5rem 0.6rem',
  fontSize: '0.8rem',
  cursor: 'pointer',
  textDecoration: 'underline',
}
const ERROR_STYLE = { background: '#f38ba8', color: '#1e1e2e', padding: '0.4rem 0.6rem', borderRadius: '4px', fontSize: '0.8rem', marginTop: '0.75rem' }
const OK_STYLE = { background: '#a6e3a1', color: '#1e1e2e', padding: '0.4rem 0.6rem', borderRadius: '4px', fontSize: '0.8rem', marginTop: '0.75rem' }
const ROW_STYLE = { display: 'flex', gap: '0.5rem', alignItems: 'flex-end' }

const PROVIDER_OPTIONS = [
  { value: 'openai', label: 'OpenAI' },
  { value: 'openai_compatible', label: 'OpenAI Compatible' },
  { value: 'anthropic', label: 'Anthropic' },
]

// Prefill for each provider type. openai_compatible has no default — the
// user must supply a base URL (it is validated as required).
const DEFAULT_BASE_URLS = {
  openai: 'https://api.openai.com/v1',
  openai_compatible: '',
  anthropic: 'https://api.anthropic.com',
}

const LAYER_CARDS = [
  { title: 'Landing', text: 'ThoughtMachine runs as a desktop app on your machine, serving a local web UI from the system tray.' },
  { title: 'Workspace', text: 'Workspaces are folders on disk that confine agents — each with its own permissions, tools and session history.' },
  { title: 'Session', text: 'Sessions are chat conversations inside a workspace where agents run with access to its resources.' },
]

function slugify(name) {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '')
}

async function parseError(res) {
  try {
    const data = await res.json()
    return data.error || data.detail || `Request failed (${res.status})`
  } catch {
    return `Request failed (${res.status})`
  }
}

function SummaryRow({ label, value }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: '1rem', fontSize: '0.8rem' }}>
      <span style={{ color: '#a6adc8' }}>{label}</span>
      <span style={{ color: '#cdd6f4', fontWeight: 600, textAlign: 'right' }}>{value}</span>
    </div>
  )
}

export default function OnboardingWizard({ onFinished, sendCommand }) {
  const [step, setStep] = useState(1)

  // Provider screen state
  const [provider, setProvider] = useState('openai_compatible')
  const [baseUrl, setBaseUrl] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [showKey, setShowKey] = useState(false)
  const [model, setModel] = useState('')
  const [testState, setTestState] = useState(null) // { kind: 'ok' | 'error', message }
  const [savingProvider, setSavingProvider] = useState(false)

  // Workspace screen state
  const [workspaceName, setWorkspaceName] = useState('')
  const [workspaceDescription, setWorkspaceDescription] = useState('')
  const [hostResources, setHostResources] = useState(false)
  const [creatingWorkspace, setCreatingWorkspace] = useState(false)

  // Finish state
  const [finishing, setFinishing] = useState(false)

  const [error, setError] = useState(null)

  const slug = slugify(workspaceName)
  const workspacePath = `~/workspaces/${slug}`
  const providerLabel = (PROVIDER_OPTIONS.find((o) => o.value === provider) || {}).label || provider

  const changeProvider = (value) => {
    setProvider(value)
    // Overwrite the prefill only when the field is empty or still a known
    // default — never clobber a URL the user typed themselves.
    setBaseUrl((prev) => {
      const knownDefaults = Object.values(DEFAULT_BASE_URLS).filter(Boolean)
      if (!prev || knownDefaults.includes(prev)) return DEFAULT_BASE_URLS[value]
      return prev
    })
    setError(null)
    setTestState(null)
  }

  const testConnection = async () => {
    setError(null)
    if (!apiKey.trim()) {
      setTestState({ kind: 'error', message: 'Enter an API key first.' })
      return
    }
    if (provider === 'openai_compatible' && !baseUrl.trim()) {
      setTestState({ kind: 'error', message: 'A base URL is required for OpenAI-compatible providers.' })
      return
    }
    setTestState(null)
    try {
      const res = await fetch('/api/onboarding/test-connection', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          provider,
          api_key: apiKey.trim(),
          base_url: baseUrl.trim() || undefined,
          model: model.trim() || undefined,
        }),
      })
      const data = await res.json().catch(() => ({}))
      if (data.ok) {
        setTestState({ kind: 'ok', message: 'Connection successful' })
      } else {
        setTestState({ kind: 'error', message: data.error || 'Connection failed' })
      }
    } catch (err) {
      setTestState({ kind: 'error', message: err.message || 'Connection failed' })
    }
  }

  const saveProvider = () => {
    setError(null)
    if (provider === 'openai_compatible' && !baseUrl.trim()) {
      setError('A base URL is required for OpenAI-compatible providers.')
      return
    }
    setSavingProvider(true)
    const sent = sendCommand('save_provider', {
      provider: {
        id: provider,
        label: providerLabel,
        provider_type: provider,
        base_url: baseUrl.trim(),
        api_key: apiKey.trim(),
        default_model: model.trim() || '',
        models: [],
        timeout: 120,
      },
    })
    setSavingProvider(false)
    if (sent === false) {
      setError('Backend connection is not ready — the provider was not saved. Try again in a moment.')
      return
    }
    setStep(3)
  }

  const createWorkspace = async () => {
    setError(null)
    if (!workspaceName.trim()) {
      setError('Enter a workspace name.')
      return
    }
    if (!slug) {
      setError('Workspace name must contain at least one letter or number.')
      return
    }
    setCreatingWorkspace(true)
    try {
      // Ensure ~/workspaces exists first — browse/create requires an
      // existing parent directory. An "Already exists" result is fine.
      const parentRes = await fetch('/api/browse/create', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ parent_path: '~', name: 'workspaces' }),
      })
      const parentData = await parentRes.json().catch(() => ({}))
      if (!parentData.success && !String(parentData.error || '').includes('Already exists')) {
        setError(parentData.error || 'Could not create ~/workspaces')
        return
      }

      const dirRes = await fetch('/api/browse/create', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ parent_path: '~/workspaces', name: slug }),
      })
      const dirData = await dirRes.json().catch(() => ({}))
      if (!dirData.success && !String(dirData.error || '').includes('Already exists')) {
        setError(dirData.error || `Could not create ${workspacePath}`)
        return
      }

      const resolveRes = await fetch('/api/workspace/resolve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: workspacePath }),
      })
      const resolveData = await resolveRes.json().catch(() => ({}))
      if (!resolveRes.ok) {
        setError(resolveData.error || resolveData.detail || `Could not register ${workspacePath} (HTTP ${resolveRes.status})`)
        return
      }
      setStep(4)
    } catch (err) {
      setError(err.message || 'Could not create the workspace')
    } finally {
      setCreatingWorkspace(false)
    }
  }

  const completeOnboarding = async () => {
    const res = await fetch('/api/onboarding/complete', { method: 'POST' })
    if (!res.ok) {
      throw new Error(await parseError(res))
    }
    const data = await res.json().catch(() => ({}))
    return data.onboarding_complete !== false
  }

  const handleFinish = async () => {
    setError(null)
    setFinishing(true)
    try {
      await completeOnboarding()
      onFinished()
    } catch (err) {
      setError(err.message || 'Could not complete onboarding — try again.')
    } finally {
      setFinishing(false)
    }
  }

  const handleSkip = async () => {
    // Skipping counts as completed. A failed POST is ignored so the user is
    // never trapped in the wizard (the marker is retried on next launch).
    try {
      await fetch('/api/onboarding/complete', { method: 'POST' })
    } catch {
      // ignore
    }
    onFinished()
  }

  return (
    <div style={BACKDROP_STYLE} role="dialog" aria-modal="true" aria-label="First-run setup">
      <div style={CARD_STYLE}>
        {step === 1 && (
          <>
            <h1 style={TITLE_STYLE}>Welcome to ThoughtMachine</h1>
            <p style={SUBTITLE_STYLE}>Step 1 of 4 — a few quick steps to get your agent harness ready.</p>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '0.6rem', margin: '0.5rem 0 1rem' }}>
              {LAYER_CARDS.map((card) => (
                <div key={card.title} style={{ background: '#1e1e2e', border: '1px solid #45475a', borderRadius: '6px', padding: '0.7rem 0.85rem' }}>
                  <div style={{ fontSize: '0.8rem', fontWeight: 600, color: '#89b4fa', marginBottom: '0.2rem' }}>{card.title}</div>
                  <div style={{ fontSize: '0.8rem', color: '#a6adc8', lineHeight: 1.4 }}>{card.text}</div>
                </div>
              ))}
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 'auto' }}>
              <button style={GHOST_BUTTON} onClick={handleSkip} aria-label="Skip onboarding">Skip</button>
              <button style={PRIMARY_BUTTON} onClick={() => setStep(2)} aria-label="Get started">Get Started</button>
            </div>
          </>
        )}

        {step === 2 && (
          <>
            <h1 style={TITLE_STYLE}>Configure your LLM provider</h1>
            <p style={SUBTITLE_STYLE}>Step 2 of 4 — connect the model that powers your agents.</p>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
              <div>
                <label style={LABEL_STYLE} htmlFor="onboarding-provider">Provider</label>
                <select
                  id="onboarding-provider"
                  style={INPUT_STYLE}
                  value={provider}
                  onChange={(e) => changeProvider(e.target.value)}
                  aria-label="Provider"
                >
                  {PROVIDER_OPTIONS.map((opt) => (
                    <option key={opt.value} value={opt.value}>{opt.label}</option>
                  ))}
                </select>
              </div>
              <div>
                <label style={LABEL_STYLE} htmlFor="onboarding-base-url">
                  Base URL {provider === 'openai_compatible' && <span style={{ color: '#f38ba8' }}> *</span>}
                </label>
                <input
                  id="onboarding-base-url"
                  style={INPUT_STYLE}
                  type="text"
                  value={baseUrl}
                  onChange={(e) => setBaseUrl(e.target.value)}
                  placeholder={provider === 'openai_compatible' ? 'https://your-endpoint.example/v1' : 'Optional — a default is used when empty'}
                  aria-label="Base URL"
                />
              </div>
              <div>
                <label style={LABEL_STYLE} htmlFor="onboarding-api-key">API key</label>
                <div style={ROW_STYLE}>
                  <input
                    id="onboarding-api-key"
                    style={INPUT_STYLE}
                    type={showKey ? 'text' : 'password'}
                    value={apiKey}
                    onChange={(e) => setApiKey(e.target.value)}
                    placeholder="sk-…"
                    aria-label="API key"
                  />
                  <button
                    type="button"
                    style={{ background: '#45475a', border: '1px solid #585b70', borderRadius: '4px', color: '#a6adc8', padding: '0.4rem 0.5rem', cursor: 'pointer', fontSize: '0.75rem' }}
                    onClick={() => setShowKey((v) => !v)}
                    aria-label={showKey ? 'Hide API key' : 'Show API key'}
                  >
                    {showKey ? 'Hide' : 'Show'}
                  </button>
                </div>
              </div>
              <div>
                <label style={LABEL_STYLE} htmlFor="onboarding-model">Default model (optional)</label>
                <input
                  id="onboarding-model"
                  style={INPUT_STYLE}
                  type="text"
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                  placeholder="e.g. gpt-4o"
                  aria-label="Default model"
                />
              </div>
            </div>

            <div style={{ display: 'flex', marginTop: '0.75rem' }}>
              <button style={SECONDARY_BUTTON} onClick={testConnection} aria-label="Test connection" disabled={savingProvider}>Test connection</button>
            </div>

            {testState && (
              <div style={testState.kind === 'ok' ? OK_STYLE : ERROR_STYLE} role={testState.kind === 'ok' ? 'status' : 'alert'}>
                {testState.message}
              </div>
            )}
            {error && (
              <div style={ERROR_STYLE} role="alert">{error}</div>
            )}

            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 'auto' }}>
              <button style={GHOST_BUTTON} onClick={handleSkip} aria-label="Skip onboarding">Skip</button>
              <div style={{ display: 'flex', gap: '0.5rem' }}>
                <button style={SECONDARY_BUTTON} onClick={() => { setStep(1); setError(null) }}>Back</button>
                <button style={PRIMARY_BUTTON} onClick={saveProvider} aria-label="Save provider and continue" disabled={savingProvider}>
                  {savingProvider ? 'Saving…' : 'Save & Continue'}
                </button>
              </div>
            </div>
          </>
        )}

        {step === 3 && (
          <>
            <h1 style={TITLE_STYLE}>Create your first workspace</h1>
            <p style={SUBTITLE_STYLE}>Step 3 of 4 — workspaces confine agents to a folder on disk.</p>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
              <div>
                <label style={LABEL_STYLE} htmlFor="onboarding-workspace-name">Workspace name</label>
                <input
                  id="onboarding-workspace-name"
                  style={INPUT_STYLE}
                  type="text"
                  value={workspaceName}
                  onChange={(e) => setWorkspaceName(e.target.value)}
                  placeholder="e.g. My Project"
                  aria-label="Workspace name"
                />
                {workspaceName.trim() !== '' && (
                  <div style={{ fontSize: '0.75rem', color: '#a6adc8', marginTop: '0.3rem' }}>
                    Will be created at <span style={{ color: '#89b4fa' }}>{workspacePath}</span>
                  </div>
                )}
              </div>
              <div>
                <label style={LABEL_STYLE} htmlFor="onboarding-workspace-description">Description (optional)</label>
                <input
                  id="onboarding-workspace-description"
                  style={INPUT_STYLE}
                  type="text"
                  value={workspaceDescription}
                  onChange={(e) => setWorkspaceDescription(e.target.value)}
                  placeholder="What is this workspace for?"
                  aria-label="Workspace description"
                />
              </div>
              <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', fontSize: '0.8rem', color: '#cdd6f4', cursor: 'pointer' }}>
                <input
                  type="checkbox"
                  checked={hostResources}
                  onChange={(e) => setHostResources(e.target.checked)}
                  aria-label="Allow host resource access"
                />
                Allow agents to access host resources
              </label>
              <div style={{ fontSize: '0.7rem', color: '#6c7086' }}>
                Description and host-resource access are frontend-only for now — they are not persisted yet.
              </div>
            </div>

            {error && (
              <div style={ERROR_STYLE} role="alert">{error}</div>
            )}

            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 'auto' }}>
              <button style={GHOST_BUTTON} onClick={handleSkip} aria-label="Skip onboarding">Skip</button>
              <div style={{ display: 'flex', gap: '0.5rem' }}>
                <button style={SECONDARY_BUTTON} onClick={() => { setStep(2); setError(null) }}>Back</button>
                <button style={PRIMARY_BUTTON} onClick={createWorkspace} aria-label="Create workspace" disabled={creatingWorkspace}>
                  {creatingWorkspace ? 'Creating…' : 'Create Workspace'}
                </button>
              </div>
            </div>
          </>
        )}

        {step === 4 && (
          <>
            <h1 style={TITLE_STYLE}>You&apos;re all set</h1>
            <p style={SUBTITLE_STYLE}>Step 4 of 4 — here&apos;s what was configured.</p>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '0.6rem', background: '#1e1e2e', border: '1px solid #45475a', borderRadius: '6px', padding: '0.85rem', margin: '0.5rem 0 0.75rem' }}>
              <SummaryRow label="Provider" value={providerLabel} />
              <SummaryRow label="Model" value={model.trim() || 'No default model'} />
              <SummaryRow label="Workspace" value={workspacePath} />
              <SummaryRow label="Host resources" value={hostResources ? 'On' : 'Off'} />
            </div>

            {error && (
              <div style={ERROR_STYLE} role="alert">{error}</div>
            )}

            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 'auto' }}>
              <button style={GHOST_BUTTON} onClick={handleSkip} aria-label="Skip onboarding">Skip</button>
              <div style={{ display: 'flex', gap: '0.5rem' }}>
                <button style={SECONDARY_BUTTON} onClick={() => { setStep(3); setError(null) }}>Back</button>
                <button style={PRIMARY_BUTTON} onClick={handleFinish} aria-label="Finish onboarding" disabled={finishing}>
                  {finishing ? 'Finishing…' : 'Finish'}
                </button>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
