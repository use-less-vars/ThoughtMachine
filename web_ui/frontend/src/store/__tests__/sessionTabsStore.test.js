// @vitest-environment jsdom
/*
 * sessionTabsStore.test.js — unit tests for the per-workspace session tab
 * strip store (src/sessionTabsStore.js): open/close/activate/title behaviour
 * plus the localStorage persistence contract (tm.sessionTabs.<workspaceId>).
 */
import { describe, it, expect, beforeEach } from 'vitest'
import useSessionTabsStore from '../../sessionTabsStore'

beforeEach(() => {
  useSessionTabsStore.getState().reset()
  localStorage.clear()
})

describe('sessionTabsStore — openTab / active tab', () => {
  it('openTab adds the first tab and makes it active', () => {
    const st = useSessionTabsStore.getState()
    st.openTab('ws-1', { sessionId: 'a', title: 'A' })
    const entry = useSessionTabsStore.getState().byWorkspace['ws-1']
    expect(entry.tabs).toEqual([{ sessionId: 'a', title: 'A' }])
    expect(entry.activeSessionId).toBe('a')
  })

  it('openTab dedupes: same sessionId twice keeps one tab and the first active', () => {
    const st = useSessionTabsStore.getState()
    st.openTab('ws-1', { sessionId: 'a', title: 'A' })
    st.openTab('ws-1', { sessionId: 'a', title: 'A2' })
    st.openTab('ws-1', { sessionId: 'b', title: 'B' })
    const entry = useSessionTabsStore.getState().byWorkspace['ws-1']
    expect(entry.tabs).toHaveLength(2)
    expect(entry.activeSessionId).toBe('a') // first tab stays active
  })

  it('openTab without ws or sessionId is a no-op', () => {
    useSessionTabsStore.getState().openTab('', { sessionId: 'a' })
    useSessionTabsStore.getState().openTab('ws-1', { sessionId: '' })
    expect(useSessionTabsStore.getState().byWorkspace).toEqual({})
  })
})

describe('sessionTabsStore — setActiveTab / closeTab', () => {
  function seed() {
    const st = useSessionTabsStore.getState()
    st.openTab('ws-1', { sessionId: 'a', title: 'A' })
    st.openTab('ws-1', { sessionId: 'b', title: 'B' })
    st.openTab('ws-1', { sessionId: 'c', title: 'C' })
  }

  it('setActiveTab switches the active tab', () => {
    seed()
    useSessionTabsStore.getState().setActiveTab('ws-1', 'c')
    expect(useSessionTabsStore.getState().byWorkspace['ws-1'].activeSessionId).toBe('c')
  })

  it('setActiveTab ignores unknown sessionIds', () => {
    seed()
    useSessionTabsStore.getState().setActiveTab('ws-1', 'nope')
    expect(useSessionTabsStore.getState().byWorkspace['ws-1'].activeSessionId).toBe('a')
  })

  it('closing the active tab activates the NEXT neighbor', () => {
    seed()
    useSessionTabsStore.getState().setActiveTab('ws-1', 'a')
    useSessionTabsStore.getState().closeTab('ws-1', 'a')
    const entry = useSessionTabsStore.getState().byWorkspace['ws-1']
    expect(entry.tabs.map(t => t.sessionId)).toEqual(['b', 'c'])
    expect(entry.activeSessionId).toBe('b')
  })

  it('closing the LAST active tab activates the PREVIOUS tab', () => {
    seed()
    useSessionTabsStore.getState().setActiveTab('ws-1', 'c')
    useSessionTabsStore.getState().closeTab('ws-1', 'c')
    const entry = useSessionTabsStore.getState().byWorkspace['ws-1']
    expect(entry.tabs.map(t => t.sessionId)).toEqual(['a', 'b'])
    expect(entry.activeSessionId).toBe('b')
  })

  it('closing a non-active tab keeps the active one', () => {
    seed()
    useSessionTabsStore.getState().setActiveTab('ws-1', 'b')
    useSessionTabsStore.getState().closeTab('ws-1', 'a')
    const entry = useSessionTabsStore.getState().byWorkspace['ws-1']
    expect(entry.tabs.map(t => t.sessionId)).toEqual(['b', 'c'])
    expect(entry.activeSessionId).toBe('b')
  })

  it('closing the last tab clears the active session', () => {
    seed()
    useSessionTabsStore.getState().closeTab('ws-1', 'a')
    useSessionTabsStore.getState().closeTab('ws-1', 'b')
    useSessionTabsStore.getState().closeTab('ws-1', 'c')
    const entry = useSessionTabsStore.getState().byWorkspace['ws-1']
    expect(entry.tabs).toEqual([])
    expect(entry.activeSessionId).toBeNull()
  })

  it('tabs are isolated per workspace', () => {
    const st = useSessionTabsStore.getState()
    st.openTab('ws-1', { sessionId: 'a', title: 'A' })
    st.openTab('ws-2', { sessionId: 'x', title: 'X' })
    const state = useSessionTabsStore.getState()
    expect(state.byWorkspace['ws-1'].tabs.map(t => t.sessionId)).toEqual(['a'])
    expect(state.byWorkspace['ws-2'].tabs.map(t => t.sessionId)).toEqual(['x'])
    expect(state.byWorkspace['ws-1'].activeSessionId).toBe('a')
    expect(state.byWorkspace['ws-2'].activeSessionId).toBe('x')
  })
})

describe('sessionTabsStore — persistence + hydrate', () => {
  it('persists { v, tabs, activeSessionId } on every mutation', () => {
    const st = useSessionTabsStore.getState()
    st.openTab('ws-1', { sessionId: 'a', title: 'A' })
    st.openTab('ws-1', { sessionId: 'b', title: 'B' })
    st.setActiveTab('ws-1', 'b')
    const saved = JSON.parse(localStorage.getItem('tm.sessionTabs.ws-1'))
    expect(saved.v).toBe(1)
    expect(saved.tabs).toEqual([
      { sessionId: 'a', title: 'A' },
      { sessionId: 'b', title: 'B' },
    ])
    expect(saved.activeSessionId).toBe('b')
  })

  it('setTabTitle updates and persists the title', () => {
    const st = useSessionTabsStore.getState()
    st.openTab('ws-1', { sessionId: 'a', title: 'A' })
    st.setTabTitle('ws-1', 'a', 'Renamed')
    expect(useSessionTabsStore.getState().byWorkspace['ws-1'].tabs[0].title).toBe('Renamed')
    expect(JSON.parse(localStorage.getItem('tm.sessionTabs.ws-1')).tabs[0].title).toBe('Renamed')
  })

  it('hydrate loads a persisted entry (tabs + active)', () => {
    localStorage.setItem(
      'tm.sessionTabs.ws-1',
      JSON.stringify({
        v: 1,
        tabs: [
          { sessionId: 'a', title: 'A' },
          { sessionId: 'b', title: 'B' },
        ],
        activeSessionId: 'b',
      })
    )
    const entry = useSessionTabsStore.getState().hydrate('ws-1')
    expect(entry.tabs).toHaveLength(2)
    expect(entry.activeSessionId).toBe('b')
    expect(useSessionTabsStore.getState().byWorkspace['ws-1'].activeSessionId).toBe('b')
  })

  it('hydrate is idempotent: returns the in-memory entry after first call', () => {
    localStorage.setItem(
      'tm.sessionTabs.ws-1',
      JSON.stringify({ v: 1, tabs: [{ sessionId: 'old', title: 'Old' }], activeSessionId: 'old' })
    )
    useSessionTabsStore.getState().hydrate('ws-1')
    useSessionTabsStore.getState().openTab('ws-1', { sessionId: 'new', title: 'New' })
    const again = useSessionTabsStore.getState().hydrate('ws-1')
    expect(again.tabs.map(t => t.sessionId).sort()).toEqual(['new', 'old'])
    expect(again.activeSessionId).toBe('old')
  })

  it('hydrate handles malformed JSON with an empty result', () => {
    localStorage.setItem('tm.sessionTabs.ws-1', 'not-json{')
    const entry = useSessionTabsStore.getState().hydrate('ws-1')
    expect(entry.tabs).toEqual([])
    expect(entry.activeSessionId).toBeNull()
  })

  it('hydrate sanitizes bad entries: drops empty sessionIds, fixes active', () => {
    localStorage.setItem(
      'tm.sessionTabs.ws-1',
      JSON.stringify({
        v: 1,
        tabs: [{ sessionId: '' }, { sessionId: 'c', title: 'C' }],
        activeSessionId: 'missing',
      })
    )
    const entry = useSessionTabsStore.getState().hydrate('ws-1')
    // sanitize drops the empty-sessionId tab but preserves valid titles
    expect(entry.tabs).toEqual([{ sessionId: 'c', title: 'C' }])
    expect(entry.activeSessionId).toBe('c') // falls back to the first tab
  })
})
