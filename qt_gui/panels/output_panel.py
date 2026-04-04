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
        

        self.init_ui()
        self.turn_container_manager = TurnContainerManager(self.output_textedit, self.filter_proxy_model)
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
        
        # Sort events chronologically for correct display order
        # Priority: created_at timestamp > turn number > original order
        import datetime
        from ..debug_log import debug_log

        # Debug: print timestamps before sorting
        debug_log(f"[TIMESTAMP_DEBUG] display_loaded_conversation: received {len(events)} events", level="DEBUG")
        for i, event in enumerate(events):
            created_at = event.get('created_at')
            turn = event.get('turn')
            event_type = event.get('type', 'unknown')
            debug_log(f"[TIMESTAMP_DEBUG] Event {i}: type={event_type}, created_at={created_at}, turn={turn}", level="DEBUG")

        def normalize_timestamp(ts):           
            """Convert timestamp to float for consistent comparison."""
            if ts is None:
                return 0.0
            # Handle float/int timestamps
            if isinstance(ts, (int, float)):
                return float(ts)
            # Handle ISO string timestamps
            if isinstance(ts, str):
                try:
                    # Try to parse ISO format
                    dt = datetime.datetime.fromisoformat(ts.replace('Z', '+00:00'))
                    return dt.timestamp()
                except (ValueError, AttributeError):
                    # Try to convert numeric string
                    try:
                        return float(ts)
                    except (ValueError, TypeError):
                        return 0.0
            # Fallback
            return 0.0
        
        def normalize_turn(turn_val):
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
        
        def get_event_order(event):
            # Priority: created_at timestamp > turn number > original order
            # Use tuple (priority, value) to ensure comparable types
            if "created_at" in event:
                ts = event["created_at"]
                normalized = normalize_timestamp(ts)
                return (0, normalized)  # timestamp priority 0
            if "turn" in event:
                return (1, normalize_turn(event["turn"]))        # turn priority 1
            return (2, 0)                        # fallback priority 2
        
        sorted_events = sorted(events, key=get_event_order)

        # Debug: print timestamps after sorting
        debug_log(f"[TIMESTAMP_DEBUG] After sorting: {len(sorted_events)} events", level="DEBUG")
        for i, event in enumerate(sorted_events):
            created_at = event.get('created_at')
            turn = event.get('turn')
            event_type = event.get('type', 'unknown')
            order_tuple = get_event_order(event)
            debug_log(f"[TIMESTAMP_DEBUG] Sorted event {i}: type={event_type}, created_at={created_at}, turn={turn}, order_key={order_tuple}", level="DEBUG")
        
        # Verify ordering
        prev_order = None
        for i, event in enumerate(sorted_events):
            current_order = get_event_order(event)
            if prev_order is not None:
                if current_order < prev_order:
                    debug_log(f"[TIMESTAMP_WARNING] Order violation at position {i}: {current_order} < {prev_order}", level="WARNING")
            prev_order = current_order

        # Add all events to the model first
        for event in sorted_events:
            self.event_model.add_event(event)        # Reset auto-scroll for loaded content
        self.smart_scroller.reset_auto_scroll()
        # Append events incrementally for consistent rendering
        self.turn_container_manager.append_new_events()
        self.smart_scroller.deferred_scroll_to_bottom()
