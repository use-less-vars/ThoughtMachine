/*
 * TabBar.jsx
 *
 * Top tab bar that displays open session tabs (like browser tabs).
 * Each tab shows the session name and a close (✕) button.
 * A "+" button creates a new tab.
 *
 * Props:
 *   tabs            — array of { id, name }
 *   activeTabId     — currently active tab id
 *   onSelectTab     — called with (tabId)
 *   onCloseTab      — called with (tabId)
 *   onNewTab        — called with ()
 *   runningStates   — { [tabId]: status }
 */

import React from 'react'

function _tabStatusClass(status) {
  switch (status) {
    case 'RUNNING':
      return 'running'
    case 'PAUSED':
      return 'pausing'
    case 'WAITING_FOR_USER':
      return 'running'
    default:
      return 'idle'
  }
}

export default function TabBar({ tabs, activeTabId, onSelectTab, onCloseTab, onNewTab, onLoggingClick, runningStates = {} }) {
  if (tabs.length === 0) {
    return null
  }

  return (
    <div className="tab-bar">
      <div className="tab-list">
        {tabs.map((tab) => (
          <div
            key={tab.id}
            className={`tab-item ${activeTabId === tab.id ? 'tab-active' : ''} ${_tabStatusClass(runningStates[tab.id])}`}
            onClick={() => onSelectTab(tab.id)}
            title={tab.name}
          >
            <span className="tab-label">{tab.name}</span>
            <button
              className="tab-close-btn"
              onClick={(e) => {
                e.stopPropagation()
                onCloseTab(tab.id)
              }}
              title="Close session"
            >
              ✕
            </button>
          </div>
        ))}
        <button className="tab-new-btn" onClick={onNewTab} title="New session">
          +
        </button>
      </div>
      <div className="tab-actions">
        <button className="tab-action-btn" onClick={onLoggingClick} title="Toggle logging panel">
          Logging
        </button>
      </div>
    </div>
  )
}
