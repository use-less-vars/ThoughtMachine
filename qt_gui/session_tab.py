"""Session Tab - Individual session tab widget for the ThoughtMachine GUI."""
import sys
import os
import json
import html
import datetime
from PyQt6.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QLineEdit, QPushButton, QTextEdit, QListWidget, QStyledItemDelegate, QGroupBox, QCheckBox, QMenuBar, QMenu, QFileDialog, QStyleOptionViewItem, QMessageBox, QScrollArea, QFrame, QComboBox, QSpinBox, QDoubleSpinBox, QSplitter, QTabWidget, QDialog, QSizePolicy, QStyle, QInputDialog
from PyQt6.QtCore import Qt, QTimer, pyqtSlot, QAbstractListModel, QModelIndex, QVariant, QRect, QPoint, QSize, QSortFilterProxyModel, QMetaObject, QThread
from PyQt6.QtGui import QAction, QKeySequence, QFont, QTextDocument, QTextCursor, QColor, QPainter, QPalette, QAbstractTextDocumentLayout, QPageLayout, QPageSize, QShortcut
from PyQt6.QtPrintSupport import QPrinter
from dotenv import load_dotenv
from agent.presenter.agent_presenter import RefactoredAgentPresenter
from agent.core.state import ExecutionState
from agent.config.service import create_agent_config_service
from qt_gui.config.config_bridge import GUIConfigBridge
from session.store import FileSystemSessionStore
from tools import SIMPLIFIED_TOOL_CLASSES
from pathlib import Path
load_dotenv()
from agent.logging import log
from qt_gui.themes import apply_theme
from qt_gui.panels.output_panel import OutputPanel
from qt_gui.panels.query_panel import QueryPanel
from qt_gui.panels.status_panel import StatusPanel
from qt_gui.panels.agent_controls import AgentControlsPanel

class SessionTab(QWidget):

    def __init__(self, parent=None, session_store=None, session_id=None):
        super().__init__(parent)
        self._session = None
        self.presenter = RefactoredAgentPresenter()
        if session_store is not None:
            self.presenter.session_store = session_store
        self.config_bridge = GUIConfigBridge(create_agent_config_service())
        self.config_bridge.add_change_listener(self._on_config_changed)
        self.total_input = 0
        self.total_output = 0
        self.context_length = 0
        self.current_theme = None
        self.last_history = None
        self._cached_config = None
        self._display_turn = 0
        self._display_retry_count = 0
        self._last_conversation_version = 0
        self._displayed_message_count = 0
        self._loading_config = False
        self._closing = False
        self._auto_save_timer = QTimer(self)
        self._auto_save_timer.setInterval(120000)
        self._auto_save_timer.timeout.connect(self._auto_save_session)
        self._auto_save_timer.start()
        self.output_panel = OutputPanel(self)
        self.query_panel = QueryPanel(self)
        self.output_textedit = self.output_panel.output_textedit
        self.filter_lineedit = self.output_panel.filter_lineedit
        self.filter_type_combo = self.output_panel.filter_type_combo
        self.query_entry = self.query_panel.query_entry
        self.run_btn = self.query_panel.run_btn
        self.pause_btn = self.query_panel.pause_btn
        self.restart_btn = self.query_panel.restart_btn
        self.init_ui()
        self.setup_signal_connections()
        self._conversation_debounce_timer = QTimer(self)
        self._conversation_debounce_timer.setSingleShot(True)
        self._conversation_debounce_timer.setInterval(100)
        self._conversation_debounce_timer.timeout.connect(self._on_conversation_debounced)
        if session_id:
            self.load_session_by_id(session_id)
        else:
            self.create_new_session()
        self.load_config()

    @property
    def session(self):
        return self._session

    @session.setter
    def session(self, value):
        from agent.logging import log
        log('DEBUG', 'debug.unknown', f'[SessionTab] session setter called, value type: {type(value)}')
        if hasattr(self, '_session') and self._session is value:
            log('DEBUG', 'debug.unknown', f'[SessionTab] session unchanged, returning')
            return
        old_session = self._session
        if old_session and hasattr(old_session, 'disconnect_conversation_changed'):
            try:
                old_session.disconnect_conversation_changed(self._on_session_conversation_changed)
                log('DEBUG', 'debug.unknown', f'[SessionTab] Disconnected conversation callback from old session')
            except Exception as e:
                log('ERROR', 'debug.unknown', f'[SessionTab] Error disconnecting callback: {e}')
        self._session = value
        log('DEBUG', 'debug.unknown', f'[SessionTab] _session updated, old: {old_session}, new: {value}')
        if value and hasattr(value, 'connect_conversation_changed'):
            try:
                value.connect_conversation_changed(self._on_session_conversation_changed)
                log('DEBUG', 'debug.unknown', f'[SessionTab] Connected conversation callback to new session')
            except Exception as e:
                log('ERROR', 'debug.unknown', f'[SessionTab] Error connecting callback: {e}')
        if value and value != old_session:
            log('DEBUG', 'debug.unknown', f'[SessionTab] New session, updating window title')
            self.update_window_title()

    def _on_session_conversation_changed(self):
        """Callback triggered when session's user_history changes via ObservableList."""
        from agent.logging import log
        from PyQt6.QtCore import QTimer as QTimer
        log('DEBUG', 'debug.unknown', f'[SessionTab] Session conversation changed callback triggered')
        if not hasattr(self, '_conversation_debounce_timer'):
            log('WARNING', 'debug.unknown', f'[SessionTab] Timer not yet initialized, skipping')
            return
        QTimer.singleShot(0, lambda: self._conversation_debounce_timer.start())

    def create_new_session(self):
        """Create fresh session with auto-generated name."""
        from session.models import Session, SessionConfig
        import uuid
        from datetime import datetime
        from agent.logging import log
        try:
            agent_config = self.presenter.create_agent_config()
            session_config = self.presenter._build_session_config(agent_config)
        except Exception as e:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.critical(self, 'Session Error', f'Failed to create session configuration: {e}')
            return
        old_session_id = self.session.session_id if self.session else None
        log('DEBUG', 'debug.unknown', f'[CALLBACK] create_new_session: replacing session {old_session_id} with new session')
        self.session = Session(session_id=str(uuid.uuid4()), config=session_config, user_history=[], metadata={})
        log('DEBUG', 'debug.unknown', f'[SessionTab] Binding session to presenter, session: {self.session}')
        self.presenter.bind_session(self.session)
        self.presenter.save_session()
        self.update_window_title()
        if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
            log('DEBUG', 'debug.unknown', f'Created new session: {self.session.session_id}')

    def load_session_by_id(self, session_id: str) -> bool:
        """Load a session by ID from the session store."""
        from agent.logging import log
        from session.models import Session
        log('DEBUG', 'debug.unknown', f'load_session_by_id called with session_id: {session_id}')
        if self.session is None:
            log('DEBUG', 'debug.unknown', f'Creating placeholder session for loading')
            agent_config = self.presenter.create_agent_config()
            session_config = self.presenter._build_session_config(agent_config)
            self.session = Session(session_id=session_id, config=session_config, user_history=[], metadata={})
            self.presenter.bind_session(self.session)
        try:
            log('DEBUG', 'debug.unknown', f'Calling presenter.load_session_by_id({session_id}, target_session=self.session)')
            success = self.presenter.load_session_by_id(session_id, target_session=self.session)
        except Exception as e:
            log('ERROR', 'debug.unknown', f'Error loading session {session_id}: {e}')
            self.create_new_session()
            return False
        if success:
            log('DEBUG', 'debug.unknown', f'presenter.load_session_by_id returned success')
            log('DEBUG', 'debug.unknown', f"Session updated in place, id: {(self.session.session_id if self.session else 'None')}")
            self._displayed_message_count = 0
            self._last_conversation_version = 0
            try:
                log('DEBUG', 'debug.unknown', f'Calling display_conversation_from_history()')
                self.display_conversation_from_history()
                log('DEBUG', 'debug.unknown', f'display_conversation_from_history() completed')
            except Exception as e:
                log('ERROR', 'debug.unknown', f'Error displaying session {session_id}: {e}')
            self.update_window_title()
            log('DEBUG', 'debug.unknown', f'Loaded session: {session_id}')
            return True
        else:
            log('WARNING', 'debug.unknown', f'Failed to load session {session_id}, creating new')
            self.create_new_session()
            return False

    def display_conversation_from_history(self, session=None):
        """Display conversation from user_history directly, without synthetic events.
        
        Args:
            session: Optional session object. If None, uses presenter.current_session.
        """
        from agent.logging import log
        log('DEBUG', 'debug.unknown', f"display_conversation_from_history called, output_panel exists: {hasattr(self, 'output_panel')}, status_panel exists: {hasattr(self, 'status_panel')}")
        if not hasattr(self, 'output_panel') or self.output_panel is None or (not hasattr(self, 'status_panel')) or (self.status_panel is None):
            log('DEBUG', 'debug.unknown', f"UI panels not ready, output_panel: {hasattr(self, 'output_panel')}, status_panel: {hasattr(self, 'status_panel')}, retry count: {self._display_retry_count}")
            self._display_retry_count += 1
            if self._display_retry_count > 10:
                log('WARNING', 'debug.unknown', f'Warning: Too many display retries ({self._display_retry_count}), giving up')
                return
            QTimer.singleShot(0, lambda: self.display_conversation_from_history(session))
            return
        self._display_retry_count = 0
        log('DEBUG', 'debug.unknown', f'UI panels ready, proceeding to display conversation')
        target_session = session if session is not None else self.presenter.current_session
        if target_session is None:
            log('WARNING', 'debug.unknown', 'No session available to display')
            return
        user_history = target_session.user_history
        log('DEBUG', 'debug.unknown', f'Displaying {len(user_history)} messages from user_history')
        if target_session.conversation_version == self._last_conversation_version and self._displayed_message_count > 0:
            log('DEBUG', 'debug.unknown', f'Conversation version unchanged ({self._last_conversation_version}) and messages already displayed, skipping display')
            return
        log('DEBUG', 'debug.unknown', f'Conversation version changed: {self._last_conversation_version} -> {target_session.conversation_version}')
        new_message_count = len(user_history)
        needs_full_rebuild = False
        messages_to_append = []
        if new_message_count > self._displayed_message_count:
            messages_to_append = user_history[self._displayed_message_count:]
            log('DEBUG', 'debug.unknown', f'Appending {len(messages_to_append)} new messages (had {self._displayed_message_count}, now {new_message_count})')
        elif new_message_count < self._displayed_message_count:
            log('WARNING', 'debug.unknown', f'History shrank from {self._displayed_message_count} to {new_message_count}, doing full rebuild')
            needs_full_rebuild = True
        else:
            log('DEBUG', 'debug.unknown', f'Same message count but version changed, doing full rebuild')
            needs_full_rebuild = True
        if needs_full_rebuild:
            self.output_panel.load_session_history(user_history, suppress_scroll=True)
        else:
            for message in messages_to_append:
                self.output_panel.display_message(message)
            self.output_panel._auto_scroll_if_bottom()
        self._last_conversation_version = target_session.conversation_version
        self._displayed_message_count = new_message_count
        self.total_input = self.presenter.total_input
        self.total_output = self.presenter.total_output
        self.context_length = self.presenter.context_length
        if hasattr(self, 'status_panel') and self.status_panel is not None:
            self.status_panel.update_tokens(self.presenter.total_input, self.presenter.total_output)
            self.status_panel.update_context_length(self.presenter.context_length)

    def display_loaded_conversation(self):
        """Display a loaded conversation from history (for compatibility)."""
        self.display_conversation_from_history()

    def init_ui(self):
        """Initialize the user interface (unchanged layout)."""
        self.update_window_title()
        main_layout = QHBoxLayout()
        self.setLayout(main_layout)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        middle_container = QWidget()
        middle_layout = QVBoxLayout()
        middle_container.setLayout(middle_layout)
        self.status_panel = StatusPanel()
        middle_layout.addWidget(self.status_panel)
        middle_layout.addStretch()
        splitter.addWidget(middle_container)
        right_container = QWidget()
        right_layout = QVBoxLayout()
        right_container.setLayout(right_layout)
        self.right_layout = right_layout
        rag_enabled = self.config_bridge.config_service.get('rag_enabled', False)
        if not rag_enabled:
            filtered_tool_classes = [cls for cls in SIMPLIFIED_TOOL_CLASSES if cls.__name__ != 'SearchCodebaseTool']
        else:
            filtered_tool_classes = SIMPLIFIED_TOOL_CLASSES
        self.agent_controls_panel = AgentControlsPanel(filtered_tool_classes)
        right_layout.addWidget(self.agent_controls_panel)
        self.agent_controls_panel.on_mcp_config_changed = self._refresh_tools
        self.agent_controls_panel.set_workspace_btn.clicked.connect(self.set_workspace)
        self.agent_controls_panel.clear_workspace_btn.clicked.connect(self.clear_workspace)
        self.agent_controls_panel.temperature_spinbox.valueChanged.connect(self._handle_config_change)
        self.agent_controls_panel.max_turns_spinbox.valueChanged.connect(self._handle_config_change)
        self.agent_controls_panel.tool_output_limit_spinbox.valueChanged.connect(self._handle_config_change)
        self.agent_controls_panel.provider_combo.currentTextChanged.connect(self._update_model_suggestions)
        self.agent_controls_panel.model_combo.currentTextChanged.connect(self._handle_config_change)
        self.agent_controls_panel.detail_combo.currentTextChanged.connect(self._handle_config_change)
        self.agent_controls_panel.token_monitor_checkbox.stateChanged.connect(self._handle_config_change)
        self.agent_controls_panel.warning_threshold_spinbox.valueChanged.connect(self._handle_config_change)
        self.agent_controls_panel.critical_threshold_spinbox.valueChanged.connect(self._handle_config_change)
        self.agent_controls_panel.api_key_edit.textChanged.connect(self._handle_config_change)
        self.agent_controls_panel.base_url_edit.textChanged.connect(self._handle_config_change)
        self.agent_controls_panel.turn_monitor_checkbox.stateChanged.connect(self._handle_config_change)
        self.agent_controls_panel.turn_warning_threshold_spinbox.valueChanged.connect(self._handle_config_change)
        self.agent_controls_panel.turn_critical_threshold_spinbox.valueChanged.connect(self._handle_config_change)
        for checkbox in self.agent_controls_panel.tool_checkboxes.values():
            checkbox.stateChanged.connect(self._handle_config_change)
        right_layout.addWidget(self.output_panel, 4)
        right_layout.addWidget(self.query_panel)
        self.query_panel.run_btn.clicked.connect(self.run_agent)
        self.query_panel.pause_btn.clicked.connect(self.pause_agent)
        self.query_panel.restart_btn.clicked.connect(self.restart_session)
        splitter.addWidget(right_container)
        splitter.setSizes([200, 150, 1050])
        main_layout.addWidget(splitter)
        self.setup_accessibility()
        self.update_buttons()

    def setup_accessibility(self):
        """Set up accessibility features: keyboard navigation, screen reader support, tooltips."""
        self.run_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.restart_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.pause_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.agent_controls_panel.toggle_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.agent_controls_panel.set_workspace_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.agent_controls_panel.clear_workspace_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.filter_lineedit.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.filter_type_combo.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.agent_controls_panel.model_combo.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.agent_controls_panel.detail_combo.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.agent_controls_panel.temperature_spinbox.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.agent_controls_panel.max_turns_spinbox.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.agent_controls_panel.warning_threshold_spinbox.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.agent_controls_panel.critical_threshold_spinbox.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.agent_controls_panel.tool_output_limit_spinbox.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.agent_controls_panel.token_monitor_checkbox.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        for checkbox in self.agent_controls_panel.tool_checkboxes.values():
            checkbox.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.query_entry.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.output_textedit.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.run_btn.setAccessibleName('Run agent')
        self.run_btn.setAccessibleDescription('Start executing the agent with the current query')
        self.restart_btn.setAccessibleName('Restart session')
        self.restart_btn.setAccessibleDescription('Restart the agent session with fresh context')
        self.pause_btn.setAccessibleName('Pause agent')
        self.pause_btn.setAccessibleDescription('Pause the currently running agent')
        self.filter_lineedit.setAccessibleName('Event filter')
        self.filter_lineedit.setAccessibleDescription('Filter events by text content')
        self.filter_type_combo.setAccessibleName('Event type filter')
        self.filter_type_combo.setAccessibleDescription('Filter events by type')
        self.query_entry.setAccessibleName('Query input')
        self.query_entry.setAccessibleDescription('Enter your query for the agent')
        self.agent_controls_panel.toggle_button.setAccessibleName('Toggle controls')
        self.agent_controls_panel.toggle_button.setAccessibleDescription('Show or hide agent controls panel')
        self.agent_controls_panel.set_workspace_btn.setAccessibleName('Set workspace')
        self.agent_controls_panel.set_workspace_btn.setAccessibleDescription('Set workspace directory for agent')
        self.agent_controls_panel.clear_workspace_btn.setAccessibleName('Clear workspace')
        self.agent_controls_panel.clear_workspace_btn.setAccessibleDescription('Clear workspace restriction')
        self.agent_controls_panel.token_monitor_checkbox.setAccessibleName('Token monitor')
        self.agent_controls_panel.token_monitor_checkbox.setAccessibleDescription('Enable token usage warnings')
        self.agent_controls_panel.warning_threshold_spinbox.setAccessibleName('Warning threshold')
        self.agent_controls_panel.warning_threshold_spinbox.setAccessibleDescription('Warning threshold in thousands of tokens')
        self.agent_controls_panel.critical_threshold_spinbox.setAccessibleName('Critical threshold')
        self.agent_controls_panel.critical_threshold_spinbox.setAccessibleDescription('Critical threshold in thousands of tokens')
        self.agent_controls_panel.temperature_spinbox.setAccessibleName('Temperature')
        self.agent_controls_panel.temperature_spinbox.setAccessibleDescription('Temperature for agent responses (0.0-2.0)')
        self.agent_controls_panel.max_turns_spinbox.setAccessibleName('Max turns')
        self.agent_controls_panel.max_turns_spinbox.setAccessibleDescription('Maximum number of turns before auto-stop')
        self.agent_controls_panel.tool_output_limit_spinbox.setAccessibleName('Tool output limit')
        self.agent_controls_panel.tool_output_limit_spinbox.setAccessibleDescription('Maximum token limit for tool outputs')
        self.agent_controls_panel.model_combo.setAccessibleName('Model')
        self.agent_controls_panel.model_combo.setAccessibleDescription('Select AI model')
        self.agent_controls_panel.detail_combo.setAccessibleName('Detail level')
        self.agent_controls_panel.detail_combo.setAccessibleDescription('Detail level for agent responses')
        for name, checkbox in self.agent_controls_panel.tool_checkboxes.items():
            checkbox.setAccessibleName(f'Tool: {name}')
            checkbox.setAccessibleDescription(f'Enable or disable {name} tool')
        self.run_btn.setToolTip('Run the agent (Ctrl+R)')
        self.restart_btn.setToolTip('Restart the session (Ctrl+Shift+R)')
        self.pause_btn.setToolTip('Pause the agent (Ctrl+P)')
        self.agent_controls_panel.toggle_button.setToolTip('Show/hide agent controls (Ctrl+T)')
        self.agent_controls_panel.set_workspace_btn.setToolTip('Set workspace directory')
        self.agent_controls_panel.clear_workspace_btn.setToolTip('Clear workspace restriction')
        self.filter_lineedit.setToolTip('Filter events by text (Ctrl+F to focus, Esc to clear)')
        self.query_entry.setToolTip('Enter query for agent (Ctrl+L to focus)')
        self.filter_type_combo.setToolTip('Filter events by type')
        self.agent_controls_panel.token_monitor_checkbox.setToolTip('Enable token usage warnings')
        self.agent_controls_panel.warning_threshold_spinbox.setToolTip('Warning threshold in thousands of tokens')
        self.agent_controls_panel.critical_threshold_spinbox.setToolTip('Critical threshold in thousands of tokens')
        self.agent_controls_panel.temperature_spinbox.setToolTip('Temperature for agent responses (0.0-2.0)')
        self.agent_controls_panel.max_turns_spinbox.setToolTip('Maximum number of turns before auto-stop')
        self.agent_controls_panel.tool_output_limit_spinbox.setToolTip('Maximum token limit for tool outputs')
        self.agent_controls_panel.model_combo.setToolTip('Select AI model')
        self.agent_controls_panel.detail_combo.setToolTip('Detail level for agent responses')
        self.run_shortcut = QShortcut(QKeySequence('Ctrl+R'), self)
        self.run_shortcut.activated.connect(self.run_agent)
        self.pause_shortcut = QShortcut(QKeySequence('Ctrl+P'), self)
        self.pause_shortcut.activated.connect(self.pause_agent)
        self.restart_shortcut = QShortcut(QKeySequence('Ctrl+Shift+R'), self)
        self.restart_shortcut.activated.connect(self.restart_session)
        self.focus_query_shortcut = QShortcut(QKeySequence('Ctrl+L'), self)
        self.focus_query_shortcut.activated.connect(lambda: self.query_entry.setFocus())
        self.focus_filter_shortcut = QShortcut(QKeySequence('Ctrl+F'), self)
        self.focus_filter_shortcut.activated.connect(lambda: self.filter_lineedit.setFocus())
        self.clear_filter_shortcut = QShortcut(QKeySequence('Esc'), self.filter_lineedit)
        self.clear_filter_shortcut.activated.connect(lambda: self.filter_lineedit.clear())
        self.toggle_controls_shortcut = QShortcut(QKeySequence('Ctrl+T'), self)
        self.toggle_controls_shortcut.activated.connect(self.agent_controls_panel.toggle_collapse)
        self.setAccessibleName('Agent Workbench')
        self.setAccessibleDescription('Graphical interface for interacting with ThoughtMachine AI agent')

    def setup_signal_connections(self):
        """Connect presenter signals to GUI slots."""
        self.presenter.state_changed.connect(self.on_state_changed)
        self.presenter.tokens_updated.connect(self.on_tokens_updated)
        self.presenter.context_updated.connect(self.on_context_updated)
        self.presenter.status_message.connect(self.on_status_message)
        self.presenter.error_occurred.connect(self.on_error_occurred)
        self.presenter.config_changed.connect(self.on_config_changed)
        self.presenter.conversation_changed.connect(self.on_conversation_changed)

    @pyqtSlot(ExecutionState)
    def on_state_changed(self, state):
        """Handle agent state changes."""
        log('DEBUG', 'debug.unknown', f'on_state_changed: {state}, _closing={self._closing}')
        if self._closing:
            log('DEBUG', 'debug.unknown', 'on_state_changed: skipping due to _closing')
            return
        if state == ExecutionState.IDLE:
            self.status_panel.update_status('Ready')
            self.update_buttons(running=False)
        elif state == ExecutionState.RUNNING:
            self.status_panel.update_status('Running')
            self.update_buttons(running=True, idle=False)
        elif state == ExecutionState.PAUSED:
            self.status_panel.update_status('Paused')
            self.update_buttons(running=True, idle=True)
        elif state == ExecutionState.WAITING_FOR_USER:
            self.status_panel.update_status('Waiting for user input')
            self.update_buttons(running=True, idle=True)
            self.query_entry.setFocus()
        elif state == ExecutionState.STOPPED:
            self.status_panel.update_status('Stopped')
            self.update_buttons(running=False)
        elif state == ExecutionState.FINALIZED:
            self.status_panel.update_status('Completed')
            self.update_buttons(running=True, idle=True)
        elif state == ExecutionState.PAUSING:
            self.status_panel.update_status('Pausing…')
            self.update_buttons(running=True, idle=False)
            self.pause_btn.setEnabled(False)
        elif state == ExecutionState.STOPPING:
            self.status_panel.update_status('Stopping…')
            self.run_btn.setEnabled(False)
            self.restart_btn.setEnabled(False)
            self.pause_btn.setEnabled(False)
        elif state == ExecutionState.MAX_TURNS_REACHED:
            self.status_panel.update_status('Max turns reached')
            self.update_buttons(running=True, idle=True)

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
        main_window = self.window()
        if main_window:
            main_window.statusBar().showMessage(message, 2000)

    def _format_event_html(self, event):
        """Format event as HTML for display in QTextEdit."""
        return delegate._event_to_html(event)

    @pyqtSlot(str, str)
    def on_error_occurred(self, error_message, traceback):
        """Handle errors from presenter."""
        QMessageBox.critical(self, 'Agent Error', f'Error: {error_message}')
        if traceback:
            pass

    @pyqtSlot(dict)
    def on_config_changed(self, config):
        """Handle configuration changes from presenter."""
        pass

    @pyqtSlot()
    def on_conversation_changed(self):
        """Handle conversation changes from presenter."""
        from PyQt6.QtCore import QTimer as QTimer
        if not hasattr(self, '_conversation_debounce_timer'):
            from agent.logging import log
            log('WARNING', 'debug.unknown', f'[SessionTab] Timer not yet initialized in on_conversation_changed, skipping')
            return
        QTimer.singleShot(0, lambda: self._conversation_debounce_timer.start())

    def _on_conversation_debounced(self):
        """Debounced handler for conversation changes."""
        self.display_conversation_from_history()

    @pyqtSlot()
    def _update_gui_from_history(self):
        """Update GUI from conversation history (must be called in main thread)."""
        from agent.logging import log
        log('DEBUG', 'debug.unknown', '[GUI UPDATE] _update_gui_from_history called')
        if self.session is None:
            return
        current_version = self.session.conversation_version
        if current_version == self._last_conversation_version:
            return
        log('DEBUG', 'debug.unknown', f'GUI update: version {self._last_conversation_version} -> {current_version}, displayed count: {self._displayed_message_count}, total messages: {len(self.session.user_history)}')
        self._last_conversation_version = current_version
        messages = self.session.user_history
        new_count = 0
        for i in range(self._displayed_message_count, len(messages)):
            msg = messages[i]
            log('DEBUG', 'debug.unknown', f"GUI update: displaying message {i}: role={msg.get('role')}, type={msg.get('type')}, tool_name={msg.get('tool_name', 'N/A')}")
            if 'tool_result' in msg:
                log('DEBUG', 'debug.unknown', f"  TOOL RESULT: tool_call_id={msg.get('tool_call_id', 'N/A')}, content length={(len(msg.get('content', '')) if msg.get('content') else 0)}, is_error={msg.get('is_error', False)}")
            self.output_panel.display_message(msg)
            new_count += 1
        self._displayed_message_count = len(messages)
        log('DEBUG', 'debug.unknown', f'Displayed {new_count} new messages via GUI update')

    def run_agent(self):
        """Start or continue agent with current query."""
        query = self.query_entry.toPlainText().strip()
        config_dict = self.agent_controls_panel.get_config_dict()
        preset_name = config_dict.pop('preset_name', None)
        self.presenter.update_config(config_dict)
        current_state = self.presenter.state
        if current_state == ExecutionState.IDLE:
            if not query:
                QMessageBox.warning(self, 'No Query', 'Please enter a query first.')
                return
            self._display_turn += 1
            #self.output_panel.show_processing_indicator(query, self._display_turn)
            try:
                self.presenter.start_session(query, config_dict, preset_name=preset_name)
            except Exception as e:
                QMessageBox.critical(self, 'Session Error', f'Failed to start session: {e}')
            self.query_entry.clear()
            self.update_window_title()
        elif current_state in [ExecutionState.PAUSED, ExecutionState.WAITING_FOR_USER]:
            if query:
                self._display_turn += 1
                #self.output_panel.show_processing_indicator(query, self._display_turn)
            else:
                pass
            try:
                self.presenter.continue_session(query)
            except Exception as e:
                QMessageBox.critical(self, 'Session Error', f'Failed to continue session: {e}')
            self.query_entry.clear()
        else:
            QMessageBox.warning(self, 'Cannot Run', f'Cannot run agent in current state: {current_state}')

    def pause_agent(self):
        """Pause the current agent session."""
        self.presenter.pause_session()

    def new_session(self):
        """Start a completely new session."""
        from PyQt6.QtWidgets import QInputDialog, QMessageBox
        from datetime import datetime
        default_name = f'{datetime.now():%Y-%m-%d-%H-%M}-session'
        name, ok = QInputDialog.getText(self, 'New Session', 'Enter a name for the new session (optional):', text=default_name)
        if not ok:
            return
        self.query_entry.clear()
        try:
            self.presenter.new_session(name=name if name else None)
        except Exception as e:
            QMessageBox.critical(self, 'Session Error', f'Failed to create new session: {e}')
        self.output_panel.clear_output()
        self.total_input = 0
        self.total_output = 0
        self.context_length = 0
        self._display_turn = 0
        self.status_panel.update_tokens(0, 0)
        self.status_panel.update_context_length(0)
        self.status_panel.update_status('Ready for new session')
        self.update_buttons(running=False)
        self.update_window_title()

    def restart_session(self):
        """Restart the agent with current configuration, staying in the same session."""
        query = self.query_entry.toPlainText().strip()
        max_turn = 0
        if self.presenter.current_session and hasattr(self.presenter.current_session, 'user_history'):
            for event in self.presenter.current_session.user_history:
                turn = event.get('turn', 0)
                if turn > max_turn:
                    max_turn = turn
        self._display_turn = max_turn
        config = self.agent_controls_panel.get_config_dict()
        self.presenter.update_config(config)
        try:
            self.presenter.restart_session(query)
        except Exception as e:
            QMessageBox.critical(self, 'Session Error', f'Failed to restart session: {e}')
        self.status_panel.update_status('Ready for new session')
        self.update_buttons(running=False)
        self.update_window_title()

    def update_buttons(self, running=None, idle=False):
        """Update button states based on agent state."""
        if running is None:
            running = self.presenter.state in [ExecutionState.RUNNING, ExecutionState.PAUSED, ExecutionState.WAITING_FOR_USER]
            idle = self.presenter.state in [ExecutionState.PAUSED, ExecutionState.WAITING_FOR_USER, ExecutionState.FINALIZED]
        if running:
            if idle:
                self.run_btn.setEnabled(True)
                self.restart_btn.setEnabled(True)
                self.pause_btn.setEnabled(False)
                self.status_panel.update_status('Ready for next query')
            else:
                self.run_btn.setEnabled(False)
                self.restart_btn.setEnabled(False)
                self.pause_btn.setEnabled(True)
                self.status_panel.update_status('Running')
        else:
            self.run_btn.setEnabled(True)
            self.restart_btn.setEnabled(True)
            self.pause_btn.setEnabled(False)
            self.status_panel.update_status('Ready')

    def load_config(self):
        """Load configuration from file and update controls."""
        self._loading_config = True
        try:
            config = self.config_bridge.get_config()
            self._refresh_tools()
            self.agent_controls_panel.set_config_dict(config)
            self.presenter.update_config(config)
        except Exception as e:
            pass
        finally:
            self._loading_config = False

    def _on_config_changed(self, config):
        """Handle configuration changes from bridge (e.g., file changed)."""
        self.load_config()

    def save_config(self, immediate=False):
        """Save current configuration to file.
        
        Args:
            immediate: If True, save immediately; otherwise use debounced save
        """
        log('DEBUG', 'debug.unknown', f'save_config called: immediate={immediate}, _loading_config={self._loading_config}')
        if self._loading_config:
            return
        try:
            config = self.agent_controls_panel.get_config_dict()
            log('DEBUG', 'debug.unknown', f'save_config: config keys: {list(config.keys())}')
            self.config_bridge.save_config(config, immediate=immediate)
            log('DEBUG', 'debug.unknown', 'save_config: bridge save completed')
        except Exception as e:
            log('ERROR', 'debug.unknown', f'save_config error: {e}')

    def _update_model_suggestions(self):
        """Update model suggestions based on selected provider."""
        self.agent_controls_panel.update_model_suggestions()
        self._handle_config_change()

    def _handle_config_change(self):
        """Handle configuration change from UI controls."""
        if self._loading_config:
            return
        config = self.agent_controls_panel.get_config_dict()
        self.presenter.update_config(config)
        self._schedule_config_save()

    def _refresh_tools(self):
        """Refresh the available tools from MCP configuration."""
        import importlib
        try:
            import tools
            importlib.reload(tools)
            from tools import SIMPLIFIED_TOOL_CLASSES
            rag_enabled = self.config_bridge.config_service.get('rag_enabled', False)
            if not rag_enabled:
                filtered_tool_classes = [cls for cls in SIMPLIFIED_TOOL_CLASSES if cls.__name__ != 'SearchCodebaseTool']
            else:
                filtered_tool_classes = SIMPLIFIED_TOOL_CLASSES
            self.tool_classes = filtered_tool_classes
            self.agent_controls_panel.tool_classes = filtered_tool_classes
            self.agent_controls_panel._rebuild_tool_checkboxes()
        except Exception as e:
            pass

    def _schedule_config_save(self):
        """Schedule a debounced configuration save."""
        if not self._loading_config:
            config = self.agent_controls_panel.get_config_dict()
            self.config_bridge.save_config(config, immediate=False)

    def set_workspace(self):
        """Open dialog to select workspace directory."""
        current_workspace = self.agent_controls_panel.workspace_display.text()
        if current_workspace == 'None (unrestricted)':
            start_dir = os.getcwd()
        else:
            start_dir = current_workspace
        new_workspace = QFileDialog.getExistingDirectory(self, 'Select Workspace Directory', start_dir)
        if new_workspace:
            new_workspace = os.path.normpath(new_workspace)
            if not os.path.isabs(new_workspace):
                new_workspace = os.path.abspath(new_workspace)
            self.agent_controls_panel.workspace_display.setText(new_workspace)
            self._handle_config_change()

    def clear_workspace(self):
        """Clear workspace restriction."""
        self.agent_controls_panel.workspace_display.setText('None (unrestricted)')
        self._handle_config_change()

    def create_menu_bar(self):
        """Create the menu bar."""
        menu_bar = QMenuBar(self)
        self.setMenuBar(menu_bar)
        file_menu = menu_bar.addMenu('File')
        save_config_action = QAction('Save Configuration', self)
        save_config_action.triggered.connect(lambda: self.save_config(immediate=True))
        file_menu.addAction(save_config_action)
        load_config_action = QAction('Load Configuration', self)
        load_config_action.triggered.connect(self.load_config)
        file_menu.addAction(load_config_action)
        file_menu.addSeparator()
        export_menu = file_menu.addMenu('Export Conversation')
        export_text_action = QAction('As Plain Text', self)
        export_text_action.triggered.connect(self.export_conversation_text)
        export_menu.addAction(export_text_action)
        export_html_action = QAction('As HTML', self)
        export_html_action.triggered.connect(self.export_conversation_html)
        export_menu.addAction(export_html_action)
        export_pdf_action = QAction('As PDF', self)
        export_pdf_action.triggered.connect(self.export_conversation_pdf)
        export_menu.addAction(export_pdf_action)
        file_menu.addSeparator()
        save_session_action = QAction('Save Session', self)
        save_session_action.triggered.connect(self.save_session)
        file_menu.addAction(save_session_action)
        export_session_action = QAction('Export Session As...', self)
        export_session_action.triggered.connect(self.export_session)
        file_menu.addAction(export_session_action)
        open_session_action = QAction('Open Session...', self)
        open_session_action.triggered.connect(self.open_session)
        file_menu.addAction(open_session_action)
        manage_sessions_action = QAction('Manage Sessions...', self)
        manage_sessions_action.triggered.connect(self.manage_sessions)
        file_menu.addAction(manage_sessions_action)
        file_menu.addSeparator()
        exit_action = QAction('Exit', self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)
        view_menu = menu_bar.addMenu('View')
        theme_menu = view_menu.addMenu('Theme')
        light_theme_action = QAction('Light', self)
        light_theme_action.triggered.connect(lambda: self.set_theme('light'))
        theme_menu.addAction(light_theme_action)
        dark_theme_action = QAction('Dark', self)
        dark_theme_action.triggered.connect(lambda: self.set_theme('dark'))
        theme_menu.addAction(dark_theme_action)
        high_contrast_theme_action = QAction('High Contrast', self)
        high_contrast_theme_action.triggered.connect(lambda: self.set_theme('high_contrast'))
        theme_menu.addAction(high_contrast_theme_action)
        save_config_action.setShortcut('Ctrl+S')
        load_config_action.setShortcut('Ctrl+O')
        exit_action.setShortcut('Ctrl+Q')

    def set_theme(self, theme_name):
        """Set application theme."""
        if apply_theme(self.window(), theme_name):
            self.current_theme = theme_name
        else:
            pass

    def export_conversation_text(self):
        """Export conversation as plain text."""
        file_path, _ = QFileDialog.getSaveFileName(self, 'Export Conversation as Text', '', 'Text Files (*.txt);;All Files (*)')
        if not file_path:
            return
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                events = []
                if self.presenter.current_session and hasattr(self.presenter.current_session, 'user_history'):
                    events = self.presenter.current_session.user_history
                for event in events:
                    if event:
                        import json
                        f.write(json.dumps(event, indent=2))
                        f.write('\n' + '-' * 80 + '\n')
            self.presenter.gui_integration.emit_status_message(f'Conversation exported to {file_path}')
        except Exception as e:
            QMessageBox.critical(self, 'Export Error', f'Failed to export conversation: {e}')

    def export_conversation_html(self):
        """Export conversation as HTML."""
        file_path, _ = QFileDialog.getSaveFileName(self, 'Export Conversation as HTML', '', 'HTML Files (*.html);;All Files (*)')
        if not file_path:
            return
        try:
            html_content = '<!DOCTYPE html>\n<html>\n<head>\n    <meta charset="utf-8">\n    <title>Agent Conversation</title>\n    <style>\n        body { font-family: sans-serif; margin: 20px; }\n        .event { border: 1px solid #ddd; margin-bottom: 20px; padding: 15px; border-radius: 5px; }\n        .role { font-weight: bold; color: #333; margin-bottom: 5px; }\n        .timestamp { color: #666; font-size: 0.9em; }\n        .content { margin-top: 10px; }\n        pre { background: #f5f5f5; padding: 10px; border-radius: 3px; overflow: auto; }\n        code { font-family: monospace; }\n    </style>\n</head>\n<body>\n    <h1>Agent Conversation</h1>\n    <p>Exported on ' + datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S') + '</p>\n'
            events = []
            if self.presenter.current_session and hasattr(self.presenter.current_session, 'user_history'):
                events = self.presenter.current_session.user_history
            for event in events:
                if event:
                    role = event.get('role', 'unknown')
                    content = event.get('content', '')
                    timestamp = event.get('timestamp', '')
                    html_content += f'<div class="event">\n        <div class="role">{html.escape(role)}</div>\n'
                    if timestamp:
                        html_content += f'        <div class="timestamp">{html.escape(str(timestamp))}</div>\n'
                    formatted_content = html.escape(content).replace('                    ', '<br>                    ')
                    formatted_content = formatted_content.replace('```', '<pre><code>')
                    html_content += f'        <div class="content">{formatted_content}</div>\n    </div>\n'
            html_content += '</body>\n</html>'
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(html_content)
            self.presenter.gui_integration.emit_status_message(f'Conversation exported to {file_path}')
        except Exception as e:
            QMessageBox.critical(self, 'Export Error', f'Failed to export conversation: {e}')

    def export_conversation_pdf(self):
        """Export conversation as PDF."""
        file_path, _ = QFileDialog.getSaveFileName(self, 'Export Conversation as PDF', '', 'PDF Files (*.pdf);;All Files (*)')
        if not file_path:
            return
        try:
            doc = QTextDocument()
            html_content = '<html>\n<head>\n    <style>\n        body { font-family: sans-serif; }\n        .event { margin-bottom: 20px; }\n        .role { font-weight: bold; color: #333; }\n        .timestamp { color: #666; font-size: 0.9em; }\n        .content { margin-top: 10px; }\n    </style>\n</head>\n<body>\n    <h1>Agent Conversation</h1>\n'
            events = []
            if self.presenter.current_session and hasattr(self.presenter.current_session, 'user_history'):
                events = self.presenter.current_session.user_history
            for event in events:
                if event:
                    role = event.get('role', 'unknown')
                    content = event.get('content', '')
                    timestamp = event.get('timestamp', '')
                    html_content += f'<div class="event">\n    <div class="role">{html.escape(role)}</div>\n'
                    if timestamp:
                        html_content += f'    <div class="timestamp">{html.escape(str(timestamp))}</div>\n'
                    formatted_content = html.escape(content).replace('                    ', '<br>')
                    html_content += f'    <div class="content">{formatted_content}</div>\n</div>\n'
            html_content += '</body>\n</html>'
            doc.setHtml(html_content)
            printer = QPrinter(QPrinter.PrinterMode.HighResolution)
            printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
            printer.setOutputFileName(file_path)
            printer.setPageSize(QPageSize(QPageSize.Size.A4))
            printer.setPageOrientation(QPageLayout.Orientation.Portrait)
            doc.print(printer)
            self.presenter.gui_integration.emit_status_message(f'Conversation exported to {file_path}')
        except Exception as e:
            QMessageBox.critical(self, 'Export Error', f'Failed to export conversation: {e}')

    def save_session(self):
        """Save current session to the central session store."""
        if not self.presenter.user_history and (not self.presenter._initial_conversation):
            QMessageBox.warning(self, 'No Session', 'No conversation to save.')
            return
        try:
            if self.presenter.controller and hasattr(self.presenter.controller, 'stop'):
                self.presenter.controller.stop()
        except Exception as e:
            log('WARNING', 'debug.unknown', f'Warning: could not stop controller: {e}')
        try:
            success = self.presenter.save_session()
            if success:
                self.update_window_title()
                self.presenter.gui_integration.emit_status_message('Session saved')
            else:
                QMessageBox.warning(self, 'Save Failed', 'Failed to save session.')
        except Exception as e:
            QMessageBox.critical(self, 'Save Error', f'Failed to save session: {e}')

    def save_session_as(self):
        """Rename/relocate existing session (Save As)."""
        log('DEBUG', 'debug.unknown', f'save_session_as called, current session_name={self.presenter.session_name}')
        if not self.presenter.user_history and (not self.presenter._initial_conversation):
            QMessageBox.warning(self, 'No Session', 'No conversation to rename.')
            return
        session_id = self.presenter.current_session_id
        if not session_id:
            success = self.presenter.save_session()
            if not success:
                QMessageBox.warning(self, 'Save Failed', 'Failed to auto-save session.')
                return
            session_id = self.presenter.current_session_id
            if not session_id:
                QMessageBox.warning(self, 'Error', 'Cannot get session ID.')
                return
        default_dir = str(self.presenter.session_store.sessions_dir)
        current_name = self.presenter.session_name or 'session'
        import re
        safe_name = re.sub('[<>:"/\\\\|?*]', '_', current_name)
        default_path = os.path.join(default_dir, safe_name + '.json')
        file_path, _ = QFileDialog.getSaveFileName(self, 'Save Session As', default_path, 'Session Files (*.json);;All Files (*)')
        if not file_path:
            return
        if not file_path.lower().endswith('.json'):
            file_path += '.json'
        filename = os.path.splitext(os.path.basename(file_path))[0]
        new_name = filename.strip()
        if not new_name:
            QMessageBox.warning(self, 'Invalid Name', 'Please enter a valid session name.')
            return
        sessions_dir = Path(self.presenter.session_store.sessions_dir)
        target_path = Path(file_path)
        is_in_sessions_dir = False
        try:
            is_in_sessions_dir = target_path.parent.samefile(sessions_dir)
        except Exception as e:
            log('WARNING', 'debug.unknown', f'Error checking sessions directory: {e}')
        if is_in_sessions_dir:
            existing_sessions = self.presenter.list_sessions()
            for session in existing_sessions:
                if session.get('name', '').lower() == new_name.lower():
                    reply = QMessageBox.question(self, 'Overwrite Session?', f"A session named '{new_name}' already exists. Overwrite it?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                    if reply != QMessageBox.StandardButton.Yes:
                        return
                    break
            success = self.presenter.rename_session(session_id, new_name)
            if success:
                self.presenter.session_name = new_name
                self.update_window_title()
                self._update_tab_label()
                self.presenter.gui_integration.emit_status_message(f"Session saved as '{new_name}'")
            else:
                QMessageBox.warning(self, 'Rename Failed', 'Failed to rename session.')
        else:
            if target_path.exists():
                reply = QMessageBox.question(self, 'Overwrite File?', f"The file '{target_path.name}' already exists. Overwrite it?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                if reply != QMessageBox.StandardButton.Yes:
                    return
            success = self.presenter.export_session(file_path, set_as_external=False)
            if success:
                self.presenter.gui_integration.emit_status_message(f'Session exported to {target_path.name}')
            else:
                QMessageBox.warning(self, 'Save Failed', 'Failed to save session to the selected location.')

    def export_session(self):
        """Export current session to a file (user chooses location)."""
        if not self.presenter.user_history and (not self.presenter._initial_conversation):
            QMessageBox.warning(self, 'No Session', 'No conversation to export.')
            return
        file_path, _ = QFileDialog.getSaveFileName(self, 'Export Session As', '', 'Session Files (*.json);;All Files (*)')
        if not file_path:
            return
        if not file_path.lower().endswith('.json'):
            file_path += '.json'
        from pathlib import Path
        target_path = Path(file_path)
        if target_path.exists():
            reply = QMessageBox.question(self, 'Overwrite File?', f"The file '{target_path.name}' already exists. Overwrite it?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return
        try:
            success = self.presenter.export_session(file_path)
            if success:
                self.presenter.gui_integration.emit_status_message(f'Exported to {target_path.name}')
            else:
                QMessageBox.warning(self, 'Export Failed', 'Failed to export session.')
        except Exception as e:
            QMessageBox.critical(self, 'Export Error', f'Failed to export session: {e}')

    def open_session(self):
        """Open a session from a file and load it into a new tab."""
        default_dir = str(self.presenter.session_store.sessions_dir)
        file_path, _ = QFileDialog.getOpenFileName(self, 'Open Session', default_dir, 'Session Files (*.json);;All Files (*)')
        if not file_path:
            return
        main_window = self.window()
        if hasattr(main_window, 'open_session_in_new_tab'):
            main_window.open_session_in_new_tab(file_path)
        else:
            self._load_session_file(file_path)

    def manage_sessions(self):
        """Open a dialog to manage saved sessions."""
        sessions = self.presenter.list_sessions()
        if not sessions:
            self.presenter.gui_integration.emit_status_message('No saved sessions found.')
            return
        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QListWidget, QDialogButtonBox, QPushButton
        dialog = QDialog(self)
        dialog.setWindowTitle('Manage Sessions')
        layout = QVBoxLayout(dialog)
        list_widget = QListWidget()
        for sess in sessions:
            name = sess.get('name', sess.get('id', 'Unknown'))
            created = sess.get('created_at', '')
            preview = sess.get('preview', '')
            display_text = f'{name} - {preview}'
            list_widget.addItem(display_text)
            list_widget.item(list_widget.count() - 1).setData(Qt.ItemDataRole.UserRole, sess['id'])
        layout.addWidget(list_widget)
        list_widget.itemDoubleClicked.connect(lambda item: self._load_session_from_list_item(list_widget, item))
        button_box = QDialogButtonBox()
        rename_btn = QPushButton('Rename')
        delete_btn = QPushButton('Delete')
        close_btn = QPushButton('Close')
        button_box.addButton(rename_btn, QDialogButtonBox.ButtonRole.ActionRole)
        button_box.addButton(delete_btn, QDialogButtonBox.ButtonRole.ActionRole)
        button_box.addButton(close_btn, QDialogButtonBox.ButtonRole.RejectRole)
        rename_btn.clicked.connect(lambda: self._rename_selected_session(list_widget))
        delete_btn.clicked.connect(lambda: self._delete_selected_session(list_widget))
        close_btn.clicked.connect(dialog.accept)
        layout.addWidget(button_box)
        dialog.exec()

    def _delete_selected_session(self, list_widget):
        """Delete the session selected in the list widget."""
        current_item = list_widget.currentItem()
        if not current_item:
            return
        session_id = current_item.data(Qt.ItemDataRole.UserRole)
        if not session_id:
            return
        reply = QMessageBox.question(self, 'Confirm Delete', f"Are you sure you want to delete session '{session_id}'?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            success = self.presenter.delete_session(session_id)
            if success:
                list_widget.takeItem(list_widget.row(current_item))
            else:
                QMessageBox.warning(self, 'Delete Failed', 'Could not delete session.')

    def _rename_selected_session(self, list_widget):
        """Rename the session selected in the list widget."""
        current_item = list_widget.currentItem()
        if not current_item:
            return
        session_id = current_item.data(Qt.ItemDataRole.UserRole)
        if not session_id:
            return
        current_text = current_item.text()
        current_name = current_text.split(' - ')[0]
        from PyQt6.QtWidgets import QMessageBox
        reply = QMessageBox.question(self, 'Rename Session', f'Renaming will change the display name in the UI, but the filename will remain:            {session_id}.json                        This helps avoid filename conflicts. Continue?', QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        if reply != QMessageBox.StandardButton.Ok:
            return
        new_name, ok = QInputDialog.getText(self, 'Rename Session', 'Enter new name:', QLineEdit.EchoMode.Normal, current_name)
        if ok and new_name.strip():
            success = self.presenter.rename_session(session_id, new_name.strip())
            if success:
                preview = current_text.split(' - ', 1)[1] if ' - ' in current_text else ''
                current_item.setText(f'{new_name.strip()} - {preview}')
                if self.presenter.current_session and self.presenter.current_session.session_id == session_id:
                    self.presenter.session_name = new_name.strip()
                    self.update_window_title()
                self.presenter.gui_integration.emit_status_message(f"Session renamed to '{new_name.strip()}' (filename unchanged)")
            else:
                QMessageBox.warning(self, 'Rename Failed', 'Could not rename session.')

    def _load_session_file(self, file_path: str) -> bool:
        """Load a session from a file and update the UI.

        Returns True if successful, False otherwise.
        """
        from PyQt6.QtWidgets import QMessageBox
        try:
            if self.presenter.controller and hasattr(self.presenter.controller, 'stop'):
                self.presenter.controller.stop()
        except Exception as e:
            pass
        success = self.presenter.load_session(file_path)
        if success:
            self.display_conversation_from_history()
            self.update_window_title()
            self.presenter.gui_integration.emit_status_message(f'Session loaded from {file_path}')
        else:
            QMessageBox.warning(self, 'Load Failed', 'Failed to load session file.')
        return success

    def _load_session_from_list_item(self, list_widget, item):
        """Load the session represented by the given list item (from double‑click)."""
        session_id = item.data(Qt.ItemDataRole.UserRole)
        if not session_id:
            return
        try:
            file_path = self.presenter.session_store.get_session_path(session_id)
            self._load_session_file(str(file_path))
        except Exception as e:
            QMessageBox.critical(self, 'Load Error', f'Failed to load session: {e}')

    def _find_tab_widget(self):
        """Find the QTabWidget that contains this session tab."""
        from PyQt6.QtWidgets import QTabWidget
        parent = self.parent()
        while parent is not None:
            if isinstance(parent, QTabWidget):
                log('DEBUG', 'debug.unknown', f'_find_tab_widget: found QTabWidget at {parent}')
                return parent
            parent = parent.parent()
        main_window = self.window()
        if main_window:
            tab_widgets = main_window.findChildren(QTabWidget)
            if tab_widgets:
                log('DEBUG', 'debug.unknown', f'_find_tab_widget: fallback found {len(tab_widgets)} QTabWidgets')
                return tab_widgets[0]
        log('DEBUG', 'debug.unknown', '_find_tab_widget: no QTabWidget found')
        return None

    def update_window_title(self):
        """Update the main window title to reflect the current session name."""
        log('DEBUG', 'debug.unknown', f'update_window_title called, session_name={self.presenter.session_name}')
        name = self.presenter.session_name
        if not name:
            name = 'Untitled Session'
        main_window = self.window()
        if main_window and main_window != self:
            log('DEBUG', 'debug.unknown', f'Setting window title to: ThoughtMachine – {name}')
            main_window.setWindowTitle(f'ThoughtMachine – {name}')
        tab_widget = self._find_tab_widget()
        if tab_widget:
            idx = tab_widget.indexOf(self)
            log('DEBUG', 'debug.unknown', f'Tab widget found, index={idx}, name={name}')
            if idx >= 0:
                log('DEBUG', 'debug.unknown', f'Setting tab text at index {idx} to {name}')
                tab_widget.setTabText(idx, name)
        else:
            log('DEBUG', 'debug.unknown', 'No tab widget found in update_window_title')

    def _update_tab_label(self):
        """Update the tab label in the main tab widget."""
        log('DEBUG', 'debug.unknown', f'_update_tab_label called, session_name={self.presenter.session_name}')
        tab_widget = self._find_tab_widget()
        if tab_widget:
            idx = tab_widget.indexOf(self)
            log('DEBUG', 'debug.unknown', f'_update_tab_label: tab widget found, index={idx}')
            if idx >= 0:
                name = self.presenter.session_name or 'Untitled'
                log('DEBUG', 'debug.unknown', f'_update_tab_label: setting tab text to {name}')
                tab_widget.setTabText(idx, name)
        else:
            log('DEBUG', 'debug.unknown', '_update_tab_label: no tab widget found')

    def _auto_save_session(self):
        """Auto-save the current session periodically."""
        try:
            success = self.presenter.auto_save_current_session()
            if success:
                self.update_window_title()
        except Exception as e:
            pass

    def closeEvent(self, event):
        """Handle closing the tab with save/discard prompts for unsaved changes."""
        log('DEBUG', 'debug.unknown', 'closeEvent: started')
        if self._closing:
            log('DEBUG', 'debug.unknown', 'closeEvent: already closing, ignoring')
            event.ignore()
            return
        self._closing = True
        try:
            self.presenter.state_changed.disconnect(self.on_state_changed)
            self.presenter.tokens_updated.disconnect(self.on_tokens_updated)
            self.presenter.context_updated.disconnect(self.on_context_updated)
            self.presenter.status_message.disconnect(self.on_status_message)
            self.presenter.error_occurred.disconnect(self.on_error_occurred)
            self.presenter.config_changed.disconnect(self.on_config_changed)
        except Exception as e:
            log('WARNING', 'debug.unknown', f'closeEvent: error disconnecting signals: {e}')
        from PyQt6.QtWidgets import QInputDialog
        self._auto_save_timer.stop()
        log('DEBUG', 'debug.unknown', f'closeEvent: attempting to save session, user_history length={(len(self.presenter.user_history) if self.presenter.user_history else 0)}, current_session_id={self.presenter.current_session_id}')
        log('DEBUG', 'debug.unknown', 'closeEvent: proceeding with closing')
        log('DEBUG', 'debug.unknown', 'closeEvent: calling save_config')
        self.save_config(immediate=True)
        log('DEBUG', 'debug.unknown', 'closeEvent: save_config returned')
        if self.presenter.controller.is_running:
            log('DEBUG', 'debug.unknown', 'closeEvent: stopping controller')
            self.presenter.controller.stop()
            log('DEBUG', 'debug.unknown', 'closeEvent: controller stopped')
        log('DEBUG', 'debug.unknown', 'closeEvent: attempting to save session')
        try:
            self.presenter.save_session()
            log('DEBUG', 'debug.unknown', 'closeEvent: save_session completed')
        except Exception as e:
            log('ERROR', 'debug.unknown', f'closeEvent: save_session failed: {e}')
        log('DEBUG', 'debug.unknown', 'closeEvent: calling presenter.cleanup')
        self.presenter.cleanup()
        log('DEBUG', 'debug.unknown', 'closeEvent: presenter.cleanup returned')
        parent = self.parent()
        if parent and hasattr(parent, 'removeTab'):
            idx = parent.indexOf(self)
            if idx >= 0:
                parent.removeTab(idx)
        self.deleteLater()
        event.accept()
        super().closeEvent(event)