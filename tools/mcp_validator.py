"""
MCP Configuration Validator Tool

Validates MCP server configurations and tests connectivity.
Supports both single-server and multi-server configurations.
"""
import json
import os
import logging
import warnings
from typing import ClassVar, Dict, Any, List, Optional, Union, Literal
from pathlib import Path
from pydantic import Field

from .mcp_client import create_mcp_client
from .base import ToolBase

logger = logging.getLogger(__name__)

warnings.warn(
    "MCPValidator is deprecated. Use MCPServerConnect instead. "
    "MCPValidator will be removed in a future version.",
    DeprecationWarning,
    stacklevel=2,
)


class MCPValidator(ToolBase):
    """Validate MCP server configurations and test connectivity."""

    @classmethod
    def get_required_categories(cls, params: dict | None = None) -> list[str]:
        """Return network:outbound when testing HTTP/SSE connections."""
        if params:
            transport = params.get("transport", "")
            test_connection = params.get("test_connection", True)
            if transport in ("http", "sse") and test_connection:
                return ["network:outbound", "filesystem:read"]
        return ["filesystem:read"]

    tool: Literal["MCPValidator"] = "MCPValidator"    
    config_path: Optional[str] = Field(
        default=None,
        description="Path to MCP configuration JSON file (optional)"
    )
    transport: Optional[str] = Field(
        default=None,
        description="Transport type (stdio, http, sse). If provided, config_path is ignored.",
        pattern="^(stdio|http|sse)$"
    )
    command: Optional[str] = Field(
        default=None,
        description="Command for stdio transport"
    )
    args: Optional[List[str]] = Field(
        default=None,
        description="Arguments for stdio transport"
    )
    url: Optional[str] = Field(
        default=None,
        description="URL for HTTP/SSE transport"
    )
    api_key: Optional[str] = Field(
        default=None,
        description="API key for authentication"
    )
    test_connection: bool = Field(
        default=True,
        description="Whether to test connection (default: true)"
    )
    server_index: int = Field(
        default=0,
        description="For multi-server configs, index of server to test (default: 0)"
    )
    
    def _validate_config(self, config: Dict[str, Any]) -> List[str]:
        """Validate configuration structure and return list of errors."""
        errors = []
        
        # Handle multi-server format
        if "servers" in config:
            if not isinstance(config["servers"], list):
                errors.append("'servers' must be a list")
                return errors
            
            # Validate each server
            for i, server in enumerate(config["servers"]):
                if not isinstance(server, dict):
                    errors.append(f"Server {i} must be a dictionary")
                    continue
                
                server_errors = self._validate_single_config(server)
                for error in server_errors:
                    errors.append(f"Server {i} ({server.get('name', 'unnamed')}): {error}")
            
            return errors
        else:
            # Single server format
            return self._validate_single_config(config)
    
    def _validate_single_config(self, config: Dict[str, Any]) -> List[str]:
        """Validate a single server configuration."""
        errors = []
        
        # Check required fields
        if "transport" not in config:
            errors.append("Missing required field: transport")
        else:
            transport = config["transport"].lower()
            if transport not in ["stdio", "http", "sse"]:
                errors.append(f"Invalid transport: {transport}. Must be 'stdio', 'http', or 'sse'")
            
            # Check transport-specific required fields
            if transport == "stdio":
                if "command" not in config:
                    errors.append("stdio transport requires 'command' field")
            elif transport in ["http", "sse"]:
                if "url" not in config:
                    errors.append(f"{transport} transport requires 'url' field")
        
        # Validate optional fields
        if "args" in config and not isinstance(config["args"], list):
            errors.append("'args' must be a list")
        
        if "env" in config and not isinstance(config["env"], dict):
            errors.append("'env' must be a dictionary")
        
        if "headers" in config and not isinstance(config["headers"], dict):
            errors.append("'headers' must be a dictionary")
        
        if "name" in config and not isinstance(config["name"], str):
            errors.append("'name' must be a string")
        
        return errors
    
    def _load_config(self, config_path: str) -> Dict[str, Any]:
        """Load configuration from file."""
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")
        
        with open(path, 'r') as f:
            return json.load(f)
    
    def _extract_server_config(self, config: Dict[str, Any], server_index: int = 0) -> Dict[str, Any]:
        """Extract single server config from potentially multi-server config."""
        if "servers" in config:
            servers = config["servers"]
            if not isinstance(servers, list) or len(servers) == 0:
                raise ValueError("No servers found in configuration")
            if server_index >= len(servers):
                raise ValueError(f"Server index {server_index} out of range, only {len(servers)} servers available")
            return servers[server_index]
        else:
            return config
    
    def _create_test_client(self, config: Dict[str, Any]) -> Any:
        """Create MCP client from configuration."""
        transport = config["transport"].lower()
        
        if transport == "stdio":
            return create_mcp_client(
                transport="stdio",
                command=config["command"],
                args=config.get("args"),
                env=config.get("env")
            )
        elif transport == "http":
            return create_mcp_client(
                transport="http",
                url=config["url"],
                headers=config.get("headers"),
                api_key=config.get("api_key")
            )
        elif transport == "sse":
            return create_mcp_client(
                transport="sse",
                url=config["url"],
                headers=config.get("headers"),
                api_key=config.get("api_key")
            )
        else:
            raise ValueError(f"Unsupported transport: {transport}")
    
    def execute(self) -> str:
        """Validate MCP configuration and optionally test connection."""
        # Deprecated: MCPValidator is superseded by MCPServerConnect.
        return "Error: MCPValidator is deprecated. Use MCPServerConnect with a registered server name instead."
        # Get parameters from instance attributes
        config_path = self.config_path
        transport = self.transport
        server_index = self.server_index
        
        # Determine if we're using file config or direct parameters
        if config_path:
            try:
                config = self._load_config(config_path)
            except json.JSONDecodeError as e:
                return json.dumps({
                    "valid": False,
                    "errors": [f"Invalid JSON: {str(e)}"],
                    "warnings": [],
                    "connection_tested": False
                }, indent=2)
            except Exception as e:
                return json.dumps({
                    "valid": False,
                    "errors": [f"Failed to load config: {str(e)}"],
                    "warnings": [],
                    "connection_tested": False
                }, indent=2)
            
            # For multi-server configs, extract the specified server
            try:
                server_config = self._extract_server_config(config, server_index)
                is_multi_server = "servers" in config
                if is_multi_server:
                    server_name = server_config.get("name", f"server_{server_index}")
                    config_summary_note = f" (server {server_index}: {server_name} from multi-server config)"
                else:
                    config_summary_note = ""
            except Exception as e:
                return json.dumps({
                    "valid": False,
                    "errors": [f"Failed to extract server config: {str(e)}"],
                    "warnings": [],
                    "connection_tested": False
                }, indent=2)
        elif transport:
            # Build config from direct parameters
            server_config = {"transport": transport}
            if transport == "stdio":
                if self.command:
                    server_config["command"] = self.command
                if self.args:
                    server_config["args"] = self.args
            elif transport in ["http", "sse"]:
                if self.url:
                    server_config["url"] = self.url
                if hasattr(self, 'headers'):  # Note: headers not in schema, would need to be added
                    server_config["headers"] = getattr(self, 'headers', {})
                if self.api_key:
                    server_config["api_key"] = self.api_key
            config_summary_note = ""
            is_multi_server = False
        else:
            return json.dumps({
                "valid": False,
                "errors": ["Either config_path or transport must be provided"],
                "warnings": [],
                "connection_tested": False
            }, indent=2)
        
        # Validate configuration
        if is_multi_server:
            # For multi-server, validate the entire config
            errors = self._validate_config(config)
        else:
            # For single server, validate just the server config
            errors = self._validate_config(server_config)
        
        warnings = []
        
        # Check for missing optional but recommended fields
        transport_type = server_config["transport"].lower()
        if transport_type == "http" and "api_key" not in server_config:
            warnings.append("No API key provided for HTTP transport (may be required for authentication)")
        
        if transport_type == "stdio" and "env" not in server_config:
            warnings.append("No environment variables provided for stdio transport")
        
        # If config has errors, return early
        if errors:
            return json.dumps({
                "valid": False,
                "errors": errors,
                "warnings": warnings,
                "connection_tested": False
            }, indent=2)
        
        # Test connection if requested
        connection_tested = self.test_connection
        connection_result = None
        
        if connection_tested and transport_type != "sse":  # SSE not implemented
            try:
                client = self._create_test_client(server_config)
                client.start()
                
                # Try health check first
                is_healthy, health_msg = client.health_check()
                
                if is_healthy:
                    # Try to list tools for more detailed validation
                    try:
                        tools = client.list_tools()
                        connection_result = {
                            "success": True,
                            "health": health_msg,
                            "tools_count": len(tools),
                            "tools": [tool.get("name") for tool in tools[:10]],  # First 10 tools
                            "message": f"Connection successful: {health_msg}"
                        }
                    except Exception as e:
                        # Health check passed but tool listing failed
                        connection_result = {
                            "success": True,
                            "health": health_msg,
                            "tools_count": 0,
                            "tools": [],
                            "message": f"Connection established but tool listing failed: {str(e)}"
                        }
                else:
                    connection_result = {
                        "success": False,
                        "health": health_msg,
                        "tools_count": 0,
                        "tools": [],
                        "message": f"Health check failed: {health_msg}"
                    }
                
                client.stop()
                
            except Exception as e:
                connection_result = {
                    "success": False,
                    "health": "Failed",
                    "tools_count": 0,
                    "tools": [],
                    "message": f"Connection test failed: {str(e)}"
                }
        
        # Return validation results
        result = {
            "valid": True,
            "errors": [],
            "warnings": warnings,
            "config_summary": {
                "transport": server_config["transport"],
                "transport_type": transport_type,
                "is_multi_server": is_multi_server,
                "note": config_summary_note
            }
        }
        
        # Add transport-specific summary
        if transport_type == "stdio":
            result["config_summary"]["command"] = server_config.get("command")
            result["config_summary"]["args"] = server_config.get("args", [])
        elif transport_type in ["http", "sse"]:
            result["config_summary"]["url"] = server_config.get("url")
            result["config_summary"]["has_api_key"] = "api_key" in server_config
            result["config_summary"]["has_headers"] = "headers" in server_config
        
        # Add server name if available
        if "name" in server_config:
            result["config_summary"]["server_name"] = server_config["name"]
        
        if connection_result:
            result["connection_tested"] = True
            result["connection_result"] = connection_result
        else:
            result["connection_tested"] = False
        
        return json.dumps(result, indent=2)


# Register tool
def register_tool():
    return MCPValidator()
