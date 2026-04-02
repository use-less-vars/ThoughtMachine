"""
Tool execution and dispatch logic.

Extracted from agent.py to separate tool execution concerns.
"""

import json
from typing import List, Dict, Any, Optional, Tuple
from pydantic import ValidationError

from fast_json_repair import loads as repair_loads
from tools.final import Final
from tools.final_report import FinalReport
from agent.core.turn_transaction import TurnTransaction
from tools.request_user_interaction import RequestUserInteraction
from tools.summarize_tool import SummarizeTool

# Import our clean debug logging for pruning/history flow
try:
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/../../")
    from debug_pruning import (
        debug_log, log_session_history, log_history_provider_reconstruction,
        log_message_insertion, log_pruning_operation, log_token_count,
        log_summary_operation, truncate_message
    )
    DEBUG_PRUNING_AVAILABLE = True
except ImportError:
    DEBUG_PRUNING_AVAILABLE = False
    debug_log = lambda *args, **kwargs: None
    log_session_history = lambda *args, **kwargs: None
    log_history_provider_reconstruction = lambda *args, **kwargs: None
    log_message_insertion = lambda *args, **kwargs: None
    log_pruning_operation = lambda *args, **kwargs: None
    log_token_count = lambda *args, **kwargs: None
    log_summary_operation = lambda *args, **kwargs: None


class ToolExecutor:
    """Handles tool execution, JSON repair, and tool result processing."""
    
    def __init__(self, tool_classes, config, state, logger=None, security_available=False):
        """
        Initialize tool executor.
        
        Args:
            tool_classes: List of tool classes available.
            config: AgentConfig instance.
            state: AgentState instance for tool allowance checking.
            logger: Optional logger instance.
            security_available: Whether security module is available.
        """
        self.tool_classes = tool_classes
        self.config = config
        self.state = state
        self.logger = logger
        self.security_available = security_available
        
        if security_available:
            from thoughtmachine.security import CapabilityRegistry
            self.CapabilityRegistry = CapabilityRegistry
        else:
            self.CapabilityRegistry = None
    
    def execute_tool_calls(
        self, 
        tool_calls: List[Dict[str, Any]], 
        add_to_conversation_func,
        update_token_func,
        agent_id: int,
        turn_transaction: Optional[TurnTransaction] = None
    ) -> Tuple[List[Dict[str, Any]], bool, bool, Optional[str], Optional[int]]:
        """
        Execute multiple tool calls from an assistant message.
        
        Args:
            tool_calls: List of tool call dictionaries from LLM.
            add_to_conversation_func: Function to add messages to conversation.
            update_token_func: Function to update token count and yield events.
            agent_id: ID of the agent for security checks.
            turn_transaction: Optional TurnTransaction to buffer messages (if None, use add_to_conversation_func).
            
        Returns:
            Tuple of:
            - executed_tools: List of executed tool information
            - final_detected: Whether a Final tool was executed
            - final_content: Content from Final/FinalReport tool if final_detected is True, otherwise None
            - user_interaction_requested: Whether RequestUserInteraction was called
            - summary_text: Summary text if SummarizeTool was called
            - summary_keep_recent_turns: Number of turns to keep for summarization
        """
        executed_tools = []
        final_detected = False
        final_content = None
        user_interaction_requested = False
        user_interaction_message = None
        summary_requested = False
        summary_text = None
        summary_keep_recent_turns = 0

        # Function to add tool result message (buffered or immediate)
        def add_tool_result(message):
            if turn_transaction is not None:
                turn_transaction.add_tool_result(message)
            else:
                add_to_conversation_func(message)
        for tool_call in tool_calls:
            tool_name = tool_call["function"]["name"]
            
            # Check if tool is allowed in current state
            if not self.state.is_tool_allowed(tool_name):
                tool_result = self._create_tool_rejection_message(tool_name)
                
                # Append tool result with error
                add_tool_result({
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "content": tool_result
                })
                # Estimate tokens for tool result
                tool_tokens = len(str(tool_result)) // 4
                update_token_func(tool_tokens)
                
                executed_tools.append({
                    "name": tool_name,
                    "arguments": {},
                    "result": tool_result
                })
                continue
            
            arguments_str = tool_call["function"]["arguments"]
            
            try:
                arguments = json.loads(arguments_str)
            except json.JSONDecodeError:
                try:
                    arguments = repair_loads(arguments_str)
                    if self.logger:
                        self.logger.py_logger.info(f"JSON repaired for {tool_name}")
                except Exception as e:
                    tool_result = f"Invalid JSON in arguments: {e}. Raw: {arguments_str}"
                    if self.logger:
                        self.logger.log_error("JSON_DECODE_ERROR", f"Failed to parse JSON for {tool_name}: {e}")
                    add_tool_result({
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": tool_result
                    })
                    executed_tools.append({
                        "name": tool_name,
                        "arguments": {"error": "Invalid JSON", "raw": arguments_str},
                        "result": tool_result
                    })
                    continue
            
            # Log tool call
            if self.logger:
                self.logger.log_tool_call(tool_name, arguments, tool_call["id"])
            
            # Find matching tool class
            tool_class = next((cls for cls in self.tool_classes if cls.__name__ == tool_name), None)
            if not tool_class:
                error_msg = f"Unknown tool: {tool_name}"
                tool_result = error_msg
            else:
                tool_execution_result = self._execute_single_tool(
                    tool_class, 
                    arguments, 
                    tool_name, 
                    agent_id,
                    lambda: final_detected,
                    lambda: final_content,
                    lambda: user_interaction_requested,
                    lambda: user_interaction_message,
                    lambda: summary_requested,
                    lambda: summary_text,
                    lambda: summary_keep_recent_turns
                )
                tool_result = tool_execution_result['result']
                tool_type = tool_execution_result.get('tool_type', 'normal')
                
                # Update flags based on tool type
                if tool_type == 'final':
                    final_detected = True
                    final_content = tool_execution_result.get('final_content')
                elif tool_type == 'user_interaction':
                    user_interaction_requested = True
                    user_interaction_message = tool_execution_result.get('user_interaction_message')
                elif tool_type == 'summary':
                    summary_requested = True
                    summary_text = tool_execution_result.get('summary_text')
                    summary_keep_recent_turns = tool_execution_result.get('summary_keep_recent_turns')
                    if DEBUG_PRUNING_AVAILABLE:
                        log_summary_operation(f"SummarizeTool executed: summary length={len(summary_text)}, keep_recent_turns={summary_keep_recent_turns}")
            
            # Log tool result
            if self.logger:
                self.logger.log_tool_result(tool_name, tool_result, tool_call["id"])
            
            # Append tool result
            add_tool_result({
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "content": tool_result
            })
            # Estimate tokens for tool result
            tool_tokens = len(str(tool_result)) // 4
            update_token_func(tool_tokens)
            
            if self.logger:
                # Note: Would need conversation reference
                pass
            
            executed_tools.append({
                "name": tool_name,
                "arguments": arguments,
                "result": tool_result
            })
        
        return (
            executed_tools,
            final_detected,
            final_content,
            user_interaction_requested,
            summary_text if summary_requested else None,
            summary_keep_recent_turns if summary_requested else None
        )
    
    def _execute_single_tool(
        self,
        tool_class,
        arguments: Dict[str, Any],
        tool_name: str,
        agent_id: int,
        get_final_detected,
        get_final_content,
        get_user_interaction_requested,
        get_user_interaction_message,
        get_summary_requested,
        get_summary_text,
        get_summary_keep_recent_turns
    ) -> Dict[str, Any]:
        """
        Execute a single tool instance.
        
        Returns:
            Dictionary with keys:
            - result: tool result string
            - tool_type: 'normal', 'final', 'user_interaction', or 'summary'
            - summary_text: if tool_type == 'summary'
            - summary_keep_recent_turns: if tool_type == 'summary'
            - final_content: if tool_type == 'final'
        """
        try:
            # Add workspace_path from config if tool supports it
            tool_args = arguments.copy()
            if self.config.workspace_path is not None:
                tool_args['workspace_path'] = self.config.workspace_path
            if self.config.tool_output_token_limit is not None:
                tool_args['token_limit'] = self.config.tool_output_token_limit
            
            # Security capability check
            if self.security_available and self.CapabilityRegistry:
                try:
                    self.CapabilityRegistry.check(agent_id, tool_name, **tool_args)
                except Exception as e:
                    tool_result = f"Security check failed: {e}"
                    raise
            
            tool_instance = tool_class(**tool_args)
            tool_result = tool_instance.execute()
            
            # Check for special tool types
            if isinstance(tool_instance, Final) or isinstance(tool_instance, FinalReport):
                return {
                    'result': tool_result,
                    'tool_type': 'final',
                    'final_content': tool_result
                }
            elif isinstance(tool_instance, RequestUserInteraction):
                return {
                    'result': tool_result,
                    'tool_type': 'user_interaction',
                    'user_interaction_message': tool_result
                }
            elif isinstance(tool_instance, SummarizeTool):
                return {
                    'result': tool_result,
                    'tool_type': 'summary',
                    'summary_text': tool_instance.summary,
                    'summary_keep_recent_turns': tool_instance.keep_recent_turns
                }
            else:
                return {
                    'result': tool_result,
                    'tool_type': 'normal'
                }
            
        except ValidationError as e:
            return {
                'result': f"Invalid arguments: {e}",
                'tool_type': 'normal'
            }
        except Exception as e:
            return {
                'result': f"Error executing tool: {e}",
                'tool_type': 'normal'
            }
    
    def _create_tool_rejection_message(self, tool_name: str) -> str:
        """Create rejection message for disallowed tool calls."""
        allowed_tools = self.state.get_allowed_tools()
        if allowed_tools:
            return f'''❌ TOOL CALL REJECTED ❌

You attempted to use '{tool_name}', which is currently FORBIDDEN.

Current state: CRITICAL token countdown expired (restrictions active)
REQUIRED ACTION: SummarizeTool
Why: Token limit exceeded - conversation must be pruned before continuing.

You may call:
- SummarizeTool (to prune and continue)
- Final (to end conversation)
- FinalReport (to end with report)

Call SummarizeTool NOW to proceed.'''
        else:
            return f'''❌ TOOL CALL REJECTED ❌

You attempted to use '{tool_name}', which is currently FORBIDDEN.

Current state: token_state={self.state.token_state.value}, turn_state={self.state.turn_state.value}
Possible reasons: Token or turn limits exceeded with active restrictions.

Check system warnings for required actions.'''