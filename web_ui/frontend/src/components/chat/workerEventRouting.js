/*
 * workerEventRouting.js
 *
 * Routes worker events (App.workerEvents[sessionId]) to per-worker panels.
 *
 * A panel is identified by its instance key `<worker_name>#<instance_id>`
 * (missing instance id treated as '0'). An event matches a panel when its
 * instance identity lines up, in order of precision:
 *   1. BOTH sides carry instance_id    → strict equality
 *   2. BOTH sides carry instance_label → strict equality
 *   3. otherwise                       → bare worker_name equality (an event or
 *      panel with no identity of its own matches every instance of that name)
 *
 * A comparison is only made when BOTH sides carry the field. An older build
 * persisted panels without instance identity (JSON.stringify drops `undefined`),
 * and `undefined === 1` is false, so treating a missing side as a mismatch hid
 * every event from a restored panel. A missing side now falls through to the
 * next, coarser rule instead (panels are backfilled on restore — see App.jsx).
 *
 * Events without a worker_name match nothing (instanceKeyOf returns null).
 */

export const instanceKeyOf = (entry) => {
  if (!entry || typeof entry !== 'object') return null
  const workerName = entry.worker_name
  if (!workerName) return null
  return `${workerName}#${entry.instance_id ?? '0'}`
}

export const matchesPanel = (event, panel) => {
  if (!event || typeof event !== 'object') return false
  if (!panel || typeof panel !== 'object') return false
  // Instance identity is scoped to a worker: events without a worker_name
  // match nothing, and a name mismatch never matches (prevents cross-worker
  // leakage when different workers share instance ids/labels).
  if (!event.worker_name || event.worker_name !== panel.worker_name) return false
  // Compare instance identity only when BOTH sides carry it: a panel restored
  // from localStorage written by an older build may have no instance_id /
  // instance_label, and `undefined === 1` would drop all of its events.
  if (event.instance_id != null && panel.instance_id != null) {
    return event.instance_id === panel.instance_id
  }
  if (event.instance_label != null && panel.instance_label != null) {
    return event.instance_label === panel.instance_label
  }
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
