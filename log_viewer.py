#!/usr/bin/env python3
"""
Log Viewer for ThoughtMachine agent logs.
Displays JSONL log files in a readable GUI format.
"""
import sys
import os
import json
from pathlib import Path
from datetime import datetime

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QListWidget, QListWidgetItem, QTextEdit, QSplitter,
    QTableWidget, QTableWidgetItem, QHeaderView, QTreeWidget,
    QTreeWidgetItem, QGroupBox, QPushButton, QFileDialog,
    QComboBox, QLineEdit, QCheckBox, QMenuBar, QMenu, QMessageBox
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QAction, QFont, QColor, QShortcut, QKeySequence


class LogViewer(QMainWindow):
    """Main window for log viewer."""
    
    def __init__(self):
        super().__init__()
        self.log_dir = Path("./logs")
        self.current_file = None
        self.all_entries = []
        self.filtered_entries = []
        self.init_ui()
        self.load_log_files()
        
    def init_ui(self):
        """Initialize the user interface."""
        self.setWindowTitle("ThoughtMachine Log Viewer")
        self.setGeometry(100, 100, 1200, 800)
        
        # Create central widget and layout
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        
        # Top toolbar
        toolbar_layout = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.load_log_files)
        toolbar_layout.addWidget(self.refresh_btn)
        
        self.open_btn = QPushButton("Open Log Directory...")
        self.open_btn.clicked.connect(self.open_log_directory)
        toolbar_layout.addWidget(self.open_btn)
        
        toolbar_layout.addStretch()
        
        self.filter_level_combo = QComboBox()
        self.filter_level_combo.addItems(["All", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
        self.filter_level_combo.currentTextChanged.connect(self.filter_logs)
        toolbar_layout.addWidget(QLabel("Level:"))
        toolbar_layout.addWidget(self.filter_level_combo)
        
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search...")
        self.search_edit.textChanged.connect(self.filter_logs)
        toolbar_layout.addWidget(self.search_edit)
        
        main_layout.addLayout(toolbar_layout)
        
        # Splitter for file list and log view
        splitter = QSplitter(Qt.Orientation.Horizontal)
        
        # Left panel: log file list
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.addWidget(QLabel("Log Files:"))
        self.file_list = QListWidget()
        self.file_list.itemClicked.connect(self.on_file_selected)
        left_layout.addWidget(self.file_list)
        splitter.addWidget(left_panel)
        
        # Right panel: log entries
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.addWidget(QLabel("Log Entries:"))
        
        # Table for log entries
        self.log_table = QTableWidget()
        self.log_table.setColumnCount(5)
        self.log_table.setHorizontalHeaderLabels(["Timestamp", "Level", "Type", "Message", "Data"])
        self.log_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.log_table.setAlternatingRowColors(True)
        self.log_table.itemDoubleClicked.connect(self.on_entry_double_clicked)
        right_layout.addWidget(self.log_table)
        
        # Bottom panel for detailed view
        detail_group = QGroupBox("Details")
        detail_layout = QVBoxLayout()
        self.detail_text = QTextEdit()
        self.detail_text.setReadOnly(True)
        detail_layout.addWidget(self.detail_text)
        detail_group.setLayout(detail_layout)
        right_layout.addWidget(detail_group)
        
        splitter.addWidget(right_panel)
        splitter.setSizes([300, 900])
        
        main_layout.addWidget(splitter)
        
        # Status bar
        self.statusBar().showMessage("Ready")
        
        # Create menu bar
        self.create_menu()
        
        # Escape key to clear search
        esc_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        esc_shortcut.activated.connect(self.clear_search)
        
    def create_menu(self):
        """Create menu bar."""
        menubar = self.menuBar()
        
        file_menu = menubar.addMenu("File")
        open_action = QAction("Open Directory...", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self.open_log_directory)
        file_menu.addAction(open_action)
        
        refresh_action = QAction("Refresh", self)
        refresh_action.setShortcut(QKeySequence.StandardKey.Refresh)
        refresh_action.triggered.connect(self.load_log_files)
        file_menu.addAction(refresh_action)
        
        file_menu.addSeparator()
        
        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)
        
        view_menu = menubar.addMenu("View")
        auto_refresh_action = QAction("Auto-refresh", self, checkable=True)
        view_menu.addAction(auto_refresh_action)
        
        help_menu = menubar.addMenu("Help")
        about_action = QAction("About", self)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)
    
    def load_log_files(self):
        """Load log files from log directory."""
        self.file_list.clear()
        if not self.log_dir.exists():
            self.statusBar().showMessage(f"Log directory not found: {self.log_dir}")
            return
        
        files = sorted(self.log_dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
        for file in files:
            item = QListWidgetItem(file.name)
            item.setData(Qt.ItemDataRole.UserRole, file)
            self.file_list.addItem(item)
        
        self.statusBar().showMessage(f"Found {len(files)} log files")
    
    def open_log_directory(self):
        """Open a different log directory."""
        dir_path = QFileDialog.getExistingDirectory(self, "Select Log Directory", str(self.log_dir))
        if dir_path:
            self.log_dir = Path(dir_path)
            self.load_log_files()
    
    def on_file_selected(self, item):
        """Load selected log file."""
        file_path = item.data(Qt.ItemDataRole.UserRole)
        self.current_file = file_path
        self.load_log_file(file_path)
    
    def load_log_file(self, file_path):
        """Load and parse a log file."""
        self.all_entries = []
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        self.all_entries.append(entry)
                    except json.JSONDecodeError as e:
                        print(f"Error parsing line {line_num}: {e}")
            self.filter_logs()
            self.statusBar().showMessage(f"Loaded {len(self.all_entries)} entries from {file_path.name}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load log file: {e}")
    
    def display_log_entries(self):
        """Display log entries in the table."""
        self.log_table.setRowCount(len(self.filtered_entries))
        for row, entry in enumerate(self.filtered_entries):
            # Timestamp
            timestamp = entry.get('timestamp', '')
            if timestamp:
                # Try to format timestamp nicely
                try:
                    dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                    timestamp = dt.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
                except:
                    pass
            self.log_table.setItem(row, 0, QTableWidgetItem(timestamp))
            
            # Level
            level = entry.get('level', '')
            level_item = QTableWidgetItem(level)
            # Color coding
            if level == 'ERROR' or level == 'CRITICAL':
                level_item.setForeground(QColor('red'))
            elif level == 'WARNING':
                level_item.setForeground(QColor('orange'))
            elif level == 'INFO':
                level_item.setForeground(QColor('blue'))
            elif level == 'DEBUG':
                level_item.setForeground(QColor('gray'))
            self.log_table.setItem(row, 1, level_item)
            
            # Type
            self.log_table.setItem(row, 2, QTableWidgetItem(entry.get('type', '')))
            
            # Message
            self.log_table.setItem(row, 3, QTableWidgetItem(entry.get('message', '')))
            
            # Data (summarized)
            data = entry.get('data', {})
            data_summary = self.summarize_data(data)
            self.log_table.setItem(row, 4, QTableWidgetItem(data_summary))
        
        self.log_table.resizeColumnsToContents()
    
    def summarize_data(self, data):
        """Create a summary string from data field."""
        if not data:
            return ""
        if isinstance(data, dict):
            # For tool calls, show tool name
            if 'tool_name' in data:
                return f"tool: {data['tool_name']}"
            # For agent start, show query snippet
            if 'query' in data:
                query = data['query']
                if len(query) > 50:
                    query = query[:47] + "..."
                return f"query: {query}"
            # Otherwise show keys
            keys = list(data.keys())
            if len(keys) > 3:
                return f"{len(keys)} keys: {', '.join(keys[:3])}..."
            return f"keys: {', '.join(keys)}"
        return str(type(data))
    
    def filter_logs(self):
        """Filter displayed log entries based on level and search text."""
        if not self.all_entries:
            self.filtered_entries = []
        else:
            self.filtered_entries = self.all_entries
        self.display_log_entries()
    
    def clear_search(self):
        """Clear search box."""
        self.search_edit.clear()

    def on_entry_double_clicked(self, item):
        """Show detailed view of log entry."""
        row = item.row()
        if row < len(self.filtered_entries):
            entry = self.filtered_entries[row]
            # Pretty print the entire entry
            formatted = json.dumps(entry, indent=2, default=str)
            self.detail_text.setPlainText(formatted)
    
    def show_about(self):
        """Show about dialog."""
        QMessageBox.about(self, "About Log Viewer",
                         "ThoughtMachine Log Viewer\n\n"
                         "A simple GUI to view JSONL log files generated by the ThoughtMachine agent.")
    
    def closeEvent(self, event):
        """Handle window close event."""
        event.accept()


def main():
    app = QApplication(sys.argv)
    viewer = LogViewer()
    viewer.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()