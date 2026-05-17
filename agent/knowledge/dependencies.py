"""
Soft dependency checks for RAG components.

This module provides a function to check if required RAG libraries are installed.
If dependencies are missing, RAG tools will gracefully degrade or provide fallback behavior.
"""

from typing import Tuple, Optional, Dict
import sys
from agent.logging import log

# Global dependency status
DEPENDENCIES: Dict[str, bool] = {}
RAG_AVAILABLE: bool = False


def check_rag_dependencies() -> Tuple[bool, Optional[str]]:
    """
    Check if all required RAG dependencies are installed.

    Returns:
        Tuple of (success, error_message)
        - success: True if all required libraries are importable
        - error_message: None if success=True, otherwise a user-friendly message
          listing missing packages
    """

    missing = []

    # Clear and rebuild DEPENDENCIES
    DEPENDENCIES.clear()

    # Try to import chromadb
    try:
        import chromadb
        DEPENDENCIES["chromadb"] = True
        log('DEBUG', 'knowledge.deps', 'chromadb imported successfully')
    except ImportError as e:
        DEPENDENCIES["chromadb"] = False
        missing.append("chromadb")
        log('DEBUG', 'knowledge.deps', f'chromadb import failed: {e}')


    # Try to import sentence_transformers
    try:
        import sentence_transformers
        DEPENDENCIES["sentence_transformers"] = True
        log('DEBUG', 'knowledge.deps', 'sentence_transformers imported successfully')
    except ImportError as e:
        DEPENDENCIES["sentence_transformers"] = False
        missing.append("sentence_transformers")
        log('DEBUG', 'knowledge.deps', f'sentence_transformers import failed: {e}')


    # Try to import tree_sitter
    try:
        import tree_sitter
        DEPENDENCIES["tree_sitter"] = True
        log('DEBUG', 'knowledge.deps', 'tree_sitter imported successfully')
    except ImportError as e:
        DEPENDENCIES["tree_sitter"] = False
        missing.append("tree_sitter")
        log('DEBUG', 'knowledge.deps', f'tree_sitter import failed: {e}')


    # Try to import pathspec (for .gitignore parsing)
    try:
        import pathspec
        DEPENDENCIES["pathspec"] = True
        log('DEBUG', 'knowledge.deps', 'pathspec imported successfully')
    except ImportError as e:
        DEPENDENCIES["pathspec"] = False
        missing.append("pathspec")
        log('DEBUG', 'knowledge.deps', f'pathspec import failed: {e}')


    # All four are required for full RAG functionality
    all_available = all(DEPENDENCIES.values())

    # Update global RAG_AVAILABLE
    global RAG_AVAILABLE
    RAG_AVAILABLE = all_available

    if not all_available:
        error_msg = f"Missing RAG dependencies: {', '.join(missing)}. Please install with: pip install {' '.join(missing)}"
        log('WARNING', 'knowledge.deps', f'RAG dependencies missing: {missing}. RAG tools will be disabled or use fallback behavior.')
        return False, error_msg
    else:
        log('INFO', 'knowledge.deps', 'All RAG dependencies available')
        return True, None