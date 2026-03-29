#!/usr/bin/env python
"""
Launcher for the ThoughtMachine GUI.
Run this script from the project root directory.
"""
import sys
import os

# Add the project root to Python path
project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project_root)

# Now run the GUI
from qt_gui.main import main

if __name__ == "__main__":
    main()
    pass