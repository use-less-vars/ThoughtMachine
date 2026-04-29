"""Agent Controls Panel - Collapsible panel for agent configuration."""
import os
import yaml
from PyQt6.QtWidgets import QGroupBox, QVBoxLayout, QHBoxLayout, QGridLayout, QWidget, QLabel, QPushButton, QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox, QCheckBox, QScrollArea, QStyle, QSizePolicy
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from .mcp_config import MCPConfigDialog
from .markdown_renderer import MarkdownRenderer
from ..utils.constants import MAX_RESULT_LENGTH
from agent.logging import log

class AgentControlsPanel(QGroupBox):
    """Collapsible panel for agent controls."""

    apply_to_agent_requested = pyqtSignal(object)

    def __init__(self, tool_classes, config_file=None):
        super().__init__('Agent Controls')
        self.tool_classes = tool_classes
        self.tool_checkboxes = {}
        self.is_collapsed = True
        self.config_file = config_file
        self._provider_mapping = {'OpenAI (compatible)': 'openai_compatible', 'Anthropic': 'anthropic', 'OpenAI': 'openai'}
        self.max_history_turns = None
        self.keep_initial_query = True
        self.keep_system_messages = True
        self.toggle_button = QPushButton('▼ Show Controls')
        self.toggle_button.setMaximumWidth(120)
        self.toggle_button.clicked.connect(self.toggle_collapse)
        self.main_layout = QVBoxLayout()
        self.setLayout(self.main_layout)
        self.main_layout.addWidget(self.toggle_button)
        self.controls_container = QWidget()
        self.controls_layout = QHBoxLayout()
        self.controls_layout.setSpacing(10)
        self.left_column = QVBoxLayout()
        self.left_column.setSpacing(10)
        self.right_column = QVBoxLayout()
        self.right_column.setSpacing(10)
        self.controls_layout.addLayout(self.left_column)
        self.controls_layout.addLayout(self.right_column)
        self.controls_container.setLayout(self.controls_layout)
        preset_row = QWidget()
        preset_layout = QHBoxLayout()
        preset_row.setLayout(preset_layout)
        preset_layout.addWidget(QLabel('Preset:'))
        self.preset_combo = QComboBox()
        self.preset_combo.setEditable(False)
        self._load_presets()
        self.preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        preset_layout.addWidget(self.preset_combo)
        preset_layout.addStretch()
        self.left_column.addWidget(preset_row)
        workspace_row = QWidget()
        workspace_layout = QHBoxLayout()
        workspace_row.setLayout(workspace_layout)
        workspace_layout.addWidget(QLabel('Workspace:'))
        self.workspace_display = QLabel('None (unrestricted)')
        self.workspace_display.setStyleSheet('color: blue;')
        self.workspace_display.setWordWrap(True)
        workspace_layout.addWidget(self.workspace_display)
        self.set_workspace_btn = QPushButton('Set')
        self.set_workspace_btn.setMaximumWidth(60)
        workspace_layout.addWidget(self.set_workspace_btn)
        self.clear_workspace_btn = QPushButton('Clear')
        self.clear_workspace_btn.setMaximumWidth(60)
        workspace_layout.addWidget(self.clear_workspace_btn)
        self.left_column.addWidget(workspace_row)
        token_monitor_row = QWidget()
        token_monitor_layout = QHBoxLayout()
        token_monitor_row.setLayout(token_monitor_layout)
        token_monitor_layout.setSpacing(5)
        self.token_monitor_checkbox = QCheckBox('Token warnings')
        self.token_monitor_checkbox.setChecked(True)
        token_monitor_layout.addWidget(self.token_monitor_checkbox)
        token_monitor_layout.addWidget(QLabel('Warning:'))
        self.warning_threshold_spinbox = QSpinBox()
        self.warning_threshold_spinbox.setRange(1, 200)
        self.warning_threshold_spinbox.setValue(35)
        self.warning_threshold_spinbox.setSingleStep(1)
        token_monitor_layout.addWidget(self.warning_threshold_spinbox)
        self.warning_formatted_label = QLabel('(35k)')
        token_monitor_layout.addWidget(self.warning_formatted_label)
        token_monitor_layout.addWidget(QLabel('tokens'))
        token_monitor_layout.addWidget(QLabel('Critical:'))
        self.critical_threshold_spinbox = QSpinBox()
        self.critical_threshold_spinbox.setRange(1, 200)
        self.critical_threshold_spinbox.setValue(50)
        self.critical_threshold_spinbox.setSingleStep(1)
        token_monitor_layout.addWidget(self.critical_threshold_spinbox)
        self.critical_formatted_label = QLabel('(50k)')
        token_monitor_layout.addWidget(self.critical_formatted_label)
        self.left_column.addWidget(token_monitor_row)
        max_turns_row = QWidget()
        max_turns_layout = QHBoxLayout()
        max_turns_row.setLayout(max_turns_layout)
        max_turns_layout.setSpacing(5)
        max_turns_layout.addWidget(QLabel('Max turns:'))
        self.max_turns_spinbox = QSpinBox()
        self.max_turns_spinbox.setRange(1, 500)
        self.max_turns_spinbox.setValue(100)
        max_turns_layout.addWidget(self.max_turns_spinbox)
        max_turns_layout.addWidget(QLabel('turns'))
        self.left_column.addWidget(max_turns_row)
        turn_monitor_row = QWidget()
        turn_monitor_layout = QHBoxLayout()
        turn_monitor_row.setLayout(turn_monitor_layout)
        turn_monitor_layout.setSpacing(5)
        self.turn_monitor_checkbox = QCheckBox('Turn warnings')
        self.turn_monitor_checkbox.setChecked(True)
        turn_monitor_layout.addWidget(self.turn_monitor_checkbox)
        turn_monitor_layout.addWidget(QLabel('Warning:'))
        self.turn_warning_threshold_spinbox = QDoubleSpinBox()
        self.turn_warning_threshold_spinbox.setRange(0.0, 1.0)
        self.turn_warning_threshold_spinbox.setValue(0.8)
        self.turn_warning_threshold_spinbox.setSingleStep(0.05)
        self.turn_warning_threshold_spinbox.setDecimals(2)
        turn_monitor_layout.addWidget(self.turn_warning_threshold_spinbox)
        self.turn_warning_formatted_label = QLabel('(80)')
        turn_monitor_layout.addWidget(self.turn_warning_formatted_label)
        turn_monitor_layout.addWidget(QLabel('turns'))
        turn_monitor_layout.addWidget(QLabel('Critical:'))
        self.turn_critical_threshold_spinbox = QDoubleSpinBox()
        self.turn_critical_threshold_spinbox.setRange(0.0, 1.0)
        self.turn_critical_threshold_spinbox.setValue(0.95)
        self.turn_critical_threshold_spinbox.setSingleStep(0.05)
        self.turn_critical_threshold_spinbox.setDecimals(2)
        turn_monitor_layout.addWidget(self.turn_critical_threshold_spinbox)
        self.turn_critical_formatted_label = QLabel('(95)')
        turn_monitor_layout.addWidget(self.turn_critical_formatted_label)
        turn_monitor_layout.addWidget(QLabel('turns'))
        self.left_column.addWidget(turn_monitor_row)
        temperature_row = QWidget()
        temperature_layout = QHBoxLayout()
        temperature_row.setLayout(temperature_layout)
        temperature_layout.setSpacing(5)
        temperature_layout.addWidget(QLabel('Temperature:'))
        self.temperature_spinbox = QDoubleSpinBox()
        self.temperature_spinbox.setRange(0.0, 2.0)
        self.temperature_spinbox.setValue(0.2)
        self.temperature_spinbox.setSingleStep(0.1)
        self.temperature_spinbox.setDecimals(1)
        temperature_layout.addWidget(self.temperature_spinbox)
        temperature_layout.addWidget(QLabel(''))
        self.left_column.addWidget(temperature_row)
        provider_row = QWidget()
        provider_layout = QHBoxLayout()
        provider_row.setLayout(provider_layout)
        provider_layout.setSpacing(5)
        provider_layout.addWidget(QLabel('Provider:'))
        self.provider_combo = QComboBox()
        self.provider_combo.addItems(['OpenAI (compatible)', 'Anthropic', 'OpenAI'])
        self.provider_combo.setCurrentText('OpenAI (compatible)')
        provider_layout.addWidget(self.provider_combo)
        provider_layout.addStretch()
        self.left_column.addWidget(provider_row)
        api_key_row = QWidget()
        api_key_layout = QHBoxLayout()
        api_key_row.setLayout(api_key_layout)
        api_key_layout.setSpacing(5)
        api_key_layout.addWidget(QLabel('API Key:'))
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setPlaceholderText('Leave empty to use environment variable')
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        api_key_layout.addWidget(self.api_key_edit)
        api_key_layout.addStretch()
        self.right_column.addWidget(api_key_row)
        base_url_row = QWidget()
        base_url_layout = QHBoxLayout()
        base_url_row.setLayout(base_url_layout)
        base_url_layout.setSpacing(5)
        base_url_layout.addWidget(QLabel('Base URL:'))
        self.base_url_edit = QLineEdit()
        self.base_url_edit.setPlaceholderText('Leave empty for default')
        base_url_layout.addWidget(self.base_url_edit)
        base_url_layout.addStretch()
        self.right_column.addWidget(base_url_row)
        model_row = QWidget()
        model_layout = QHBoxLayout()
        model_row.setLayout(model_layout)
        model_layout.setSpacing(5)
        model_layout.addWidget(QLabel('Model:'))
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.addItems(['deepseek-reasoner', 'gpt-4', 'claude-3', 'llama-3', 'big pickle', 'gpt-3.5-turbo', 'claude-3-haiku', 'claude-3-sonnet', 'claude-3-opus'])
        self.model_combo.setCurrentText('deepseek-reasoner')
        model_layout.addWidget(self.model_combo)
        model_layout.addStretch()
        self.right_column.addWidget(model_row)
        tool_limit_row = QWidget()
        tool_limit_layout = QHBoxLayout()
        tool_limit_row.setLayout(tool_limit_layout)
        tool_limit_layout.setSpacing(5)
        tool_limit_layout.addWidget(QLabel('Tool output limit:'))
        self.tool_output_limit_spinbox = QSpinBox()
        self.tool_output_limit_spinbox.setRange(1000, 100000)
        self.tool_output_limit_spinbox.setValue(10000)
        self.tool_output_limit_spinbox.setSingleStep(1000)
        tool_limit_layout.addWidget(self.tool_output_limit_spinbox)
        tool_limit_layout.addWidget(QLabel('tokens'))
        self.right_column.addWidget(tool_limit_row)
        detail_row = QWidget()
        detail_layout = QHBoxLayout()
        detail_row.setLayout(detail_layout)
        detail_layout.setSpacing(5)
        detail_layout.addWidget(QLabel('Detail:'))
        self.detail_combo = QComboBox()
        self.detail_combo.addItems(['minimal', 'normal', 'verbose'])
        self.detail_combo.setCurrentText('normal')
        detail_layout.addWidget(self.detail_combo)
        detail_layout.addStretch()
        self.right_column.addWidget(detail_row)
        tool_group = QGroupBox('Tools')
        tool_layout = QGridLayout()
        tool_group.setLayout(tool_layout)
        tool_group.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.MinimumExpanding)
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
        tool_layout.setRowStretch(tool_row + 1, 1)
        tool_scroll_area = QScrollArea()
        tool_scroll_area.setWidgetResizable(True)
        tool_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        tool_scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        tool_scroll_area.setMaximumHeight(400)
        tool_scroll_area.setWidget(tool_group)
        self.tool_group = tool_group
        self.tool_scroll_area = tool_scroll_area
        mcp_config_row = QWidget()
        mcp_config_layout = QHBoxLayout()
        mcp_config_row.setLayout(mcp_config_layout)
        self.mcp_config_btn = QPushButton('MCP Config')
        self.mcp_config_btn.setMaximumWidth(120)
        mcp_config_layout.addWidget(self.mcp_config_btn)
        mcp_config_layout.addStretch()
        self.right_column.addWidget(mcp_config_row)
        self.right_column.addWidget(tool_scroll_area)
        self.left_column.addStretch()
        self.right_column.addStretch()
        self.main_layout.addWidget(self.controls_container)
        self.controls_container.setVisible(False)
        apply_row = QWidget()
        apply_layout = QHBoxLayout()
        apply_row.setLayout(apply_layout)
        self.apply_btn = QPushButton('Apply to Agent')
        self.apply_btn.setToolTip('Apply runtime changes (temperature, max_tokens, top_p) without restarting the agent.')
        self.apply_btn.setMaximumWidth(150)
        self.apply_btn.clicked.connect(self._on_apply_to_agent)
        apply_layout.addWidget(self.apply_btn)
        self.save_global_btn = QPushButton('Save as Global Default')
        self.save_global_btn.setToolTip('Save current configuration as the global default config file.')
        self.save_global_btn.setMaximumWidth(180)
        self.save_global_btn.clicked.connect(self._on_save_global_default)
        apply_layout.addWidget(self.save_global_btn)
        apply_layout.addStretch()
        self.main_layout.addWidget(apply_row)
        self._warning_threshold_timer = QTimer()
        self._warning_threshold_timer.setSingleShot(True)
        self._warning_threshold_timer.timeout.connect(self._adjust_warning_threshold)
        self._critical_threshold_timer = QTimer()
        self._critical_threshold_timer.setSingleShot(True)
        self._critical_threshold_timer.timeout.connect(self._adjust_critical_threshold)
        self._turn_warning_threshold_timer = QTimer()
        self._turn_warning_threshold_timer.setSingleShot(True)
        self._turn_warning_threshold_timer.timeout.connect(self._adjust_turn_warning_threshold)
        self._turn_critical_threshold_timer = QTimer()
        self._turn_critical_threshold_timer.setSingleShot(True)
        self._turn_critical_threshold_timer.timeout.connect(self._adjust_turn_critical_threshold)
        self.warning_threshold_spinbox.valueChanged.connect(self._on_warning_threshold_changed)
        self.critical_threshold_spinbox.valueChanged.connect(self._on_critical_threshold_changed)
        self.token_monitor_checkbox.stateChanged.connect(self.update_token_monitor_controls)
        self.turn_monitor_checkbox.stateChanged.connect(self.update_turn_monitor_controls)
        self.turn_warning_threshold_spinbox.valueChanged.connect(self._on_turn_warning_threshold_changed)
        self.turn_critical_threshold_spinbox.valueChanged.connect(self._on_turn_critical_threshold_changed)
        self.mcp_config_btn.clicked.connect(self._open_mcp_config)
        self._update_token_threshold_labels()
        self.update_turn_monitor_controls()
        self._update_turn_threshold_labels()

    def _load_presets(self):
        """Load available presets from the presets directory into the combo box."""
        presets_dir = 'presets'
        self.preset_combo.clear()
        self.preset_combo.addItem('None', None)
        if os.path.isdir(presets_dir):
            for filename in sorted(os.listdir(presets_dir)):
                if filename.endswith('.yaml') or filename.endswith('.yml'):
                    filepath = os.path.join(presets_dir, filename)
                    try:
                        with open(filepath, 'r') as f:
                            data = yaml.safe_load(f)
                            name = data.get('name', filename)
                            self.preset_combo.addItem(name, filepath)
                    except Exception as e:
                        log('ERROR', 'debug.unknown', f'Error loading preset {filepath}: {e}')
        else:
            log('WARNING', 'debug.unknown', f"Presets directory '{presets_dir}' not found.")

    def _on_preset_changed(self, index):
        """Handle preset selection change.

        Load the selected preset and update UI controls to reflect its values.
        """
        filepath = self.preset_combo.itemData(index)
        if not filepath:
            return
        try:
            with open(filepath, 'r') as f:
                preset_data = yaml.safe_load(f)
        except Exception as e:
            log('ERROR', 'debug.unknown', f'Error loading preset {filepath}: {e}')
            return
        self._apply_preset_values(preset_data)

    def _apply_preset_values(self, preset_data):
        """Apply preset dictionary values to UI controls.

        This updates the controls to show the preset's configuration.
        Individual controls can still be adjusted by the user after selection.
        """
        if 'model' in preset_data:
            self.model_combo.setEditText(preset_data['model'])
        if 'temperature' in preset_data:
            self.temperature_spinbox.setValue(preset_data['temperature'])
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
        if hasattr(self, 'on_mcp_config_changed') and callable(self.on_mcp_config_changed):
            self.on_mcp_config_changed()

    def toggle_collapse(self):
        """Toggle visibility of controls."""
        self.is_collapsed = not self.is_collapsed
        self.controls_container.setVisible(not self.is_collapsed)
        if self.is_collapsed:
            self.toggle_button.setText('▼ Show Controls')
        else:
            self.toggle_button.setText('▲ Hide Controls')
        self.adjustSize()

    def get_enabled_tool_names(self):
        return [name for name, cb in self.tool_checkboxes.items() if cb.isChecked()]

    def _rebuild_tool_checkboxes(self):
        """Rebuild tool checkboxes from the current tool_classes list.

        This method clears existing checkboxes and recreates them based on
        self.tool_classes. Called when MCP configuration changes.
        """
        for checkbox in self.tool_checkboxes.values():
            checkbox.setParent(None)
            checkbox.deleteLater()
        self.tool_checkboxes.clear()
        tool_layout = self.tool_group.layout()
        while tool_layout.count():
            item = tool_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        col = 0
        tool_row = 0
        for i, cls in enumerate(self.tool_classes):
            checkbox = QCheckBox(cls.__name__)
            checkbox.setToolTip(cls.__doc__ or 'No documentation available')
            checkbox.setChecked(True)
            tool_layout.addWidget(checkbox, tool_row, col)
            self.tool_checkboxes[cls.__name__] = checkbox
            col += 1
            if col >= 2:
                col = 0
                tool_row += 1
        if col == 1:
            tool_layout.addWidget(QWidget(), tool_row, col)
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
            clamped_value = critical - step
            if clamped_value < 1:
                clamped_value = 1
            self.warning_threshold_spinbox.blockSignals(True)
            self.warning_threshold_spinbox.setValue(clamped_value)
            self.warning_threshold_spinbox.blockSignals(False)
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
            clamped_value = warning + step
            max_val = self.critical_threshold_spinbox.maximum()
            if clamped_value > max_val:
                clamped_value = max_val
            self.critical_threshold_spinbox.blockSignals(True)
            self.critical_threshold_spinbox.setValue(clamped_value)
            self.critical_threshold_spinbox.blockSignals(False)
        self._update_token_threshold_labels()

    def _update_token_threshold_labels(self):
        """Update formatted labels for token thresholds."""
        warning_value = self.warning_threshold_spinbox.value() * 1000
        if warning_value >= 1000:
            warning_text = f'({warning_value // 1000}k)'
        else:
            warning_text = f'({warning_value})'
        self.warning_formatted_label.setText(warning_text)
        critical_value = self.critical_threshold_spinbox.value() * 1000
        if critical_value >= 1000:
            critical_text = f'({critical_value // 1000}k)'
        else:
            critical_text = f'({critical_value})'
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
            clamped_value = critical - step
            if clamped_value < 0.0:
                clamped_value = 0.0
            self.turn_warning_threshold_spinbox.blockSignals(True)
            self.turn_warning_threshold_spinbox.setValue(clamped_value)
            self.turn_warning_threshold_spinbox.blockSignals(False)
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
            clamped_value = warning + step
            max_val = self.turn_critical_threshold_spinbox.maximum()
            if clamped_value > max_val:
                clamped_value = max_val
            self.turn_critical_threshold_spinbox.blockSignals(True)
            self.turn_critical_threshold_spinbox.setValue(clamped_value)
            self.turn_critical_threshold_spinbox.blockSignals(False)
        self._update_turn_threshold_labels()

    def _update_turn_threshold_labels(self):
        """Update formatted labels for turn thresholds."""
        warning_value = self.turn_warning_threshold_spinbox.value()
        warning_text = f'({int(warning_value * 100)})'
        self.turn_warning_formatted_label.setText(warning_text)
        critical_value = self.turn_critical_threshold_spinbox.value()
        critical_text = f'({int(critical_value * 100)})'
        self.turn_critical_formatted_label.setText(critical_text)

    def update_model_suggestions(self, model_to_set=None):
        """Update model suggestions based on current provider selection.

        Args:
            model_to_set: If provided, try to select this model after updating suggestions.
                          If not provided, try to restore current model text.
        """
        provider = self.provider_combo.currentText()
        current_model = model_to_set if model_to_set is not None else self.model_combo.currentText()
        self.model_combo.clear()
        if provider == 'OpenAI (compatible)':
            suggestions = ['deepseek-reasoner', 'gpt-4', 'gpt-3.5-turbo', 'big pickle', 'llama-3', 'mixtral']
        elif provider == 'Anthropic':
            suggestions = ['claude-3-opus', 'claude-3-sonnet', 'claude-3-haiku', 'claude-3']
        elif provider == 'OpenAI':
            suggestions = ['gpt-4-turbo', 'gpt-4', 'gpt-3.5-turbo', 'gpt-3.5']
        else:
            suggestions = ['deepseek-reasoner', 'gpt-4', 'claude-3', 'llama-3']
        self.model_combo.addItems(suggestions)
        index = self.model_combo.findText(current_model)
        if index >= 0:
            self.model_combo.setCurrentIndex(index)
        elif current_model:
            self.model_combo.setCurrentText(current_model)
        else:
            self.model_combo.setCurrentIndex(0)

    def _on_apply_to_agent(self):
        """Emit signal with current config when Apply to Agent is clicked."""
        config = self.get_config_dict()
        self.apply_to_agent_requested.emit(config)

    def _on_save_global_default(self):
        """Save current config as global default (user config file)."""
        config = self.get_config_dict()
        from PyQt6.QtWidgets import QMessageBox
        from agent.config import get_config_paths
        import json
        paths = get_config_paths()
        user_config_path = paths.get('user_config')
        if not user_config_path:
            QMessageBox.warning(self, 'Error', 'Could not determine user config path.')
            return
        if not str(user_config_path).endswith('.json'):
            user_config_path = str(user_config_path) + '.json'
        try:
            with open(user_config_path, 'w') as f:
                json.dump(config, f, indent=2)
            QMessageBox.information(
                self, 'Saved',
                f'Configuration saved as global default to:\n{user_config_path}'
            )
        except Exception as e:
            QMessageBox.critical(self, 'Save Error', f'Failed to save config:\n{e}')

    def get_config_dict(self):
        """Return a dictionary of current control values suitable for JSON serialization."""
        config = {}
        provider_display = self.provider_combo.currentText()
        config['provider_type'] = self._provider_mapping.get(provider_display, 'openai_compatible')
        config['api_key'] = self.api_key_edit.text().strip()
        base_url = self.base_url_edit.text().strip()
        if base_url:
            config['base_url'] = base_url
        config['model'] = self.model_combo.currentText()
        config['temperature'] = self.temperature_spinbox.value()
        config['max_turns'] = self.max_turns_spinbox.value()
        config['token_monitor_enabled'] = self.token_monitor_checkbox.isChecked()
        config['warning_threshold'] = self.warning_threshold_spinbox.value()
        config['critical_threshold'] = self.critical_threshold_spinbox.value()
        config['token_monitor_warning_threshold'] = self.warning_threshold_spinbox.value() * 1000
        config['token_monitor_critical_threshold'] = self.critical_threshold_spinbox.value() * 1000
        config['turn_monitor_enabled'] = self.turn_monitor_checkbox.isChecked()
        config['turn_monitor_warning_threshold'] = self.turn_warning_threshold_spinbox.value()
        config['turn_monitor_critical_threshold'] = self.turn_critical_threshold_spinbox.value()
        config['max_history_turns'] = self.max_history_turns
        config['keep_initial_query'] = self.keep_initial_query
        config['keep_system_messages'] = self.keep_system_messages
        workspace_display = self.workspace_display.text()
        workspace_path = None if workspace_display == 'None (unrestricted)' else workspace_display
        config['workspace_path'] = workspace_path
        config['tool_output_limit'] = self.tool_output_limit_spinbox.value()
        config['detail'] = self.detail_combo.currentText()
        config['enabled_tools'] = [name for name, cb in self.tool_checkboxes.items() if cb.isChecked()]
        config['provider_config'] = {}
        preset_filepath = self.preset_combo.currentData()
        if preset_filepath:
            config['preset_name'] = preset_filepath
        else:
            config['preset_name'] = None
        return config

    def set_config_dict(self, config):
        """Set control values from a configuration dictionary."""
        reverse_mapping = {v: k for k, v in self._provider_mapping.items()}
        if 'provider_type' in config:
            provider_type = config['provider_type']
            display_text = reverse_mapping.get(provider_type, 'OpenAI (compatible)')
            index = self.provider_combo.findText(display_text)
            if index >= 0:
                self.provider_combo.setCurrentIndex(index)
        if 'api_key' in config:
            self.api_key_edit.setText(config['api_key'])
        if 'base_url' in config:
            self.base_url_edit.setText(config['base_url'])
        if 'model' in config:
            self.model_combo.setEditText(config['model'])
        if 'temperature' in config:
            self.temperature_spinbox.setValue(config['temperature'])
        if 'max_turns' in config:
            self.max_turns_spinbox.setValue(config['max_turns'])
        if 'token_monitor_enabled' in config:
            self.token_monitor_checkbox.setChecked(config['token_monitor_enabled'])
        if 'warning_threshold' in config:
            self.warning_threshold_spinbox.setValue(config['warning_threshold'])
        if 'critical_threshold' in config:
            self.critical_threshold_spinbox.setValue(config['critical_threshold'])
        if 'turn_monitor_enabled' in config:
            self.turn_monitor_checkbox.setChecked(config['turn_monitor_enabled'])
        if 'turn_monitor_warning_threshold' in config:
            self.turn_warning_threshold_spinbox.setValue(config['turn_monitor_warning_threshold'])
        if 'turn_monitor_critical_threshold' in config:
            self.turn_critical_threshold_spinbox.setValue(config['turn_monitor_critical_threshold'])
        if 'tool_output_limit' in config:
            self.tool_output_limit_spinbox.setValue(config['tool_output_limit'])
        if 'detail' in config:
            self.detail_combo.setCurrentText(config['detail'])
        if 'enabled_tools' in config:
            enabled_tools = set(config['enabled_tools'])
            for name, checkbox in self.tool_checkboxes.items():
                checkbox.setChecked(name in enabled_tools)
        if 'workspace_path' in config:
            workspace_path = config['workspace_path']
            if workspace_path is None:
                self.workspace_display.setText('None (unrestricted)')
            else:
                self.workspace_display.setText(workspace_path)
        if 'preset_name' in config and config['preset_name']:
            for i in range(self.preset_combo.count()):
                if self.preset_combo.itemData(i) == config['preset_name']:
                    self.preset_combo.setCurrentIndex(i)
                    break