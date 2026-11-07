// @vitest-environment jsdom
/*
 * TabBar.test.jsx
 *
 * Label contract: the tab strip must agree with the session header
 * (SessionTab renders `sessionName || 'Untitled'`). A tab opened before
 * `sessions_list` lands carries an empty title, so it must fall back to
 * 'Untitled' instead of rendering a blank tab.
 */
import React from 'react'
import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { render, cleanup } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import TabBar from '../TabBar'

const noop = () => {}

function labels() {
  return Array.from(document.querySelectorAll('.tab-item .tab-label')).map((n) => n.textContent)
}

beforeEach(() => {
  cleanup()
})

afterEach(() => {
  cleanup()
})

describe('TabBar label fallback', () => {
  it('falls back to Untitled for an empty/absent name and keeps a real name', () => {
    render(
      <TabBar
        tabs={[{ id: 'a', name: '' }, { id: 'b' }, { id: 'c', name: 'Real Name' }]}
        activeTabId="a"
        onSelectTab={noop}
        onCloseTab={noop}
        onNewTab={noop}
      />
    )
    expect(labels()).toEqual(['Untitled', 'Untitled', 'Real Name'])
  })
})
