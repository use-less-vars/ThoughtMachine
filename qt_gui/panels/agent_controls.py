"""Agent Controls Panel - Collapsible panel for agent configuration."""
import os
import yaml
from PyQt6.QtWidgets import (
    QGroupBox, QVBoxLayout, QHBoxLayout, QGridLayout, QWidget, QLabel,
    QPushButton, QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox,
    QCheckBox, QScrollArea, QStyle, QSizePolicy
)
from PyQt6.QtCore import Qt, QTimer

# Import from other extracted modules
from .mcp_config import MCPConfigDialog
from .markdown_renderer import MarkdownRenderer
from ..utils.constants import MAX_RESULT_LENGTH
from ..debug_log import debug_log



class AgentControlsPanel(QGroupBox):
    """Collapsible panel for agent controls."""

    def __init__(self, tool_classes, config_file=None):
        super().__init__("Agent Controls")
        self.tool_classes = tool_classes
        self.tool_checkboxes = {}  # name -> QCheckBox
        self.is_collapsed = True
        self.config_file = config_file  # Store config file path

        # Provider type mapping: GUI display -> internal type
        self._provider_mapping = {
            "OpenAI (compatible)": "openai_compatible",
            "Anthropic": "anthropic",
            "OpenAI": "openai"
        }

        # Conversation pruning settings
        self.max_history_turns = None
        self.keep_initial_query = True
        self.keep_system_messages = True

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
        self.controls_layout = QHBoxLayout()  # Changed from QGridLayout to QHBoxLayout
        self.controls_layout.setSpacing(10)

        # Create two columns for better use of horizontal space
        self.left_column = QVBoxLayout()
        self.left_column.setSpacing(10)
        self.right_column = QVBoxLayout()
        self.right_column.setSpacing(10)

        self.controls_layout.addLayout(self.left_column)
        self.controls_layout.addLayout(self.right_column)

        self.controls_container.setLayout(self.controls_layout)


        # Row 0: Preset selection
        preset_row = QWidget()
        preset_layout = QHBoxLayout()
        preset_row.setLayout(preset_layout)

        preset_layout.addWidget(QLabel("Preset:"))
        self.preset_combo = QComboBox()
        self.preset_combo.setEditable(False)
        self._load_presets()
        self.preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        preset_layout.addWidget(self.preset_combo)
        preset_layout.addStretch()

        self.left_column.addWidget(preset_row)

        # Row 1: Workspace controls
        workspace_row = QWidget()
        workspace_layout = QHBoxLayout()
        workspace_row.setLayout(workspace_layout)

        workspace_layout.addWidget(QLabel("Workspace:"))
        self.workspace_display = QLabel("None (unrestricted)")
        self.workspace_display.setStyleSheet("color: blue;")
        self.workspace_display.setWordWrap(True)
        workspace_layout.addWidget(self.workspace_display)

        self.set_workspace_btn = QPushButton("Set")
        self.set_workspace_btn.setMaximumWidth(60)
        workspace_layout.addWidget(self.set_workspace_btn)

        self.clear_workspace_btn = QPushButton("Clear")
        self.clear_workspace_btn.setMaximumWidth(60)
        workspace_layout.addWidget(self.clear_workspace_btn)

        # Add workspace row to left column
        self.left_column.addWidget(workspace_row)

        # Row 1: Token monitoring controls

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

        # Add token monitor row to left column
        self.left_column.addWidget(token_monitor_row)

        # Row 2: Max turns control

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

        # Add max turns row to left column
        self.left_column.addWidget(max_turns_row)

        # Row 3: Turn monitoring controls

        turn_monitor_row = QWidget()
        turn_monitor_layout = QHBoxLayout()
        turn_monitor_row.setLayout(turn_monitor_layout)
        turn_monitor_layout.setSpacing(5)

        self.turn_monitor_checkbox = QCheckBox("Turn warnings")
        self.turn_monitor_checkbox.setChecked(True)
        turn_monitor_layout.addWidget(self.turn_monitor_checkbox)

        turn_monitor_layout.addWidget(QLabel("Warning:"))
        self.turn_warning_threshold_spinbox = QDoubleSpinBox()
        self.turn_warning_threshold_spinbox.setRange(0.0, 1.0)
        self.turn_warning_threshold_spinbox.setValue(0.8)
        self.turn_warning_threshold_spinbox.setSingleStep(0.05)
        self.turn_warning_threshold_spinbox.setDecimals(2)
        turn_monitor_layout.addWidget(self.turn_warning_threshold_spinbox)
        self.turn_warning_formatted_label = QLabel("(80)")
        turn_monitor_layout.addWidget(self.turn_warning_formatted_label)
        turn_monitor_layout.addWidget(QLabel("turns"))

        turn_monitor_layout.addWidget(QLabel("Critical:"))
        self.turn_critical_threshold_spinbox = QDoubleSpinBox()
        self.turn_critical_threshold_spinbox.setRange(0.0, 1.0)
        self.turn_critical_threshold_spinbox.setValue(0.95)
        self.turn_critical_threshold_spinbox.setSingleStep(0.05)
        self.turn_critical_threshold_spinbox.setDecimals(2)
        turn_monitor_layout.addWidget(self.turn_critical_threshold_spinbox)
        self.turn_critical_formatted_label = QLabel("(95)")
        turn_monitor_layout.addWidget(self.turn_critical_formatted_label)
        turn_monitor_layout.addWidget(QLabel("turns"))

        # Add turn monitor row to left column
        self.left_column.addWidget(turn_monitor_row)

        # Row 4: Temperature control
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

        # Add temperature row to left column
        self.left_column.addWidget(temperature_row)

        # Row 4: Provider selection

        provider_row = QWidget()
        provider_layout = QHBoxLayout()
        provider_row.setLayout(provider_layout)
        provider_layout.setSpacing(5)

        provider_layout.addWidget(QLabel("Provider:"))
        self.provider_combo = QComboBox()
        self.provider_combo.addItems(["OpenAI (compatible)", "Anthropic", "OpenAI"])
        self.provider_combo.setCurrentText("OpenAI (compatible)")
        provider_layout.addWidget(self.provider_combo)
        provider_layout.addStretch()

        # Add provider row to left column
        self.left_column.addWidget(provider_row)

        # Row 6: API Key (optional)

        api_key_row = QWidget()
        api_key_layout = QHBoxLayout()
        api_key_row.setLayout(api_key_layout)
        api_key_layout.setSpacing(5)

        api_key_layout.addWidget(QLabel("API Key:"))
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setPlaceholderText("Leave empty to use environment variable")
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        api_key_layout.addWidget(self.api_key_edit)
        api_key_layout.addStretch()

        # Add API key row to right column
        self.right_column.addWidget(api_key_row)

        # Row 7: Base URL (optional)

        base_url_row = QWidget()
        base_url_layout = QHBoxLayout()
        base_url_row.setLayout(base_url_layout)
        base_url_layout.setSpacing(5)

        base_url_layout.addWidget(QLabel("Base URL:"))
        self.base_url_edit = QLineEdit()
        self.base_url_edit.setPlaceholderText("Leave empty for default")
        base_url_layout.addWidget(self.base_url_edit)
        base_url_layout.addStretch()

        # Add base URL row to right column
        self.right_column.addWidget(base_url_row)

        # Row 8: Model selection

        model_row = QWidget()
        model_layout = QHBoxLayout()
        model_row.setLayout(model_layout)
        model_layout.setSpacing(5)

        model_layout.addWidget(QLabel("Model:"))
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.addItems(["deepseek-reasoner", "gpt-4", "claude-3", "llama-3", "big pickle", "gpt-3.5-turbo", "claude-3-haiku", "claude-3-sonnet", "claude-3-opus"])
        self.model_combo.setCurrentText("deepseek-reasoner")
        model_layout.addWidget(self.model_combo)
        model_layout.addStretch()

        # Add model row to right column
        self.right_column.addWidget(model_row)

        # Row 9: Tool output token limit

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

        # Add tool limit row to right column
        self.right_column.addWidget(tool_limit_row)

        # Row 10: Detail combo

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

        # Add detail row to right column
        self.right_column.addWidget(detail_row)

        # Row 11: Tool loader (as a sub-group)

        tool_group = QGroupBox("Tools")
        tool_layout = QGridLayout()
        tool_group.setLayout(tool_layout)
        tool_group.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.MinimumExpanding)

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
        tool_scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        tool_scroll_area.setMaximumHeight(400)  # Limit height, show scrollbar if needed
        tool_scroll_area.setWidget(tool_group)
        self.tool_group = tool_group
        self.tool_scroll_area = tool_scroll_area

        # Add tool loader to right column

        # MCP Configuration button
        mcp_config_row = QWidget()
        mcp_config_layout = QHBoxLayout()
        mcp_config_row.setLayout(mcp_config_layout)
        self.mcp_config_btn = QPushButton("MCP Config")
        self.mcp_config_btn.setMaximumWidth(120)
        mcp_config_layout.addWidget(self.mcp_config_btn)
        mcp_config_layout.addStretch()
        self.right_column.addWidget(mcp_config_row)
        self.right_column.addWidget(tool_scroll_area)

        # Add stretches to push content to top in both columns
        self.left_column.addStretch()
        self.right_column.addStretch()

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

        # Turn monitoring debounce timers
        self._turn_warning_threshold_timer = QTimer()
        self._turn_warning_threshold_timer.setSingleShot(True)
        self._turn_warning_threshold_timer.timeout.connect(self._adjust_turn_warning_threshold)
        self._turn_critical_threshold_timer = QTimer()
        self._turn_critical_threshold_timer.setSingleShot(True)
        self._turn_critical_threshold_timer.timeout.connect(self._adjust_turn_critical_threshold)

        # Connect signals
        self.warning_threshold_spinbox.valueChanged.connect(self._on_warning_threshold_changed)
        self.critical_threshold_spinbox.valueChanged.connect(self._on_critical_threshold_changed)
        self.token_monitor_checkbox.stateChanged.connect(self.update_token_monitor_controls)
        # Turn monitoring connections
        self.turn_monitor_checkbox.stateChanged.connect(self.update_turn_monitor_controls)
        self.turn_warning_threshold_spinbox.valueChanged.connect(self._on_turn_warning_threshold_changed)
        self.turn_critical_threshold_spinbox.valueChanged.connect(self._on_turn_critical_threshold_changed)
        self.mcp_config_btn.clicked.connect(self._open_mcp_config)

        # Initial updates        self.update_token_monitor_controls()
        self._update_token_threshold_labels()
        self.update_turn_monitor_controls()
        self._update_turn_threshold_labels()


    def _load_presets(self):
        """Load available presets from the presets directory into the combo box."""
        presets_dir = "presets"
        self.preset_combo.clear()
        # Add a "None" option to indicate no preset
        self.preset_combo.addItem("None", None)

        if os.path.isdir(presets_dir):
            for filename in sorted(os.listdir(presets_dir)):
                if filename.endswith(".yaml") or filename.endswith(".yml"):
                    filepath = os.path.join(presets_dir, filename)
                    try:
                        with open(filepath, 'r') as f:
                            data = yaml.safe_load(f)
                            name = data.get('name', filename)
                            # Store the filepath as user data
                            self.preset_combo.addItem(name, filepath)
                    except Exception as e:
                        debug_log(f"Error loading preset {filepath}: {e}", level="ERROR")
        else:
            debug_log(f"Presets directory '{presets_dir}' not found.", level="WARNING")

    def _on_preset_changed(self, index):
        """Handle preset selection change.

        Load the selected preset and update UI controls to reflect its values.
        """
        # Get the filepath stored as user data
        filepath = self.preset_combo.itemData(index)
        if not filepath:
            # "None" selected - no action needed
            return

        try:
            with open(filepath, 'r') as f:
                preset_data = yaml.safe_load(f)
        except Exception as e:
            debug_log(f"Error loading preset {filepath}: {e}", level="ERROR")
            return

        # Apply preset values to controls
        self._apply_preset_values(preset_data)

    def _apply_preset_values(self, preset_data):
        """Apply preset dictionary values to UI controls.

        This updates the controls to show the preset's configuration.
        Individual controls can still be adjusted by the user after selection.
        """
        # Model
        if 'model' in preset_data:
            self.model_combo.setEditText(preset_data['model'])

        # Temperature
        if 'temperature' in preset_data:
            self.temperature_spinbox.setValue(preset_data['temperature'])

        # Tools list - update tool checkboxes
        if 'tools' in preset_data:
            enabled_tools = set(preset_data['tools'])
            for name, checkbox in self.tool_checkboxes.items():
                checkbox.setChecked(name in enabled_tools)

    def _open_mcp_config(self):
        """
        Open MCP configuration dialog.

        This dialog allows the user to configure MCP server connections.
        Changes to the configuration will trigger a tool refresh.
        """
        dialog = MCPConfigDialog(self)
        dialog.exec()
        # After dialog closes, trigger tool refresh via callback
        if hasattr(self, 'on_mcp_config_changed') and callable(self.on_mcp_config_changed):
            self.on_mcp_config_changed()

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

    def _rebuild_tool_checkboxes(self):
        """Rebuild tool checkboxes from the current tool_classes list.

        This method clears existing checkboxes and recreates them based on
        self.tool_classes. Called when MCP configuration changes.
        """
        # Clear existing checkboxes
        for checkbox in self.tool_checkboxes.values():
            checkbox.setParent(None)
            checkbox.deleteLater()
        self.tool_checkboxes.clear()

        # Recreate checkboxes in the tool group
        tool_layout = self.tool_group.layout()
        # Clear existing widgets from layout
        while tool_layout.count():
            item = tool_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        # Recreate checkboxes
        col = 0
        tool_row = 0
        for i, cls in enumerate(self.tool_classes):
            checkbox = QCheckBox(cls.__name__)
            checkbox.setToolTip(cls.__doc__ or "No documentation available")
            checkbox.setChecked(True)  # Default enabled
            tool_layout.addWidget(checkbox, tool_row, col)
            self.tool_checkboxes[cls.__name__] = checkbox
            col += 1
            if col >= 2:
                col = 0
                tool_row += 1
        # If odd number of tools, add a spacer in the second column
        if col == 1:
            tool_layout.addWidget(QWidget(), tool_row, col)
        # Add stretch to fill remaining space
        tool_layout.setRowStretch(tool_row + 1, 1)

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

    def update_turn_monitor_controls(self):
        """Enable/disable turn monitor threshold controls based on checkbox."""
        enabled = self.turn_monitor_checkbox.isChecked()
        self.turn_warning_threshold_spinbox.setEnabled(enabled)
        self.turn_critical_threshold_spinbox.setEnabled(enabled)

    def _on_turn_warning_threshold_changed(self, value):
        """Start debounced adjustment of turn warning threshold."""
        self._turn_warning_threshold_timer.start(500)

    def _adjust_turn_warning_threshold(self):
        """Ensure turn warning threshold is always lower than critical threshold."""
        value = self.turn_warning_threshold_spinbox.value()
        critical = self.turn_critical_threshold_spinbox.value()
        step = self.turn_warning_threshold_spinbox.singleStep()
        if value >= critical:
            # Clamp warning to critical - step (instead of adjusting critical)
            clamped_value = critical - step
            if clamped_value < 0.0:
                clamped_value = 0.0
            # Temporarily block signals to prevent infinite recursion
            self.turn_warning_threshold_spinbox.blockSignals(True)
            self.turn_warning_threshold_spinbox.setValue(clamped_value)
            self.turn_warning_threshold_spinbox.blockSignals(False)
        # Update formatted labels
        self._update_turn_threshold_labels()

    def _on_turn_critical_threshold_changed(self, value):
        """Start debounced adjustment of turn critical threshold."""
        self._turn_critical_threshold_timer.start(500)

    def _adjust_turn_critical_threshold(self):
        """Ensure turn critical threshold is always higher than warning threshold."""
        value = self.turn_critical_threshold_spinbox.value()
        warning = self.turn_warning_threshold_spinbox.value()
        step = self.turn_critical_threshold_spinbox.singleStep()
        if value <= warning:
            # Clamp critical to warning + step (instead of adjusting warning)
            clamped_value = warning + step
            max_val = self.turn_critical_threshold_spinbox.maximum()
            if clamped_value > max_val:
                clamped_value = max_val
            # Temporarily block signals to prevent infinite recursion
            self.turn_critical_threshold_spinbox.blockSignals(True)
            self.turn_critical_threshold_spinbox.setValue(clamped_value)
            self.turn_critical_threshold_spinbox.blockSignals(False)
        # Update formatted labels
        self._update_turn_threshold_labels()

    def _update_turn_threshold_labels(self):
        """Update formatted labels for turn thresholds."""
        # Format warning threshold (percentage)
        warning_value = self.turn_warning_threshold_spinbox.value()
        warning_text = f"({int(warning_value * 100)})"
        self.turn_warning_formatted_label.setText(warning_text)

        # Format critical threshold (percentage)
        critical_value = self.turn_critical_threshold_spinbox.value()
        critical_text = f"({int(critical_value * 100)})"
        self.turn_critical_formatted_label.setText(critical_text)

    def update_model_suggestions(self, model_to_set=None):
        """Update model suggestions based on current provider selection.

        Args:
            model_to_set: If provided, try to select this model after updating suggestions.
                          If not provided, try to restore current model text.
        """
        provider = self.provider_combo.currentText()

        # Store current model text before clearing
        current_model = model_to_set if model_to_set is not None else self.model_combo.currentText()

        # Clear current items
        self.model_combo.clear()

        # Add provider-specific suggestions
        if provider == "OpenAI (compatible)":
            suggestions = ["deepseek-reasoner", "gpt-4", "gpt-3.5-turbo", "big pickle", "llama-3", "mixtral"]
        elif provider == "Anthropic":
            suggestions = ["claude-3-opus", "claude-3-sonnet", "claude-3-haiku", "claude-3"]
        elif provider == "OpenAI":
            suggestions = ["gpt-4-turbo", "gpt-4", "gpt-3.5-turbo", "gpt-3.5"]
        else:
            suggestions = ["deepseek-reasoner", "gpt-4", "claude-3", "llama-3"]

        self.model_combo.addItems(suggestions)

        # Try to restore specified model, or use first suggestion
        index = self.model_combo.findText(current_model)
        if index >= 0:
            self.model_combo.setCurrentIndex(index)
        elif current_model:
            # If custom model was entered, set it as current text
            self.model_combo.setCurrentText(current_model)
        else:
            self.model_combo.setCurrentIndex(0)

    def get_config_dict(self):
        """Return a dictionary of current control values suitable for JSON serialization."""
        config = {}
        # Provider configuration
        provider_display = self.provider_combo.currentText()
        config["provider_type"] = self._provider_mapping.get(provider_display, "openai_compatible")
        # Get API key - store empty string if field is empty
        config["api_key"] = self.api_key_edit.text().strip()

        # Get base URL - only include if not empty (empty means use default)
        base_url = self.base_url_edit.text().strip()
        if base_url:
            config["base_url"] = base_url
        config["model"] = self.model_combo.currentText()

        # Agent parameters
        config["temperature"] = self.temperature_spinbox.value()
        config["max_turns"] = self.max_turns_spinbox.value()
        config["token_monitor_enabled"] = self.token_monitor_checkbox.isChecked()
        config["warning_threshold"] = self.warning_threshold_spinbox.value()
        config["critical_threshold"] = self.critical_threshold_spinbox.value()
        config["token_monitor_warning_threshold"] = self.warning_threshold_spinbox.value() * 1000
        config["token_monitor_critical_threshold"] = self.critical_threshold_spinbox.value() * 1000
        # Turn monitoring
        config["turn_monitor_enabled"] = self.turn_monitor_checkbox.isChecked()
        config["turn_monitor_warning_threshold"] = self.turn_warning_threshold_spinbox.value()
        config["turn_monitor_critical_threshold"] = self.turn_critical_threshold_spinbox.value()
        # Conversation pruning (default values)
        config["max_history_turns"] = self.max_history_turns
        config["keep_initial_query"] = self.keep_initial_query
        config["keep_system_messages"] = self.keep_system_messages
        # Workspace path: None if display is "None (unrestricted)"
        workspace_display = self.workspace_display.text()
        workspace_path = None if workspace_display == "None (unrestricted)" else workspace_display
        config["workspace_path"] = workspace_path
        config["tool_output_limit"] = self.tool_output_limit_spinbox.value()
        config["detail"] = self.detail_combo.currentText()
        config["enabled_tools"] = [name for name, cb in self.tool_checkboxes.items() if cb.isChecked()]
        # Provider-specific config (empty dict for now)
        config["provider_config"] = {}
        # Preset selection
        preset_filepath = self.preset_combo.currentData()
        if preset_filepath:
            config["preset_name"] = preset_filepath
        else:
            config["preset_name"] = None
        return config

    def set_config_dict(self, config):
        """Set control values from a configuration dictionary."""
        # Provider configuration
        # Map internal provider_type to GUI display value
        reverse_mapping = {v: k for k, v in self._provider_mapping.items()}
        if "provider_type" in config:
            provider_type = config["provider_type"]
            display_text = reverse_mapping.get(provider_type, "OpenAI (compatible)")
            index = self.provider_combo.findText(display_text)
            if index >= 0:
                self.provider_combo.setCurrentIndex(index)
        if "api_key" in config:
            self.api_key_edit.setText(config["api_key"])
        if "base_url" in config:
            self.base_url_edit.setText(config["base_url"])
        # Model selection (already handled later, but we need to ensure it's after provider)
        # ... (continuing with rest of method)

        # Model
        if "model" in config:
            self.model_combo.setEditText(config["model"])

        # Temperature
        if "temperature" in config:
            self.temperature_spinbox.setValue(config["temperature"])

        # Max turns
        if "max_turns" in config:
            self.max_turns_spinbox.setValue(config["max_turns"])

        # Token monitoring
        if "token_monitor_enabled" in config:
            self.token_monitor_checkbox.setChecked(config["token_monitor_enabled"])
        if "warning_threshold" in config:
            self.warning_threshold_spinbox.setValue(config["warning_threshold"])
        if "critical_threshold" in config:
            self.critical_threshold_spinbox.setValue(config["critical_threshold"])

        # Turn monitoring
        if "turn_monitor_enabled" in config:
            self.turn_monitor_checkbox.setChecked(config["turn_monitor_enabled"])
        if "turn_monitor_warning_threshold" in config:
            self.turn_warning_threshold_spinbox.setValue(config["turn_monitor_warning_threshold"])
        if "turn_monitor_critical_threshold" in config:
            self.turn_critical_threshold_spinbox.setValue(config["turn_monitor_critical_threshold"])

        # Tool output limit
        if "tool_output_limit" in config:
            self.tool_output_limit_spinbox.setValue(config["tool_output_limit"])

        # Detail level
        if "detail" in config:
            self.detail_combo.setCurrentText(config["detail"])

        # Enabled tools
        if "enabled_tools" in config:
            enabled_tools = set(config["enabled_tools"])
            for name, checkbox in self.tool_checkboxes.items():
                checkbox.setChecked(name in enabled_tools)

        # Workspace
        if "workspace_path" in config:
            workspace_path = config["workspace_path"]
            if workspace_path is None:
                self.workspace_display.setText("None (unrestricted)")
            else:
                self.workspace_display.setText(workspace_path)

        # Preset - find and select if present
        if "preset_name" in config and config["preset_name"]:
            for i in range(self.preset_combo.count()):
                if self.preset_combo.itemData(i) == config["preset_name"]:
                    self.preset_combo.setCurrentIndex(i)
                    break
