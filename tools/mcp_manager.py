"""
MCP Manager - Integrates Model Context Protocol servers as agent tools.
"""
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Any, Union, Type, get_type_hints
from enum import Enum
from pydantic import BaseModel, Field, create_model, model_validator
import logging

from .mcp_client import StdioMCPClient
from .base import ToolBase

logger = logging.getLogger(__name__)

class TransportType(str, Enum):
    STDIO = "stdio"
    HTTP = "http"
    SSE = "sse"

class MCPServerConfig(BaseModel):
    """Configuration for a single MCP server.
    
    Attributes:
        name: Unique identifier for the server
        transport: Transport protocol (stdio, http, sse)
        command: Executable to run (required for stdio)
        args: Command arguments
        env: Environment variables for subprocess
        url: Server URL (required for http/sse)
        api_key_env_var: Environment variable containing API key
    """
    name: str = Field(..., description="Unique name for this server")
    transport: TransportType = Field(..., description="Transport type")
    # For stdio transport
    command: Optional[str] = Field(None, description="Command to execute (for stdio)")
    args: Optional[List[str]] = Field(None, description="Arguments for command")
    env: Optional[Dict[str, str]] = Field(None, description="Environment variables")
    # For HTTP/SSE transport
    url: Optional[str] = Field(None, description="URL for HTTP/SSE server")
    # Authentication
    api_key_env_var: Optional[str] = Field(None, description="Environment variable name for API key")
    
    @model_validator(mode='after')
    def validate_transport_params(self) -> "MCPServerConfig":
        """Validate transport-specific required parameters.
        
        Returns:
            Self if validation passes
            
        Raises:
            ValueError: If required parameters are missing
        """
        if self.transport == TransportType.STDIO:
            if not self.command:
                raise ValueError(
                    f"Server '{self.name}': 'command' is required for stdio transport"
                )
        elif self.transport in (TransportType.HTTP, TransportType.SSE):
            if not self.url:
                raise ValueError(
                    f"Server '{self.name}': 'url' is required for {self.transport} transport"
                )
        return self

class MCPConfig(BaseModel):
    """Root configuration for MCP servers."""
    servers: List[MCPServerConfig] = Field(default_factory=list)
    config_path: Optional[str] = Field(None, description="Path to config file (auto-populated)")
    
    @classmethod
    def load(cls, path: Optional[str] = None) -> "MCPConfig":
        """Load configuration from file or environment."""
        config_path = path or os.environ.get("MCP_CONFIG_PATH", "mcp_config.json")
        config_path = Path(config_path)
        if not config_path.exists():
            logger.info(f"MCP config file not found at {config_path}, using empty config")
            return cls(servers=[])
        with open(config_path, "r") as f:
            data = json.load(f)
        config = cls(**data, config_path=str(config_path))
        # Validation is automatically performed during model construction via @model_validator
        return config


def json_schema_to_field(schema: Dict[str, Any], required: bool = False) -> Any:
    """Convert a JSON Schema property to a Pydantic Field.
    
    Supports a subset of JSON Schema draft 7:
    - Basic types: string, integer, number, boolean, array, object
    - Array item types (including nested arrays and objects)
    - Optional: format, minimum, maximum, pattern (for validation)
    
    Args:
        schema: JSON Schema property definition
        required: Whether this field is required
        
    Returns:
        Tuple of (Python type, Field instance)
    """
    field_type = schema.get("type", "string")
    description = schema.get("description", "")
    default = ... if required else None
    
    # Map JSON schema types to Python types
    type_mapping = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": List[Any],
        "object": Dict[str, Any]
    }
    py_type = type_mapping.get(field_type, str)
    
    # Build field kwargs
    field_kwargs = {"default": default, "description": description}
    
    # Handle array items with proper typing
    if field_type == "array" and "items" in schema:
        items_schema = schema["items"]
        items_type = items_schema.get("type")
        if items_type == "string":
            py_type = List[str]
        elif items_type == "integer":
            py_type = List[int]
        elif items_type == "number":
            py_type = List[float]
        elif items_type == "boolean":
            py_type = List[bool]
        elif items_type == "object":
            py_type = List[Dict[str, Any]]
        elif items_type == "array":
            # Nested arrays - simplified to List[List[Any]]
            py_type = List[List[Any]]
    
    # Handle object properties (simplified - could be enhanced with nested models)
    if field_type == "object" and "properties" in schema:
        # For now, keep as Dict[str, Any]
        # Future: could create nested Pydantic models
        pass
    
    # Add basic validation constraints where appropriate
    if field_type == "string":
        if "minLength" in schema or "maxLength" in schema:
            pass  # TODO: could add StringConstraints
        if "pattern" in schema:
            pass  # TODO: could add regex validation
    elif field_type in ("integer", "number"):
        if "minimum" in schema or "maximum" in schema:
            pass  # TODO: could add ge/le constraints
    
    # Create Field with description
    return (py_type, Field(**field_kwargs))


def create_tool_class(server_name: str, tool_def: Dict[str, Any], client: StdioMCPClient) -> Type[ToolBase]:
    """Create a dynamic ToolBase subclass for an MCP tool."""
    tool_name = tool_def["name"]
    description = tool_def.get("description", "")
    input_schema = tool_def.get("inputSchema", {})
    properties = input_schema.get("properties", {})
    required = input_schema.get("required", [])
    
    # Build fields dict for create_model
    fields = {}
    for prop_name, prop_schema in properties.items():
        required_flag = prop_name in required
        field_type, field_info = json_schema_to_field(prop_schema, required_flag)
        fields[prop_name] = (field_type, field_info)
    
    # Add server_name and tool_name as class attributes
    class_name = f"{server_name}_{tool_name}".title().replace("_", "").replace("-", "")
    # Ensure class name is valid Python identifier
    class_name = ''.join(c for c in class_name if c.isalnum())
    if not class_name[0].isalpha():
        class_name = "MCP" + class_name
    
    # Create the model
    ToolModel = create_model(class_name, __base__=ToolBase, **fields)
    
    # Add execute method that calls the MCP client
    def execute(self: ToolBase) -> str:
        # Extract arguments from self
        arguments = {}
        for field_name in properties.keys():
            if hasattr(self, field_name):
                value = getattr(self, field_name)
                if value is not None:
                    arguments[field_name] = value
        try:
            result = client.call_tool(tool_name, arguments)
            return self._truncate_output(str(result))
        except Exception as e:
            return f"Error calling MCP tool {tool_name}: {e}"
    
    ToolModel.execute = execute
    
    # Store metadata
    ToolModel._mcp_server_name = server_name
    ToolModel._mcp_tool_name = tool_name
    ToolModel.__doc__ = description
    
    return ToolModel


class MCPServerManager:
    """Manager for MCP server lifecycle and tool registration.
    
    Responsibilities:
        - Load server configurations from MCPConfig
        - Start/stop MCP server subprocesses
        - Dynamically generate ToolBase classes from MCP tool schemas
        - Register generated tools with the global TOOL_CLASSES list
        
    Attributes:
        config: MCPConfig with server definitions
        servers: Active MCP clients keyed by server name
        tool_classes: List of dynamically generated tool classes
    """
    
    def __init__(self, config: MCPConfig):
        self.config = config
        self.servers: Dict[str, StdioMCPClient] = {}
        self.tool_classes: List[Type[ToolBase]] = []
        
    def start_all(self) -> None:
        """Start all configured servers and register their tools.
        
        Only stdio transport servers are currently supported. HTTP/SSE
        servers will be skipped with a warning.
        """
        for server_config in self.config.servers:
            if server_config.transport != TransportType.STDIO:
                logger.warning(f"Skipping server {server_config.name}: only stdio transport supported currently")
                continue
            try:
                client = StdioMCPClient(
                    command=server_config.command,
                    args=server_config.args,
                    env=server_config.env
                )
                client.start()
                self.servers[server_config.name] = client
                tools = client.list_tools()
                logger.info(f"Server {server_config.name} provided {len(tools)} tools")
                for tool_def in tools:
                    tool_class = create_tool_class(server_config.name, tool_def, client)
                    self.tool_classes.append(tool_class)
            except Exception as e:
                logger.error(f"Failed to start MCP server {server_config.name}: {e}")
                
    def stop_all(self) -> None:
        """Stop all running MCP server subprocesses.
        
        graceful shutdown via terminate() with 5s timeout.
        """
        for name, client in self.servers.items():
            try:
                client.stop()
            except Exception as e:
                logger.error(f"Error stopping server {name}: {e}")
        self.servers.clear()
        
    def get_tool_classes(self) -> List[Type[ToolBase]]:
        """Return list of dynamically generated tool classes.
        
        Returns:
            List of ToolBase subclasses ready for registration
        """
        return self.tool_classes


# Global instance
_manager: Optional[MCPServerManager] = None

def get_mcp_manager() -> MCPServerManager:
    """Get or create the global MCP server manager singleton.
    
    Uses lazy initialization: on first call, loads config and starts
    all configured servers. Subsequent calls return the same manager.
    
    Returns:
        MCPServerManager instance
    """
    global _manager
    if _manager is None:
        config = MCPConfig.load()
        _manager = MCPServerManager(config)
        _manager.start_all()
    return _manager

def register_mcp_tools() -> None:
    """Register MCP-generated tool classes with the global TOOL_CLASSES.
    
    This function is called automatically from tools/__init__.py during
    module initialization. It discovers all tools from configured MCP
    servers and adds them to TOOL_CLASSES so they become available to
    the agent.
    """
    manager = get_mcp_manager()
    from . import TOOL_CLASSES
    tool_classes = manager.get_tool_classes()
    for cls in tool_classes:
        if cls not in TOOL_CLASSES:
            TOOL_CLASSES.append(cls)

