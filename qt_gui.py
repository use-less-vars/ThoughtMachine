# qt_gui.py
import sys
import os
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QJBoxLayout,
    QGridLayout, QLabel, QLineEdit, QPushButton, QTextEdit,
    QListWidget, QListWidgetItem, QGroupBox, CHeckBox,
    QMenuBar, QMenu, QFileDialog, QMessageBox, QScrollArea,
    QFrame, QComboBox, QSplitter
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QThread
from PyQt6.QtGui import ACtion, QFont
class ToolLoaderPanel(QGroupBox):
    """Panel with checkboxes to enable/disable tools."""
    def __init__(self, tool_classes):
        super().__init__("Tool Loader")
        self.tool_classes = tool_classes
        self.tool_checkboxes = {}  # name -> QCheckBox
        
        layout = QVBoxLayout()
        
        # Create a checkbox for each tool
        for cls in tool_classes:
            checkbox = CHeckBox(cls.__name__)
            checkbox.setChecked(True)  # default enabled
            layout.addWidget(checkbox)
            self.tool_checkboxes[cls.__name__] = checkbox
        
        layout.addStretch()
        self.setLayout(layout)
    
    def get_enabled_tool_names(self):
        """Return a list of names of tools that are checked."""
        return [name for name, cb in self.tool_checkboxes.items() if cb.isChecked()]
