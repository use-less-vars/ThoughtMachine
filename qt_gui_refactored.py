# qt_gui_refactored.py
"""
Refactored Agent GUI using Presenter/ViewModel pattern.

Features:
- Uses AgentPresenter for business logic
- Signal-based event handling (no polling)
- Clean separation of concerns
- Maintains backward compatibility with existing UI components
"""

import sys
import os
import json
import html
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QTextEdit, QListWidget, QListView, QStyledItemDelegate,
    QGroupBox, QCheckBox, QMenuBar, QMenu, QFileDialog, QStyleOptionViewItem,
    QMessageBox, QScrollArea, QFrame, QComboBox, QSpinBox, QDoubleSpinBox, QSplitter, QDialog, QSizePolicy,QStyle
)
from PyQt6.QtCore import Qt, QTimer, pyqtSlot, QAbstractListModel, QModelIndex, QVariant, QRect, QPoint, QSize, QSortFilterProxyModel
from PyQt6.QtGui import QAction, QFont, QTextDocument, QColor, QPainter, QPalette, QAbstractTextDocumentLayout
from dotenv import load_dotenv

from agent_presenter import AgentPresenter, AgentState
from config_service import create_agent_config_service, ConfigService
from tools import TOOL_CLASSES, SIMPLIFIED_TOOL_CLASSES

load_dotenv()

MAX_RESULT_LENGTH = 500  # characters before truncation


# --- Existing panel classes (unchanged) ---
class ToolLoaderPanel(QGroupBox):
    """Panel with checkboxes to enable/disable tools."""
    def __init__(self, tool_classes):
        super().__init__("Tool Loader")
        self.tool_classes = tool_classes
        self.tool_checkboxes = {}  # name -> QCheckBox
        
        layout = QVBoxLayout()
        for cls in tool_classes:
            checkbox = QCheckBox(cls.__name__)
            checkbox.setChecked(True)
            layout.addWidget(checkbox)
            self.tool_checkboxes[cls.__name__] = checkbox
        
        layout.addStretch()
        self.setLayout(layout)
    
    def get_enabled_tool_names(self):
        return [name for name, cb in self.tool_checkboxes.items() if cb.isChecked()]


class SystemViewPanel(QGroupBox):
    """Simple file browser panel."""
    def __init__(self):
        super().__init__("System View")
        try:
            self.current_dir = os.getcwd()
        except FileNotFoundError:
            # fallback to user's home directory
            self.current_dir = os.path.expanduser("~")
        
        layout = QVBoxLayout()
        
        dir_frame = QWidget()
        dir_layout = QHBoxLayout()
        dir_frame.setLayout(dir_layout)
        dir_label = QLabel("Dir:")
        dir_layout.addWidget(dir_label)
        
        self.dir_display = QLabel(self.current_dir)
        self.dir_display.setStyleSheet("color: blue;")
        self.dir_display.setWordWrap(True)
        dir_layout.addWidget(self.dir_display, 1)
        
        change_btn = QPushButton("Change")
        change_btn.clicked.connect(self.choose_directory)
        dir_layout.addWidget(change_btn)
        layout.addWidget(dir_frame)
        
        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget)
        
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh_list)
        layout.addWidget(refresh_btn)
        
        self.setLayout(layout)
        self.refresh_list()
    
    def choose_directory(self):
        new_dir = QFileDialog.getExistingDirectory(self, "Select Directory", self.current_dir)
        if new_dir:
            self.current_dir = new_dir
            self.dir_display.setText(new_dir)
            self.refresh_list()
    
    def refresh_list(self):
        self.list_widget.clear()
        try:
            entries = os.listdir(self.current_dir)
            for entry in sorted(entries):
                self.list_widget.addItem(entry)
        except Exception as e:
            self.list_widget.addItem(f"Error: {e}")


class AgenticHelpersPanel(QGroupBox):
    """Placeholder for future sub‑agent controls."""
    def __init__(self):
        super().__init__("Agentic Helpers")
        layout = QVBoxLayout()
        layout.addWidget(QLabel("(Placeholder)"))
        layout.addStretch()
        self.setLayout(layout)


class AgentControlsPanel(QGroupBox):
    """Collapsible panel for agent controls."""
    def __init__(self, tool_classes):
        super().__init__("Agent Controls")
        self.tool_classes = tool_classes
        self.tool_checkboxes = {}  # name -> QCheckBox
        self.is_collapsed = True
        
        # Create toggle button for collapse/expand
        self.toggle_button = QPushButton("▼ Show Controls")
        self.toggle_button.setMaximumWidth(120)
        self.toggle_button.clicked.connect(self.toggle_collapse)
        
        # Main layout for the panel
        self.main_layout = QVBoxLayout()
        self.setLayout(self.main_layout)
        
        # Add toggle button at top
        self.main_layout.addWidget(self.toggle_button)
        
        # Create container widget for controls (hidden when collapsed)
        self.controls_container = QWidget()
        self.controls_layout = QGridLayout()
        self.controls_layout.setSpacing(10)
        self.controls_layout.setColumnStretch(1, 1)
        self.controls_container.setLayout(self.controls_layout)
        
        # Row 0: Workspace controls
        row = 0
        self.controls_layout.addWidget(QLabel("Workspace:"), row, 0)
        self.workspace_display = QLabel("None (unrestricted)")
        self.workspace_display.setStyleSheet("color: blue;")
        self.workspace_display.setWordWrap(True)
        self.controls_layout.addWidget(self.workspace_display, row, 1)
        
        self.set_workspace_btn = QPushButton("Set")
        self.set_workspace_btn.setMaximumWidth(60)
        self.controls_layout.addWidget(self.set_workspace_btn, row, 2)
        
        self.clear_workspace_btn = QPushButton("Clear")
        self.clear_workspace_btn.setMaximumWidth(60)
        self.controls_layout.addWidget(self.clear_workspace_btn, row, 3)
        
        # Row 1: Token monitoring controls
        row += 1
        token_monitor_row = QWidget()
        token_monitor_layout = QHBoxLayout()
        token_monitor_row.setLayout(token_monitor_layout)
        token_monitor_layout.setSpacing(5)
        
        self.token_monitor_checkbox = QCheckBox("Token warnings")
        self.token_monitor_checkbox.setChecked(True)
        token_monitor_layout.addWidget(self.token_monitor_checkbox)
        
        token_monitor_layout.addWidget(QLabel("Warning:"))
        self.warning_threshold_spinbox = QSpinBox()
        self.warning_threshold_spinbox.setRange(1, 200)
        self.warning_threshold_spinbox.setValue(35)
        self.warning_threshold_spinbox.setSingleStep(1)
        token_monitor_layout.addWidget(self.warning_threshold_spinbox)
        self.warning_formatted_label = QLabel("(35k)")
        token_monitor_layout.addWidget(self.warning_formatted_label)
        token_monitor_layout.addWidget(QLabel("tokens"))
        
        token_monitor_layout.addWidget(QLabel("Critical:"))
        self.critical_threshold_spinbox = QSpinBox()
        self.critical_threshold_spinbox.setRange(1, 200)
        self.critical_threshold_spinbox.setValue(50)
        self.critical_threshold_spinbox.setSingleStep(1)
        token_monitor_layout.addWidget(self.critical_threshold_spinbox)
        self.critical_formatted_label = QLabel("(50k)")
        token_monitor_layout.addWidget(self.critical_formatted_label)
        
        self.controls_layout.addWidget(token_monitor_row, row, 0, 1, 4)
        
        # Row 2: Max turns control
        row += 1
        max_turns_row = QWidget()
        max_turns_layout = QHBoxLayout()
        max_turns_row.setLayout(max_turns_layout)
        max_turns_layout.setSpacing(5)
        
        max_turns_layout.addWidget(QLabel("Max turns:"))
        self.max_turns_spinbox = QSpinBox()
        self.max_turns_spinbox.setRange(1, 500)
        self.max_turns_spinbox.setValue(100)
        max_turns_layout.addWidget(self.max_turns_spinbox)
        max_turns_layout.addWidget(QLabel("turns"))
        
        self.controls_layout.addWidget(max_turns_row, row, 0, 1, 4)
        
        # Row 3: Temperature control
        row += 1
        temperature_row = QWidget()
        temperature_layout = QHBoxLayout()
        temperature_row.setLayout(temperature_layout)
        temperature_layout.setSpacing(5)
        
        temperature_layout.addWidget(QLabel("Temperature:"))
        self.temperature_spinbox = QDoubleSpinBox()
        self.temperature_spinbox.setRange(0.0, 2.0)
        self.temperature_spinbox.setValue(0.2)
        self.temperature_spinbox.setSingleStep(0.1)
        self.temperature_spinbox.setDecimals(1)
        temperature_layout.addWidget(self.temperature_spinbox)
        temperature_layout.addWidget(QLabel(""))
        
        self.controls_layout.addWidget(temperature_row, row, 0, 1, 4)
        
        # Row 4: Model selection
        row += 1
        model_row = QWidget()
        model_layout = QHBoxLayout()
        model_row.setLayout(model_layout)
        model_layout.setSpacing(5)
        
        model_layout.addWidget(QLabel("Model:"))
        self.model_combo = QComboBox()
        self.model_combo.addItems(["deepseek-reasoner", "gpt-4", "claude-3", "llama-3"])
        self.model_combo.setCurrentText("deepseek-reasoner")
        model_layout.addWidget(self.model_combo)
        model_layout.addStretch()
        
        self.controls_layout.addWidget(model_row, row, 0, 1, 4)
        
        # Row 5: Tool output token limit
        row += 1
        tool_limit_row = QWidget()
        tool_limit_layout = QHBoxLayout()
        tool_limit_row.setLayout(tool_limit_layout)
        tool_limit_layout.setSpacing(5)
        
        tool_limit_layout.addWidget(QLabel("Tool output limit:"))
        self.tool_output_limit_spinbox = QSpinBox()
        self.tool_output_limit_spinbox.setRange(1000, 100000)
        self.tool_output_limit_spinbox.setValue(10000)
        self.tool_output_limit_spinbox.setSingleStep(1000)
        tool_limit_layout.addWidget(self.tool_output_limit_spinbox)
        tool_limit_layout.addWidget(QLabel("tokens"))
        
        self.controls_layout.addWidget(tool_limit_row, row, 0, 1, 4)
        
        # Row 6: Detail combo
        row += 1
        detail_row = QWidget()
        detail_layout = QHBoxLayout()
        detail_row.setLayout(detail_layout)
        detail_layout.setSpacing(5)
        
        detail_layout.addWidget(QLabel("Detail:"))
        self.detail_combo = QComboBox()
        self.detail_combo.addItems(["minimal", "normal", "verbose"])
        self.detail_combo.setCurrentText("normal")
        detail_layout.addWidget(self.detail_combo)
        detail_layout.addStretch()
        
        self.controls_layout.addWidget(detail_row, row, 0, 1, 4)
        
        # Row 7: Tool loader (as a sub-group)
        row += 1
        tool_group = QGroupBox("Tools")
        tool_layout = QGridLayout()
        tool_group.setLayout(tool_layout)
        
        # Add tool checkboxes in 2 columns
        col = 0
        tool_row = 0
        for i, cls in enumerate(self.tool_classes):
            checkbox = QCheckBox(cls.__name__)
            checkbox.setChecked(True)
            tool_layout.addWidget(checkbox, tool_row, col)
            self.tool_checkboxes[cls.__name__] = checkbox
            
            col += 1
            if col >= 2:
                col = 0
                tool_row += 1
        
        # Add stretch to fill remaining space
        tool_layout.setRowStretch(tool_row + 1, 1)
        
        # Wrap tool group in a scroll area to limit height
        tool_scroll_area = QScrollArea()
        tool_scroll_area.setWidgetResizable(True)
        tool_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        tool_scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        tool_scroll_area.setMaximumHeight(400)  # Limit height, show scrollbar if needed
        tool_scroll_area.setWidget(tool_group)
        
        self.controls_layout.addWidget(tool_scroll_area, row, 0, 1, 4)
        
        # Add controls container to main layout
        self.main_layout.addWidget(self.controls_container)
        
        # Initially collapse the panel
        self.controls_container.setVisible(False)
        
        # Set up debounce timers for threshold changes
        self._warning_threshold_timer = QTimer()
        self._warning_threshold_timer.setSingleShot(True)
        self._warning_threshold_timer.timeout.connect(self._adjust_warning_threshold)
        self._critical_threshold_timer = QTimer()
        self._critical_threshold_timer.setSingleShot(True)
        self._critical_threshold_timer.timeout.connect(self._adjust_critical_threshold)
        
        # Connect signals
        self.warning_threshold_spinbox.valueChanged.connect(self._on_warning_threshold_changed)
        self.critical_threshold_spinbox.valueChanged.connect(self._on_critical_threshold_changed)
        self.token_monitor_checkbox.stateChanged.connect(self.update_token_monitor_controls)
        
        # Initial updates
        self.update_token_monitor_controls()
        self._update_token_threshold_labels()
    
    def toggle_collapse(self):
        """Toggle visibility of controls."""
        self.is_collapsed = not self.is_collapsed
        self.controls_container.setVisible(not self.is_collapsed)
        if self.is_collapsed:
            self.toggle_button.setText("▼ Show Controls")
        else:
            self.toggle_button.setText("▲ Hide Controls")
        
        # Adjust size to fit content
        self.adjustSize()
    
    def get_enabled_tool_names(self):
        return [name for name, cb in self.tool_checkboxes.items() if cb.isChecked()]
    
    def update_token_monitor_controls(self):
        """Enable/disable token monitor threshold controls based on checkbox."""
        enabled = self.token_monitor_checkbox.isChecked()
        self.warning_threshold_spinbox.setEnabled(enabled)
        self.critical_threshold_spinbox.setEnabled(enabled)
    
    def _on_warning_threshold_changed(self, value):
        """Start debounced adjustment of warning threshold."""
        self._warning_threshold_timer.start(500)
    
    def _adjust_warning_threshold(self):
        """Ensure warning threshold is always lower than critical threshold."""
        value = self.warning_threshold_spinbox.value()
        critical = self.critical_threshold_spinbox.value()
        step = self.warning_threshold_spinbox.singleStep()
        if value >= critical:
            # Clamp warning to critical - step (instead of adjusting critical)
            clamped_value = critical - step
            if clamped_value < 1:
                clamped_value = 1
            # Temporarily block signals to prevent infinite recursion
            self.warning_threshold_spinbox.blockSignals(True)
            self.warning_threshold_spinbox.setValue(clamped_value)
            self.warning_threshold_spinbox.blockSignals(False)
        # Update formatted labels
        self._update_token_threshold_labels()
    
    def _on_critical_threshold_changed(self, value):
        """Start debounced adjustment of critical threshold."""
        self._critical_threshold_timer.start(500)
    
    def _adjust_critical_threshold(self):
        """Ensure critical threshold is always higher than warning threshold."""
        value = self.critical_threshold_spinbox.value()
        warning = self.warning_threshold_spinbox.value()
        step = self.critical_threshold_spinbox.singleStep()
        if value <= warning:
            # Clamp critical to warning + step (instead of adjusting warning)
            clamped_value = warning + step
            max_val = self.critical_threshold_spinbox.maximum()
            if clamped_value > max_val:
                clamped_value = max_val
            # Temporarily block signals to prevent infinite recursion
            self.critical_threshold_spinbox.blockSignals(True)
            self.critical_threshold_spinbox.setValue(clamped_value)
            self.critical_threshold_spinbox.blockSignals(False)
        # Update formatted labels
        self._update_token_threshold_labels()
    
    def _update_token_threshold_labels(self):
        """Update formatted labels for token thresholds."""
        # Format warning threshold (multiply by 1000 for display)
        warning_value = self.warning_threshold_spinbox.value() * 1000
        if warning_value >= 1000:
            warning_text = f"({warning_value // 1000}k)"
        else:
            warning_text = f"({warning_value})"
        self.warning_formatted_label.setText(warning_text)
        
        # Format critical threshold (multiply by 1000 for display)
        critical_value = self.critical_threshold_spinbox.value() * 1000
        if critical_value >= 1000:
            critical_text = f"({critical_value // 1000}k)"
        else:
            critical_text = f"({critical_value})"
        self.critical_formatted_label.setText(critical_text)
    
    def get_config_dict(self):
        """Return a dictionary of current control values suitable for JSON serialization."""
        config = {}
        config["temperature"] = self.temperature_spinbox.value()
        config["max_turns"] = self.max_turns_spinbox.value()
        config["token_monitor_enabled"] = self.token_monitor_checkbox.isChecked()
        config["warning_threshold"] = self.warning_threshold_spinbox.value()
        config["critical_threshold"] = self.critical_threshold_spinbox.value()
        # Workspace path: None if display is "None (unrestricted)"
        workspace_display = self.workspace_display.text()
        workspace_path = None if workspace_display == "None (unrestricted)" else workspace_display
        config["workspace_path"] = workspace_path
        config["tool_output_limit"] = self.tool_output_limit_spinbox.value()
        config["model"] = self.model_combo.currentText()
        config["detail"] = self.detail_combo.currentText()
        config["enabled_tools"] = [name for name, cb in self.tool_checkboxes.items() if cb.isChecked()]
        return config
    
    def set_config_dict(self, config):
        """Set control values from a configuration dictionary."""
        # Temperature
        if "temperature" in config:
            self.temperature_spinbox.setValue(config["temperature"])
        # Max turns
        if "max_turns" in config:
            self.max_turns_spinbox.setValue(config["max_turns"])
        # Token monitoring enabled
        if "token_monitor_enabled" in config:
            self.token_monitor_checkbox.setChecked(config["token_monitor_enabled"])
        # Warning threshold (in thousands)
        if "warning_threshold" in config:
            self.warning_threshold_spinbox.setValue(config["warning_threshold"])
        # Critical threshold (in thousands)
        if "critical_threshold" in config:
            self.critical_threshold_spinbox.setValue(config["critical_threshold"])
        # Workspace path
        if "workspace_path" in config:
            workspace_path = config["workspace_path"]
            if workspace_path is None:
                self.workspace_display.setText("None (unrestricted)")
            else:
                # Ensure path is normalized and absolute
                workspace_path = os.path.normpath(workspace_path)
                if not os.path.isabs(workspace_path):
                    workspace_path = os.path.abspath(workspace_path)
                self.workspace_display.setText(workspace_path)
        # Tool output limit
        if "tool_output_limit" in config:
            self.tool_output_limit_spinbox.setValue(config["tool_output_limit"])
        # Model selection
        if "model" in config:
            index = self.model_combo.findText(config["model"])
            if index >= 0:
                self.model_combo.setCurrentIndex(index)
        # Detail level
        if "detail" in config:
            index = self.detail_combo.findText(config["detail"])
            if index >= 0:
                self.detail_combo.setCurrentIndex(index)
        # Enabled tools
        if "enabled_tools" in config:
            enabled_names = set(config["enabled_tools"])
            for name, checkbox in self.tool_checkboxes.items():
                checkbox.setChecked(name in enabled_names)


class StatusPanel(QGroupBox):
    """Shows current status and token usage."""
    def __init__(self):
        super().__init__("Status")
        layout = QVBoxLayout()
        self.status_label = QLabel("Ready")
        self.token_label = QLabel("Tokens: 0 in / 0 out")
        layout.addWidget(self.status_label)
        self.context_label = QLabel("Context: 0 tokens")
        layout.addWidget(self.context_label)
        layout.addWidget(self.token_label)
        layout.addStretch()
        self.setLayout(layout)
    
    def update_status(self, text):
        self.status_label.setText(text)
    
    def format_tokens(self, tokens):
        """Format token count in thousands with 'k' suffix."""
        if tokens >= 1000:
            return f"{tokens // 1000}k"
        return str(tokens)
    
    def update_tokens(self, total_input, total_output):
        in_text = self.format_tokens(total_input)
        out_text = self.format_tokens(total_output)
        self.token_label.setText(f"Tokens: {in_text} in / {out_text} out")
    
    def update_context_length(self, context_tokens):
        text = self.format_tokens(context_tokens)
        self.context_label.setText(f"Context: {text} tokens")


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
        import html
        unescaped_text = html.unescape(text)
        
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
                # Wrap in span with inline style
                html_result = f'<span style="{style}">{html_result}</span>'
            
            return html_result
            
        except Exception:
            # Fall back to custom markdown parser
            return MarkdownRenderer._fallback_markdown_to_html(unescaped_text, style)
    
    @staticmethod
    def _fallback_markdown_to_html(text, style=""):
        """Custom markdown parser as fallback when Qt's markdown fails."""
        import re
        import html as html_module
        
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
            if re.match(r'^---+s*$', line) or re.match(r'^\*\*\*+\s*$', line):
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
            escaped = f'<span style="{style}">{escaped}</span>'
        
        return escaped


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
                if detail_level == "minimal":
                    add_line(f"🛠️ {tc['name']}", style="color: #0000FF;")
                else:
                    add_line(f"🛠️ {tc['name']}", style="color: #0000FF; font-weight: bold;")
                    if detail_level == "verbose":
                        add_line(f"  Arguments: {tc['arguments']}", style="color: #0000AA;")
                
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
            
        elif etype == "stopped":
            add_line("Agent stopped by user.", style="color: #FF8C00;")
        elif etype == "user_interaction_requested":
            add_line(f"Agent requests interaction: {event.get('message', '')}", style="color: #008080;")
        elif etype == "token_warning":
            add_line(event.get("message", ""), style="color: #FFA500; font-weight: bold;")
        elif etype == "turn_warning":
            add_line(event.get("message", ""), style="color: #FFA500; font-weight: bold;")
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
            
        elif etype == "stopped":
            add_line("Agent stopped by user.")
        elif etype == "user_interaction_requested":
            add_line(f"Agent requests interaction: {event.get('message', '')}")
        elif etype == "token_warning":
            add_line(event.get("message", ""))
        elif etype == "turn_warning":
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


# --- Main GUI class (refactored to use Presenter) ---
class AgentGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        
        # Initialize presenter and config service
        self.presenter = AgentPresenter()
        self.config_service = create_agent_config_service()
        
        # Token tracking (now managed by presenter but also cached locally for UI)
        self.total_input = 0
        self.total_output = 0
        self.context_length = 0
        
        # State tracking
        self.last_history = None
        self._cached_config = None  # Config created by restart_session for next run
        
        # Smart scrolling tracking
        self._auto_scroll_enabled = True
        self._user_scrolled_away = False
        self._programmatic_scroll = False
        
        # Configuration auto-save timer
        self._config_save_timer = QTimer()
        self._config_save_timer.setSingleShot(True)
        self._config_save_timer.timeout.connect(self.save_config)
        self._loading_config = False  # Flag to prevent save during load
        
        # Event history and pagination
        self.event_history = []  # All events stored as dictionaries
        self.visible_event_widgets = []  # Widgets currently displayed
        self.max_visible_events = 100  # Maximum events to show at once
        
        self.init_ui()
        self.setup_signal_connections()
        self.load_config()
    
    def init_ui(self):
        """Initialize the user interface (unchanged layout)."""
        self.setWindowTitle("Agent Workbench - QT (Refactored)")
        self.setGeometry(100, 100, 1400, 900)
        
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout()
        central_widget.setLayout(main_layout)
        
        splitter = QSplitter(Qt.Orientation.Horizontal)
        
        # Left panel
        self.system_view = SystemViewPanel()
        splitter.addWidget(self.system_view)
        
        # Middle panels
        middle_container = QWidget()
        middle_layout = QVBoxLayout()
        middle_container.setLayout(middle_layout)
        
        self.helpers_panel = AgenticHelpersPanel()
        middle_layout.addWidget(self.helpers_panel)
        self.status_panel = StatusPanel()
        middle_layout.addWidget(self.status_panel)
        middle_layout.addStretch()
        splitter.addWidget(middle_container)
        
        # Right panel (AgentView)
        right_container = QWidget()
        right_layout = QVBoxLayout()
        right_container.setLayout(right_layout)
        
        # Agent Controls Panel
        self.agent_controls_panel = AgentControlsPanel(SIMPLIFIED_TOOL_CLASSES)
        right_layout.addWidget(self.agent_controls_panel)
        
        # Connect workspace buttons
        self.agent_controls_panel.set_workspace_btn.clicked.connect(self.set_workspace)
        self.agent_controls_panel.clear_workspace_btn.clicked.connect(self.clear_workspace)
        
        # Connect all controls to configuration update
        self.agent_controls_panel.temperature_spinbox.valueChanged.connect(self._handle_config_change)
        self.agent_controls_panel.max_turns_spinbox.valueChanged.connect(self._handle_config_change)
        self.agent_controls_panel.tool_output_limit_spinbox.valueChanged.connect(self._handle_config_change)
        self.agent_controls_panel.model_combo.currentTextChanged.connect(self._handle_config_change)
        self.agent_controls_panel.detail_combo.currentTextChanged.connect(self._handle_config_change)
        self.agent_controls_panel.token_monitor_checkbox.stateChanged.connect(self._handle_config_change)
        self.agent_controls_panel.warning_threshold_spinbox.valueChanged.connect(self._handle_config_change)
        self.agent_controls_panel.critical_threshold_spinbox.valueChanged.connect(self._handle_config_change)
        
        # Connect tool checkboxes
        for checkbox in self.agent_controls_panel.tool_checkboxes.values():
            checkbox.stateChanged.connect(self._handle_config_change)
        
        # Create filter controls for event list
        filter_widget = QWidget()
        filter_layout = QHBoxLayout()
        filter_widget.setLayout(filter_layout)
        
        filter_layout.addWidget(QLabel("Filter:"))
        self.filter_lineedit = QLineEdit()
        self.filter_lineedit.setPlaceholderText("Search events...")
        self.filter_lineedit.textChanged.connect(self._apply_filter)
        filter_layout.addWidget(self.filter_lineedit, 1)  # Stretch
        
        filter_layout.addWidget(QLabel("Type:"))
        self.filter_type_combo = QComboBox()
        self.filter_type_combo.addItems(["all", "turn", "final", "user_query", "stopped", 
                                         "user_interaction_requested", "token_warning", 
                                         "turn_warning", "paused", "max_turns", "error", 
                                         "thread_finished"])
        self.filter_type_combo.currentTextChanged.connect(self._apply_filter)
        filter_layout.addWidget(self.filter_type_combo)
        
        right_layout.addWidget(filter_widget)

        # Create output area for agent events using virtual scrolling
        self.event_model = EventModel()
        self.filter_proxy_model = EventFilterProxyModel()
        self.filter_proxy_model.setSourceModel(self.event_model)
        
        self.event_list_view = QListView()
        self.event_list_view.setModel(self.filter_proxy_model)
        self.event_list_view.setItemDelegate(EventDelegate())
        self.event_list_view.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.event_list_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.event_list_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.event_list_view.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self.event_list_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.event_list_view.customContextMenuRequested.connect(self._on_event_context_menu)

        right_layout.addWidget(self.event_list_view, 4)  # Larger stretch factor        
        # Monitor scrollbar to track user scrolling
        self.event_list_view.verticalScrollBar().valueChanged.connect(self._on_scrollbar_value_changed)        
        # Query input and buttons at bottom
        query_frame = QFrame()
        query_frame.setFrameStyle(QFrame.Shape.Box)
        query_layout = QVBoxLayout()
        query_frame.setLayout(query_layout)
        
        query_layout.addWidget(QLabel("Query:"))
        self.query_entry = QTextEdit()
        self.query_entry.setMaximumHeight(100)
        self.query_entry.setPlaceholderText("Enter your query here...")
        query_layout.addWidget(self.query_entry)
        
        button_layout = QHBoxLayout()
        self.run_btn = QPushButton("Run")
        self.run_btn.clicked.connect(self.run_agent)
        button_layout.addWidget(self.run_btn)
        
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self.stop_agent)
        self.stop_btn.setEnabled(False)
        button_layout.addWidget(self.stop_btn)
        
        self.restart_btn = QPushButton("Restart")
        self.restart_btn.clicked.connect(self.restart_session)
        self.restart_btn.setEnabled(False)
        button_layout.addWidget(self.restart_btn)
        
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.clicked.connect(self.pause_agent)
        self.pause_btn.setEnabled(False)
        button_layout.addWidget(self.pause_btn)
        
        query_layout.addLayout(button_layout)
        right_layout.addWidget(query_frame)
        
        splitter.addWidget(right_container)
        
        # Set initial splitter sizes
        splitter.setSizes([200, 150, 1050])
        
        main_layout.addWidget(splitter)
        
        # Create menu bar
        self.create_menu_bar()
        
        # Update buttons based on initial state
        self.update_buttons()
    
    def setup_signal_connections(self):
        """Connect presenter signals to GUI slots."""
        # Connect presenter signals
        self.presenter.state_changed.connect(self.on_state_changed)
        self.presenter.event_received.connect(self.display_event)
        self.presenter.tokens_updated.connect(self.on_tokens_updated)
        self.presenter.context_updated.connect(self.on_context_updated)
        self.presenter.status_message.connect(self.on_status_message)
        self.presenter.error_occurred.connect(self.on_error_occurred)
        self.presenter.config_changed.connect(self.on_config_changed)
    
    # ----- Signal Handlers -----
    
    @pyqtSlot(AgentState)
    def on_state_changed(self, state):
        """Handle agent state changes."""
        print(f"[GUI] State changed to: {state}")
        
        # Update UI based on state
        if state == AgentState.IDLE:
            self.status_panel.update_status("Ready")
            self.update_buttons(running=False)
        elif state == AgentState.RUNNING:
            self.status_panel.update_status("Running")
            self.update_buttons(running=True, idle=False)
        elif state == AgentState.PAUSED:
            self.status_panel.update_status("Paused")
            self.update_buttons(running=True, idle=True)
        elif state == AgentState.WAITING_FOR_USER:
            self.status_panel.update_status("Waiting for user input")
            self.update_buttons(running=True, idle=True)
            # Auto-focus query input
            self.query_entry.setFocus()
        elif state == AgentState.STOPPED:
            self.status_panel.update_status("Stopped")
            self.update_buttons(running=False)
        elif state == AgentState.FINISHED:
            self.status_panel.update_status("Completed")
            self.update_buttons(running=True, idle=True)
    
    @pyqtSlot(dict)
    def display_event(self, event):
        """Display an event from presenter (similar to original display_event)."""
        etype = event["type"]
        detail_level = self.agent_controls_panel.detail_combo.currentText()
        
        # Store conversation history if present
        print(f"[GUI] display_event: checking history, etype={etype}, has_history={'history' in event}")
        if "history" in event:
            self.last_history = event["history"]
        
        # Add detail level to event for rendering
        event_with_detail = event.copy()
        event_with_detail["_detail_level"] = detail_level
        
        # Add event to model for virtual scrolling
        self.event_model.add_event(event_with_detail)
        
        # Handle any UI interactions
        if etype == "user_interaction_requested":
            # Auto-focus the query input
            self.query_entry.setFocus()
        
        # Update token counts if present in event
        if "context_length" in event:
            self.context_length = event["context_length"]
            self.status_panel.update_context_length(self.context_length)
        
        # Support both naming conventions for token counts
        # Token counts are typically inside event["usage"] dict
        input_tokens = None
        output_tokens = None
        
        # First check usage dict
        usage = event.get("usage", {})
        if "total_input_tokens" in usage and "total_output_tokens" in usage:
            input_tokens = usage["total_input_tokens"]
            output_tokens = usage["total_output_tokens"]
        elif "total_input" in usage and "total_output" in usage:
            input_tokens = usage["total_input"]
            output_tokens = usage["total_output"]
        # For backward compatibility, also check top-level
        elif "total_input_tokens" in event and "total_output_tokens" in event:
            input_tokens = event["total_input_tokens"]
            output_tokens = event["total_output_tokens"]
        elif "total_input" in event and "total_output" in event:
            input_tokens = event["total_input"]
            output_tokens = event["total_output"]
        
        if input_tokens is not None and output_tokens is not None:
            self.total_input = input_tokens
            self.total_output = output_tokens
            self.status_panel.update_tokens(self.total_input, self.total_output)

        # Auto-scroll to bottom
        self._scroll_to_bottom()    
    @pyqtSlot(int, int)
    def on_tokens_updated(self, total_input, total_output):
        """Handle token count updates."""
        self.total_input = total_input
        self.total_output = total_output
        self.status_panel.update_tokens(total_input, total_output)

    @pyqtSlot(int)
    def on_context_updated(self, context_length):
        """Handle context token count updates."""
        self.context_length = context_length
        self.status_panel.update_context_length(context_length)

    @pyqtSlot(str)
    def on_status_message(self, message):
        """Handle status messages."""
        print(f"[GUI] Status: {message}")
        # Could update a status bar if we add one
    def _apply_filter(self):
        """Apply filter based on search text and event type selection."""
        filter_text = self.filter_lineedit.text()
        filter_type = self.filter_type_combo.currentText()
        self.filter_proxy_model.set_filter(filter_text, filter_type)
    
    @pyqtSlot(str, str)
    def on_error_occurred(self, error_message, traceback):
        """Handle errors from presenter."""
        QMessageBox.critical(self, "Agent Error", f"Error: {error_message}")
        if traceback:
            print(f"[GUI] Error traceback: {traceback}")
    
    @pyqtSlot(dict)
    def on_config_changed(self, config):
        """Handle configuration changes from presenter."""
        # Update UI controls if needed
        pass
    
    # ----- Agent Control Methods -----
    
    def run_agent(self):
        """Start or continue agent with current query."""
        query = self.query_entry.toPlainText().strip()
        if not query:
            QMessageBox.warning(self, "No Query", "Please enter a query first.")
            return
        
        # Get current configuration from controls
        config_dict = self.agent_controls_panel.get_config_dict()
        
        # Update presenter configuration
        self.presenter.update_config(config_dict)
        
        # Check current state to decide action
        current_state = self.presenter.state
        
        if current_state == AgentState.IDLE:
            # Start new session
            self.display_user_query(query)
            self.presenter.start_session(query, config_dict)
            self.query_entry.clear()
            
        elif current_state in [AgentState.PAUSED, AgentState.WAITING_FOR_USER]:
            # Continue existing session
            self.display_user_query(query)
            self.presenter.continue_session(query)
            self.query_entry.clear()
            
        else:
            QMessageBox.warning(self, "Cannot Run", 
                               f"Cannot run agent in current state: {current_state}")
    
    def stop_agent(self):
        """Stop the current agent session."""
        self.presenter.stop_session()
    
    def pause_agent(self):
        """Pause the current agent session."""
        self.presenter.pause_session()
    
    def restart_session(self):
        """Restart a fresh session with current GUI settings."""
        self.presenter.restart_session()
        
        # Clear event model (virtual scrolling)
        self.event_model.clear()
        
        # Reset token counters
        self.total_input = 0
        self.total_output = 0
        self.context_length = 0
        self.status_panel.update_tokens(0, 0)
        self.status_panel.update_context_length(0)
        
        # Update UI
        self.status_panel.update_status("Ready for new session")
        self.update_buttons(running=False)
    
    # ----- UI Helper Methods -----
    
    def update_buttons(self, running=None, idle=False):
        """Update button states based on agent state."""
        if running is None:
            running = self.presenter.state in [
                AgentState.RUNNING, 
                AgentState.PAUSED, 
                AgentState.WAITING_FOR_USER
            ]
            idle = self.presenter.state in [
                AgentState.PAUSED,
                AgentState.WAITING_FOR_USER,
                AgentState.FINISHED
            ]
        
        print(f"[GUI] update_buttons(running={running}, idle={idle}), state={self.presenter.state}")
        
        if running:
            if idle:
                self.run_btn.setEnabled(True)
                self.stop_btn.setEnabled(True)
                self.restart_btn.setEnabled(True)
                self.pause_btn.setEnabled(False)  # Already paused
                self.status_panel.update_status("Ready for next query")
            else:
                self.run_btn.setEnabled(False)
                self.stop_btn.setEnabled(True)
                self.restart_btn.setEnabled(False)
                self.pause_btn.setEnabled(True)
                self.status_panel.update_status("Running")
        else:
            self.run_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            self.restart_btn.setEnabled(self.last_history is not None)
            self.pause_btn.setEnabled(False)
            self.status_panel.update_status("Ready")
    
    def display_user_query(self, query):
        """Display a user query in the output area."""
        # Create a synthetic event for user query
        event = {
            "type": "user_query",
            "content": query,
            "_detail_level": self.agent_controls_panel.detail_combo.currentText()
        }
        self.event_model.add_event(event)
        self._scroll_to_bottom()
    
    def _create_result_widget(self, result_text, full_text):
        """
        Create a widget to display a tool result.
        If result_text is longer than MAX_RESULT_LENGTH, show a truncated version
        with a "Show full" button. Otherwise just show a label.
        """
        widget = QWidget()
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        widget.setLayout(layout)
        
        # Unescape HTML entities in the text
        unescaped_full_text = html.unescape(full_text)
        
        # Determine if truncation is needed
        if len(unescaped_full_text) > MAX_RESULT_LENGTH:
            truncated = unescaped_full_text[:MAX_RESULT_LENGTH] + "..."
            label = QLabel(f"Result: {truncated}")
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setStyleSheet("color: #006400;")
            layout.addWidget(label, 1)  # stretch factor 1
            
            button = QPushButton("Show full")
            button.setMaximumWidth(80)
            # Connect button to open a dialog with full text
            button.clicked.connect(lambda checked, text=unescaped_full_text: self._show_full_text_dialog(text))
            layout.addWidget(button)
        else:
            label = QLabel(f"Result: {unescaped_full_text}")
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setStyleSheet("color: #006400;")
            layout.addWidget(label)
        
        return widget
    
    def _show_full_text_dialog(self, text):
        """Open a modal dialog displaying the full text."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Full Tool Result")
        dialog.resize(600, 400)
        layout = QVBoxLayout(dialog)
        text_edit = QTextEdit()
        # Unescape any HTML entities in the text
        unescaped_text = html.unescape(text)
        text_edit.setPlainText(unescaped_text)
        text_edit.setReadOnly(True)
        layout.addWidget(text_edit)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dialog.accept)
        layout.addWidget(close_btn)
        dialog.exec()
    
    def _scroll_to_bottom(self):
        """Scroll to bottom only if auto-scroll is enabled (i.e., user hasn't scrolled away)."""
        if self._auto_scroll_enabled:
            QTimer.singleShot(0, self._do_scroll_to_bottom)
    
    def _do_scroll_to_bottom(self):
        """Programmatically scroll to bottom."""
        self._programmatic_scroll = True
        try:
            scrollbar = self.event_list_view.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())
        finally:
            self._programmatic_scroll = False
    
    def _on_scrollbar_value_changed(self, value):
        """Track if user has scrolled away from bottom."""
        # Ignore scroll position changes during programmatic scrolling
        if self._programmatic_scroll:
            return
        
        scrollbar = self.event_list_view.verticalScrollBar()
        max_val = scrollbar.maximum()
        # If user is within 10 pixels of bottom, consider them at bottom
        self._user_scrolled_away = value < max_val - 10
        # Auto-scroll enabled when user at bottom
        self._auto_scroll_enabled = not self._user_scrolled_away
    
    def _on_event_context_menu(self, position):
        """Show context menu for event list with copy option."""
        index = self.event_list_view.indexAt(position)
        if not index.isValid():
            return
        
        # Get the event data
        event = index.data(Qt.ItemDataRole.UserRole)
        if not event:
            return
        
        # Create context menu
        menu = QMenu()
        copy_action = menu.addAction("Copy")
        
        # Show menu and get selected action
        selected_action = menu.exec(self.event_list_view.viewport().mapToGlobal(position))
        
        if selected_action == copy_action:
            # Extract plain text from event using the delegate
            delegate = self.event_list_view.itemDelegate()
            if hasattr(delegate, '_event_to_plain_text'):
                plain_text = delegate._event_to_plain_text(event)
                if plain_text:
                    clipboard = QApplication.clipboard()
                    clipboard.setText(plain_text)
            else:
                # Fallback: just convert event dict to string
                import json
                plain_text = json.dumps(event, indent=2)
                clipboard = QApplication.clipboard()
                clipboard.setText(plain_text)
    
    # ----- Configuration Management -----
    
    def load_config(self):
        """Load configuration from file and update controls."""
        self._loading_config = True
        
        try:
            # Load config from service
            config = self.config_service.get_all()
            
            # Update controls
            self.agent_controls_panel.set_config_dict(config)
            
            # Update presenter configuration
            self.presenter.update_config(config)
            
            print("[GUI] Configuration loaded")
            
        except Exception as e:
            print(f"[GUI] Error loading config: {e}")
        finally:
            self._loading_config = False
    
    def save_config(self, immediate=False):
        """Save current configuration to file.
        
        Args:
            immediate: If True, save immediately; otherwise use debounced save
        """
        if self._loading_config:
            return
        
        try:
            config = self.agent_controls_panel.get_config_dict()
            print(f"[GUI] Saving config: {config} (immediate={immediate})")
            # Update config in service
            self.config_service.update(config, save=False)
            # Save with appropriate immediacy
            self.config_service.save(immediate=immediate)
            print("[GUI] Configuration saved to service")
        except Exception as e:
            print(f"[GUI] Error saving config: {e}")
    
    def _handle_config_change(self):
        """Handle configuration change from UI controls."""
        # Skip if we're loading config (to avoid duplicate updates)
        if self._loading_config:
            return
        
        # Get current config from controls panel
        config = self.agent_controls_panel.get_config_dict()
        # Update presenter config
        self.presenter.update_config(config)
        # Schedule save to ConfigService
        self._schedule_config_save()
        
    def _schedule_config_save(self):
        """Schedule a debounced configuration save."""
        if not self._loading_config:
            self._config_save_timer.start(1000)  # 1 second delay
    
    # ----- Workspace Methods -----
    
    def set_workspace(self):
        """Open dialog to select workspace directory."""
        current_workspace = self.agent_controls_panel.workspace_display.text()
        if current_workspace == "None (unrestricted)":
            start_dir = os.getcwd()
        else:
            start_dir = current_workspace
        
        new_workspace = QFileDialog.getExistingDirectory(self, "Select Workspace Directory", start_dir)
        if new_workspace:
            # Ensure path is normalized and absolute
            new_workspace = os.path.normpath(new_workspace)
            if not os.path.isabs(new_workspace):
                new_workspace = os.path.abspath(new_workspace)
            self.agent_controls_panel.workspace_display.setText(new_workspace)
            self._handle_config_change()
    
    def clear_workspace(self):
        """Clear workspace restriction."""
        self.agent_controls_panel.workspace_display.setText("None (unrestricted)")
        self._handle_config_change()
    
    # ----- Menu Bar -----
    
    def create_menu_bar(self):
        """Create the menu bar."""
        menu_bar = QMenuBar(self)
        self.setMenuBar(menu_bar)
        
        # File menu
        file_menu = menu_bar.addMenu("File")
        
        save_config_action = QAction("Save Configuration", self)
        save_config_action.triggered.connect(self.save_config)
        file_menu.addAction(save_config_action)
        
        load_config_action = QAction("Load Configuration", self)
        load_config_action.triggered.connect(self.load_config)
        file_menu.addAction(load_config_action)
        
        file_menu.addSeparator()
        
        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)
    
    def closeEvent(self, event):
        """Save configuration before closing the GUI."""
        # Save immediately on close to ensure config is persisted
        self.save_config(immediate=True)
        # Clean up presenter
        self.presenter.cleanup()
        super().closeEvent(event)


# ----- Main Function -----
def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    gui = AgentGUI()
    gui.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()