/*
 * LoggingPanel.jsx
 *
 * Sidebar panel for viewing and modifying runtime logging configuration.
 * Fetches config via REST API and allows editing with a save/apply pattern.
 *
 * Props:
 *   config         — current logging config object (null while loading)
 *   configError    — error string if fetch failed (null otherwise)
 *   onRetry        — function to retry fetching config
 *   onSaveConfig   — async function(configPayload) => updatedConfig
 *   onClose        — called when panel close button is clicked
 */

import React, { useState, useCallback, useRef, useEffect } from 'react'

// ── Helpers ─────────────────────────────────────────────────────────────

const TRUNCATION_LABELS = {
  message: 'Max message length',
  context: 'Max context length',
  tool_input: 'Max tool input',
  tool_output: 'Max tool output',
  thought: 'Max thought length',
  metadata: 'Max metadata length',
}

function tagMatchesPattern(tag, pattern) {
  if (pattern === '*') return true
  if (pattern.endsWith('.*')) {
    const prefix = pattern.slice(0, -2)
    return tag === prefix || tag.startsWith(prefix + '.')
  }
  return tag === pattern
}

function isTagEnabled(tag, patterns) {
  if (!Array.isArray(patterns)) return false
  return patterns.some(p => tagMatchesPattern(tag, p))
}

function parseTagGroups(availableTags) {
  if (!Array.isArray(availableTags) || availableTags.length === 0) return {}
  const groups = {}
  for (const tag of availableTags) {
    const dotIdx = tag.indexOf('.')
    const group = dotIdx === -1 ? '_other' : tag.slice(0, dotIdx)
    if (!groups[group]) groups[group] = []
    groups[group].push(tag)
  }
  return groups
}

// ── Component ───────────────────────────────────────────────────────────

export default function LoggingPanel({ config, onSaveConfig, onClose, configError, onRetry }) {
  const [localConfig, setLocalConfig] = useState(null)
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState(null)
  const [showRaw, setShowRaw] = useState(false)

  // Initialise localConfig from config prop
  useEffect(() => {
    if (config) {
      setLocalConfig(prev => {
        const merged = {
          log_level: config.log_level || 'INFO',
          log_tags: Array.isArray(config.log_tags) ? [...config.log_tags] : [],
          truncation_limits: config.truncation_limits ? { ...config.truncation_limits } : {},
          available_tags: Array.isArray(config.available_tags) ? [...config.available_tags] : [],
          tag_levels: config.tag_levels ? { ...config.tag_levels } : {},
          ...config,
        }
        return merged
      })
    }
  }, [config])

  // ── Handlers ─────────────────────────────────────────────────────────

  const handleLevelChange = useCallback((e) => {
    const value = e.target.value
    setLocalConfig(prev => prev ? { ...prev, log_level: value } : prev)
    setDirty(true)
  }, [])

  const handleTagsInputChange = useCallback((e) => {
    const value = e.target.value
    const patterns = value.split(',').map(s => s.trim()).filter(Boolean)
    setLocalConfig(prev => prev ? { ...prev, log_tags: patterns } : prev)
    setDirty(true)
  }, [])

  const handleTagToggle = useCallback((tag) => {
    setLocalConfig(prev => {
      if (!prev) return prev
      const patterns = Array.isArray(prev.log_tags) ? [...prev.log_tags] : []
      if (isTagEnabled(tag, patterns)) {
        const filtered = patterns.filter(p => p !== tag && !(p.endsWith('.*') && tag.startsWith(p.slice(0, -2))))
        return { ...prev, log_tags: filtered }
      } else {
        if (!patterns.includes(tag)) {
          patterns.push(tag)
        }
        return { ...prev, log_tags: patterns }
      }
    })
    setDirty(true)
  }, [])

  const handleTagLevelChange = useCallback((tag, level) => {
    setLocalConfig(prev => {
      if (!prev) return prev
      const tagLevels = { ...(prev.tag_levels || {}) }
      if (level === 'OFF' || level === 'INHERIT') {
        delete tagLevels[tag]
      } else {
        tagLevels[tag] = level
      }
      return { ...prev, tag_levels: tagLevels }
    })
    setDirty(true)
  }, [])

  const handleTruncationChange = useCallback((key, value) => {
    setLocalConfig(prev => {
      if (!prev) return prev
      const limits = { ...(prev.truncation_limits || {}) }
      limits[key] = parseInt(value, 10) || 0
      return { ...prev, truncation_limits: limits }
    })
    setDirty(true)
  }, [])

  const handleSave = useCallback(async () => {
    if (!localConfig) return
    setSaving(true)
    setSaveError(null)
    try {
      const payload = {
        log_level: localConfig.log_level,
        log_tags: localConfig.log_tags || [],
        truncation_limits: localConfig.truncation_limits || {},
        tag_levels: localConfig.tag_levels || {},
      }
      if (onSaveConfig) {
        await onSaveConfig(payload)
      }
      setDirty(false)
    } catch (err) {
      setSaveError(err.message || 'Failed to save configuration')
    } finally {
      setSaving(false)
    }
  }, [localConfig, onSaveConfig])

  // ── Derived ──────────────────────────────────────────────────────────

  const tagGroups = localConfig ? parseTagGroups(localConfig.available_tags) : {}
  const logLevel = localConfig?.log_level || ''
  const logTagPatterns = Array.isArray(localConfig?.log_tags) ? localConfig.log_tags : []
  const truncationLimits = localConfig?.truncation_limits || {}
  const tagLevels = localConfig?.tag_levels || {}

  const tagsInputValue = logTagPatterns.join(', ')

  // ── Loading / Error states ──────────────────────────────────────────

  if (!config && !localConfig) {
    return (
      <div className="logging-panel">
        <div className="logging-panel-header">
          <span>Logging</span>
          <button className="logging-panel-close" onClick={onClose}>✕</button>
        </div>
        {configError ? (
          <div style={{ padding: '1rem', textAlign: 'center' }}>
            <div style={{
              color: 'var(--text-danger, #e74c3c)',
              fontSize: '0.9rem',
              marginBottom: '0.75rem',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              gap: '0.4rem',
            }}>
              <span>⚠️</span>
              <span>Logging API not available on backend</span>
            </div>
            <div style={{ fontSize: '0.8rem', color: 'var(--text-secondary, #888)', marginBottom: '0.75rem' }}>
              {configError}
            </div>
            {onRetry && (
              <button
                className="logging-retry-btn"
                onClick={onRetry}
                style={{ padding: '0.35rem 1rem', cursor: 'pointer' }}
              >
                Retry
              </button>
            )}
          </div>
        ) : (
          <div style={{
            color: 'var(--text-secondary)',
            fontStyle: 'italic',
            fontSize: '0.85rem',
            padding: '1.5rem 0',
            textAlign: 'center',
          }}>
            <span className="loading-dots">Loading configuration</span>
          </div>
        )}
      </div>
    )
  }

  // ── Main render ─────────────────────────────────────────────────────

  return (
    <div className="logging-panel">
      <div className="logging-panel-header">
        <span>Logging</span>
        <button className="logging-panel-close" onClick={onClose}>✕</button>
      </div>

      {/* Log Level */}
      <div className="logging-section">
        <div className="logging-section-title">Log Level</div>
        <div className="logging-section-desc">Set the verbosity of log output.</div>
        <select
          className="logging-select"
          value={logLevel}
          onChange={handleLevelChange}
          style={{ width: '100%' }}
        >
          <option value="DEBUG">DEBUG</option>
          <option value="INFO">INFO</option>
          <option value="WARNING">WARNING</option>
          <option value="ERROR">ERROR</option>
          <option value="CRITICAL">CRITICAL</option>
        </select>
      </div>

      {/* Tag Patterns (text input) */}
      <div className="logging-section">
        <div className="logging-section-title">Log Tags (patterns)</div>
        <div className="logging-section-desc">Comma-separated tag patterns. Use &quot;*&quot; for all tags, &quot;area.*&quot; for all in an area.</div>
        <input
          className="logging-input"
          type="text"
          value={tagsInputValue}
          placeholder="e.g., core.*, tools.*"
          onChange={handleTagsInputChange}
          style={{ width: '100%', boxSizing: 'border-box' }}
        />
      </div>

      {/* Tag Checkboxes (grouped by area) */}
      {Object.keys(tagGroups).length > 0 && (
        <div className="logging-section">
          <div className="logging-section-title">Tag Filters</div>
          <div className="logging-section-desc">Toggle individual tags on/off and set per-tag levels.</div>
          <div className="logging-tag-groups">
            {Object.entries(tagGroups).map(([group, tags]) => (
              <div key={group} className="logging-tag-group">
                <div className="logging-tag-group-header">{group}</div>
                {tags.map(tag => {
                  const enabled = isTagEnabled(tag, logTagPatterns)
                  const currentLevel = tagLevels[tag] || 'INHERIT'
                  return (
                    <div key={tag} className="logging-tag-row">
                      <label className="logging-tag-checkbox-label">
                        <input
                          type="checkbox"
                          checked={enabled}
                          onChange={() => handleTagToggle(tag)}
                        />
                        <span className="logging-tag-name" title={tag}>{tag}</span>
                      </label>
                      <select
                        className="logging-tag-level-select"
                        value={currentLevel}
                        onChange={(e) => handleTagLevelChange(tag, e.target.value)}
                        disabled={!enabled}
                        style={{ fontSize: '0.75rem', padding: '1px 2px' }}
                      >
                        <option value="INHERIT">—</option>
                        <option value="DEBUG">DEBUG</option>
                        <option value="INFO">INFO</option>
                        <option value="WARNING">WARNING</option>
                        <option value="ERROR">ERROR</option>
                        <option value="OFF">OFF</option>
                      </select>
                    </div>
                  )
                })}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Truncation Limits */}
      {Object.keys(truncationLimits).length > 0 && (
        <div className="logging-section">
          <div className="logging-section-title">Truncation Limits</div>
          <div className="logging-section-desc">Maximum character length per log field.</div>
          <div className="logging-truncation-table">
            {Object.entries(truncationLimits).map(([key, value]) => (
              <div key={key} className="logging-truncation-row">
                <span className="logging-truncation-key" title={key}>
                  {TRUNCATION_LABELS[key] || key}
                </span>
                <input
                  className="logging-truncation-value"
                  type="number"
                  min={0}
                  value={value}
                  onChange={(e) => handleTruncationChange(key, e.target.value)}
                  style={{ width: '80px' }}
                />
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Save Error */}
      {saveError && (
        <div style={{
          color: 'var(--text-danger, #e74c3c)',
          fontSize: '0.8rem',
          padding: '0.3rem 0.75rem',
          margin: '0 0.75rem',
          background: 'rgba(231, 76, 60, 0.1)',
          borderRadius: '4px',
        }}>
          Save failed: {saveError}
        </div>
      )}

      {/* Apply Button */}
      <div style={{ padding: '0.75rem', display: 'flex', gap: '0.5rem' }}>
        <button
          className="logging-apply-btn"
          onClick={handleSave}
          disabled={!dirty || saving || !localConfig}
          style={{
            flex: 1,
            padding: '0.4rem 0',
            cursor: (!dirty || saving) ? 'not-allowed' : 'pointer',
            opacity: (!dirty || saving) ? 0.6 : 1,
            background: 'var(--accent-color, #4a90d9)',
            color: '#fff',
            border: 'none',
            borderRadius: '4px',
            fontWeight: 600,
          }}
        >
          {saving ? 'Saving\u2026' : dirty ? 'Apply Changes' : 'Saved \u2713'}
        </button>
      </div>

      {/* Raw config toggle */}
      <div className="logging-section">
        <button className="logging-raw-toggle" onClick={() => setShowRaw(prev => !prev)}>
          {showRaw ? '\u25bc' : '\u25b6'} Raw Config
        </button>
        {showRaw && (
          <pre className="logging-raw-json">{JSON.stringify(localConfig, null, 2)}</pre>
        )}
      </div>
    </div>
  )
}
