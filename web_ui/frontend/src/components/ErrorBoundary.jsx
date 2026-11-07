/*
 * ErrorBoundary.jsx
 *
 * React error boundary — catches render-phase errors and shows a fallback UI
 * instead of leaving a blank white page. Wraps the entire <App /> tree in
 * main.jsx so that any component crash is contained.
 */

import React from 'react'

export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { hasError: false, error: null }
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error }
  }

  componentDidCatch(error, errorInfo) {
    console.error('[ErrorBoundary] Caught render error:', error)
    console.error('[ErrorBoundary] Component stack:', errorInfo?.componentStack)
  }

  render() {
    if (this.state.hasError) {
      const err = this.state.error
      return (
        <div style={{
          display: 'flex',
          justifyContent: 'center',
          alignItems: 'center',
          minHeight: '100vh',
          fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif',
          background: '#1a1a2e',
          color: '#e0e0e0',
          padding: '24px',
        }}>
          <div style={{
            maxWidth: '600px',
            width: '100%',
            background: '#16213e',
            borderRadius: '12px',
            padding: '32px',
            boxShadow: '0 8px 32px rgba(0,0,0,0.4)',
          }}>
            <div style={{
              background: '#e74c3c',
              color: '#fff',
              padding: '12px 20px',
              borderRadius: '8px',
              fontWeight: 700,
              fontSize: '18px',
              marginBottom: '20px',
            }}>
              Something went wrong loading ThoughtMachine.
            </div>

            <p style={{ margin: '0 0 12px', fontSize: '14px', opacity: 0.8 }}>
              The application encountered an unrecoverable error during render.
            </p>

            <pre style={{
              background: '#0f3460',
              padding: '16px',
              borderRadius: '8px',
              fontSize: '13px',
              fontFamily: '"Fira Code", "Cascadia Code", "Source Code Pro", monospace',
              overflowX: 'auto',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
              margin: '0 0 20px',
              color: '#ff6b6b',
            }}>
              {err?.message ?? 'Unknown error'}
              {err?.stack ? '\n\n' + err.stack : ''}
            </pre>

            <button
              onClick={() => window.location.reload()}
              style={{
                background: '#0f3460',
                color: '#e0e0e0',
                border: '1px solid #533483',
                borderRadius: '8px',
                padding: '12px 24px',
                fontSize: '15px',
                fontWeight: 600,
                cursor: 'pointer',
                display: 'inline-block',
              }}
              onMouseOver={(e) => e.currentTarget.style.background = '#533483'}
              onMouseOut={(e) => e.currentTarget.style.background = '#0f3460'}
            >
              Reload
            </button>

            <p style={{ margin: '16px 0 0', fontSize: '12px', opacity: 0.5 }}>
              If this persists, switch to a stable branch or revert recent
              frontend changes via the terminal.
            </p>
          </div>
        </div>
      )
    }

    return this.props.children
  }
}
