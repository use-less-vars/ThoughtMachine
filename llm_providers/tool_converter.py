"""
Tool format conversion between different provider schemas.
Handles OpenAI, Anthropic, and other tool formats .
"""
from typing import List, Dict, Any
import json

from agent.logging import log
from .exceptions import ToolFormatError

class ToolFormatConverter:
    """
    Converts internal tool definitions to provider-specific formats.
    Implements conversion layer for different provider tool schemas .
    """
    
    @staticmethod
    def to_openai(tools: List[Dict]) -> List[Dict]:
        """
        Convert to OpenAI function calling format.
        This is our internal standard format .
        """
        return tools  # Already in OpenAI format
    
    @staticmethod
    def to_anthropic(tools: List[Dict]) -> List[Dict]:
        """
        Convert OpenAI format to Anthropic tool format .
        
        Anthropic format:
        {
            "name": "function_name",
            "description": "function description",
            "input_schema": { ... }  # JSON schema
        }
        """
        anthropic_tools = []
        
        for tool in tools:
            try:
                # Extract function definition
                function = tool.get("function", {})
                
                # Convert to Anthropic format
                anthropic_tool = {
                    "name": function.get("name"),
                    "description": function.get("description", ""),
                    "input_schema": function.get("parameters", {})
                }
                
                anthropic_tools.append(anthropic_tool)
                
            except Exception as e:
                raise ToolFormatError(f"Failed to convert tool to Anthropic format: {e}")
        
        return anthropic_tools
    
    @staticmethod
    def to_gemini(tools: List[Dict]) -> List[Dict]:
        """
        Convert OpenAI format to Google Gemini function declarations.
        
        Gemini format:
        {
            "function_declarations": [
                {
                    "name": "function_name",
                    "description": "...",
                    "parameters": { ... }
                }
            ]
        }
        """
        declarations = []
        
        for tool in tools:
            try:
                function = tool.get("function", {})
                declaration = {
                    "name": function.get("name"),
                    "description": function.get("description", ""),
                    "parameters": function.get("parameters", {})
                }
                declarations.append(declaration)
                
            except Exception as e:
                raise ToolFormatError(f"Failed to convert tool to Gemini format: {e}")
        
        return [{"function_declarations": declarations}]
    
    @staticmethod
    def from_tool_calls(response, provider: str) -> List[Dict]:
        """
        Convert provider-specific tool calls back to internal format.
        """
        if provider == "anthropic":
            return ToolFormatConverter._from_anthropic_tool_calls(response)
        elif provider == "openai_compatible":
            return ToolFormatConverter._from_openai_tool_calls(response)
        else:
            log('WARNING', 'llm.tool_converter', f'Unknown provider for tool call conversion: {provider}')
            return []
    
    @staticmethod
    def _from_openai_tool_calls(response) -> List[Dict]:
        """Extract tool calls from OpenAI response"""
        if not hasattr(response, 'choices') or not response.choices:
            return []
        
        message = response.choices[0].message
        if not hasattr(message, 'tool_calls') or not message.tool_calls:
            return []
        
        tool_calls = []
        for tc in message.tool_calls:
            # Handle both dictionary and object tool calls
            if hasattr(tc, 'function'):
                # Object format (OpenAI SDK)
                name = tc.function.name
                arguments = tc.function.arguments
                tc_id = tc.id
            else:
                # Dictionary format
                func = tc.get("function", {})
                name = func.get("name")
                arguments = func.get("arguments")
                tc_id = tc.get("id")
            tool_calls.append({
                "id": tc_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": arguments
                }
            })
        
        return tool_calls
    
    @staticmethod
    def _from_anthropic_tool_calls(response) -> List[Dict]:
        """Extract tool calls from Anthropic response"""
        if not hasattr(response, 'content') or not response.content:
            return []
        
        tool_calls = []
        for content_block in response.content:
            if content_block.type == "tool_use":
                # Handle both dictionary and object tool calls
                if hasattr(content_block, 'name'):
                    # Object format (Anthropic SDK)
                    name = content_block.name
                    arguments = json.dumps(content_block.input)
                    cb_id = content_block.id
                else:
                    # Dictionary format
                    name = content_block.get("name")
                    arguments = json.dumps(content_block.get("input", {}))
                    cb_id = content_block.get("id")
                tool_calls.append({
                    "id": cb_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": arguments
                    }
                })
        
        return tool_calls
