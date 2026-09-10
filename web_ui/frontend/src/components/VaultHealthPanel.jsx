// --- VaultHealthPanel.jsx ---
// Landing-page "Vault repair health" panel. Fetches /api/vault/repair/status on
// mount and groups the returned findings into a fixed five-section order by RISK
// category (security_critical, permission_integrity, config_drift, cosmetic,
// then an "Other" bucket for any unknown category — never hidden). Risk and
// severity are colour-coded via classes; the internal engine category
// (`issue_category`) is NEVER rendered.
//
// Repair actions are gated:
//   * machine_apply findings only (manual_review is informational),
//   * cosmetic findings are informational only,
//   * unknown ("Other") categories are never auto-fixable,
//   * security_critical findings are per-item only (never select-all/bulk).
// Every apply path (bulk or per-item) first opens a mandatory preview dialog.

import React, { useCallback, useEffect, useState } from 'react'
import { fetchVaultRepairStatus, fetchVaultRepairApply } from '../globalApi'

// Fixed section order, keyed by the RISK-category value (`finding.category`).
const RISK_SECTIONS = [
  { key: 'security_critical', title: 'Security critical' },
  { key: 'permission_integrity', title: 'Permission integrity' },
  { key: 'config_drift', title: 'Configuration drift' },
  { key: 'cosmetic', title: 'Cosmetic' },
]
const OTHER_SECTION = { key: 'other', title: 'Other' }

// Risk categories that may be repaired in bulk (select-all / batch apply).
// security_critical is deliberately excluded: it requires an explicit per-item
// action. cosmetic and unknown ("Other") categories are never auto-applied.
const BULK_APPLY_RISKS = ['permission_integrity', 'config_drift']

// backend severity vocabulary is error | warning | info; unknown -> info.
const SEVERITY_CLASS = { error: 'error', warning: 'warning', info: 'info' }

function severityClass(severity) {
  return SEVERITY_CLASS[severity] || 'info'
}

function riskClass(category) {
  const known = RISK_SECTIONS.some((section) => section.key === category)
  return known ? category.replace(/_/g, '-') : 'other'
}

function isMachineApply(finding) {
  return Boolean(finding) && finding.classification === 'machine_apply'
}

// A finding may be repaired individually when it is machine_apply and its risk
// category is one the backend will act on. security_critical is included here
// but only ever through an explicit per-item selection.
function isItemEligible(finding) {
  if (!isMachineApply(finding)) return false
  return (
    finding.category === 'security_critical' || BULK_APPLY_RISKS.includes(finding.category)
  )
}

// A finding may take part in a bulk (select-all) selection only when it is
// machine_apply and its risk category is bulk-safe.
function isBulkEligible(finding) {
  if (!isMachineApply(finding)) return false
  return BULK_APPLY_RISKS.includes(finding.category)
}

// Prefer the `findings[]` projection; fall back to mapping the raw `issues[]`
// when a payload only carries that shape. Never surfaces issue_category.
function normaliseFindings(status) {
  if (!status || typeof status !== 'object') return []
  if (Array.isArray(status.findings)) return status.findings
  if (Array.isArray(status.issues)) {
    return status.issues.map((issue) => ({
      id: issue.id,
      category: issue.risk_category,
      severity: issue.severity,
      file: issue.file,
      message: issue.message,
      suggested_fix: issue.fix,
      classification: issue.classification,
      path_in_file: issue.path_in_file,
    }))
  }
  return []
}

// seeded_files entries are objects ({file,status}); extra_files are strings.
// Render both as text so no object ever reaches a React child.
function fileLabel(entry) {
  if (entry == null) return ''
  if (typeof entry === 'string') return entry
  if (typeof entry === 'object') {
    if (entry.file) return entry.status ? `${entry.file} (${entry.status})` : String(entry.file)
    return ''
  }
  return String(entry)
}

function itemKey(finding, index) {
  return finding && finding.id != null ? String(finding.id) : `${(finding && finding.file) || 'finding'}-${index}`
}

export default function VaultHealthPanel({ defaultOpen = false }) {
  const [status, setStatus] = useState(null)
  const [loaded, setLoaded] = useState(false)
  const [open, setOpen] = useState(defaultOpen)
  const [selectedIds, setSelectedIds] = useState([])
  const [preview, setPreview] = useState(null)
  const [applying, setApplying] = useState(false)
  const [notice, setNotice] = useState(null)

  const load = useCallback(async () => {
    const data = await fetchVaultRepairStatus()
    setStatus(data && typeof data === 'object' ? data : null)
    setSelectedIds([])
    setLoaded(true)
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const refresh = () => {
    setNotice(null)
    load()
  }

  if (!loaded) return null

  const hasReport =
    status &&
    (Array.isArray(status.findings) ||
      Array.isArray(status.issues) ||
      (status.summary && typeof status.summary === 'object'))

  if (!hasReport) {
    return (
      <section
        className="vault-health-panel vault-health-panel-unavailable"
        aria-label="Vault repair health"
      >
        <div className="vault-health-panel-header">
          <h3 className="vault-health-panel-title">Vault repair</h3>
          <button type="button" className="vault-health-panel-refresh" onClick={refresh}>
            Retry
          </button>
        </div>
        <p className="vault-health-panel-notice vault-health-panel-notice-error" role="status">
          Vault repair status unavailable — cannot check the vault right now.
        </p>
      </section>
    )
  }

  const findings = normaliseFindings(status)
  const machineEntries = findings.filter(isMachineApply)
  const bulkEntries = findings.filter(isBulkEligible)
  const healthy = findings.length === 0
  const manualCount = findings.length - machineEntries.length

  const repairsAvailable = Boolean(status) && status.repairs_available === true

  const groups = new Map()
  RISK_SECTIONS.forEach((section) => groups.set(section.key, []))
  groups.set('other', [])
  findings.forEach((finding) => {
    const key = RISK_SECTIONS.some((section) => section.key === finding.category)
      ? finding.category
      : 'other'
    groups.get(key).push(finding)
  })
  const orderedSections = [...RISK_SECTIONS, OTHER_SECTION]

  const selectedBulk = bulkEntries.filter((finding) => selectedIds.includes(String(finding.id)))
  const allBulkSelected = bulkEntries.length > 0 && selectedBulk.length === bulkEntries.length

  const toggleSelected = (id) => {
    const key = String(id)
    setSelectedIds((prev) =>
      prev.includes(key) ? prev.filter((value) => value !== key) : [...prev, key]
    )
  }

  const toggleSelectAll = () => {
    setSelectedIds(allBulkSelected ? [] : bulkEntries.map((finding) => String(finding.id)))
  }

  const openBulkPreview = () => {
    const chosen = bulkEntries.filter((finding) => selectedIds.includes(String(finding.id)))
    if (chosen.length === 0) return
    setPreview({ findings: chosen })
  }

  const openItemPreview = (finding) => {
    setPreview({ findings: [finding] })
  }

  const confirmApply = async () => {
    if (!preview || preview.findings.length === 0) return
    const ids = preview.findings.map((finding) => finding.id)
    setApplying(true)
    const result = await fetchVaultRepairApply({ repair_ids: ids, confirmed: true })
    if (result && typeof result === 'object') {
      const backups = Array.isArray(result.backups_created)
        ? result.backups_created.length
        : typeof result.backups_created === 'number'
          ? result.backups_created
          : 0
      const quarantined = Array.isArray(result.quarantine_moves)
        ? result.quarantine_moves.length
        : 0
      setNotice({
        kind: 'success',
        text: `Repairs applied. ${backups} backups created, ${quarantined} files quarantined.`,
      })
      setPreview(null)
      setApplying(false)
      await load()
    } else {
      setNotice({
        kind: 'error',
        text: 'Could not apply repairs. Your selection was kept — please try again.',
      })
      setPreview(null)
      setApplying(false)
    }
  }

  const hasFooter =
    (Array.isArray(status.extra_files) && status.extra_files.length > 0) ||
    (Array.isArray(status.seeded_files) && status.seeded_files.length > 0)

  return (
    <section className="vault-health-panel" aria-label="Vault repair health">
      <div className="vault-health-panel-header">
        <h3 className="vault-health-panel-title">Vault repair</h3>
        <span
          className={
            'vault-health-panel-verdict ' +
            (healthy ? 'vault-health-panel-verdict-ok' : 'vault-health-panel-verdict-drift')
          }
        >
          {healthy
            ? '\u2713 Vault is healthy'
            : `\u26a0 ${findings.length} finding${findings.length === 1 ? '' : 's'} detected`}
        </span>
        {!healthy ? (
          <button
            type="button"
            className="vault-health-panel-toggle"
            aria-expanded={open}
            aria-controls="vault-health-panel-body"
            onClick={() => setOpen((prev) => !prev)}
          >
            <span className="vault-health-panel-toggle-label">
              {open ? 'Collapse details' : 'Expand details'}
            </span>
            <span className="vault-health-panel-chevron" aria-hidden="true">
              {open ? '\u25be' : '\u25b8'}
            </span>
          </button>
        ) : null}
        <button
          type="button"
          className="vault-health-panel-refresh"
          onClick={refresh}
          disabled={applying}
        >
          Refresh
        </button>
      </div>

      {notice ? (
        <p
          className={`vault-health-panel-notice vault-health-panel-notice-${notice.kind}`}
          role={notice.kind === 'error' ? 'alert' : 'status'}
        >
          {notice.text}
        </p>
      ) : null}

      {healthy || open ? (
        <div id="vault-health-panel-body" className="vault-health-panel-body">
          {healthy ? (
            <React.Fragment>
              <p className="vault-health-panel-healthy">Vault is healthy — no issues found.</p>
              <button
                type="button"
                className="vault-health-panel-rescan"
                onClick={refresh}
                disabled={applying}
              >
                Re-scan
              </button>
            </React.Fragment>
          ) : (
            <React.Fragment>
          <div className="vault-health-panel-summary">
            <span className="vault-health-panel-stat">
              <strong>{findings.length}</strong> total
            </span>
            <span className="vault-health-panel-stat">
              <strong>{machineEntries.length}</strong> machine-appliable
            </span>
            <span className="vault-health-panel-stat">
              <strong>{manualCount}</strong> manual review
            </span>
          </div>

          {!repairsAvailable ? (
            <p className="vault-health-panel-hint" role="status">
              No automatic repairs are available for this vault; the findings below are informational.
            </p>
          ) : null}

          {orderedSections.map((section) => {
            const items = groups.get(section.key) || []
            if (items.length === 0) return null
            return (
              <div
                className={`vault-health-panel-group vault-health-panel-group-${section.key.replace(/_/g, '-')}`}
                key={section.key}
              >
                <h4 className="vault-health-panel-group-title">
                  {section.title} <span className="vault-health-panel-count">{items.length}</span>
                </h4>
                <ul className="vault-health-panel-list">
                  {items.map((finding, index) => {
                    const bulkEligible = isBulkEligible(finding)
                    const itemEligible = isItemEligible(finding)
                    const checked = selectedIds.includes(String(finding.id))
                    return (
                      <li
                        className={`vault-health-panel-item vault-health-panel-item-${riskClass(finding.category)}`}
                        key={itemKey(finding, index)}
                      >
                        <span
                          className={`vault-health-panel-sev vault-health-panel-sev-${severityClass(finding.severity)}`}
                        >
                          {finding.severity || 'info'}
                        </span>
                        <span className="vault-health-panel-file">
                          {finding.file || 'unknown file'}
                        </span>
                        <span className="vault-health-panel-message">{finding.message || ''}</span>
                        {finding.suggested_fix ? (
                          <span className="vault-health-panel-fix">
                            suggested fix: {finding.suggested_fix}
                          </span>
                        ) : null}
                        {bulkEligible && repairsAvailable ? (
                          <label className="vault-health-panel-select">
                            <input
                              type="checkbox"
                              checked={checked}
                              onChange={() => toggleSelected(finding.id)}
                              aria-label={`Select fix for ${finding.file || 'finding'}`}
                            />
                            <span className="vault-health-panel-select-label">Select</span>
                          </label>
                        ) : null}
                        {!bulkEligible && itemEligible && repairsAvailable ? (
                          <button
                            type="button"
                            className="vault-health-panel-item-action"
                            onClick={() => openItemPreview(finding)}
                            aria-label={`Preview fix for ${finding.file || 'finding'}`}
                          >
                            Preview fix
                          </button>
                        ) : null}
                        {!itemEligible ? (
                          <span className="vault-health-panel-info-label">
                            {isMachineApply(finding)
                              ? 'not auto-appliable'
                              : 'manual review \u2014 no automatic fix'}
                          </span>
                        ) : null}
                      </li>
                    )
                  })}
                </ul>
              </div>
            )
          })}

          {bulkEntries.length > 0 && repairsAvailable ? (
            <div className="vault-health-panel-actions">
              <label className="vault-health-panel-select-all">
                <input type="checkbox" checked={allBulkSelected} onChange={toggleSelectAll} />
                <span>Select all machine-appliable fixes ({bulkEntries.length})</span>
              </label>
              <button
                type="button"
                className="vault-health-panel-apply"
                onClick={openBulkPreview}
                disabled={selectedBulk.length === 0 || applying}
              >
                Apply selected repairs ({selectedBulk.length})
              </button>
            </div>
          ) : null}

          {applying ? (
            <p className="vault-health-panel-applying" role="status">
              {'Applying repairs\u2026'}
            </p>
          ) : null}

          {hasFooter ? (
            <div className="vault-health-panel-footer">
              {Array.isArray(status.extra_files) && status.extra_files.length > 0 ? (
                <p className="vault-health-panel-footer-line">
                  Extra files: {status.extra_files.map(fileLabel).join(', ')}
                </p>
              ) : null}
              {Array.isArray(status.seeded_files) && status.seeded_files.length > 0 ? (
                <p className="vault-health-panel-footer-line">
                  Seeded files: {status.seeded_files.map(fileLabel).join(', ')}
                </p>
              ) : null}
            </div>
          ) : null}
            </React.Fragment>
          )}
        </div>
      ) : null}

      {preview ? (
        <div className="vault-health-panel-overlay">
          <div
            className="vault-health-panel-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="vault-health-panel-preview-title"
          >
            <h4 className="vault-health-panel-dialog-title" id="vault-health-panel-preview-title">
              Confirm repairs
            </h4>
            <p className="vault-health-panel-dialog-intro">
              Apply {preview.findings.length} selected repair
              {preview.findings.length === 1 ? '' : 's'} to vault files? Backups are created before
              changes, and affected files are moved to quarantine if needed. This is a summary of the
              selected findings, not a full file diff.
            </p>
            <ul className="vault-health-panel-dialog-list">
              {preview.findings.map((finding, index) => (
                <li className="vault-health-panel-dialog-item" key={itemKey(finding, index)}>
                  <span className="vault-health-panel-file">
                    {finding.file || 'unknown file'}
                  </span>
                  {typeof finding.path_in_file === 'string' && finding.path_in_file ? (
                    <span className="vault-health-panel-path">{finding.path_in_file}</span>
                  ) : null}
                  <span className="vault-health-panel-message">{finding.message || ''}</span>
                  {typeof finding.suggested_fix === 'string' && finding.suggested_fix ? (
                    <span className="vault-health-panel-fix">
                      suggested fix: {finding.suggested_fix}
                    </span>
                  ) : null}
                </li>
              ))}
            </ul>
            <div className="vault-health-panel-dialog-actions">
              <button
                type="button"
                className="vault-health-panel-dialog-cancel"
                onClick={() => setPreview(null)}
                disabled={applying}
              >
                Cancel
              </button>
              <button
                type="button"
                className="vault-health-panel-dialog-confirm"
                onClick={confirmApply}
                disabled={applying}
              >
                {applying ? 'Applying\u2026' : 'Apply repairs'}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </section>
  )
}
