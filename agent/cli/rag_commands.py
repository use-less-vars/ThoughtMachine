#!/usr/bin/env python3
"""
CLI commands for RAG operations.

Provides the `index-codebase` command for creating semantic search indexes.
"""

import argparse
import os
import sys
from pathlib import Path
from typing import Optional

# Add the agent module to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agent.config.models import AgentConfig
from agent.knowledge.codebase_indexer import index_codebase
from agent.knowledge.dependencies import check_rag_dependencies


def index_codebase_command(args) -> int:
    """
    Execute the index-codebase command.
    
    Args:
        args: argparse namespace with workspace and force flags
        
    Returns:
        Exit code (0 for success, 1 for failure)
    """
    workspace_path = args.workspace
    if workspace_path is None:
        # Try to get workspace from environment or current directory
        workspace_path = os.getcwd()
        print(f"Using current directory as workspace: {workspace_path}")
    
    workspace_path = os.path.abspath(workspace_path)
    
    if not os.path.exists(workspace_path):
        print(f"Error: Workspace path does not exist: {workspace_path}")
        return 1
    
    if not os.path.isdir(workspace_path):
        print(f"Error: Workspace path is not a directory: {workspace_path}")
        return 1
    
    # Check RAG dependencies
    import sys
    print(f"DEBUG: Python executable: {sys.executable}", file=sys.stderr)
    print(f"DEBUG: Python path: {sys.path}", file=sys.stderr)
    rag_available, missing_msg = check_rag_dependencies()
    if not rag_available:
        print(f"Error: {missing_msg}", file=sys.stderr)
        print("DEBUG: Trying to import packages directly...", file=sys.stderr)
        for pkg in ['chromadb', 'sentence_transformers', 'tree_sitter', 'pathspec']:
            try:
                __import__(pkg)
                print(f"DEBUG: {pkg} imported successfully", file=sys.stderr)
            except ImportError as e:
                print(f"DEBUG: {pkg} import failed: {e}", file=sys.stderr)
        print("Please install required packages: pip install chromadb sentence-transformers tree-sitter pathspec")
        return 1
    
    # Create a minimal AgentConfig with default RAG settings
    config = AgentConfig()
    
    # Optionally, we could load a preset config if exists
    
    print(f"Indexing codebase at: {workspace_path}")
    if args.force:
        print("Force flag set: will overwrite existing index.")
        # Force flag will delete existing index and create new collection.
    
    success, message = index_codebase(workspace_path, config, args.force)
    
    if success:
        print(f"Success: {message}")
        return 0
    else:
        print(f"Failed: {message}")
        return 1


def add_index_codebase_subparser(subparsers):
    """
    Add the index-codebase subcommand to argparse subparsers.
    """
    parser = subparsers.add_parser(
        "index-codebase",
        help="Create a semantic search index of the codebase"
    )
    parser.add_argument(
        "--workspace",
        "-w",
        type=str,
        help="Path to workspace directory (default: current directory)"
    )
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Force re-indexing (overwrite existing index)"
    )
    parser.set_defaults(func=index_codebase_command)
    return parser


def main():
    """Main entry point for the RAG CLI."""
    parser = argparse.ArgumentParser(
        description="ThoughtMachine RAG Operations",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(
        dest="command",
        help="Available commands"
    )
    
    # Add subcommands
    add_index_codebase_subparser(subparsers)
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return 1
    
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())