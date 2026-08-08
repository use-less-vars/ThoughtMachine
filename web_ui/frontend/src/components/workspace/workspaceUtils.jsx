// --- workspaceUtils.js ---
// Shared helpers extracted from WorkspacePanel.jsx (Phase 4 structural split).
// Consumed by the workspace tabs, the workspace modals, and the
// WorkspacePanel shell. No store imports — pure helpers only.

import React from 'react'

export const RISK_CLASS = { Low: 'low', Medium: 'medium', High: 'high', Critical: 'critical' }

// Ceiling options for the Permissions tab dropdown. 'enabled' is the value the
// container permission map reports for effective permissions, so it is part of
// the standard list (see PERM_RANK for its advisory rank).
export const PERMISSION_CEILINGS = ['banned', 'read', 'write', 'ask', 'full', 'enabled']
export const PERM_RANK = { banned: 0, read: 1, write: 2, ask: 3, full: 4, enabled: 2 }

export const PROVIDERS = ['Local LLM', 'OpenAI', 'Anthropic', 'DeepSeek']
export const PRESET_OPTIONS = ['Agent (full)', 'Engineer (read-only)', 'Custom']

export const CONTAINER_LIMIT = 4          // workspace container limit (matches the backend)
export const PROMPT_PREVIEW_LENGTH = 120  // worker systemPrompt truncation

// --- Permissions reconciliation ---
// Phase 1's store builds permissions as an ARRAY of { name, ceiling, effective }
// (see buildWorkspace in workspaceStore.js); some backend shapes may instead be
// an OBJECT like { git: 'write' }. Normalize to an array for rendering, and
// preserve the original shape when writing updates back.
export function normalizePermissions(permissions) {
  if (Array.isArray(permissions)) return permissions
  if (permissions && typeof permissions === 'object') {
    return Object.entries(permissions).map(([name, effective]) => ({
      name,
      ceiling: effective,
      effective,
    }))
  }
  return []
}

export function permissionsWithCeiling(original, name, ceiling) {
  if (Array.isArray(original)) {
    return original.map((p) => (p.name === name ? { ...p, ceiling, effective: ceiling } : p))
  }
  return { ...(original || {}), [name]: ceiling }
}

export function permissionEffective(permissions, name) {
  const entry = normalizePermissions(permissions).find((p) => p.name === name)
  return entry ? entry.effective || entry.ceiling : null
}

// 'enabled' ranks at 2 (same tier as write): the only rank comparison is the
// advisory's network check, and ranking 'enabled' at write tier means it never
// falsely escalates against a write-tier expectation (while still counting as
// higher than read-only expectations).
export function isHigherPermission(current, expected) {
  const c = PERM_RANK[current] ?? -1
  const e = PERM_RANK[expected] ?? -1
  return e >= 0 && c > e
}

export function countOf(value) {
  if (Array.isArray(value)) return value.length
  if (value && typeof value === 'object') return Object.keys(value).length
  return 0
}

export function fmtUptime(seconds) {
  if (seconds == null || Number.isNaN(Number(seconds))) return '—'
  const s = Math.max(0, Math.floor(Number(seconds)))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${s % 60}s`
  const h = Math.floor(m / 60)
  return `${h}h ${m % 60}m`
}

// --- Small shared placeholder modal ---
export function PlaceholderModal({ title, message, onClose }) {
  return (
    <div className="wp-modal-overlay" onClick={onClose}>
      <div className="wp-modal" role="dialog" aria-label={title || message} onClick={(e) => e.stopPropagation()}>
        {title && <h3 className="wp-modal-title">{title}</h3>}
        <p>{message}</p>
        <button className="wp-btn" onClick={onClose}>Close</button>
      </div>
    </div>
  )
}

export function Toggle({ checked, onChange, label }) {
  return (
    <label className="wp-toggle">
      <input type="checkbox" checked={!!checked} onChange={(e) => onChange(e.target.checked)} />
      <span className="wp-toggle-label">{label}</span>
    </label>
  )
}
