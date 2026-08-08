// --- router.js ---
// Dependency-free hash router (Phase 1 of the Workspace Panel).
// react-router-dom is staged in package.json but NOT installed, so routing is
// a minimal hash-based implementation. useNavigate() mirrors the
// react-router-dom signature, so swapping to the real router later is a
// one-line import change per file (see package.json "_comment").

import { useState, useEffect, useCallback } from 'react'

export function parseHash(hash) {
  const path = (hash || '').replace(/^#/, '')
  if (path === '' || path === '/' || path === '/workspaces') {
    return { view: 'selector' }
  }
  const match = path.match(/^\/workspace\/([^/]+)$/)
  if (match) {
    return { view: 'workspace', id: decodeURIComponent(match[1]) }
  }
  const sessionMatch = path.match(/^\/session\/([^/]+)$/)
  if (sessionMatch) {
    return { view: 'session', id: decodeURIComponent(sessionMatch[1]) }
  }
  return null
}

export function useRoute() {
  const [route, setRoute] = useState(() => parseHash(window.location.hash))
  useEffect(() => {
    const onHashChange = () => setRoute(parseHash(window.location.hash))
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])
  return route
}

export function useNavigate() {
  return useCallback((path) => {
    window.location.hash = '#' + path
  }, [])
}
