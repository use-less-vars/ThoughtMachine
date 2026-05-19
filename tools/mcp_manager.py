"""
MCP Manager - Integrates Model Context Protocol servers as agent tools.
"""
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Any, Union, Type, get_type_hints, Literal
from enum import Enum
from pydantic import BaseModel, Field, create_model, model_validator
import logging

from .mcp_client import create_mcp_client, MCPClientBase
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
        headers: Additional HTTP headers (for http/sse transport)
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
    headers: Optional[Dict[str, str]] = Field(None, description="Additional HTTP headers")
    
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
        Tuple of (type_annotation, Field instance)
    """
    field_type = Any
    field_kwargs = {}
    
    # Handle type (could be string or list)
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        # Use first non-null type
        schema_type = [t for t in schema_type if t != "null"]
        schema_type = schema_type[0] if schema_type else "string"
    
    if schema_type == "string":
        field_type = str
        if "format" in schema:
            # Could add format validation here
            pass
        if "pattern" in schema:
            field_kwargs["regex"] = schema["pattern"]
    elif schema_type == "integer":
        field_type = int
        if "minimum" in schema:
            field_kwargs["ge"] = schema["minimum"]
        if "maximum" in schema:
            field_kwargs["le"] = schema["maximum"]
    elif schema_type == "number":
        field_type = float
        if "minimum" in schema:
            field_kwargs["ge"] = schema["minimum"]
        if "maximum" in schema:
            field_kwargs["le"] = schema["maximum"]
    elif schema_type == "boolean":
        field_type = bool
    elif schema_type == "array":
        items = schema.get("items", {})
        if isinstance(items, dict):
            item_field = json_schema_to_field(items, required=False)
            field_type = List[item_field[0]]
        else:
            # Tuple type (array of schemas) - use Any for simplicity
            field_type = List[Any]
    elif schema_type == "object":
        # For objects, we could recursively create a nested model,
        # but for simplicity use Dict[str, Any]
        field_type = Dict[str, Any]
    
    # Handle description
    if "description" in schema:
        field_kwargs["description"] = schema["description"]
    
    # Handle default value
    if "default" in schema:
        field_kwargs["default"] = schema["default"]
    elif not required:
        field_kwargs["default"] = None
    
    return (field_type, Field(**field_kwargs))


def create_tool_class(
    server_name: str,
    tool_def: Dict[str, Any],
    client: MCPClientBase
) -> Type[ToolBase]:
    """Dynamically create a ToolBase subclass from an MCP tool definition.
    
    Args:
        server_name: Name of the MCP server (used for class naming)
        tool_def: MCP tool definition from tools/list response
        client: MCP client instance for making calls
        
    Returns:
        ToolBase subclass with execute() method that calls the MCP tool
    """
    tool_name = tool_def["name"]
    description = tool_def.get("description", f"MCP tool {tool_name} from {server_name}")
    # Support both 'inputSchema' (legacy) and 'parameters' (standard)
    input_schema = tool_def.get("inputSchema") or tool_def.get("parameters", {})
    
    # Build fields from input schema
    fields = {}
    properties = input_schema.get("properties", {})
    required_fields = input_schema.get("required", [])

    for prop_name, prop_schema in properties.items():
        required = prop_name in required_fields
        field_type, field = json_schema_to_field(prop_schema, required)
        fields[prop_name] = (field_type, field)    
    # Always prefix with MCP to avoid conflicts with native tools
    class_name = f"MCP{server_name}_{tool_name}".title().replace("_", "").replace("-", "")
    # Ensure class name is valid Python identifier
    class_name = ''.join(c for c in class_name if c.isalnum())
    
    # Create the model
    # Add tool field for schema compatibility
    # Use class_name as tool identifier for consistency with native tools
    tool_identifier = class_name
    fields["tool"] = (Literal[tool_identifier], Field(default=tool_identifier, description="MCP tool identifier"))
    
    ToolModel = create_model(class_name, __base__=ToolBase, **fields)
    
    # Add execute method that calls the MCP client
    def execute(self) -> str:
        """Execute the MCP tool by extracting arguments from self."""
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
            return f"Error calling MCP tool {tool_name}: {str(e)}"
    
    ToolModel.execute = execute
    
    # Add docstring
    ToolModel.__doc__ = description
    
    return ToolModel


class MCPServerManager:
    """Manager for MCP server lifecycle and tool registration.
    
    Responsibilities:
        - Load server configurations from MCPConfig
        - Start/stop MCP server subprocesses or connections
        - Dynamically generate ToolBase classes from MCP tool schemas
        - Register generated tools with the global TOOL_CLASSES list
        
    Attributes:
        config: MCPConfig with server definitions
        servers: Active MCP clients keyed by server name
        tool_classes: List of dynamically generated tool classes
    """
    
    def __init__(self, config: MCPConfig):
        self.config = config
        self.servers: Dict[str, MCPClientBase] = {}
        self.tool_classes: List[Type[ToolBase]] = []
        
    def start_all(self) -> None:
        """Start all configured servers and register their tools."""
        for server_config in self.config.servers:
            try:
                # Get API key from environment if specified
                api_key = None
                if server_config.api_key_env_var:
                    api_key = os.environ.get(server_config.api_key_env_var)
                    if not api_key:
                        logger.warning(
                            f"Server {server_config.name}: API key environment variable "
                            f"'{server_config.api_key_env_var}' not set"
                        )
                
                # Create client based on transport type
                client = create_mcp_client(
                    transport=server_config.transport.value,
                    command=server_config.command,
                    args=server_config.args,
                    env=server_config.env,
                    url=server_config.url,
                    headers=server_config.headers,
                    api_key=api_key
                )
                
                client.start()
                self.servers[server_config.name] = client
                
                tools = client.list_tools()
                logger.info(f"Server {server_config.name} ({server_config.transport}) provided {len(tools)} tools")
                
                for tool_def in tools:
                    tool_class = create_tool_class(server_config.name, tool_def, client)
                    self.tool_classes.append(tool_class)
                    
            except NotImplementedError as e:
                logger.warning(f"Skipping server {server_config.name}: {e}")
            except Exception as e:
                logger.error(f"Failed to start MCP server {server_config.name}: {e}")
                
    def stop_all(self) -> None:
        """Stop all running MCP server connections.
        
        graceful shutdown with appropriate transport-specific cleanup.
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

def register_mcp_tools(timeout: float = 5.0) -> None:
    """Register MCP-generated tool classes with the global TOOL_CLASSES.
    
    Called lazily from agent initialization (not during module import)
    to avoid hangs when MCP servers are unavailable. Uses a thread-based
    timeout to protect against misconfigured or unreachable servers.
    
    Args:
        timeout: Maximum seconds to wait for MCP servers to start.
                 Default 5.0 seconds. Set to 0 to skip entirely.
    """
    if timeout <= 0:
        logger.warning("register_mcp_tools: timeout <= 0, skipping MCP tool registration")
        return
    
    import concurrent.futures
    
    def _do_register():
        """Perform actual MCP tool registration."""
        manager = get_mcp_manager()
        from . import TOOL_CLASSES
        tool_classes = manager.get_tool_classes()
        count = 0
        for cls in tool_classes:
            if cls not in TOOL_CLASSES:
                TOOL_CLASSES.append(cls)
                count += 1
        return count
    
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(_do_register)
    try:
        count = future.result(timeout=timeout)
        logger.info(f"Registered {count} MCP tool(s)")
    except concurrent.futures.TimeoutError:
        logger.warning(f"MCP tool registration timed out after {timeout}s "
                      "- MCP servers may be unavailable")
    except Exception as e:
        logger.warning(f"MCP tool registration failed: {e}")
    finally:
        # Shutdown without waiting for hung worker
        executor.shutdown(wait=False)
