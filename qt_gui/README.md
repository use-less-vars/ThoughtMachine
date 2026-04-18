# ThoughtMachine GUI Module

This module provides a modular PyQt6-based graphical user interface for the ThoughtMachine agent.

## Architecture

The GUI is organized into separate components for maintainability and testability:

- **main_window.py**: `AgentGUI` class - the main application window with tab management and menu bar.
- **session_tab.py**: `SessionTab` class - a tab containing a full agent session.
- **panels/**: UI components:
  - `output_panel.py` - Event display and output area with smart scrolling.
  - `query_panel.py` - Query entry and control buttons.
  - `agent_controls.py` - Configuration controls (provider, model, temperature, tools, etc.).
  - `status_panel.py` - Token usage and context length display.
  - `tool_loader.py` - Tool enable/disable checkboxes.
  - `markdown_renderer.py` - Markdown to HTML rendering.

  - `mcp_config.py` - MCP server configuration dialog.
- **config/**: Configuration management:
  - `config_bridge.py` - `GUIConfigBridge` adapter between GUI and ConfigService, providing debounced saving.
- **utils/**: Shared utilities:
  - `smart_scrolling.py` - `SmartScroller` for auto-scroll behavior.
  - `signal_helpers.py` - Helper functions for signal connections.
  - `constants.py` - Centralized constants (e.g., `MAX_RESULT_LENGTH`).
- **themes.py**: Theme definitions and `apply_theme()` function.

## Usage

The recommended entry point is `qt_gui.main`:

```python
from qt_gui.main import main

if __name__ == "__main__":
    main()
```

Or as a module:

```bash
python -m qt_gui.main
```

## Backward Compatibility

For legacy code, `qt_gui_refactored.py` is retained as a deprecation wrapper. It re-exports all public symbols but emits a `DeprecationWarning` on import. New code should use the explicit modules.

## Design Principles

- **Separation of Concerns**: Each panel has a single responsibility.
- **Signal-based Communication**: Components communicate via Qt signals, avoiding direct dependencies.
- **Configuration Centralization**: All config access goes through `GUIConfigBridge`.
- **Testability**: Panels can be instantiated and tested in isolation.
- **Accessibility**: Keyboard navigation, screen reader support, and tooltips are provided.
