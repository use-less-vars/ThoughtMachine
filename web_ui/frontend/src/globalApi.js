// --- globalApi.js ---
// API access for the Global Management landing view (WorkspaceSelector).
// Every endpoint here is optional: the backend may not expose it yet, so all
// fetchers degrade to null / [] instead of throwing.

const API_BASE = ''

async function safeGet(url) {
  try {
    const res = await fetch(API_BASE + url)
    if (!res.ok) return null
    return await res.json()
  } catch {
    return null
  }
}

export async function fetchGlobalSummary() {
  const data = await safeGet('/api/global/summary')
  return data && typeof data === 'object' ? data : null
}

export async function fetchVaultStatus() {
  const data = await safeGet('/api/vault/status')
  return data && typeof data === 'object' ? data : null
}

export async function fetchResourceCatalog() {
  const data = await safeGet('/api/resource-catalog')
  if (Array.isArray(data)) return data
  if (data && Array.isArray(data.items)) return data.items
  return []
}

export async function fetchCredentials() {
  const data = await safeGet('/api/credentials')
  if (Array.isArray(data)) return data
  if (data && Array.isArray(data.credentials)) return data.credentials
  if (data && Array.isArray(data.items)) return data.items
  return []
}

export async function createCredential({ name, secret }) {
  try {
    const res = await fetch(API_BASE + '/api/credentials', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, value: secret }),
    })
    return res.ok
  } catch {
    return false
  }
}

export async function deleteCredential(name) {
  try {
    const res = await fetch(API_BASE + '/api/credentials/' + encodeURIComponent(name), {
      method: 'DELETE',
    })
    return res.ok
  } catch {
    return false
  }
}
