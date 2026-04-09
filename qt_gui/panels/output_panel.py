"""Output Panel - Event display and output area for the agent."""
import html
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QComboBox,
    QTextEdit, QFrame, QScrollArea
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QTextCursor


# Import from other extracted modules
from .event_models import EventModel, EventFilterProxyModel, EventDelegate
from .turn_container_manager import TurnContainerManager
from .markdown_renderer import MarkdownRenderer
from ..utils.constants import MAX_RESULT_LENGTH, MAX_TOOL_RESULTS_PER_TURN, MAX_LINES_PER_RESULT, ENABLE_RESULT_TRUNCATION, INTERNAL_EVENT_TYPES
from ..debug_log import debug_log
from ..utils.smart_scrolling import SmartScroller



class OutputPanel(QWidget):
    """Panel containing event display, filtering, and query controls."""
    # Special tools that should have blue styling, no truncation, full markdown
    SPECIAL_TOOLS = ["Final", "FinalReport", "RequestUserInteraction"]

    def __init__(self, parent=None):
        super().__init__(parent)

        # Event model
        self.event_model = EventModel()
        self.filter_proxy_model = EventFilterProxyModel()
        self.filter_proxy_model.setSourceModel(self.event_model)

        # Token tracking (mirrored from SessionTab)
        self.total_input = 0
        self.total_output = 0
        self.context_length = 0
        
        # Filter state tracking
        self._last_filter_text = ""
        self._last_filter_type = "all"
        
        # Batch event updates for performance
        self._pending_events = []
        self._batch_update_timer = QTimer(self)
        self._batch_update_timer.setSingleShot(True)
        self._batch_update_timer.setInterval(50)  # 50ms batch delay
        self._batch_update_timer.timeout.connect(self._process_batched_events)
        
        # Processing indicator tracking
        self._processing_indicators = {}  # turn_number -> event_index
        
        # Tool call mapping for special styling
        self._tool_call_map = {}  # tool_call_id -> tool_name
        

        self.init_ui()
        self.turn_container_manager = TurnContainerManager(self.output_textedit, self.filter_proxy_model)
        self.markdown_renderer = MarkdownRenderer()
        self.setup_signal_connections()

    def _normalize_turn(self, turn_val):
        """Convert turn value to integer for consistent comparison."""
        if turn_val is None:
            return 0
        # Handle int/float turn values
        if isinstance(turn_val, (int, float)):
            return int(turn_val)
        # Handle string turn values
        if isinstance(turn_val, str):
            try:
                return int(turn_val)
            except (ValueError, TypeError):
                # Try to convert float string
                try:
                    return int(float(turn_val))
                except (ValueError, TypeError):
                    return 0
        # Fallback
        return 0

    def init_ui(self):
        """Initialize the output panel UI."""
        layout = QVBoxLayout()
        self.setLayout(layout)

        # Filter controls
        filter_widget = QWidget()
        filter_layout = QHBoxLayout()
        filter_widget.setLayout(filter_layout)

        filter_layout.addWidget(QLabel("Filter:"))
        self.filter_lineedit = QLineEdit()
        self.filter_lineedit.setPlaceholderText("Search events...")
        filter_layout.addWidget(self.filter_lineedit, 1)  # Stretch

        filter_layout.addWidget(QLabel("Type:"))
        self.filter_type_combo = QComboBox()
        self.filter_type_combo.addItems([
            "all", "turn", "final", "user_query", "processing", "stopped",
            "system", "user_interaction_requested", "token_warning",
            "turn_warning", "paused", "max_turns", "error",
            "thread_finished"
        ])
        filter_layout.addWidget(self.filter_type_combo)

        layout.addWidget(filter_widget)

        # Output text area
        self.output_textedit = QTextEdit()
        self.output_textedit.setReadOnly(True)
        self.output_textedit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.output_textedit.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.output_textedit.setAcceptRichText(True)
        self.output_textedit.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.output_textedit.setStyleSheet("""
            QTextEdit:focus {
                border: none;
                outline: none;
            }
            QTextEdit {
                selection-background-color: #3399ff;
                selection-color: white;
            }
        """)
        layout.addWidget(self.output_textedit, 4)  # Larger stretch factor

        # Initialize smart scrolling
        self.smart_scroller = SmartScroller(self.output_textedit)

    def setup_signal_connections(self):
        """Connect filter signals."""
        self.filter_lineedit.textChanged.connect(self._apply_filter)
        self.filter_type_combo.currentTextChanged.connect(self._apply_filter)


    def _apply_filter(self):
        """Apply current filter settings and rebuild output."""
        import traceback
        import os, traceback
        debug_enabled = os.environ.get('THOUGHTMACHINE_DEBUG') == '1'
        filter_text = self.filter_lineedit.text()
        filter_type = self.filter_type_combo.currentText()
        # Skip if filter hasn't changed
        if filter_text == self._last_filter_text and filter_type == self._last_filter_type:
            return
        self._last_filter_text = filter_text
        self._last_filter_type = filter_type
        if debug_enabled:
            debug_log(f"[OutputPanel] _apply_filter: text='{filter_text}', type='{filter_type}'", level="DEBUG")
            stack_str = "".join(traceback.format_stack(limit=10))
            debug_log(f"Stack trace:\n{stack_str}", level="DEBUG")
        self.filter_proxy_model.set_filter(filter_text, filter_type)
        # Rebuild the output document with filtered events
        self._rebuild_output_document()
        # Still trigger auto-scroll if enabled
        self.smart_scroller.deferred_scroll_to_bottom()

    def _format_event_html(self, event):
        """Format event as HTML for display in QTextEdit."""
        delegate = EventDelegate()
        return delegate._event_to_html(event, suppress_title_bar=False)


    def _rebuild_output_document(self):
        """Rebuild the output text document from filtered events."""
        self.output_textedit.clear()
        # Reset incremental state
        self.turn_container_manager.reset()
        delegate = EventDelegate()
        
        # Group events by turn
        turns = {}
        for row in range(self.filter_proxy_model.rowCount()):
            index = self.filter_proxy_model.index(row, 0)
            event = index.data(Qt.ItemDataRole.UserRole)
            if not event:
                continue
                
            etype = event.get('type', 'unknown')
            turn_num = self._normalize_turn(event.get('turn', 0))
            
            # Initialize turn group if not exists
            if turn_num not in turns:
                turns[turn_num] = {
                    'user_query': None,
                    'assistant': None,
                    'tool_calls': [],
                    'tool_results': [],
                    'final': None,
                    'other_events': []
                }
            
            # Categorize event
            if etype == 'user_query':
                turns[turn_num]['user_query'] = event
            elif etype == 'turn':
                turns[turn_num]['assistant'] = event
            elif etype == 'tool_call':
                turns[turn_num]['tool_calls'].append(event)
            elif etype == 'tool_result':
                turns[turn_num]['tool_results'].append(event)
            elif etype == 'final':
                turns[turn_num]['final'] = event
            else:
                turns[turn_num]['other_events'].append(event)
        
        # Sort turns by number
        sorted_turns = sorted(turns.items())
        
        # Render each turn as a cohesive block
        max_turn = -1
        for i, (turn_num, turn_data) in enumerate(sorted_turns):
            html = delegate._turn_to_html(turn_num, turn_data)
            if html:
                cursor = self.output_textedit.textCursor()
                cursor.movePosition(QTextCursor.MoveOperation.End)
                # Close container for turn 0 events
                if turn_num == 0 and self.turn_container_manager.open_turn_container is not None:
                    cursor.insertHtml("</div>")
                    self.turn_container_manager.open_turn_container = None
                if i > 0:  # Add separator between turns, not before first turn
                    cursor.insertHtml("<hr style='margin: 10px 0; border: 1px solid #ddd;'>")
                cursor.insertHtml(html)
                max_turn = max(max_turn, turn_num)
        # Update incremental state after rebuild
        self.turn_container_manager.last_row_count = self.filter_proxy_model.rowCount()
        self.turn_container_manager.last_displayed_turn = max_turn



    def display_event(self, event):
        """Add an event to the output display."""
        # Debug logging for user_query events
        if event.get('type') == 'user_query':
            debug_log(f"[TIMESTAMP_DEBUG] OutputPanel.display_event: user_query event, turn={event.get('turn')}, created_at={event.get('created_at')}", level="DEBUG")
        # Add to model immediately
        self.event_model.add_event(event)
        
        # Batch display updates for performance
        self._pending_events.append(event)
        self._batch_update_timer.start()
    
    def _process_batched_events(self):
        """Process batched events incrementally without rebuilding entire document."""
        if not self._pending_events:
            return
        
        # Clear pending events (they're already added to model in display_event)
        self._pending_events.clear()
        
        # Append new events incrementally
        self.turn_container_manager.append_new_events()
        
        # Scroll to bottom if auto-scroll enabled
        self.smart_scroller.deferred_scroll_to_bottom()
    
    def show_processing_indicator(self, query, turn_number):
        """Show a temporary 'Processing...' indicator for a user query."""
        import time
        from datetime import datetime
        
        # Create a temporary processing event
        event = {
            "type": "processing",
            "content": f"⏳ Processing your query: {query[:50]}{'...' if len(query) > 50 else ''}",
            "turn": turn_number,
            "timestamp": datetime.now().isoformat(),
            "created_at": datetime.now().isoformat(),
            "_detail_level": "normal",
            "_is_processing_indicator": True  # Marker to identify processing events
        }
        
        # Add to model
        self.event_model.add_event(event)
        
        # Store the event index for later removal
        # Since events are appended, the index is the last one
        event_count = self.event_model.rowCount()
        if event_count > 0:
            self._processing_indicators[turn_number] = event_count - 1
        
        # Trigger display update
        self._pending_events.append(event)
        self._batch_update_timer.start()
    
    def remove_processing_indicator(self, turn_number):
        """Remove the processing indicator for a given turn."""
        if turn_number in self._processing_indicators:
            # We could remove from model, but for now just clear the tracking
            # Actual removal will happen when real user_query replaces it
            del self._processing_indicators[turn_number]
    
    # Message display methods for direct rendering from user_history
    def display_message(self, message: dict):
        """Display a message from user_history (role-based routing)."""
        role = message.get('role')
        debug_log(f"display_message: role={role}, content preview={str(message.get('content', ''))[:50]}", level="DEBUG")
        
        # Add visual separation between messages
        # Check if we're not at the beginning of the document
        cursor = self.output_textedit.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        if cursor.position() > 0:
            # Add a spacer between messages
            self._append_html('<div style="height: 8px;"></div>')
        
        if role == 'user':
            self.display_user_message(message)
        elif role == 'assistant':
            self.display_assistant_message(message)
        elif role == 'tool':
            self.display_tool_result(message)
        elif role == 'system':
            self.display_system_message(message)
        else:
            debug_log(f"Unknown message role: {role}", level="WARNING")
    
    def display_user_message(self, message: dict):
        """Display a user message."""
        content = message.get('content', '')
        created_at = message.get('created_at', '')
        
        # Detect system messages (token warnings, etc.)
        is_system = content.startswith('[SYSTEM]')
        if is_system:
            # Strip the prefix for cleaner display
            content = content[8:].lstrip()
            header = 'System'
            border_color = '#ff9999'
            bg_color = '#ffe6e6'
        else:
            header = 'User'
            border_color = '#cc99ff'
            bg_color = '#f0e6ff'
            
        self._append_html(
            f'<div style="border: 1px solid {border_color}; border-radius: 5px; margin-bottom: 8px; overflow: hidden;">'
            f'<div style="background-color: {bg_color}; padding: 8px 10px; font-weight: bold; border-bottom: 1px solid {border_color};">{header}</div>'
            f'<div style="padding: 10px;">'
            f'{self._render_content(content)}'
            f'</div>'
            f'</div>'
        )
    
    def display_assistant_message(self, message: dict):
        """Display an assistant message with optional tool calls."""
        content = message.get('content', '')
        tool_calls = message.get('tool_calls', [])
        reasoning_content = message.get('reasoning_content', '')
        
        debug_log(f"display_assistant_message: content length={len(content)}, tool_calls count={len(tool_calls)}", level="DEBUG")
        
        self._append_html(
            f'<div style="border: 1px solid #99ccff; border-radius: 5px; margin-bottom: 8px; overflow: hidden;">'
            f'<div style="background-color: #e6f3ff; padding: 8px 10px; font-weight: bold; border-bottom: 1px solid #99ccff;">Assistant</div>'
            f'<div style="padding: 10px;">'
        )
        
        # Display reasoning content if present
        if reasoning_content:
            self._append_html(
                f'<div style="background-color: #f5f5f5; border-left: 4px solid #ccc; padding: 8px; margin-bottom: 12px; font-style: italic;">'
                f'{self._render_content(reasoning_content)}'
                f'</div>'
            )
            # Add separation between reasoning and main content
            self._append_html('<div style="height: 4px;"></div>')
        
        # Display main content
        if content:
            self._append_html(f'{self._render_content(content)}')
        
        self._append_html(f'</div></div>')

        # Display tool calls if present
        if tool_calls:
            # Add visual separator before tool calls
            self._append_html('<div style="height: 12px; border-top: 1px solid #ddd; margin: 8px 0;"></div>')
        
        for i, tool_call in enumerate(tool_calls):
            debug_log(f"display_assistant_message: calling display_tool_call {i}", level="DEBUG")
            self.display_tool_call(tool_call)
    
    def display_tool_call(self, tool_call: dict):
        """Display a tool call (called from assistant message)."""
        tool_name = tool_call.get('function', {}).get('name', 'unknown')
        arguments = tool_call.get('function', {}).get('arguments', '{}')
        tool_call_id = tool_call.get('id', '')
        
        debug_log(f"display_tool_call: tool_name={tool_name}, arguments length={len(str(arguments))}", level="DEBUG")
        
        # Store mapping for tool result styling
        if tool_call_id:
            self._tool_call_map[tool_call_id] = tool_name
        
        # Special tools that should have blue styling, no truncation, full markdown
        SPECIAL_TOOLS = self.SPECIAL_TOOLS
        debug_log(f"display_tool_call: SPECIAL_TOOLS={SPECIAL_TOOLS}, tool_name in SPECIAL_TOOLS={tool_name in SPECIAL_TOOLS}", level="DEBUG")
        
        if tool_name in SPECIAL_TOOLS:
            self._append_html(
                f'<div style="margin-left: 20px; margin-top: 8px; margin-bottom: 8px; border-left: 4px solid #3498db; background-color: #eef4ff; padding: 8px; border-radius: 4px;">'
                f'<div style="color: #0000FF; font-weight: bold;">Tool: {tool_name}</div>'
                f'<div style="color: #666666; font-size: 0.9em;">Arguments: {html.escape(arguments)}</div>'
                f'</div>'
            )
        else:
            # Regular tool: truncate arguments, monospace font
            args_str = str(arguments)
            if len(args_str) > 200:
                args_str = args_str[:200] + '...'
            escaped_args = html.escape(args_str)
            self._append_html(
                f'<div style="margin-left: 20px; margin-top: 5px; margin-bottom: 5px;">'
                f'<div style="color: #006400; font-weight: bold;">Tool: {tool_name}</div>'
                f'<div style="color: #666666; font-size: 0.9em; font-family: monospace, monospace;">Arguments: {escaped_args}</div>'
                f'</div>'
            )
    
    def display_tool_result(self, message: dict):
        """Display a tool result message."""
        content = message.get('content', '')
        tool_call_id = message.get('tool_call_id', '')
        
        debug_log(f"display_tool_result: tool_call_id={tool_call_id}, content length={len(content)}", level="DEBUG")
        
        # Look up tool name for special styling
        tool_name = self._tool_call_map.get(tool_call_id, '')
        debug_log(f"display_tool_result: found tool_name='{tool_name}' from _tool_call_map (size={len(self._tool_call_map)})", level="DEBUG")
        SPECIAL_TOOLS = self.SPECIAL_TOOLS
        debug_log(f"display_tool_result: SPECIAL_TOOLS={SPECIAL_TOOLS}, tool_name in SPECIAL_TOOLS={tool_name in SPECIAL_TOOLS}", level="DEBUG")
        
        if tool_name in SPECIAL_TOOLS:
            self._append_html(
                f'<div style="margin-left: 20px; margin-top: 8px; margin-bottom: 10px; border-left: 4px solid #3498db; background-color: #eef4ff; padding: 8px; border-radius: 4px;">'
                f'<div style="color: #0000FF; font-weight: bold;">Tool Result ({tool_name})</div>'
                f'<div style="color: #0000FF;">{self._render_content(content)}</div>'
                f'</div>'
            )
        else:
            # Regular tool: truncate plain text, HTML escape, monospace font
            truncated_content = self._truncate_plain_text(content, tool_name)
            escaped_content = html.escape(truncated_content)
            self._append_html(
                f'<div style="margin-left: 20px; margin-top: 5px; margin-bottom: 10px;">'
                f'<div style="color: #006400; font-weight: bold;">Tool Result</div>'
                f'<div style="color: #006400; font-family: monospace, monospace; white-space: pre-wrap;">{escaped_content}</div>'
                f'</div>'
            )
    
    def display_system_message(self, message: dict):
        """Display a system message."""
        content = message.get('content', '')
        
        # Detect system messages that already have [SYSTEM] prefix (from token warnings)
        is_system_prefixed = content.startswith('[SYSTEM]')
        if is_system_prefixed:
            # Strip the prefix for cleaner display
            content = content[8:].lstrip()
        
        self._append_html(
            f'<div style="border: 1px solid #ff9999; border-radius: 5px; margin-bottom: 8px; overflow: hidden;">'
            f'<div style="background-color: #ffe6e6; padding: 8px 10px; font-weight: bold; border-bottom: 1px solid #ff9999;">System</div>'
            f'<div style="padding: 10px;">'
            f'{self._render_content(content)}'
            f'</div>'
            f'</div>'
        )
    
    def _render_content(self, content: str) -> str:
        """Render message content to HTML (handles markdown)."""
        if not content:
            return ''
        # Use the existing markdown renderer
        return self.markdown_renderer.markdown_to_html(content)
    
    def _truncate_plain_text(self, content: str, tool_name: str = '') -> str:
        """Truncate plain text content for regular tool results."""
        if not content:
            return ''
        
        # Don't truncate special tools
        if tool_name in self.SPECIAL_TOOLS:
            return content
        
        # Check if truncation is enabled
        if not ENABLE_RESULT_TRUNCATION:
            return content
        
        lines = content.split('\n')
        
        # Limit number of lines
        if len(lines) > MAX_LINES_PER_RESULT:
            lines = lines[:MAX_LINES_PER_RESULT]
            lines.append('...')
        
        # Process each line
        truncated_lines = []
        total_chars = 0
        for line in lines:
            # Check total character limit
            if total_chars + len(line) > MAX_RESULT_LENGTH:
                truncated_lines.append('...')
                break
            
            # Limit line length
            if len(line) > 100:
                line = line[:100] + '...'
            
            truncated_lines.append(line)
            total_chars += len(line)
        
        return '\n'.join(truncated_lines)
    
    def _append_html(self, html: str):
        """Append HTML to the output text edit."""
        cursor = self.output_textedit.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertHtml(html)
        # Scroll to bottom
        self.smart_scroller.deferred_scroll_to_bottom()
    
    # Note: _render_pending_turns removed in favor of incremental per-event rendering

    def _scroll_to_bottom(self):
        """Scroll output to bottom (for backward compatibility)."""
        self.smart_scroller.deferred_scroll_to_bottom()

    def clear_output(self):
        """Clear all output."""
        self.event_model.clear()
        self.output_textedit.clear()
        self._pending_events.clear()
        # Reset incremental state
        self.turn_container_manager.reset()
        # Clear tool call mapping
        self._tool_call_map.clear()
        debug_log("clear_output: cleared _tool_call_map", level="DEBUG")


    def update_tokens(self, total_input, total_output):
        """Update token counts (delegate to status panel)."""
        # This will be connected to status panel's update_tokens method
        pass

    def update_context_length(self, context_tokens):
        """Update context length (delegate to status panel)."""
        # This will be connected to status panel's update_context_length method
        pass

    def update_status(self, text):
        """Update status message (delegate to status panel)."""
        # This will be connected to status panel's update_status method
        pass

