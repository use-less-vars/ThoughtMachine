"""
Base Knowledge Base interface.

Defines a consistent API for all knowledge bases (codebase, notebook, future MCP sources).
"""

from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional


class BaseKnowledgeBase(ABC):
    """
    Abstract base class for all knowledge bases.
    
    A knowledge base provides semantic search over a collection of documents.
    Documents can be code snippets, notes, or any structured text.
    
    All implementations should inherit from this class and provide:
    - search(query, top_k): returns a list of search result dicts
    - optionally add(content, metadata): for writable knowledge bases
    """
    
    @abstractmethod
    def search(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """
        Search the knowledge base for documents relevant to the query.
        
        Args:
            query: Search query string
            top_k: Maximum number of results to return (default: 5)
            
        Returns:
            List of result dictionaries, each containing at least:
                - content: str - The document text
                - metadata: Dict[str, Any] - Document metadata (e.g., file path, line numbers, timestamp)
                - score: float - Similarity score (higher = more relevant)
                
            Example result dict:
                {
                    "content": "def execute_tool(self, tool_name, arguments): ...",
                    "metadata": {
                        "file_path": "agent/core/tool_executor.py",
                        "line_start": 45,
                        "line_end": 60,
                        "language": "python"
                    },
                    "score": 0.87
                }
        """
        pass
    
    def add(self, content: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """
        Add a document to the knowledge base (optional operation).
        
        Args:
            content: Document text to add
            metadata: Optional metadata dict (e.g., source, timestamp, tags)
            
        Raises:
            NotImplementedError: If the knowledge base is read-only
        """
        raise NotImplementedError(f"{self.__class__.__name__} does not support adding documents")
    
    def is_available(self) -> bool:
        """
        Check if the knowledge base is operational.
        
        Returns:
            True if the knowledge base can be used, False otherwise
            (e.g., dependencies missing, index not created)
        """
        return True