// --- VaultHealthBanner.jsx ---
// Polls /api/vault/status every 30s and renders a banner describing the vault
// integrity state. Renders nothing until the first check completes.

import React, { useEffect, useState } from 'react'
import { fetchVaultStatus } from '../globalApi'

const SEVERITY_CLASS = {
  critical: 'critical',
  high: 'high',
  medium: 'medium',
  low: 'low',
}

export default function VaultHealthBanner() {
  const [status, setStatus] = useState(null)
  const [loaded, setLoaded] = useState(false)

  useEffect(() => {
    let cancelled = false
    const check = async () => {
      const s = await fetchVaultStatus()
      if (cancelled) return
      setStatus(s)
      setLoaded(true)
    }
    check()
    const interval = setInterval(check, 30000)
    return () => {
      cancelled = true
      clearInterval(interval)
    }
  }, [])

  if (!loaded) return null

  if (!status) {
    return (
      <div className="vault-health-banner vault-health-degraded" role="status">
        Vault status unavailable — cannot verify vault integrity right now.
      </div>
    )
  }

  if (status.ok === true) {
    return (
      <div className="vault-health-banner vault-health-ok" role="status">
        ✓ Vault healthy
      </div>
    )
  }

  const issues = Array.isArray(status.issues) ? status.issues : []
  return (
    <div className="vault-health-banner vault-health-error" role="alert">
      <div className="vault-health-title">⚠ Vault integrity issues detected</div>
      {issues.length === 0 ? (
        <p className="gms-empty">Vault reported unhealthy but no issues were listed.</p>
      ) : (
        <ul className="vault-health-list">
          {issues.map((issue, idx) => (
            <li className="vault-health-issue" key={issue.file || idx}>
              <span className={`vault-health-severity ${SEVERITY_CLASS[issue.severity] || 'medium'}`}>
                {issue.severity || 'unknown'}
              </span>
              <span className="vault-health-file">{issue.file || 'unknown file'}</span>
              <span className="vault-health-message">{issue.message || ''}</span>
              {issue.action ? <span className="vault-health-action">→ {issue.action}</span> : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
