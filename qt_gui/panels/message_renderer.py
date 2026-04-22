"""
Message Renderer - Centralized service for rendering messages in the GUI.

This module provides a unified service for rendering all message types
(user, assistant, system, tool calls, tool results) with consistent styling
and layout. It centralizes CSS definitions to eliminate duplication between
output_panel.py and event_models.py.
"""

import html
import re
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from enum import Enum
from ..utils.constants import ENABLE_RESULT_TRUNCATION, MAX_LINES_PER_RESULT, MAX_CHARS_PER_LINE, MAX_RESULT_LENGTH


class MessageType(Enum):
    """Message type enumeration."""
    USER = "user"
    USER_SYSTEM = "user_system"  # User messages that are system notifications
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    REASONING = "reasoning"


@dataclass
class MessageStyle:
    """CSS style definitions for a message type."""
    border_color: str
    background_color: str
    text_color: str = "#000000"
    header_text_color: str = "#000000"
    font_family: str = "inherit"
    special_tool: bool = False


class MessageRenderer:
    """
    Centralized message rendering service.
    
    Provides methods to render all message types with consistent styling.
    """
    
    # Special tools that get blue styling
    SPECIAL_TOOLS = {"Final", "FinalReport", "RequestUserInteraction", "ProgressReport"}
    
    # CSS style definitions
    STYLES = {
        MessageType.USER: MessageStyle(
            border_color="#440424",
            background_color="#F0AFC5",
            header_text_color="#000000"
        ),
        MessageType.USER_SYSTEM: MessageStyle(
            border_color="#ff9999",
            background_color="#ffe6e6",
            header_text_color="#000000"
        ),
        MessageType.ASSISTANT: MessageStyle(
            border_color="#99ccff",
            background_color="#e6f3ff",
            header_text_color="#000000"
        ),
        MessageType.SYSTEM: MessageStyle(
            border_color="#ff9999",
            background_color="#ffe6e6",
            header_text_color="#000000"
        ),
        MessageType.TOOL_CALL: MessageStyle(
            border_color="#3498db",
            background_color="#eef4ff",
            text_color="#0000FF",
            special_tool=False
        ),
        MessageType.TOOL_RESULT: MessageStyle(
            border_color="#006400",
            background_color="#f0fff0",
            text_color="#006400",
            special_tool=False
        ),
        MessageType.REASONING: MessageStyle(
            border_color="#888888",
            background_color="#f8f8f8",
            text_color="#333333"
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
        msg_type = MessageType.USER_SYSTEM if is_system_notification else MessageType.USER
        style = self.STYLES[msg_type]
        header = "System" if is_system_notification else "User"
        
        rendered_content = self._render_content(content)
        
        html = (f'<div style="border: 1px solid {style.border_color}; border-radius: 5px; margin-bottom: 12px; overflow: hidden;">'
                f'<div style="background-color: {style.background_color}; padding: 8px 10px; font-weight: bold; border-bottom: 1px solid {style.border_color};">{header}</div>'
                f'<div style="padding: 10px;">'
                f'{rendered_content}'
                f'</div>'
                f'</div>')
        return html
    
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
        style = self.STYLES[MessageType.ASSISTANT]
        tool_calls = tool_calls or []
        
        html_parts = []
        
        # Main message container
        html_parts.append(
            f'<div style="border: 1px solid {style.border_color}; border-radius: 5px; margin-bottom: 12px; overflow: hidden;">'
            f'<div style="background-color: {style.background_color}; padding: 8px 10px; font-weight: bold; border-bottom: 1px solid {style.border_color};">Assistant</div>'
            f'<div style="padding: 10px;">'
        )
        
        # Display reasoning content if present
        if reasoning_content:
            reasoning_style = self.STYLES[MessageType.REASONING]
            html_parts.append(
                f'<div style="background-color: {reasoning_style.background_color}; border-left: 4px solid {reasoning_style.border_color}; padding: 8px; margin-bottom: 12px;">'
                f'<div style="color: {reasoning_style.text_color}; font-weight: bold;">Reasoning:</div>'
                f'{self._render_content(reasoning_content)}'
                f'</div>'
            )
            # Add separation between reasoning and main content
            html_parts.append('<div style="height: 4px;"></div>')
        
        # Display main content
        if content:
            html_parts.append(self._render_content(content))
        
        html_parts.append('</div></div>')
        
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
        
        if is_special:
            # Special tool: blue styling, no truncation, full markdown
            return (
                f'<div style="margin-left: 20px; margin-top: 8px; margin-bottom: 8px; border-left: 4px solid #3498db; background-color: #eef4ff; padding: 8px; border-radius: 4px; display: block; clear: both;">'
                f'<div style="color: #0000FF; font-weight: bold; display: block; clear: both;">Tool: {html.escape(tool_name)}</div>'
                f'<div style="color: #666666; font-size: 0.9em;">Arguments: {html.escape(arguments)}</div>'
                f'</div>'
            )
        else:
            # Regular tool: truncate arguments, monospace font
            args_str = str(arguments)
            if len(args_str) > 200:
                args_str = args_str[:200] + '...'
            escaped_args = html.escape(args_str)
            return (
                f'<div style="margin-left: 20px; margin-top: 5px; margin-bottom: 5px; display: block; clear: both;">'
                f'<div style="color: #006400; font-weight: bold; display: block; clear: both;">Tool: {html.escape(tool_name)}</div>'
                f'<div style="color: #666666; font-size: 0.9em; font-family: monospace, monospace;">Arguments: {escaped_args}</div>'
                f'</div>'
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

        # Debug logging
        # print(f"[DEBUG] render_tool_result: tool_name={tool_name}, is_special={is_special}, content length={len(content)}")
        
        if is_special:
            # print(f"[DEBUG] render_tool_result special branch")
            # Special tool: blue styling, full markdown rendering
            display_name = f" ({tool_name})" if tool_name else ""
            return (
                f'<div style="margin-left: 20px; margin-top: 8px; margin-bottom: 10px; border-left: 4px solid #3498db; background-color: #eef4ff; padding: 8px; border-radius: 4px; display: block; clear: both;">'
                f'<div style="color: #0000FF; font-weight: bold; margin-bottom: 4px;">Tool Result{display_name}</div><br>'
                f'<div style="color: #0000FF; margin-left: 10px;">{self._render_content(content)}</div>'
                f'</div>'
            )
        else:
            # Regular tool: truncate plain text, HTML escape, monospace font
            if enable_truncation:
                truncated_content = self._truncate_plain_text(content, tool_name)
            else:
                truncated_content = content
            
            escaped_content = html.escape(truncated_content)
            
            if error:
                # print(f"[DEBUG] render_tool_result error: {error}")
                # Error styling
                return (
                    f'<div style="margin-left: 20px; margin-top: 5px; margin-bottom: 10px; display: block; clear: both;">'
                    f'<div style="color: #FF0000; font-weight: bold; margin-bottom: 4px; clear: both;">Error:</div><br>'
                    f'<div style="color: #FF0000; font-family: monospace, monospace; white-space: pre-wrap; margin-left: 10px;">{html.escape(error)}</div>'
                    f'</div>'
                )
            elif not success:
                # print(f"[DEBUG] render_tool_result warning: success={success}")
                # Warning styling
                return (
                    f'<div style="margin-left: 20px; margin-top: 5px; margin-bottom: 10px; display: block; clear: both;">'
                    f'<div style="color: #FFA500; font-weight: bold; margin-bottom: 4px; clear: both;">Warning:</div><br>'
                    f'<div style="color: #FFA500; font-family: monospace, monospace; white-space: pre-wrap; margin-left: 10px;">{escaped_content}</div>'
                    f'</div>'
                )
            else:
                # Success styling
                # Build the result HTML with explicit line break
                result_html = f'''
<div style="margin-top: 8px; margin-bottom: 8px;">
    <div style="font-weight: bold;">Result:</div>
    <br>
    <div style="color: #006400; font-family: monospace; white-space: pre-wrap; margin-left: 10px;">{escaped_content}</div>
</div>
'''
                # print(f"[DEBUG] render_tool_result success HTML: {result_html}")
                return result_html
    
    def render_system_message(self, content: str, created_at: str = "") -> str:
        """
        Render a system message.
        
        Args:
            content: Message content
            created_at: Timestamp (optional)
            
        Returns:
            HTML string
        """
        style = self.STYLES[MessageType.SYSTEM]
        
        # Check if content already has [SYSTEM] prefix
        if content.startswith('[SYSTEM]'):
            content = content[8:].lstrip()
        
        rendered_content = self._render_content(content)
        
        html = (f'<div style="border: 1px solid {style.border_color}; border-radius: 5px; margin-bottom: 8px; overflow: hidden;">'
                f'<div style="background-color: {style.background_color}; padding: 8px 10px; font-weight: bold; border-bottom: 1px solid {style.border_color};">System</div>'
                f'<div style="padding: 10px;">'
                f'{rendered_content}'
                f'</div>'
                f'</div>')
        return html
    
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
        content = event.get('content', '')
        tool_calls = event.get('tool_calls', [])
        reasoning_content = event.get('reasoning_content', '')
        tool_call_id = event.get('tool_call_id', '')
        
        # Handle standalone tool calls and results using existing methods
        if event_type == 'tool_call':
            tool_name = event.get('function', {}).get('name', 'unknown')
            arguments = event.get('function', {}).get('arguments', '{}')
            call_id = event.get('id', '')
            created_at = event.get('created_at', '')
            return self.render_standalone_tool_call(tool_name, arguments, call_id, created_at)
        
        if event_type == 'tool_result':
            tool_name = event.get('tool_name', '')  # Note: OutputPanel uses tool_call_map, but we can't access it here
            success = event.get('success', True)
            error = event.get('error', '')
            created_at = event.get('created_at', '')
            # Note: We need to pass tool_name from event or tool_call_map
            # For now, use empty string; OutputPanel will need to provide tool_name
            return self.render_standalone_tool_result(
                content, tool_name, tool_call_id, success, error,
                ENABLE_RESULT_TRUNCATION, created_at
            )
        
        # Map event types to existing render methods
        if event_type == 'user_query':
            is_system_notification = self._is_system_message(content)
            return self.render_user_message(content, '', is_system_notification)
        
        if event_type == 'turn':
            # For assistant messages with tool calls and reasoning
            return self.render_assistant_message(content, tool_calls, reasoning_content, '')
        
        if event_type in ('system', 'token_warning', 'turn_warning'):
            return self.render_system_message(content, '')
        
        # Special handling for final events (orange styling)
        if event_type == 'final':
            # Final events not in MessageRenderer, keep original orange styling
            border_color = '#FFA500'
            bg_color = '#FFF5E6'
            header = 'Final'
            rendered_content = self._render_content(content)
            html_block = f'''<div style="border: 1px solid {border_color}; border-radius: 5px; margin-bottom: 12px; overflow: hidden; width: 100%;">
                <div style="background-color: {bg_color} !important; padding: 8px 10px; font-weight: bold; border-bottom: 1px solid {border_color}; margin: 0 !important; display: inline-block !important; vertical-align: top; width: 100% !important; box-sizing: border-box; min-width: 100% !important; position: relative;">{header}</div>
                <div style="padding: 10px; width: 100%; box-sizing: border-box;">
                    {rendered_content}
                </div>
            </div>'''
            return html_block
        
        # Generic fallback for unknown event types
        # Note: log function not available here; could use print for debugging
        # print(f'DEBUG render_event unknown event_type: {event_type}')
        border_color = '#cccccc'
        bg_color = self.STYLES[MessageType.REASONING].background_color
        header = event_type.replace('_', ' ').title()
        rendered_content = self._render_content(content)
        html_block = f'''<div style="border: 1px solid {border_color}; border-radius: 5px; margin-bottom: 12px; overflow: hidden; width: 100%;">
            <div style="background-color: {bg_color} !important; padding: 8px 10px; font-weight: bold; border-bottom: 1px solid {border_color}; margin: 0 !important; display: inline-block !important; vertical-align: top; width: 100% !important; box-sizing: border-box; min-width: 100% !important; position: relative;">{header}</div>
            <div style="padding: 10px; width: 100%; box-sizing: border-box;">
                {rendered_content}
            </div>
        </div>'''
        return html_block

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
        
        if is_special:
            # Special tool: blue styling
            border_color = self.STYLES[MessageType.TOOL_CALL].border_color
            bg_color = self.STYLES[MessageType.TOOL_CALL].background_color
            header = f'Tool: {tool_name}'
        else:
            # Regular tool: green styling
            border_color = self.STYLES[MessageType.TOOL_RESULT].border_color
            bg_color = self.STYLES[MessageType.TOOL_RESULT].background_color
            header = f'Tool: {tool_name}'
        
        # Prepare arguments display
        args_str = str(arguments)
        if len(args_str) > 200 and not is_special:
            args_str = args_str[:200] + '...'
        escaped_args = html.escape(args_str)
        
        # Content background for special tools
        content_background = f'background-color: {bg_color};' if is_special else ''
        
        html_block = f'''<div style="border: 1px solid {border_color}; border-radius: 5px; margin-bottom: 12px; overflow: hidden; width: 100%;">
            <div style="background-color: {bg_color} !important; padding: 8px 10px; font-weight: bold; border-bottom: 1px solid {border_color}; margin: 0 !important; display: inline-block !important; vertical-align: top; width: 100% !important; box-sizing: border-box; min-width: 100% !important; position: relative;">{header}</div>
            <div style="padding: 10px; {content_background} width: 100%; box-sizing: border-box;">'''
        
        if not is_special:
            html_block += f'<div style="color: #666666; font-size: 0.9em; font-family: monospace, monospace;">Arguments: {escaped_args}</div>'
        
        html_block += '</div></div>'
        return html_block

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
        
        if is_special:
            # Special tool: blue styling
            border_color = self.STYLES[MessageType.TOOL_CALL].border_color
            bg_color = self.STYLES[MessageType.TOOL_CALL].background_color
            header = f'Tool Result ({tool_name})' if tool_name else 'Tool Result'
        else:
            # Regular tool: green styling
            border_color = self.STYLES[MessageType.TOOL_RESULT].border_color
            bg_color = self.STYLES[MessageType.TOOL_RESULT].background_color
            header = 'Tool Result'
        
        # Content background for special tools
        content_background = f'background-color: {bg_color};' if is_special else ''
        
        # Render content based on tool type and success status
        if error:
            rendered_content = f'<div style="color: #FF0000; font-family: monospace, monospace; white-space: pre-wrap;">{html.escape(error)}</div>'
        elif not success:
            rendered_content = f'<div style="color: #FFA500; font-family: monospace, monospace; white-space: pre-wrap;">{html.escape(content)}</div>'
        else:
            if is_special:
                # Special tool: full markdown rendering
                rendered_content = self._render_content(content)
            else:
                # Regular tool: truncate plain text, HTML escape, monospace font
                if enable_truncation:
                    truncated_content = self._truncate_plain_text(content, tool_name)
                else:
                    truncated_content = content
                escaped_content = html.escape(truncated_content)
                rendered_content = f'<div style="font-family: monospace, monospace; white-space: pre-wrap;">{escaped_content}</div>'
        
        html_block = f'''<div style="border: 1px solid {border_color}; border-radius: 5px; margin-bottom: 12px; overflow: hidden; width: 100%;">
            <div style="background-color: {bg_color} !important; padding: 8px 10px; font-weight: bold; border-bottom: 1px solid {border_color}; margin: 0 !important; display: inline-block !important; vertical-align: top; width: 100% !important; box-sizing: border-box; min-width: 100% !important; position: relative;">{header}</div>
            <div style="padding: 10px; {content_background} width: 100%; box-sizing: border-box;">
                {rendered_content}
            </div>
        </div>'''
        return html_block

    # Helper methods for event_models.py to centralize CSS
    def get_tool_call_container_style(self, is_special: bool = False) -> str:
        """Get CSS style for tool call container."""
        if is_special:
            return "border-left: 4px solid #3498db; background-color: #eef4ff; padding: 8px; margin: 8px 0; border-radius: 4px; display: block; clear: both;"
        else:
            return "margin-left: 20px; margin-top: 5px; margin-bottom: 5px; display: block; clear: both;"
    
    def get_tool_call_header_style(self, is_special: bool = False) -> str:
        """Get CSS style for tool call header."""
        if is_special:
            return "color: #0000FF; font-weight: bold; display: block; clear: both;"
        else:
            return "color: #006400; font-weight: bold; display: block; clear: both;"
    
    def get_tool_result_container_style(self, is_special: bool = False) -> str:
        """Get CSS style for tool result container."""
        if is_special:
            return "border-left: 4px solid #3498db; background-color: #eef4ff; padding: 8px; margin: 8px 0; border-radius: 4px; display: block; clear: both;"
        else:
            return "margin-left: 20px; margin-top: 5px; margin-bottom: 10px; display: block; clear: both;"
    
    def get_tool_result_header_style(self, is_special: bool = False) -> str:
        """Get CSS style for tool result header."""
        if is_special:
            return "color: #0000FF; font-weight: bold; display: block; clear: both;"
        else:
            return "color: #006400; font-weight: bold; display: block; clear: both;"
    
    def get_event_title_style(self) -> str:
        """Get CSS style for event title bar."""
        return "font-weight: bold; background-color: #e0e0e0; padding: 3px; display: block; clear: both;"
    
    def get_reasoning_container_style(self) -> str:
        """Get CSS style for reasoning container."""
        return "background-color: #f8f8f8; border-left: 4px solid #888; padding: 8px; margin-bottom: 12px;"
    
    def get_reasoning_header_style(self) -> str:
        """Get CSS style for reasoning header."""
        return "color: #333; font-weight: bold;"