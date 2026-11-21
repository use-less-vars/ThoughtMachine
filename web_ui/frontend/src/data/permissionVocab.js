// ── Shared canonical permission vocabulary (frontend mirror) ────────────────
// Single source of truth for the permission values/order/pill styling that the
// UI displays. Mirrors the backend session permission vocabulary
// (thoughtmachine/security.py PERMISSION_SCHEMA / SessionPermissions —
//  and security/security_gate.py _LEVEL_MAP).
// Consumers: ConfigPanel (permission <option> sets), SessionWorkspaceTab
// and WorkerManagementPanel (effective-permission pills).

// Session resources are the ONLY keys a session may store / the ConfigPanel may
// PUT: git, filesystem, container, network, mcp, host_bash, credential, visual
// (mirrors the backend session-permission store). Legacy frontend resources
// (system, execution) and gate grains (git_read, git_write) are NOT session
// resources.
export const SESSION_RESOURCE_VOCAB = {
  git: ['banned', 'ask', 'read', 'write', 'write_on_feature_branch'],
  filesystem: ['banned', 'read', 'write'],
  container: [true, false], // booleans, not strings
  network: ['banned', 'ask', 'write', 'outbound'],
  mcp: ['banned', 'connect', 'full'],
  host_bash: ['banned', 'ask', 'allow'],
  credential: ['banned', 'read', 'use'],
  visual: ['banned', 'ask', 'view'],
};

export const PERMISSION_RANK_ORDER = {
  banned: 0,
  none: 1,
  ask: 2,
  read: 1,
  // visual view grant: ranks ABOVE ask (2) at 2.5, like the backend
  // security.resource_catalog.GRANT_LEVEL_RANKS table -- an ``ask`` session
  // value no longer satisfies a ``view`` requirement silently.
  view: 2.5,
  connect: 1,
  outbound: 3.5,
  write: 3,
  // Same write tier as the security-gate rank — not a separate level.
  write_on_feature_branch: 3,
  // credential value-injection grant: same tier as write.
  use: 3,
  full: 4,
};

// ── Catppuccin permission-pill map (previously duplicated in consumers) ─────
export const PILL_COLORS = {
  full: { bg: '#a6e3a1', fg: '#1e1e2e', label: 'Full' },
  write: { bg: '#a6e3a1', fg: '#1e1e2e', label: 'Write' },
  write_on_feature_branch: { bg: '#a6e3a1', fg: '#1e1e2e', label: 'Feature Branch' },
  read: { bg: '#89b4fa', fg: '#1e1e2e', label: 'Read' },
  view: { bg: '#89b4fa', fg: '#1e1e2e', label: 'View' },
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

// ── One word per state (the effective column) ───────────────────────────────
// PILL_COLORS keys above are TOKENS, not states: 'banned' is a level a ceiling
// or a grant may name, and its pill legitimately reads 'Banned'. An *effective*
// value is an OUTCOME instead -- the question "what does the agent actually
// have?" -- and it has exactly two answers plus "unknown".
//
// The gate spells that denial two ways for the same state: 'banned' on a LEVEL
// axis (git, network, filesystem, mcp, host_bash, credential, visual) and a
// literal boolean `false` on the boolean axis (container). Same state, so same
// word -- otherwise the same denial prints 'Banned' or 'Disabled' depending on
// which axis it happened to land on.
export function effectivePermissionLabel(value) {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'boolean') return value ? 'Enabled' : 'Disabled';
  const token = String(value);
  // A level-axis denial is the same state as a boolean-axis denial.
  return token.toLowerCase() === 'banned' ? 'Disabled' : token;
}
