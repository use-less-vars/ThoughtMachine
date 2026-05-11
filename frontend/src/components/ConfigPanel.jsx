/*
 * ConfigPanel.jsx
 *
 * Sidebar panel — each control sends update_config immediately.
 * The component is driven by the `config` prop (controlled component).
 *
 * Props:
 *   sendCommand(command, payload)
 *   config — { temperature, max_turns, provider, tools }
 */

import React from 'react'

export default function ConfigPanel({ sendCommand, config }) {

  const updateField = (field, value) => {
    sendCommand('update_config', { field, value })
  }

  const toggleTool = (index) => {
    const tool = config.tools[index]
    if (tool) updateField(`tools.${index}.enabled`, !tool.enabled)
  }

  return (
    <div className="config-panel">
      <h3>Configuration</h3>

      <label className="config-field">
        <span>Temperature: {config.temperature}</span>
        <input
          type="range"
          min="0"
          max="2"
          step="0.1"
          value={config.temperature}
          onChange={(e) => updateField('temperature', parseFloat(e.target.value))}
        />
      </label>

      <label className="config-field">
        <span>Max turns: {config.max_turns}</span>
        <input
          type="range"
          min="1"
          max="100"
          step="1"
          value={config.max_turns}
          onChange={(e) => updateField('max_turns', parseInt(e.target.value, 10))}
        />
      </label>

      <label className="config-field">
        <span>Provider:</span>
        <select
          value={config.provider}
          onChange={(e) => updateField('provider', e.target.value)}
        >
          <option value="openai">OpenAI</option>
          <option value="anthropic">Anthropic</option>
          <option value="local">Local</option>
        </select>
      </label>

      <div className="config-tools">
        <span>Tools:</span>
        {config.tools.map((tool, i) => (
          <label className="config-tool-item" key={tool.name}>
            <input
              type="checkbox"
              checked={tool.enabled}
              onChange={() => toggleTool(i)}
            />
            {tool.name}
          </label>
        ))}
      </div>
    </div>
  )
}
