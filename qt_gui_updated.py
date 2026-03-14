# qt_gui.py (Step 3) - Updated with 6-line prompt and music player buttons
import sys
import os
import json
import html
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QTextEdit, QListWidget,
    QGroupBox, QCheckBox, QMenuBar, QMenu, QFileDialog,
    QMessageBox, QScrollArea, QFrame, QComboBox, QSpinBox, QDoubleSpinBox, QSplitter, QDialog, QSizePolicy
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QAction, QFont
from dotenv import load_dotenv

from agent_controller import AgentController
from agent_core import AgentConfig
from tools import TOOL_CLASSES, SIMPLIFIED_TOOL_CLASSES

load_dotenv()

MAX_RESULT_LENGTH = 500  # characters before truncation

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
    
    def add_content_line(self, text, style=""):
        """Add a simple text line (label)."""
        # Unescape any HTML entities in the text for PlainText format
        unescaped_text = html.unescape(text)
        label = QLabel(unescaped_text)
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        # Set size policy to allow vertical expansion for wrapped text
        label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        if style:
            label.setStyleSheet(style)
        self.content_layout.addWidget(label)

class AgentGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.controller = AgentController()
        self.total_input = 0
        self.total_output = 0
        self.context_length = 0
        self.last_history = None
        self.agent_idle = False
        self._cached_config = None  # Config created by restart_session for next run
        # Turn monitoring defaults
        self.turn_monitor_enabled = True
        self.turn_monitor_warning_threshold = 0.8
        self.turn_monitor_critical_threshold = 0.95
        # Smart scrolling tracking
        self._auto_scroll_enabled = True
        self._user_scrolled_away = False
        
        # Configuration auto-save timer
        self._config_save_timer = QTimer()
        self._config_save_timer.setSingleShot(True)
        self._config_save_timer.timeout.connect(self.save_config)
        self._loading_config = False  # Flag to prevent save during load

        self.init_ui()        
        self.setup_polling()
        self.load_config()
    
    def init_ui(self):
        self.setWindowTitle("Agent Workbench - QT")
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
        
        
        # Agent Controls Panel (replaces individual controls)
        self.agent_controls_panel = AgentControlsPanel(SIMPLIFIED_TOOL_CLASSES)
        right_layout.addWidget(self.agent_controls_panel)

        # Connect workspace buttons
        self.agent_controls_panel.set_workspace_btn.clicked.connect(self.set_workspace)
        self.agent_controls_panel.clear_workspace_btn.clicked.connect(self.clear_workspace)

        # Connect token monitor checkbox to update AgentGUI's internal state
        self.agent_controls_panel.token_monitor_checkbox.stateChanged.connect(
            lambda state: setattr(self, '_token_monitor_enabled', state == Qt.CheckState.Checked.value)
        )
        # Connect threshold spinboxes to update AgentGUI's internal thresholds
        self.agent_controls_panel.warning_threshold_spinbox.valueChanged.connect(
            lambda value: setattr(self, 'token_monitor_warning_threshold', value * 1000)
        )
        self.agent_controls_panel.critical_threshold_spinbox.valueChanged.connect(
            lambda value: setattr(self, 'token_monitor_critical_threshold', value * 1000)
        )
        # Connect all controls to auto-save configuration
        self.agent_controls_panel.temperature_spinbox.valueChanged.connect(self._schedule_config_save)
        self.agent_controls_panel.max_turns_spinbox.valueChanged.connect(self._schedule_config_save)
        self.agent_controls_panel.tool_output_limit_spinbox.valueChanged.connect(self._schedule_config_save)
        self.agent_controls_panel.model_combo.currentTextChanged.connect(self._schedule_config_save)
        self.agent_controls_panel.detail_combo.currentTextChanged.connect(self._schedule_config_save)
        self.agent_controls_panel.token_monitor_checkbox.stateChanged.connect(self._schedule_config_save)
        self.agent_controls_panel.warning_threshold_spinbox.valueChanged.connect(self._schedule_config_save)
        self.agent_controls_panel.critical_threshold_spinbox.valueChanged.connect(self._schedule_config_save)
        
        # Connect tool checkboxes
        for checkbox in self.agent_controls_panel.tool_checkboxes.values():
            checkbox.stateChanged.connect(self._schedule_config_save)
        
        # Set initial values
        self._token_monitor_enabled = self.agent_controls_panel.token_monitor_checkbox.isChecked()
        self.token_monitor_warning_threshold = self.agent_controls_panel.warning_threshold_spinbox.value() * 1000
        self.token_monitor_critical_threshold = self.agent_controls_panel.critical_threshold_spinbox.value() * 1000
        

        
        # Output area (scrollable container)
        self.output_scroll_area = QScrollArea()
        self.output_scroll_area.setWidgetResizable(True)
        self.output_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.output_scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        
        self.output_container = QWidget()
        # Set expanding size policy so container grows with content
        self.output_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.output_layout = QVBoxLayout(self.output_container)
        self.output_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.output_layout.setSpacing(5)
        
        self.output_scroll_area.setWidget(self.output_container)
        # Connect scrollbar to track user scrolling
        scrollbar = self.output_scroll_area.verticalScrollBar()
        scrollbar.valueChanged.connect(self._on_scrollbar_value_changed)
        right_layout.addWidget(self.output_scroll_area)
        # Query input - Updated: Label above, 6-line text edit
        query_label = QLabel("Query:")
        query_label.setStyleSheet("font-weight: bold;")
        right_layout.addWidget(query_label)

        self.query_entry = QTextEdit()
        self.query_entry.setMaximumHeight(120)  # Approx 6 lines
        self.query_entry.setPlaceholderText("Enter your query here...")
        right_layout.addWidget(self.query_entry)

        # Music player style buttons - Updated: smaller, with symbols
        btn_frame = QWidget()
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(5)  # Smaller spacing between buttons
        btn_frame.setLayout(btn_layout)

        # Run button (▶)
        self.run_btn = QPushButton("▶ Run")
        self.run_btn.setMaximumWidth(80)
        self.run_btn.setStyleSheet("""
            QPushButton {
                padding: 5px;
                font-size: 12px;
                background-color: #4CAF50;
                color: white;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
            QPushButton:disabled {
                background-color: #cccccc;
            }
        """)
        self.run_btn.clicked.connect(self.run_agent)
        btn_layout.addWidget(self.run_btn)

        # Pause button (⏸)
        self.stop_btn = QPushButton("⏸ Pause")
        self.stop_btn.setMaximumWidth(80)
        self.stop_btn.setStyleSheet("""
            QPushButton {
                padding: 5px;
                font-size: 14px;
                background-color: #FF9800;
                color: white;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #F57C00;
            }
            QPushButton:disabled {
                background-color: #cccccc;
            }
        """)
        self.stop_btn.clicked.connect(self.stop_agent)
        self.stop_btn.setEnabled(False)
        btn_layout.addWidget(self.stop_btn)

        btn_layout.addStretch()
        # New session button
        self.restart_btn = QPushButton("New Session")
        self.restart_btn.setMaximumWidth(80)
        self.restart_btn.setStyleSheet("""
            QPushButton {
                padding: 5px;
                font-size: 12px;
                background-color: #9C27B0;
                color: white;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #7B1FA2;
            }
            QPushButton:disabled {
                background-color: #cccccc;
            }
        """)
        self.restart_btn.clicked.connect(self.restart_session)
        self.restart_btn.setEnabled(False)
        btn_layout.addWidget(self.restart_btn)
        right_layout.addWidget(btn_frame)
        
        splitter.addWidget(right_container)
        splitter.setSizes([300, 200, 900])
        main_layout.addWidget(splitter)
        
        self.create_menu_bar()
    def load_config(self):
        """Load configuration from file."""
        self._loading_config = True
        try:
            config_path = "agent_config.json"
            if os.path.exists(config_path):
                try:
                    with open(config_path, 'r') as f:
                        config = json.load(f)
                    # Apply configuration to controls panel
                    self.agent_controls_panel.set_config_dict(config)
                    print(f"[GUI] Loaded configuration from {config_path}")
                except Exception as e:
                    print(f"[GUI] Error loading config: {e}")
            else:
                print(f"[GUI] No config file found at {config_path}, using defaults")
        finally:
            self._loading_config = False
    def _schedule_config_save(self):
        """Schedule a configuration save (debounced)."""
        if self._loading_config:
            return  # Don't save while loading config
        self._config_save_timer.start(1000)  # 1 second debounce
    
    def save_config(self):
        """Save current configuration to file."""
        config = self.agent_controls_panel.get_config_dict()
        config_path = "agent_config.json"
        try:
            with open(config_path, 'w') as f:
                json.dump(config, f, indent=2)
            print(f"[GUI] Saved configuration to {config_path}")
        except Exception as e:
            print(f"[GUI] Error saving config: {e}")
    
    def create_menu_bar(self):
        menubar = self.menuBar()
        file_menu = menubar.addMenu("File")
        open_api_action = QAction("Open API Key...", self)
        open_api_action.triggered.connect(self.open_api_key)
        file_menu.addAction(open_api_action)
        open_prompt_action = QAction("Open Prompt...", self)
        open_prompt_action.triggered.connect(self.open_prompt)
        file_menu.addAction(open_prompt_action)
        file_menu.addSeparator()
        save_session_action = QAction("Save Session...", self)
        save_session_action.triggered.connect(self.save_session)
        file_menu.addAction(save_session_action)
        load_session_action = QAction("Load Session...", self)
        load_session_action.triggered.connect(self.load_session)
        file_menu.addAction(load_session_action)
        file_menu.addSeparator()
        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)
    
    def setup_polling(self):
        self.timer = QTimer()
        self.timer.timeout.connect(self.poll)
        self.timer.start(100)
    
    # ---- Menu commands ----
    def open_api_key(self):
        api_key = os.getenv('DEEPSEEK_API_KEY', 'Not set')
        masked_key = f"{api_key[:5]}..." if api_key and len(api_key) > 5 else "Not set"
        QMessageBox.information(self, "API Key", f"Using key from .env: {masked_key}")
    
    def open_prompt(self):
        filename, _ = QFileDialog.getOpenFileName(self, "Open Prompt", "", "Text files (*.txt)")
        if filename:
            try:
                with open(filename, 'r') as f:
                    content = f.read()
                    self.query_entry.setText(content)
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to read file: {e}")
    def save_session(self):
        if self.last_history is None:
            QMessageBox.warning(self, "No history", "No conversation history to save.")
            return
        filename, _ = QFileDialog.getSaveFileName(self, "Save Session", "", "JSON files (*.json)")
        if not filename:
            return
        enabled_names = self.agent_controls_panel.get_enabled_tool_names()
        workspace_display = self.agent_controls_panel.workspace_display.text()
        workspace_path = None if workspace_display == "None (unrestricted)" else workspace_display
        data = {
            "history": self.last_history,
            "enabled_tools": enabled_names,
            "token_monitor_enabled": self.agent_controls_panel.token_monitor_checkbox.isChecked(),
            "warning_threshold": self.agent_controls_panel.warning_threshold_spinbox.value(),
            "critical_threshold": self.agent_controls_panel.critical_threshold_spinbox.value(),
            "turn_monitor_enabled": True,
            "turn_monitor_warning_threshold": 0.8,
            "turn_monitor_critical_threshold": 0.95,
            "workspace_path": workspace_path,
            "max_turns": self.agent_controls_panel.max_turns_spinbox.value(),
            "temperature": self.agent_controls_panel.temperature_spinbox.value(),
            "tool_output_token_limit": self.agent_controls_panel.tool_output_limit_spinbox.value(),
        }
        try:
            with open(filename, 'w') as f:
                json.dump(data, f, indent=2)
            QMessageBox.information(self, "Session Saved", f"Session saved to {filename}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save session: {e}")

    def load_session(self):
        filename, _ = QFileDialog.getOpenFileName(self, "Load Session", "", "JSON files (*.json)")
        if not filename:
            return
        try:
            with open(filename, 'r') as f:
                data = json.load(f)
            # Validate required keys
            if "history" not in data or "enabled_tools" not in data:
                raise ValueError("Invalid session file format.")
            self.last_history = data["history"]
            enabled_names = set(data["enabled_tools"])
            # Update tool checkboxes
            for tool_name, checkbox in self.agent_controls_panel.tool_checkboxes.items():
                checkbox.setChecked(tool_name in enabled_names)
            
            # Load token monitoring settings if available
            if "token_monitor_enabled" in data:
                self.agent_controls_panel.token_monitor_checkbox.setChecked(data["token_monitor_enabled"])
            if "warning_threshold" in data:
                warning_value = data["warning_threshold"]
                # Convert old session files (values in actual tokens) to display units
                if warning_value >= 1000:  # Old format: actual token count (e.g., 55000)
                    warning_value = max(1, warning_value // 1000)
                elif warning_value > 200:  # Out of range display value, clamp
                    warning_value = 200
                self.agent_controls_panel.warning_threshold_spinbox.setValue(warning_value)
            if "critical_threshold" in data:
                critical_value = data["critical_threshold"]
                if critical_value >= 1000:  # Old format
                    critical_value = max(1, critical_value // 1000)
                elif critical_value > 200:  # Out of range display value, clamp
                    critical_value = 200
                self.agent_controls_panel.critical_threshold_spinbox.setValue(critical_value)
            
            self.agent_controls_panel.update_token_monitor_controls()  # Update UI state
            self.agent_controls_panel._update_token_threshold_labels()  # Update formatted labels

            # Load turn monitoring settings if available
            if "turn_monitor_enabled" in data:
                self.turn_monitor_enabled = data["turn_monitor_enabled"]
            if "turn_monitor_warning_threshold" in data:
                self.turn_monitor_warning_threshold = data["turn_monitor_warning_threshold"]
            if "turn_monitor_critical_threshold" in data:
                self.turn_monitor_critical_threshold = data["turn_monitor_critical_threshold"]

            # Load workspace path if available            if "workspace_path" in data:
                workspace_path = data["workspace_path"]
                if workspace_path is None:
                    self.agent_controls_panel.workspace_display.setText("None (unrestricted)")
                else:
                    # Ensure path is normalized and absolute
                    workspace_path = os.path.normpath(workspace_path)
                    if not os.path.isabs(workspace_path):
                        workspace_path = os.path.abspath(workspace_path)
                    self.agent_controls_panel.workspace_display.setText(workspace_path)
            
            # Load temperature, max turns, and tool output limit if available
            if "temperature" in data:
                temperature_value = data["temperature"]
                # Clamp to spinbox range
                temperature_value = max(0.0, min(2.0, temperature_value))
                self.agent_controls_panel.temperature_spinbox.setValue(temperature_value)
            if "max_turns" in data:
                max_turns_value = data["max_turns"]
                # Clamp to spinbox range
                max_turns_value = max(1, min(1000, max_turns_value))
                self.agent_controls_panel.max_turns_spinbox.setValue(max_turns_value)
            if "tool_output_token_limit" in data:
                tool_limit_value = data["tool_output_token_limit"]
                # Clamp to spinbox range
                tool_limit_value = max(1000, min(100000, tool_limit_value))
                self.agent_controls_panel.tool_output_limit_spinbox.setValue(tool_limit_value)
            
            self.update_buttons(running=False)
            QMessageBox.information(self, "Session Loaded", f"Session loaded from {filename}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load session: {e}")
    
    # ---- Agent control ----
    def run_agent(self):
        print(f"[GUI] run_agent called, controller.is_running={self.controller.is_running}, agent_idle={self.agent_idle}")
        query = self.query_entry.toPlainText().strip()
        if not query:
            QMessageBox.warning(self, "No query", "Please enter a query.")
            return
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            QMessageBox.critical(self, "API Key missing", "Set DEEPSEEK_API_KEY in .env file")
            return

        enabled_names = self.agent_controls_panel.get_enabled_tool_names()
        tool_name_to_class = {cls.__name__: cls for cls in SIMPLIFIED_TOOL_CLASSES}
        enabled_classes = [tool_name_to_class[name] for name in enabled_names]

        # Extract workspace path
        workspace_display = self.agent_controls_panel.workspace_display.text()
        workspace_path = None if workspace_display == "None (unrestricted)" else workspace_display
        
        # Use cached config if available (created by restart_session)
        if self._cached_config is not None:
            # Start with cached config but update fields that may have changed
            base_config = self._cached_config
            # Create new config with current GUI values
            config = AgentConfig(
                api_key=api_key,
                model=base_config.model,
                max_turns=self.agent_controls_panel.max_turns_spinbox.value(),
                temperature=self.agent_controls_panel.temperature_spinbox.value(),
                tool_classes=enabled_classes,  # Use current tool selection
                max_history_turns=base_config.max_history_turns,
                keep_initial_query=base_config.keep_initial_query,
                keep_system_messages=base_config.keep_system_messages,
                initial_input_tokens=self.total_input if self.last_history is not None else 0,
                initial_output_tokens=self.total_output if self.last_history is not None else 0,
                token_monitor_enabled=self.agent_controls_panel.token_monitor_checkbox.isChecked(),
                token_monitor_warning_threshold=self.agent_controls_panel.warning_threshold_spinbox.value() * 1000,
                token_monitor_critical_threshold=self.agent_controls_panel.critical_threshold_spinbox.value() * 1000,
                turn_monitor_enabled=self.turn_monitor_enabled,
                turn_monitor_warning_threshold=self.turn_monitor_warning_threshold,
                turn_monitor_critical_threshold=self.turn_monitor_critical_threshold,
                workspace_path=workspace_path,
                tool_output_token_limit=self.agent_controls_panel.tool_output_limit_spinbox.value()
            )
            print(f"[GUI] Using cached config with updated token monitoring: enabled={config.token_monitor_enabled}, warning={config.token_monitor_warning_threshold}, critical={config.token_monitor_critical_threshold}")
            self._cached_config = None  # Clear after use
        else:
            # Create fresh config
            config = AgentConfig(
                api_key=api_key,
                model="deepseek-reasoner",
                max_turns=self.agent_controls_panel.max_turns_spinbox.value(),
                temperature=self.agent_controls_panel.temperature_spinbox.value(),
                tool_classes=enabled_classes,
                max_history_turns=None,  # Pruning removed
                keep_initial_query=True,  # Pruning removed
                keep_system_messages=True,
                initial_input_tokens=self.total_input if self.last_history is not None else 0,
                initial_output_tokens=self.total_output if self.last_history is not None else 0,
                token_monitor_enabled=self.agent_controls_panel.token_monitor_checkbox.isChecked(),
                token_monitor_warning_threshold=self.agent_controls_panel.warning_threshold_spinbox.value() * 1000,
                token_monitor_critical_threshold=self.agent_controls_panel.critical_threshold_spinbox.value() * 1000,
                turn_monitor_enabled=self.turn_monitor_enabled,
                turn_monitor_warning_threshold=self.turn_monitor_warning_threshold,
                turn_monitor_critical_threshold=self.turn_monitor_critical_threshold,
                workspace_path=workspace_path,
                tool_output_token_limit=self.agent_controls_panel.tool_output_limit_spinbox.value()
            )

        try:
            if self.controller.is_running:
                if self.agent_idle:
                    # Agent is idle (paused), check if config has changed
                    current_config = self.controller.get_config()
                    # Compare configs - check if token monitoring settings, turn monitoring settings, or tool classes changed
                    config_changed = False
                    # Compare token monitoring settings
                    if current_config.token_monitor_enabled != config.token_monitor_enabled:
                        config_changed = True
                    elif current_config.token_monitor_warning_threshold != config.token_monitor_warning_threshold:
                        config_changed = True
                    elif current_config.token_monitor_critical_threshold != config.token_monitor_critical_threshold:
                        config_changed = True
                    # Compare turn monitoring settings
                    if current_config.turn_monitor_enabled != config.turn_monitor_enabled:
                        config_changed = True
                    elif current_config.turn_monitor_warning_threshold != config.turn_monitor_warning_threshold:
                        config_changed = True
                    elif current_config.turn_monitor_critical_threshold != config.turn_monitor_critical_threshold:
                        config_changed = True
                    # Compare tool classes (by name)
                    current_tool_names = {cls.__name__ for cls in (current_config.tool_classes or [])}
                    new_tool_names = {cls.__name__ for cls in (config.tool_classes or [])}
                    if current_tool_names != new_tool_names:
                        config_changed = True
                    
                    if config_changed:
                        # Config changed, need to restart agent with new config
                        print(f"[GUI] Config changed, restarting agent with new config")
                        # Stop current agent
                        self.controller.stop()
                        # Wait for thread to finish
                        while self.controller.is_running:
                            import time
                            time.sleep(0.01)
                        # Start new session with new config
                        if self.last_history is not None:
                            self.display_user_query(query)
                            self.controller.start(query, config, initial_conversation=self.last_history.copy())
                        else:
                            self.display_user_query(query)
                            self.controller.start(query, config)
                            # Reset token totals only for new session without history
                            self.total_input = 0
                            self.total_output = 0
                            self.context_length = 0
                            self.status_panel.update_context_length(self.context_length)
                            self.status_panel.update_tokens(self.total_input, self.total_output)
                    else:
                        # Config unchanged, just submit new query
                        self.display_user_query(query)
                        self.controller.continue_session(query)
                    self.agent_idle = False
                else:
                    # Agent is still processing previous query (should not happen)
                    QMessageBox.warning(self, "Agent busy", "Agent is still processing previous query.")
                    return
            else:
                # Start new session
                if self.last_history is not None:
                    # Continue existing session (with history)
                    self.display_user_query(query)
                    self.controller.start(query, config, initial_conversation=self.last_history.copy())
                else:
                    # Start new session
                    self.display_user_query(query)
                    self.controller.start(query, config)
                    # Reset token totals only for new session
                    self.total_input = 0
                    self.total_output = 0
                    self.context_length = 0
                    self.status_panel.update_context_length(self.context_length)
                    self.status_panel.update_tokens(self.total_input, self.total_output)
        except RuntimeError as e:
            QMessageBox.critical(self, "Error", str(e))
            return

        self.update_buttons(running=True, idle=False)
        self.status_panel.update_status("Running")
    def restart_session(self):
        """Start a fresh session with current GUI settings (including token monitoring)."""
        # Reset controller to clean state, clearing all queues and events
        self.controller.reset()
        
        # Clear history and token totals
        self.last_history = None
        self.total_input = 0
        self.total_output = 0
        self.context_length = 0
        self.status_panel.update_context_length(self.context_length)
        self.status_panel.update_tokens(self.total_input, self.total_output)
        # Force save any pending configuration changes
        self.save_config()
        # Ensure any pending debounced saves are executed immediately
        if self._config_save_timer.isActive():
            self._config_save_timer.stop()
            self._save_current_config_to_file()

        # Update buttons - agent is not running
        self.update_buttons(running=False)
        self.agent_idle = False
        
        # Create new AgentConfig with current GUI settings for next run
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if api_key:
            enabled_names = self.agent_controls_panel.get_enabled_tool_names()
            tool_name_to_class = {cls.__name__: cls for cls in SIMPLIFIED_TOOL_CLASSES}
            enabled_classes = [tool_name_to_class[name] for name in enabled_names]
            
            # Extract workspace path
            workspace_display = self.agent_controls_panel.workspace_display.text()
            workspace_path = None if workspace_display == "None (unrestricted)" else workspace_display
            # Store config for next run (similar to run_agent())
            self._cached_config = AgentConfig(
                api_key=api_key,
                model="deepseek-reasoner",
                max_turns=self.agent_controls_panel.max_turns_spinbox.value(),
                temperature=self.agent_controls_panel.temperature_spinbox.value(),
                tool_classes=enabled_classes,
                max_history_turns=None,  # Pruning removed
                keep_initial_query=True,  # Pruning removed
                keep_system_messages=True,
                initial_input_tokens=0,
                initial_output_tokens=0,
                token_monitor_enabled=self.agent_controls_panel.token_monitor_checkbox.isChecked(),
                token_monitor_warning_threshold=self.agent_controls_panel.warning_threshold_spinbox.value() * 1000,
                token_monitor_critical_threshold=self.agent_controls_panel.critical_threshold_spinbox.value() * 1000,
                workspace_path=workspace_path,
                tool_output_token_limit=self.agent_controls_panel.tool_output_limit_spinbox.value()
            )
            print(f"[GUI] Created new config for next session with token monitoring: enabled={self.agent_controls_panel.token_monitor_checkbox.isChecked()}, warning={self.agent_controls_panel.warning_threshold_spinbox.value() * 1000}, critical={self.agent_controls_panel.critical_threshold_spinbox.value() * 1000}")
        else:
            self._cached_config = None    
    def stop_agent(self):
        self.controller.request_pause()
        self.status_panel.update_status("Pausing...")
    
    
    def update_buttons(self, running, idle=False):
        print(f"[GUI] update_buttons(running={running}, idle={idle}), controller.is_running={self.controller.is_running}, agent_idle={self.agent_idle}")
        if running:
            if idle:
                self.run_btn.setEnabled(True)
                self.stop_btn.setEnabled(True)
                self.restart_btn.setEnabled(True)
                self.status_panel.update_status("Ready for next query")
            else:
                self.run_btn.setEnabled(False)
                self.stop_btn.setEnabled(True)
                self.restart_btn.setEnabled(False)
        else:
            self.run_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            self.restart_btn.setEnabled(self.last_history is not None)
            self.status_panel.update_status("Ready")
    
    def poll(self):
        event = self.controller.get_event()
        if event:
            print(f"[GUI] Received event: {event['type']}")
            self.display_event(event)
            if event["type"] == "thread_finished":
                self.update_buttons(running=False)
        else:
            # Debug: print every 10th poll
            if hasattr(self, '_poll_count'):
                self._poll_count += 1
            else:
                self._poll_count = 1
            if self._poll_count % 100 == 0:
                print(f"[GUI] Poll count: {self._poll_count}")
    
    # ---- Helper for result widget ----
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

    def display_user_query(self, query):
        """Display a user query in the output area."""
        frame = EventFrame("USER", "user_query")
        frame.add_content_line(f"Query: {query}", style="color: #006400; font-weight: bold;")
        self.output_layout.addWidget(frame)
        self.output_container.updateGeometry()
        self._scroll_to_bottom()
        
    def display_event(self, event):
        etype = event["type"]
        detail_level = self.agent_controls_panel.detail_combo.currentText()
        # Store conversation history if present
        print(f"[GUI] display_event: checking history, etype={etype}, has_history={'history' in event}")
        if "history" in event:
            self.last_history = event["history"]

        frame = EventFrame(etype.upper(), etype)

        if etype == "turn":
            turn = event.get("turn", "?")
            frame.add_content_line(f"Turn {turn}", style="font-weight: bold;")

            # Show assistant's natural language content (if any)
            assistant_content = event.get("assistant_content", "")
            if assistant_content and detail_level != "minimal":
                frame.add_content_line(f"Assistant: {assistant_content}", style="color: #000000;")

            # Show reasoning
            if detail_level != "minimal" and "reasoning" in event and event["reasoning"]:
                frame.add_content_line(f"Reasoning: {event['reasoning']}", style="color: #666666;")

            # Show tool calls
            for tc in event.get("tool_calls", []):
                if detail_level == "minimal":
                    frame.add_content_line(f"🛠️ {tc['name']}", style="color: #0000FF;")
                else:
                    frame.add_content_line(f"🛠️ {tc['name']}", style="color: #0000FF; font-weight: bold;")
                    if detail_level == "verbose":
                        frame.add_content_line(f"  Arguments: {tc['arguments']}", style="color: #0000AA;")

                    result_widget = self._create_result_widget(tc['result'], tc['result'])
                    frame.content_layout.addWidget(result_widget)

            # Token usage
            usage = event.get("usage", {})
            self.total_input = usage.get("total_input", self.total_input)
            self.total_output = usage.get("total_output", self.total_output)
            self.context_length = usage.get("input", self.context_length)
            self.status_panel.update_context_length(self.context_length)
            self.status_panel.update_tokens(self.total_input, self.total_output)
            self.agent_idle = False
            self.update_buttons(running=True, idle=False)

        elif etype == "final":
            frame.add_content_line(f"Final answer: {event['content']}", style="font-weight: bold; color: #000080;")
            if detail_level != "minimal" and "reasoning" in event and event["reasoning"]:
                frame.add_content_line(f"Reasoning: {event['reasoning']}", style="color: #666666;")
            usage = event.get("usage", {})
            self.total_input = usage.get("total_input", self.total_input)
            self.total_output = usage.get("total_output", self.total_output)
            self.context_length = usage.get("input", self.context_length)
            self.status_panel.update_context_length(self.context_length)
            self.status_panel.update_tokens(self.total_input, self.total_output)
            self.agent_idle = True
            self.update_buttons(running=True, idle=True)

        elif etype == "stopped":
            frame.add_content_line("Agent stopped by user.", style="color: #FF8C00;")
            usage = event.get("usage", {})
            self.total_input = usage.get("total_input", self.total_input)
            self.total_output = usage.get("total_output", self.total_output)
            self.context_length = usage.get("input", self.context_length)
            self.status_panel.update_tokens(self.total_input, self.total_output)
            self.status_panel.update_context_length(self.context_length)
            self.agent_idle = False  # Agent is not running, not idle
            self.update_buttons(running=False)
        elif etype == "user_interaction_requested":
            frame.add_content_line(f"Agent requests interaction: {event.get('message', '')}", style="color: #008080;")
            # Optionally auto-focus the query input
            self.query_entry.setFocus()
            usage = event.get("usage", {})
            self.total_input = usage.get("total_input", self.total_input)
            self.total_output = usage.get("total_output", self.total_output)
            self.context_length = usage.get("input", self.context_length)
            self.status_panel.update_context_length(self.context_length)
            self.status_panel.update_tokens(self.total_input, self.total_output)
            self.agent_idle = True
        elif etype == "token_warning":
            print(f"[GUI] token_warning event received, has_history={'history' in event}, agent_idle={self.agent_idle}, controller.is_running={self.controller.is_running}")
            print(f"[GUI] token_warning event, current agent_idle={self.agent_idle}")
            if self.agent_idle and self.controller.is_running:
                print("[GUI] WARNING: token_warning but agent_idle=True! This indicates a bug.")
            frame.add_content_line(event["message"], style="color: #FFA500; font-weight: bold;")
            usage = event.get("usage", {})
            self.total_input = usage.get("total_input", self.total_input)
            self.total_output = usage.get("total_output", self.total_output)
            # Update context length with token_count from warning
            token_count = event.get("token_count", 0)
            if token_count > 0:
                self.context_length = token_count
                self.status_panel.update_context_length(self.context_length)
            self.status_panel.update_tokens(self.total_input, self.total_output)
            # Note: agent_idle NOT set here - token warning is informational, agent continues processing
            # However, ensure agent is marked as not idle if it's running
            if self.controller.is_running:
                self.agent_idle = False
            # Update buttons to reflect current state
            self.update_buttons(running=self.controller.is_running, idle=self.agent_idle)
        elif etype == "turn_warning":
            print(f"[GUI] turn_warning event received, agent_idle={self.agent_idle}, controller.is_running={self.controller.is_running}")
            frame.add_content_line(event["message"], style="color: #FFA500; font-weight: bold;")
            usage = event.get("usage", {})
            self.total_input = usage.get("total_input", self.total_input)
            self.total_output = usage.get("total_output", self.total_output)
            # Note: agent_idle NOT set here - turn warning is informational, agent continues processing
            # However, ensure agent is marked as not idle if it's running
            if self.controller.is_running:
                self.agent_idle = False
            # Update buttons to reflect current state
            self.update_buttons(running=self.controller.is_running, idle=self.agent_idle)
        elif etype == "paused":
            frame.add_content_line("Agent paused, ready for next query.", style="color: #808080;")
            self.agent_idle = True
            self.update_buttons(running=True, idle=True)
        elif etype == "max_turns":
            frame.add_content_line("Max turns reached without final answer.", style="color: #FF8C00;")
            usage = event.get("usage", {})
            self.total_input = usage.get("total_input", self.total_input)
            self.total_output = usage.get("total_output", self.total_output)
            self.context_length = usage.get("input", self.context_length)
            self.status_panel.update_tokens(self.total_input, self.total_output)
            self.status_panel.update_context_length(self.context_length)
            self.agent_idle = False  # Agent is terminating, not idle
            self.update_buttons(running=False)
        elif etype == "error":
            frame.add_content_line(f"ERROR: {event.get('message')}", style="color: #FF0000; font-weight: bold;")
            if "traceback" in event and detail_level == "verbose":
                frame.add_content_line(event['traceback'], style="color: #FF0000;")
            # Agent has errored, thread will finish soon
            self.agent_idle = False  # Agent is terminating, not idle
            self.update_buttons(running=False)
        elif etype == "thread_finished":
            frame.add_content_line("Background thread finished.", style="color: #808080;")
        else:
            frame.add_content_line(str(event))

        self.output_layout.addWidget(frame)
        self.output_container.updateGeometry()
        self._scroll_to_bottom()

    def _scroll_to_bottom(self):
        """Scroll to bottom only if auto-scroll is enabled (i.e., user hasn't scrolled away)."""
        if self._auto_scroll_enabled:
            QTimer.singleShot(0, self._do_scroll_to_bottom)

    def _do_scroll_to_bottom(self):
        scrollbar = self.output_scroll_area.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _on_scrollbar_value_changed(self, value):
        """Track if user has scrolled away from bottom."""
        scrollbar = self.output_scroll_area.verticalScrollBar()
        max_val = scrollbar.maximum()
        # If user is within 10 pixels of bottom, consider them at bottom
        self._user_scrolled_away = value < max_val - 10
        # Auto-scroll enabled when user at bottom
        self._auto_scroll_enabled = not self._user_scrolled_away

    # ---- Workspace methods ----
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
            self._schedule_config_save()
            
    def clear_workspace(self):
        """Clear workspace restriction."""
        self.agent_controls_panel.workspace_display.setText("None (unrestricted)")
        self._schedule_config_save()
    def closeEvent(self, event):
        """Save configuration before closing the GUI."""
        self.save_config()
        super().closeEvent(event)

def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    gui = AgentGUI()
    gui.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
