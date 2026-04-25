# ThoughtMachine RAG System – Technical Report

## 1. Overview
The system provides **codebase memory** for the agent via Retrieval-Augmented Generation (RAG). It indexes project source files into a vector database (ChromaDB) and exposes a natural-language search tool (`SearchCodebaseTool`). The goal is to enable semantic discovery of code structure, implementations, and patterns without relying on exact symbol names.

## 2. Architecture

```
Source Files
    │
    ▼
[AST Chunker] ──▶ chunks (text + metadata)
    │
    ▼
[Embedding Model] ──▶ embeddings (truncated vectors)
    │
    ▼
[ChromaDB Collection] ──▶ vector store (per workspace)
    │
    ▼
[SearchCodebaseTool] ──▶ query → embedding → similarity search → formatted results
```

- **Embedding model**: `BAAI/bge-small-en-v1.5` (33M parameters) from Hugging Face, loaded via `sentence-transformers`.
- **Vector store**: ChromaDB (persistent, on-disk at `~/.thoughtmachine/rag/`).
- **Chunking**: AST-aware splitting using `tree-sitter` for supported languages, fallback to line/paragraph splitting.
- **Indexing**: Batch processing with configurable batch size and embedding truncation.
- **Search**: Cosine similarity with score thresholding; supports intents (`exact`, `broad`, `file`) and path filtering.

## 3. Key Files & Modules

| File | Role |
|------|------|
| `agent/knowledge/codebase_indexer.py` | Core indexing logic (scanning, chunking, embedding, storing) + CLI commands |
| `agent/knowledge/embedder.py` | `embed_chunks_batched()` – loads model, generates embeddings, truncates dimensions |
| `agent/knowledge/chunker.py` | AST-based chunker using tree-sitter |
| `agent/config/models.py` | `AgentConfig` Pydantic model with RAG fields |
| `agent/cli/rag_commands.py` | CLI entry points: `index-codebase`, `update-index` |
| `tools/search_codebase.py` | `SearchCodebaseTool` definition, parameter schema, and execution |
| `agent_config.json` | Runtime config file (user-editable defaults) |
| `system_prompt.txt` | Contains rule 8 promoting use of `SearchCodebaseTool` |
| `.index_state.json` | (in ChromaDB directory) tracks file hashes/timestamps for incremental updates |

## 4. Configuration (AgentConfig)

All RAG parameters live in `AgentConfig` (`agent/config/models.py`).  
They are exposed in `agent_config.json` with defaults:

```json
{
  "rag_enabled": true,
  "rag_embedding_model": "BAAI/bge-small-en-v1.5",
  "rag_vector_store_path": null,        // defaults to ~/.thoughtmachine/rag/
  "rag_chunk_size": 1500,
  "rag_chunk_overlap": 200,
  "rag_batch_size": 16,
  "rag_truncate_dim": 256
}
```

**Important**: There is a duplicate configuration schema in `ConfigService._SCHEMA` (a manual dict). When adding new fields, both locations must be updated until the duplication is resolved (see §8).

## 5. Indexing Workflow

### Full Index (`index-codebase`)
1. **Scan**: Walk workspace directory respecting `.gitignore` rules.
2. **File filtering**: Only text files, size limit (default 1 MB), skip binaries.
3. **Chunking**: For each file, AST-based splitter yields chunks of ~`rag_chunk_size` characters with `rag_chunk_overlap` overlap.
4. **Metadata**: Each chunk stores `file_path`, `start_line`, `end_line`, `language`, `chunk_type`.
5. **Embedding**: Chunks batched (`rag_batch_size`) → model encodes to 384-d vectors → truncated to `rag_truncate_dim` dimensions (default 256).
6. **Storage**: Added to ChromaDB collection named `codebase_{workspace_hash}`.

### Incremental Update (`update-index`)
1. Load `.index_state.json` from vector store directory (maps file path → (hash, mtime)).
2. Compare current filesystem state vs state.
3. **Added/Modified files**: Re-chunk and re-embed; old chunks for that file are deleted first.
4. **Deleted files**: Remove all chunks belonging to the file from ChromaDB.
5. Save updated `.index_state.json`.

**State tracking format** (`~/.thoughtmachine/rag/.index_state.json`):
```json
{
  "workspace": "/path/to/project",
  "files": {
    "relative/file.py": {"hash": "<sha256>", "mtime": 1234567890.0}
  }
}
```

## 6. Search Tool Details

**Class**: `SearchCodebaseTool` (in `tools/search_codebase.py`)

### Parameters
- `query` (str): Natural language query.
- `top_k` (int, default=5): Number of results.
- `intent` (str, default="broad"):
  - `"exact"`: min similarity threshold 0.5 (high precision)
  - `"broad"`: min threshold 0.3 (more results)
  - `"file"`: groups results by file, returns one chunk per file with combined score.
- `restrict_to_path` (str, optional): Only return results from files under this relative path (e.g., `"qt_gui/"`).

### Execution Flow
1. Load embedding model (cached after first load).
2. Embed query with same model and truncation.
3. Query ChromaDB collection with optional metadata filter (`file_path` starts with `restrict_to_path` if provided).
4. Filter by score threshold based on intent.
5. Format results as Markdown with file paths, line numbers, scores, and code snippets.

### Integration
- Registered in `SIMPLIFIED_TOOL_CLASSES` and `TOOL_CLASSES` (both list the tool).
- GUI checkbox visible only when `rag_enabled` is `true`.
- System prompt rule 8 instructs the agent to use this tool for semantic code queries.

## 7. Workspace Isolation

Each indexed workspace has a separate ChromaDB collection identified by a hash of the absolute workspace path:

```python
workspace_hash = hashlib.sha256(workspace_path.encode()).hexdigest()[:16]
collection_name = f"codebase_{workspace_hash}"
```

- All collections stored in the same persistence directory (`rag_vector_store_path`).
- Switching projects: run `index-codebase --workspace /new/path`, then launch agent from that directory (or set workspace accordingly).

## 8. Known Issues & Limitations

- **Config duplication**: `ConfigService._SCHEMA` must be manually synced with `AgentConfig`; missing fields can cause validation errors.
- **Path filter edge case**: Earlier reports of `restrict_to_path` returning no results were traced to query phrasing or high score thresholds; the filter itself works correctly when tested with diagnostic scripts.
- **Embedding dimension warning**: `sentence-transformers` logs an `UNEXPECTED` key warning for `position_ids` – harmless, can be ignored.
- **No automatic re-indexing**: The agent does not detect stale indexes; user must run `update-index` manually. Staleness detection is a planned enhancement.
- **No multi‑project UI**: Workspace switching requires restarting the GUI from a different directory or using `--workspace`. A GUI selector is planned.
- **Model caching**: The embedding model is downloaded on first use and cached locally; first run may be slow.

## 9. Future Enhancements (Planned)

1. **Session Notebook Memory** – A second ChromaDB collection (`session_memory`) where the agent can store its own insights via `RememberTool` and retrieve them via `RecallTool`. This would work across projects.
2. **Staleness detection** – On agent startup, check if codebase has changed since last index and prompt user.
3. **GUI workspace switcher** – Allow changing active project without restart.
4. **Unify config validation** – Eliminate `_SCHEMA` dict and use Pydantic model directly for all configuration loading/saving.

## 10. Debugging & Maintenance Commands

- Rebuild full index:
  ```bash
  python -m agent.cli.rag_commands index-codebase --force
  ```
- Incremental update:
  ```bash
  python -m agent.cli.rag_commands update-index
  ```
- View ChromaDB collections:
  ```python
  import chromadb
  client = chromadb.PersistentClient(path="~/.thoughtmachine/rag")
  print(client.list_collections())
  ```
- Check index state:
  ```bash
  cat ~/.thoughtmachine/rag/.index_state.json
  ```

## 11. Summary

The RAG system is **production-ready for single-workspace use**. It provides semantic code search with configurable chunking/embedding parameters. The tool is advertised in the agent’s prompt but is inherently limited for exact‑symbol lookups where `FileSearchTool` is more efficient. Its strength lies in conceptual exploration. The next major step is adding **session memory** to create a genuinely learning agent.