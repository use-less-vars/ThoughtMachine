"""Main window for the ThoughtMachine GUI."""
from PyQt6.QtWidgets import QMainWindow, QTabWidget, QPushButton, QMenuBar, QMenu, QWidget, QVBoxLayout, QHBoxLayout, QApplication, QLabel
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeySequence, QAction
from session.store import FileSystemSessionStore
from qt_gui.themes import apply_theme
from agent.logging import log
import json
import os
from pathlib import Path

class AgentGUI(QMainWindow):
    """Main application window with tab management and menu bar."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle('ThoughtMachine')
        self._closing = False
        self.current_theme = None
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
        new_tab_btn = QPushButton('+')
        new_tab_btn.setFixedSize(24, 24)
        new_tab_btn.setToolTip('New Session Tab')
        new_tab_btn.clicked.connect(self.new_tab)
        self.tab_widget.setCornerWidget(new_tab_btn, Qt.Corner.TopRightCorner)
        main_layout.addWidget(self.tab_widget)
        self._init_session_size_label()
        self.restore_open_sessions()
        if self.tab_widget.count() == 0:
            self.new_tab()
        self.create_menu_bar()

    def _init_session_size_label(self):
        """Create the session file size label as a permanent widget in the status bar."""
        self._session_size_label = QLabel("Session: N/A")
        self._session_size_label.setToolTip(
            "Size of the session file on disk. "
            "Saves automatically reduce the file after summarisation."
        )
        self.statusBar().addPermanentWidget(self._session_size_label)

    def _update_session_size_label(self):
        """Update the session file size label from the current tab."""
        tab = self.tab_widget.currentWidget()
        if not tab or not tab.presenter.current_session_id:
            self._session_size_label.setText("Session: N/A")
            return
        session_id = tab.presenter.current_session_id
        path = self.session_store.get_session_path(session_id)
        if path.exists():
            size = os.path.getsize(path)
            text = self._format_file_size(size)
            self._session_size_label.setText(f"Session: {text}")
        else:
            self._session_size_label.setText("Session: unsaved")

    @staticmethod
    def _format_file_size(size_bytes: int) -> str:
        """Format file size in human-readable format."""
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        else:
            return f"{size_bytes / (1024 * 1024):.1f} MB"

    def new_tab(self, session_id=None):
        from qt_gui.session_tab import SessionTab
        tab = SessionTab(session_store=self.session_store, session_id=session_id)
        index = self.tab_widget.addTab(tab, tab.presenter.session_name or 'Untitled')
        self.tab_widget.setCurrentWidget(tab)
        tab.update_window_title()
        tab._update_tab_label()
        self._update_session_size_label()

    def restore_open_sessions(self):
        """Restore previously open sessions from open_sessions.json.

        Reads from the new state_dir location. If the file is not there,
        falls back to the old sessions_dir location and migrates it.
        """
        open_sessions_path = self.session_store.get_open_sessions_path()
        session_ids = None

        # Try new location first
        if open_sessions_path.exists():
            try:
                with open(open_sessions_path, 'r') as f:
                    session_ids = json.load(f)
                log('DEBUG', 'debug.unknown', f'Read open sessions from {open_sessions_path}')
            except Exception as e:
                log('ERROR', 'debug.unknown', f'Failed to read open sessions from {open_sessions_path}: {e}')

        # Fallback: try old location in sessions_dir
        if session_ids is None:
            old_path = self.session_store.sessions_dir / 'open_sessions.json'
            if old_path.exists():
                try:
                    with open(old_path, 'r') as f:
                        session_ids = json.load(f)
                    log('INFO', 'debug.unknown', f'Migrated open sessions from old location {old_path}')
                except Exception as e:
                    log('ERROR', 'debug.unknown', f'Failed to read open sessions from old location {old_path}: {e}')

        if session_ids is None:
            log('DEBUG', 'debug.unknown', f'No open sessions file found')
            return

        if not isinstance(session_ids, list):
            log('WARNING', 'debug.unknown', f'Invalid open_sessions.json content: {session_ids}')
            return

        existing_ids = []
        for sid in session_ids:
            if self.session_store.get_session_path(sid).exists():
                existing_ids.append(sid)
            else:
                log('WARNING', 'debug.unknown', f'Session {sid} no longer exists, skipping')
        for sid in existing_ids:
            self.new_tab(session_id=sid)
        log('INFO', 'debug.unknown', f'Restored {len(existing_ids)} open sessions')

    def save_open_sessions(self):
        """Save list of open session IDs to open_sessions.json."""
        session_ids = []
        for i in range(self.tab_widget.count()):
            tab = self.tab_widget.widget(i)
            if tab and tab.presenter.current_session_id:
                session_ids.append(tab.presenter.current_session_id)
        open_sessions_path = self.session_store.get_open_sessions_path()
        try:
            with open(open_sessions_path, 'w') as f:
                json.dump(session_ids, f)
            log('DEBUG', 'debug.unknown', f'Saved {len(session_ids)} open sessions to {open_sessions_path}')
        except Exception as e:
            log('DEBUG', 'debug.unknown', f'Failed to save open sessions: {e}')

    def open_session_in_new_tab(self, file_path):
        """Open a session file in a new tab."""
        from qt_gui.session_tab import SessionTab
        tab = SessionTab(session_store=self.session_store)
        try:
            success = tab.presenter.load_session(file_path)
            if success:
                tab.display_loaded_conversation()
            else:
                log('DEBUG', 'debug.unknown', f'Failed to load session from file: {file_path}')
                tab.close()
                return
        except Exception as e:
            log('DEBUG', 'debug.unknown', f'Failed to load session {file_path}: {e}')
            tab.close()
            return
        index = self.tab_widget.addTab(tab, tab.presenter.session_name or 'Untitled')
        self.tab_widget.setCurrentWidget(tab)
        tab.update_window_title()
        tab._update_tab_label()
        self._update_session_size_label()

    def close_tab(self, index):
        tab = self.tab_widget.widget(index)
        if tab:
            if tab.close():
                self.save_open_sessions()
            if self.tab_widget.count() == 0:
                self.new_tab()

    def on_current_tab_changed(self, index):
        tab = self.tab_widget.currentWidget()
        if tab:
            tab.update_window_title()
            self.statusBar().showMessage(f'Tokens: in={tab.total_input}, out={tab.total_output}, ctx={tab.context_length}')
        self._update_session_size_label()

    def create_menu_bar(self):
        menu_bar = QMenuBar(self)
        self.setMenuBar(menu_bar)
        file_menu = menu_bar.addMenu('File')
        save_config_action = QAction('Save Configuration', self)
        save_config_action.triggered.connect(lambda: self.current_tab().save_config())
        file_menu.addAction(save_config_action)
        load_config_action = QAction('Load Configuration', self)
        load_config_action.triggered.connect(lambda: self.current_tab().load_config())
        file_menu.addAction(load_config_action)
        file_menu.addSeparator()
        export_menu = file_menu.addMenu('Export Conversation')
        export_text_action = QAction('As Plain Text', self)
        export_text_action.triggered.connect(lambda: self.current_tab().export_conversation_text())
        export_menu.addAction(export_text_action)
        export_html_action = QAction('As HTML', self)
        export_html_action.triggered.connect(lambda: self.current_tab().export_conversation_html())
        export_menu.addAction(export_html_action)
        export_pdf_action = QAction('As PDF', self)
        export_pdf_action.triggered.connect(lambda: self.current_tab().export_conversation_pdf())
        export_menu.addAction(export_pdf_action)
        file_menu.addSeparator()
        save_session_action = QAction('Save Session As...', self)
        save_session_action.triggered.connect(lambda: self.current_tab().save_session_as())
        file_menu.addAction(save_session_action)
        open_session_action = QAction('Open Session...', self)
        open_session_action.triggered.connect(lambda: self.current_tab().open_session())
        file_menu.addAction(open_session_action)
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

    def current_tab(self):
        return self.tab_widget.currentWidget()

    def set_theme(self, theme_name):
        """Set application theme."""
        if apply_theme(self, theme_name):
            self.current_theme = theme_name
            log('DEBUG', 'debug.unknown', f'Theme set to: {theme_name}')
        else:
            log('DEBUG', 'debug.unknown', f'Unknown theme: {theme_name}')

    def closeEvent(self, event):
        if self._closing:
            event.accept()
            super().closeEvent(event)
            return
        self._closing = True
        log('DEBUG', 'debug.unknown', 'closeEvent called')
        self.save_open_sessions()
        log('DEBUG', 'debug.unknown', f'Starting tab close loop, count={self.tab_widget.count()}')
        tabs_closed = 0
        while self.tab_widget.count() > 0:
            tab = self.tab_widget.widget(0)
            if tab:
                log('DEBUG', 'debug.unknown', f'Calling tab.close()')
                if not tab.close():
                    event.ignore()
                    self._closing = False
                    super().closeEvent(event)
                    return
                log('DEBUG', 'debug.unknown', f'Tab closed successfully, new count={self.tab_widget.count()}')
                if self.tab_widget.count() > 0 and self.tab_widget.widget(0) is tab:
                    log('DEBUG', 'debug.unknown', f'WARNING: tab.close() returned True but tab not removed, manually removing')
                    self.tab_widget.removeTab(0)
                    tabs_closed += 1
                    continue
                tabs_closed += 1
            else:
                log('DEBUG', 'debug.unknown', f'No tab at index 0, breaking')
                break
        log('DEBUG', 'debug.unknown', f'Closed {tabs_closed} tabs, accepting window close')
        event.accept()
        super().closeEvent(event)
        self.hide()
        QApplication.instance().quit()
        log('DEBUG', 'debug.unknown', 'closeEvent accepted, window hidden, app quitting')