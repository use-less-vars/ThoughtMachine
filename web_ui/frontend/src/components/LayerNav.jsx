import React from 'react'

/*
 * LayerNav — shared top-left breadcrumb for the three-layer model
 * (Workspaces → Workspace → Session).
 *
 * Renders nothing on the selector view (and for a falsy route). Otherwise a
 * single breadcrumb bar where ancestor layers are real anchors (<a href=…>)
 * and the current layer is a non-link <span aria-current="page">. Crumbs are
 * separated by ' / '. Inline styles only — no stylesheet.
 */
const styles = {
  nav: {
    display: 'flex',
    alignItems: 'center',
    gap: '4px',
    padding: '6px 12px',
    fontSize: '13px',
    lineHeight: 1.4,
  },
  link: {
    color: '#6ea8fe',
    textDecoration: 'none',
    cursor: 'pointer',
  },
  current: {
    color: '#e6e6e6',
    fontWeight: 600,
  },
  sep: {
    color: '#5a6472',
  },
}

export default function LayerNav({ route, workspaceLabel, sessionLabel }) {
  if (!route || route.view === 'selector') return null

  const sep = (key) => (
    <span key={key} style={styles.sep}>{' / '}</span>
  )

  if (route.view === 'workspace') {
    return (
      <div data-testid="layer-nav" style={styles.nav}>
        <a href="#/workspaces" style={styles.link}>Workspaces</a>
        {sep('sep-ws')}
        <span aria-current="page" style={styles.current}>{workspaceLabel ?? 'Workspace'}</span>
      </div>
    )
  }

  if (route.view === 'session') {
    if (route.workspaceId) {
      return (
        <div data-testid="layer-nav" style={styles.nav}>
          <a href="#/workspaces" style={styles.link}>Workspaces</a>
          {sep('sep-ws')}
          <a
            href={`#/workspace/${encodeURIComponent(route.workspaceId)}`}
            style={styles.link}
          >
            {workspaceLabel ?? 'Workspace'}
          </a>
          {sep('sep-session')}
          <span aria-current="page" style={styles.current}>{sessionLabel ?? 'Session'}</span>
        </div>
      )
    }
    return (
      <div data-testid="layer-nav" style={styles.nav}>
        <a href="#/workspaces" style={styles.link}>Workspaces</a>
        {sep('sep-session')}
        <span aria-current="page" style={styles.current}>{sessionLabel ?? 'Session'}</span>
      </div>
    )
  }

  return null
}
