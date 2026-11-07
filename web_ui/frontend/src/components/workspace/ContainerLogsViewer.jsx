// --- ContainerLogsViewer.jsx ---
// Layer 2 core: a self-contained, read-only viewer for a single container's
// logs. Fetches GET /api/workspace/{id}/containers/{name}/logs?tail={n} and
// renders the text inside an accessible region with a tail-size selector.
// Nothing here starts, stops or otherwise mutates a container.

import React, { useEffect, useState } from 'react'

export const CONTAINER_LOGS_MAX_TAIL = 10000

const TAIL_OPTIONS = [200, 500, 1000, 2000]

export function clampTail(value) {
  const n = Number(value)
  if (!Number.isFinite(n) || n <= 0) return 200
  return Math.min(n, CONTAINER_LOGS_MAX_TAIL)
}

export default function ContainerLogsViewer({ workspaceId, containerName, tail = 200 }) {
  const [selectedTail, setSelectedTail] = useState(() => clampTail(tail))
  const [status, setStatus] = useState('loading')
  const [text, setText] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    setStatus('loading')
    setError(null)
    setText(null)
    fetch(`/api/workspace/${workspaceId}/containers/${containerName}/logs?tail=${selectedTail}`)
      .then((response) => {
        if (!response.ok) throw new Error(`Failed to load logs (${response.status})`)
        return response.json()
      })
      .then((data) => {
        if (cancelled) return
        if (!data || data.success === false) {
          throw new Error((data && data.detail) || 'Failed to load logs')
        }
        const stdout = (data && data.stdout) || ''
        const stderr = (data && data.stderr) || ''
        setText(stderr ? `${stdout}\n[stderr]\n${stderr}` : stdout)
        setStatus('done')
      })
      .catch((err) => {
        if (cancelled) return
        setError((err && err.message) || 'Failed to load logs')
        setStatus('error')
      })
    return () => {
      cancelled = true
    }
  }, [workspaceId, containerName, selectedTail])

  return (
    <div className="wdp-container-logs-viewer" role="region" aria-label="Container logs">
      <div className="wdp-container-logs-controls">
        <label className="wdp-logs-tail-label">
          Lines
          <select
            className="wdp-logs-tail-select"
            aria-label="Log tail size"
            value={selectedTail}
            onChange={(event) => setSelectedTail(clampTail(event.target.value))}
          >
            {TAIL_OPTIONS.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>
      </div>
      {status === 'loading' && (
        <div className="wdp-container-logs-loading">Loading logs…</div>
      )}
      {status === 'error' && <div className="wdp-container-logs-error">{error}</div>}
      {status === 'done' && <pre className="wdp-container-logs">{text}</pre>}
    </div>
  )
}
