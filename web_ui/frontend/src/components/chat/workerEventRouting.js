/*
 * workerEventRouting.js
 *
 * Routes worker events (App.workerEvents[sessionId]) to per-worker panels.
 *
 * A panel is identified by its instance key `<worker_name>#<instance_id>`
 * (missing instance id treated as '0').
 *
 * THE CONTRACT: instance identity is compared against the EVENT's identity,
 * and it is compared STRICTLY. When the event carries `instance_id`, only the
 * panel with that exact id matches (otherwise `instance_label`, then bare
 * `worker_name`). The event side is the source of truth because every event
 * published on a worker's own EventBus is stamped with the producing
 * WorkerThread's `instance_id` / `instance_label` (see
 * tools/workspace/worker_thread.py `_stamp_instance_identity`).
 *
 * PANELS ALWAYS CARRY AN IDENTITY. Both ways a panel can come into existence
 * give it a numeric `instance_id`: `openPanel` and `normalisePersistedPanel`
 * in App.jsx backfill `instance_id ?? 1` / `instance_label ?? worker_name`,
 * mirroring the REST default (`getattr(thread, "instance_id", 1)` in
 * web_ui/backend/workspace_routes.py). So a panel with no identity is a BUG
 * ON THE PANEL SIDE, and the fix for it belongs there — not in a wider routing
 * rule that would let an identity-carrying event reach a panel it does not
 * belong to.
 *
 * Historical cause: a panel persisted by an OLDER build could carry no
 * instance identity at all (`JSON.stringify` drops `undefined`), and
 * `undefined === 1` is false, so every stamped event was dropped and the
 * restored panel rendered nothing. That is why App.jsx backfills on restore,
 * and why the (now unreachable) drop below is LOGGED instead of being silent.
 *
 * Events without a worker_name match nothing (instanceKeyOf returns null).
 */

export const instanceKeyOf = (entry) => {
  if (!entry || typeof entry !== 'object') return null
  const workerName = entry.worker_name
  if (!workerName) return null
  return `${workerName}#${entry.instance_id ?? '0'}`
}

// Panels that were handed an identity-carrying event while carrying no
// instance identity of their own. Guarded once per worker name so a stale
// record cannot flood the console on every rendered event (same rationale as
// `warnedPersistedPanelIdentity` in App.jsx).
const warnedPanelWithoutIdentity = new Set()

const warnPanelWithoutIdentity = (event, panel) => {
  if (warnedPanelWithoutIdentity.has(event.worker_name)) return
  warnedPanelWithoutIdentity.add(event.worker_name)
  console.warn(
    `[workerEventRouting] panel ${instanceKeyOf(panel)} has no instance identity ` +
    `(instance_id=${String(panel.instance_id)}, ` +
    `instance_label=${String(panel.instance_label)}) and cannot match events for ` +
    `instance_id=${String(event.instance_id)}; it is probably a stale persisted ` +
    `record — panels are backfilled on restore in App.jsx (normalisePersistedPanel)`
  )
}

export const matchesPanel = (event, panel) => {
  if (!event || typeof event !== 'object') return false
  if (!panel || typeof panel !== 'object') return false
  // Instance identity is scoped to a worker: events without a worker_name
  // match nothing, and a name mismatch never matches (prevents cross-worker
  // leakage when different workers share instance ids/labels).
  if (!event.worker_name || event.worker_name !== panel.worker_name) return false
  // The event carries an identity → compare it strictly. No panel-side
  // fallback: a panel without an identity is a stale/buggy record (see the
  // header), and guessing an identity for it is how two instances of one
  // worker end up sharing a stream.
  if (event.instance_id != null) {
    const matched = event.instance_id === panel.instance_id
    if (!matched && panel.instance_id == null) {
      // Unreachable by construction (App.jsx backfills `instance_id ?? 1`), so
      // if it ever happens it is a real defect: say it ONCE, loudly, rather
      // than dropping the panel's events in silence.
      warnPanelWithoutIdentity(event, panel)
    }
    return matched
  }
  if (event.instance_label != null) return event.instance_label === panel.instance_label
  return true
}

export const routeEventsToPanels = (events, panels) => {
  const result = {}
  const list = Array.isArray(events) ? events : []
  const panelList = Array.isArray(panels) ? panels : []
  panelList.forEach(p => {
    const key = instanceKeyOf(p)
    if (key) result[key] = list.filter(e => matchesPanel(e, p))
  })
  return result
}
