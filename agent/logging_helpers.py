"""
Logging helpers for debugging user_history, GUI, and LLM context.

Phase 0 of the debugging plan.
"""
import os
from typing import List, Dict, Any
from agent.logging import log

def dump_messages(messages: List[Dict[str, Any]], label: str, max_items: int = 50):
    """Log a compact representation of a message list."""
    if not messages:
        return
    
    sample = []
    truncate_limit = int(os.getenv("TM_DEBUG_TRUNCATE_LENGTH", 100))
    for i, m in enumerate(messages[:max_items]):
        role = m.get("role", "unknown")
        content = m.get("content", "")
        preview = content[:truncate_limit] + ("..." if len(content) > truncate_limit else "")
        content_preview = preview
        
        # Check for token warnings
        is_token_warning = "[SYSTEM NOTIFICATION]" in content and any(
            phrase in content.lower() 
            for phrase in ['token', 'turn', 'context window', 'limit', 'warning', 'critical', 'countdown']
        )
        
        sample.append({
            "idx": i,
            "role": role,
            "content_preview": content_preview,
            "has_tool_calls": "tool_calls" in m,
            "tool_call_id": m.get("tool_call_id"),
            "metadata_keys": list(m.keys() - {"role", "content", "tool_calls", "tool_call_id"}),
            "is_token_warning": is_token_warning,
            "is_summary": "Summary of previous conversation:" in content,
        })
    