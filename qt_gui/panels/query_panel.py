"""Query Panel - Input and controls for agent queries."""
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTextEdit, QPushButton, QFrame


class QueryPanel(QWidget):
    """Panel containing query input and control buttons."""

    def __init__(self, parent=None):
        super().__init__(parent)

        # Callback slots (to be connected by SessionTab)
        self.on_run = None
        self.on_pause = None

        self.init_ui()
        self.setup_signal_connections()

    def init_ui(self):
        """Initialize the query panel UI."""
        layout = QVBoxLayout()
        self.setLayout(layout)

        # Query input frame
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

        # Single run/pause button — toggles label and behavior
        self.run_pause_btn = QPushButton("RUN")
        self.run_pause_btn.setMinimumWidth(80)
        button_layout.addWidget(self.run_pause_btn)

        button_layout.addStretch()

        query_layout.addLayout(button_layout)
        layout.addWidget(query_frame)

    def setup_signal_connections(self):
        """Connect button signals to callbacks."""
        self.run_pause_btn.clicked.connect(self._on_run_pause_clicked)

    def _on_run_pause_clicked(self):
        """Handle run/pause button click — dispatches based on current label."""
        if self.run_pause_btn.text() == "RUN":
            if self.on_run:
                self.on_run()
        else:
            if self.on_pause:
                self.on_pause()

    def get_query_text(self):
        """Get the current query text."""
        return self.query_entry.toPlainText().strip()

    def clear_query(self):
        """Clear the query input."""
        self.query_entry.clear()

    def set_buttons_running(self):
        """Set button for running state — shows PAUSE (enabled)."""
        self.run_pause_btn.setText("PAUSE")
        self.run_pause_btn.setEnabled(True)

    def set_buttons_pausing(self):
        """Set button for pausing transition — shows PAUSE (disabled/greyed)."""
        self.run_pause_btn.setText("PAUSE")
        self.run_pause_btn.setEnabled(False)

    def set_buttons_paused(self):
        """Set button for paused state — shows RUN (to resume)."""
        self.run_pause_btn.setText("RUN")
        self.run_pause_btn.setEnabled(True)

    def set_buttons_idle(self):
        """Set button for idle state — shows RUN (enabled)."""
        self.run_pause_btn.setText("RUN")
        self.run_pause_btn.setEnabled(True)

    def set_focus_to_query(self):
        """Set focus to query entry."""
        self.query_entry.setFocus()
