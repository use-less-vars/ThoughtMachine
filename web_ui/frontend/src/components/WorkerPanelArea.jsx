/*
 * WorkerPanelArea.jsx
 *
 * Renders the worker panel sidebar for one session. Owns no state — it is a
 * pure view over App's per-session panel arrays:
 *
 *   panels     — [{ worker_name, instance_id, instance_label, size,
 *                  maximized, pinned, ... }]
 *   focusedKey — instance key of the active panel (null → fall back to last)
 *   eventsByKey— { [instanceKey]: [event, ...] } pre-routed by
 *                routeEventsToPanels (see components/chat/workerEventRouting.js)
 *
 * Layout:
 *   0 panels        → null (no sidebar)
 *   1 panel         → plain WorkerOutputPanel (single-panel fallback — no tab
 *                     strip, and no maximize/pin controls: nothing to toggle
 *                     against in a single-panel layout)
 *   >1 panels       → tab strip + stacked bodies; only the focused body is
 *                     displayed. Each tab shows move-left/move-right chevrons
 *                     and a close button (drag-reorder intentionally not
 *                     implemented — chevrons keep the strip simple/reliable).
 */

import React from 'react'
import WorkerOutputPanel from './WorkerOutputPanel'

// Instance key uniquely identifying a worker panel within a session
// (worker name + instance id; missing instance id treated as '0').
const workerPanelInstanceKey = (panel) => `${panel.worker_name}#${panel.instance_id ?? '0'}`

export default function WorkerPanelArea({
  sessionId,
  workspaceId,
  panels,
  focusedKey,
  eventsByKey,
  onClose,
  onFocus,
  onResize,
  onToggleMaximize,
  onTogglePin,
  onMoveLeft,
  onMoveRight,
}) {
  const list = Array.isArray(panels) ? panels : []
  if (list.length === 0) return null

  // Single panel — plain sidebar fallback (no tabs, no maximize/pin).
  if (list.length === 1) {
    const panel = list[0]
    const key = workerPanelInstanceKey(panel)
    return (
      <div className="worker-output-panel">
        <WorkerOutputPanel
          workspaceId={workspaceId}
          workerName={panel.worker_name}
          instanceId={panel.instance_id}
          instanceLabel={panel.instance_label}
          sessionId={sessionId}
          size={panel.size}
          maximized={panel.maximized}
          pinned={panel.pinned}
          onClose={() => onClose(key)}
          onResize={(w) => onResize(key, w)}
          incomingEvents={eventsByKey[key] || []}
        />
      </div>
    )
  }

  // Multiple panels — tab strip + stacked bodies. ALL bodies stay mounted
  // (hidden ones display:none) so panel state survives tab switches; only the
  // focused body is displayed. Bodies render focused-first in DOM order so the
  // visible panel is also the first querySelector hit (matches visual/read order).
  const keys = list.map(workerPanelInstanceKey)
  const focused = focusedKey && keys.includes(focusedKey) ? focusedKey : keys[keys.length - 1]
  const orderedBodies = [...list].sort((a, b) => {
    const ka = workerPanelInstanceKey(a)
    const kb = workerPanelInstanceKey(b)
    if (ka === focused) return -1
    if (kb === focused) return 1
    return 0
  })

  return (
    <div className="worker-output-panel multi">
      <div className="worker-panel-tabs">
        {list.map((panel, idx) => {
          const key = keys[idx]
          const active = key === focused
          return (
            <div
              key={key}
              className={`worker-panel-tab${active ? ' active' : ''}`}
              onClick={() => onFocus(key)}
              title={panel.instance_label || panel.worker_name}
            >
              <button
                type="button"
                className="worker-panel-tab-chevron"
                disabled={idx === 0}
                onClick={(e) => { e.stopPropagation(); onMoveLeft(key) }}
                aria-label="Move panel left"
              >◀</button>
              <span className="worker-panel-tab-label">
                {panel.pinned ? '📌 ' : ''}{panel.instance_label || panel.worker_name}
              </span>
              <button
                type="button"
                className="worker-panel-tab-chevron"
                disabled={idx === list.length - 1}
                onClick={(e) => { e.stopPropagation(); onMoveRight(key) }}
                aria-label="Move panel right"
              >▶</button>
              <button
                type="button"
                className="worker-panel-tab-close"
                onClick={(e) => { e.stopPropagation(); onClose(key) }}
                aria-label="Close panel"
              >✕</button>
            </div>
          )
        })}
      </div>
      <div className="worker-panel-bodies">
        {orderedBodies.map((panel) => {
          const key = workerPanelInstanceKey(panel)
          return (
            <div
              key={key}
              className="worker-panel-body"
              style={{ display: key === focused ? 'block' : 'none', height: '100%' }}
            >
              <WorkerOutputPanel
                workspaceId={workspaceId}
                workerName={panel.worker_name}
                instanceId={panel.instance_id}
                instanceLabel={panel.instance_label}
                sessionId={sessionId}
                size={panel.size}
                maximized={panel.maximized}
                pinned={panel.pinned}
                onClose={() => onClose(key)}
                onResize={(w) => onResize(key, w)}
                onToggleMaximize={onToggleMaximize ? () => onToggleMaximize(key) : undefined}
                onTogglePin={onTogglePin ? () => onTogglePin(key) : undefined}
                incomingEvents={eventsByKey[key] || []}
              />
            </div>
          )
        })}
      </div>
      <style>{`
        .worker-output-panel.multi { display: flex; flex-direction: column; }
        .worker-panel-tabs { display: flex; align-items: center; gap: .25rem; padding: .25rem .5rem; background: var(--bg-secondary); border-bottom: 1px solid var(--bg-surface); flex-shrink: 0; overflow-x: auto; }
        .worker-panel-tab { display: inline-flex; align-items: center; gap: .25rem; padding: .2rem .4rem; cursor: pointer; color: var(--text-secondary); font-family: var(--font-mono); font-size: .8rem; user-select: none; white-space: nowrap; border-radius: 4px; }
        .worker-panel-tab:hover, .worker-panel-tab.active { background: var(--bg-surface); color: var(--text-primary); }
        .worker-panel-tab-chevron, .worker-panel-tab-close { background: transparent; border: none; color: inherit; cursor: pointer; font-size: .7rem; padding: 0 .15rem; line-height: 1; }
        .worker-panel-tab-chevron:disabled { opacity: .3; cursor: default; }
        .worker-panel-bodies { flex: 1; min-height: 0; overflow: hidden; position: relative; }
        .worker-panel-body { height: 100%; }
      `}</style>
    </div>
  )
}
