"""Main window for the ThoughtMachine GUI."""
from PyQt6.QtWidgets import (
    QMainWindow, QTabWidget, QPushButton, QMenuBar, QMenu,
    QWidget, QVBoxLayout, QHBoxLayout, QApplication
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeySequence, QAction
from session.store import FileSystemSessionStore
from qt_gui.themes import apply_theme
from qt_gui.debug_log import debug_log
import json
from pathlib import Path
class AgentGUI(QMainWindow):
    """Main application window with tab management and menu bar."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("ThoughtMachine")
        self._closing = False
        self.current_theme = None
        # Ensure window is deleted when closed
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.session_store = FileSystemSessionStore()
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout()
        central_widget.setLayout(main_layout)
        self.tab_widget = QTabWidget()
        self.tab_widget.setTabsClosable(True)
        self.tab_widget.tabCloseRequested.connect(self.close_tab)
        self.tab_widget.currentChanged.connect(self.on_current_tab_changed)

        # Add new tab button on the tab bar
        new_tab_btn = QPushButton("+")
        new_tab_btn.setFixedSize(24, 24)
        new_tab_btn.setToolTip("New Session Tab")
        new_tab_btn.clicked.connect(self.new_tab)
        self.tab_widget.setCornerWidget(new_tab_btn, Qt.Corner.TopRightCorner)
        main_layout.addWidget(self.tab_widget)
        self.restore_open_sessions()
        if self.tab_widget.count() == 0:
            self.new_tab()  # create initial tab
        self.create_menu_bar()

    def new_tab(self, session_id=None):
        from qt_gui.session_tab import SessionTab
        tab = SessionTab(session_store=self.session_store, session_id=session_id)
        index = self.tab_widget.addTab(tab, tab.presenter.session_name or "Untitled")
        self.tab_widget.setCurrentWidget(tab)
        # Update tab label after adding to tab widget
        tab.update_window_title()
        tab._update_tab_label()

    def restore_open_sessions(self):
        """Restore previously open sessions from open_sessions.json."""
        open_sessions_path = self.session_store.sessions_dir / "open_sessions.json"
        if not open_sessions_path.exists():
            debug_log(f"No open sessions file at {open_sessions_path}", level="DEBUG")
            return
        try:
            with open(open_sessions_path, 'r') as f:
                session_ids = json.load(f)
            if not isinstance(session_ids, list):
                debug_log(f"Invalid open_sessions.json content: {session_ids}", level="WARNING")
                return
            # Filter out session IDs that no longer exist
            existing_ids = []
            for sid in session_ids:
                # Use session store's path resolution (supports friendly filenames)
                if self.session_store.get_session_path(sid).exists():
                    existing_ids.append(sid)
                else:
                    debug_log(f"Session {sid} no longer exists, skipping", level="WARNING")
            # Create tabs for each session ID
            for sid in existing_ids:
                self.new_tab(session_id=sid)
            debug_log(f"Restored {len(existing_ids)} open sessions", level="INFO")
        except Exception as e:
            debug_log(f"Failed to restore open sessions: {e}", level="ERROR")

    def save_open_sessions(self):
        """Save list of open session IDs to open_sessions.json."""
        session_ids = []
        for i in range(self.tab_widget.count()):
            tab = self.tab_widget.widget(i)
            if tab and tab.presenter.current_session_id:
                session_ids.append(tab.presenter.current_session_id)
        open_sessions_path = self.session_store.sessions_dir / "open_sessions.json"
        try:
            with open(open_sessions_path, 'w') as f:
                json.dump(session_ids, f)
            debug_log(f"Saved {len(session_ids)} open sessions to {open_sessions_path}")
        except Exception as e:
            debug_log(f"Failed to save open sessions: {e}")

    def open_session_in_new_tab(self, file_path):
        """Open a session file in a new tab."""
        from qt_gui.session_tab import SessionTab
        tab = SessionTab(session_store=self.session_store)
        try:
            success = tab.presenter.load_session(file_path)
            if success:
                tab.display_loaded_conversation()
            else:
                debug_log(f"Failed to load session from file: {file_path}")
                # Close the tab?
                tab.close()
                return
        except Exception as e:
            debug_log(f"Failed to load session {file_path}: {e}")
            tab.close()
            return
        index = self.tab_widget.addTab(tab, tab.presenter.session_name or "Untitled")
        self.tab_widget.setCurrentWidget(tab)
        tab.update_window_title()
        tab._update_tab_label()

    def close_tab(self, index):
        tab = self.tab_widget.widget(index)
        if tab:
            if tab.close():  # triggers closeEvent; the tab will remove itself if accepted
                # Save open sessions after tab removal
                self.save_open_sessions()
            # If no tabs remain, create a new empty tab
            if self.tab_widget.count() == 0:
                self.new_tab()

    def on_current_tab_changed(self, index):
        tab = self.tab_widget.currentWidget()
        if tab:
            tab.update_window_title()
            self.statusBar().showMessage(f"Tokens: in={tab.total_input}, out={tab.total_output}, ctx={tab.context_length}")



    def create_menu_bar(self):
        menu_bar = QMenuBar(self)
        self.setMenuBar(menu_bar)
        # File menu
        file_menu = menu_bar.addMenu("File")
        save_config_action = QAction("Save Configuration", self)
        save_config_action.triggered.connect(lambda: self.current_tab().save_config())
        file_menu.addAction(save_config_action)
        load_config_action = QAction("Load Configuration", self)
        load_config_action.triggered.connect(lambda: self.current_tab().load_config())
        file_menu.addAction(load_config_action)
        file_menu.addSeparator()
        # Export submenu
        export_menu = file_menu.addMenu("Export Conversation")
        export_text_action = QAction("As Plain Text", self)
        export_text_action.triggered.connect(lambda: self.current_tab().export_conversation_text())
        export_menu.addAction(export_text_action)
        export_html_action = QAction("As HTML", self)
        export_html_action.triggered.connect(lambda: self.current_tab().export_conversation_html())
        export_menu.addAction(export_html_action)
        export_pdf_action = QAction("As PDF", self)
        export_pdf_action.triggered.connect(lambda: self.current_tab().export_conversation_pdf())
        export_menu.addAction(export_pdf_action)
        file_menu.addSeparator()
        # Session management actions
        save_session_action = QAction("Save Session As...", self)
        save_session_action.triggered.connect(lambda: self.current_tab().save_session_as())
        file_menu.addAction(save_session_action)
        open_session_action = QAction("Open Session...", self)
        open_session_action.triggered.connect(lambda: self.current_tab().open_session())
        file_menu.addAction(open_session_action)
        file_menu.addSeparator()
        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)
        # View menu for theme
        view_menu = menu_bar.addMenu("View")
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
        # Shortcuts
        save_config_action.setShortcut("Ctrl+S")
        load_config_action.setShortcut("Ctrl+O")
        exit_action.setShortcut("Ctrl+Q")

    def current_tab(self):
        return self.tab_widget.currentWidget()

    def set_theme(self, theme_name):
        """Set application theme."""
        if apply_theme(self, theme_name):
            self.current_theme = theme_name
            debug_log(f"Theme set to: {theme_name}")
        else:
            debug_log(f"Unknown theme: {theme_name}")

    def closeEvent(self, event):
        # Close all tabs by calling close() on each; if any rejects, abort the application close.
        # Tabs will remove themselves upon acceptance.
        if self._closing:
            event.accept()
            super().closeEvent(event)
            return
        self._closing = True
        debug_log("closeEvent called")
        # Save open sessions before closing tabs
        self.save_open_sessions()

        debug_log(f"Starting tab close loop, count={self.tab_widget.count()}")
        tabs_closed = 0
        while self.tab_widget.count() > 0:
            tab = self.tab_widget.widget(0)
            if tab:
                debug_log(f"Calling tab.close()")
                if not tab.close():
                    event.ignore()
                    self._closing = False
                    super().closeEvent(event)
                    return
                debug_log(f"Tab closed successfully, new count={self.tab_widget.count()}")
                # Safety check: ensure tab count decreased after successful close
                if self.tab_widget.count() > 0 and self.tab_widget.widget(0) is tab:
                    # Tab didn't close, manually remove it to avoid infinite loop
                    debug_log(f"WARNING: tab.close() returned True but tab not removed, manually removing")
                    self.tab_widget.removeTab(0)
                    tabs_closed += 1
                    continue  # Continue with next tab (now at index 0)
                tabs_closed += 1
            else:
                debug_log(f"No tab at index 0, breaking")
                break
        debug_log(f"Closed {tabs_closed} tabs, accepting window close")
        event.accept()
        super().closeEvent(event)
        # Force hide the window to ensure it closes
        self.hide()
        # Quit the application since main window is closing
        QApplication.instance().quit()
        debug_log("closeEvent accepted, window hidden, app quitting")
