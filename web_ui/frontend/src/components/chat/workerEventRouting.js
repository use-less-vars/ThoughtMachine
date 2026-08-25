/*
 * workerEventRouting.js
 *
 * Routes worker events (App.workerEvents[sessionId]) to per-worker panels.
 *
 * A panel is identified by its instance key `<worker_name>#<instance_id>`
 * (missing instance id treated as '0'). An event matches a panel when its
 * instance identity lines up, in order of precision:
 *   1. event.instance_id != null  → strict equality with panel.instance_id
 *   2. event.instance_label != null → strict equality with panel.instance_label
 *   3. otherwise                   → bare worker_name equality (event has no
 *      instance identity — matches EVERY panel for that worker name)
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
  if (event.instance_id != null) return event.instance_id === panel.instance_id
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
