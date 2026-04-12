#!/usr/bin/env python3
"""
Minimal QML GUI for ThoughtMachine.
"""

import os
import sys
import json
from pathlib import Path

# Add parent directory to Python path to allow importing agent, session, etc.
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from PyQt6.QtGui import QGuiApplication
from PyQt6.QtQml import QQmlApplicationEngine

from agent.presenter.agent_presenter import RefactoredAgentPresenter
from qml_gui.models.conversation_model import ConversationModel


def load_config() -> dict:
    """Load configuration from environment variables and config file."""
    config = {}
    
    # 1. Environment variables
    api_key = os.getenv('OPENAI_API_KEY') or os.getenv('DEEPSEEK_API_KEY')
    model = os.getenv('MODEL')
    base_url = os.getenv('BASE_URL')
    
    if api_key:
        config['api_key'] = api_key
    if model:
        config['model'] = model
    if base_url:
        config['base_url'] = base_url
    
    # Determine provider type based on environment or api key source
    if os.getenv('DEEPSEEK_API_KEY'):
        config['provider_type'] = 'openai_compatible'
        # Default DeepSeek URL if not set
        if 'base_url' not in config:
            config['base_url'] = 'https://api.deepseek.com'
    elif os.getenv('OPENAI_API_KEY'):
        config['provider_type'] = 'openai'
        # Default OpenAI URL if not set
        if 'base_url' not in config:
            config['base_url'] = 'https://api.openai.com/v1'
    
    # 2. Config file: ~/.thoughtmachine/config.json
    config_dir = Path.home() / '.thoughtmachine'
    config_file = config_dir / 'config.json'
    if config_file.exists():
        try:
            with open(config_file, 'r') as f:
                file_config = json.load(f)
            # Merge file config (overwrites env vars)
            config.update(file_config)
        except Exception as e:
            print(f"Warning: Failed to load config file {config_file}: {e}")
    
    # 3. Also check for agent_config.json in project root (legacy)
    project_config = Path(project_root) / 'agent_config.json'
    if project_config.exists():
        try:
            with open(project_config, 'r') as f:
                project_config_data = json.load(f)
            # Merge project config (lower priority than user config)
            for key, value in project_config_data.items():
                if key not in config:
                    config[key] = value
        except Exception as e:
            print(f"Warning: Failed to load project config {project_config}: {e}")
    
    return config


def main():
    """Application entry point."""
    app = QGuiApplication(sys.argv)
    app.setApplicationName("ThoughtMachine QML")
    app.setOrganizationName("ThoughtMachine")
    
    # Create presenter (backend)
    presenter = RefactoredAgentPresenter()
    
    # Load configuration from environment/file and apply to presenter
    config = load_config()
    if config:
        print(f"Loaded config with keys: {list(config.keys())}")
        presenter.update_config(config)
    else:
        print("No configuration found. Agent will not run until API key is set via config dialog.")
    
    # Create conversation model
    conversation_model = ConversationModel(presenter)
    
    engine = QQmlApplicationEngine()
    
    # Expose model to QML as a context property
    engine.rootContext().setContextProperty("conversationModel", conversation_model)
    # Expose presenter to QML as a context property
    engine.rootContext().setContextProperty("presenter", presenter)
    
    # Load the main QML file
    qml_path = os.path.join(os.path.dirname(__file__), "qml", "MainWindow.qml")
    engine.load(qml_path)
    
    if not engine.rootObjects():
        print("Failed to load QML file", file=sys.stderr)
        sys.exit(1)
    
    sys.exit(app.exec())


if __name__ == "__main__":
    main()