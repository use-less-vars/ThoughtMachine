"""
Search Codebase Tool.

Provides semantic search over an indexed codebase using RAG.
"""

import logging
from pathlib import Path
from typing import Literal, ClassVar, List, Dict, Any, Optional

from pydantic import Field

from .base import ToolBase


def _check_rag_dependencies():
    """Lazy import wrapper for RAG dependency check."""
    from agent.knowledge.dependencies import check_rag_dependencies
    return check_rag_dependencies()

def _get_local_codebase_kb():
    """Lazy import wrapper for LocalCodebaseKB class."""
    from agent.knowledge.codebase_kb import LocalCodebaseKB
    return LocalCodebaseKB


logger = logging.getLogger(__name__)


class SearchCodebaseTool(ToolBase):
    """
    Search the indexed codebase using natural language queries.
    
    Requires RAG dependencies (chromadb, sentence-transformers, tree-sitter, pathspec).
    Requires an existing codebase index (created via index-codebase command).
    """
    
    tool: Literal["SearchCodebase"] = "SearchCodebase"
    
    # Security capabilities required by this tool
    requires_capabilities: ClassVar[List[str]] = ["read_files"]
    
    query: str = Field(..., description="Natural language query about the codebase.")
    top_k: int = Field(5, description="Number of results to return.")
    intent: Literal["exact", "broad", "file"] = Field("broad", description="Search intent: 'exact' for high precision, 'broad' for more results, 'file' for file-level results.")
    restrict_to_path: Optional[str] = Field(None, description="Optional path to restrict search to (e.g., 'src/utils/')")
    
    def execute(self) -> str:
        """
        Search the indexed codebase for code relevant to the query.
        
        Returns:
            Markdown-formatted results with code snippets and metadata,
            or error message if dependencies missing or index not found.
        """
        self._log_debug(f"SearchCodebaseTool.execute called with query='{self.query}', top_k={self.top_k}")
        
        # Check RAG dependencies
        success, error_msg = _check_rag_dependencies()
        if not success:
            self._log_tool_warning("RAG dependencies missing, cannot search codebase")
            return "RAG dependencies not installed. Run `pip install chromadb sentence-transformers tree-sitter pathspec`."
        
        # Get workspace path from context or use current directory
        workspace_path = self.workspace_path
        if not workspace_path:
            # Use current directory if workspace_path not provided
            workspace_path = str(Path.cwd())
            self._log_debug(f"No workspace_path provided, using current directory: {workspace_path}")
        
        # Try to get config from context if available
        config = None
        try:
            # Check if context is available (as described in task requirements)
            if hasattr(self, 'context') and hasattr(self.context, 'config') and self.context.config is not None:
                config = self.context.config
            else:
                # Try to import config from agent (fallback for development)
                from agent.config.models import AgentConfig
                # Create minimal config with defaults matching AgentConfig defaults
                config = AgentConfig(
                    workspace_path=workspace_path,
                    # Use default from AgentConfig model
                    rag_vector_store_path=None  # Use default location
                )
        except ImportError as e:
            return f"Error: Cannot create configuration: {e}"
        
        # Create knowledge base instance
        try:
            LocalCodebaseKB = _get_local_codebase_kb()
            kb = LocalCodebaseKB(workspace_path, config)
        except Exception as e:
            return f"Error creating codebase knowledge base: {e}"
        
        # Check if index exists
        if not kb.is_indexed():
            return "No codebase index found. Run `thoughtmachine index-codebase` first to create an index."
        
        # Determine search parameters based on intent
        min_score = None
        where = None
        
        # Map intent to minimum score threshold
        if self.intent == "exact":
            min_score = 0.5
        elif self.intent == "broad":
            min_score = None  # No threshold
        elif self.intent == "file":
            # File-level search (not yet fully implemented - treats as broad)
            min_score = None
            self._log_debug("File-level search intent selected - will group results by file")
        
        # Add path restriction if specified
        if self.restrict_to_path:
            # ChromaDB $contains operator for substring match in file_path metadata
            where = {"file_path": {"$contains": self.restrict_to_path}}
            self._log_debug(f"Restricting search to path: {self.restrict_to_path}")
        
        # Perform search
        try:
            results = kb.search(
                query=self.query,
                top_k=self.top_k,
                min_score=min_score,
                where=where
            )
        except Exception as e:
            return f"Error searching codebase: {e}"
        
        # For file-level intent, group results by file
        if self.intent == "file" and results:
            # Group results by file_path, keep highest scoring result per file
            file_groups = {}
            for result in results:
                metadata = result.get("metadata", {})
                file_path = metadata.get("file_path", "")
                if file_path not in file_groups:
                    file_groups[file_path] = result
                else:
                    # Keep the result with higher score
                    if result.get("score", 0) > file_groups[file_path].get("score", 0):
                        file_groups[file_path] = result
            # Convert back to list
            results = list(file_groups.values())
            self._log_debug(f"File-level grouping: {len(results)} unique files")

        # Format results
        if not results:
            return "No relevant code found."

        return self._format_results(results)    
    def _format_results(self, results: List[Dict[str, Any]]) -> str:
        """
        Format search results as Markdown.
        
        Args:
            results: List of search result dicts from LocalCodebaseKB
            
        Returns:
            Markdown string with formatted results
        """
        output_lines = ["## Codebase Search Results\n"]
        
        for i, result in enumerate(results, 1):
            content = result.get("content", "").strip()
            metadata = result.get("metadata", {})
            score = result.get("score", 0.0)
            
            file_path = metadata.get("file_path", "unknown")
            start_line = metadata.get("start_line", metadata.get("line_start", 1))
            end_line = metadata.get("end_line", metadata.get("line_end", start_line))
            language = metadata.get("language", "")
            
            # Format score as percentage
            score_pct = f"{score * 100:.1f}%"
            
            # Add result header
            if language:
                language_tag = f" ({language})"
            else:
                language_tag = ""
            
            output_lines.append(f"### {i}. `{file_path}`{language_tag} • {score_pct} relevance")
            
            # Add line range if available
            if start_line and end_line:
                if start_line == end_line:
                    output_lines.append(f"**Line {start_line}**")
                else:
                    output_lines.append(f"**Lines {start_line}-{end_line}**")
            
            # Add code snippet with language-specific formatting
            if language.lower() == "python":
                code_fence = "```python"
            elif language.lower() in ["javascript", "typescript", "js", "ts"]:
                code_fence = "```javascript"
            elif language.lower() in ["java", "kotlin", "scala"]:
                code_fence = "```java"
            elif language.lower() in ["go", "golang"]:
                code_fence = "```go"
            elif language.lower() == "rust":
                code_fence = "```rust"
            else:
                code_fence = "```"
            
            output_lines.append(f"{code_fence}")
            output_lines.append(content)
            output_lines.append("```")
            output_lines.append("")  # Empty line between results
        
        return "\n".join(output_lines)