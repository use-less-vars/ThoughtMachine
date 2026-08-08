// --- tabs/SessionDefaultsTab.jsx ---
// Moved verbatim from WorkspacePanel.jsx (Phase 4 structural split).

import React, { useState } from 'react'
import { PROVIDERS, PRESET_OPTIONS } from '../workspaceUtils.jsx'

export default function SessionDefaultsTab({ workspace, update }) {
  const defaults = workspace.sessionDefaults || {}
  const [form, setForm] = useState({
    systemPrompt: defaults.systemPrompt || '',
    tokenLimit: defaults.tokenLimit ?? 8000,
    temperature: defaults.temperature ?? 0.7,
    maxTurns: defaults.maxTurns ?? 20,
    toolOutputTokenLimit: defaults.toolOutputTokenLimit ?? 2000,
    allowedProviders: defaults.allowedProviders || [],
    defaultPreset: defaults.defaultPreset || 'balanced',
  })
  const [saved, setSaved] = useState(false)

  const commit = (next) => {
    setForm(next)
    update('sessionDefaults', next)
  }
  const commitField = (field, value) => commit({ ...form, [field]: value })

  const toggleProvider = (provider) => {
    const has = form.allowedProviders.includes(provider)
    const next = has
      ? form.allowedProviders.filter((p) => p !== provider)
      : [...form.allowedProviders, provider]
    commitField('allowedProviders', next)
  }

  const handleSave = () => {
    update('sessionDefaults', form)
    setSaved(true)
    window.setTimeout(() => setSaved(false), 1500)
  }

  // Show the current value even when it is not one of the standard options
  // (the store seeds defaultPreset: 'balanced').
  const presetOptions = PRESET_OPTIONS.includes(form.defaultPreset)
    ? PRESET_OPTIONS
    : [form.defaultPreset, ...PRESET_OPTIONS]

  return (
    <div className="wp-section">
      <div className="wp-section-header">
        <h3>Session Defaults</h3>
      </div>
      <div className="wp-form">
        <label className="wp-field wp-field-full">
          <span>System prompt</span>
          <textarea
            className="wp-textarea"
            rows={5}
            value={form.systemPrompt}
            onChange={(e) => setForm({ ...form, systemPrompt: e.target.value })}
            onBlur={(e) => commitField('systemPrompt', e.target.value)}
          />
        </label>

        <label className="wp-field">
          <span>Token limit</span>
          <input
            className="wp-input"
            type="number"
            min={1}
            value={form.tokenLimit}
            onChange={(e) => setForm({ ...form, tokenLimit: Number(e.target.value) || 0 })}
            onBlur={(e) => commitField('tokenLimit', Number(e.target.value) || 0)}
          />
        </label>

        <label className="wp-field">
          <span>Temperature — {form.temperature}</span>
          <input
            className="wp-slider"
            type="range"
            min={0}
            max={2}
            step={0.1}
            value={form.temperature}
            onChange={(e) => setForm({ ...form, temperature: Number(e.target.value) })}
            onBlur={(e) => commitField('temperature', Number(e.target.value))}
          />
        </label>

        <label className="wp-field">
          <span>Max turns</span>
          <input
            className="wp-input"
            type="number"
            min={1}
            max={200}
            value={form.maxTurns}
            onChange={(e) => setForm({ ...form, maxTurns: Number(e.target.value) || 0 })}
            onBlur={(e) => commitField('maxTurns', Number(e.target.value) || 0)}
          />
        </label>

        <label className="wp-field">
          <span>Tool output token limit</span>
          <input
            className="wp-input"
            type="number"
            min={1}
            value={form.toolOutputTokenLimit}
            onChange={(e) => setForm({ ...form, toolOutputTokenLimit: Number(e.target.value) || 0 })}
            onBlur={(e) => commitField('toolOutputTokenLimit', Number(e.target.value) || 0)}
          />
        </label>

        <div className="wp-field wp-field-full">
          <span>Allowed providers</span>
          <div className="wp-checkboxes">
            {PROVIDERS.map((p) => (
              <label key={p} className="wp-checkbox">
                <input
                  type="checkbox"
                  checked={form.allowedProviders.includes(p)}
                  onChange={() => toggleProvider(p)}
                />
                <span>{p}</span>
              </label>
            ))}
          </div>
        </div>

        <label className="wp-field">
          <span>Default session preset</span>
          <select
            className="wp-select"
            value={form.defaultPreset}
            onChange={(e) => commitField('defaultPreset', e.target.value)}
          >
            {presetOptions.map((o) => (
              <option key={o} value={o}>{o}</option>
            ))}
          </select>
        </label>

        <div className="wp-form-footer">
          <button className="wp-btn wp-btn-primary" onClick={handleSave}>Save</button>
          {saved && <span className="wp-saved">Saved</span>}
        </div>
      </div>
    </div>
  )
}
