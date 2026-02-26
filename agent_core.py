# agent_core.py
import json
import logging
from typing import Optional, Callable, List, Any, Dict
from openai import OpenAI
from pydantic import BaseModel, ValidationError

from tools import TOOL_CLASSES
from tools.base import ToolBase
from tools.final import Final
from tools.utils import model_to_openai_tool
from fast_json_repair import loads as repair_loads 

class AgentConfig(BaseModel):
    api_key: str
    model: str = "deepseek-chat"
    temperature: float = 0.2
    max_turns: int = 12
    extra_system: Optional[str] = None
    stop_check: Optional[Callable[[], bool]] = None
    tool_classes: Optional[List[type]] = None   # 

SYSTEM_PROMPT = """
You are an assistant that can use tools.
First, think about the problem. Use the tools if needed. When done, use the final tool to output your answer.

IMPORTANT: When providing long text content (e.g., for file writing), you must base64‑encode it to avoid JSON escaping issues. Use the field `content_base64` for such tools.
"""

def run_agent_stream(query: str, config: AgentConfig):
    client = OpenAI(api_key=config.api_key, base_url="https://api.deepseek.com")

    # Prepare tool definitions for OpenAI
    tool_classes = config.tool_classes if config.tool_classes is not None else TOOL_CLASSES
    tool_definitions = [model_to_openai_tool(cls) for cls in tool_classes]
    # Build conversation starting with system message(s) and the user query
    conversation: List[Dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT}
    ]
    if config.extra_system:
        conversation.append({"role": "system", "content": config.extra_system})
    conversation.append({"role": "user", "content": query})

    total_input_tokens = 0
    total_output_tokens = 0

    for turn in range(config.max_turns):
        # Check stop signal
        if config.stop_check and config.stop_check():
            yield {
                "type": "stopped",
                "turn": turn,
                "usage": {"total_input": total_input_tokens, "total_output": total_output_tokens}
            }
            return

        # Use the full conversation as messages (system messages remain)
        messages = conversation

        # Call OpenAI with tools
        response = client.chat.completions.create(
            model=config.model,
            messages=messages,
            tools=tool_definitions,
            tool_choice="auto",
            temperature=config.temperature,
        )

        # Token usage
        usage = response.usage
        if usage:
            input_tokens = usage.prompt_tokens or 0
            output_tokens = usage.completion_tokens or 0
        else:
            input_tokens = output_tokens = 0
        total_input_tokens += input_tokens
        total_output_tokens += output_tokens

        # Extract assistant message (may contain reasoning_content and tool_calls)
        assistant_message = response.choices[0].message
        content = assistant_message.content or ""
        reasoning = getattr(assistant_message, 'reasoning_content', None)
        tool_calls = assistant_message.tool_calls

        # Build assistant message dict for storage
        assistant_dict = {"role": "assistant", "content": content}
        if reasoning:
            assistant_dict["reasoning_content"] = reasoning
        if tool_calls:
            # Convert tool calls to dict for storage (they are Pydantic models)
            assistant_dict["tool_calls"] = [tc.model_dump() for tc in tool_calls]

        conversation.append(assistant_dict)

        # If there are tool calls, execute them and append tool responses
        if tool_calls:
            executed_tools = []
            final_detected = False
            final_content = None

            for tool_call in tool_calls:
                tool_name = tool_call.function.name
                arguments_str = tool_call.function.arguments

                try:
                    arguments = json.loads(arguments_str)
                except json.JSONDecodeError:
                    try:
                        arguments = repair_loads(arguments_str)
                        # Optional: log repair
                        print(f"Repaired JSON for {tool_name}")
                    except Exception as e:
                        tool_result = f"Invalid JSON in arguments: {e}. Raw: {arguments_str}"
                        # Append error result
                        conversation.append({
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
                try:
    # First attempt: normal JSON parse (fastest for valid JSON)
                    arguments = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError:
                    try:
                        # Second attempt: use repair library
                        arguments = repair_loads(tool_call.function.arguments)
                        # Log that repair was needed (optional, for debugging)
                        print(f"JSON repair applied for tool {tool_name}")
                    except Exception as repair_error:
                        # If both fail, return error to LLM for retry
                        error_msg = f"Invalid JSON in tool arguments. Original error: {e}. Repair failed: {repair_error}"
                        tool_result = error_msg
                        continue  # Skip to next tool or handle appropriately

                # Find matching tool class
                tool_class = next((cls for cls in TOOL_CLASSES if cls.__name__ == tool_name), None)
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
                    except ValidationError as e:
                        tool_result = f"Invalid arguments: {e}"
                    except Exception as e:
                        tool_result = f"Error executing tool: {e}"

                # Append tool result as a message with role "tool"
                conversation.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": tool_result
                })

                executed_tools.append({
                    "name": tool_name,
                    "arguments": arguments,
                    "result": tool_result
                })

            # Yield turn event with all tool calls and results
            yield {
                "type": "turn",
                "turn": turn,
                "tool_calls": executed_tools,
                "reasoning": reasoning,
                "history": conversation.copy(),
                "usage": {
                    "input": input_tokens,
                    "output": output_tokens,
                    "total_input": total_input_tokens,
                    "total_output": total_output_tokens,
                }
            }

            # If a Final tool was called, stop the agent
            if final_detected:
                yield {
                    "type": "final",
                    "content": final_content,
                    "reasoning": reasoning,
                    "usage": {
                        "total_input": total_input_tokens,
                        "total_output": total_output_tokens,
                    }
                }
                return

            # Otherwise continue to next turn
        else:
            # No tool calls: this is the final answer
            yield {
                "type": "final",
                "content": content,
                "reasoning": reasoning,
                "usage": {
                    "total_input": total_input_tokens,
                    "total_output": total_output_tokens,
                }
            }
            return

    # Max turns reached without final answer
    yield {
        "type": "max_turns",
        "turn": config.max_turns,
        "usage": {"total_input": total_input_tokens, "total_output": total_output_tokens}
    }