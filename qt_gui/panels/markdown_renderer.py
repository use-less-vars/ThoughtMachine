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
            # Look for <body> tag case-insensitively
            import re
            body_match = re.search(r'<body[^>]*>(.*?)</body>', html_result, re.IGNORECASE | re.DOTALL)
            if body_match:
                body_content = body_match.group(1).strip()
                # Keep <p> elements as-is. Qt DOES support font-size on <p> elements
                # when set via inline style, but NOT on nested <div> elements.
                # The previous <p>-><div> conversion broke font-size propagation.
                html_result = body_content
            else:
                # If no body tag found, strip any DOCTYPE, html, head tags
                # Remove DOCTYPE declaration
                html_result = re.sub(r'<!DOCTYPE[^>]*>', '', html_result, flags=re.IGNORECASE)
                # Remove <html> tags
                html_result = re.sub(r'<html[^>]*>', '', html_result, flags=re.IGNORECASE)
                html_result = re.sub(r'</html>', '', html_result, flags=re.IGNORECASE)
                # Remove <head> tags and everything between them
                html_result = re.sub(r'<head[^>]*>.*?</head>', '', html_result, flags=re.IGNORECASE | re.DOTALL)
                html_result = html_result.strip()

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

        # Process code blocks (triple backticks) before line processing
        code_blocks = []
        import re
        
        def replace_code_block(match):
            # Extract language (optional) and code content
            # match groups: group(1) = language (optional), group(2) = code content
            language = match.group(1) or ''
            code_content = match.group(2)
            # Store the code block with placeholder
            placeholder = f"@@@CB{len(code_blocks)}@@@"
            # Create HTML for code block
            if language:
                code_blocks.append(f'<pre style="background-color: #f5f5f5; border: 1px solid #ddd; border-radius: 3px; padding: 8px; overflow: auto; white-space: pre-wrap;"><code class="language-{language}" style="font-family: monospace, monospace;">{code_content}</code></pre>')
            else:
                code_blocks.append(f'<pre style="background-color: #f5f5f5; border: 1px solid #ddd; border-radius: 3px; padding: 8px; overflow: auto; white-space: pre-wrap;"><code style="font-family: monospace, monospace;">{code_content}</code></pre>')
            return placeholder
        
        # Regex for code blocks: ```language?\n?content\n``` (non-greedy, DOTALL)
        # Note: language cannot contain spaces, only word characters
        code_block_pattern = re.compile(r'```(\w*)\n?(.*?)```', re.DOTALL)
        escaped = code_block_pattern.sub(replace_code_block, escaped)

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
        escaped = re.sub(r'`(.+?)`', r'<code style="font-family: monospace, monospace; background-color: #f0f0f0; padding: 2px 4px; border-radius: 3px;">\1</code>', escaped)
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

        # Replace code block placeholders with actual HTML
        for i, code_block_html in enumerate(code_blocks):
            placeholder = f"@@@CB{i}@@@"
            escaped = escaped.replace(placeholder, code_block_html)

        # Apply style if provided
        if style:
            # Wrap in div with inline style (span is inline, div is block)
            # Check if content likely contains block elements
            if any(tag in escaped for tag in ['<div', '<p>', '<h1', '<h2', '<h3', '<h4', '<h5', '<h6', '<ul', '<ol', '<li', '<blockquote', '<pre', '<hr', '<br>']):
                escaped = f'<div style="{style}">{escaped}</div>'
            else:
                escaped = f'<span style="{style}">{escaped}</span>'

        return escaped
