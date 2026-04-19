"""
Codebase indexer for RAG.

Scans a workspace, chunks code with tree-sitter, embeds with sentence-transformers,
and stores vectors in ChromaDB for semantic search.
"""

import hashlib
import logging
import os
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
import fnmatch
import gc
import time

from agent.config.models import AgentConfig
import agent.knowledge.dependencies as deps
import atexit
import signal
import sys

# Global client for cleanup
_chroma_client = None


def _cleanup_client():
    if '_chroma_client' in globals() and _chroma_client:
        try:
            _chroma_client._system.stop()
        except Exception:
            pass

def _signal_handler(sig, frame):
    _cleanup_client()
    sys.exit(0)

atexit.register(_cleanup_client)
signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)
logger = logging.getLogger(__name__)


def split_text_with_overlap(text: str, chunk_size: int = 1500, chunk_overlap: int = 200) -> List[str]:
    """
    Split text into chunks with overlap, trying to preserve line boundaries.
    
    Args:
        text: Text to split
        chunk_size: Maximum characters per chunk (default 1500)
        chunk_overlap: Overlap between chunks in characters (default 200)
        
    Returns:
        List of text chunks
    """
    if len(text) <= chunk_size:
        return [text]
    
    chunks = []
    start = 0
    
    while start < len(text):
        # Calculate end position
        end = start + chunk_size
        
        # If we're at the end of the text
        if end >= len(text):
            chunks.append(text[start:])
            break
        
        # Try to find a line break near the end
        line_break = text.rfind('\n', start, end)
        if line_break != -1 and line_break > start + chunk_size // 2:
            # Found a line break in the second half of the chunk
            end = line_break + 1  # Include the newline
        
        chunks.append(text[start:end])
        
        # Move start forward, accounting for overlap
        start = end - chunk_overlap
        
        # Ensure we make progress
        if start >= end:
            start = end
    
    return chunks


def compute_workspace_hash(workspace_path: str) -> str:
    """
    Compute a deterministic hash for the workspace.
    
    Uses the normalized absolute path of the workspace.
    """
    abs_path = os.path.abspath(workspace_path)
    # Normalize path separators
    normalized = os.path.normpath(abs_path)
    # Compute hash
    return hashlib.md5(normalized.encode()).hexdigest()[:12]


_GITIGNORE_CACHE = {}


def load_gitignore_spec(workspace_path: Path):
    """Load .gitignore patterns from workspace using pathspec."""
    global _GITIGNORE_CACHE
    
    if workspace_path in _GITIGNORE_CACHE:
        return _GITIGNORE_CACHE[workspace_path]
    
    # If pathspec is not available, return None
    if not deps.DEPENDENCIES.get("pathspec", False):
        _GITIGNORE_CACHE[workspace_path] = None
        return None
    
    try:
        import pathspec
        
        # Collect all .gitignore files in the workspace
        patterns = []
        for gitignore_path in workspace_path.rglob(".gitignore"):
            try:
                with open(gitignore_path, 'r', encoding='utf-8') as f:
                    # Read patterns, skip empty lines and comments
                    lines = [line.strip() for line in f.readlines()]
                    for line in lines:
                        if line and not line.startswith('#'):
                            # Convert to relative path pattern
                            rel_path = gitignore_path.parent.relative_to(workspace_path)
                            if rel_path == Path('.'):
                                patterns.append(line)
                            else:
                                # Pattern is relative to subdirectory
                                patterns.append(f"{rel_path}/{line}")
            except Exception as e:
                logger.warning(f"Could not read {gitignore_path}: {e}")
                continue
        
        # Also include global .gitignore if exists
        global_gitignore = workspace_path / ".gitignore"
        if global_gitignore.exists():
            try:
                with open(global_gitignore, 'r', encoding='utf-8') as f:
                    lines = [line.strip() for line in f.readlines()]
                    for line in lines:
                        if line and not line.startswith('#'):
                            patterns.append(line)
            except Exception as e:
                logger.warning(f"Could not read global .gitignore: {e}")
        
        if patterns:
            spec = pathspec.PathSpec.from_lines('gitwildmatch', patterns)
            _GITIGNORE_CACHE[workspace_path] = spec
            logger.debug(f"Loaded {len(patterns)} gitignore patterns for {workspace_path}")
        else:
            _GITIGNORE_CACHE[workspace_path] = None
            logger.debug(f"No gitignore patterns found for {workspace_path}")
        
        return _GITIGNORE_CACHE[workspace_path]
    
    except Exception as e:
        logger.warning(f"Failed to load gitignore patterns: {e}")
        _GITIGNORE_CACHE[workspace_path] = None
        return None


def should_skip_file(file_path: Path, workspace_path: Path) -> bool:
    """
    Determine if a file should be skipped based on .gitignore patterns and heuristics.
    """
    # Skip hidden files and directories (except .gitignore itself)
    if any(part.startswith('.') for part in file_path.parts):
        # Allow .gitignore files to be read
        if file_path.name == '.gitignore':
            pass
        else:
            return True
    
    # Skip common cache/build directories
    skip_dirs = {
        '__pycache__', '.git', '.svn', '.hg', '.idea', '.vscode',
        'node_modules', 'build', 'dist', 'target', 'venv', '.env',
        '.pytest_cache', '.mypy_cache', '.coverage'
    }
    if any(part in skip_dirs for part in file_path.parts):
        return True
    
    # Skip common binary/archive extensions
    skip_extensions = {
        '.pyc', '.pyo', '.pyd', '.so', '.dll', '.exe',
        '.zip', '.tar', '.gz', '.bz2', '.xz', '.rar',
        '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.ico',
        '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt',
        '.pptx', '.mp3', '.mp4', '.avi', '.mov', '.wmv',
        '.db', '.sqlite', '.sqlite3'
    }
    if file_path.suffix.lower() in skip_extensions:
        return True
    
    # Check against .gitignore patterns if available
    spec = load_gitignore_spec(workspace_path)
    if spec is not None:
        # Convert file path relative to workspace
        try:
            rel_path = file_path.relative_to(workspace_path)
            # pathspec expects forward slashes
            rel_str = str(rel_path).replace('\\', '/')
            if spec.match_file(rel_str):
                logger.debug(f"File {rel_path} matches gitignore pattern")
                return True
        except ValueError:
            # File not relative to workspace (should not happen)
            pass
    
    return False


def get_supported_languages() -> Dict[str, Any]:
    """
    Get tree-sitter language parsers for supported languages.
    
    Returns a dict mapping file extensions to language parsers.
    Currently supports Python, JavaScript, TypeScript, Java, Go, and Rust.
    """
    if not deps.DEPENDENCIES.get("tree_sitter", False):
        return {}
    
    try:
        from tree_sitter import Language, Parser
        
        # Initialize language parsers lazily
        languages = {}
        
        # Try to load Python parser
        try:
            from tree_sitter_python import language as python_language
            languages["python"] = python_language()
        except ImportError:
            logger.warning("tree_sitter_python not installed. Python parsing disabled.")
        
        # Try to load JavaScript/TypeScript parser  
        try:
            from tree_sitter_javascript import language as js_language
            languages["javascript"] = js_language()
            languages["typescript"] = js_language()  # TypeScript uses same parser
        except ImportError:
            logger.warning("tree_sitter_javascript not installed. JavaScript/TypeScript parsing disabled.")
        
        # Try to load Java parser
        try:
            from tree_sitter_java import language as java_language
            languages["java"] = java_language()
        except ImportError:
            logger.warning("tree_sitter_java not installed. Java parsing disabled.")
        
        # Try to load Go parser
        try:
            from tree_sitter_go import language as go_language
            languages["go"] = go_language()
        except ImportError:
            logger.warning("tree_sitter_go not installed. Go parsing disabled.")
        
        # Try to load Rust parser
        try:
            from tree_sitter_rust import language as rust_language
            languages["rust"] = rust_language()
        except ImportError:
            logger.warning("tree_sitter_rust not installed. Rust parsing disabled.")
        
        return languages
    
    except Exception as e:
        logger.warning(f"Failed to load tree-sitter languages: {e}")
        return {}


def parse_file_with_tree_sitter(file_path: Path, language: str, config: AgentConfig) -> List[Dict[str, Any]]:
    """
    Parse a file with tree-sitter and extract logical units.
    
    Args:
        file_path: Path to the file
        language: Language identifier ('python', 'javascript', etc.)
    
    Returns:
        List of chunks, each with:
            - content: str
            - metadata: dict with file_path, line_start, line_end, language, unit_type
    """
    # Fallback if tree-sitter is not available
    if not deps.DEPENDENCIES.get("tree_sitter", False):
        return _fallback_file_chunk(file_path, language, chunk_size=config.rag_chunk_size, chunk_overlap=config.rag_chunk_overlap)
    
    # Get language parsers
    languages = get_supported_languages()
    if language not in languages:
        logger.debug(f"No tree-sitter parser for language '{language}', using fallback")
        return _fallback_file_chunk(file_path, language, chunk_size=config.rag_chunk_size, chunk_overlap=config.rag_chunk_overlap)
    
    try:
        content = file_path.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        logger.warning(f"Cannot decode {file_path} as UTF-8, skipping")
        return []
    except Exception as e:
        logger.warning(f"Error reading {file_path}: {e}")
        return []
    
    try:
        from tree_sitter import Parser
        
        # Create parser and set language
        parser = Parser()
        parser.set_language(languages[language])
        
        # Parse the file
        tree = parser.parse(bytes(content, 'utf-8'))
        root_node = tree.root_node
        
        # Extract chunks based on language
        chunks = []
        
        if language == "python":
            chunks = _extract_python_chunks(content, root_node, file_path, language)
        elif language in ["javascript", "typescript"]:
            chunks = _extract_javascript_chunks(content, root_node, file_path, language)
        elif language == "java":
            chunks = _extract_java_chunks(content, root_node, file_path, language)
        elif language == "go":
            chunks = _extract_go_chunks(content, root_node, file_path, language)
        elif language == "rust":
            chunks = _extract_rust_chunks(content, root_node, file_path, language)
        else:
            # For unsupported languages, use file-level chunking
            logger.debug(f"No specific extractor for language '{language}', using fallback")
            return _fallback_file_chunk(file_path, language, chunk_size=config.rag_chunk_size, chunk_overlap=config.rag_chunk_overlap)
        
        # If no semantic chunks found, fall back to file chunk
        if not chunks:
            return _fallback_file_chunk(file_path, language, chunk_size=config.rag_chunk_size, chunk_overlap=config.rag_chunk_overlap)
        
        return chunks
        
    except Exception as e:
        logger.warning(f"Tree-sitter parsing failed for {file_path}: {e}")
        # Fall back to file-level chunking
        return _fallback_file_chunk(file_path, language, chunk_size=config.rag_chunk_size, chunk_overlap=config.rag_chunk_overlap)


def _fallback_file_chunk(file_path: Path, language: str, chunk_size: int = 1500, chunk_overlap: int = 200) -> List[Dict[str, Any]]:
    """
    Create chunks for the entire file when parsing fails.
    
    Args:
        file_path: Path to the file
        language: Programming language
        chunk_size: Maximum characters per chunk (default 1500)
        chunk_overlap: Overlap between chunks in characters (default 200)
    """
    try:
        content = file_path.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        logger.warning(f"Cannot decode {file_path} as UTF-8, skipping")
        return []
    except Exception as e:
        logger.warning(f"Error reading {file_path}: {e}")
        return []
    
    # Split content into chunks with overlap
    text_chunks = split_text_with_overlap(content, chunk_size, chunk_overlap)
    
    if not text_chunks:
        return []
    
    chunks = []
    total_lines = content.count('\n') + 1 if content else 1
    
    for chunk_index, chunk_content in enumerate(text_chunks):
        # Calculate approximate line numbers for this chunk
        # This is approximate since chunks may split lines
        if chunk_index == 0:
            line_start = 1
        else:
            # Estimate based on previous content
            prev_content = ''.join(text_chunks[:chunk_index])
            line_start = prev_content.count('\n') + 2
        
        line_end = line_start + chunk_content.count('\n')
        
        chunks.append({
            "content": chunk_content,
            "metadata": {
                "file_path": str(file_path),
                "line_start": line_start,
                "line_end": min(line_end, total_lines),
                "language": language,
                "unit_type": "file_chunk",
                "chunk_index": chunk_index,
                "total_chunks": len(text_chunks)
            }
        })
    
    logger.debug(f"Created {len(chunks)} fallback chunks for {file_path}")
    return chunks

def _extract_python_chunks(content: str, root_node, file_path: Path, language: str) -> List[Dict[str, Any]]:
    """Extract Python functions, classes, and methods."""
    chunks = []
    
    # Traverse tree and find function/class definitions
    from tree_sitter import Node
    
    def traverse(node: Node):
        # Check for function definitions (async and regular)
        if node.type in ['function_definition', 'async_function_definition']:
            # Get function name
            for child in node.children:
                if child.type == 'identifier':
                    func_name = content[child.start_byte:child.end_byte]
                    break
            else:
                func_name = "anonymous"
            
            # Get function body
            func_content = content[node.start_byte:node.end_byte]
            lines = content[:node.start_byte].count('\n')
            start_line = lines + 1
            end_line = start_line + func_content.count('\n')
            
            chunks.append({
                "content": func_content,
                "metadata": {
                    "file_path": str(file_path),
                    "line_start": start_line,
                    "line_end": end_line,
                    "language": language,
                    "unit_type": "function",
                    "name": func_name
                }
            })
        
        # Check for class definitions
        elif node.type == 'class_definition':
            # Get class name
            for child in node.children:
                if child.type == 'identifier':
                    class_name = content[child.start_byte:child.end_byte]
                    break
            else:
                class_name = "anonymous"
            
            # Get class body
            class_content = content[node.start_byte:node.end_byte]
            lines = content[:node.start_byte].count('\n')
            start_line = lines + 1
            end_line = start_line + class_content.count('\n')
            
            chunks.append({
                "content": class_content,
                "metadata": {
                    "file_path": str(file_path),
                    "line_start": start_line,
                    "line_end": end_line,
                    "language": language,
                    "unit_type": "class",
                    "name": class_name
                }
            })
        
        # Recursively traverse children
        for child in node.children:
            traverse(child)
    
    traverse(root_node)
    return chunks


def _extract_javascript_chunks(content: str, root_node, file_path: Path, language: str) -> List[Dict[str, Any]]:
    """Extract JavaScript/TypeScript functions, classes, and methods."""
    chunks = []
    
    from tree_sitter import Node
    
    def traverse(node: Node):
        # Function declarations
        if node.type in ['function_declaration', 'generator_function_declaration',
                         'arrow_function', 'function_expression']:
            # Try to get function name
            func_name = "anonymous"
            for child in node.children:
                if child.type == 'identifier':
                    func_name = content[child.start_byte:child.end_byte]
                    break
                elif child.type == 'formal_parameters':
                    # Anonymous function
                    pass
            
            func_content = content[node.start_byte:node.end_byte]
            lines = content[:node.start_byte].count('\n')
            start_line = lines + 1
            end_line = start_line + func_content.count('\n')
            
            chunks.append({
                "content": func_content,
                "metadata": {
                    "file_path": str(file_path),
                    "line_start": start_line,
                    "line_end": end_line,
                    "language": language,
                    "unit_type": "function",
                    "name": func_name
                }
            })
        
        # Class declarations
        elif node.type == 'class_declaration':
            # Get class name
            class_name = "anonymous"
            for child in node.children:
                if child.type == 'identifier':
                    class_name = content[child.start_byte:child.end_byte]
                    break
            
            class_content = content[node.start_byte:node.end_byte]
            lines = content[:node.start_byte].count('\n')
            start_line = lines + 1
            end_line = start_line + class_content.count('\n')
            
            chunks.append({
                "content": class_content,
                "metadata": {
                    "file_path": str(file_path),
                    "line_start": start_line,
                    "line_end": end_line,
                    "language": language,
                    "unit_type": "class",
                    "name": class_name
                }
            })
        
        # Method definitions (inside classes)
        elif node.type == 'method_definition':
            # Get method name
            method_name = "anonymous"
            for child in node.children:
                if child.type == 'property_identifier':
                    method_name = content[child.start_byte:child.end_byte]
                    break
            
            method_content = content[node.start_byte:node.end_byte]
            lines = content[:node.start_byte].count('\n')
            start_line = lines + 1
            end_line = start_line + method_content.count('\n')
            
            chunks.append({
                "content": method_content,
                "metadata": {
                    "file_path": str(file_path),
                    "line_start": start_line,
                    "line_end": end_line,
                    "language": language,
                    "unit_type": "method",
                    "name": method_name
                }
            })
        
        # Recursively traverse children
        for child in node.children:
            traverse(child)
    
    traverse(root_node)
    return chunks


def _extract_java_chunks(content: str, root_node, file_path: Path, language: str) -> List[Dict[str, Any]]:
    """Extract Java classes, methods, and fields."""
    chunks = []
    
    from tree_sitter import Node
    
    def traverse(node: Node):
        # Class declarations
        if node.type == 'class_declaration':
            # Get class name
            class_name = "anonymous"
            for child in node.children:
                if child.type == 'identifier':
                    class_name = content[child.start_byte:child.end_byte]
                    break
            
            class_content = content[node.start_byte:node.end_byte]
            lines = content[:node.start_byte].count('\n')
            start_line = lines + 1
            end_line = start_line + class_content.count('\n')
            
            chunks.append({
                "content": class_content,
                "metadata": {
                    "file_path": str(file_path),
                    "line_start": start_line,
                    "line_end": end_line,
                    "language": language,
                    "unit_type": "class",
                    "name": class_name
                }
            })
        
        # Method declarations
        elif node.type == 'method_declaration':
            # Get method name
            method_name = "anonymous"
            for child in node.children:
                if child.type == 'identifier':
                    method_name = content[child.start_byte:child.end_byte]
                    break
            
            method_content = content[node.start_byte:node.end_byte]
            lines = content[:node.start_byte].count('\n')
            start_line = lines + 1
            end_line = start_line + method_content.count('\n')
            
            chunks.append({
                "content": method_content,
                "metadata": {
                    "file_path": str(file_path),
                    "line_start": start_line,
                    "line_end": end_line,
                    "language": language,
                    "unit_type": "method",
                    "name": method_name
                }
            })
        
        # Recursively traverse children
        for child in node.children:
            traverse(child)
    
    traverse(root_node)
    return chunks


def _extract_go_chunks(content: str, root_node, file_path: Path, language: str) -> List[Dict[str, Any]]:
    """Extract Go functions, methods, and types."""
    chunks = []
    
    from tree_sitter import Node
    
    def traverse(node: Node):
        # Function declarations
        if node.type == 'function_declaration':
            # Get function name
            func_name = "anonymous"
            for child in node.children:
                if child.type == 'identifier':
                    func_name = content[child.start_byte:child.end_byte]
                    break
            
            func_content = content[node.start_byte:node.end_byte]
            lines = content[:node.start_byte].count('\n')
            start_line = lines + 1
            end_line = start_line + func_content.count('\n')
            
            chunks.append({
                "content": func_content,
                "metadata": {
                    "file_path": str(file_path),
                    "line_start": start_line,
                    "line_end": end_line,
                    "language": language,
                    "unit_type": "function",
                    "name": func_name
                }
            })
        
        # Method declarations
        elif node.type == 'method_declaration':
            # Get method name
            method_name = "anonymous"
            for child in node.children:
                if child.type == 'field_identifier':
                    method_name = content[child.start_byte:child.end_byte]
                    break
            
            method_content = content[node.start_byte:node.end_byte]
            lines = content[:node.start_byte].count('\n')
            start_line = lines + 1
            end_line = start_line + method_content.count('\n')
            
            chunks.append({
                "content": method_content,
                "metadata": {
                    "file_path": str(file_path),
                    "line_start": start_line,
                    "line_end": end_line,
                    "language": language,
                    "unit_type": "method",
                    "name": method_name
                }
            })
        
        # Type declarations (struct, interface, etc.)
        elif node.type in ['type_declaration', 'type_spec']:
            # Get type name
            type_name = "anonymous"
            for child in node.children:
                if child.type == 'type_identifier':
                    type_name = content[child.start_byte:child.end_byte]
                    break
            
            type_content = content[node.start_byte:node.end_byte]
            lines = content[:node.start_byte].count('\n')
            start_line = lines + 1
            end_line = start_line + type_content.count('\n')
            
            chunks.append({
                "content": type_content,
                "metadata": {
                    "file_path": str(file_path),
                    "line_start": start_line,
                    "line_end": end_line,
                    "language": language,
                    "unit_type": "type",
                    "name": type_name
                }
            })
        
        # Recursively traverse children
        for child in node.children:
            traverse(child)
    
    traverse(root_node)
    return chunks


def _extract_rust_chunks(content: str, root_node, file_path: Path, language: str) -> List[Dict[str, Any]]:
    """Extract Rust functions, structs, enums, and impls."""
    chunks = []
    
    from tree_sitter import Node
    
    def traverse(node: Node):
        # Function definitions
        if node.type == 'function_item':
            # Get function name
            func_name = "anonymous"
            for child in node.children:
                if child.type == 'identifier':
                    func_name = content[child.start_byte:child.end_byte]
                    break
            
            func_content = content[node.start_byte:node.end_byte]
            lines = content[:node.start_byte].count('\n')
            start_line = lines + 1
            end_line = start_line + func_content.count('\n')
            
            chunks.append({
                "content": func_content,
                "metadata": {
                    "file_path": str(file_path),
                    "line_start": start_line,
                    "line_end": end_line,
                    "language": language,
                    "unit_type": "function",
                    "name": func_name
                }
            })
        
        # Struct definitions
        elif node.type == 'struct_item':
            # Get struct name
            struct_name = "anonymous"
            for child in node.children:
                if child.type == 'type_identifier':
                    struct_name = content[child.start_byte:child.end_byte]
                    break
            
            struct_content = content[node.start_byte:node.end_byte]
            lines = content[:node.start_byte].count('\n')
            start_line = lines + 1
            end_line = start_line + struct_content.count('\n')
            
            chunks.append({
                "content": struct_content,
                "metadata": {
                    "file_path": str(file_path),
                    "line_start": start_line,
                    "line_end": end_line,
                    "language": language,
                    "unit_type": "struct",
                    "name": struct_name
                }
            })
        
        # Impl blocks
        elif node.type == 'impl_item':
            # Get type name for impl
            impl_type = "anonymous"
            for child in node.children:
                if child.type == 'type_identifier':
                    impl_type = content[child.start_byte:child.end_byte]
                    break
            
            impl_content = content[node.start_byte:node.end_byte]
            lines = content[:node.start_byte].count('\n')
            start_line = lines + 1
            end_line = start_line + impl_content.count('\n')
            
            chunks.append({
                "content": impl_content,
                "metadata": {
                    "file_path": str(file_path),
                    "line_start": start_line,
                    "line_end": end_line,
                    "language": language,
                    "unit_type": "impl",
                    "name": impl_type
                }
            })
        
        # Recursively traverse children
        for child in node.children:
            traverse(child)
    
    traverse(root_node)
    return chunks


def chunk_codebase(workspace_path: Path, config: AgentConfig) -> List[Dict[str, Any]]:
    """
    Walk through workspace and chunk all supported files.
    
    Returns a list of chunks ready for embedding.
    """
    chunks = []
    supported_extensions = {
        '.py': 'python',
        '.js': 'javascript',
        '.jsx': 'javascript',
        '.ts': 'typescript',
        '.tsx': 'typescript',
        '.java': 'java',
        '.c': 'c',
        '.cpp': 'cpp',
        '.h': 'c',
        '.hpp': 'cpp',
        '.go': 'go',
        '.rs': 'rust',
        '.rb': 'ruby',
        '.php': 'php',
        '.swift': 'swift',
        '.kt': 'kotlin',
        '.scala': 'scala',
        '.cs': 'csharp',
        '.fs': 'fsharp',
        '.vb': 'vb',
        '.sql': 'sql',
        '.html': 'html',
        '.css': 'css',
        '.json': 'json',
        '.yaml': 'yaml',
        '.yml': 'yaml',
        '.toml': 'toml',
        '.xml': 'xml',
        '.sh': 'bash',
        '.bash': 'bash',
        '.md': 'markdown',
        '.txt': 'text',
    }
    
    for ext, lang in supported_extensions.items():
        for file_path in workspace_path.rglob(f"*{ext}"):
            if should_skip_file(file_path, workspace_path):
                continue
            
            relative_path = file_path.relative_to(workspace_path)
            logger.debug(f"Processing {relative_path}")
            
            file_chunks = parse_file_with_tree_sitter(file_path, lang, config)
            chunks.extend(file_chunks)
    
    return chunks


def create_or_get_chroma_collection(workspace_hash: str, config: AgentConfig, force: bool = False):
    """
    Create or get a ChromaDB collection for the workspace.
    
    Args:
        workspace_hash: Unique hash for the workspace
        config: AgentConfig with vector store path
        force: If True, delete existing collection and create new
        
    Returns:
        ChromaDB collection object
    """
    if not deps.DEPENDENCIES.get("chromadb", False):
        raise ImportError("chromadb is not available")
    
    import chromadb
    from chromadb.config import Settings
    
    # Determine vector store path
    if config.rag_vector_store_path:
        vector_store_path = Path(config.rag_vector_store_path)
    else:
        # Default: .thoughtmachine/rag/ under workspace
        workspace_root = Path(config.workspace_path) if config.workspace_path else Path.cwd()
        vector_store_path = workspace_root / ".thoughtmachine" / "rag"
    
    # Ensure directory exists
    vector_store_path.mkdir(parents=True, exist_ok=True)
    
    # Create ChromaDB client with persistent storage
    client = chromadb.PersistentClient(
        path=str(vector_store_path),
        settings=Settings(anonymized_telemetry=False)
    )
    global _chroma_client
    _chroma_client = client
    
    collection_name = f"codebase_{workspace_hash}"
    
    # Delete existing collection if force=True
    if force:
        try:
            client.delete_collection(collection_name)
            logger.info(f"Deleted existing collection: {collection_name}")
        except Exception:
            # Collection may not exist, ignore
            pass
    
    # Get or create collection
    try:
        collection = client.get_collection(collection_name)
        logger.info(f"Using existing collection: {collection_name}")
    except Exception:
        collection = client.create_collection(
            name=collection_name,
            metadata={"description": f"Codebase embeddings for workspace {workspace_hash}"}
        )
        logger.info(f"Created new collection: {collection_name}")
    
    return collection
def embed_chunks_batched(chunks: List[Dict[str, Any]], config: AgentConfig, collection, workspace_hash: str, batch_size: int = 32, truncate_dim: int = 256):
    """
    Embed chunks using sentence-transformers in batches and add to collection.

    Args:
        chunks: List of chunk dicts
        config: AgentConfig with embedding model
        collection: ChromaDB collection to add embeddings to
        workspace_hash: Workspace hash for progress logging
        batch_size: Number of chunks to process at once
        truncate_dim: Dimension to truncate embeddings to (default 256)
    """
    if not deps.DEPENDENCIES.get("sentence_transformers", False):
        raise ImportError("sentence_transformers is not available")
    
    from sentence_transformers import SentenceTransformer
    import torch

    # Load model with GPU acceleration if available
    model_name = config.rag_embedding_model
    
    # Check for CUDA availability
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    logger.info(f"Loading embedding model: {model_name} (device: {device})")
    
    # Load model with appropriate device
    model = SentenceTransformer(model_name, device=device)
    total_chunks = len(chunks)
    processed = 0
    
    # Process in batches
    for i in range(0, total_chunks, batch_size):
        batch = chunks[i:i + batch_size]
        
        # Extract texts for this batch
        texts = [chunk["content"] for chunk in batch]
        metadatas = [chunk["metadata"] for chunk in batch]
        
        # Generate embeddings for this batch
        logger.debug(f"Embedding batch {i//batch_size + 1}/{(total_chunks + batch_size - 1)//batch_size} ({len(batch)} chunks)")
        
        # Use truncate_dim to reduce memory (e.g., 256 dimensions instead of default model dimensions)
        # This dramatically reduces memory usage with minimal quality loss
        embeddings = model.encode(texts, truncate_dim=truncate_dim, show_progress_bar=False)
        
        # Generate IDs for this batch
        ids = [f"chunk_{workspace_hash}_{i + j:06d}" for j in range(len(batch))]
        
        # Add to collection immediately
        collection.upsert(
            embeddings=embeddings.tolist() if hasattr(embeddings, "tolist") else embeddings,
            documents=texts,
            metadatas=metadatas,
            ids=ids
        )
        
        processed += len(batch)
        
        # Show progress every 10 batches or at the end
        if (i // batch_size) % 10 == 0 or i + batch_size >= total_chunks:
            logger.info(f"Processed {processed}/{total_chunks} chunks ({processed/total_chunks*100:.1f}%)")
        
        # Force garbage collection to free memory
        del embeddings
        gc.collect()
        time.sleep(0.05)  # Small pause to let GC work
    
    logger.info(f"Finished embedding all {total_chunks} chunks")
    return processed

def index_codebase(workspace_path: str, config: AgentConfig, force: bool = False) -> Tuple[bool, str]:

    """
    Main entry point: index a codebase for semantic search.
    
    Args:
        workspace_path: Path to the workspace directory
        config: AgentConfig with RAG settings
        force: If True, delete existing index and re-index from scratch
        
    Returns:
        Tuple of (success, message)
        - success: True if indexing succeeded, False otherwise
        - message: Descriptive message about the result
    """
    if not deps.RAG_AVAILABLE:
        msg = "RAG dependencies not available. Cannot index codebase."
        logger.error(msg)
        return False, msg
    
    workspace_path = Path(workspace_path)
    if not workspace_path.exists():
        msg = f"Workspace path does not exist: {workspace_path}"
        logger.error(msg)
        return False, msg
    if not workspace_path.is_dir():
        msg = f"Workspace path is not a directory: {workspace_path}"
        logger.error(msg)
        return False, msg
    
    logger.info(f"Starting codebase indexing for: {workspace_path}")
    
    try:
        # Compute workspace hash
        workspace_hash = compute_workspace_hash(str(workspace_path))
        logger.info(f"Workspace hash: {workspace_hash}")
        
        # Chunk the codebase
        logger.info("Scanning and chunking codebase...")
        chunks = chunk_codebase(workspace_path, config)
        if not chunks:
            msg = "No code chunks found. Is the workspace empty?"
            logger.warning(msg)
            return False, msg
        
        logger.info(f"Found {len(chunks)} code chunks")
        
        # Create ChromaDB collection (with force option)
        collection = create_or_get_chroma_collection(workspace_hash, config, force=force)
        
        # Generate embeddings and add to collection in batches
        logger.info("Generating embeddings and adding to vector store in batches...")
        processed_count = embed_chunks_batched(chunks, config, collection, workspace_hash, batch_size=16)
        
        if processed_count != len(chunks):
            logger.warning(f"Processed {processed_count} chunks but expected {len(chunks)}")
        
        # Count total documents
        count = collection.count()
        msg = f"Indexed {len(chunks)} chunks successfully. Collection now contains {count} documents."
        logger.info(msg)
        
        return True, msg
        
    except Exception as e:
        msg = f"Indexing failed: {e}"
        logger.exception(msg)
        return False, msg


def incremental_index(workspace_path: str, config: AgentConfig) -> Tuple[bool, str]:
    """
    Incrementally update an existing codebase index.
    
    This function tracks file modifications since the last indexing run and
    only processes files that have changed or been added.
    
    Args:
        workspace_path: Path to the workspace directory
        config: AgentConfig with RAG settings
        
    Returns:
        Tuple of (success, message)
        - success: True if incremental indexing succeeded, False otherwise
        - message: Descriptive message about the result
    """
    if not deps.RAG_AVAILABLE:
        msg = "RAG dependencies not available. Cannot perform incremental indexing."
        logger.error(msg)
        return False, msg
    
    workspace_path = Path(workspace_path)
    if not workspace_path.exists():
        msg = f"Workspace path does not exist: {workspace_path}"
        logger.error(msg)
        return False, msg
    if not workspace_path.is_dir():
        msg = f"Workspace path is not a directory: {workspace_path}"
        logger.error(msg)
        return False, msg
    
    logger.info(f"Starting incremental indexing for: {workspace_path}")
    
    try:
        # Compute workspace hash
        workspace_hash = compute_workspace_hash(str(workspace_path))
        logger.info(f"Workspace hash: {workspace_hash}")
        
        # Load or create index state file
        state_file = Path(config.vector_store_path) / f"index_state_{workspace_hash}.json"
        index_state = {}
        if state_file.exists():
            try:
                import json
                with open(state_file, 'r', encoding='utf-8') as f:
                    index_state = json.load(f)
                logger.info(f"Loaded existing index state from: {state_file}")
            except Exception as e:
                logger.warning(f"Failed to load index state: {e}. Creating new state.")
        
        # Get collection to check existing documents
        collection = create_or_get_chroma_collection(workspace_hash, config, force=False)
        existing_count = collection.count()
        logger.info(f"Collection currently contains {existing_count} documents")
        
        # Scan for modified files
        logger.info("Scanning for modified files...")
        modified_files = []
        deleted_files = []
        
        # Track current file modifications
        for file_path in workspace_path.rglob("*"):
            if should_skip_file(file_path, workspace_path):
                continue
            
            if not file_path.is_file():
                continue
            
            # Get file stats
            try:
                mtime = file_path.stat().st_mtime
                file_size = file_path.stat().st_size
                file_info = {
                    "path": str(file_path.relative_to(workspace_path)),
                    "mtime": mtime,
                    "size": file_size
                }
                
                # Check if file has changed
                file_key = str(file_path.relative_to(workspace_path))
                if file_key in index_state:
                    old_info = index_state[file_key]
                    if old_info["mtime"] == mtime and old_info["size"] == file_size:
                        # File unchanged, skip
                        continue
                
                # File is new or modified
                modified_files.append(file_path)
                # Update state
                index_state[file_key] = {"mtime": mtime, "size": file_size}
                
            except Exception as e:
                logger.warning(f"Could not stat file {file_path}: {e}")
                continue
        
        # Identify deleted files (present in state but not on disk)
        for file_key in list(index_state.keys()):
            full_path = workspace_path / file_key
            if not full_path.exists():
                deleted_files.append(file_key)
                # Remove from state
                del index_state[file_key]
        
        logger.info(f"Found {len(modified_files)} modified/new files and {len(deleted_files)} deleted files")
        
        if not modified_files and not deleted_files:
            msg = "No changes detected. Index is up to date."
            logger.info(msg)
            return True, msg
        
        # Delete documents for removed files
        if deleted_files:
            logger.info(f"Removing {len(deleted_files)} deleted files from index...")
            deleted_ids = []
            for file_key in deleted_files:
                # Query for documents from this file
                try:
                    results = collection.get(
                        where={"file_path": {"$contains": file_key}}
                    )
                    if results and results.get("ids"):
                        deleted_ids.extend(results["ids"])
                except Exception as e:
                    logger.warning(f"Failed to query documents for deleted file {file_key}: {e}")
            
            if deleted_ids:
                collection.delete(ids=deleted_ids)
                logger.info(f"Removed {len(deleted_ids)} documents for deleted files")
        
        # Process modified/new files
        total_chunks = 0
        for file_path in modified_files:
            try:
                # Determine language from extension
                ext = file_path.suffix.lower()
                supported_extensions = {
                    '.py': 'python', '.js': 'javascript', '.jsx': 'javascript',
                    '.ts': 'typescript', '.tsx': 'typescript', '.java': 'java',
                    '.c': 'c', '.cpp': 'cpp', '.h': 'c', '.hpp': 'cpp',
                    '.go': 'go', '.rs': 'rust', '.rb': 'ruby', '.php': 'php',
                    '.swift': 'swift', '.kt': 'kotlin', '.scala': 'scala',
                    '.cs': 'csharp', '.fs': 'fsharp', '.vb': 'vb', '.sql': 'sql',
                    '.html': 'html', '.css': 'css', '.json': 'json',
                    '.yaml': 'yaml', '.yml': 'yaml', '.toml': 'toml', '.xml': 'xml',
                    '.sh': 'bash', '.bash': 'bash', '.md': 'markdown', '.txt': 'text'
                }
                
                if ext not in supported_extensions:
                    continue
                
                language = supported_extensions[ext]
                
                # First, remove existing chunks for this file (if any)
                file_key = str(file_path.relative_to(workspace_path))
                existing_results = collection.get(
                    where={"file_path": {"$contains": file_key}}
                )
                if existing_results and existing_results.get("ids"):
                    collection.delete(ids=existing_results["ids"])
                    logger.debug(f"Removed {len(existing_results['ids'])} existing chunks for {file_key}")
                
                # Parse and create new chunks
                file_chunks = parse_file_with_tree_sitter(file_path, language, config)
                if not file_chunks:
                    continue
                
                # Add new chunks to collection
                processed = embed_chunks_batched(
                    file_chunks, config, collection, workspace_hash, batch_size=16
                )
                total_chunks += processed
                logger.debug(f"Added {processed} chunks for {file_key}")
                
            except Exception as e:
                logger.warning(f"Failed to process file {file_path}: {e}")
                continue
        
        # Save updated state
        try:
            import json
            state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(state_file, 'w', encoding='utf-8') as f:
                json.dump(index_state, f, indent=2)
            logger.info(f"Saved index state to: {state_file}")
        except Exception as e:
            logger.warning(f"Failed to save index state: {e}")
        
        # Count total documents
        final_count = collection.count()
        msg = f"Incremental indexing completed. Added {total_chunks} chunks, removed {len(deleted_files)} files. Collection now contains {final_count} documents."
        logger.info(msg)
        
        return True, msg
        
    except Exception as e:
        msg = f"Incremental indexing failed: {e}"
        logger.exception(msg)
        return False, msg