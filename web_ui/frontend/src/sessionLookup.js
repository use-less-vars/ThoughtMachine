import { apiUrl } from './apiBase'

/**
 * Tri-state workspace-membership probe for a session id.
 *
 *    true  → the backend confirmed `sessionId` is listed under `workspaceId`
 *    false → the backend answered and `sessionId` is NOT in that workspace
 *    null  → undecidable (missing args, network error, non-OK response, or a
 *            non-array payload)
 *
 * Never throws. Callers must treat null as "unknown" and fail OPEN — keep the
 * deep link and let the session report for itself — because a probe that could
 * not answer is not evidence that the session is foreign.
 */
export async function isSessionInWorkspace(workspaceId, sessionId) {
  if (!workspaceId || !sessionId) return null
  try {
    const res = await fetch(apiUrl(`/api/session/list?workspace_id=${encodeURIComponent(workspaceId)}`))
    if (!res.ok) return null
    const data = await res.json()
    if (!Array.isArray(data)) return null
    return data.some(s => s && s.session_id === sessionId)
  } catch {
    return null
  }
}
