"""
Debug context monitoring utilities.

Extracted from agent.py to separate debugging concerns.
"""

import os
import logging
from typing import List, Dict, Any, Optional


class DebugContext:
    """Debug helper for context monitoring."""
    
    def __init__(self, logger=None):
        """
        Initialize debug context.
        
        Args:
            logger: Optional logger instance.
        """
        self.logger = logger
    
    def debug_context(
        self, 
        stage: str, 
        messages: Optional[List[Dict[str, Any]]] = None, 
        context_builder = None, 
        usage = None,
        session = None,
        conversation = None
    ):
        """
        Debug helper for context monitoring.
        
        Shows:
        1. Full history (session.user_history if available) with metadata
        2. Runtime context being built (what gets sent to LLM)
        3. Token estimates vs actual usage from LLM response
        4. Relationship between full history and runtime context
        """
        if not os.environ.get('DEBUG_CONTEXT'):
            return
        
        logger = self.logger.py_logger if self.logger and hasattr(self.logger, 'py_logger') else logging.getLogger(__name__)
        
        logger.debug(f"\n=== DEBUG_CONTEXT: {stage} ===")
        
        # Show full history if session exists
        if session is not None:
            full_history = session.user_history
            logger.debug(f"Full session history: {len(full_history)} messages")
            for i, msg in enumerate(full_history[:10]):
                role = msg.get('role', 'NO_ROLE')
                content_preview = str(msg.get('content', ''))[:60].replace('\n', ' ')
                metadata = {k: v for k, v in msg.items() if k not in ['role', 'content', 'tool_calls', 'tool_call_id', 'name', 'reasoning_content']}
                meta_str = f" {metadata}" if metadata else ""
                if role == 'tool':
                    tool_call_id = msg.get('tool_call_id', 'NO_ID')
                    logger.debug(f"  [{i}] {role}: tool_call_id={tool_call_id}, content={content_preview}...{meta_str}")
                elif role == 'assistant':
                    tool_calls = msg.get('tool_calls')
                    if tool_calls:
                        logger.debug(f"  [{i}] {role}: HAS TOOL_CALLS {len(tool_calls)}, content={content_preview}...{meta_str}")
                    else:
                        logger.debug(f"  [{i}] {role}: {content_preview}...{meta_str}")
                else:
                    logger.debug(f"  [{i}] {role}: {content_preview}...{meta_str}")
        
            if len(full_history) > 10: 
                logger.debug(f"  ... {len(full_history) - 10} more messages")
        
        # Show current conversation (what agent sees)
        if conversation is not None:
            logger.debug(f"\nAgent conversation: {len(conversation)} messages")
            for i, msg in enumerate(conversation[:10]):
                role = msg.get('role', 'NO_ROLE')
                content_preview = str(msg.get('content', ''))[:60].replace('\n', ' ')
                if role == 'tool':
                    tool_call_id = msg.get('tool_call_id', 'NO_ID')
                    logger.debug(f"  [{i}] {role}: tool_call_id={tool_call_id}, content={content_preview}...")
                elif role == 'assistant':
                    tool_calls = msg.get('tool_calls')
                    if tool_calls:
                        logger.debug(f"  [{i}] {role}: HAS TOOL_CALLS {len(tool_calls)}, content={content_preview}...")
                    else:
                        logger.debug(f"  [{i}] {role}: {content_preview}...")
                else:
                    logger.debug(f"  [{i}] {role}: {content_preview}...")
            
            if len(conversation) > 10: 
                logger.debug(f"  ... {len(conversation) - 10} more messages")
        
        # Show runtime context if provided
        if messages is not None:
            logger.debug(f"\nRuntime context (sent to LLM): {len(messages)} messages")
            # Note: token estimation would require token_counter dependency
            for i, msg in enumerate(messages[:10]):
                role = msg.get('role', 'NO_ROLE')
                content_preview = str(msg.get('content', ''))[:60].replace('\n', ' ')
                if role == 'tool':
                    tool_call_id = msg.get('tool_call_id', 'NO_ID')
                    logger.debug(f"  [{i}] {role}: tool_call_id={tool_call_id}, content={content_preview}...")
                elif role == 'assistant':
                    tool_calls = msg.get('tool_calls')
                    if tool_calls:
                        logger.debug(f"  [{i}] {role}: HAS TOOL_CALLS {len(tool_calls)}, content={content_preview}...")
                    else:
                        logger.debug(f"  [{i}] {role}: {content_preview}...")
                else:
                    logger.debug(f"  [{i}] {role}: {content_preview}...")
            
            if len(messages) > 10: 
                logger.debug(f"  ... {len(messages) - 10} more messages")
        
        # Show token usage comparison if available
        if usage is not None:
            logger.debug(f"\nToken usage from LLM:")
            logger.debug(f"  Input tokens: {usage.get('prompt_tokens', 'N/A')}")
            logger.debug(f"  Output tokens: {usage.get('completion_tokens', 'N/A')}")
            logger.debug(f"  Total tokens: {usage.get('total_tokens', 'N/A')}")