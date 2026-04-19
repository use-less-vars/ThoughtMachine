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
    SEMANTIC CODEBASE SEARCH - Understands natural language queries about your code.
    
    Use this tool when you need to:
    - Find where specific classes/functions are defined
    - Understand how a feature is implemented
    - Discover configuration loading patterns
    - Locate MCP client implementations or other architectural components
    - Search within specific directories (e.g., 'qt_gui/')
    
    ADVANTAGES over FileSearchTool:
    - Understands natural language meaning, not just keywords
    - Returns semantically relevant code snippets with relevance scores
    - Can restrict search to specific paths
    - Three search intents: 'exact' (high precision), 'broad' (more results), 'file' (file-level overview)
    
    Examples:
    - "Find the AgentControlsPanel class definition"
    - "How does configuration loading work?"
    - "Show me MCP client implementation"
    - "Search for token_monitor in qt_gui directory"
    
    Requirements:
    - RAG dependencies (chromadb, sentence-transformers, tree-sitter, pathspec)
    - Existing codebase index (run `thoughtmachine index-codebase` first)
    """    
    tool: Literal["SearchCodebase"] = "SearchCodebase"
    
    # Security capabilities required by this tool
    requires_capabilities: ClassVar[List[str]] = ["read_files"]
    
    query: str = Field(..., description="Natural language query about the codebase. Examples: 'Find the AgentControlsPanel class', 'How does configuration loading work?', 'Show MCP client implementation'.")
    top_k: int = Field(5, description="Number of results to return. Default 5, increase for broader searches, decrease for precision.")
    intent: Literal["exact", "broad", "file"] = Field("broad", description="Search intent: 'exact' (high precision, min score 0.5), 'broad' (more results), 'file' (file-level overview, one result per file).")
    restrict_to_path: Optional[str] = Field(None, description="Optional path to restrict search to (e.g., 'qt_gui/', 'tools/'). Only returns results from files under this path.")
    
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
        
        # Perform search without where filter initially
        # ChromaDB's $contains filter is broken in version 1.5.7, so we'll filter client-side
        try:
            # Get more results than needed because we'll filter client-side
            search_top_k = min(self.top_k * 3, 50)  # Get up to 50 results for filtering
            raw_results = kb.search(
                query=self.query,
                top_k=search_top_k,
                min_score=min_score,
                where=None  # Don't use broken $contains filter
            )
            
            # Filter results by restrict_to_path if specified
            results = []
            if self.restrict_to_path:
                # Normalize the path restriction
                restrict_path = self.restrict_to_path
                if not restrict_path.endswith('/') and not restrict_path.endswith('\\'):
                    # For better matching, ensure path separator for directory matching
                    restrict_path = restrict_path + '/'
                
                self._log_debug(f"Filtering results by path: {restrict_path}")
                filtered_count = 0
                for result in raw_results:
                    metadata = result.get("metadata", {})
                    file_path = metadata.get("file_path", "")
                    
                    # Check if restrict_path appears in file_path
                    # Works for both absolute and relative paths
                    if restrict_path in file_path:
                        results.append(result)
                        filtered_count += 1
                    else:
                        # Try without the trailing slash too
                        if self.restrict_to_path in file_path:
                            results.append(result)
                            filtered_count += 1
                        else:
                            self._log_debug(f"Skipping file (does not match {restrict_path}): {file_path[:80]}...")
                
                self._log_debug(f"Path filtering: {len(raw_results)} raw results -> {filtered_count} matched {self.restrict_to_path}")
            else:
                results = raw_results
                
            # Trim to requested top_k
            results = results[:self.top_k]
            
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
            return "No relevant code found.\n\n💡 **Search Tips**:\n- Try rephrasing your query in natural language (e.g., 'Find class definitions' instead of 'class')\n- Use `intent='broad'` for more results\n- Remove `restrict_to_path` if set\n- Increase `top_k` value\n- The index may not contain the specific code you're looking for"

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
        
        # Add helpful tip about tool usage
        output_lines.append("---")
        output_lines.append("**💡 Semantic Search Tip**: This tool understands natural language queries about code structure. Try queries like:")
        output_lines.append("- \"Find where [class/function] is defined\"")
        output_lines.append("- \"How does [feature] work in this project?\"")
        output_lines.append("- \"Show me the implementation of [component]\"")
        output_lines.append("- \"Search for [term] in [directory]/\" (use restrict_to_path parameter)")
        output_lines.append("Use `intent='exact'` for high precision, `intent='broad'` for more results, or `intent='file'` for file-level overview.")
        
        return "\n".join(output_lines)