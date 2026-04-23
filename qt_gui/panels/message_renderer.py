"""
Message Renderer - Centralized service for rendering messages in the GUI.

This module provides a unified service for rendering all message types
(user, assistant, system, tool calls, tool results) with consistent styling
and layout. It centralizes CSS definitions to eliminate duplication between
output_panel.py and event_models.py.
"""

import html
import re
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass
from enum import Enum
from ..utils.constants import ENABLE_RESULT_TRUNCATION, MAX_LINES_PER_RESULT, MAX_CHARS_PER_LINE, MAX_RESULT_LENGTH
from agent.logging import log



class MessageType(Enum):
    """Message type enumeration."""
    USER = "user"
    USER_SYSTEM = "user_system"  # User messages that are system notifications
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    REASONING = "reasoning"
    SPECIAL = "special"
    ERROR = "error"
    WARNING = "warning"
    SUMMARY = "summary"


@dataclass
class MessageStyle:
    """CSS style definitions for a message type."""
    border_color: str
    background_color: str
    text_color: str = "#000000"
    header_text_color: str = "#000000"
    font_family: str = "inherit"
    special_tool: bool = False


@dataclass
class Recipe:
    """Unified rendering recipe for message types.

    Used to parameterize the _render_card method with consistent styling.
    """
    title: str
    style_key: MessageType
    indent_level: int = 0
    show_arguments: bool = False
    render_content_func: Optional[Callable] = None
    truncate_content: bool = True
    max_chars: int = 2000

class MessageRenderer:
    """
    Centralized message rendering service.
    
    Provides methods to render all message types with consistent styling.
    """
    
    # Special tools that get blue styling
    SPECIAL_TOOLS = {"Final", "FinalReport", "RequestUserInteraction", "ProgressReport"}

    # Tool configuration overrides
    TOOL_OVERRIDES = {
        "Final": {"show_arguments": False, "truncate_content": False, "style_key": MessageType.SPECIAL},
        "FinalReport": {"show_arguments": False, "truncate_content": False, "style_key": MessageType.SPECIAL},
        "RequestUserInteraction": {"show_arguments": False, "truncate_content": False, "style_key": MessageType.SPECIAL},
        "ProgressReport": {"show_arguments": False, "truncate_content": False, "style_key": MessageType.SPECIAL},
    }

    # CSS style definitions
    STYLES = {
        MessageType.USER: MessageStyle(
            border_color="#ffffff",
            background_color="#F3E8FF",
            text_color="#4B0082",
            header_text_color="#4B0082",
        ),
        MessageType.USER_SYSTEM: MessageStyle(
            border_color="#ffffff",
            background_color="#ffe6e6",
            header_text_color="#000000"
        ),
        MessageType.ASSISTANT: MessageStyle(
            border_color="#ffffff",
            background_color="#e6f3ff",
            header_text_color="#000000"
        ),
        MessageType.SYSTEM: MessageStyle(
            border_color="#ffffff",
            background_color="#ffe6e6",
            header_text_color="#000000"
        ),
        MessageType.TOOL_CALL: MessageStyle(
            border_color="#ffffff",
            background_color="#eef9ee",
            text_color="#000000",
            special_tool=False
        ),
        MessageType.TOOL_RESULT: MessageStyle(
            border_color="#ffffff",
            background_color="#eef9ee",
            text_color="#000000",
            special_tool=False
        ),
        MessageType.REASONING: MessageStyle(
            border_color="#ffffff",
            background_color="#f8f8f8",
            text_color="#333333"
        ),
        MessageType.SPECIAL: MessageStyle(
            border_color="#ffffff",
            background_color="#c9daf8",
            text_color="#0000FF",
            special_tool=True
        ),
        MessageType.ERROR: MessageStyle(
            border_color="#ffffff",
            background_color="#ffe6e6",
            text_color="#FF0000",
            special_tool=False
        ),
        MessageType.WARNING: MessageStyle(
            border_color="#ffffff",
            background_color="#fff3e6",
            text_color="#FFA500",
            special_tool=False
        ),
        MessageType.SUMMARY: MessageStyle(
            border_color="#ffffff",
            background_color="#f8f8f8",
            text_color="#666666",
            special_tool=False
        )
    }
    
    def __init__(self, markdown_renderer=None):
        """
        Initialize the message renderer.
        
        Args:
            markdown_renderer: Optional markdown renderer for converting
                markdown content to HTML.
        """
        self.markdown_renderer = markdown_renderer
    
    def render_user_message(self, content: str, created_at: str = "", is_system_notification: bool = False) -> str:
        """
        Render a user message.

        Args:
            content: Message content
            created_at: Timestamp (optional)
            is_system_notification: Whether this is a system notification

        Returns:
            HTML string
        """
        # Determine style and header
        if is_system_notification:
            style_key = MessageType.SYSTEM
            header = "System Notification"
        else:
            style_key = MessageType.USER
            header = "User"
        
        # Use unified card layout
        return self._render_card(
            title=header,
            style_key=style_key,
            indent_level=0,
            content_html=self._render_content(content),
            extra_html=""
        )
    def render_assistant_message(self, content: str = "", tool_calls: List[Dict] = None,
                                reasoning_content: str = "", created_at: str = "") -> str:
        """
        Render an assistant message with optional tool calls and reasoning.

        Args:
            content: Main content
            tool_calls: List of tool call dictionaries
            reasoning_content: Reasoning content (if any)
            created_at: Timestamp (optional)

        Returns:
            HTML string
        """
        tool_calls = tool_calls or []
        html_parts = []

        # Display reasoning content if present (standalone sibling card)
        if reasoning_content:
            reasoning_html = self._render_card(
                title="Reasoning:",
                style_key=MessageType.REASONING,
                indent_level=0,
                content_html=self._render_content(reasoning_content),
            )
            html_parts.append(reasoning_html)

        # Build content HTML for the main assistant card
        content_html_parts = []
        if content:
            content_html_parts.append(self._render_content(content))
        content_html = ''.join(content_html_parts)
        
        # Main assistant card (title + content, unified blue background)
        html_parts.append(self._render_card(
            title="Assistant",
            style_key=MessageType.ASSISTANT,
            indent_level=0,
            content_html=content_html,
            extra_html=""
        ))

        # Display tool calls if present
        if tool_calls:
            # Add visual separator before tool calls
            html_parts.append('<div style="height: 12px; border-top: 1px solid #ddd; margin: 8px 0;"></div>')

            for tool_call in tool_calls:
                html_parts.append(self.render_tool_call(tool_call))

        return ''.join(html_parts)
    def render_tool_call(self, tool_call: Dict) -> str:
        """
        Render a tool call.

        Args:
            tool_call: Tool call dictionary

        Returns:
            HTML string
        """
        tool_name = tool_call.get('function', {}).get('name', 'unknown')
        arguments = tool_call.get('function', {}).get('arguments', '{}')
        tool_call_id = tool_call.get('id', '')
        
        is_special = tool_name in self.SPECIAL_TOOLS
        
        # Prepare arguments display
        args_str = str(arguments)
        if len(args_str) > 200 and not is_special:
            args_str = args_str[:200] + '...'
        escaped_args = html.escape(args_str)
        extra_html = f"Arguments: {escaped_args}"
        
        # Use unified card layout
        if is_special:
            # Special tool: blue styling (SPECIAL type)
            return self._render_card(
                title=f"Tool: {tool_name}",
                style_key=MessageType.SPECIAL,
                indent_level=1,
                content_html="",
                extra_html=extra_html
            )
        else:
            # Regular tool: green styling (TOOL_CALL type)
            return self._render_card(
                title=f"Tool: {tool_name}",
                style_key=MessageType.TOOL_CALL,
                indent_level=1,
                content_html="",
                extra_html=extra_html
            )    
    def render_tool_result(self, content: str, tool_call_id: str = "", tool_name: str = "", 
                          success: bool = True, error: str = "", enable_truncation: bool = True) -> str:
        """
        Render a tool result.
        
        Args:
            content: Result content
            tool_call_id: Tool call ID (optional)
            tool_name: Tool name (optional)
            success: Whether the tool succeeded
            error: Error message (if any)
            enable_truncation: Whether to truncate long results
            
        Returns:
            HTML string
        """
        is_special = tool_name in self.SPECIAL_TOOLS

        log("DEBUG", "ui.message_renderer", "render_tool_result called", {"tool_name": tool_name, "is_special": is_special, "content_length": len(content)})
        
        if is_special:
            log("DEBUG", "ui.message_renderer", "render_tool_result special branch")
            # Special tool: blue styling, full markdown rendering
            title = "Tool Result"
            if tool_name:
                title = f"Tool Result ({tool_name})"
            
            # Use unified card layout with SPECIAL type
            return self._render_card(
                title=title,
                style_key=MessageType.SPECIAL,
                indent_level=1,
                content_html=self._render_content(content),
                extra_html=""
            )
        else:
            # Regular tool: truncate plain text, HTML escape, monospace font
            style = self.STYLES[MessageType.TOOL_RESULT]
            if enable_truncation:
                truncated_content = self._truncate_plain_text(content, tool_name)
            else:
                truncated_content = content
            
            escaped_content = html.escape(truncated_content)
            
            if error:
                log("DEBUG", "ui.message_renderer", "render_tool_result error", {"error": error})
                # Error styling
                return self._render_card(
                    title="Error:",
                    style_key=MessageType.ERROR,
                    indent_level=1,
                    content_html=html.escape(error),
                    extra_html=""
                )
            elif not success:
                log("DEBUG", "ui.message_renderer", "render_tool_result warning", {"success": success})
                # Warning styling
                return self._render_card(
                    title="Warning:",
                    style_key=MessageType.WARNING,
                    indent_level=1,
                    content_html=escaped_content,
                    extra_html=""
                )
            else:
                # Success styling - use unified card layout
                title = "Tool Result"
                if tool_name:
                    title = f"Tool Result ({tool_name})"
                
                result_html = self._render_card(
                    title=title,
                    style_key=MessageType.TOOL_RESULT,
                    indent_level=1,
                    content_html=escaped_content,
                    extra_html=""
                )
                log("DEBUG", "ui.message_renderer", "render_tool_result success HTML", {"html_length": len(result_html)})
                return result_html
    
    def render_system_message(self, content: str, created_at: str = "", summary: bool = False) -> str:
        """
        Render a system message.

        Args:
            content: Message content
            created_at: Timestamp (optional)
            summary: Whether this is a summary message (uses SUMMARY style)

        Returns:
            HTML string
        """
        # Check if content already has [SYSTEM] prefix
        if content.startswith('[SYSTEM]'):
            content = content[8:].lstrip()

        # Determine style based on summary flag
        if summary:
            style_key = MessageType.SUMMARY
            header = "Summary"
        else:
            style_key = MessageType.SYSTEM
            header = "System"

        # Use unified card layout
        return self._render_card(
            title=header,
            style_key=style_key,
            indent_level=0,
            content_html=self._render_content(content),
            extra_html=""
        )
    
    def render_event_title(self, event_type: str) -> str:
        """
        Render an event title bar (for event_models.py).
        
        Args:
            event_type: Event type string
            
        Returns:
            HTML string
        """
        skip_title_events = ["user_query", "processing"]
        if event_type in skip_title_events:
            return ""
        
        return f'<div style="font-weight: bold; background-color: #e0e0e0; padding: 3px; display: block; clear: both;">{html.escape(event_type.upper())}</div>'

    def render_event(self, event: Dict) -> str:
        """
        Render any event type as a self-contained HTML block.
        
        This is a unified method to handle all event types, eliminating duplicate
        styling code in OutputPanel.
        
        Args:
            event: Event dictionary with at least a 'type' key
            
        Returns:
            HTML string
        """
        event_type = event.get('type', 'unknown')
        log("DEBUG", "ui.message_renderer", "render_event", {"event_type": event_type})
        
        content = event.get('content', '')
        tool_calls = event.get('tool_calls', [])
        reasoning_content = event.get('reasoning_content', '')
        tool_call_id = event.get('tool_call_id', '')
        
        # Handle tool-related events using existing dedicated methods
        if event_type == 'tool_call':
            tool_name = event.get('function', {}).get('name', 'unknown')
            arguments = event.get('function', {}).get('arguments', '{}')
            call_id = event.get('id', '')
            created_at = event.get('created_at', '')
            return self.render_standalone_tool_call(tool_name, arguments, call_id, created_at)
        
        if event_type == 'tool_result':
            tool_name = event.get('tool_name', '')
            success = event.get('success', True)
            error = event.get('error', '')
            created_at = event.get('created_at', '')
            return self.render_standalone_tool_result(
                content, tool_name, tool_call_id, success, error,
                ENABLE_RESULT_TRUNCATION, created_at
            )
        
        # Get recipe for this event
        recipe = self._get_recipe(event)
        
        # Handle turn events (assistant messages with tool calls and reasoning)
        if event_type == 'turn':
            return self.render_assistant_message(content, tool_calls, reasoning_content, '')
        
        # For simple events, use _render_card directly with the recipe
        content_html = self._render_content(content)

        result_html = self._render_card(
            title=recipe.title,
            style_key=recipe.style_key,
            indent_level=recipe.indent_level,
            content_html=content_html,
            extra_html=""
        )
        log('DEBUG', 'ui.message_renderer', 'render_event output', {
            'event_type': event_type,
            'recipe_style': recipe.style_key.name,
            'html_length': len(result_html),
            'html_preview': result_html[:300]
        })
        return result_html
    def _is_system_message(self, content: str) -> bool:
        """Check if content appears to be a system notification (token warning, etc.)"""
        if not content:
            return False
        # Accept both formats: with or without asterisks
        return (content.startswith('[SYSTEM NOTIFICATION]') or
                content.startswith('[**SYSTEM NOTIFICATION**]') or
                content.startswith('[SYSTEM]'))

    def _render_content(self, content: str) -> str:
        """
        Render message content to HTML (handles markdown).
        
        Args:
            content: Content string
            
        Returns:
            HTML string
        """
        if not content:
            return ''
        
        if self.markdown_renderer:
            return self.markdown_renderer.markdown_to_html(content)
        else:
            # Fallback: simple HTML escaping
            return html.escape(content).replace('\n', '<br>')
    
    def _get_recipe(self, event: Dict) -> Recipe:
        """
        Get rendering recipe for an event.
        
        Args:
            event: Event dictionary
            
        Returns:
            Recipe object with rendering parameters
        """
        event_type = event.get('type', 'unknown')
        content = event.get('content', '')
        tool_name = event.get('tool_name', '')
        
        # Handle system notifications in user messages
        if event_type == 'user_query':
            is_system_notification = self._is_system_message(content) or event.get('is_system_notification', False)
            if is_system_notification:
                return Recipe(
                    title="System Notification",
                    style_key=MessageType.SYSTEM,
                    indent_level=0,
                    show_arguments=False,
                    truncate_content=False,
                    max_chars=2000
                )
            else:
                return Recipe(
                    title="User",
                    style_key=MessageType.USER,
                    indent_level=0,
                    show_arguments=False,
                    truncate_content=False,
                    max_chars=2000
                )
        
        # Handle summaries
        if event.get('summary', False):
            return Recipe(
                title="Summary",
                style_key=MessageType.SUMMARY,
                indent_level=0,
                show_arguments=False,
                truncate_content=False,
                max_chars=2000
            )
        
        # Handle tool-related events (these use dedicated methods)
        if event_type in ('tool_call', 'tool_result'):
            # These events use existing dedicated methods
            # Return a generic recipe that will be handled specially
            return Recipe(
                title=event_type.replace('_', ' ').title(),
                style_key=MessageType.REASONING,  # Default, will be overridden by tool-specific logic
                indent_level=1 if event_type == 'tool_call' else 0,
                show_arguments=event_type == 'tool_call',
                truncate_content=True,
                max_chars=2000
            )
        
        # Map event types to recipes
        recipe_map = {
            'turn': Recipe(
                title="Assistant",
                style_key=MessageType.ASSISTANT,
                indent_level=0,
                show_arguments=False,
                truncate_content=False,
                max_chars=2000
            ),
            'system': Recipe(
                title="System",
                style_key=MessageType.SYSTEM,
                indent_level=0,
                show_arguments=False,
                truncate_content=False,
                max_chars=2000
            ),
            'token_warning': Recipe(
                title="System",
                style_key=MessageType.SYSTEM,
                indent_level=0,
                show_arguments=False,
                truncate_content=False,
                max_chars=2000
            ),
            'turn_warning': Recipe(
                title="System",
                style_key=MessageType.SYSTEM,
                indent_level=0,
                show_arguments=False,
                truncate_content=False,
                max_chars=2000
            ),
            'final': Recipe(
                title="Final",
                style_key=MessageType.SPECIAL,
                indent_level=0,
                show_arguments=False,
                truncate_content=False,
                max_chars=2000
            ),
        }
        
        # Return recipe from map or generic fallback
        if event_type in recipe_map:
            return recipe_map[event_type]
        else:
            # Generic fallback
            return Recipe(
                title=event_type.replace('_', ' ').title(),
                style_key=MessageType.REASONING,
                indent_level=0,
                show_arguments=False,
                truncate_content=False,
                max_chars=2000
            )
    def _truncate_plain_text(self, content: str, tool_name: str = "") -> str:
        """
        Truncate plain text content for regular tool results.
        
        Args:
            content: Content to truncate
            tool_name: Tool name (for special tool detection)
            
        Returns:
            Truncated content string
        """
        if not content:
            return ''
        
        # Don't truncate special tools
        if tool_name in self.SPECIAL_TOOLS:
            return content
        
        # Check if truncation is enabled
        if not ENABLE_RESULT_TRUNCATION:
            return content
        
        lines = content.split('\n')
        if len(lines) > MAX_LINES_PER_RESULT:
            lines = lines[:MAX_LINES_PER_RESULT]
            lines.append('...')

        # Character-based fallback: if only one line remains but it's very long, truncate it
        if len(lines) == 1 and len(lines[0]) > MAX_CHARS_PER_LINE:
            lines[0] = lines[0][:MAX_CHARS_PER_LINE] + '…'

        return '\n'.join(lines)
    
    def _render_card(self, title: str, style_key: MessageType, indent_level: int = 1, 
                    content_html: str = "", extra_html: str = "") -> str:
        """
        Render a unified card layout for messages.
        
        Args:
            title: Card title (e.g., "Tool: name", "Result:")
            style_key: MessageType for styling
            indent_level: Indentation level (0 = no indent, 1 = 20px, etc.)
            content_html: Main content HTML
            extra_html: Optional extra HTML (e.g., arguments display)
            
        Returns:
            HTML string for the card
        """
        style = self.STYLES[style_key]
        indent_px = indent_level * 20
        
        # Build the card HTML
        card_html = f'''
<div style="margin-left: {indent_px}px !important; margin-top: 5px; margin-bottom: 10px; border-left: 4px solid {style.border_color}; background-color: {style.background_color}; padding: 8px; border-radius: 4px; display: block; clear: both; list-style: none;">
'''
        if title:
            card_html += f'    <div style="font-weight: bold; color: {style.text_color};">{html.escape(title)}</div>\n'
        
        if extra_html:
            card_html += f'    <div style="color: #666666; font-size: 0.9em; font-family: monospace, monospace;">{extra_html}</div>\n'
        
        if content_html:
            # Add line break between title/extra and content if we have content
            if extra_html:
                card_html += '    <br>\n'
            card_html += f'    <div style="color: {style.text_color}; font-family: monospace; white-space: pre-wrap; padding-left: 10px !important;">{content_html}</div>\n'
        
        card_html += '</div>'
        return card_html
    
    def format_result_plain(self, content: str, tool_name: str = "", 
                            enable_truncation: bool = True, 
                            max_length: int = MAX_RESULT_LENGTH) -> str:
        """
        Format tool result content for plain text display.
        
        Args:
            content: Result content
            tool_name: Tool name (for special tool detection)
            enable_truncation: Whether to truncate long results
            max_length: Maximum character length before truncation
            
        Returns:
            Formatted plain text result (without any prefix like "Result:")
        """
        if not content:
            return ''
        
        # Don't truncate special tools
        if tool_name in self.SPECIAL_TOOLS:
            return content
        
        if not enable_truncation:
            return content
        
        # Apply character-based truncation
        if len(content) > max_length:
            return content[:max_length] + '...'
        
        return content

    def render_standalone_tool_call(self, tool_name: str, arguments: str, tool_call_id: str = "", created_at: str = "") -> str:
        """
        Render a standalone tool call as a card layout.

        Args:
            tool_name: Name of the tool
            arguments: JSON string of arguments
            tool_call_id: Tool call ID (optional)
            created_at: Timestamp (optional)

        Returns:
            HTML string with card layout
        """
        is_special = tool_name in self.SPECIAL_TOOLS
        
        # Prepare arguments display
        args_str = str(arguments)
        if len(args_str) > 200 and not is_special:
            args_str = args_str[:200] + '...'
        escaped_args = html.escape(args_str)
        extra_html = f"Arguments: {escaped_args}"
        
        # Use unified card layout
        if is_special:
            # Special tool: blue styling (SPECIAL type)
            return self._render_card(
                title=f"Tool: {tool_name}",
                style_key=MessageType.SPECIAL,
                indent_level=0,  # Standalone tool calls have no indentation
                content_html="",
                extra_html=extra_html
            )
        else:
            # Regular tool: green styling (TOOL_CALL type)
            return self._render_card(
                title=f"Tool: {tool_name}",
                style_key=MessageType.TOOL_CALL,
                indent_level=0,  # Standalone tool calls have no indentation
                content_html="",
                extra_html=extra_html
            )
    def render_standalone_tool_result(self, content: str, tool_name: str = "", tool_call_id: str = "", 
                                     success: bool = True, error: str = "", enable_truncation: bool = True,
                                     created_at: str = "") -> str:
        """
        Render a standalone tool result as a card layout.

        Args:
            content: Result content
            tool_name: Tool name (optional)
            tool_call_id: Tool call ID (optional)
            success: Whether the tool succeeded
            error: Error message (if any)
            enable_truncation: Whether to truncate long results
            created_at: Timestamp (optional)

        Returns:
            HTML string with card layout
        """
        is_special = tool_name in self.SPECIAL_TOOLS
        
        # Determine title
        title = "Tool Result"
        if tool_name:
            title = f"Tool Result ({tool_name})"
        
        # Handle error/warning cases
        if error:
            # Error styling - use red styling
            return self._render_card(
                title="Error:",
                style_key=MessageType.ERROR,
                indent_level=0,
                content_html=html.escape(error),
                extra_html=""
            )
        elif not success:
            # Warning styling - use orange styling
            return self._render_card(
                title="Warning:",
                style_key=MessageType.WARNING,
                indent_level=0,
                content_html=html.escape(content),
                extra_html=""
            )
        else:
            # Success case
            if is_special:
                # Special tool: blue styling, full markdown rendering
                return self._render_card(
                    title=title,
                    style_key=MessageType.SPECIAL,
                    indent_level=0,
                    content_html=self._render_content(content),
                    extra_html=""
                )
            else:
                # Regular tool: truncate plain text, HTML escape
                if enable_truncation:
                    truncated_content = self._truncate_plain_text(content, tool_name)
                else:
                    truncated_content = content
                escaped_content = html.escape(truncated_content)
                
                return self._render_card(
                    title=title,
                    style_key=MessageType.TOOL_RESULT,
                    indent_level=1,
                    content_html=escaped_content,
                    extra_html=""
                )
    # Helper methods for event_models.py to centralize CSS
    def get_tool_call_container_style(self, is_special: bool = False) -> str:
        """Get CSS style for tool call container."""
        if is_special:
            return "border-left: 4px solid #3498db; background-color: #eef4ff; padding: 8px; margin: 8px 0; border-radius: 4px; display: block; clear: both;"
        else:
            return "padding-left: 20px !important; margin-top: 5px; margin-bottom: 5px; display: block; clear: both;"
    
    def get_tool_call_header_style(self, is_special: bool = False) -> str:
        """Get CSS style for tool call header."""
        if is_special:
            return "color: #0000FF; font-weight: bold; display: block; clear: both;"
        else:
            return "color: #0000FF; font-weight: bold; display: block; clear: both;"
    
    def get_tool_result_container_style(self, is_special: bool = False) -> str:
        """Get CSS style for tool result container."""
        if is_special:
            return "border-left: 4px solid #3498db; background-color: #eef4ff; padding: 8px; margin: 8px 0; border-radius: 4px; display: block; clear: both;"
        else:
            return "padding-left: 20px !important; margin-top: 5px; margin-bottom: 10px; display: block; clear: both;"
    
    def get_tool_result_header_style(self, is_special: bool = False) -> str:
        """Get CSS style for tool result header."""
        if is_special:
            return "color: #0000FF; font-weight: bold; display: block; clear: both;"
        else:
            return "color: #0000FF; font-weight: bold; display: block; clear: both;"
    
    def get_event_title_style(self) -> str:
        """Get CSS style for event title bar."""
        return "font-weight: bold; background-color: #e0e0e0; padding: 3px; display: block; clear: both;"
    
    def get_reasoning_container_style(self) -> str:
        """Get CSS style for reasoning container."""
        return "background-color: #f8f8f8; border-left: 4px solid #888; padding: 8px; margin-bottom: 12px;"
    
    def get_reasoning_header_style(self) -> str:
        """Get CSS style for reasoning header."""
        return "color: #333; font-weight: bold;"