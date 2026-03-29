"""Output Panel - Event display and output area for the agent."""
import html
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QComboBox,
    QTextEdit, QFrame, QScrollArea
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QTextCursor


# Import from other extracted modules
from qt_gui.panels.event_models import EventModel, EventFilterProxyModel, EventDelegate
from qt_gui.panels.markdown_renderer import MarkdownRenderer
from qt_gui.utils.constants import MAX_RESULT_LENGTH, MAX_TOOL_RESULTS_PER_TURN, MAX_LINES_PER_RESULT, ENABLE_RESULT_TRUNCATION
from qt_gui.utils.smart_scrolling import SmartScroller



class OutputPanel(QWidget):
    """Panel containing event display, filtering, and query controls."""

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

        self.init_ui()
        self.setup_signal_connections()

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
            "all", "turn", "final", "user_query", "stopped",
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
        import os
        debug_enabled = os.environ.get('THOUGHTMACHINE_DEBUG') == '1'
        filter_text = self.filter_lineedit.text()
        filter_type = self.filter_type_combo.currentText()
        # Skip if filter hasn't changed
        if filter_text == self._last_filter_text and filter_type == self._last_filter_type:
            return
        self._last_filter_text = filter_text
        self._last_filter_type = filter_type
        if debug_enabled:
            print(f"[OutputPanel] _apply_filter: text='{filter_text}', type='{filter_type}'")
            traceback.print_stack(limit=10)
        self.filter_proxy_model.set_filter(filter_text, filter_type)
        # Rebuild the output document with filtered events
        self._rebuild_output_document()
        # Still trigger auto-scroll if enabled
        self.smart_scroller.deferred_scroll_to_bottom()

    def _format_event_html(self, event):
        """Format event as HTML for display in QTextEdit."""
        delegate = EventDelegate()
        return delegate._event_to_html(event)

    def _event_passes_filter(self, event):
        """Check if event passes current filter criteria."""
        filter_type = self.filter_proxy_model.filter_type
        if filter_type != "all":
            if event.get("type") != filter_type:
                return False
        filter_text = self.filter_proxy_model.filter_text
        if filter_text:
            content = event.get("content", "").lower()
            reasoning = event.get("reasoning", "").lower()
            tool_calls = event.get("tool_calls", [])
            tool_text = " ".join([tc.get("name", "") + " " + str(tc.get("arguments", "")) for tc in tool_calls]).lower()
            if (filter_text not in content and
                filter_text not in reasoning and
                filter_text not in tool_text and
                filter_text not in event.get("type", "").lower()):
                return False
        return True

    def _rebuild_output_document(self):
        """Rebuild the output text document from filtered events."""
        self.output_textedit.clear()
        delegate = EventDelegate()
        
        # Group events by turn
        turns = {}
        for row in range(self.filter_proxy_model.rowCount()):
            index = self.filter_proxy_model.index(row, 0)
            event = index.data(Qt.ItemDataRole.UserRole)
            if not event:
                continue
                
            etype = event.get('type', 'unknown')
            turn_num = event.get('turn', 0)
            
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
        for i, (turn_num, turn_data) in enumerate(sorted_turns):
            html = delegate._turn_to_html(turn_num, turn_data)
            if html:
                cursor = self.output_textedit.textCursor()
                cursor.movePosition(QTextCursor.MoveOperation.End)
                if i > 0:  # Add separator between turns, not before first turn
                    cursor.insertHtml("<hr style='margin: 20px 0; border: 1px solid #ccc;'>")
                cursor.insertHtml(html)



    def display_event(self, event):
        """Add an event to the output display."""
        # Add to model immediately
        self.event_model.add_event(event)
        
        # Batch display updates for performance
        self._pending_events.append(event)
        self._batch_update_timer.start()
    
    def _process_batched_events(self):
        """Process batched events by rebuilding the entire output document."""
        if not self._pending_events:
            return
        
        # Clear pending events (they're already added to model in display_event)
        self._pending_events.clear()
        
        # Rebuild entire output document to ensure proper turn grouping
        self._rebuild_output_document()
        
        # Scroll to bottom if auto-scroll enabled
        self.smart_scroller.deferred_scroll_to_bottom()

    def _scroll_to_bottom(self):
        """Scroll output to bottom (for backward compatibility)."""
        self.smart_scroller.deferred_scroll_to_bottom()

    def clear_output(self):
        """Clear all output."""
        self.event_model.clear()
        self.output_textedit.clear()
        self._pending_events.clear()


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

    def display_loaded_conversation(self, events):
        """Display a loaded conversation from history."""
        self.clear_output()
        # Add all events to the model first
        for event in events:
            self.event_model.add_event(event)
        # Reset auto-scroll for loaded content
        self.smart_scroller.reset_auto_scroll()
        # Rebuild output document with all events
        self._rebuild_output_document()
        self.smart_scroller.deferred_scroll_to_bottom()
