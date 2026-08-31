// --- useWorkspaceSummary.js ---
// Loads the workspace detail summary for a workspace ID.
// Guards against stale resolutions when the ID changes mid-flight and
// exposes refetch() for the Apply / Retry flows.

import { useCallback, useEffect, useState } from 'react'
import { fetchWorkspaceSummary } from './workspaceApi'

export default function useWorkspaceSummary(workspaceId) {
  const [summary, setSummary] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [tick, setTick] = useState(0)

  const refetch = useCallback(() => setTick((t) => t + 1), [])

  useEffect(() => {
    let cancelled = false
    if (!workspaceId) {
      setSummary(null)
      setError(null)
      setLoading(false)
      return undefined
    }
    setLoading(true)
    setError(null)
    fetchWorkspaceSummary(workspaceId)
      .then((data) => {
        if (!cancelled) setSummary(data)
      })
      .catch((err) => {
        if (!cancelled) {
          setError((err && err.message) || 'Failed to load workspace summary')
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [workspaceId, tick])

  return { summary, loading, error, refetch }
}
