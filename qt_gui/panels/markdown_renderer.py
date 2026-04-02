"""Markdown Renderer: Convert markdown to HTML using Qt's built-in support with fallback."""
import re
import html as html_module
from PyQt6.QtGui import QTextDocument


class MarkdownRenderer:
    """Render markdown to HTML using Qt's built-in markdown support with fallback."""

    @staticmethod
    def markdown_to_html(text, style=""):
        """
        Convert markdown text to HTML.

        Args:
            text: Markdown text to convert
            style: Optional CSS style string to apply to the content

        Returns:
            HTML string with markdown converted to HTML
        """
        # First unescape any HTML entities
        unescaped_text = html_module.unescape(text)

        # Try Qt's built-in markdown support first
        try:
            doc = QTextDocument()
            doc.setMarkdown(unescaped_text)
            html_result = doc.toHtml()

            # Extract just the body content (Qt adds full HTML document)
            # Look for <body> tag
            body_start = html_result.find('<body>')
            body_end = html_result.find('</body>')
            if body_start != -1 and body_end != -1:
                # Extract body content plus 6 for '<body>'
                body_content = html_result[body_start + 6:body_end]
                # Also need to include any styles in the head
                # For simplicity, we'll just use the body content
                html_result = body_content.strip()

            # Apply style if provided
            if style and html_result:
                # Wrap in div with inline style (span is inline, div is block)
                # Check if content likely contains block elements
                if any(tag in html_result for tag in ['<div', '<p>', '<h1', '<h2', '<h3', '<h4', '<h5', '<h6', '<ul', '<ol', '<li', '<blockquote', '<pre', '<hr', '<br>']):
                    html_result = f'<div style="{style}">{html_result}</div>'
                else:
                    html_result = f'<span style="{style}">{html_result}</span>'

            return html_result

        except Exception:
            # Fall back to custom markdown parser
            return MarkdownRenderer._fallback_markdown_to_html(unescaped_text, style)

    @staticmethod
    def _fallback_markdown_to_html(text, style=""):
        """Custom markdown parser as fallback when Qt's markdown fails."""
        # Escape HTML special characters
        escaped = html_module.escape(text)

        # Process line by line for block elements
        lines = escaped.split('\n')
        result_lines = []
        in_list = False
        list_type = None  # 'ul' or 'ol'
        in_paragraph = False

        for line in lines:
            # Headers: # Header 1, ## Header 2, etc.
            header_match = re.match(r'^(#{1,6})\s+(.+)$', line)
            if header_match:
                if in_paragraph:
                    result_lines.append('<br/>')
                    in_paragraph = False
                if in_list:
                    result_lines.append(f'</{list_type}>')
                    in_list = False
                    list_type = None
                level = len(header_match.group(1))
                content = header_match.group(2)
                result_lines.append(f'<h{level}>{content}</h{level}>')
                continue

            # Horizontal rule: --- or *** (three or more)
            if re.match(r'^---+\s*$', line) or re.match(r'^\*\*\*+\s*$', line):
                if in_paragraph:
                    result_lines.append('<br/>')
                    in_paragraph = False
                if in_list:
                    result_lines.append(f'</{list_type}>')
                    in_list = False
                    list_type = None
                result_lines.append('<hr/>')
                continue

            # Blockquote: > text
            if line.startswith('> ') or line.startswith('&gt; '):
                if in_paragraph:
                    result_lines.append('<br/>')
                    in_paragraph = False
                if in_list:
                    result_lines.append(f'</{list_type}>')
                    in_list = False
                    list_type = None
                content = line[2:] if line.startswith('> ') else line[5:]  # Remove '&gt; ' (5 chars)
                result_lines.append(f'<blockquote>{content}</blockquote>')
                continue

            # Unordered list: - item or * item
            list_match = re.match(r'^[-*+]\s+(.+)$', line)
            if list_match:
                if in_paragraph:
                    result_lines.append('<br/>')
                    in_paragraph = False
                if in_list:
                    result_lines.append(f'</{list_type}>')
                    in_list = False
                    list_type = None
                content = list_match.group(1)
                if not in_list or list_type != 'ul':
                    if in_list:
                        result_lines.append(f'</{list_type}>')
                    result_lines.append('<ul>')
                    in_list = True
                    list_type = 'ul'
                result_lines.append(f'<li>{content}</li>')
                continue

            # Ordered list: 1. item
            ordered_match = re.match(r'^\d+\.\s+(.+)$', line)
            if ordered_match:
                if in_paragraph:
                    result_lines.append('<br/>')
                    in_paragraph = False
                if in_list:
                    result_lines.append(f'</{list_type}>')
                    in_list = False
                    list_type = None
                content = ordered_match.group(1)
                if not in_list or list_type != 'ol':
                    if in_list:
                        result_lines.append(f'</{list_type}>')
                    result_lines.append('<ol>')
                    in_list = True
                    list_type = 'ol'
                result_lines.append(f'<li>{content}</li>')
                continue

            # Empty line
            if line.strip() == '':
                if in_paragraph:
                    result_lines.append('<br/>')
                    in_paragraph = False
                if in_list:
                    result_lines.append(f'</{list_type}>')
                    in_list = False
                    list_type = None
                continue

            # Regular text line
            if not in_paragraph:
                in_paragraph = True
            result_lines.append(line)

        # Close any open structures
        if in_paragraph:
            result_lines.append('<br/>')
        if in_list:
            result_lines.append(f'</{list_type}>')

        # Join lines - block elements already have proper HTML
        escaped = ''.join(result_lines)

        # Now apply inline formatting
        # Process code blocks first to protect them from other markdown
        escaped = re.sub(r'`(.+?)`', r'<code>\1</code>', escaped)
        # Handle triple asterisks/underscores (bold+italic)
        escaped = re.sub(r'\*\*\*(.+?)\*\*\*', r'<b><i>\1</i></b>', escaped)
        escaped = re.sub(r'___(.+?)___', r'<b><i>\1</i></b>', escaped)
        # Bold: **text** or __text__
        escaped = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', escaped)
        escaped = re.sub(r'__(.+?)__', r'<b>\1</b>', escaped)
        # Italic: *text* or _text_
        escaped = re.sub(r'\*(?!\*)(.+?)\*(?!\*)', r'<i>\1</i>', escaped)
        escaped = re.sub(r'_(?!_)(.+?)_(?!_)', r'<i>\1</i>', escaped)
        # Strikethrough: ~~text~~
        escaped = re.sub(r'~~(.+?)~~', r'<s>\1</s>', escaped)
        # Links: [text](url)
        escaped = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', escaped)

        # Apply style if provided
        if style:
            # Wrap in div with inline style (span is inline, div is block)
            # Check if content likely contains block elements
            if any(tag in escaped for tag in ['<div', '<p>', '<h1', '<h2', '<h3', '<h4', '<h5', '<h6', '<ul', '<ol', '<li', '<blockquote', '<pre', '<hr', '<br>']):
                escaped = f'<div style="{style}">{escaped}</div>'
            else:
                escaped = f'<span style="{style}">{escaped}</span>'

        return escaped
