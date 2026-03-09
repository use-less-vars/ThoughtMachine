# agent.py - Modular agent core
from __future__ import annotations
import json
import os
import queue
from typing import Optional, List, Dict, Any, TYPE_CHECKING
from openai import OpenAI
from pydantic import ValidationError
from tools import SIMPLIFIED_TOOL_CLASSES
from tools.utils import model_to_openai_tool
from tools.final import Final
from tools.request_user_interaction import RequestUserInteraction
from fast_json_repair import loads as repair_loads

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

class Agent:
    def __init__(self, config: AgentConfig, initial_conversation=None):
        self.config = config
        self.client = OpenAI(api_key=config.api_key, base_url="https://api.deepseek.com")
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
            if self.config.extra_system:
                self.conversation.append({"role": "system", "content": self.config.extra_system})
    
    def reset(self):
        self.conversation = []
        self._ensure_system_prompt()
        self.total_input_tokens = self.config.initial_input_tokens
        self.total_output_tokens = self.config.initial_output_tokens
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
    
    def process_query(self, query):
        """Process a user query, appending it to conversation and running the agent.
        Yields events as dicts."""
        from agent_core import prune_conversation_history
        # Ensure system prompt present
        self._ensure_system_prompt()
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
        self.conversation.append({"role": "user", "content": query})
        
        prev_conversation_len = len(self.conversation)
        last_input_tokens = 0
        last_output_tokens = 0
        
        for turn in range(self.config.max_turns):
            # Log turn start
            if self.logger:
                self.logger.log_turn_start(turn)
            
            # Check stop signal
            if self.stop_check and self.stop_check():
                if self.logger:
                    self.logger.log_stop_signal()
                    self.logger.log_agent_end("stopped", "Stop signal received")
                    self.logger.close()
                yield {
                    "type": "stopped",
                    "turn": turn,
                    "usage": {"input": last_input_tokens, "output": last_output_tokens,
                              "total_input": self.total_input_tokens, "total_output": self.total_output_tokens}
                }
                return
            
            # Prune conversation history if configured
            original_len = len(self.conversation)
            self.conversation = prune_conversation_history(self.conversation, self.config)
            new_len = len(self.conversation)
            if self.logger and new_len < original_len:
                reason = "config.max_history_turns" if self.config.max_history_turns else "config.max_tokens"
                self.logger.log_conversation_prune(original_len, new_len, reason)
            
            # Ensure any assistant message with tool_calls has reasoning_content field
            for msg in self.conversation:
                if msg.get("role") == "assistant" and "tool_calls" in msg:
                    if msg.get("reasoning_content") is None:
                        msg["reasoning_content"] = ""
            
            messages = self.conversation
            
            # Log LLM request
            if self.logger:
                self.logger.log_llm_request(messages, self.tool_definitions)
            
            # Call OpenAI with tools
            response = self.client.chat.completions.create(
                model=self.config.model,
                messages=messages,
                tools=self.tool_definitions,
                tool_choice="auto",
                temperature=self.config.temperature,
            )
            
            # Token usage
            usage = response.usage
            if usage:
                input_tokens = usage.prompt_tokens or 0
                output_tokens = usage.completion_tokens or 0
            else:
                input_tokens = output_tokens = 0
            self.total_input_tokens += input_tokens
            self.total_output_tokens += output_tokens
            last_input_tokens = input_tokens
            last_output_tokens = output_tokens
            
            # Extract assistant message
            assistant_message = response.choices[0].message
            content = assistant_message.content or ""
            reasoning = getattr(assistant_message, 'reasoning_content', None)
            tool_calls = assistant_message.tool_calls
            
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
                    tool_calls_dict = [tc.model_dump() for tc in tool_calls]
                self.logger.log_llm_response(content, reasoning, tool_calls_dict, usage_dict)
            
            # Build assistant message dict for storage
            assistant_dict = {"role": "assistant", "content": content}
            if reasoning is not None:
                assistant_dict["reasoning_content"] = reasoning
            elif tool_calls:
                assistant_dict["reasoning_content"] = ""
            if tool_calls:
                assistant_dict["tool_calls"] = [tc.model_dump() for tc in tool_calls]
            
            self.conversation.append(assistant_dict)
            
            if self.logger:
                self.logger.log_conversation_update(self.conversation, "append_assistant")
            
            # If there are tool calls, execute them and append tool responses
            if tool_calls:
                executed_tools = []
                final_detected = False
                final_content = None
                user_interaction_requested = False
                user_interaction_message = None
                
                for tool_call in tool_calls:
                    tool_name = tool_call.function.name
                    arguments_str = tool_call.function.arguments
                    
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
                                "tool_call_id": tool_call.id,
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
                        self.logger.log_tool_call(tool_name, arguments, tool_call.id)
                    
                    # Find matching tool class
                    tool_class = next((cls for cls in self.tool_classes if cls.__name__ == tool_name), None)
                    if not tool_class:
                        error_msg = f"Unknown tool: {tool_name}"
                        tool_result = error_msg
                    else:
                        try:
                            tool_instance = tool_class(**arguments)
                            tool_result = tool_instance.execute()
                            # Check if this is a Final tool
                            if isinstance(tool_instance, Final):
                                final_detected = True
                                final_content = tool_result
                            # Check if this is a RequestUserInteraction tool
                            if isinstance(tool_instance, RequestUserInteraction):
                                user_interaction_requested = True
                                user_interaction_message = tool_result
                        except ValidationError as e:
                            tool_result = f"Invalid arguments: {e}"
                        except Exception as e:
                            tool_result = f"Error executing tool: {e}"
                    
                    # Log tool result
                    if self.logger:
                        self.logger.log_tool_result(tool_name, tool_result, tool_call.id)
                    
                    # Append tool result
                    self.conversation.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_result
                    })
                    
                    if self.logger:
                        self.logger.log_conversation_update(self.conversation, "append_tool_result")
                    
                    executed_tools.append({
                        "name": tool_name,
                        "arguments": arguments,
                        "result": tool_result
                    })
                
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
                    if self.logger:
                        self.logger.log_user_interaction_requested(user_interaction_message)
                        self.logger.log_agent_end("user_interaction_requested", "Waiting for user response")
                        self.logger.close()
                    yield {
                        "type": "user_interaction_requested",
                        "turn": turn,
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
                    if self.logger:
                        self.logger.log_final_detected(final_content)
                        self.logger.log_agent_end("final", "Final tool executed", final_content)
                        self.logger.close()
                    yield {
                        "type": "final",
                        "content": final_content,
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
                if self.logger:
                    self.logger.log_agent_end("final_no_tools", "Final answer without tool calls", content)
                    self.logger.close()
                yield {
                    "type": "final",
                    "content": content,
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
        if self.logger:
            self.logger.log_max_turns_reached()
            self.logger.log_agent_end("max_turns", f"Maximum turns ({self.config.max_turns}) reached without final answer")
            self.logger.close()
        yield {
            "type": "max_turns",
            "turn": self.config.max_turns,
            "usage": {"input": last_input_tokens, "output": last_output_tokens,
                      "total_input": self.total_input_tokens, "total_output": self.total_output_tokens}
        }