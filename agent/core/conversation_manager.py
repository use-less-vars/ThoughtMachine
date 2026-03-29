"""
Conversation history management.

Extracted from agent.py to separate conversation management concerns.
"""

import os
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional


class ConversationManager:
    """Manages conversation history, message grouping, and pruning operations."""
    
    def __init__(self, session=None, context_builder=None, logger=None):
        """
        Initialize conversation manager.
        
        Args:
            session: Optional Session object.
            context_builder: Optional context builder for HistoryProvider integration.
            logger: Optional logger instance.
        """
        self.session = session
        self.context_builder = context_builder
        self.logger = logger
    
    def add_message(self, message: Dict[str, Any], conversation: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Add a message to the conversation history (session).
        
        Args:
            message: Message dictionary to add.
            conversation: Current conversation list.
            
        Returns:
            Updated conversation list.
        """
        if self.session is None:
            # Fallback for agent without session (should not happen in normal use)
            conversation.append(message)
            return conversation
        
        # Use HistoryProvider.add_message if available (ensures cache invalidation)
        if self.context_builder is not None and hasattr(self.context_builder, 'add_message'):
            self.context_builder.add_message(message)
        else:
            # Fallback: append directly
            self.session.user_history.append(message)
            self.session.updated_at = datetime.now()
        
        # Return the session's user_history
        return self.session.user_history
    
    def group_messages_into_turns(self, messages: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        """
        Group non-system messages into turns.
        
        Rules:
        - User messages always start a new turn
        - Assistant messages with tool_calls can also start a turn (after pruning)
        - All messages after a turn start belong to that turn until next user message
        - System messages should be filtered out before calling this method
        - Turns that don't start with user or assistant-with-tools are discarded
        
        This ensures tool call sequences stay together, even when they start with
        an assistant (due to pruning cutting off the user part of the turn).
        """
        logger = self.logger.py_logger if self.logger and hasattr(self.logger, 'py_logger') else logging.getLogger(__name__)
        turns = []
        current_turn = []
        
        # Debug logging
        debug = os.environ.get('DEBUG_TURN_GROUPING')
        if debug:
            logger.debug(f"[DEBUG_TURN_GROUPING] Grouping {len(messages)} messages")
            max_to_show = 10
            for i, msg in enumerate(messages[:max_to_show]):
                role = msg.get("role")
                content_preview = str(msg.get("content", ""))[:50]
                has_tool_calls = "tool_calls" in msg and msg["tool_calls"]
                logger.debug(f"  [{i}] {role}: {content_preview}... tool_calls={has_tool_calls}")
            if len(messages) > max_to_show:
                logger.debug(f"  ... and {len(messages) - max_to_show} more messages")
        
        for msg in messages:
            role = msg.get("role")
            
            # Skip system messages (should have been filtered out)
            if role == "system":
                continue
                
            if role == "user":
                # User always starts a new turn
                if current_turn:
                    turns.append(current_turn)
                current_turn = [msg]
            elif role == "assistant" and msg.get("tool_calls"):
                # Assistant with tool_calls can start a turn (after pruning)
                # However, if current turn already starts with a user, this assistant belongs to that turn
                if current_turn:
                    if current_turn[0].get("role") == "user":
                        # Continue current turn (user -> assistant with tools)
                        current_turn.append(msg)
                    else:
                        # Start new turn
                        turns.append(current_turn)
                        current_turn = [msg]
                else:
                    # Start new turn
                    current_turn = [msg]
            else:
                # Other messages (assistant without tools, tool) - add to current turn with validation
                if current_turn:
                    # For tool messages, validate they follow an assistant with tool_calls
                    if role == 'tool':
                        if current_turn and current_turn[-1].get('role') == 'assistant' and current_turn[-1].get('tool_calls'):
                            current_turn.append(msg)
                        else:
                            # Orphaned tool message - skip it
                            if debug:
                                tool_call_id = msg.get('tool_call_id', 'unknown')
                                logger.debug(f"[DEBUG_TURN_GROUPING] Discarding orphaned tool message: {tool_call_id}")
                            continue
                    else:
                        # assistant without tools - add to current turn
                        current_turn.append(msg)
                else:
                    # Orphaned message without user or assistant-with-tools
                    # Discard it
                    if debug:
                        logger.debug(f"[DEBUG_TURN_GROUPING] Discarding orphaned {role} message")
                    continue
        
        if current_turn:
            turns.append(current_turn)
        
        # Filter to keep only valid turns
        valid_turns = []
        for turn in turns:
            if not turn:
                continue
            first_msg = turn[0]
            first_role = first_msg.get("role")
            
            if first_role == "user":
                valid_turns.append(turn)
            elif first_role == "assistant" and first_msg.get("tool_calls"):
                # Turn starts with assistant that made tool calls
                # This is valid (e.g., after pruning cut off the user)
                valid_turns.append(turn)
            elif debug:
                logger.debug(f"[DEBUG_TURN_GROUPING] Discarding turn starting with {first_role}")
        
        if debug:
            logger.debug(f"[DEBUG_TURN_GROUPING] Returned {len(valid_turns)} valid turns")
            max_to_show = 10
            for i, turn in enumerate(valid_turns[:max_to_show]):
                logger.debug(f"  Turn {i}: {[msg.get('role') for msg in turn]}")
            if len(valid_turns) > max_to_show:
                logger.debug(f"  ... and {len(valid_turns) - max_to_show} more turns")
        
        return valid_turns