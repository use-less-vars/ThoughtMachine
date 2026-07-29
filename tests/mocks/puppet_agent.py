"""Puppet LLM for hermetic agent testing."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from llm_providers.base import LLMResponse


class PuppetLLM:
    """Mimics LLMClient.chat_completion by returning canned LLMResponses.

    Initialise with a scenario (list of turn dicts) and an optional default
    response. Each call to chat_completion() pops the next turn and returns
    the appropriate LLMResponse.

    Scenario turn formats:
        {"type": "assistant", "content": "Hello"}
            -> LLMResponse(content="Hello")
        {"type": "tool_call", "tool_name": "ReadFile", "arguments": {"path": "/tmp/x"}}
            -> LLMResponse(content="", tool_calls=[{...}])
        {"type": "respond", "content": "Done", "status": "final", ...}
            -> LLMResponse(content="", tool_calls=[Respond tool call])
    """

    def __init__(
        self,
        scenario: List[Dict[str, Any]],
        default_response: Optional[LLMResponse] = None,
    ):
        self.scenario = list(scenario)
        self.default_response = default_response or LLMResponse(content="")
        self.call_count = 0

    def chat_completion(
        self, messages, tools=None, **kwargs
    ) -> LLMResponse:
        """Return the next canned response. Signature matches LLMClient."""
        self.call_count += 1

        if not self.scenario:
            return self.default_response

        turn = self.scenario.pop(0)
        turn_type = turn["type"]

        if turn_type == "assistant":
            return LLMResponse(
                content=turn.get("content", ""),
                reasoning=turn.get("reasoning"),
            )

        elif turn_type == "tool_call":
            return LLMResponse(
                content="",
                tool_calls=[
                    {
                        "id": f"call_{self.call_count}",
                        "type": "function",
                        "function": {
                            "name": turn["tool_name"],
                            "arguments": json.dumps(turn.get("arguments", {})),
                        },
                    }
                ],
            )

        elif turn_type == "respond":
            args = {"content": turn.get("content", "")}
            for key in ("status", "confidence", "response_type"):
                if key in turn:
                    args[key] = turn[key]
            return LLMResponse(
                content="",
                tool_calls=[
                    {
                        "id": f"call_{self.call_count}",
                        "type": "function",
                        "function": {
                            "name": "Respond",
                            "arguments": json.dumps(args),
                        },
                    }
                ],
            )

        else:
            raise ValueError(f"Unknown turn type: {turn_type}")
