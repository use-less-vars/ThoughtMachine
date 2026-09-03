// ── Shared canonical permission vocabulary (frontend mirror) ────────────────
// Single source of truth for the permission values/order/pill styling that the
// UI displays. Mirrors the backend session permission vocabulary
// (thoughtmachine/security.py PERMISSION_SCHEMA / SessionPermissions —
//  and security/security_gate.py _LEVEL_MAP).
// Consumers: ConfigPanel (permission <option> sets), WorkspacePanel and
// WorkerManagementPanel (effective-permission pills).

export const SESSION_RESOURCE_VOCAB = {
  filesystem: ['banned', 'ask', 'read', 'write', 'full'],
  system: ['banned', 'ask', 'read', 'write', 'full'],
  git: ['banned', 'ask', 'read', 'write', 'full'],
  network: ['banned', 'ask', 'write', 'outbound'],
  git_read: ['banned', 'ask', 'read', 'write'],
  git_write: ['banned', 'ask', 'read', 'write'],
  mcp: ['banned', 'connect', 'full'],
  execution: ['banned', 'ask', 'read', 'write', 'full'],
  container: [true, false], // booleans, not strings
};

export const PERMISSION_RANK_ORDER = {
  banned: 0,
  none: 1,
  ask: 1.5,
  read: 2,
  outbound: 2.5,
  write: 3,
  full: 4,
  write_feature_branches: 4,
};

// ── Catppuccin permission-pill map (previously duplicated in consumers) ─────
export const PILL_COLORS = {
  full: { bg: '#a6e3a1', fg: '#1e1e2e', label: 'Full' },
  write: { bg: '#a6e3a1', fg: '#1e1e2e', label: 'Write' },
  read: { bg: '#89b4fa', fg: '#1e1e2e', label: 'Read' },
  ask: { bg: '#f9e2af', fg: '#1e1e2e', label: 'Ask' },
  banned: { bg: '#f38ba8', fg: '#1e1e2e', label: 'Banned' },
  true: { bg: '#a6e3a1', fg: '#1e1e2e', label: 'Enabled' },
  false: { bg: '#f38ba8', fg: '#1e1e2e', label: 'Disabled' },
};

// Consumers call getPill(value); the lookup key is String(value) so boolean
// container values ('true'/'false') resolve to their map entries.
export function getPill(key) {
  const normalizedKey = String(key);
  return PILL_COLORS[normalizedKey] || { bg: '#6c7086', fg: '#cdd6f4', label: normalizedKey };
}
