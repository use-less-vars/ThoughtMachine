"""Output Panel - Event display and output area for the agent."""
import html
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QComboBox, QTextEdit, QFrame, QScrollArea
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QTextCursor
from agent.logging import log
from .markdown_renderer import MarkdownRenderer
from .message_renderer import MessageRenderer, MessageType
from ..utils.constants import MAX_RESULT_LENGTH, MAX_TOOL_RESULTS_PER_TURN, MAX_LINES_PER_RESULT, ENABLE_RESULT_TRUNCATION, INTERNAL_EVENT_TYPES

class OutputPanel(QWidget):
    """Panel containing event display, filtering, and query controls."""
    SPECIAL_TOOLS = {'Final', 'FinalReport', 'RequestUserInteraction', 'ProgressReport'}
    COLOR_USER_QUERY = MessageRenderer.STYLES[MessageType.USER].border_color
    COLOR_SYSTEM_USER = MessageRenderer.STYLES[MessageType.USER_SYSTEM].border_color
    COLOR_REASONING_BG = MessageRenderer.STYLES[MessageType.REASONING].background_color
    COLOR_REASONING_BORDER = MessageRenderer.STYLES[MessageType.REASONING].border_color
    COLOR_FINAL = MessageRenderer.STYLES[MessageType.TOOL_CALL].border_color
    COLOR_REQUEST_USER = MessageRenderer.STYLES[MessageType.TOOL_CALL].border_color
    COLOR_REGULAR_TOOL = MessageRenderer.STYLES[MessageType.TOOL_RESULT].border_color
    COLOR_TOOL_CALL = MessageRenderer.STYLES[MessageType.TOOL_CALL].border_color
    COLOR_TOOL_RESULT = MessageRenderer.STYLES[MessageType.TOOL_RESULT].border_color
    COLOR_SYSTEM = MessageRenderer.STYLES[MessageType.SYSTEM].border_color
    COLOR_ASSISTANT = MessageRenderer.STYLES[MessageType.ASSISTANT].border_color

    def _is_system_message(self, content: str) -> bool:
        """Check if content appears to be a system notification (token warning, etc.)"""
        if not content:
            return False
        # Accept both formats: with or without asterisks
        return (content.startswith('[SYSTEM NOTIFICATION]') or 
                content.startswith('[**SYSTEM NOTIFICATION**]') or 
                content.startswith('[SYSTEM]'))

    def __init__(self, parent=None):
        super().__init__(parent)
        self._last_filter_text = ''
        self._last_filter_type = 'all'
        self.total_input = 0
        self.total_output = 0
        self.context_length = 0
        self._tool_call_map = {}
        self.init_ui()
        self.markdown_renderer = MarkdownRenderer()
        self.message_renderer = MessageRenderer(self.markdown_renderer)
        self.setup_signal_connections()

    def init_ui(self):
        """Initialize the output panel UI."""
        layout = QVBoxLayout()
        self.setLayout(layout)
        filter_widget = QWidget()
        filter_layout = QHBoxLayout()
        filter_widget.setLayout(filter_layout)
        filter_layout.addWidget(QLabel('Filter:'))
        self.filter_lineedit = QLineEdit()
        self.filter_lineedit.setPlaceholderText('Search events...')
        filter_layout.addWidget(self.filter_lineedit, 1)
        filter_layout.addWidget(QLabel('Type:'))
        self.filter_type_combo = QComboBox()
        self.filter_type_combo.addItems(['all', 'turn', 'final', 'user_query', 'processing', 'stopped', 'system', 'user_interaction_requested', 'token_warning', 'turn_warning', 'paused', 'max_turns', 'error', 'thread_finished'])
        filter_layout.addWidget(self.filter_type_combo)
        layout.addWidget(filter_widget)
        self.output_textedit = QTextEdit()
        self.output_textedit.setReadOnly(True)
        self.output_textedit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.output_textedit.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.output_textedit.setAcceptRichText(True)
        self.output_textedit.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.output_textedit.setStyleSheet('\n            QTextEdit:focus {\n                border: none;\n                outline: none;\n            }\n            QTextEdit {\n                selection-background-color: #3399ff;\n                selection-color: white;\n            }\n        ')
        layout.addWidget(self.output_textedit, 4)

    def setup_signal_connections(self):
        """Connect filter signals."""
        self.filter_lineedit.textChanged.connect(self._apply_filter)
        self.filter_type_combo.currentTextChanged.connect(self._apply_filter)

    def _apply_filter(self):
        """Apply current filter settings and rebuild output.
        TODO: Replace with new filtering logic in Phase 5."""
        filter_text = self.filter_lineedit.text()
        filter_type = self.filter_type_combo.currentText()
        if filter_text == self._last_filter_text and filter_type == self._last_filter_type:
            return
        self._last_filter_text = filter_text
        self._last_filter_type = filter_type
        log('DEBUG', 'debug.unknown', f"[OutputPanel] _apply_filter: text='{filter_text}', type='{filter_type}'")

    def display_event(self, event: dict) -> None:
        """Single entry point for all events from the presenter."""
        log('DEBUG', 'ui.output_panel', f'DEBUG display_event keys: {list(event.keys())}')
        log('DEBUG', 'ui.output_panel', f"DEBUG display_event type: {event.get('type')}")
        log('DEBUG', 'ui.output_panel', f'DEBUG display_event content sample: {str(event)[:200]}')
        if event.get('type') == 'system':
            log('DEBUG', 'ui.output_panel', f"DEBUG system event: content={event.get('content', '')[:100]}")
        if not self._should_display(event):
            return
        html = self._render_event(event)
        self._append_html(html)

    def _should_display(self, event) -> bool:
        """Decide whether this event should be shown in the output.
        Later this can be made configurable via user settings.
        For now, always return True."""
        log('DEBUG', 'ui.output_panel', f"DEBUG _should_display: type={event.get('type')}, role={event.get('role')}")
        return True

    def _render_event(self, event) -> str:
        """Convert any event to a self-contained HTML block."""
        event_type = event.get('type', 'unknown')
        content = event.get('content', '')
        tool_calls = event.get('tool_calls', [])
        reasoning_content = event.get('reasoning_content', '')
        tool_call_id = event.get('tool_call_id', '')
        tool_calls_html = ''
        if tool_calls:
            for tool_call in tool_calls:
                tool_id = tool_call.get('id', '')
                tool_func = tool_call.get('function', {})
                tool_name = tool_func.get('name', 'unknown')
                arguments = tool_func.get('arguments', '{}')
                if tool_id:
                    log('DEBUG', 'ui.output_panel', f'DEBUG _render_event assistant tool_call mapping: id={tool_id}, name={tool_name}')
                    self._tool_call_map[tool_id] = tool_name
                if tool_name in self.SPECIAL_TOOLS:
                    if tool_name == 'RequestUserInteraction':
                        border_color = self.COLOR_REQUEST_USER
                    else:
                        border_color = self.COLOR_FINAL
                    bg_color = MessageRenderer.STYLES[MessageType.TOOL_CALL].background_color
                else:
                    border_color = self.COLOR_TOOL_CALL
                    bg_color = MessageRenderer.STYLES[MessageType.TOOL_RESULT].background_color
                if tool_name in self.SPECIAL_TOOLS:
                    header = f"Tool: <span style='color: #001f3f;'>{tool_name}</span>"
                else:
                    header = f'Tool: {tool_name}'
                args_str = str(arguments)
                if len(args_str) > 200:
                    args_str = args_str[:200] + '...'
                escaped_args = html.escape(args_str)
                inner_bg = f'background-color: {bg_color};' if tool_name in self.SPECIAL_TOOLS else ''
                tool_block = f'<div style="border: 1px solid {border_color}; border-radius: 5px; margin-top: 8px; margin-bottom: 8px; overflow: hidden; width: 100%;"><div style="background-color: {bg_color} !important; padding: 8px 10px; font-weight: bold; border-bottom: 1px solid {border_color}; margin: 0 !important; display: inline-block !important; vertical-align: top; width: 100% !important; box-sizing: border-box; min-width: 100% !important; position: relative;">{header}</div><div style="padding: 10px; {inner_bg} width: 100%; box-sizing: border-box;">'
                if tool_name not in self.SPECIAL_TOOLS:
                    tool_block += f'<div style="color: #666666; font-size: 0.9em; font-family: monospace, monospace;">Arguments: {escaped_args}</div>'
                tool_block += '</div></div>'
                tool_calls_html += tool_block
        if event_type == 'tool_call':
            tool_name = event.get('function', {}).get('name', 'unknown')
            tool_call_id = event.get('id', '')
            if tool_call_id:
                log('DEBUG', 'ui.output_panel', f'DEBUG _render_event tool_call mapping: id={tool_call_id}, name={tool_name}')
                self._tool_call_map[tool_call_id] = tool_name
        if event_type == 'user_query':
            content = event.get('content', '')
            is_system_notification = self._is_system_message(content)
            if is_system_notification:
                border_color = self.COLOR_SYSTEM_USER
                bg_color = MessageRenderer.STYLES[MessageType.USER_SYSTEM].background_color
                header = 'System'
            else:
                border_color = self.COLOR_USER_QUERY
                bg_color = MessageRenderer.STYLES[MessageType.USER].background_color
                header = 'User'
        elif event_type == 'turn':
            border_color = self.COLOR_ASSISTANT
            bg_color = MessageRenderer.STYLES[MessageType.ASSISTANT].background_color
            header = 'Assistant'
        elif event_type == 'tool_call':
            tool_name = event.get('function', {}).get('name', 'unknown')
            if tool_name in self.SPECIAL_TOOLS:
                if tool_name == 'RequestUserInteraction':
                    border_color = self.COLOR_REQUEST_USER
                else:
                    border_color = self.COLOR_FINAL
                bg_color = MessageRenderer.STYLES[MessageType.TOOL_CALL].background_color
                header = f'Tool: {tool_name}'
            else:
                border_color = self.COLOR_TOOL_CALL
                bg_color = MessageRenderer.STYLES[MessageType.TOOL_RESULT].background_color
                header = f'Tool: {tool_name}'
        elif event_type == 'tool_result':
            log('DEBUG', 'ui.output_panel', f'DEBUG _render_event tool_result: tool_call_id={tool_call_id}, map keys={list(self._tool_call_map.keys())}')
            tool_name = self._tool_call_map.get(tool_call_id, '')
            log('DEBUG', 'ui.output_panel', f"DEBUG _render_event tool_result found: '{tool_name}' for id {tool_call_id}")
            if tool_name in self.SPECIAL_TOOLS:
                if tool_name == 'RequestUserInteraction':
                    border_color = self.COLOR_REQUEST_USER
                else:
                    border_color = self.COLOR_FINAL
                bg_color = MessageRenderer.STYLES[MessageType.TOOL_CALL].background_color
                header = f'Tool Result ({tool_name})'
            else:
                border_color = self.COLOR_TOOL_RESULT
                bg_color = MessageRenderer.STYLES[MessageType.TOOL_RESULT].background_color
                header = 'Tool Result'
        elif event_type in ('system', 'token_warning', 'turn_warning'):
            border_color = self.COLOR_SYSTEM
            bg_color = MessageRenderer.STYLES[MessageType.SYSTEM].background_color
            header = 'System'
        elif event_type == 'final':
            border_color = '#FFA500'
            bg_color = '#FFF5E6'  # Final event not in MessageRenderer, keep original
            header = 'Final'
        else:
            log('DEBUG', 'ui.output_panel', f'DEBUG _render_event unknown event_type: {event_type}, keys: {list(event.keys())}')
            border_color = '#cccccc'
            bg_color = MessageRenderer.STYLES[MessageType.REASONING].background_color
            header = event_type.replace('_', ' ').title()
        current_tool_name = ''
        if event_type == 'tool_result':
            current_tool_name = self._tool_call_map.get(tool_call_id, '')
        elif event_type == 'tool_call':
            current_tool_name = event.get('function', {}).get('name', 'unknown')
        rendered_content = self._render_event_content(event_type, content, current_tool_name, tool_call_id)
        reasoning_html = ''
        if reasoning_content:
            reasoning_html = f'<div style="background-color: {self.COLOR_REASONING_BG}; border-left: 4px solid {self.COLOR_REASONING_BORDER}; padding: 8px; margin-bottom: 12px;"><div style="color: #333; font-weight: bold;">Reasoning:</div>{self._render_content(reasoning_content)}</div>'
        content_background = ''
        if event_type == 'tool_result' and current_tool_name in self.SPECIAL_TOOLS:
            content_background = f'background-color: {bg_color};'
        elif event_type == 'tool_call' and current_tool_name in self.SPECIAL_TOOLS:
            content_background = f'background-color: {bg_color};'
        html_block = f'<div style="border: 1px solid {border_color}; border-radius: 5px; margin-bottom: 12px; overflow: hidden; width: 100%;"><div style="background-color: {bg_color} !important; padding: 8px 10px; font-weight: bold; border-bottom: 1px solid {border_color}; margin: 0 !important; display: inline-block !important; vertical-align: top; width: 100% !important; box-sizing: border-box; min-width: 100% !important; position: relative;">{header}</div><div style="padding: 10px; {content_background} width: 100%; box-sizing: border-box;">{reasoning_html}{tool_calls_html}{rendered_content}</div></div>'
        if event_type == 'tool_call':
            arguments = event.get('function', {}).get('arguments', '{}')
            args_str = str(arguments)
            if len(args_str) > 200:
                args_str = args_str[:200] + '...'
            escaped_args = html.escape(args_str)
            tool_name = event.get('function', {}).get('name', 'unknown')
            if tool_name not in self.SPECIAL_TOOLS:
                html_block += f'<div style="margin-left: 20px; margin-top: 5px; margin-bottom: 5px;"><div style="color: #666666; font-size: 0.9em; font-family: monospace, monospace;">Arguments: {escaped_args}</div></div>'
        return html_block

    def _render_event_content(self, event_type: str, content: str, tool_name: str='', tool_call_id: str='') -> str:
        """Render event content based on event type and tool."""
        if event_type == 'tool_result':
            if not tool_name and tool_call_id:
                tool_name = self._tool_call_map.get(tool_call_id, '')
            if tool_name in self.SPECIAL_TOOLS:
                rendered = self._render_content(content)
                if tool_name in ['Final', 'FinalReport']:
                    return f'<div style="color: #001f3f;">{rendered}</div>'
                else:
                    return rendered
            else:
                truncated = self._truncate_plain_text(content, tool_name)
                full_content_escaped = html.escape(content, quote=True)
                return f'<div style="font-family: monospace, monospace; white-space: pre-wrap;" data-full-content="{full_content_escaped}">{html.escape(truncated)}</div>'
        elif event_type == 'user_query':
            if self._is_system_message(content):
                color = self.COLOR_SYSTEM_USER
            else:
                color = self.COLOR_USER_QUERY
            return f'<div style="color: {color};">{self._render_content(content)}</div>'
        else:
            rendered = self._render_content(content)
            if tool_name in ['Final', 'FinalReport']:
                return f'<div style="color: {self.COLOR_TOOL_CALL};">{rendered}</div>'
            else:
                return rendered

    def _render_content(self, content: str) -> str:
        """Render message content to HTML (handles markdown)."""
        if not content:
            return ''
        return self.markdown_renderer.markdown_to_html(content)

    def _truncate_plain_text(self, content: str, tool_name: str='') -> str:
        """Truncate plain text content for regular tool results."""
        if not content:
            return ''
        if tool_name in self.SPECIAL_TOOLS:
            return content
        if not ENABLE_RESULT_TRUNCATION:
            return content
        lines = content.split('\n')
        if len(lines) > MAX_LINES_PER_RESULT:
            lines = lines[:MAX_LINES_PER_RESULT]
            lines.append('...')
        truncated_lines = []
        total_chars = 0
        for line in lines:
            if total_chars + len(line) > MAX_RESULT_LENGTH:
                truncated_lines.append('...')
                break
            if len(line) > 100:
                line = line[:100] + '...'
            truncated_lines.append(line)
            total_chars += len(line)
        return '\n'.join(truncated_lines)

    def _append_html(self, html: str) -> None:
        # Check if user is near bottom before inserting new content
        scrollbar = self.output_textedit.verticalScrollBar()
        was_near_bottom = scrollbar.value() >= scrollbar.maximum() - 5
        
        cursor = self.output_textedit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertBlock()
        cursor.insertHtml(html)
        cursor.insertBlock()
        
        # If user was near bottom before new content, scroll to new bottom
        if was_near_bottom:
            QTimer.singleShot(0, lambda: scrollbar.setValue(scrollbar.maximum()))

    def _auto_scroll_if_bottom(self) -> None:
        scrollbar = self.output_textedit.verticalScrollBar()
        if scrollbar.value() >= scrollbar.maximum() - 5:
            QTimer.singleShot(0, lambda: scrollbar.setValue(scrollbar.maximum()))

    def set_updates_enabled(self, enabled: bool) -> None:
        """Enable or disable widget updates for bulk operations."""
        self.output_textedit.setUpdatesEnabled(enabled)

    def load_session_history(self, history, suppress_scroll: bool=True) -> None:
        """Bulk load session history without jumping.

        Args:
            history: List of event dicts from session.user_history
            suppress_scroll: If True, disable auto-scroll during bulk load
        """
        from agent.logging import log
        log('DEBUG', 'ui.output_panel', f'DEBUG load_session_history: processing {len(history)} messages')
        
        # Save scroll state before clearing if suppressing scroll
        old_scroll_value = 0
        old_scroll_max = 0
        scroll_percentage = 0.0
        if suppress_scroll:
            scrollbar = self.output_textedit.verticalScrollBar()
            old_scroll_value = scrollbar.value()
            old_scroll_max = scrollbar.maximum()
            if old_scroll_max > 0:
                scroll_percentage = old_scroll_value / old_scroll_max
            
        for i, msg in enumerate(history):
            log('DEBUG', 'ui.output_panel', f"DEBUG load_session_history message {i}: role={msg.get('role')}, keys={list(msg.keys())}")
            if msg.get('role') == 'assistant':
                tool_calls = msg.get('tool_calls', [])
                log('DEBUG', 'ui.output_panel', f'DEBUG load_session_history assistant tool_calls count: {len(tool_calls)}')
                for tc in tool_calls:
                    tool_id = tc.get('id', '')
                    tool_name = tc.get('function', {}).get('name', 'unknown')
                    log('DEBUG', 'ui.output_panel', f'DEBUG load_session_history tool_call mapping: id={tool_id}, name={tool_name}')
            elif msg.get('role') == 'tool' and 'tool_call_id' in msg:
                tool_call_id = msg.get('tool_call_id', '')
                log('DEBUG', 'ui.output_panel', f'DEBUG load_session_history tool_result: tool_call_id={tool_call_id}')
        if suppress_scroll:
            self.set_updates_enabled(False)
        self.clear_output()
        log('DEBUG', 'ui.output_panel', f'DEBUG load_session_history first pass: building tool_call_map')
        for msg in history:
            role = msg.get('role')
            log('DEBUG', 'ui.output_panel', f'DEBUG load_session_history first pass message role: {role}, keys: {list(msg.keys())}')
            if role == 'assistant':
                tool_calls = msg.get('tool_calls', [])
                log('DEBUG', 'ui.output_panel', f'DEBUG load_session_history assistant tool_calls count: {len(tool_calls)}')
                for tc in tool_calls:
                    tool_id = tc.get('id', '')
                    tool_name = tc.get('function', {}).get('name', 'unknown')
                    log('DEBUG', 'ui.output_panel', f'DEBUG load_session_history tool_call mapping: id={tool_id}, name={tool_name}')
                    if tool_id:
                        log('DEBUG', 'ui.output_panel', f'DEBUG load_session_history mapping: id={tool_id}, name={tool_name}')
                        self._tool_call_map[tool_id] = tool_name
            elif role == 'tool':
                if 'tool_call_id' in msg:
                    tool_call_id = msg.get('tool_call_id', '')
                    log('DEBUG', 'ui.output_panel', f'DEBUG load_session_history tool_result in first pass: tool_call_id={tool_call_id}')
                else:
                    tool_id = msg.get('id', '')
                    tool_name = msg.get('function', {}).get('name', 'unknown')
                    log('DEBUG', 'ui.output_panel', f'DEBUG load_session_history standalone tool_call: id={tool_id}, name={tool_name}')
                    if tool_id:
                        log('DEBUG', 'ui.output_panel', f'DEBUG load_session_history standalone tool_call mapping: id={tool_id}, name={tool_name}')
                        self._tool_call_map[tool_id] = tool_name
        log('DEBUG', 'ui.output_panel', f'DEBUG load_session_history second pass: displaying {len(history)} messages')
        for message in history:
            self.display_message(message)
        if suppress_scroll:
            self.set_updates_enabled(True)
            # Restore scroll position
            scrollbar = self.output_textedit.verticalScrollBar()
            new_max = scrollbar.maximum()
            if scroll_percentage > 0.95:  # Was near bottom
                QTimer.singleShot(0, lambda: scrollbar.setValue(new_max))  # Scroll to new bottom
            elif scroll_percentage > 0 and new_max > 0:
                # Restore relative position
                new_value = int(scroll_percentage * new_max)
                QTimer.singleShot(0, lambda: scrollbar.setValue(new_value))

    def show_processing_indicator(self, query, turn_number):
        """Show a temporary 'Processing...' indicator for a user query."""
        from datetime import datetime
        event = {'type': 'processing', 'content': f"⏳ Processing your query: {query[:50]}{('...' if len(query) > 50 else '')}", 'turn': turn_number, 'timestamp': datetime.now().isoformat(), 'created_at': datetime.now().isoformat(), '_detail_level': 'normal', '_is_processing_indicator': True}
        self.display_event(event)

    def remove_processing_indicator(self, turn_number):
        """Remove the processing indicator for a given turn.
        
        Note: This is a stub for compatibility; processing indicators
        are temporary and will be replaced by actual events.
        """
        pass

    def _normalize_turn(self, turn_val):
        """Convert turn value to integer for consistent comparison."""
        if turn_val is None:
            return 0
        if isinstance(turn_val, (int, float)):
            return int(turn_val)
        if isinstance(turn_val, str):
            try:
                return int(turn_val)
            except (ValueError, TypeError):
                try:
                    return int(float(turn_val))
                except (ValueError, TypeError):
                    return 0
        return 0

    def clear_output(self):
        """Clear the output text edit."""
        self.output_textedit.clear()
        self._tool_call_map.clear()

    def _role_to_event_type(self, message):
        """Convert a message with 'role' to appropriate event type."""
        log('DEBUG', 'ui.output_panel', f'DEBUG _role_to_event_type keys: {list(message.keys())}')
        role = message.get('role')
        log('DEBUG', 'ui.output_panel', f'DEBUG _role_to_event_type role: {role}')
        if role == 'user':
            return 'user_query'
        elif role == 'assistant':
            return 'turn'
        elif role == 'tool':
            if 'tool_call_id' in message:
                return 'tool_result'
            else:
                return 'tool_call'
        elif role == 'system':
            return 'system'
        else:
            log('DEBUG', 'ui.output_panel', f'DEBUG _role_to_event_type unknown role: {role}, message keys: {list(message.keys())}')
            return 'unknown'

    def display_message(self, message):
        """Display a message from user_history.
        
        Args:
            message: A message dict from session.user_history.
                    Should have 'role' and 'content' keys.
        """
        log('DEBUG', 'ui.output_panel', f'DEBUG display_message keys: {list(message.keys())}')
        log('DEBUG', 'ui.output_panel', f"DEBUG display_message role: {message.get('role')}")
        event = message.copy()
        if 'type' not in event:
            event['type'] = self._role_to_event_type(message)
            log('DEBUG', 'ui.output_panel', f"DEBUG display_message mapped type: {event['type']}")
        if 'content' not in event:
            event['content'] = ''
        self.display_event(event)
        # Phase 2 logging: GUI display
        log('DEBUG', 'ui.output_panel', f'GUI displayed message: role={message.get("role")}, content_preview={str(message.get("content", ""))[:100]}')
        content_hash = hash(message.get("content", ""))
        log('DEBUG', 'ui.output_panel', f'GUI displayed message hash: {content_hash}')

    @property
    def smart_scroller(self):
        """Dummy smart scroller for compatibility during transition."""

        class DummySmartScroller:

            def pause_tracking(self):
                pass

            def resume_tracking(self):
                pass

            def scroll_to_bottom(self):
                pass
        return DummySmartScroller()