"""Event model, filter proxy, and delegate for event list display."""
import html
from PyQt6.QtCore import Qt, QModelIndex, QVariant, QSortFilterProxyModel, QSize, QPoint, QAbstractListModel
from PyQt6.QtWidgets import (
    QStyledItemDelegate, QStyleOptionViewItem, QStyle,
    QFrame, QLabel, QVBoxLayout, QSizePolicy
)
from PyQt6.QtGui import QPainter, QPalette, QTextDocument

# Import from other extracted modules
from qt_gui.panels.markdown_renderer import MarkdownRenderer

from qt_gui.utils.constants import MAX_RESULT_LENGTH


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
        position = len(self.events)
        self.beginInsertRows(QModelIndex(), position, position)
        self.events.append(event)
        self.endInsertRows()

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

        # Type filter
        if self.filter_type != "all":
            if event.get("type") != self.filter_type:
                return False

        # Text filter
        if self.filter_text:
            # Search in content, reasoning, tool names, etc.
            search_text = self.filter_text
            content = event.get("content", "").lower()
            reasoning = event.get("reasoning", "").lower()
            # Also search in tool calls
            tool_calls = event.get("tool_calls", [])
            tool_text = " ".join([tc.get("name", "") + " " + str(tc.get("arguments", "")) for tc in tool_calls]).lower()
            if (search_text not in content and
                search_text not in reasoning and
                search_text not in tool_text):
                # Also check type
                if search_text not in event.get("type", "").lower():
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

    def _event_to_html(self, event):
        """Convert event dictionary to HTML representation."""
        etype = event.get('type', 'unknown')
        detail_level = event.get('_detail_level', 'normal')

        # Helper to add a content line
        lines = []

        def add_line(text, style='', use_markdown=False, title=''):
            # Unescape any HTML entities
            unescaped_text = html.unescape(text)
            if use_markdown:
                html_text = MarkdownRenderer.markdown_to_html(unescaped_text, style)
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

        # Title bar
        html_content = f'<div style="font-weight: bold; background-color: #e0e0e0; padding: 3px;">{html.escape(etype.upper())}</div>'

        # Content container
        html_content += '<div style="padding: 5px;">'

        if etype == "turn":
            turn = event.get("turn", "?")
            add_line(f"Turn {turn}", style="font-weight: bold;")

            assistant_content = event.get("assistant_content", "")
            if assistant_content and detail_level != "minimal":
                add_line(f"Assistant: {assistant_content}", style="color: #000000;", use_markdown=True)

            # Show reasoning
            if detail_level != "minimal" and "reasoning" in event and event["reasoning"]:
                add_line(f"Reasoning: {event['reasoning']}", style="color: #666666;", use_markdown=True)

            # Show tool calls
            for tc in event.get("tool_calls", []):
                # Normalize tool call format (support both flattened and OpenAI format)
                tool_name = tc.get('name')
                if tool_name is None:
                    function = tc.get('function', {})
                    tool_name = function.get('name', 'Unknown')
                tool_arguments = tc.get('arguments')
                if tool_arguments is None:
                    function = tc.get('function', {})
                    tool_arguments = function.get('arguments', {})

                if detail_level == "minimal":
                    add_line(f"🛠️ {tool_name}", style="color: #0000FF;")
                else:
                    add_line(f"🛠️ {tool_name}", style="color: #0000FF; font-weight: bold;")
                    if detail_level == "verbose":
                        add_line(f"  Arguments: {tool_arguments}", style="color: #0000AA;")

                # Result
                result_text = tc.get('result', '')
                # Truncate if needed
                unescaped_result = html.unescape(result_text)
                if len(unescaped_result) > MAX_RESULT_LENGTH:
                    truncated = unescaped_result[:MAX_RESULT_LENGTH] + "..."
                    add_line(f"Result: {truncated}", style="color: #006400;", title=unescaped_result)
                else:
                    add_line(f"Result: {unescaped_result}", style="color: #006400;")

        elif etype == "final":
            add_line(f"Final answer: {event['content']}", style="font-weight: bold; color: #000080;", use_markdown=True)
            if detail_level != "minimal" and "reasoning" in event and event["reasoning"]:
                add_line(f"Reasoning: {event['reasoning']}", style="color: #666666;", use_markdown=True)

        elif etype == "user_query":
            add_line(f"User query: {event.get('content', '')}", style="font-weight: bold; color: #8B008B;", use_markdown=True)

        elif etype == "tool_result":
            tool_call_id = event.get('tool_call_id', 'unknown')
            add_line(f"Tool result (call id: {tool_call_id}): {event.get('content', '')}", style="color: #006400;", use_markdown=True)

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
            add_line(f"Agent requests interaction: {event.get('message', '')}", style="color: #008080;")
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
            if detail_level != "minimal" and "reasoning" in event and event["reasoning"]:
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
            add_line(f"Final answer: {event['content']}")
            if detail_level != "minimal" and "reasoning" in event and event["reasoning"]:
                add_line(f"Reasoning: {event['reasoning']}")

        elif etype == "user_query":
            add_line(f"User query: {event.get('content', '')}")

        elif etype == "system":
            add_line(f"System: {event.get('content', '')}")
            # Show full summary if present (from SummarizeTool)
            if 'summary' in event:
                add_line(f"Summary: {event['summary']}")

        elif etype == "stopped":
            add_line("Agent stopped by user.")
        elif etype == "user_interaction_requested":
            add_line(f"Agent requests interaction: {event.get('message', '')}")
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
