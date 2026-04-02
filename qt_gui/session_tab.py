"""Session Tab - Individual session tab widget for the ThoughtMachine GUI."""
import sys
import os
import json
import html
import datetime
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QTextEdit, QListWidget, QStyledItemDelegate,
    QGroupBox, QCheckBox, QMenuBar, QMenu, QFileDialog, QStyleOptionViewItem, 
    QMessageBox, QScrollArea, QFrame, QComboBox, QSpinBox, QDoubleSpinBox, QSplitter, QTabWidget, QDialog, QSizePolicy, QStyle, QInputDialog
)
from PyQt6.QtCore import Qt, QTimer, pyqtSlot, QAbstractListModel, QModelIndex, QVariant, QRect, QPoint, QSize, QSortFilterProxyModel
from PyQt6.QtGui import QAction, QKeySequence, QFont, QTextDocument, QTextCursor, QColor, QPainter, QPalette, QAbstractTextDocumentLayout, QPageLayout, QPageSize, QShortcut
from PyQt6.QtPrintSupport import QPrinter
from dotenv import load_dotenv

from agent.presenter.agent_presenter import RefactoredAgentPresenter
from agent.core.state import ExecutionState
from agent.config.service import create_agent_config_service
from qt_gui.config.config_bridge import GUIConfigBridge
from session.store import FileSystemSessionStore
from tools import TOOL_CLASSES, SIMPLIFIED_TOOL_CLASSES

load_dotenv()

# Debug logging
import datetime
from pathlib import Path
DEBUG_LOG_PATH = Path("debug_close.log")
def debug_log(msg: str):
    timestamp = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] {msg}\n")
    # print(f"[DEBUG] {msg}")  # Also print for immediate visibility

from qt_gui.utils.constants import MAX_RESULT_LENGTH

from qt_gui.themes import apply_theme
from qt_gui.panels.output_panel import OutputPanel
from qt_gui.panels.query_panel import QueryPanel
from qt_gui.panels.status_panel import StatusPanel
from qt_gui.panels.agent_controls import AgentControlsPanel
from qt_gui.panels.event_models import EventDelegate, EventModel, EventFilterProxyModel

# Import the extracted panels that were previously in qt_gui_refactored.py
# (ToolLoaderPanel and StatusPanel are already in qt_gui.panels)

# --- Main GUI class (refactored to use Presenter) ---
class SessionTab(QWidget):
    def __init__(self, parent=None, session_store=None, session_id=None):
        super().__init__(parent)

        # Session ownership - Tab owns the Session
        self.session = None
        
        # Initialize presenter and config service
        self.presenter = RefactoredAgentPresenter()
        if session_store is not None:
            self.presenter.session_store = session_store
        self.config_bridge = GUIConfigBridge(create_agent_config_service())
        self.config_bridge.add_change_listener(self._on_config_changed)

        # Token tracking (now managed by presenter but also cached locally for UI)
        self.total_input = 0
        self.total_output = 0
        self.context_length = 0
        self.current_theme = None

        # State tracking
        self.last_history = None
        self._cached_config = None  # Config created by restart_session for next run
        self._display_turn = 0  # Counter for GUI grouping of events per user query
        self._display_retry_count = 0  # Counter for deferred display retries

        self._loading_config = False  # Flag to prevent save during load
        self._closing = False  # Flag to prevent reentrant close

        # Session auto-save timer (every 2 minutes)
        self._auto_save_timer = QTimer(self)
        self._auto_save_timer.setInterval(120000)  # 2 minutes
        self._auto_save_timer.timeout.connect(self._auto_save_session)
        self._auto_save_timer.start()
        
        # Initialize output and query panels
        self.output_panel = OutputPanel(self)
        self.query_panel = QueryPanel(self)

        # Expose panel widgets for backward compatibility
        self.output_textedit = self.output_panel.output_textedit
        self.event_model = self.output_panel.event_model
        self.filter_proxy_model = self.output_panel.filter_proxy_model
        self.filter_lineedit = self.output_panel.filter_lineedit
        self.filter_type_combo = self.output_panel.filter_type_combo
        self.query_entry = self.query_panel.query_entry
        self.run_btn = self.query_panel.run_btn
        self.pause_btn = self.query_panel.pause_btn
        self.restart_btn = self.query_panel.restart_btn
        
        # Create or load session
        if session_id:
            self.load_session_by_id(session_id)
        else:
            self.create_new_session()
        
        # Conversation changed debounce timer (prevents excessive rebuilds)
        self._conversation_debounce_timer = QTimer(self)
        self._conversation_debounce_timer.setSingleShot(True)
        self._conversation_debounce_timer.setInterval(100)  # 100ms debounce
        self._conversation_debounce_timer.timeout.connect(self._on_conversation_debounced)
        
        self.init_ui()
        self.setup_signal_connections()
        self.load_config()
    def create_new_session(self):
        """Create fresh session with auto-generated name."""
        from session.models import Session, SessionConfig
        import uuid
        from datetime import datetime
        
        # Create default session config
        agent_config = self.presenter.create_agent_config()
        session_config = self.presenter._build_session_config(agent_config)
        
        # Create session with auto-generated name (ensure_name will set it)
        self.session = Session(
            session_id=str(uuid.uuid4()),
            config=session_config,
            user_history=[],
            metadata={}
        )
        # Session.ensure_name() is called in __post_init__
        
        # Bind session to presenter
        self.presenter.bind_session(self.session)
        
        # Auto-save the empty session
        self.presenter.save_session()
        self.update_window_title()
        
        if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
            print(f"[SessionTab] Created new session: {self.session.session_id}")
    
    def load_session_by_id(self, session_id: str) -> bool:
        """Load a session by ID from the session store."""
        try:
            # Load session via presenter
            success = self.presenter.load_session_by_id(session_id)
        except Exception as e:
            print(f"[SessionTab] Error loading session {session_id}: {e}")
            self.create_new_session()
            return False
        
        if success:
            # Get the loaded session from presenter
            # The session is stored in state_bridge.current_session
            self.session = self.presenter.state_bridge.current_session
            
            # Try to display, but don't fail if UI not ready
            # display_loaded_conversation will handle deferred display
            try:
                self.display_loaded_conversation()
            except Exception as e:
                print(f"[SessionTab] Error displaying session {session_id}: {e}")
                # Continue anyway - session is loaded
            
            self.update_window_title()
            
            if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                print(f"[SessionTab] Loaded session: {session_id}")
            return True
        else:
            # If loading fails, create new session
            print(f"[SessionTab] Failed to load session {session_id}, creating new")
            self.create_new_session()
            return False
    
    def init_ui(self):
        """Initialize the user interface (unchanged layout)."""
        self.update_window_title()

        main_layout = QHBoxLayout()
        self.setLayout(main_layout)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Middle panels - Status panel
        middle_container = QWidget()
        middle_layout = QVBoxLayout()
        middle_container.setLayout(middle_layout)
        self.status_panel = StatusPanel()
        middle_layout.addWidget(self.status_panel)
        middle_layout.addStretch()
        splitter.addWidget(middle_container)

        # Right panel - Use output_panel and query_panel
        right_container = QWidget()
        right_layout = QVBoxLayout()
        right_container.setLayout(right_layout)

        # Agent Controls Panel
        self.agent_controls_panel = AgentControlsPanel(SIMPLIFIED_TOOL_CLASSES)
        right_layout.addWidget(self.agent_controls_panel)

        # Set callback for MCP config changes to refresh tools
        self.agent_controls_panel.on_mcp_config_changed = self._refresh_tools

        # Connect workspace buttons
        self.agent_controls_panel.set_workspace_btn.clicked.connect(self.set_workspace)
        self.agent_controls_panel.clear_workspace_btn.clicked.connect(self.clear_workspace)

        # Connect all controls to configuration update
        self.agent_controls_panel.temperature_spinbox.valueChanged.connect(self._handle_config_change)
        self.agent_controls_panel.max_turns_spinbox.valueChanged.connect(self._handle_config_change)
        self.agent_controls_panel.tool_output_limit_spinbox.valueChanged.connect(self._handle_config_change)
        self.agent_controls_panel.provider_combo.currentTextChanged.connect(self._update_model_suggestions)
        self.agent_controls_panel.model_combo.currentTextChanged.connect(self._handle_config_change)
        self.agent_controls_panel.detail_combo.currentTextChanged.connect(self._handle_config_change)
        self.agent_controls_panel.token_monitor_checkbox.stateChanged.connect(self._handle_config_change)
        self.agent_controls_panel.warning_threshold_spinbox.valueChanged.connect(self._handle_config_change)
        self.agent_controls_panel.critical_threshold_spinbox.valueChanged.connect(self._handle_config_change)
        # API key and base URL connections
        self.agent_controls_panel.api_key_edit.textChanged.connect(self._handle_config_change)
        self.agent_controls_panel.base_url_edit.textChanged.connect(self._handle_config_change)
        # Turn monitoring connections
        self.agent_controls_panel.turn_monitor_checkbox.stateChanged.connect(self._handle_config_change)
        self.agent_controls_panel.turn_warning_threshold_spinbox.valueChanged.connect(self._handle_config_change)
        self.agent_controls_panel.turn_critical_threshold_spinbox.valueChanged.connect(self._handle_config_change)

        # Connect tool checkboxes
        for checkbox in self.agent_controls_panel.tool_checkboxes.values():
            checkbox.stateChanged.connect(self._handle_config_change)

        # Add output panel (contains filter controls and output textedit)
        right_layout.addWidget(self.output_panel, 4)  # Larger stretch factor

        # Add query panel at bottom
        right_layout.addWidget(self.query_panel)

        # Connect query panel signals
        self.query_panel.run_btn.clicked.connect(self.run_agent)
        self.query_panel.pause_btn.clicked.connect(self.pause_agent)
        self.query_panel.restart_btn.clicked.connect(self.restart_session)

        splitter.addWidget(right_container)

        # Set initial splitter sizes
        splitter.setSizes([200, 150, 1050])

        main_layout.addWidget(splitter)

        # Set up accessibility features
        self.setup_accessibility()

        # Update buttons based on initial state
        self.update_buttons()
    def setup_accessibility(self):
        """Set up accessibility features: keyboard navigation, screen reader support, tooltips."""
        # Set focus policies for interactive widgets
        # Buttons
        self.run_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.restart_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.pause_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.agent_controls_panel.toggle_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.agent_controls_panel.set_workspace_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.agent_controls_panel.clear_workspace_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # Line edit
        self.filter_lineedit.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # Combo boxes
        self.filter_type_combo.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.agent_controls_panel.model_combo.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.agent_controls_panel.detail_combo.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # Spin boxes
        self.agent_controls_panel.temperature_spinbox.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.agent_controls_panel.max_turns_spinbox.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.agent_controls_panel.warning_threshold_spinbox.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.agent_controls_panel.critical_threshold_spinbox.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.agent_controls_panel.tool_output_limit_spinbox.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # Checkboxes
        self.agent_controls_panel.token_monitor_checkbox.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        for checkbox in self.agent_controls_panel.tool_checkboxes.values():
            checkbox.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # Query entry
        self.query_entry.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # Output text area
        self.output_textedit.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # Set accessible names and descriptions
        self.run_btn.setAccessibleName("Run agent")
        self.run_btn.setAccessibleDescription("Start executing the agent with the current query")
        self.restart_btn.setAccessibleName("Restart session")
        self.restart_btn.setAccessibleDescription("Restart the agent session with fresh context")
        self.pause_btn.setAccessibleName("Pause agent")
        self.pause_btn.setAccessibleDescription("Pause the currently running agent")
        self.filter_lineedit.setAccessibleName("Event filter")
        self.filter_lineedit.setAccessibleDescription("Filter events by text content")
        self.filter_type_combo.setAccessibleName("Event type filter")
        self.filter_type_combo.setAccessibleDescription("Filter events by type")
        self.query_entry.setAccessibleName("Query input")
        self.query_entry.setAccessibleDescription("Enter your query for the agent")

        # Additional accessible names for controls
        self.agent_controls_panel.toggle_button.setAccessibleName("Toggle controls")
        self.agent_controls_panel.toggle_button.setAccessibleDescription("Show or hide agent controls panel")
        self.agent_controls_panel.set_workspace_btn.setAccessibleName("Set workspace")
        self.agent_controls_panel.set_workspace_btn.setAccessibleDescription("Set workspace directory for agent")
        self.agent_controls_panel.clear_workspace_btn.setAccessibleName("Clear workspace")
        self.agent_controls_panel.clear_workspace_btn.setAccessibleDescription("Clear workspace restriction")
        self.agent_controls_panel.token_monitor_checkbox.setAccessibleName("Token monitor")
        self.agent_controls_panel.token_monitor_checkbox.setAccessibleDescription("Enable token usage warnings")
        self.agent_controls_panel.warning_threshold_spinbox.setAccessibleName("Warning threshold")
        self.agent_controls_panel.warning_threshold_spinbox.setAccessibleDescription("Warning threshold in thousands of tokens")
        self.agent_controls_panel.critical_threshold_spinbox.setAccessibleName("Critical threshold")
        self.agent_controls_panel.critical_threshold_spinbox.setAccessibleDescription("Critical threshold in thousands of tokens")
        self.agent_controls_panel.temperature_spinbox.setAccessibleName("Temperature")
        self.agent_controls_panel.temperature_spinbox.setAccessibleDescription("Temperature for agent responses (0.0-2.0)")
        self.agent_controls_panel.max_turns_spinbox.setAccessibleName("Max turns")
        self.agent_controls_panel.max_turns_spinbox.setAccessibleDescription("Maximum number of turns before auto-stop")
        self.agent_controls_panel.tool_output_limit_spinbox.setAccessibleName("Tool output limit")
        self.agent_controls_panel.tool_output_limit_spinbox.setAccessibleDescription("Maximum token limit for tool outputs")
        self.agent_controls_panel.model_combo.setAccessibleName("Model")
        self.agent_controls_panel.model_combo.setAccessibleDescription("Select AI model")
        self.agent_controls_panel.detail_combo.setAccessibleName("Detail level")
        self.agent_controls_panel.detail_combo.setAccessibleDescription("Detail level for agent responses")
        # Tool checkboxes
        for name, checkbox in self.agent_controls_panel.tool_checkboxes.items():
            checkbox.setAccessibleName(f"Tool: {name}")
            checkbox.setAccessibleDescription(f"Enable or disable {name} tool")

        # Set tooltips
        self.run_btn.setToolTip("Run the agent (Ctrl+R)")
        self.restart_btn.setToolTip("Restart the session (Ctrl+Shift+R)")
        self.pause_btn.setToolTip("Pause the agent (Ctrl+P)")
        self.agent_controls_panel.toggle_button.setToolTip("Show/hide agent controls (Ctrl+T)")
        self.agent_controls_panel.set_workspace_btn.setToolTip("Set workspace directory")
        self.agent_controls_panel.clear_workspace_btn.setToolTip("Clear workspace restriction")
        self.filter_lineedit.setToolTip("Filter events by text (Ctrl+F to focus, Esc to clear)")
        self.query_entry.setToolTip("Enter query for agent (Ctrl+L to focus)")
        self.filter_type_combo.setToolTip("Filter events by type")
        self.agent_controls_panel.token_monitor_checkbox.setToolTip("Enable token usage warnings")
        self.agent_controls_panel.warning_threshold_spinbox.setToolTip("Warning threshold in thousands of tokens")
        self.agent_controls_panel.critical_threshold_spinbox.setToolTip("Critical threshold in thousands of tokens")
        self.agent_controls_panel.temperature_spinbox.setToolTip("Temperature for agent responses (0.0-2.0)")
        self.agent_controls_panel.max_turns_spinbox.setToolTip("Maximum number of turns before auto-stop")
        self.agent_controls_panel.tool_output_limit_spinbox.setToolTip("Maximum token limit for tool outputs")
        self.agent_controls_panel.model_combo.setToolTip("Select AI model")
        self.agent_controls_panel.detail_combo.setToolTip("Detail level for agent responses")

        # Set tab order (logical top-to-bottom, left-to-right)
        # Let Qt handle default tab order based on widget creation order.
        # We'll ensure order by setting focus proxies if needed.

        # Add keyboard shortcuts
        self.run_shortcut = QShortcut(QKeySequence("Ctrl+R"), self)
        self.run_shortcut.activated.connect(self.run_agent)
        self.pause_shortcut = QShortcut(QKeySequence("Ctrl+P"), self)
        self.pause_shortcut.activated.connect(self.pause_agent)
        self.restart_shortcut = QShortcut(QKeySequence("Ctrl+Shift+R"), self)
        self.restart_shortcut.activated.connect(self.restart_session)

        # Additional keyboard shortcuts
        self.focus_query_shortcut = QShortcut(QKeySequence("Ctrl+L"), self)
        self.focus_query_shortcut.activated.connect(lambda: self.query_entry.setFocus())
        self.focus_filter_shortcut = QShortcut(QKeySequence("Ctrl+F"), self)
        self.focus_filter_shortcut.activated.connect(lambda: self.filter_lineedit.setFocus())
        self.clear_filter_shortcut = QShortcut(QKeySequence("Esc"), self.filter_lineedit)
        self.clear_filter_shortcut.activated.connect(lambda: self.filter_lineedit.clear())
        self.toggle_controls_shortcut = QShortcut(QKeySequence("Ctrl+T"), self)
        self.toggle_controls_shortcut.activated.connect(self.agent_controls_panel.toggle_collapse)

        # Set window accessible name
        self.setAccessibleName("Agent Workbench")
        self.setAccessibleDescription("Graphical interface for interacting with ThoughtMachine AI agent")
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
        self.presenter.conversation_changed.connect(self.on_conversation_changed)

    # ----- Signal Handlers -----

    @pyqtSlot(ExecutionState)
    def on_state_changed(self, state):
        """Handle agent state changes."""
        debug_log(f"on_state_changed: {state}, _closing={self._closing}")
        # print(f"[GUI] State changed to: {state}")
        if self._closing:
            debug_log("on_state_changed: skipping due to _closing")
            return

        # Update UI based on state
        if state == ExecutionState.IDLE:
            self.status_panel.update_status("Ready")
            self.update_buttons(running=False)
        elif state == ExecutionState.RUNNING:
            self.status_panel.update_status("Running")
            self.update_buttons(running=True, idle=False)
        elif state == ExecutionState.PAUSED:
            self.status_panel.update_status("Paused")
            self.update_buttons(running=True, idle=True)
        elif state == ExecutionState.WAITING_FOR_USER:
            self.status_panel.update_status("Waiting for user input")
            self.update_buttons(running=True, idle=True)
            # Auto-focus query input
            self.query_entry.setFocus()
        elif state == ExecutionState.STOPPED:
            self.status_panel.update_status("Stopped")
            self.update_buttons(running=False)
        elif state == ExecutionState.FINALIZED:
            self.status_panel.update_status("Completed")
            self.update_buttons(running=True, idle=True)
        elif state == ExecutionState.PAUSING:
            self.status_panel.update_status("Pausing…")
            self.update_buttons(running=True, idle=False)
            self.pause_btn.setEnabled(False)
        elif state == ExecutionState.STOPPING:
            self.status_panel.update_status("Stopping…")
            # Disable all buttons during stop
            self.run_btn.setEnabled(False)
            self.restart_btn.setEnabled(False)
            self.pause_btn.setEnabled(False)
        elif state == ExecutionState.MAX_TURNS_REACHED:
            self.status_panel.update_status("Max turns reached")
            self.update_buttons(running=True, idle=True)

    @pyqtSlot(dict)
    def display_event(self, event):
        """Display an event from presenter (similar to original display_event)."""
        import os
        debug_enabled = os.environ.get('THOUGHTMACHINE_DEBUG') == '1'
        etype = event["type"]
        if debug_enabled:
            print(f"[SessionTab] display_event: type={etype}, content preview={str(event.get('content', ''))[:50]}...")
            print(f"[SessionTab] Event model has {self.event_model.rowCount()} events total")
        if etype == "token_update":
            # Token updates are handled by tokens_updated signal, skip display
            return
        detail_level = self.agent_controls_panel.detail_combo.currentText()

        # Store conversation history if present
        # print(f"[GUI] display_event: checking history, etype={etype}, has_history={'history' in event}")
        if "history" in event:
            self.last_history = event["history"]

        # Add detail level to event for rendering
        event_with_detail = event.copy()
        event_with_detail["_detail_level"] = detail_level

        # Delegate to output panel for display
        self.output_panel.display_event(event_with_detail)

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
        # Show message in main window status bar for 2 seconds
        main_window = self.window()
        if main_window:
            main_window.statusBar().showMessage(message, 2000)

    def _format_event_html(self, event):
        """Format event as HTML for display in QTextEdit."""
        delegate = EventDelegate()
        return delegate._event_to_html(event)



    @pyqtSlot(str, str)
    def on_error_occurred(self, error_message, traceback):
        """Handle errors from presenter."""
        QMessageBox.critical(self, "Agent Error", f"Error: {error_message}")
        if traceback:
            # print(f"[GUI] Error traceback: {traceback}")
            pass

    @pyqtSlot(dict)
    def on_config_changed(self, config):
        """Handle configuration changes from presenter."""
        # Update UI controls if needed
        pass
    
    @pyqtSlot()
    def on_conversation_changed(self):
        """Handle conversation changes from presenter."""
        # Debounce to prevent excessive rebuilds
        self._conversation_debounce_timer.start()
    
    def _on_conversation_debounced(self):
        """Debounced handler for conversation changes."""
        # Only rebuild if agent is IDLE
        # When agent is running/paused, we get events via display_event()
        if self.presenter.state != ExecutionState.IDLE:
            return
        
        # Refresh conversation display
        self.display_loaded_conversation()
    # ----- Agent Control Methods -----
    
    def run_agent(self):
        """Start or continue agent with current query."""
        query = self.query_entry.toPlainText().strip()

        # Get current configuration from controls
        config_dict = self.agent_controls_panel.get_config_dict()
        
        # Extract preset_name if present (it will be passed separately)
        preset_name = config_dict.pop('preset_name', None)

        # Update presenter configuration
        self.presenter.update_config(config_dict)

        # Check current state to decide action
        current_state = self.presenter.state

        if current_state == ExecutionState.IDLE:
            # Start new session - require query
            if not query:
                QMessageBox.warning(self, "No Query", "Please enter a query first.")
                return
            # Increment turn counter for new user query
            self._display_turn += 1
            self.display_user_query(query)
            self.presenter.start_session(query, config_dict, preset_name=preset_name)
            self.query_entry.clear()
            self.update_window_title()

        elif current_state in [ExecutionState.PAUSED, ExecutionState.WAITING_FOR_USER]:
            # Continue existing session - allow empty query (resume without new input)
            if query:
                # New user input when continuing - increment turn counter
                self._display_turn += 1
                self.display_user_query(query)
            else:
                # Display a placeholder for empty resume (no new turn)
                self.display_user_query("(resumed)")
            self.presenter.continue_session(query)
            self.query_entry.clear()

        else:
            QMessageBox.warning(self, "Cannot Run",
                               f"Cannot run agent in current state: {current_state}")    
    def pause_agent(self):
        """Pause the current agent session."""
        self.presenter.pause_session()
    
    def new_session(self):
        """Start a completely new session."""
        from PyQt6.QtWidgets import QInputDialog, QMessageBox
        
        
        # Ask for new session name (optional) with default suggestion
        from datetime import datetime
        default_name = f"{datetime.now():%Y-%m-%d-%H-%M}-session"
        name, ok = QInputDialog.getText(
            self, "New Session", 
            "Enter a name for the new session (optional):",
            text=default_name
        )
        
        if not ok:
            return  # User cancelled
        
        # Clear the query entry
        self.query_entry.clear()
        # In presenter, this will stop agent if running and clear session data
        self.presenter.new_session(name=name if name else None)
        # Clear UI components
        self.output_panel.clear_output()
        # Reset token counters and turn counter
        self.total_input = 0
        self.total_output = 0
        self.context_length = 0
        self._display_turn = 0  # Reset turn counter for new session
        self.status_panel.update_tokens(0, 0)
        self.status_panel.update_context_length(0)
        # Update UI
        self.status_panel.update_status("Ready for new session")
        self.update_buttons(running=False)
        self.update_window_title()

    def restart_session(self):
        """Restart the agent with current configuration, staying in the same session."""
        # Get current query (it may be used by presenter? but not needed)
        query = self.query_entry.toPlainText().strip()

        # Sync turn counter with existing events before restart
        from PyQt6.QtCore import Qt
        max_turn = 0
        for i in range(self.event_model.rowCount()):
            index = self.event_model.index(i, 0)
            event = self.event_model.data(index, Qt.ItemDataRole.UserRole)
            if event:
                turn = event.get('turn', 0)
                if turn > max_turn:
                    max_turn = turn
        self._display_turn = max_turn  # Next query will increment from here

        # Update presenter config with current UI config before restart
        config = self.agent_controls_panel.get_config_dict()
        self.presenter.update_config(config)
        
        # Restart the agent (preserves session and conversation)
        self.presenter.restart_session(query)

        # Update UI status (token counters remain as they represent cumulative session totals)
        self.status_panel.update_status("Ready for new session")
        self.update_buttons(running=False)
        # Note: we do NOT clear the chat display; the conversation history remains visible.
        self.update_window_title()    
    # ----- UI Helper Methods -----
    
    def update_buttons(self, running=None, idle=False):
        """Update button states based on agent state."""
        if running is None:
            running = self.presenter.state in [
                ExecutionState.RUNNING,
                ExecutionState.PAUSED,
                ExecutionState.WAITING_FOR_USER            ]
            idle = self.presenter.state in [
                ExecutionState.PAUSED,
                ExecutionState.WAITING_FOR_USER,
                ExecutionState.FINALIZED
            ]
        
        # print(f"[GUI] update_buttons(running={running}, idle={idle}), state={self.presenter.state}")
        
        if running:
            if idle:
                self.run_btn.setEnabled(True)
                self.restart_btn.setEnabled(True)
                self.pause_btn.setEnabled(False)  # Already paused
                self.status_panel.update_status("Ready for next query")
            else:
                self.run_btn.setEnabled(False)
                self.restart_btn.setEnabled(False)
                self.pause_btn.setEnabled(True)
                self.status_panel.update_status("Running")
        else:
            self.run_btn.setEnabled(True)
            self.restart_btn.setEnabled(True)
            self.pause_btn.setEnabled(False)
            self.status_panel.update_status("Ready")
    
    def display_user_query(self, query, turn=None):
        """Display a user query in the output area."""
        # print(f"[GUI] display_user_query: '{query[:50]}...'")
        # Create a synthetic event for user query
        event = {
            "type": "user_query",
            "content": query,
            "turn": self._display_turn if turn is None else turn,
            "_detail_level": self.agent_controls_panel.detail_combo.currentText()
        }
        # Delegate to output panel for display
        self.output_panel.display_event(event)
    
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
    
    
    def load_config(self):
        """Load configuration from file and update controls."""
        self._loading_config = True
        
        try:
            # Load config from bridge
            config = self.config_bridge.get_config()
            
            # Update controls
            self.agent_controls_panel.set_config_dict(config)
            
            # Update presenter configuration
            self.presenter.update_config(config)
            
            # print("[GUI] Configuration loaded")
            
        except Exception as e:
            # print(f"[GUI] Error loading config: {e}")
            pass
        finally:
            self._loading_config = False
    
    def _on_config_changed(self, config):
        """Handle configuration changes from bridge (e.g., file changed)."""
        # Update UI with new config
        self.load_config()
    
    def save_config(self, immediate=False):
        """Save current configuration to file.
        
        Args:
            immediate: If True, save immediately; otherwise use debounced save
        """
        debug_log(f"save_config called: immediate={immediate}, _loading_config={self._loading_config}")
        print(f"[SessionTab] save_config called, immediate={immediate}")
        if self._loading_config:
            return

        try:
            config = self.agent_controls_panel.get_config_dict()
            debug_log(f"save_config: config keys: {list(config.keys())}")
            # print(f"[GUI] Saving config: {config} (immediate={immediate})")
            # Use config bridge for saving
            self.config_bridge.save_config(config, immediate=immediate)
            debug_log("save_config: bridge save completed")
            # print("[GUI] Configuration saved via bridge")
        except Exception as e:
            debug_log(f"save_config error: {e}")
            # print(f"[GUI] Error saving config: {e}")    
    def _update_model_suggestions(self):
        """Update model suggestions based on selected provider."""
        # Delegate to the controls panel's method
        self.agent_controls_panel.update_model_suggestions()
        
        # Also trigger config change since provider changed
        self._handle_config_change()
    
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
        
    def _refresh_tools(self):
        """Refresh the available tools from MCP configuration."""
        import importlib
        try:
            import tools
            importlib.reload(tools)
            from tools import TOOL_CLASSES
            self.tool_classes = TOOL_CLASSES
            self.agent_controls_panel.tool_classes = TOOL_CLASSES
            self.agent_controls_panel._rebuild_tool_checkboxes()
            # print(f"[GUI] Refreshed tools: {len(TOOL_CLASSES)} tools loaded")
        except Exception as e:
            # print(f"[GUI] Error refreshing tools: {e}")
            pass
    def _schedule_config_save(self):
        """Schedule a debounced configuration save."""
        if not self._loading_config:
            # Get current config from controls and save via bridge
            config = self.agent_controls_panel.get_config_dict()
            self.config_bridge.save_config(config, immediate=False)
    
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
        save_config_action.triggered.connect(lambda: self.save_config(immediate=True))
        file_menu.addAction(save_config_action)
        
        load_config_action = QAction("Load Configuration", self)
        load_config_action.triggered.connect(self.load_config)
        file_menu.addAction(load_config_action)
        
        file_menu.addSeparator()
        
        # Export submenu
        export_menu = file_menu.addMenu("Export Conversation")
        
        export_text_action = QAction("As Plain Text", self)
        export_text_action.triggered.connect(self.export_conversation_text)
        export_menu.addAction(export_text_action)
        
        export_html_action = QAction("As HTML", self)
        export_html_action.triggered.connect(self.export_conversation_html)
        export_menu.addAction(export_html_action)
        
        export_pdf_action = QAction("As PDF", self)
        export_pdf_action.triggered.connect(self.export_conversation_pdf)
        export_menu.addAction(export_pdf_action)
        
        file_menu.addSeparator()
        # Session management actions
        save_session_action = QAction("Save Session", self)
        save_session_action.triggered.connect(self.save_session)
        file_menu.addAction(save_session_action)

        export_session_action = QAction("Export Session As...", self)
        export_session_action.triggered.connect(self.export_session)
        file_menu.addAction(export_session_action)

        open_session_action = QAction("Open Session...", self)
        open_session_action.triggered.connect(self.open_session)
        file_menu.addAction(open_session_action)

        manage_sessions_action = QAction("Manage Sessions...", self)
        manage_sessions_action.triggered.connect(self.manage_sessions)
        file_menu.addAction(manage_sessions_action)

        file_menu.addSeparator()

        
        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)
        
        # View menu
        view_menu = menu_bar.addMenu("View")
        
        # Theme submenu
        theme_menu = view_menu.addMenu("Theme")
        
        light_theme_action = QAction("Light", self)
        light_theme_action.triggered.connect(lambda: self.set_theme("light"))
        theme_menu.addAction(light_theme_action)
        
        dark_theme_action = QAction("Dark", self)
        dark_theme_action.triggered.connect(lambda: self.set_theme("dark"))
        theme_menu.addAction(dark_theme_action)
        
        high_contrast_theme_action = QAction("High Contrast", self)
        high_contrast_theme_action.triggered.connect(lambda: self.set_theme("high_contrast"))
        theme_menu.addAction(high_contrast_theme_action)
        
        # Keyboard shortcuts
        save_config_action.setShortcut("Ctrl+S")
        load_config_action.setShortcut("Ctrl+O")
        exit_action.setShortcut("Ctrl+Q")
    
    # ----- Theme Methods -----
    
    def set_theme(self, theme_name):
        """Set application theme."""
        if apply_theme(self.window(), theme_name):
            self.current_theme = theme_name
            # print(f"[GUI] Theme set to: {theme_name}")
        else:
            # print(f"[GUI] Unknown theme: {theme_name}")
            pass
    
    # ----- Export Methods -----
    
    def export_conversation_text(self):
        """Export conversation as plain text."""
        file_path, _ = QFileDialog.getSaveFileName(self, "Export Conversation as Text", "", "Text Files (*.txt);;All Files (*)")
        if not file_path:
            return
        
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                # Get all events from the model
                for i in range(self.event_model.rowCount()):
                    event = self.event_model.data(self.event_model.index(i), Qt.ItemDataRole.UserRole)
                    if event:
                        # Use delegate's plain text conversion method
                        delegate = EventDelegate()
                        if hasattr(delegate, '_event_to_plain_text'):
                            plain_text = delegate._event_to_plain_text(event)
                            f.write(plain_text)
                            f.write('\n' + '-'*80 + '\n\n')
                        else:
                            # Fallback to JSON representation
                            import json
                            f.write(json.dumps(event, indent=2))
                            f.write('\n' + '-'*80 + '\n\n')
            
            self.presenter.gui_integration.emit_status_message(f"Conversation exported to {file_path}")
        except Exception as e:
            QMessageBox.critical(self, "Export Error", f"Failed to export conversation: {e}")
    
    def export_conversation_html(self):
        """Export conversation as HTML."""
        file_path, _ = QFileDialog.getSaveFileName(self, "Export Conversation as HTML", "", "HTML Files (*.html);;All Files (*)")
        if not file_path:
            return
        
        try:
            html_content = '''<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Agent Conversation</title>
    <style>
        body { font-family: sans-serif; margin: 20px; }
        .event { border: 1px solid #ddd; margin-bottom: 20px; padding: 15px; border-radius: 5px; }
        .role { font-weight: bold; color: #333; margin-bottom: 5px; }
        .timestamp { color: #666; font-size: 0.9em; }
        .content { margin-top: 10px; }
        pre { background: #f5f5f5; padding: 10px; border-radius: 3px; overflow: auto; }
        code { font-family: monospace; }
    </style>
</head>
<body>
    <h1>Agent Conversation</h1>
    <p>Exported on ''' + datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S') + '''</p>
'''
            
            # Get all events from the model
            for i in range(self.event_model.rowCount()):
                event = self.event_model.data(self.event_model.index(i), Qt.ItemDataRole.UserRole)
                if event:
                    role = event.get('role', 'unknown')
                    content = event.get('content', '')
                    timestamp = event.get('timestamp', '')
                    
                    # Escape HTML and wrap in appropriate tags
                    html_content += f'''<div class="event">
        <div class="role">{html.escape(role)}</div>
'''
                    if timestamp:
                        html_content += f'''        <div class="timestamp">{html.escape(str(timestamp))}</div>
'''
                    
                    # Format content - preserve line breaks and code blocks
                    formatted_content = html.escape(content).replace('\n', '<br>\n')
                    # Simple code block detection
                    formatted_content = formatted_content.replace('```', '<pre><code>')
                    
                    html_content += f'''        <div class="content">{formatted_content}</div>
    </div>
'''
            
            html_content += '''</body>
</html>'''
            
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(html_content)
            
            self.presenter.gui_integration.emit_status_message(f"Conversation exported to {file_path}")
        except Exception as e:
            QMessageBox.critical(self, "Export Error", f"Failed to export conversation: {e}")
    
    def export_conversation_pdf(self):
        """Export conversation as PDF."""
        file_path, _ = QFileDialog.getSaveFileName(self, "Export Conversation as PDF", "", "PDF Files (*.pdf);;All Files (*)")
        if not file_path:
            return
        
        try:
            # Create a QTextDocument for PDF rendering
            doc = QTextDocument()
            html_content = '''<html>
<head>
    <style>
        body { font-family: sans-serif; }
        .event { margin-bottom: 20px; }
        .role { font-weight: bold; color: #333; }
        .timestamp { color: #666; font-size: 0.9em; }
        .content { margin-top: 10px; }
    </style>
</head>
<body>
    <h1>Agent Conversation</h1>
'''
            
            # Get all events from the model
            for i in range(self.event_model.rowCount()):
                event = self.event_model.data(self.event_model.index(i), Qt.ItemDataRole.UserRole)
                if event:
                    role = event.get('role', 'unknown')
                    content = event.get('content', '')
                    timestamp = event.get('timestamp', '')
                    
                    html_content += f'''<div class="event">
    <div class="role">{html.escape(role)}</div>
'''
                    if timestamp:
                        html_content += f'''    <div class="timestamp">{html.escape(str(timestamp))}</div>
'''
                    
                    # Format content for PDF
                    formatted_content = html.escape(content).replace('\n', '<br>')
                    html_content += f'''    <div class="content">{formatted_content}</div>
</div>
'''
            
            html_content += '''</body>
</html>'''
            
            doc.setHtml(html_content)
            
            # Create printer and print to PDF
            printer = QPrinter(QPrinter.PrinterMode.HighResolution)
            printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
            printer.setOutputFileName(file_path)
            printer.setPageSize(QPageSize(QPageSize.Size.A4))
            printer.setPageOrientation(QPageLayout.Orientation.Portrait)
            
            # Print the document
            doc.print(printer)
            
            self.presenter.gui_integration.emit_status_message(f"Conversation exported to {file_path}")
        except Exception as e:
            QMessageBox.critical(self, "Export Error", f"Failed to export conversation: {e}")
    
    # ----- Session Management Methods -----

    def save_session(self):
        """Save current session to the central session store."""
        # Check if there is a session to save
        if not self.presenter.user_history and not self.presenter._initial_conversation:
            QMessageBox.warning(self, "No Session", "No conversation to save.")
            return

        # Stop any running controller before saving
        try:
            if self.presenter.controller and hasattr(self.presenter.controller, 'stop'):
                self.presenter.controller.stop()
        except Exception as e:
            print(f"[GUI] Warning: could not stop controller: {e}")

        try:
            success = self.presenter.save_session()
            if success:
                # session_name is updated by presenter
                self.update_window_title()
                self.presenter.gui_integration.emit_status_message("Session saved")
            else:
                QMessageBox.warning(self, "Save Failed", "Failed to save session.")
        except Exception as e:
            QMessageBox.critical(self, "Save Error", f"Failed to save session: {e}")

    def save_session_as(self):
        """Rename/relocate existing session (Save As)."""
        debug_log(f"save_session_as called, current session_name={self.presenter.session_name}")
        # Check if there is a session to rename
        if not self.presenter.user_history and not self.presenter._initial_conversation:
            QMessageBox.warning(self, "No Session", "No conversation to rename.")
            return
        
        # Get current session ID
        session_id = self.presenter.current_session_id
        if not session_id:
            # Session hasn't been saved yet; auto-save it first
            success = self.presenter.save_session()
            if not success:
                QMessageBox.warning(self, "Save Failed", "Failed to auto-save session.")
                return
            session_id = self.presenter.current_session_id
            if not session_id:
                QMessageBox.warning(self, "Error", "Cannot get session ID.")
                return
        
        # Use file dialog to get new name (with folder navigation)
        default_dir = str(self.presenter.session_store.sessions_dir)
        current_name = self.presenter.session_name or "session"
        # Clean current name for filename: remove invalid characters
        import re
        safe_name = re.sub(r'[<>:"/\\|?*]', '_', current_name)
        default_path = os.path.join(default_dir, safe_name + ".json")
        
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Save Session As", default_path, "Session Files (*.json);;All Files (*)"
        )
        if not file_path:
            return
        
        # Ensure .json extension
        if not file_path.lower().endswith('.json'):
            file_path += '.json'
        
        # Extract name from filename (without extension) for session metadata
        filename = os.path.splitext(os.path.basename(file_path))[0]
        new_name = filename.strip()
        if not new_name:
            QMessageBox.warning(self, "Invalid Name", "Please enter a valid session name.")
            return
        
        # Check if saving within the sessions directory
        sessions_dir = Path(self.presenter.session_store.sessions_dir)
        target_path = Path(file_path)
        
        is_in_sessions_dir = False
        try:
            is_in_sessions_dir = target_path.parent.samefile(sessions_dir)
        except Exception as e:
            # If path comparison fails, assume not in sessions directory
            print(f"[SessionTab] Error checking sessions directory: {e}")
        
        if is_in_sessions_dir:
            # User is saving to the sessions directory (rename session)
            # Check if a session with this name already exists
            existing_sessions = self.presenter.list_sessions()
            for session in existing_sessions:
                if session.get('name', '').lower() == new_name.lower():
                    reply = QMessageBox.question(
                        self, "Overwrite Session?",
                        f"A session named '{new_name}' already exists. Overwrite it?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                    )
                    if reply != QMessageBox.StandardButton.Yes:
                        return
                    break
            
            # Rename session (which will rename the file)
            success = self.presenter.rename_session(session_id, new_name)
            if success:
                self.presenter.session_name = new_name
                self.update_window_title()
                self._update_tab_label()
                # Show status message
                self.presenter.gui_integration.emit_status_message(f"Session saved as '{new_name}'")
            else:
                QMessageBox.warning(self, "Rename Failed", "Failed to rename session.")
        else:
            # Export to external location
            # Check if file already exists (QFileDialog may have warned, but we check again)
            if target_path.exists():
                reply = QMessageBox.question(
                    self, "Overwrite File?",
                    f"The file '{target_path.name}' already exists. Overwrite it?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return
            
            # Export the session
            success = self.presenter.export_session(file_path, set_as_external=False)
            if success:
                self.presenter.gui_integration.emit_status_message(f"Session exported to {target_path.name}")
            else:
                QMessageBox.warning(self, "Save Failed", "Failed to save session to the selected location.")

    def export_session(self):
        """Export current session to a file (user chooses location)."""
        # Check if there is a session to export
        if not self.presenter.user_history and not self.presenter._initial_conversation:
            QMessageBox.warning(self, "No Session", "No conversation to export.")
            return
        
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Export Session As", "", "Session Files (*.json);;All Files (*)"
        )
        if not file_path:
            return
        
        # Ensure .json extension
        if not file_path.lower().endswith('.json'):
            file_path += '.json'
        
        from pathlib import Path
        target_path = Path(file_path)
        
        # Check if file already exists (QFileDialog may have warned, but we check again)
        if target_path.exists():
            reply = QMessageBox.question(
                self, "Overwrite File?",
                f"The file '{target_path.name}' already exists. Overwrite it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        
        try:
            success = self.presenter.export_session(file_path)
            if success:
                self.presenter.gui_integration.emit_status_message(f"Exported to {target_path.name}")
            else:
                QMessageBox.warning(self, "Export Failed", "Failed to export session.")
        except Exception as e:
            QMessageBox.critical(self, "Export Error", f"Failed to export session: {e}")

    def open_session(self):
        """Open a session from a file and load it into a new tab."""
        default_dir = str(self.presenter.session_store.sessions_dir)
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Open Session", default_dir, "Session Files (*.json);;All Files (*)"
        )
        if not file_path:
            return
        # Delegate to main window to open in a new tab
        main_window = self.window()
        if hasattr(main_window, 'open_session_in_new_tab'):
            main_window.open_session_in_new_tab(file_path)
        else:
            # fallback (should not happen)
            self._load_session_file(file_path)

    def manage_sessions(self):
        """Open a dialog to manage saved sessions."""
        sessions = self.presenter.list_sessions()
        if not sessions:
            self.presenter.gui_integration.emit_status_message("No saved sessions found.")
            return

        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QListWidget, QDialogButtonBox, QPushButton
        dialog = QDialog(self)
        dialog.setWindowTitle("Manage Sessions")
        layout = QVBoxLayout(dialog)

        list_widget = QListWidget()
        for sess in sessions:
            name = sess.get('name', sess.get('id', 'Unknown'))
            created = sess.get('created_at', '')
            preview = sess.get('preview', '')
            display_text = f"{name} - {preview}"
            list_widget.addItem(display_text)
            list_widget.item(list_widget.count()-1).setData(Qt.ItemDataRole.UserRole, sess['id'])

        layout.addWidget(list_widget)

        # Double‑click to load
        list_widget.itemDoubleClicked.connect(
            lambda item: self._load_session_from_list_item(list_widget, item)
        )

        # Custom buttons: Rename and Delete, plus Close
        button_box = QDialogButtonBox()
        rename_btn = QPushButton("Rename")
        delete_btn = QPushButton("Delete")
        close_btn = QPushButton("Close")
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

        reply = QMessageBox.question(
            self, "Confirm Delete",
            f"Are you sure you want to delete session '{session_id}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            success = self.presenter.delete_session(session_id)
            if success:
                list_widget.takeItem(list_widget.row(current_item))
            else:
                QMessageBox.warning(self, "Delete Failed", "Could not delete session.")

    def _rename_selected_session(self, list_widget):
        """Rename the session selected in the list widget."""
        current_item = list_widget.currentItem()
        if not current_item:
            return
        session_id = current_item.data(Qt.ItemDataRole.UserRole)
        if not session_id:
            return

        # Get current name from display text (everything before ' - ')
        current_text = current_item.text()
        current_name = current_text.split(' - ')[0]

        # Show explanation about rename (metadata only, not filename)
        from PyQt6.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            self, "Rename Session",
            f"Renaming will change the display name in the UI, but the filename will remain:\n"
            f"{session_id}.json\n\n"
            f"This helps avoid filename conflicts. Continue?",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
        )
        if reply != QMessageBox.StandardButton.Ok:
            return
        
        new_name, ok = QInputDialog.getText(
            self, "Rename Session", "Enter new name:", QLineEdit.EchoMode.Normal, current_name
        )
        if ok and new_name.strip():
            success = self.presenter.rename_session(session_id, new_name.strip())
            if success:
                # Update item text: keep preview part
                preview = current_text.split(' - ', 1)[1] if ' - ' in current_text else ''
                current_item.setText(f"{new_name.strip()} - {preview}")
                # If this session is currently loaded, update the window title
                if self.presenter.current_session and self.presenter.current_session.session_id == session_id:
                    self.presenter.session_name = new_name.strip()
                    self.update_window_title()
                self.presenter.gui_integration.emit_status_message(f"Session renamed to '{new_name.strip()}' (filename unchanged)")
                
            else:
                QMessageBox.warning(self, "Rename Failed", "Could not rename session.")

    def display_loaded_conversation(self):
        """Display the currently loaded conversation (full user history)."""
        # If UI panels are not initialized yet, defer until they are
        if not hasattr(self, 'output_panel') or self.output_panel is None or \
           not hasattr(self, 'status_panel') or self.status_panel is None:
            # Try again after a short delay, but limit retries
            self._display_retry_count += 1
            if self._display_retry_count > 10:
                print(f"[SessionTab] Warning: Too many display retries ({self._display_retry_count}), giving up")
                return
            QTimer.singleShot(0, self.display_loaded_conversation)
            return
        
        # Reset retry counter since we're about to display successfully
        self._display_retry_count = 0
        
        # Clear current display
        self.output_panel.clear_output()

        # Use the presenter's user_history to show the full conversation
        # Update status panel with current token totals and context length from presenter
        # These reflect the loaded session's persisted values (even if conversation is empty)
        self.total_input = self.presenter.total_input
        self.total_output = self.presenter.total_output
        self.context_length = self.presenter.context_length
        self.status_panel.update_tokens(self.presenter.total_input, self.presenter.total_output)
        self.status_panel.update_context_length(self.presenter.context_length)

        conversation = self.presenter.user_history
        if not conversation:
            return

        # Build mapping of tool_call_id -> tool_name from assistant messages
        tool_call_id_to_name = {}
        for msg in conversation:
            if msg['role'] == 'assistant' and msg.get('tool_calls'):
                for tc in msg['tool_calls']:
                    func = tc.get('function', {})
                    tool_name = func.get('name', 'unknown')
                    tool_call_id_to_name[tc.get('id')] = tool_name
        
        # Collect events from conversation with simple turn numbering
        events = []
        current_turn = 0
        
        for msg in conversation:
            role = msg['role']
            content = msg['content']
            reasoning = msg.get('reasoning_content')
            tool_calls = msg.get('tool_calls')
            tool_call_id = msg.get('tool_call_id')
            
            # Start new turn on user messages
            if role == 'user':
                current_turn += 1
            
            # Assign turn number (0 for system messages, current_turn for others)
            turn = 0 if role == 'system' else current_turn
            
            # Handle system messages separately (no turn)
            if role == 'system':
                event = {
                    'type': 'system',
                    'content': content,
                    'timestamp': datetime.datetime.now().isoformat(),
                    '_detail_level': self.presenter._config.get('detail', 'normal')
                }
                events.append(event)
                continue
            
            # Handle tool messages (tool results)
            if role == 'tool':
                event = {
                    'type': 'tool_result',
                    'content': content,
                    'tool_call_id': tool_call_id,
                    'timestamp': datetime.datetime.now().isoformat(),
                    '_detail_level': self.presenter._config.get('detail', 'normal')
                }
                # Add tool_name from mapping
                if tool_call_id and tool_call_id in tool_call_id_to_name:
                    event['tool_name'] = tool_call_id_to_name[tool_call_id]
                event['turn'] = turn
                events.append(event)
                continue
            
            # For all other roles (user, assistant with or without tool_calls), use _create_chat_event
            event = self._create_chat_event(
                role, content, tool_calls, tool_call_id, reasoning=reasoning
            )
            event['turn'] = turn
            events.append(event)

        # If the session has a final_content (from Final tool), add it as a final event
        session = self.presenter.current_session
        if session and getattr(session, 'final_content', None):
            final_event = {
                'type': 'final',
                'content': session.final_content,
                'reasoning': getattr(session, 'final_reasoning', '') or '',
                '_detail_level': self.presenter._config.get('detail', 'normal')
            }
            events.append(final_event)
        
        # Delegate batch display to output panel
        if events:
            # Debug: print event count and structure
            print(f"[DEBUG] display_loaded_conversation: Created {len(events)} events from {len(conversation)} messages")
            if events:
                print(f"[DEBUG] First event type: {events[0].get('type')}, turn: {events[0].get('turn', 'N/A')}")
                print(f"[DEBUG] Event types: {[e.get('type') for e in events[:5]]}")
            self.output_panel.display_loaded_conversation(events)


    def _load_session_file(self, file_path: str) -> bool:
        """Load a session from a file and update the UI.

        Returns True if successful, False otherwise.
        """
        from PyQt6.QtWidgets import QMessageBox
        
        try:
            if self.presenter.controller and hasattr(self.presenter.controller, 'stop'):
                self.presenter.controller.stop()
        except Exception as e:
            # print(f"[GUI] Warning: could not stop controller: {e}")
            pass

        success = self.presenter.load_session(file_path)  # Auto-save is always performed
        if success:
            self.display_loaded_conversation()
            # Window title and UI updated by display_loaded_conversation
            self.update_window_title()
            self.presenter.gui_integration.emit_status_message(f"Session loaded from {file_path}")
        else:
            QMessageBox.warning(self, "Load Failed", "Failed to load session file.")
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
            QMessageBox.critical(self, "Load Error", f"Failed to load session: {e}")

    def _find_tab_widget(self):
        """Find the QTabWidget that contains this session tab."""
        from PyQt6.QtWidgets import QTabWidget
        parent = self.parent()
        while parent is not None:
            if isinstance(parent, QTabWidget):
                debug_log(f"_find_tab_widget: found QTabWidget at {parent}")
                return parent
            parent = parent.parent()
        # Fallback: search through window
        main_window = self.window()
        if main_window:
            tab_widgets = main_window.findChildren(QTabWidget)
            if tab_widgets:
                debug_log(f"_find_tab_widget: fallback found {len(tab_widgets)} QTabWidgets")
                return tab_widgets[0]
        debug_log("_find_tab_widget: no QTabWidget found")
        return None

    def update_window_title(self):
        """Update the main window title to reflect the current session name."""
        debug_log(f"update_window_title called, session_name={self.presenter.session_name}")
        name = self.presenter.session_name
        if not name:
            name = "Untitled Session"
        # Set the main window title
        main_window = self.window()
        if main_window and main_window != self:
            debug_log(f"Setting window title to: ThoughtMachine – {name}")
            main_window.setWindowTitle(f"ThoughtMachine – {name}")
        # Update tab text if we're in a QTabWidget
        tab_widget = self._find_tab_widget()
        if tab_widget:
            idx = tab_widget.indexOf(self)
            debug_log(f"Tab widget found, index={idx}, name={name}")
            if idx >= 0:
                debug_log(f"Setting tab text at index {idx} to {name}")
                tab_widget.setTabText(idx, name)
        else:
            debug_log("No tab widget found in update_window_title")

    def _update_tab_label(self):
        """Update the tab label in the main tab widget."""
        debug_log(f"_update_tab_label called, session_name={self.presenter.session_name}")
        tab_widget = self._find_tab_widget()
        if tab_widget:
            idx = tab_widget.indexOf(self)
            debug_log(f"_update_tab_label: tab widget found, index={idx}")
            if idx >= 0:
                name = self.presenter.session_name or "Untitled"
                debug_log(f"_update_tab_label: setting tab text to {name}")
                tab_widget.setTabText(idx, name)
        else:
            debug_log("_update_tab_label: no tab widget found")

    def _auto_save_session(self):
        """Auto-save the current session periodically."""
        # Always attempt auto-save - let the presenter decide if there's anything to save
        # Empty sessions (no conversation) should still be saved to preserve session metadata and config
        try:
            success = self.presenter.auto_save_current_session()
            if success:
                self.update_window_title()
        except Exception as e:
            # print(f"[SessionTab] Auto-save error: {e}")
            pass

    def _create_chat_event(self, role: str, content: str, tool_calls=None, tool_call_id=None, reasoning=None, tool_name=None):
        """Create a chat event dictionary for the given role and content.
        
        Args:
            role: 'user', 'assistant', or 'tool'
            content: message content
            tool_calls: optional list of tool calls (for assistant)
            tool_call_id: optional tool call ID (for user tool response)
            reasoning: optional reasoning text (for assistant)
            tool_name: optional tool name (for tool results)
        
        Returns:
            Dictionary representing the event
        """
        # Get detail level from presenter config
        detail_level = 'normal'
        if hasattr(self, 'presenter') and self.presenter and hasattr(self.presenter, '_config'):
            detail_level = self.presenter._config.get('detail', 'normal')

        # Handle tool messages (tool results)
        if role == 'tool':
            event = {
                'type': 'tool_result',
                'content': content,
                'tool_call_id': tool_call_id,
                'timestamp': datetime.datetime.now().isoformat(),
                '_detail_level': detail_level
            }
            if tool_name:
                event['tool_name'] = tool_name
            return event        
        # Create event dictionary with appropriate type and fields based on role
        if role == 'system':
            event = {
                'type': 'system',
                'content': content,
                'timestamp': datetime.datetime.now().isoformat(),
                '_detail_level': detail_level
            }
        elif role == 'user':
            event = {
                'type': 'user_query',
                'content': content,
                'turn': self._display_turn,
                'timestamp': datetime.datetime.now().isoformat(),
                '_detail_level': detail_level
            }
        elif role == 'assistant':
            event = {
                'type': 'turn',
                'assistant_content': content,
                'timestamp': datetime.datetime.now().isoformat(),
                '_detail_level': detail_level
            }
            if reasoning is not None:
                event['reasoning'] = reasoning
            if tool_calls:
                event['tool_calls'] = tool_calls
        else:
            # Fallback for unknown roles (including 'tool' which should be handled earlier)
            event = {
                'type': 'turn',
                'assistant_content': content,
                'timestamp': datetime.datetime.now().isoformat(),
                '_detail_level': detail_level
            }
            if reasoning is not None:
                event['reasoning'] = reasoning
            if tool_calls:
                event['tool_calls'] = tool_calls
        
        return event
    
    def _append_chat_message(self, role: str, content: str, tool_calls=None, tool_call_id=None, reasoning=None, tool_name=None):
        """Append a chat message to the event model and output display.
        
        Args:
            role: 'user', 'assistant', or 'tool'
            content: message content
            tool_calls: optional list of tool calls (for assistant)
            tool_call_id: optional tool call ID (for user tool response)
            reasoning: optional reasoning text (for assistant)
            tool_name: optional tool name (for tool results)
        """
        event = self._create_chat_event(role, content, tool_calls, tool_call_id, reasoning, tool_name)
        # Delegate to output panel for display
        self.output_panel.display_event(event)



    def closeEvent(self, event):
        """Handle closing the tab with save/discard prompts for unsaved changes."""
        debug_log("closeEvent: started")
        # Prevent re-entrant calls
        if self._closing:
            debug_log("closeEvent: already closing, ignoring")
            event.ignore()
            return
        # Set closing flag immediately to prevent re-entrance
        self._closing = True
        # Disconnect all presenter signals to prevent signal-driven re-entrance
        try:
            self.presenter.state_changed.disconnect(self.on_state_changed)
            self.presenter.event_received.disconnect(self.display_event)
            self.presenter.tokens_updated.disconnect(self.on_tokens_updated)
            self.presenter.context_updated.disconnect(self.on_context_updated)
            self.presenter.status_message.disconnect(self.on_status_message)
            self.presenter.error_occurred.disconnect(self.on_error_occurred)
            self.presenter.config_changed.disconnect(self.on_config_changed)
        except Exception as e:
            debug_log(f"closeEvent: error disconnecting signals: {e}")
        from PyQt6.QtWidgets import QInputDialog
        # Stop auto-save timer to prevent interference during close
        self._auto_save_timer.stop()

        # Always attempt to save session before closing
        if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
            import sys
            sys.stderr.write(f'[SessionTab] closeEvent: attempting to save session, user_history length={len(self.presenter.user_history) if self.presenter.user_history else 0}, current_session_id={self.presenter.current_session_id}\n')

        debug_log("closeEvent: proceeding with closing")
        # Proceed with closing
        # self._closing = True already set at beginning
        # Save UI configuration
        debug_log("closeEvent: calling save_config")
        self.save_config(immediate=True)
        debug_log("closeEvent: save_config returned")
        # Stop controller if running and reset state (without auto-saving)
        if self.presenter.controller.is_running:
            debug_log("closeEvent: stopping controller")
            self.presenter.controller.stop()
            debug_log("closeEvent: controller stopped")
        # self.presenter.state = ExecutionState.IDLE  # Disabled to avoid infinite loop
        # print("[GUI] closeEvent: skipping state reset to avoid infinite loop")
        # Auto-save session before cleanup
        debug_log("closeEvent: attempting to save session")
        try:
            self.presenter.save_session()
            debug_log("closeEvent: save_session completed")
        except Exception as e:
            debug_log(f"closeEvent: save_session failed: {e}")
        
        # Cleanup presenter
        debug_log("closeEvent: calling presenter.cleanup")
        self.presenter.cleanup()
        debug_log("closeEvent: presenter.cleanup returned")
        # Remove this tab from the parent QTabWidget
        parent = self.parent()
        if parent and hasattr(parent, 'removeTab'):
            idx = parent.indexOf(self)
            if idx >= 0:
                parent.removeTab(idx)
        self.deleteLater()
        event.accept()
        super().closeEvent(event)
