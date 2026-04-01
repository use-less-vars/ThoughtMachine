"""Utilities for session management."""

import json
from typing import List, Dict, Any


def normalize_conversation_for_hash(conversation: List[Dict[str, Any]]) -> str:
    """Create normalized JSON representation for consistent hashing.
    
    Strips transient fields and ensures consistent ordering for stable hashing.
    """
    normalized = []
    for msg in conversation:
        # Base required fields
        norm_msg = {'role': msg['role'], 'content': msg.get('content', '')}
        
        # Optional standard fields (OpenAI format)
        if 'name' in msg:
            norm_msg['name'] = msg['name']
        if 'tool_calls' in msg:
            # Normalize tool calls: sort by id, normalize arguments
            tool_calls = []
            for tc in sorted(msg['tool_calls'], key=lambda x: x.get('id', '')):
                norm_tc = {
                    'id': tc.get('id', ''),
                    'type': tc.get('type', 'function'),
                    'function': {
                        'name': tc['function']['name'],
                        'arguments': tc['function']['arguments']  # Already JSON string
                    }
                }
                tool_calls.append(norm_tc)
            norm_msg['tool_calls'] = tool_calls
        if 'tool_call_id' in msg:
            norm_msg['tool_call_id'] = msg['tool_call_id']
            
        normalized.append(norm_msg)
    
    return json.dumps(normalized, sort_keys=True, separators=(',', ':'))