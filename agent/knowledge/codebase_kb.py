"""
Local Codebase Knowledge Base.

Provides semantic search over a locally indexed codebase using ChromaDB.
"""

from __future__ import annotations

import logging
import os
import hashlib
from pathlib import Path
from typing import List, Dict, Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from agent.config.models import AgentConfig


from agent.knowledge.base import BaseKnowledgeBase
from agent.knowledge.dependencies import check_rag_dependencies

logger = logging.getLogger(__name__)


class LocalCodebaseKB(BaseKnowledgeBase):
    """
    Knowledge base for searching a locally indexed codebase.
    
    Uses ChromaDB vector store and sentence-transformers embeddings.
    The collection is lazily loaded on first search query.
    """
    
    def __init__(self, workspace_path: str, config: AgentConfig):
        """
        Initialize the codebase knowledge base.
        
        Args:
            workspace_path: Path to the workspace directory
            config: AgentConfig object (must have rag_vector_store_path and rag_embedding_model)
        """
        self.workspace_path = workspace_path
        self.config = config
        
        # Lazily loaded components
        self._collection = None
        self._embedding_model = None
        
        # Compute workspace hash (same as indexer)
        self._workspace_hash = self._compute_workspace_hash(workspace_path)
        
    def _compute_workspace_hash(self, workspace_path: str) -> str:
        """Compute deterministic hash for workspace (same as codebase_indexer)."""
        abs_path = os.path.abspath(workspace_path)
        normalized = os.path.normpath(abs_path)
        return hashlib.md5(normalized.encode()).hexdigest()[:12]
    
    def _ensure_dependencies(self) -> None:
        """
        Check if RAG dependencies are available.
        
        Raises:
            ImportError: If any required dependency is missing
        """
        success, error_msg = check_rag_dependencies()
        if not success:
            raise ImportError(f"RAG dependencies not available: {error_msg}")
    
    def _get_collection(self):
        """
        Lazy-load the ChromaDB collection for the workspace.
        
        Returns:
            ChromaDB collection object
            
        Raises:
            ImportError: If chromadb is not available
            RuntimeError: If collection does not exist (index not created)
        """
        if self._collection is not None:
            return self._collection
        
        # Check dependencies
        self._ensure_dependencies()
        
        # Import chromadb
        try:
            import chromadb
            from chromadb.config import Settings
        except ImportError:
            raise ImportError("chromadb is not available")
        
        # Determine vector store path (same logic as codebase_indexer)
        if self.config.rag_vector_store_path:
            vector_store_path = Path(self.config.rag_vector_store_path)
        else:
            # Default: .thoughtmachine/rag/ under workspace
            workspace_root = Path(self.config.workspace_path) if self.config.workspace_path else Path.cwd()
            vector_store_path = workspace_root / ".thoughtmachine" / "rag"
        
        # Ensure directory exists
        vector_store_path.mkdir(parents=True, exist_ok=True)
        
        # Create ChromaDB client
        client = chromadb.PersistentClient(
            path=str(vector_store_path),
            settings=Settings(anonymized_telemetry=False)
        )
        
        collection_name = f"codebase_{self._workspace_hash}"
        
        # Try to get collection
        try:
            collection = client.get_collection(collection_name)
            logger.debug(f"Loaded collection: {collection_name}")
        except Exception as e:
            # Collection doesn't exist or cannot be loaded
            raise RuntimeError(
                f"Codebase index not found for workspace '{self.workspace_path}'. "
                f"Run index_codebase() first."
            ) from e
        
        self._collection = collection
        return collection
    
    def _get_collection_dimension(self) -> int:
        """
        Get the embedding dimension of the collection.
        
        Returns:
            Embedding dimension (default 256 if cannot determine)
        """
        if hasattr(self, '_collection_dimension') and self._collection_dimension is not None:
            return self._collection_dimension
        
        try:
            collection = self._get_collection()
            # Try to get the count and peek at first embedding if possible
            count = collection.count()
            if count > 0:
                # Get first embedding by querying with a minimal query
                # Use the model to generate a test embedding
                try:
                    model = self._get_embedding_model()
                    test_embedding = model.encode("test", truncate_dim=384).tolist()
                    # Try query with this embedding to get stored embeddings
                    results = collection.query(
                        query_embeddings=[test_embedding],
                        n_results=1,
                        include=['embeddings']
                    )
                    embeddings = results.get('embeddings')
                    if embeddings and len(embeddings) > 0:
                        first_embedding = embeddings[0]
                        if first_embedding and len(first_embedding) > 0:
                            dimension = len(first_embedding[0]) if isinstance(first_embedding[0], list) else len(first_embedding)
                            self._collection_dimension = dimension
                            logger.debug(f"Detected collection dimension: {dimension}")
                            return dimension
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"Failed to get collection dimension: {e}")
        
        # Default to 256 (original truncation default from indexing)
        logger.debug("Using default collection dimension: 256")
        self._collection_dimension = 256
        return 256
    
    def _get_embedding_model(self):
        """
        Lazy-load the sentence-transformers model.
        
        Returns:
            SentenceTransformer model
            
        Raises:
            ImportError: If sentence-transformers is not available
        """
        if self._embedding_model is not None:
            return self._embedding_model
        
        # Check dependencies
        self._ensure_dependencies()
        
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise ImportError("sentence_transformers is not available")
        
        model_name = self.config.rag_embedding_model
        logger.debug(f"Loading embedding model: {model_name}")
        model = SentenceTransformer(model_name)
        
        self._embedding_model = model
        return model
    
    def is_indexed(self) -> bool:
        """
        Check if the workspace has been indexed.
        
        Returns:
            True if a ChromaDB collection exists for this workspace
        """
        try:
            # Check dependencies first
            success, _ = check_rag_dependencies()
            if not success:
                return False
            
            self._get_collection()
            return True
        except (ImportError, RuntimeError):
            return False
    
    def is_available(self) -> bool:
        """
        Check if the knowledge base is operational.
        
        Returns:
            True if dependencies are available AND workspace is indexed
        """
        return self.is_indexed()
    
    def search(self, query: str, top_k: int = 5, min_score: Optional[float] = None, where: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """
        Search the indexed codebase for relevant code snippets.

        Args:
            query: Natural language query
            top_k: Maximum number of results to return
            min_score: Minimum similarity score (0-1) to filter results. If None, all results returned.
            where: Optional filter dictionary for metadata fields (ChromaDB where clause).

        Returns:
            List of result dictionaries with keys:
                - content: str - The code snippet text
                - metadata: Dict[str, Any] - File path, line numbers, language
                - score: float - Similarity score (0-1, higher is more relevant)

        Returns empty list if:
            - RAG dependencies are missing
            - Collection does not exist (index not created)
        """
        try:
            self._ensure_dependencies()
        except ImportError as e:
            logger.warning(f"Cannot search codebase: {e}")
            return []
        
        try:
            collection = self._get_collection()
        except RuntimeError as e:
            logger.warning(f"Cannot search codebase: {e}")
            return []
        
        # Get embedding model
        try:
            model = self._get_embedding_model()
        except ImportError as e:
            logger.warning(f"Cannot search codebase: {e}")
            return []
        
        # Get collection dimension and truncate query embedding accordingly
        try:
            dimension = self._get_collection_dimension()
            logger.debug(f"Using embedding dimension: {dimension}")
            
            # Embed query with truncation to match collection dimension
            query_embedding = model.encode(query, truncate_dim=dimension).tolist()
        except Exception as e:
            logger.warning(f"Failed to get dimension or embed query: {e}")
            # Fallback: try without truncation
            query_embedding = model.encode(query).tolist()
        
        # Perform search
        try:
            import chromadb
            # Build query parameters
            query_params = {
                "query_embeddings": [query_embedding],
                "n_results": top_k,
                "include": ["metadatas", "documents", "distances"]
            }
            if where is not None:
                query_params["where"] = where
            
            results = collection.query(**query_params)
        except chromadb.errors.InvalidArgumentError as e:
            # Dimension mismatch - suggest re-indexing
            logger.error(
                f"Embedding dimension mismatch. The collection expects a different dimension. "
                f"Please re-index the codebase with: python -m agent.knowledge.codebase_indexer index {self.workspace_path} --force"
            )
            return []
        except Exception as e:
            logger.warning(f"Search failed: {e}")
            return []
        
        # Format results
        formatted_results = []
        
        # Extract lists from results
        documents = results.get("documents", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]
        
        # Ensure all lists have same length
        n_results = len(documents)
        if len(metadatas) != n_results:
            metadatas = [{}] * n_results
        if len(distances) != n_results:
            distances = [0.0] * n_results
        
        for i, (doc, metadata, distance) in enumerate(zip(documents, metadatas, distances)):
            # Convert distance to similarity score (ChromaDB returns L2 distance)
            # Higher distance = less similar, lower distance = more similar
            # Normalize to 0-1 range: score = 1 / (1 + distance)
            try:
                score = 1.0 / (1.0 + float(distance))
            except (TypeError, ValueError):
                score = 0.0
            
            formatted_results.append({
                "content": doc,
                "metadata": metadata,
                "score": score
            })
        
        # Filter by minimum score if specified
        if min_score is not None:
            filtered_results = []
            for result in formatted_results:
                if result["score"] >= min_score:
                    filtered_results.append(result)
            formatted_results = filtered_results
            
        logger.debug(f"Codebase search returned {len(formatted_results)} results (after min_score filter)")
        return formatted_results    
    def add(self, content: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """
        Add a document to the knowledge base.
        
        Not supported for codebase knowledge base (read-only).
        
        Raises:
            NotImplementedError: Always raised
        """
        raise NotImplementedError("LocalCodebaseKB is read-only; use index_codebase() to create index")