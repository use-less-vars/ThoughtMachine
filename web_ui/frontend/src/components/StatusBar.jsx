/*
 * StatusBar.jsx
 *
 * Shows session status (coloured dot + label), tokensIn/Out, contextLength.
 *
 * Props: status, tokensIn, tokensOut, contextLength
 */

import React from 'react'

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

function StatusBar({ status, tokensIn, tokensOut, contextLength }) {
  const { className, label } = statusDisplay(status)

  return (
    <div className="status-bar">
      <span className={`status-indicator ${className}`}>{label}</span>
      <span className="status-sep">|</span>
      <span>Tokens: In={tokensIn} / Out={tokensOut}</span>
      <span className="status-sep">|</span>
      <span>Context: {contextLength}</span>
    </div>
  )
}

export default React.memo(StatusBar)
