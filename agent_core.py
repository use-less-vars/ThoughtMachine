import json
import logging
from typing import Optional, Callable, Dict, Any, Union
from pathlib import Path
import instructor
from openai import OpenAI
from pydantic import BaseModel, Field

# Import the tool registry and union type
from tools import TOOL_REGISTRY, AgentResponse, ToolBase

# ----------------------------
# Configuration
# ----------------------------

class AgentConfig(BaseModel):
    api_key: str
    model: str = "deepseek-chat"
    temperature: float = 0.2
    max_turns: int = 12
    extra_system: Optional[str] = None
    # Optional callable to check if we should stop (e.g., from GUI)
    stop_check: Optional[Callable[[], bool]] = None
    # You can add more parameters like tool_choice, response_model, etc.

# ----------------------------
# Client factory
# ----------------------------

def get_client(api_key: str):
    return instructor.from_openai(
        OpenAI(api_key=api_key, base_url="https://api.deepseek.com"),
        mode=instructor.Mode.JSON
    )

# ----------------------------
# System prompt (can be moved to a separate file later)
# ----------------------------

SYSTEM_PROMPT = """
You are an assistant that can use tools.
First, think about the problem. Use the tools if needed. When done, use the final tool to output your answer.
"""

# ----------------------------
# Agent loop with streaming
# ----------------------------

def run_agent_stream(query: str, config: AgentConfig):
    """
    Yields dictionaries with keys:
      - type: 'turn', 'final', 'stopped', 'max_turns', 'error'
      - turn: int (if applicable)
      - tool_call: dict (if turn)
      - tool_result: str (if turn)
      - history: list of messages (if turn)
      - usage: dict with input/output tokens
    """
    client = get_client(config.api_key)

    # Conversation history starts with system and user query
    conversation = [
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
                "usage": {
                    "total_input": total_input_tokens,
                    "total_output": total_output_tokens,
                }
            }
            return

        # Build messages for this turn: fresh system + everything after initial system
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        if config.extra_system:
            messages.append({"role": "system", "content": config.extra_system})
        # Append all messages from conversation after the first system message(s)
        # Conversation[0] is the original system, maybe followed by extra_system.
        # We want to include everything from index 1 onward (including the user query)
        start_idx = 2 if config.extra_system else 1
        messages.extend(conversation[start_idx:])

        # Call the LLM
        response = client.chat.completions.create(
            model=config.model,
            messages=messages,
            response_model=AgentResponse,
            temperature=config.temperature,
            max_retries=3,
        )

        # Extract token usage if available (instructor may put it in _raw_response)
        usage = {}
        if hasattr(response, '_raw_response') and response._raw_response.usage:
            raw_usage = response._raw_response.usage
            input_tokens = raw_usage.prompt_tokens
            output_tokens = raw_usage.completion_tokens
            total_input_tokens += input_tokens
            total_output_tokens += output_tokens
            usage = {
                "input": input_tokens,
                "output": output_tokens,
                "total_input": total_input_tokens,
                "total_output": total_output_tokens,
            }
        else:
            usage = {
                "total_input": total_input_tokens,
                "total_output": total_output_tokens,
            }

        tool_call = response.model_dump_tool()  # uses our helper to exclude execute
        tool_result = response.execute()

        # Append to conversation
        conversation.append({"role": "assistant", "content": json.dumps(tool_call)})
        conversation.append({"role": "user", "content": f"Tool responded: {tool_result}"})

        # Yield turn data
        yield {
            "type": "turn",
            "turn": turn,
            "tool_call": tool_call,
            "tool_result": tool_result,
            "history": conversation.copy(),
            "usage": usage,
        }

        # Check if this was the final answer
        if response.tool == "final":
            yield {
                "type": "final",
                "content": tool_result,
                "usage": {
                    "total_input": total_input_tokens,
                    "total_output": total_output_tokens,
                }
            }
            return

    # Max turns reached
    yield {
        "type": "max_turns",
        "turn": config.max_turns,
        "usage": {
            "total_input": total_input_tokens,
            "total_output": total_output_tokens,
        }
    }