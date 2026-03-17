# agent.py - Modular agent core
from __future__ import annotations
import json
import os
import queue
from typing import Optional, List, Dict, Any, TYPE_CHECKING

from pydantic import ValidationError
from llm_providers.factory import ProviderFactory
from llm_providers.exceptions import ProviderError
from tools import SIMPLIFIED_TOOL_CLASSES
from tools.utils import model_to_openai_tool
from tools.final import Final
from tools.request_user_interaction import RequestUserInteraction
from tools.summarize_tool import SummarizeTool
from fast_json_repair import loads as repair_loads
import tiktoken
import traceback

# Import logging module
try:
    from agent_logging import create_logger
    LOGGING_AVAILABLE = True
except ImportError:
    LOGGING_AVAILABLE = False
    create_logger = None

if TYPE_CHECKING:
    from agent_core import AgentConfig
# prune_conversation_history imported inside process_query method
from agent_state import AgentState, TokenState, TurnState, ExecutionState, SessionState

class Agent:
    def __init__(self, config: AgentConfig, initial_conversation=None):
        self.config = config
        
        # Create LLM provider using factory
        provider_config = {
            "api_key": config.api_key,
            "base_url": config.base_url,
            "model": config.model,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            **config.provider_config  # Merge any provider-specific config
        }
        self.provider = ProviderFactory.create_provider(
            config.provider_type,
            **provider_config
        )
        self.logger = None
        if LOGGING_AVAILABLE and config.enable_logging:
            self.logger = create_logger(config)
        # Prepare tool definitions
        self.tool_classes = config.tool_classes if config.tool_classes is not None else SIMPLIFIED_TOOL_CLASSES
        self.tool_definitions = [model_to_openai_tool(cls) for cls in self.tool_classes]
        
        # Initialize conversation
        self.conversation = []
        if initial_conversation is not None:
            self.conversation = initial_conversation.copy()
        else:
            self._ensure_system_prompt()
        
        # Token totals
        self.total_input_tokens = config.initial_input_tokens
        self.total_output_tokens = config.initial_output_tokens
        
        # Stop check
        self.stop_check = config.stop_check
        # Keep-alive queue and flags
        self._next_query_queue = queue.Queue()
        self._paused = False
        self._should_reset = False
        # State management
        self.state = AgentState(self.config, self.logger)
        
        # Set session state based on initial_conversation
        if initial_conversation is not None and len(initial_conversation) > 0:
            # Continuing an existing session
            events = self.state.set_session_state(SessionState.CONTINUING)
            for event in events:
                self._handle_state_event(event)
        else:
            # New session (already defaults to NEW in AgentState)
            pass
        
        self._token_encoder = None
        
    def _estimate_tokens(self, text_or_message):
        """Estimate token count for a string or message dict using tiktoken."""
        if self._token_encoder is None:
            # Default to cl100k_base (used by gpt-4, gpt-3.5-turbo)
            try:
                self._token_encoder = tiktoken.get_encoding("cl100k_base")
            except Exception:
                # Fallback to approximate estimation
                self._token_encoder = None
                # Use len//4 as fallback
                if isinstance(text_or_message, dict):
                    return len(str(text_or_message)) // 4
                else:
                    return len(str(text_or_message)) // 4
        
        if isinstance(text_or_message, dict):
            # Convert dict to JSON string for tokenization
            text = str(text_or_message)
        else:
            text = str(text_or_message)
        
        tokens = self._token_encoder.encode(text)
        return len(tokens)

    def _load_system_prompt(self):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        possible_paths = [
            os.path.join(script_dir, "system_prompt.txt"),
            os.path.join(script_dir, "..", "system_prompt.txt"),
            "./system_prompt.txt"
        ]
        system_prompt = None
        for path in possible_paths:
            try:
                with open(path, "r") as f:
                    system_prompt = f.read()
                    break
            except FileNotFoundError:
                continue
        if system_prompt is None:
            raise RuntimeError("Could not find system_prompt.txt in any known location")
        return system_prompt
    
    def _ensure_system_prompt(self):
        if not any(msg.get("role") == "system" for msg in self.conversation):
            system_prompt = self._load_system_prompt()
            self.conversation.insert(0, {"role": "system", "content": system_prompt})

    
    def reset(self):
        self.conversation = []
        self._ensure_system_prompt()
        self.total_input_tokens = self.config.initial_input_tokens
        self.total_output_tokens = self.config.initial_output_tokens
        # Reset state machine
        reset_events = self.state.reset()
        # Process any events from reset (though usually none for fresh reset)
        for event in reset_events:
            self._handle_state_event(event)
    def _handle_state_event(self, event):
        """Process a state event (e.g., token warning, turn warning).
        
        Events are dictionaries with 'type' field.
        For warning events, inject warning message into conversation.
        """
        if event.get("type") == "token_warning":
            # Note: token_warning events from AgentState have "message" field
            warning = event.get("message", event.get("warning", ""))
            sender = event.get("sender", "system")
            # Append warning as user message
            warning_msg = {"role": sender, "content": warning}
            self.conversation.append(warning_msg)
            # Estimate tokens for warning message and update state
            warning_tokens = self._estimate_tokens(warning_msg)
            self.state.current_conversation_tokens += warning_tokens
        elif event.get("type") == "turn_warning":
            # Note: turn_warning events from AgentState have "message" field
            warning = event.get("message", event.get("warning", ""))
            sender = event.get("sender", "system")
            warning_msg = {"role": sender, "content": warning}
            self.conversation.append(warning_msg)
            warning_tokens = self._estimate_tokens(warning_msg)
            self.state.current_conversation_tokens += warning_tokens
        elif event.get("type") == "execution_state_change":
            # Log execution state changes
            old_state = event.get("old_state")
            new_state = event.get("new_state")
            if self.logger:
                self.logger.py_logger.debug(
                    f"Execution state change: {old_state} -> {new_state}"
                )
        elif event.get("type") == "session_state_change":
            # Log session state changes
            old_state = event.get("old_state")
            new_state = event.get("new_state")
            if self.logger:
                self.logger.py_logger.debug(
                    f"Session state change: {old_state} -> {new_state}"
                )
        elif event.get("type") == "state_change":
            # Just log state changes for now
            if self.logger:
                self.logger.py_logger.debug(
                    f"State change: {event.get('old_state')} -> {event.get('new_state')}"
                )
    def submit_next_query(self, query: str):
        """Submit next query to paused agent."""
        self._next_query_queue.put(query)

    def request_reset(self):
        """Signal agent to reset on next iteration."""
        self._should_reset = True
        self._next_query_queue.put("[RESET]")

    def _wait_for_next_query(self):
        """Wait for next query to be submitted."""
        while self._paused:
            try:
                next_query = self._next_query_queue.get(timeout=0.1)
                if next_query == "[RESET]":
                    self.reset()
                    continue
                self._paused = False
                return next_query
            except queue.Empty:
                if self._should_reset:
                    self.reset()
                    self._should_reset = False
                    continue
    
    def _apply_summary_pruning(self, summary: str, keep_recent_turns: int):
        """Replace older conversation turns with a summary, keeping the most recent turns."""
        original_len = len(self.conversation)
        # Separate system messages and other messages
        system_messages = []
        other_messages = []
        for msg in self.conversation:
            if msg.get("role") == "system":
                system_messages.append(msg)
            else:
                other_messages.append(msg)

        if not other_messages:
            return

        # Group messages into turns: 
        # - User messages always start a new turn
        # - Assistant messages start a new turn only if current turn already has an assistant
        # - Tool messages stay with their preceding assistant
        turns = []
        current_turn = []
        current_has_assistant = False
        
        for msg in other_messages:
            role = msg.get("role")
            
            if role == "user":
                # User always starts a new turn
                if current_turn:
                    turns.append(current_turn)
                current_turn = [msg]
                current_has_assistant = False
            elif role == "assistant":
                if current_has_assistant:
                    # Current turn already has an assistant, start new turn
                    if current_turn:
                        turns.append(current_turn)
                    current_turn = [msg]
                    current_has_assistant = True
                else:
                    # No assistant in current turn yet, add to current turn
                    if not current_turn:
                        # Start new turn if empty
                        current_turn = [msg]
                    else:
                        current_turn.append(msg)
                    current_has_assistant = True
            else:
                # Tool messages - add to current turn
                if not current_turn:
                    # Should not happen, but handle gracefully
                    current_turn = [msg]
                else:
                    current_turn.append(msg)
        
        if current_turn:
            turns.append(current_turn)

        # Determine how many turns to keep from the end
        if keep_recent_turns <= 0:
            kept_turns = []
        else:
            kept_turns = turns[-keep_recent_turns:] if keep_recent_turns <= len(turns) else turns

        # Flatten kept turns
        pruned_other = []
        for turn in kept_turns:
            pruned_other.extend(turn)

        # Create summary system message
        MAX_SUMMARY_LENGTH = 2000
        if len(summary) > MAX_SUMMARY_LENGTH:
            summary = summary[:MAX_SUMMARY_LENGTH] + "... (truncated)"
        summary_msg = {"role": "system", "content": f"Summary of previous conversation: {summary}"}

        # Clean up old system messages: keep only:
        # 1. Original system prompt (first system message if it looks like a prompt)
        # 2. Our new summary message
        # Discard all other system messages (old warnings, old summaries)
        cleaned_system_messages = []
        if system_messages:
            # Keep first system message (assumed to be main system prompt)
            cleaned_system_messages.append(system_messages[0])
        
        # Combine: cleaned system messages, summary message, then kept turns
        new_conversation = cleaned_system_messages + [summary_msg] + pruned_other
        self.conversation = new_conversation
        new_len = len(self.conversation)
        if self.logger:
            self.logger.log_conversation_prune(original_len, new_len, "summary_pruning")
        
        # Debug logging
        if self.logger and hasattr(self.logger, 'py_logger'):
            self.logger.py_logger.info(
                f"[PRUNING] Applied summary pruning: kept {len(kept_turns)} turns, "
                f"removed {len(system_messages) - len(cleaned_system_messages)} old system messages, "
                f"conversation length: {len(self.conversation)} messages"
            )
        
        # Update current conversation tokens estimate after pruning
        # We need to estimate because we won't get accurate token count until next API call
        old_token_count = self.state.current_conversation_tokens
        estimated_tokens = 0
        for msg in self.conversation:
            estimated_tokens += self._estimate_tokens(msg)
        self.state.current_conversation_tokens = estimated_tokens
        if self.logger and hasattr(self.logger, 'py_logger'):
            self.logger.py_logger.info(
                f"[PRUNING] Updated token estimate: {estimated_tokens} tokens (was {old_token_count})"
            )
    
    def _format_tokens(self, tokens):
        """Format token count in thousands with 'k' suffix."""
        if tokens >= 1000:
            return f"{tokens // 1000}k"
        return str(tokens)
    

    def process_query(self, query):
        """Process a user query, appending it to conversation and running the agent.
        Yields events as dicts."""
        # Ensure system prompt present
        self._ensure_system_prompt()
        
        # Update execution state: transition to RUNNING
        current_exec_state = self.state.execution_state
        if current_exec_state == ExecutionState.RUNNING:
            # This shouldn't happen, but handle gracefully
            if self.logger:
                self.logger.log_error("EXECUTION_STATE", "process_query called while already RUNNING")
        elif current_exec_state in (ExecutionState.PAUSED, ExecutionState.WAITING_FOR_USER):
            # Resuming from pause or user interaction
            events = self.state.set_execution_state(ExecutionState.RUNNING)
            for event in events:
                self._handle_state_event(event)
        else:
            # Starting from IDLE, STOPPED, etc.
            events = self.state.set_execution_state(ExecutionState.RUNNING)
            for event in events:
                self._handle_state_event(event)
        
        # Log agent start if logger exists
        if self.logger:
            config_data = {
                "model": self.config.model,
                "temperature": self.config.temperature,
                "max_turns": self.config.max_turns,
                "max_history_turns": self.config.max_history_turns,
                "max_tokens": self.config.max_tokens,
                "keep_initial_query": self.config.keep_initial_query,
                "keep_system_messages": self.config.keep_system_messages,
            }
            self.logger.log_agent_start(query, config_data)
        # Append user message
        user_msg = {"role": "user", "content": query}
        self.conversation.append(user_msg)
        # Estimate tokens for the new user message (including JSON structure) and update current count
        estimated_tokens = self._estimate_tokens(user_msg)
        self.state.current_conversation_tokens += estimated_tokens
        
        prev_conversation_len = len(self.conversation)
        last_input_tokens = 0
        last_output_tokens = 0
        
        for turn in range(self.config.max_turns):
            # Log turn start
            if self.logger:
                self.logger.log_turn_start(turn)
            
            # Check stop signal
            if self.stop_check and self.stop_check():
                # Update execution state: transition to STOPPED
                events = self.state.set_execution_state(ExecutionState.STOPPED)
                for event in events:
                    self._handle_state_event(event)
                
                if self.logger:
                    self.logger.log_stop_signal()
                    self.logger.log_agent_end("stopped", "Stop signal received")
                    self.logger.close()
                yield {
                    "type": "stopped",
                    "turn": turn,
                    "context_length": self.state.current_conversation_tokens,
                    "usage": {"input": last_input_tokens, "output": last_output_tokens,
                              "total_input": self.total_input_tokens, "total_output": self.total_output_tokens}
                }
                return
            
            # Turn monitoring warning
            turn_events = self.state.update_turn_state(turn)
            for event in turn_events:
                if event["type"] == "turn_warning":
                    # Add warning message to conversation as user message
                    warning_msg = {"role": "user", "content": event["message"]}
                    self.conversation.append(warning_msg)
                    # Update token count for warning message
                    warning_tokens = self._estimate_tokens(warning_msg)
                    self.state.current_conversation_tokens += warning_tokens
                # Yield event with usage info
                yield {
                    "type": event["type"],
                    "message": event["message"],
                    "turn_count": event.get("turn_count", turn),
                    "context_length": self.state.current_conversation_tokens,
                    "usage": {
                        "input": last_input_tokens,
                        "output": last_output_tokens,
                        "total_input": self.total_input_tokens,
                        "total_output": self.total_output_tokens,
                    }
                }

            # Token monitoring warning
            token_events = self.state.update_token_state(self.state.current_conversation_tokens)
            for event in token_events:
                if event["type"] == "token_warning":
                    # Add warning message to conversation as user message
                    warning_msg = {"role": "user", "content": event["message"]}
                    self.conversation.append(warning_msg)
                    # Update token count for warning message
                    warning_tokens = self._estimate_tokens(warning_msg)
                    self.state.current_conversation_tokens += warning_tokens
                # Yield event with usage info
                yield {
                    "type": event["type"],
                    "message": event["message"],
                    "token_count": event.get("token_count", self.state.current_conversation_tokens),
                    "context_length": self.state.current_conversation_tokens,
                    "usage": {
                        "input": last_input_tokens,
                        "output": last_output_tokens,
                        "total_input": self.total_input_tokens,
                        "total_output": self.total_output_tokens,
                    }
                }

            
            # Ensure any assistant message with tool_calls has reasoning_content field
            for msg in self.conversation:
                if msg.get("role") == "assistant" and "tool_calls" in msg:
                    if msg.get("reasoning_content") is None:
                        msg["reasoning_content"] = ""
            
            messages = self.conversation
            
            # Log LLM request
            if self.logger:
                self.logger.log_llm_request(messages, self.tool_definitions)
            
            # Call LLM provider
            formatted_tools = self.provider.format_tools(self.tool_definitions)
            try:
                response = self.provider.chat_completion(
                    messages=messages,
                    tools=formatted_tools,
                    temperature=self.config.temperature,
                )
                
                # Debug: Print raw response if environment variable is set
                import os
                if os.environ.get('DEBUG_RAW_RESPONSE'):
                    raw = str(response.raw_response)
                    if len(raw) > 1000:
                        raw = raw[:1000] + f"... (truncated, total {len(raw)} chars)"
                    print(f"[DEBUG_RAW_RESPONSE] Raw response type: {type(response.raw_response)}")
                    print(f"[DEBUG_RAW_RESPONSE] Raw response: {raw}")
            except ProviderError as e:
                # Update execution state to STOPPED
                events = self.state.set_execution_state(ExecutionState.STOPPED)
                for event in events:
                    self._handle_state_event(event)
                if self.logger:
                    self.logger.log_error("PROVIDER_ERROR", str(e))
                    self.logger.log_agent_end("provider_error", f"Provider error: {e}")
                    self.logger.close()
                yield {
                    "type": "error",
                    "message": str(e),
                    "traceback": traceback.format_exc(),
                    "turn": turn,
                    "context_length": self.state.current_conversation_tokens,
                    "usage": {
                        "input": last_input_tokens,
                        "output": last_output_tokens,
                        "total_input": self.total_input_tokens,
                        "total_output": self.total_output_tokens,
                    }
                }
                return
            
            # Token usage
            usage = response.usage
            if usage:
                input_tokens = usage.get("prompt_tokens", 0) or 0
                output_tokens = usage.get("completion_tokens", 0) or 0
            else:
                input_tokens = output_tokens = 0
            self.total_input_tokens += input_tokens
            self.total_output_tokens += output_tokens
            self.state.current_conversation_tokens = input_tokens
            last_input_tokens = input_tokens
            last_output_tokens = output_tokens

            # Extract assistant message from normalized response
            content = response.content or ""
            reasoning = response.reasoning
            tool_calls = response.tool_calls  # Already in normalized format            
            # Log LLM response
            if self.logger:
                usage_dict = {
                    "input": input_tokens,
                    "output": output_tokens,
                    "total_input": self.total_input_tokens,
                    "total_output": self.total_output_tokens,
                }
                tool_calls_dict = None
                if tool_calls:
                    tool_calls_dict = tool_calls
                self.logger.log_llm_response(content, reasoning, tool_calls_dict, usage_dict, response.raw_response)
            
            # Build assistant message dict for storage
            assistant_dict = {"role": "assistant", "content": content}
            if reasoning is not None:
                assistant_dict["reasoning_content"] = reasoning
            elif tool_calls:
                assistant_dict["reasoning_content"] = ""
            if tool_calls:
                assistant_dict["tool_calls"] = tool_calls
            
            self.conversation.append(assistant_dict)
            # Estimate tokens for assistant message
            assistant_tokens = self._estimate_tokens(assistant_dict)
            self.state.current_conversation_tokens += assistant_tokens
            
            if self.logger:
                self.logger.log_conversation_update(self.conversation, "append_assistant")
            
            # If there are tool calls, execute them and append tool responses
            if tool_calls:
                executed_tools = []
                final_detected = False
                final_content = None
                user_interaction_requested = False
                user_interaction_message = None
                summary_requested = False
                summary_text = None
                summary_keep_recent_turns = 0
                
                for tool_call in tool_calls:
                    tool_name = tool_call["function"]["name"]
                    
                    # Check if tool is allowed in current state
                    if not self.state.is_tool_allowed(tool_name):
                        allowed_tools = self.state.get_allowed_tools()
                        if allowed_tools:
                            tool_result = f"Tool '{tool_name}' not allowed in current state. Allowed tools: {', '.join(allowed_tools)}"
                        else:
                            tool_result = f"Tool '{tool_name}' not allowed in current state (token_state: {self.state.token_state.value}, turn_state: {self.state.turn_state.value})"
                        
                        # Append tool result with error
                        self.conversation.append({
                            "role": "tool",
                            "tool_call_id": tool_call["id"],
                            "content": tool_result
                        })
                        # Estimate tokens for tool result
                        tool_tokens = len(str(tool_result)) // 4
                        self.state.current_conversation_tokens += tool_tokens
                        
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
                            self.conversation.append({
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
                        try:
                            # Add workspace_path from config if tool supports it
                            tool_args = arguments.copy()
                            if self.config.workspace_path is not None:
                                tool_args['workspace_path'] = self.config.workspace_path
                            if self.config.tool_output_token_limit is not None:
                                tool_args['token_limit'] = self.config.tool_output_token_limit
                            tool_instance = tool_class(**tool_args)
                            tool_result = tool_instance.execute()
                            # Check if this is a Final tool
                            if isinstance(tool_instance, Final):
                                final_detected = True
                                final_content = tool_result
                            # Check if this is a RequestUserInteraction tool
                            if isinstance(tool_instance, RequestUserInteraction):
                                user_interaction_requested = True
                                user_interaction_message = tool_result
                            # Check if this is a SummarizeTool
                            if isinstance(tool_instance, SummarizeTool):
                                summary_requested = True
                                summary_text = tool_instance.summary
                                summary_keep_recent_turns = tool_instance.keep_recent_turns
                        except ValidationError as e:
                            tool_result = f"Invalid arguments: {e}"
                        except Exception as e:
                            tool_result = f"Error executing tool: {e}"
                    
                    # Log tool result
                    if self.logger:
                        self.logger.log_tool_result(tool_name, tool_result, tool_call["id"])
                    
                    # Append tool result
                    self.conversation.append({
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": tool_result
                    })
                    # Estimate tokens for tool result
                    tool_tokens = len(str(tool_result)) // 4
                    self.state.current_conversation_tokens += tool_tokens
                    
                    if self.logger:
                        self.logger.log_conversation_update(self.conversation, "append_tool_result")
                    
                    executed_tools.append({
                        "name": tool_name,
                        "arguments": arguments,
                        "result": tool_result
                    })
                
                # Apply summary pruning if requested
                if summary_requested:
                    self._apply_summary_pruning(summary_text, summary_keep_recent_turns)
                # Log turn completion
                if self.logger:
                    turn_usage = {"input": input_tokens, "output": output_tokens}
                    self.logger.log_turn_complete(turn, turn_usage)
                    current_len = len(self.conversation)
                    if current_len < prev_conversation_len:
                        if self.logger and hasattr(self.logger, 'py_logger'):
                            self.logger.py_logger.warning(f"[WARNING] Conversation length decreased within turn {turn}: {prev_conversation_len} -> {current_len}")
                prev_conversation_len = len(self.conversation)
                
                # Yield turn event
                yield {
                    "type": "turn",
                    "turn": turn,
                    "assistant_content": content,
                    "tool_calls": executed_tools,
                    "reasoning": reasoning,
                    "context_length": self.state.current_conversation_tokens,
                    "history": self.conversation.copy(),
                    "usage": {
                        "input": input_tokens,
                        "output": output_tokens,
                        "total_input": self.total_input_tokens,
                        "total_output": self.total_output_tokens,
                    }
                }
                
                # If a RequestUserInteraction tool was called, stop the agent and wait for user response
                if user_interaction_requested:
                    # Update execution state: transition to WAITING_FOR_USER
                    events = self.state.set_execution_state(ExecutionState.WAITING_FOR_USER)
                    for event in events:
                        self._handle_state_event(event)
                    
                    if self.logger:
                        self.logger.log_user_interaction_requested(user_interaction_message)
                        self.logger.log_agent_end("user_interaction_requested", "Waiting for user response")
                        self.logger.close()
                    yield {
                        "type": "user_interaction_requested",
                        "turn": turn,
                        "context_length": self.state.current_conversation_tokens,
                        "message": user_interaction_message,
                        "history": self.conversation.copy(),
                        "usage": {
                            "input": input_tokens,
                            "output": output_tokens,
                            "total_input": self.total_input_tokens,
                            "total_output": self.total_output_tokens,
                        }
                    }
                    return
                
                # If a Final tool was called, stop the agent
                if final_detected:
                    # Update execution state: transition to FINALIZED
                    events = self.state.set_execution_state(ExecutionState.FINALIZED)
                    for event in events:
                        self._handle_state_event(event)
                    
                    if self.logger:
                        self.logger.log_final_detected(final_content)
                        self.logger.log_agent_end("final", "Final tool executed", final_content)
                        self.logger.close()
                    yield {
                        "type": "final",
                        "content": final_content,
                        "context_length": self.state.current_conversation_tokens,
                        "reasoning": reasoning,
                        "usage": {
                            "input": input_tokens,
                            "output": output_tokens,
                            "total_input": self.total_input_tokens,
                            "total_output": self.total_output_tokens,
                        }
                    }
                    return
                
                # Otherwise continue to next turn
            else:
                # No tool calls: this is the final answer
                # Update execution state: transition to FINALIZED
                events = self.state.set_execution_state(ExecutionState.FINALIZED)
                for event in events:
                    self._handle_state_event(event)
                
                if self.logger:
                    self.logger.log_agent_end("final_no_tools", "Final answer without tool calls", content)
                    self.logger.close()
                yield {
                    "type": "final",
                    "content": content,
                     "context_length": self.state.current_conversation_tokens,
                    "reasoning": reasoning,
                    "usage": {
                        "input": input_tokens,
                        "output": output_tokens,
                        "total_input": self.total_input_tokens,
                        "total_output": self.total_output_tokens,
                    }
                }
                return
        
        # Max turns reached without final answer
        # Update execution state: transition to MAX_TURNS_REACHED
        events = self.state.set_execution_state(ExecutionState.MAX_TURNS_REACHED)
        for event in events:
            self._handle_state_event(event)
        
        if self.logger:
            self.logger.log_max_turns_reached()
            self.logger.log_agent_end("max_turns", f"Maximum turns ({self.config.max_turns}) reached without final answer")
            self.logger.close()
        yield {
            "type": "max_turns",
            "turn": self.config.max_turns,
             "context_length": self.state.current_conversation_tokens,
            "usage": {"input": last_input_tokens, "output": last_output_tokens,
                      "total_input": self.total_input_tokens, "total_output": self.total_output_tokens}
        }