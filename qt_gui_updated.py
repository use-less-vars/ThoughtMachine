# qt_gui.py (Step 3) - Updated with 6-line prompt and music player buttons
import sys
import os
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTextEdit, QListWidget,
    QGroupBox, QCheckBox, QMenuBar, QMenu, QFileDialog,
    QMessageBox, QScrollArea, QFrame, QComboBox, QSplitter, QDialog
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QAction, QFont
from dotenv import load_dotenv

from agent_controller import AgentController
from agent_core import AgentConfig
from tools import TOOL_CLASSES

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
        self.current_dir = os.getcwd()
        
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

class StatusPanel(QGroupBox):
    """Shows current status and token usage."""
    def __init__(self):
        super().__init__("Status")
        layout = QVBoxLayout()
        self.status_label = QLabel("Ready")
        self.token_label = QLabel("Tokens: 0 in / 0 out")
        layout.addWidget(self.status_label)
        layout.addWidget(self.token_label)
        layout.addStretch()
        self.setLayout(layout)
    
    def update_status(self, text):
        self.status_label.setText(text)
    
    def update_tokens(self, total_input, total_output):
        self.token_label.setText(f"Tokens: {total_input} in / {total_output} out")

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
        label = QLabel(text)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        if style:
            label.setStyleSheet(style)
        self.content_layout.addWidget(label)

class AgentGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.controller = AgentController()
        self.total_input = 0
        self.total_output = 0
        
        self.init_ui()
        self.setup_polling()
    
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
        self.tool_loader = ToolLoaderPanel(TOOL_CLASSES)
        middle_layout.addWidget(self.tool_loader)
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
        
        # Stop button (■)
        self.stop_btn = QPushButton("■ Stop")
        self.stop_btn.setMaximumWidth(80)
        self.stop_btn.setStyleSheet("""
            QPushButton {
                padding: 5px;
                font-size: 12px;
                background-color: #f44336;
                color: white;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #da190b;
            }
            QPushButton:disabled {
                background-color: #cccccc;
            }
        """)
        self.stop_btn.clicked.connect(self.stop_agent)
        self.stop_btn.setEnabled(False)
        btn_layout.addWidget(self.stop_btn)
        
        # Pause button (⏸)
        self.pause_btn = QPushButton("⏸ Pause")
        self.pause_btn.setMaximumWidth(80)
        self.pause_btn.setStyleSheet("""
            QPushButton {
                padding: 5px;
                font-size: 12px;
                background-color: #ff9800;
                color: white;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #e68a00;
            }
            QPushButton:disabled {
                background-color: #cccccc;
            }
        """)
        self.pause_btn.clicked.connect(self.pause_agent)
        self.pause_btn.setEnabled(False)
        btn_layout.addWidget(self.pause_btn)
        
        # Resume button (⏵)
        self.resume_btn = QPushButton("⏵ Resume")
        self.resume_btn.setMaximumWidth(80)
        self.resume_btn.setStyleSheet("""
            QPushButton {
                padding: 5px;
                font-size: 12px;
                background-color: #2196F3;
                color: white;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #0b7dda;
            }
            QPushButton:disabled {
                background-color: #cccccc;
            }
        """)
        self.resume_btn.clicked.connect(self.resume_agent)
        self.resume_btn.setEnabled(False)
        btn_layout.addWidget(self.resume_btn)
        
        btn_layout.addStretch()
        right_layout.addWidget(btn_frame)
        
        # Controls
        controls_frame = QWidget()
        controls_layout = QHBoxLayout()
        controls_frame.setLayout(controls_layout)
        controls_layout.addWidget(QLabel("Interactive Prompt (placeholder):"))
        self.interactive_entry = QLineEdit()
        self.interactive_entry.setMaximumWidth(300)
        controls_layout.addWidget(self.interactive_entry)
        controls_layout.addWidget(QLabel("Detail:"))
        self.detail_combo = QComboBox()
        self.detail_combo.addItems(["minimal", "normal", "verbose"])
        self.detail_combo.setCurrentText("normal")
        controls_layout.addWidget(self.detail_combo)
        controls_layout.addStretch()
        right_layout.addWidget(controls_frame)
        
        # Clear button
        clear_btn = QPushButton("Clear Output")
        clear_btn.clicked.connect(self.clear_output)
        right_layout.addWidget(clear_btn)
        
        # Output area (scrollable container)
        self.output_scroll_area = QScrollArea()
        self.output_scroll_area.setWidgetResizable(True)
        self.output_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.output_scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        
        self.output_container = QWidget()
        self.output_layout = QVBoxLayout(self.output_container)
        self.output_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.output_layout.setSpacing(5)
        
        self.output_scroll_area.setWidget(self.output_container)
        right_layout.addWidget(self.output_scroll_area)
        
        splitter.addWidget(right_container)
        splitter.setSizes([300, 200, 900])
        main_layout.addWidget(splitter)
        
        self.create_menu_bar()
    
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
    
    # ---- Agent control ----
    def run_agent(self):
        query = self.query_entry.toPlainText().strip()  # Changed from .text() to .toPlainText()
        if not query:
            QMessageBox.warning(self, "No query", "Please enter a query.")
            return
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            QMessageBox.critical(self, "API Key missing", "Set DEEPSEEK_API_KEY in .env file")
            return
        
        enabled_names = self.tool_loader.get_enabled_tool_names()
        tool_name_to_class = {cls.__name__: cls for cls in TOOL_CLASSES}
        enabled_classes = [tool_name_to_class[name] for name in enabled_names]
        
        config = AgentConfig(
            api_key=api_key,
            model="deepseek-chat",
            max_turns=30,
            temperature=0.2,
            extra_system=None,
            tool_classes=enabled_classes
        )
        
        try:
            self.controller.start(query, config)
        except RuntimeError as e:
            QMessageBox.critical(self, "Error", str(e))
            return
        
        self.update_buttons(running=True)
        self.status_panel.update_status("Running")
        self.total_input = 0
        self.total_output = 0
        self.status_panel.update_tokens(self.total_input, self.total_output)
    
    def stop_agent(self):
        self.controller.stop()
        self.status_panel.update_status("Stopping...")
    
    def pause_agent(self):
        self.controller.pause()
        self.pause_btn.setEnabled(False)
        self.resume_btn.setEnabled(True)
        self.status_panel.update_status("Paused")
    
    def resume_agent(self):
        self.controller.resume()
        self.pause_btn.setEnabled(True)
        self.resume_btn.setEnabled(False)
        self.status_panel.update_status("Running")
    
    def update_buttons(self, running):
        if running:
            self.run_btn.setEnabled(False)
            self.stop_btn.setEnabled(True)
            self.pause_btn.setEnabled(True)
            self.resume_btn.setEnabled(False)
        else:
            self.run_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            self.pause_btn.setEnabled(False)
            self.resume_btn.setEnabled(False)
            self.status_panel.update_status("Ready")
    
    def poll(self):
        event = self.controller.get_event()
        if event:
            self.display_event(event)
            if event["type"] == "thread_finished":
                self.update_buttons(running=False)
    
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
        
        # Determine if truncation is needed
        if len(full_text) > MAX_RESULT_LENGTH:
            truncated = full_text[:MAX_RESULT_LENGTH] + "..."
            label = QLabel(f"Result: {truncated}")
            label.setWordWrap(True)
            label.setStyleSheet("color: #006400;")
            layout.addWidget(label, 1)  # stretch factor 1
            
            button = QPushButton("Show full")
            button.setMaximumWidth(80)
            # Connect button to open a dialog with full text
            button.clicked.connect(lambda checked, text=full_text: self._show_full_text_dialog(text))
            layout.addWidget(button)
        else:
            label = QLabel(f"Result: {full_text}")
            label.setWordWrap(True)
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
        text_edit.setPlainText(text)
        text_edit.setReadOnly(True)
        layout.addWidget(text_edit)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dialog.accept)
        layout.addWidget(close_btn)
        dialog.exec()

    def display_event(self, event):
        etype = event["type"]
        detail_level = self.detail_combo.currentText()

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
            self.status_panel.update_tokens(self.total_input, self.total_output)

        elif etype == "final":
            frame.add_content_line(f"Final answer: {event['content']}", style="font-weight: bold; color: #000080;")
            if detail_level != "minimal" and "reasoning" in event and event["reasoning"]:
                frame.add_content_line(f"Reasoning: {event['reasoning']}", style="color: #666666;")
            usage = event.get("usage", {})
            self.total_input = usage.get("total_input", self.total_input)
            self.total_output = usage.get("total_output", self.total_output)
            self.status_panel.update_tokens(self.total_input, self.total_output)

        elif etype == "stopped":
            frame.add_content_line("Agent stopped by user.", style="color: #FF8C00;")
        elif etype == "max_turns":
            frame.add_content_line("Max turns reached without final answer.", style="color: #FF8C00;")
        elif etype == "error":
            frame.add_content_line(f"ERROR: {event.get('message')}", style="color: #FF0000; font-weight: bold;")
            if "traceback" in event and detail_level == "verbose":
                frame.add_content_line(event['traceback'], style="color: #FF0000;")
        elif etype == "thread_finished":
            frame.add_content_line("Background thread finished.", style="color: #808080;")
        else:
            frame.add_content_line(str(event))

        self.output_layout.addWidget(frame)
        self._scroll_to_bottom()

    def _scroll_to_bottom(self):
        QTimer.singleShot(0, self._do_scroll_to_bottom)

    def _do_scroll_to_bottom(self):
        scrollbar = self.output_scroll_area.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def clear_output(self):
        while self.output_layout.count():
            item = self.output_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    gui = AgentGUI()
    gui.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
