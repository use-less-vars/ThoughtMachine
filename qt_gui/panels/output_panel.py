"""Output Panel - Event display and output area for the agent."""
import html
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QComboBox,
    QTextEdit, QFrame, QScrollArea
)
from PyQt6.QtCore import Qt, QTimer


# Import from other extracted modules
from qt_gui.panels.event_models import EventModel, EventFilterProxyModel, EventDelegate
from qt_gui.panels.markdown_renderer import MarkdownRenderer
from qt_gui.utils.constants import MAX_RESULT_LENGTH
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

    def _format_event_html(self, event):
        """Convert event dictionary to HTML representation."""
        etype = event.get('type', 'unknown')
        detail_level = event.get('_detail_level', 'normal')

        # Use EventDelegate's method for consistent formatting
        delegate = EventDelegate()
        return delegate._event_to_html(event)



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
        self._rebuild_output_document()
        self.smart_scroller.deferred_scroll_to_bottom()

    def _rebuild_output_document(self):
        """Rebuild the output text document from filtered events."""
        import traceback
        import os
        debug_enabled = os.environ.get('THOUGHTMACHINE_DEBUG') == '1'
        if debug_enabled:
            print("[OutputPanel] _rebuild_output_document called from:")
            traceback.print_stack(limit=10)
        row_count = self.filter_proxy_model.rowCount()
        source_row_count = self.event_model.rowCount()
        if debug_enabled:
            print(f"[OutputPanel] _rebuild_output_document: source rows={source_row_count}, filtered rows={row_count}")
        
        # Build complete HTML document
        html_parts = []
        delegate = EventDelegate()
        for row in range(row_count):
            index = self.filter_proxy_model.index(row, 0)
            event = index.data(Qt.ItemDataRole.UserRole)
            if event:
                html = delegate._event_to_html(event)
                html_parts.append(html)
        
        # Set entire document at once
        self.smart_scroller.pause_tracking()
        try:
            self.output_textedit.clear()
            if html_parts:
                # Join HTML parts (no separator needed as each part is self-contained)
                full_html = "".join(html_parts)
                self.output_textedit.setHtml(full_html)
        finally:
            self.smart_scroller.resume_tracking()

    def display_event(self, event):
        """Add an event to the output display."""
        # Add to model immediately
        self.event_model.add_event(event)
        
        # Batch display updates for performance
        self._pending_events.append(event)
        self._batch_update_timer.start()
    
    def _process_batched_events(self):
        """Process batched events and rebuild output."""
        if not self._pending_events:
            return
        
        # Clear the pending list
        events = self._pending_events.copy()
        self._pending_events.clear()
        
        # Rebuild output document with all events
        self._rebuild_output_document()
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
        # Rebuild document once
        self._rebuild_output_document()
        self.smart_scroller.deferred_scroll_to_bottom()
