// --- workspaceApi.js ---
// API access for the Workspace Detail page (Layer 2 core).
// Unlike globalApi.js (degrading safeGet), these fetchers throw on non-ok so
// the page can surface backend errors inline.

const API_BASE = ''

// Extract a human-readable message from an error response body. Handles:
//   {errors: [string, ...]}            — PUT /permissions validation errors
//   {detail: {errors: [string, ...]}}  — same, wrapped by FastAPI HTTPException
//   {detail: "message"}                — plain FastAPI detail
//   {detail: [{loc, msg, type}, ...]}  — FastAPI request-validation errors
function extractErrorMessage(body) {
  if (!body || typeof body !== 'object') return 'Request failed'
  if (Array.isArray(body.errors) && body.errors.length > 0) {
    return body.errors.join('; ')
  }
  const detail = body.detail
  if (typeof detail === 'string' && detail) return detail
  if (detail && typeof detail === 'object') {
    if (Array.isArray(detail.errors) && detail.errors.length > 0) {
      return detail.errors.join('; ')
    }
    if (Array.isArray(detail)) {
      const messages = detail
        .map((entry) => (entry && typeof entry.msg === 'string' ? entry.msg : null))
        .filter(Boolean)
      if (messages.length > 0) return messages.join('; ')
    }
  }
  return 'Request failed'
}

async function parseError(res) {
  let body = null
  try {
    body = await res.json()
  } catch {
    // Non-JSON error body — fall through to the generic message.
  }
  return new Error(extractErrorMessage(body))
}

export async function fetchWorkspaceSummary(workspaceId) {
  const res = await fetch(
    API_BASE + '/api/workspace/' + encodeURIComponent(workspaceId) + '/summary'
  )
  if (!res.ok) throw await parseError(res)
  return res.json()
}

// `payload` is { permissions: {...} } plus allow_host_resources ONLY when the
// caller wants to change it (the backend treats an absent key as "unchanged").
export async function updateWorkspacePermissions(workspaceId, payload) {
  const res = await fetch(
    API_BASE + '/api/workspace/' + encodeURIComponent(workspaceId) + '/permissions',
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }
  )
  if (!res.ok) throw await parseError(res)
  return res.json()
}

// Global tool registry (read-only list for the Tools tab). The backend
// derives `enabled` from GLOBAL settings (e.g. allow_host_resources), not
// from this workspace's config — the tab shows the fetched truth as-is.
// Response: { tools: [{name, enabled, disabled_reason, permission_level}] }
export async function fetchTools() {
  const res = await fetch(API_BASE + '/api/tools')
  if (!res.ok) throw await parseError(res)
  return res.json()
}

