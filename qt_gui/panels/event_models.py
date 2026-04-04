"""Event model, filter proxy, and delegate for event list display."""
import html
import datetime
from PyQt6.QtCore import Qt, QModelIndex, QVariant, QSortFilterProxyModel, QSize, QPoint, QAbstractListModel
from PyQt6.QtWidgets import (
    QStyledItemDelegate, QStyleOptionViewItem, QStyle,
    QFrame, QLabel, QVBoxLayout, QSizePolicy
)
from PyQt6.QtGui import QPainter, QPalette, QTextDocument

# Import from other extracted modules
from .markdown_renderer import MarkdownRenderer

from ..utils.constants import MAX_RESULT_LENGTH, MAX_TOOL_RESULTS_PER_TURN, MAX_LINES_PER_RESULT, ENABLE_RESULT_TRUNCATION, INTERNAL_EVENT_TYPES
from ..debug_log import debug_log


class EventModel(QAbstractListModel):
    """Model for storing and displaying events in a list view."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.events = []  # List of event dictionaries

    def rowCount(self, parent=QModelIndex()):
        return len(self.events)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self.events):
            return QVariant()

        event = self.events[index.row()]

        if role == Qt.ItemDataRole.DisplayRole:
            # Return a simple text representation for debugging
            return f"{event.get('type', 'unknown')}: {event.get('content', '')[:50]}..."
        elif role == Qt.ItemDataRole.UserRole:
            # Return the full event dictionary for the delegate
            return event

        return QVariant()

    def add_event(self, event):
        """Add an event to the model."""
        # Remove processing indicators for this turn before adding user_query event
        etype = event.get('type', '')
        
        if etype == 'user_query':
            debug_log(f"[TIMESTAMP_DEBUG] EventModel.add_event: user_query event, turn={event.get('turn')}, created_at={event.get('created_at')}, timestamp={event.get('timestamp')}", level="DEBUG")
            # First, remove any processing indicators for this turn
            turn = event.get('turn', None)
            if turn is not None:
                # Find processing events with matching turn
                processing_indices = []
                for i, existing_event in enumerate(self.events):
                    if existing_event.get('type') == 'processing' and existing_event.get('turn') == turn:
                        processing_indices.append(i)
                # Remove in reverse order to maintain indices
                for idx in reversed(processing_indices):
                    self.beginRemoveRows(QModelIndex(), idx, idx)
                    self.events.pop(idx)
                    self.endRemoveRows()
        
        # Find correct insertion position based on chronological order
        position = self._find_insertion_position(event)
        
        # Insert event at correct position
        self.beginInsertRows(QModelIndex(), position, position)
        self.events.insert(position, event)
        self.endInsertRows()
    
    def _find_insertion_position(self, event):
        """Find the correct position to insert an event based on chronological order."""
        # Get event ordering key
        event_order = self._get_event_order_key(event)
        
        # Debug logging for user_query events
        if event.get('type') == 'user_query':
            debug_log(f"[TIMESTAMP_DEBUG] EventModel._find_insertion_position: user_query event order={event_order}, created_at={event.get('created_at')}, turn={event.get('turn')}", level="DEBUG")
            for i, existing_event in enumerate(self.events):
                existing_order = self._get_event_order_key(existing_event)
                debug_log(f"  [{i}] existing event type={existing_event.get('type')}, order={existing_order}, created_at={existing_event.get('created_at')}, turn={existing_event.get('turn')}", level="DEBUG")
        
        # Find first position where existing event has greater order key
        for i, existing_event in enumerate(self.events):
            existing_order = self._get_event_order_key(existing_event)
            if existing_order > event_order:
                return i
        
        # If no greater event found, append at end
        return len(self.events)
    
    def _normalize_timestamp(self, ts):
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

    def _get_event_order_key(self, event):
        """Get a sortable key for event ordering."""
        # Debug logging for user_query events
        if event.get('type') == 'user_query':
            debug_log(f"[TIMESTAMP_DEBUG] EventModel._get_event_order_key: user_query event, created_at={event.get('created_at')}, normalized={self._normalize_timestamp(event.get('created_at')) if 'created_at' in event else 'N/A'}, turn={event.get('turn')}", level="DEBUG")
        
        # Priority: created_at timestamp > turn number > type-based fallback
        
        # 1. created_at timestamp (microsecond precision)
        if "created_at" in event:
            return (1, self._normalize_timestamp(event["created_at"]))
        
        # 2. turn number (events within same turn need sub-ordering)
        if "turn" in event:
            # Normalize turn to integer for consistent comparison
            try:
                turn = int(event["turn"])
            except (ValueError, TypeError):
                turn = 0
            # Within same turn, order by event type to maintain logical flow
            type_order = {
                "user_query": 0,
                "tool_call": 1,
                "tool_result": 2,
                "turn": 3,
                "final": 4,
                "paused": 5,
                "stopped": 6,
                "error": 7,
                "token_update": 8,
                "token_warning": 9,
                "turn_warning": 10,
                "rate_limit_warning": 11,
                "user_interaction_requested": 12,
                "session_state_change": 13,
                "execution_state_change": 14
            }
            type_priority = type_order.get(event.get("type", ""), 999)
            return (2, turn, type_priority)
        
        # 3. No turn or timestamp - place at end
        return (3, 0)

    def clear(self):
        """Clear all events from the model."""
        if self.events:
            self.beginRemoveRows(QModelIndex(), 0, len(self.events) - 1)
            self.events.clear()
            self.endRemoveRows()


class EventFilterProxyModel(QSortFilterProxyModel):
    """Filter proxy model for event search and filtering."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.filter_text = ""
        self.filter_type = "all"

    def set_filter(self, text="", event_type="all"):
        """Set filter criteria."""
        self.filter_text = text.lower()
        self.filter_type = event_type
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row, source_parent):
        """Override to filter rows based on text and type."""
        model = self.sourceModel()
        if not model:
            return True

        index = model.index(source_row, 0, source_parent)
        event = model.data(index, Qt.ItemDataRole.UserRole)
        if not event:
            return False

        # Hide internal event types even when "all" is selected
        event_type = event.get("type", "")
        if event_type in INTERNAL_EVENT_TYPES:
            return False

        # Type filter
        if self.filter_type != "all":
            if event_type != self.filter_type:
                return False

        # Text filter
        if self.filter_text:
            # Search in content, reasoning, tool names, etc.
            search_text = self.filter_text
            content = event.get("content", "").lower()
            reasoning = event.get("reasoning", "").lower()
            # Handle tool-related events
            tool_text = ""
            etype = event.get("type", "")

            if etype in ["tool_call", "tool_result"]:
                # Search in tool_name for separate tool events
                tool_name = event.get("tool_name", event.get("name", ""))
                arguments = event.get("arguments", {})
                result = event.get("result", event.get("content", ""))
                tool_text = f"{tool_name} {arguments} {result}".lower()
            else:
                # Legacy: search in embedded tool_calls array
                tool_calls = event.get("tool_calls", [])
                tool_text = " ".join([
                    tc.get("name", "") + " " + str(tc.get("arguments", ""))
                    for tc in tool_calls
                ]).lower()

            if (search_text not in content and
                search_text not in reasoning and
                search_text not in tool_text):
                # Also check type
                if search_text not in etype.lower():
                    return False

        return True


class EventDelegate(QStyledItemDelegate):
    """Delegate for rendering events in the list view."""

    def __init__(self, parent=None):
        super().__init__(parent)

    def paint(self, painter, option, index):
        """Paint the event using HTML rendering."""
        # Get event data from model
        event = index.data(Qt.ItemDataRole.UserRole)
        if not event:
            super().paint(painter, option, index)
            return

        # Setup painter
        painter.save()

        # Draw background
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())
        else:
            painter.fillRect(option.rect, option.palette.base())

        # Create text document with HTML content
        doc = QTextDocument()
        doc.setHtml(self._event_to_html(event))

        # Adjust document width to fit within cell
        doc.setTextWidth(option.rect.width() - 10)  # 5px margin each side

        # Translate painter to rectangle position
        painter.translate(option.rect.topLeft() + QPoint(5, 5))

        # Draw the document
        doc.drawContents(painter)

        painter.restore()

    def sizeHint(self, option, index):
        """Calculate size needed for the event."""
        event = index.data(Qt.ItemDataRole.UserRole)
        if not event:
            return super().sizeHint(option, index)

        doc = QTextDocument()
        doc.setHtml(self._event_to_html(event))
        doc.setTextWidth(option.rect.width() - 10)  # Same as paint

        return QSize(int(doc.idealWidth()) + 10, int(doc.size().height()) + 10)

    def _event_to_html(self, event, suppress_turn_header=False, suppress_title_bar=False):
        """Convert event dictionary to HTML representation.
        
        Args:
            event: The event dictionary
            suppress_turn_header: If True, skip the "Turn X" line for turn events
            suppress_title_bar: If True, skip the event type title bar
        """
        etype = event.get('type', 'unknown')
        detail_level = event.get('_detail_level', 'normal')
        
        # DEBUG
        import os
        if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
            debug_log(f"[EventDelegate] _event_to_html called for type={etype}, suppress_title_bar={suppress_title_bar}", level="DEBUG")

        # Helper to add a content line
        lines = []

        def add_line(text, style='', use_markdown=False, title=''):
            # Unescape any HTML entities
            unescaped_text = html.unescape(text)
            if use_markdown:
                html_text = MarkdownRenderer.markdown_to_html(unescaped_text, '')
                # If style provided, wrap in div with that style
                if style:
                    lines.append(f'<div style="{style}">{html_text}</div>')
                else:
                    lines.append(html_text)
            else:
                # Escape HTML special characters
                escaped_text = html.escape(unescaped_text)
                if title:
                    lines.append(f'<div style="{style}" title="{html.escape(title)}">{escaped_text}</div>')
                else:
                    if style:
                        lines.append(f'<div style="{style}">{escaped_text}</div>')
                    else:
                        lines.append(f'<div>{escaped_text}</div>')

        # Title bar - suppress for events that have their own headers
        skip_title_events = ["user_query", "user_interaction_requested", "final", "finalreport", "processing"]
        if not suppress_title_bar and etype not in skip_title_events:
            html_content = f'<div style="font-weight: bold; background-color: #e0e0e0; padding: 3px; display: block; clear: both;">{html.escape(etype.upper())}</div>'
        else:
            html_content = ''
        
        # Content container
        html_content += '<div style="padding: 5px;">'

        if etype == "turn":
            turn = event.get("turn", "?")
            if not suppress_turn_header:
                add_line(f"Turn {turn}", style="font-weight: bold; margin-bottom: 4px; padding-bottom: 4px; border-bottom: 1px solid #e0e0e0;")

            assistant_content = event.get("assistant_content", "")
            if assistant_content:
                add_line(f"{assistant_content}", style="color: #000000; margin-bottom: 8px;", use_markdown=True)

            # Show reasoning
            if "reasoning" in event and event["reasoning"]:
                add_line(f"Reasoning: {event['reasoning']}", style="color: #666666; margin-bottom: 8px;", use_markdown=True)

            # Show tool calls
            # Show tool calls
            tool_calls = event.get("tool_calls", [])
            # Filter out Final/FinalReport/RequestUserInteraction tool calls since they get special events
            filtered_tool_calls = []
            for tc in tool_calls:
                tool_name = tc.get('name')
                if tool_name is None:
                    function = tc.get('function', {})
                    tool_name = function.get('name', 'Unknown')
                if tool_name and tool_name.lower() not in ["final", "finalreport", "requestuserinteraction"]:
                    filtered_tool_calls.append(tc)
            # Limit number of displayed tool calls
            display_calls = filtered_tool_calls[:MAX_TOOL_RESULTS_PER_TURN] if ENABLE_RESULT_TRUNCATION else filtered_tool_calls
            # Add separator before tool calls if any
            if display_calls:
                lines.append('<div style="margin-top: 16px; border-top: 1px solid #e0e0e0; padding-top: 8px;"></div>')
            for i, tc in enumerate(display_calls):
                # Add separator between tool calls (except before first)
                if i > 0:
                    lines.append('<div style="margin-top: 8px; border-top: 1px dotted #e0e0e0; padding-top: 8px;"></div>')
                
                # Normalize tool call format (support both flattened and OpenAI format)
                tool_name = tc.get('name')
                if tool_name is None:
                    function = tc.get('function', {})
                    tool_name = function.get('name', 'Unknown')
                tool_arguments = tc.get('arguments')
                if tool_arguments is None:
                    function = tc.get('function', {})
                    tool_arguments = function.get('arguments', {})

                # Always show tool name in bold as visual anchor
                add_line(f"🛠️ {tool_name}", style="color: #0000FF; font-weight: bold; margin-top: 10px; margin-bottom: 4px; display: block;")
                if detail_level == "verbose":
                    add_line(f"  Arguments: {tool_arguments}", style="color: #0000AA; font-size: 0.9em; margin-bottom: 2px;")
                elif detail_level == "normal" and tool_arguments:
                    # Show brief arguments in normal mode
                    args_str = str(tool_arguments)
                    if len(args_str) > 100:
                        args_str = args_str[:100] + "..."
                    add_line(f"  Arguments: {args_str}", style="color: #0000AA; font-size: 0.9em; margin-bottom: 2px;")

                # Result
                result_text = tc.get('result', '')
                if result_text:
                    # Truncate if needed
                    unescaped_result = html.unescape(result_text)
                    # Replace any HTML line breaks with newlines for consistent splitting
                    unescaped_result = unescaped_result.replace('<br>', '\n').replace('<br/>', '\n').replace('<br />', '\n')
                    
                    # Process line breaks: replace newlines with <br> for HTML display
                    lines_result = unescaped_result.split('\n')
                    
                    # Apply truncation
                    if ENABLE_RESULT_TRUNCATION:
                        # Limit lines
                        if len(lines_result) > MAX_LINES_PER_RESULT:
                            lines_result = lines_result[:MAX_LINES_PER_RESULT]
                            lines_result.append("...")
                        
                        # Limit characters per line and overall
                        truncated_lines = []
                        total_chars = 0
                        for line in lines_result:
                            if total_chars + len(line) > MAX_RESULT_LENGTH:
                                # Already approaching total limit, add ellipsis and stop
                                truncated_lines.append("...")
                                break
                            if len(line) > 100:  # Limit per line
                                line = line[:100] + "..."
                            truncated_lines.append(line)
                            total_chars += len(line)
                        
                        # Join with <br> for HTML display
                        display_text = '<br>'.join([html.escape(line) for line in truncated_lines])
                    else:
                        # Join with <br> for HTML display
                        display_text = '<br>'.join([html.escape(line) for line in lines_result])
                    
                    # Add result with green color and proper spacing
                    lines.append(f'<div style="color: #006400; margin-left: 10px; margin-top: 10px; font-size: 0.95em; display: block;"><b>Result:</b> {display_text}</div>')
            
            # Show truncation message if we limited tool calls
            if ENABLE_RESULT_TRUNCATION and len(tool_calls) > MAX_TOOL_RESULTS_PER_TURN:
                remaining = len(tool_calls) - MAX_TOOL_RESULTS_PER_TURN
                add_line(f"... and {remaining} more tool calls", style="color: #666666; font-style: italic;")

        elif etype == "final":
            # Skip specific GUI fix reports that are too verbose
            if event.get('content', '').startswith('## ✅ **All GUI Display Issues Fixed**'):
                return ''
            # Final answer with prominent header - use wrapper to avoid border gaps
            lines.append('<div style="border: 1px solid #99ccff; border-radius: 5px; margin-bottom: 8px; overflow: hidden;">')
            lines.append('<div style="background-color: #e6f7ff; padding: 8px 10px; font-weight: bold; border-bottom: 1px solid #99ccff;">FINAL ANSWER</div>')
            # Content with blue background styling
            add_line(f"{event['content']}", style="font-weight: bold; color: #000080; background-color: #f0f8ff; padding: 12px;", use_markdown=True)
            if "reasoning" in event and event["reasoning"]:
                add_line(f"{event['reasoning']}", style="color: #666666; font-style: italic; margin-top: 8px; margin-left: 10px;", use_markdown=True)
            lines.append('</div>')

        elif etype == "user_query":
            # User query with prominent header - use wrapper to avoid border gaps
            lines.append('<div style="border: 1px solid #cc99ff; border-radius: 5px; margin-bottom: 8px; overflow: hidden;">')
            lines.append('<div style="background-color: #f3e6ff; padding: 8px 10px; font-weight: bold; border-bottom: 1px solid #cc99ff;">USER QUERY</div>')
            add_line(f"{event.get('content', '')}", style="color: #8B008B; background-color: #f9f0ff; padding: 12px; font-weight: bold; margin: 0; border: none;", use_markdown=True)
            lines.append('</div>')

        elif etype == "tool_call":
            # Handle separate tool call events
            tool_name = event.get('tool_name', event.get('name', 'unknown'))
            # Skip Final/FinalReport/RequestUserInteraction tool calls since they get special events
            if tool_name and tool_name.lower() in ["final", "finalreport", "requestuserinteraction"]:
                
                return ''
            tool_call_id = event.get('tool_call_id', 'unknown')
            arguments = event.get('arguments', {})

            display_name = tool_name if tool_name != 'unknown' else f"call {tool_call_id}"

            # Show tool name as bold visual anchor
            add_line(f"🛠️ {display_name}", style="color: #0000FF; font-weight: bold; margin-top: 8px; margin-bottom: 2px; display: block; clear: both;")
            
            # Show arguments if available
            if arguments:
                args_str = str(arguments)
                if len(args_str) > 100:
                    args_str = args_str[:100] + "..."
                add_line(f"  Arguments: {args_str}", style="color: #0000AA; font-size: 0.9em; margin-bottom: 2px;")
        elif etype == "tool_result":
            # Handle both legacy and new formats
            tool_name = event.get('tool_name', event.get('name', 'unknown'))
            # Skip Final/FinalReport/RequestUserInteraction tool results since they get special events
            if tool_name and tool_name.lower() in ["final", "finalreport", "requestuserinteraction"]:
                
                return ''
            tool_call_id = event.get('tool_call_id', 'unknown')
            # Try content first (legacy), then result (new)
            result_text = event.get('content', event.get('result', ''))
            success = event.get('success', True)
            error = event.get('error')
            
            display_name = tool_name if tool_name != 'unknown' else f"call {tool_call_id}"
            
            if error:
                add_line(f"❌ Tool {display_name} failed: {error}", style="color: #FF0000; margin-top: 8px;", use_markdown=True)
            elif not success:
                add_line(f"⚠️ Tool {display_name} returned warning: {result_text}", style="color: #FFA500; margin-top: 8px;", use_markdown=True)
            else:
                # Success - display result with proper formatting
                if result_text:
                    # Truncate if needed
                    unescaped_result = html.unescape(result_text)
                    # Replace any HTML line breaks with newlines for consistent splitting
                    unescaped_result = unescaped_result.replace('<br>', '\n').replace('<br/>', '\n').replace('<br />', '\n')
                    
                    # Process line breaks: replace newlines with <br> for HTML display
                    lines_result = unescaped_result.split('\n')
                    
                    # Apply truncation
                    if ENABLE_RESULT_TRUNCATION:
                        # Limit lines
                        if len(lines_result) > MAX_LINES_PER_RESULT:
                            lines_result = lines_result[:MAX_LINES_PER_RESULT]
                            lines_result.append("…")
                        
                        # Limit characters per line and overall
                        truncated_lines = []
                        total_chars = 0
                        for line in lines_result:
                            if total_chars + len(line) > MAX_RESULT_LENGTH:
                                # Already approaching total limit, add ellipsis and stop
                                truncated_lines.append("…")
                                break
                            if len(line) > 100:  # Limit per line
                                line = line[:100] + "…"
                            truncated_lines.append(line)
                            total_chars += len(line)
                        
                        # Join with <br> for HTML display
                        display_text = '<br>'.join([html.escape(line) for line in truncated_lines])
                    else:
                        # Join with <br> for HTML display
                        display_text = '<br>'.join([html.escape(line) for line in lines_result])
                    
                    # Add result with green color and proper spacing
                    lines.append(f'<div style="color: #006400; margin-left: 10px; margin-top: 10px; font-size: 0.95em; display: block;"><b>Result:</b> {display_text}</div>')
        elif etype == "processing":
            # Processing indicator - gray italic with hourglass
            add_line(f"⏳ {event.get('content', '')}", style="color: #808080; font-style: italic; background-color: #f8f8f8; padding: 4px; border-radius: 3px;", use_markdown=False)
        elif etype == "system":
            add_line(f"System: {event.get('content', '')}", style="color: #808080; font-style: italic;", use_markdown=True)
            # Show full summary if present (from SummarizeTool)
            if 'summary' in event:
                add_line(f"<b>Summary:</b> {event['summary']}", style="color: #000000;")
                # Also add a separator
                lines.append('<hr>')

        elif etype == "stopped":
            add_line("Agent stopped by user.", style="color: #FF8C00;")
        elif etype == "user_interaction_requested":
            # User interaction request with prominent header - use wrapper to avoid border gaps
            lines.append('<div style="border: 1px solid #99ccff; border-radius: 5px; margin-bottom: 8px; overflow: hidden;">')
            lines.append('<div style="background-color: #e6f3ff; padding: 8px 10px; font-weight: bold; border-bottom: 1px solid #99ccff;">USER INTERACTION REQUESTED</div>')
            # Message with light blue background styling
            message = event.get('message', '')
            if message:
                add_line(f"{message}", style="color: #006699; background-color: #f0faff; padding: 12px; margin: 0; border: none;", use_markdown=True)
            else:
                add_line("Agent requests interaction", style="color: #006699; background-color: #f0faff; padding: 12px; margin: 0; border: none;", use_markdown=True)
            lines.append('</div>')
        elif etype == "token_warning":
            add_line(event.get("message", ""), style="color: #FFA500; font-weight: bold;")
        elif etype == "turn_warning":
            add_line(event.get("message", ""), style="color: #FFA500; font-weight: bold;")
        elif etype == "rate_limit_warning":
            add_line(event.get("message", ""), style="color: #FF8C00; font-weight: bold;")
        elif etype == "paused":
            add_line("Agent paused, ready for next query.", style="color: #808080;")
        elif etype == "max_turns":
            add_line("Max turns reached without final answer.", style="color: #FF8C00;")
        elif etype == "error":
            add_line(f"ERROR: {event.get('message')}", style="color: #FF0000; font-weight: bold;")
            if "traceback" in event and detail_level == "verbose":
                add_line(event['traceback'], style="color: #FF0000;")
        elif etype == "thread_finished":
            add_line("Background thread finished.", style="color: #808080;")
        else:
            add_line(str(event))

        # Append lines
        for line in lines:
            html_content += line

        html_content += '</div>'
        return html_content

    def _turn_to_html(self, turn_num, turn_data):
        """Convert turn data to HTML representation with grouped events."""
        html_content = f'<div style="border: 1px solid #ddd; border-radius: 5px; margin: 10px 0; overflow: hidden;">'
        
        # Turn header
        html_content += f'<div style="background-color: #f0f0f0; padding: 8px 10px; font-weight: bold; border-bottom: 1px solid #ddd;">Turn {turn_num}</div>'
        
        # Content area
        html_content += '<div style="padding: 10px;">'
        
        # User query with prominent header (purple)
        user_query = turn_data.get('user_query')
        if user_query:
            content = user_query.get('content', '')
            if content:
                html_content += '<div style="border: 1px solid #cc99ff; border-radius: 5px; margin-bottom: 8px; overflow: hidden;">'
                html_content += '<div style="background-color: #f3e6ff; padding: 8px 10px; font-weight: bold; border-bottom: 1px solid #cc99ff;">USER QUERY</div>'
                html_content += f'<div style="color: #8B008B; background-color: #f9f0ff; padding: 12px; font-weight: bold; margin: 0; border: none;">{html.escape(content)}</div>'
                html_content += '</div>'
        
        # Assistant content (grey)
        assistant = turn_data.get('assistant')
        if assistant:
            content = assistant.get('assistant_content', '')
            if content:
                html_content += f'<div style="color: #000000; margin-bottom: 8px;">Assistant: {html.escape(content)}</div>'
            
            # Reasoning (grey)
            reasoning = assistant.get('reasoning', '')
            if reasoning:
                html_content += f'<div style="color: #666666; font-style: italic; margin-bottom: 8px; padding-left: 10px; border-left: 2px solid #ccc;">Reasoning: {html.escape(reasoning)}</div>'
        
        # Tool calls (blue) and results (green)
        tool_calls = turn_data.get('tool_calls', [])
        tool_results = turn_data.get('tool_results', [])
        
        # Filter out Final/FinalReport/RequestUserInteraction tool calls since they get special events
        filtered_tool_calls = []
        for tc in tool_calls:
            tool_name = tc.get('tool_name')
            if tool_name is None:
                tool_name = tc.get('name', 'Unknown')
            if tool_name and tool_name.lower() not in ["final", "finalreport", "requestuserinteraction"]:
                filtered_tool_calls.append(tc)
        
        # Also filter tool_results for the same tools
        filtered_tool_results = []
        for tr in tool_results:
            tool_name = tr.get('tool_name', tr.get('name', 'Unknown'))
            if tool_name and tool_name.lower() not in ["final", "finalreport", "requestuserinteraction"]:
                filtered_tool_results.append(tr)
        
        # Add separator before tool calls if any
        if filtered_tool_calls:
            html_content += '<div style="margin-top: 16px; border-top: 1px solid #e0e0e0; padding-top: 8px;"></div>'
        
        # Match tool calls with results
        for i, tool_call in enumerate(filtered_tool_calls):
            # Add separator between tool calls (except before first)
            if i > 0:
                html_content += '<div style="margin-top: 8px; border-top: 1px dotted #e0e0e0; padding-top: 8px;"></div>'
            
            tool_name = tool_call.get('tool_name')
            if tool_name is None:
                tool_name = tool_call.get('name', 'Unknown')
            # Ensure tool_name is string
            tool_name = str(tool_name) if tool_name is not None else 'Unknown'
            arguments = tool_call.get('arguments', {})
            
            html_content += f'<div style="color: #0000FF; font-weight: bold; margin: 8px 0 4px 0; display: block; clear: both;">🛠️ {html.escape(tool_name)}</div>'
            
            # Show arguments if available
            if arguments:
                html_content += f'<div style="color: #0000AA; font-size: 0.9em; margin-left: 10px; margin-bottom: 4px;">Arguments: {html.escape(str(arguments))}</div>'
            
            # Find matching result
            result_text = ''
            for result in filtered_tool_results:
                # Match by tool_name or name field
                result_tool_name = result.get('tool_name', result.get('name'))
                if result_tool_name == tool_name:
                    # Try result field first, then content field for backward compatibility
                    result_text = result.get('result', result.get('content', ''))
                    break
            # If no result found in tool_results, check if tool_call has result
            if not result_text:
                result_text = tool_call.get('result', '')
            
            # Ensure result_text is string
            result_text = str(result_text) if result_text is not None else ''
            if result_text:
                # Truncate result if needed
                
                unescaped_result = html.unescape(result_text)
                # Replace any HTML line breaks with newlines for consistent splitting
                unescaped_result = unescaped_result.replace('<br>', '\n').replace('<br/>', '\n').replace('<br />', '\n')
                
                if ENABLE_RESULT_TRUNCATION:
                    # Limit lines
                    lines_result = unescaped_result.split('\n')
                    if len(lines_result) > MAX_LINES_PER_RESULT:
                        lines_result = lines_result[:MAX_LINES_PER_RESULT]
                        lines_result.append("...")
                        unescaped_result = '\n'.join(lines_result)
                    
                    # Limit characters
                    if len(unescaped_result) > MAX_RESULT_LENGTH:
                        truncated = unescaped_result[:MAX_RESULT_LENGTH] + "..."
                        html_content += f'<div style="color: #006400; margin-left: 10px; font-size: 0.9em;" title="{html.escape(unescaped_result)}">Result: {html.escape(truncated)}</div>'
                    else:
                        html_content += f'<div style="color: #006400; margin-left: 10px; font-size: 0.9em;">Result: {html.escape(unescaped_result)}</div>'
                else:
                    html_content += f'<div style="color: #006400; margin-left: 10px; font-size: 0.9em;">Result: {html.escape(unescaped_result)}</div>'
            
            html_content += '<div style="margin-bottom: 8px;"></div>'  # Spacer
        
        # Final output (special user interaction message)
        final = turn_data.get('final')
        # Skip specific GUI fix reports that are too verbose
        if final and final.get('content', '').startswith('## ✅ **All GUI Display Issues Fixed**'):
            final = None
        if final:
            content = final.get('content', '')
            if content:
                # Add final header with wrapper to avoid border gaps
                html_content += '<div style="border: 1px solid #99ccff; border-radius: 5px; margin-bottom: 8px; overflow: hidden;">'
                html_content += '<div style="background-color: #e6f7ff; padding: 8px 10px; font-weight: bold; border-bottom: 1px solid #99ccff;">FINAL ANSWER</div>'
                # Use markdown rendering for final output with new styling
                rendered_content = MarkdownRenderer.markdown_to_html(content)
                html_content += f'<div style="color: #000080; font-weight: bold; background-color: #f0f8ff; padding: 12px;">{rendered_content}</div>'

            # Include reasoning if present (always visible in compact display)
            reasoning = final.get('reasoning')
            if reasoning:
                rendered_reasoning = MarkdownRenderer.markdown_to_html(reasoning)
                html_content += f'<div style="color: #666666; font-style: italic; margin-top: 8px; margin-left: 10px;">{rendered_reasoning}</div>'
            # Close wrapper div
            html_content += '</div>'        
        # Other events (system messages, warnings, etc.)
        other_events = turn_data.get('other_events', [])
        for event in other_events:
            etype = event.get('type', 'unknown')
            content = event.get('content', event.get('message', ''))
            
            if etype == 'system':
                html_content += f'<div style="color: #808080; font-style: italic; margin-top: 8px;">System: {html.escape(content)}</div>'
            elif etype in ['token_warning', 'turn_warning', 'rate_limit_warning']:
                html_content += f'<div style="color: #FFA500; font-weight: bold; margin-top: 8px;">⚠️ {html.escape(content)}</div>'
            elif etype == 'error':
                html_content += f'<div style="color: #FF0000; font-weight: bold; margin-top: 8px;">❌ {html.escape(content)}</div>'
            elif etype == 'user_interaction_requested':
                # Use markdown rendering for user interaction messages with header
                html_content += '<div style="border: 1px solid #99ccff; border-radius: 5px; margin-bottom: 8px; overflow: hidden;">'
                html_content += '<div style="background-color: #e6f3ff; padding: 8px 10px; font-weight: bold; border-bottom: 1px solid #99ccff;">USER INTERACTION REQUESTED</div>'
                rendered_content = MarkdownRenderer.markdown_to_html(content)
                html_content += f'<div style="color: #006699; background-color: #f0faff; padding: 12px; margin: 0; border: none;">👤 {rendered_content}</div>'
                html_content += '</div>'
        
        html_content += '</div>'  # Close content area
        html_content += '</div>'  # Close turn container
        return html_content

    def _event_to_plain_text(self, event):
        """Convert event dictionary to plain text representation for copying."""
        etype = event.get('type', 'unknown')
        detail_level = event.get('_detail_level', 'normal')

        lines = []

        def add_line(text):
            # Unescape any HTML entities
            unescaped_text = html.unescape(text)
            lines.append(unescaped_text)

        # Title/type
        lines.append(f"{etype.upper()}")
        lines.append("=" * len(etype))

        if etype == "turn":
            turn = event.get("turn", "?")
            add_line(f"Turn {turn}")

            assistant_content = event.get("assistant_content", "")
            if assistant_content and detail_level != "minimal":
                add_line(f"Assistant: {assistant_content}")

            # Show reasoning
            if "reasoning" in event and event["reasoning"]:
                add_line(f"Reasoning: {event['reasoning']}")

            # Show tool calls
            for tc in event.get("tool_calls", []):
                if detail_level == "minimal":
                    add_line(f"Tool: {tc['name']}")
                else:
                    add_line(f"Tool: {tc['name']}")
                    if detail_level == "verbose":
                        add_line(f"  Arguments: {tc['arguments']}")

                # Result
                result_text = tc.get('result', '')
                unescaped_result = html.unescape(result_text)
                if len(unescaped_result) > MAX_RESULT_LENGTH:
                    truncated = unescaped_result[:MAX_RESULT_LENGTH] + "..."
                    add_line(f"Result: {truncated}")
                else:
                    add_line(f"Result: {unescaped_result}")

        elif etype == "final":
            # Skip specific GUI fix reports that are too verbose
            if event.get('content', '').startswith('## ✅ **All GUI Display Issues Fixed**'):
                return ''
            add_line(f"Final answer: {event['content']}")
            if detail_level != "minimal" and "reasoning" in event and event["reasoning"]:
                add_line(f"Reasoning: {event['reasoning']}")

        elif etype == "user_query":
            add_line(f"User query: {event.get('content', '')}")

        elif etype == "tool_call":
            # Handle separate tool call events
            tool_name = event.get('tool_name', event.get('name', 'unknown'))
            # Skip Final/FinalReport tool calls since they get special final event
            if tool_name in ["Final", "FinalReport"]:
                return ''
            tool_call_id = event.get('tool_call_id', 'unknown')
            arguments = event.get('arguments', {})

            display_name = tool_name if tool_name != 'unknown' else f"call {tool_call_id}"            
            if arguments:
                add_line(f"Tool call: {display_name} with arguments: {arguments}")
            else:
                add_line(f"Tool call: {display_name}")

        elif etype == "tool_result":
            # Handle both legacy and new formats
            tool_name = event.get('tool_name', event.get('name', 'unknown'))
            # Skip Final/FinalReport tool results since they get special final event
            if tool_name in ["Final", "FinalReport"]:
                return ''
            tool_call_id = event.get('tool_call_id', 'unknown')
            # Try content first (legacy), then result (new)
            result_text = event.get('content', event.get('result', ''))
            success = event.get('success', True)
            error = event.get('error')
            
            display_name = tool_name if tool_name != 'unknown' else f"call {tool_call_id}"
            
            if error:
                add_line(f"Tool {display_name} failed: {error}")
            elif not success:
                add_line(f"Tool {display_name} returned warning: {result_text}")
            else:
                add_line(f"Tool {display_name} result: {result_text}")

        elif etype == "system":
            add_line(f"System: {event.get('content', '')}")
            # Show full summary if present (from SummarizeTool)
            if 'summary' in event:
                add_line(f"Summary: {event['summary']}")

        elif etype == "stopped":
            add_line("Agent stopped by user.")
        elif etype == "user_interaction_requested":
            message = event.get('message', '')
            if message:
                add_line(f"{message}")
            else:
                add_line("Agent requests interaction")
        elif etype == "token_warning":
            add_line(event.get("message", ""))
        elif etype == "turn_warning":
            add_line(event.get("message", ""))
        elif etype == "rate_limit_warning":
            add_line(event.get("message", ""))
        elif etype == "paused":
            add_line("Agent paused, ready for next query.")
        elif etype == "max_turns":
            add_line("Max turns reached without final answer.")
        elif etype == "error":
            add_line(f"ERROR: {event.get('message')}")
            if "traceback" in event and detail_level == "verbose":
                add_line(event['traceback'])
        elif etype == "thread_finished":
            add_line("Background thread finished.")
        else:
            add_line(str(event))

        # Add token usage and context length if available
        if "context_length" in event:
            add_line(f"Context length: {event['context_length']} tokens")

        if "usage" in event:
            usage = event["usage"]
            if "input" in usage and "output" in usage:
                add_line(f"Token usage (this event): input {usage['input']}, output {usage['output']}")
            if "total_input" in usage and "total_output" in usage:
                add_line(f"Cumulative tokens: input {usage['total_input']}, output {usage['total_output']}")

        return '\n'.join(lines)


class EventFrame(QFrame):
    """A frame that holds a single event with structured content lines."""

    def __init__(self, title, event_type, parent=None):
        super().__init__(parent)
        self.setFrameStyle(QFrame.Shape.Box)
        self.setLineWidth(1)
        layout = QVBoxLayout()
        layout.setSpacing(2)

        # Title
        title_label = QLabel(f"<b>{title}</b>")
        title_label.setStyleSheet("background-color: #e0e0e0; padding: 3px;")
        layout.addWidget(title_label)

        # Content area
        self.content_layout = QVBoxLayout()
        self.content_layout.setSpacing(2)
        layout.addLayout(self.content_layout)

        self.setLayout(layout)

    def add_content_line(self, text, style="", use_markdown=False):
        """Add a simple text line (label)."""
        # Unescape any HTML entities in the text for PlainText format
        unescaped_text = html.unescape(text)

        if use_markdown:
            # Convert markdown to HTML using Qt's built-in markdown support
            html_text = MarkdownRenderer.markdown_to_html(unescaped_text, style)
            label = QLabel(html_text)
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.RichText)
            # Don't apply style sheet for markdown labels - already handled inline
        else:
            # Use plain text format
            label = QLabel(unescaped_text)
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            if style:
                label.setStyleSheet(style)

        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        # Set size policy to allow vertical expansion for wrapped text
        label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.content_layout.addWidget(label)
