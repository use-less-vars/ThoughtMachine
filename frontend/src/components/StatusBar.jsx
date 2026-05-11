/*
 * StatusBar.jsx
 *
 * Shows session.status (coloured dot + label), tokensIn/Out, contextLength.
 */

import React from 'react'
import useStore from '../store/useStore'

function statusDisplay(status) {
  switch (status) {
    case 'IDLE':
      return { className: 'status-idle', label: '● Idle' }
    case 'RUNNING':
      return { className: 'status-running', label: '● Running' }
    case 'PAUSED':
      return { className: 'status-paused', label: '⏸ Paused' }
    case 'WAITING_FOR_USER':
      return { className: 'status-waiting', label: '● Waiting' }
    default:
      return { className: 'status-idle', label: status }
  }
}

export default function StatusBar() {
  const session = useStore((s) => s.session)
  const { className, label } = statusDisplay(session.status)

  return (
    <div className="status-bar">
      <span className={`status-indicator ${className}`}>{label}</span>
      <span className="status-sep">|</span>
      <span>Tokens: In={session.tokensIn} / Out={session.tokensOut}</span>
      <span className="status-sep">|</span>
      <span>Context: {session.contextLength}</span>
    </div>
  )
}
