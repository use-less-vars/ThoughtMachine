"""Turn Container Manager - Manages turn grouping and incremental display state."""
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QTextCursor

from .event_models import EventDelegate
from ..debug_log import debug_log


class TurnContainerManager:
    """Manages turn container state and incremental event display."""
    
    def __init__(self, output_widget, proxy_model):
        """
        Initialize the turn container manager.
        
        Args:
            output_widget: QTextEdit widget where output is displayed
            proxy_model: EventFilterProxyModel used for filtering events
        """
        self.output_widget = output_widget
        self.proxy_model = proxy_model
        self.delegate = EventDelegate()
        
        # State variables
        self.last_row_count = 0
        self.last_displayed_turn = -1
        self.cached_turns = {}
        self.last_appended_turn = -1
        self.open_turn_container = None
    
    def reset(self):
        """Reset all incremental display state."""
        self.last_row_count = 0
        self.last_displayed_turn = -1
        self.cached_turns.clear()
        self.last_appended_turn = -1
        self.open_turn_container = None
    
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

    def append_new_events(self):
        """Append new events to output widget incrementally."""
        current_row_count = self.proxy_model.rowCount()
        if current_row_count <= self.last_row_count:
            return
        
        # Process only new rows
        for row in range(self.last_row_count, current_row_count):
            index = self.proxy_model.index(row, 0)
            event = index.data(Qt.ItemDataRole.UserRole)
            if not event:
                continue
                
            etype = event.get('type', 'unknown')
            turn_num = self._normalize_turn(event.get('turn', 0))
            
            # Add turn header if this is a new turn
            if turn_num != self.last_displayed_turn and turn_num > 0:
                self._append_turn_header(turn_num)
                self.last_displayed_turn = turn_num
            
            # Format and append event HTML
            suppress_turn_header = (etype == "turn")
            # Suppress title bar for tool calls/results within a turn
            top_level_event_types = {"turn", "user_query", "final", "system", "error", "stopped", 
                                     "user_interaction_requested", "token_warning", "turn_warning", 
                                     "rate_limit_warning", "paused", "max_turns", "thread_finished"}
            suppress_title_bar = (etype not in top_level_event_types) and (turn_num > 0)
            html = self.delegate._event_to_html(event, suppress_turn_header=suppress_turn_header, 
                                              suppress_title_bar=suppress_title_bar)
            if html:
                # DEBUG: Print HTML
                import os
                if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                    debug_log(f"[TurnContainer] Inserting HTML for {etype}, turn {turn_num}: {repr(html[:500])}", level="DEBUG")
                    debug_log(f"[TurnContainer] HTML length: {len(html)}", level="DEBUG")
                cursor = self.output_widget.textCursor()
                cursor.movePosition(QTextCursor.MoveOperation.End)
                # Close container for turn 0 events
                if turn_num == 0 and self.open_turn_container is not None:
                    cursor.insertHtml("</div></div>")
                    self.open_turn_container = None
                # Add separator only if there's already content AND we're still in the same turn
                # (don't add separator right after turn header)
                if self.output_widget.document().characterCount() > 0 and self.last_appended_turn == turn_num:
                    # For tool calls and results, add a light horizontal line for visual separation
                    if etype in ["tool_call", "tool_result"]:
                        cursor.insertHtml("<hr style='margin: 4px 0; border: 1px solid #eee;'>")
                    else:
                        # Minimal separator for other events within same turn
                        cursor.insertHtml("<div style='margin: 8px 0;'></div>")
                cursor.insertHtml(html)
                self.last_appended_turn = turn_num
        
        self.last_row_count = current_row_count
    
    def _append_turn_header(self, turn_num):
        """Append a turn header to the output."""
        cursor = self.output_widget.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        
        # Close previous turn container if open and different turn
        if self.open_turn_container is not None and self.open_turn_container != turn_num:
            # Close content area first, then turn container
            cursor.insertHtml("</div></div>")
            self.open_turn_container = None
        # Add separator before new turn (except at very beginning)
        if self.output_widget.document().characterCount() > 0:
            cursor.insertHtml("<hr style='margin: 10px 0; border: 1px solid #ddd;'>")
        # Open new turn container
        cursor.insertHtml('<div style="border: 1px solid #ddd; border-radius: 5px; margin: 10px 0; overflow: hidden;">')
        # Turn header
        header_html = f'''<div style="background-color: #f0f0f0; padding: 8px 10px; font-weight: bold; border-radius: 5px 5px 0 0; border: 1px solid #ddd; border-bottom: none; margin-bottom: 5px; display: block; width: 100%; clear: both;">Turn {turn_num}</div>'''
        cursor.insertHtml(header_html)
        # Open content area for turn events
        cursor.insertHtml('<div style="padding: 10px;">')
        self.open_turn_container = turn_num
    
    def close_open_turn_container(self):
        """Close any open turn container."""
        if self.open_turn_container is not None:
            cursor = self.output_widget.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            # Close content area div and container div
            cursor.insertHtml("</div>\n</div>")
            self.open_turn_container = None