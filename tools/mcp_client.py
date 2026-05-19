"""
MCP Client - lightweight client for Model Context Protocol.

Supports multiple transports:
- stdio: JSON-RPC over stdin/stdout with subprocess
- http: JSON-RPC over HTTP POST
- sse: JSON-RPC over HTTP with Server-Sent Events (future)
"""
import json
import os
import subprocess
import sys
import threading
import queue
import time
from abc import ABC, abstractmethod
from typing import Dict, List, Any, Optional, Union, Callable, Tuple
import logging

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False
    logger = logging.getLogger(__name__)
    logger.warning("requests package not installed, HTTP/SSE transport will not be available")

try:
    import sseclient
    HAS_SSECLIENT = True
except ImportError:
    HAS_SSECLIENT = False

logger = logging.getLogger(__name__)


class MCPClientBase(ABC):
    """Abstract base class for MCP clients.
    
    Defines common interface for all transport types.
    
    Attributes:
        request_id: Monotonically increasing request ID
        pending_requests: Map of request ID to Queue for responses
        _lock: Thread lock for synchronization
        _shutdown: Flag indicating client is shutting down
    """
    
    def __init__(self):
        self.request_id = 0
        self.pending_requests: Dict[int, queue.Queue] = {}
        self._lock = threading.RLock()
        self._shutdown = False
    
    @abstractmethod
    def start(self) -> None:
        """Start the client and establish connection."""
        pass
    
    @abstractmethod
    def stop(self) -> None:
        """Stop the client and cleanup resources."""
        pass
    
    def _handle_message(self, msg: Dict[str, Any]) -> None:
        """Handle incoming JSON-RPC message."""
        if "id" in msg:
            request_id = msg["id"]
            with self._lock:
                q = self.pending_requests.pop(request_id, None)
            if q is not None:
                q.put(msg)
            else:
                logger.warning(f"Received response for unknown request id {request_id}")
        else:
            # Notification (e.g., logging)
            logger.info(f"Server notification: {msg}")
    
    def _send_request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Send a JSON-RPC request and wait for response.
        
        Args:
            method: JSON-RPC method name
            params: Optional parameters dict
            
        Returns:
            Response result dict
            
        Raises:
            TimeoutError: If response not received within timeout
            Exception: If server returns error response
        """
        with self._lock:
            request_id = self.request_id
            self.request_id += 1
        q = queue.Queue()
        with self._lock:
            self.pending_requests[request_id] = q
        
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {}
        }
        
        try:
            self._send_request_impl(request)
        except Exception as e:
            with self._lock:
                self.pending_requests.pop(request_id, None)
            raise Exception(f"Failed to send request: {e}")
        
        # Wait for response with timeout
        try:
            response = q.get(timeout=5.0)
        except queue.Empty:
            with self._lock:
                self.pending_requests.pop(request_id, None)
            raise TimeoutError(f"Timeout waiting for response to {method}")
        
        if "error" in response:
            error = response["error"]
            raise Exception(f"MCP error {error.get('code')}: {error.get('message')}")
        return response.get("result", {})
    
    @abstractmethod
    def _send_request_impl(self, request: Dict[str, Any]) -> None:
        """Transport-specific implementation to send a request."""
        pass
    
    def initialize(self) -> Dict[str, Any]:
        """Send initialize request to server.
        
        Returns:
            Initialize result dict
        """
        result = self._send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {
                "name": "ThoughtMachine Agent",
                "version": "1.0"
            }
        })
        logger.info(f"Initialized MCP server: {result.get('serverInfo', {})}")
        return result
    
    def list_tools(self) -> List[Dict[str, Any]]:
        """Request list of tools from server.
        
        Returns:
            List of tool definition dicts
        """
        result = self._send_request("tools/list")
        return result.get("tools", [])
    
    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """Call a tool with given arguments.
        
        Args:
            tool_name: Name of tool to call
            arguments: Tool arguments dict
            
        Returns:
            Tool result (concatenated text content)
        """
        result = self._send_request("tools/call", {
            "name": tool_name,
            "arguments": arguments
        })
        # MCP returns a list of content items (text, image, etc.)
        # For simplicity, concatenate text content
        content_items = result.get("content", [])
        texts = []
        for item in content_items:
            if item.get("type") == "text":
                texts.append(item.get("text", ""))
        return "\n".join(texts)
    
    def health_check(self) -> Tuple[bool, str]:
        """Check health of the MCP server connection.
        
        Returns:
            Tuple of (is_healthy, message)
        """
        try:
            # Try to list tools as a basic health check
            tools = self.list_tools()
            return (True, f"Healthy, {len(tools)} tools available")
        except Exception as e:
            return (False, f"Health check failed: {str(e)}")


class StdioMCPClient(MCPClientBase):
    """Lightweight JSON-RPC client for MCP over stdio transport.
    
    This client manages a subprocess running an MCP server and communicates
    via stdin/stdout using JSON-RPC 2.0 protocol.
    
    Attributes:
        command: Executable to run
        args: Command arguments
        env: Environment variables for subprocess
        process: Subprocess handle
        _reader_thread: Background thread for reading responses
    """
    
    def __init__(self, command: str, args: Optional[List[str]] = None, env: Optional[Dict[str, str]] = None):
        super().__init__()
        self.command = command
        self.args = args or []
        self.env = env
        self.process = None
        self._reader_thread = None
        
    def start(self):
        """Start the subprocess and reader thread."""
        if self.process is not None:
            raise RuntimeError("Client already started")
        
        env = os.environ.copy()
        if self.env:
            env.update(self.env)
        
        self.process = subprocess.Popen(
            [self.command] + self.args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line buffered
            env=env
        )
        
        self._shutdown = False
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()
        
        # Initialize the connection
        self.initialize()
    
    def stop(self):
        """Stop the client and subprocess."""
        self._shutdown = True
        if self.process:
            self.process.terminate()
            self.process.wait(timeout=5)
            self.process = None
        if self._reader_thread:
            self._reader_thread.join(timeout=2)
    
    def _send_request_impl(self, request: Dict[str, Any]) -> None:
        """Send JSON-RPC request over stdin."""
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("Client not started or stdin closed")
        json_str = json.dumps(request)
        self.process.stdin.write(json_str + "\n")
        self.process.stdin.flush()
    
    def _read_loop(self) -> None:
        """Read lines from stdout and dispatch responses."""
        while not self._shutdown and self.process.poll() is None:
            line = self.process.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                self._handle_message(msg)
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse JSON from server: {line} - {e}")
        logger.debug("Stdio reader thread exiting")
    
    def health_check(self) -> Tuple[bool, str]:
        """Check health of stdio subprocess."""
        if self.process is None:
            return (False, "Process not started")
        if self.process.poll() is not None:
            return (False, f"Process terminated with code {self.process.returncode}")
        
        # Try parent health check (list tools)
        return super().health_check()
    
    def __enter__(self):
        self.start()
        return self
    
    def __exit__(self, *args):
        self.stop()


class HTTPMCPClient(MCPClientBase):
    """HTTP transport for MCP using JSON-RPC over HTTP POST.
    
    Attributes:
        url: Server URL endpoint
        headers: HTTP headers to include
        session: requests Session for connection pooling
    """
    
    def __init__(self, url: str, headers: Optional[Dict[str, str]] = None, api_key: Optional[str] = None):
        super().__init__()
        self.url = url
        self.headers = headers or {}
        self.session = None
        
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"
        
        # Standard JSON-RPC headers
        self.headers.setdefault("Content-Type", "application/json")
        self.headers.setdefault("Accept", "application/json")
    
    def start(self) -> None:
        """Initialize HTTP session."""
        if not HAS_REQUESTS:
            raise ImportError("requests package required for HTTP transport")
        
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        
        # Initialize connection
        self.initialize()
    
    def stop(self) -> None:
        """Close HTTP session."""
        if self.session:
            self.session.close()
            self.session = None
    
    def _send_request_impl(self, request: Dict[str, Any]) -> None:
        """Send JSON-RPC request via HTTP POST and handle response synchronously."""
        if self.session is None:
            raise RuntimeError("HTTP session not initialized")
        
        try:
            response = self.session.post(self.url, json=request, timeout=30)
            response.raise_for_status()
            result = response.json()
            # Directly handle the response (bypass queue mechanism)
            self._handle_message(result)
        except requests.exceptions.RequestException as e:
            logger.error(f"HTTP request failed: {e}")
            raise
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON response: {e}")
            raise
    
    # Override _send_request to use synchronous HTTP (simpler)
    def _send_request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Override to use synchronous HTTP request/response."""
        with self._lock:
            request_id = self.request_id
            self.request_id += 1
        
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {}
        }
        
        if self.session is None:
            raise RuntimeError("HTTP session not initialized")
        
        try:
            response = self.session.post(self.url, json=request, timeout=30)
            response.raise_for_status()
            result = response.json()
        except requests.exceptions.RequestException as e:
            raise Exception(f"HTTP request failed: {e}")
        except json.JSONDecodeError as e:
            raise Exception(f"Failed to parse JSON response: {e}")
        
        if "error" in result:
            error = result["error"]
            raise Exception(f"MCP error {error.get('code')}: {error.get('message')}")
        
        return result.get("result", {})
    
    def health_check(self) -> Tuple[bool, str]:
        """Check health of HTTP connection."""
        if self.session is None:
            return (False, "HTTP session not initialized")
        
        # Try to ping the server with a simple request
        try:
            # Use a short timeout for health check
            response = self.session.get(self.url.replace("/rpc", "/health") if "/rpc" in self.url else self.url, timeout=5)
            if response.status_code == 200:
                return (True, "HTTP server responding")
        except:
            pass
        
        # Fall back to parent health check (list tools)
        return super().health_check()
    
    def __enter__(self):
        self.start()
        return self
    
    def __exit__(self, *args):
        self.stop()


class SSEMCPClient(MCPClientBase):
    """SSE transport for MCP (Server-Sent Events).
    
    NOTE: This is a placeholder implementation. Full SSE support
    requires more complex handling of bidirectional communication.
    """
    
    def __init__(self, url: str, headers: Optional[Dict[str, str]] = None, api_key: Optional[str] = None):
        super().__init__()
        self.url = url
        self.headers = headers or {}
        
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"
        
        logger.warning("SSE transport is not fully implemented")
    
    def start(self) -> None:
        """Initialize SSE connection."""
        raise NotImplementedError("SSE transport not yet implemented")
    
    def stop(self) -> None:
        """Stop SSE connection."""
        pass
    
    def _send_request_impl(self, request: Dict[str, Any]) -> None:
        """Send message over SSE transport."""
        raise NotImplementedError("SSE transport not yet implemented")
    
    # Override since we can't use the base implementation
    def _send_request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        raise NotImplementedError("SSE transport not yet implemented")
    
    def health_check(self) -> Tuple[bool, str]:
        """SSE health check."""
        return (False, "SSE transport not implemented")


# Factory function to create appropriate client based on transport
def create_mcp_client(
    transport: str,
    command: Optional[str] = None,
    args: Optional[List[str]] = None,
    env: Optional[Dict[str, str]] = None,
    url: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
    api_key: Optional[str] = None
) -> MCPClientBase:
    """Factory function to create MCP client based on transport type.
    
    Args:
        transport: Transport type ('stdio', 'http', or 'sse')
        command: Command for stdio transport
        args: Arguments for stdio transport
        env: Environment for stdio transport
        url: URL for HTTP/SSE transport
        headers: HTTP headers for HTTP/SSE transport
        api_key: API key for authentication
        
    Returns:
        Appropriate MCPClientBase instance
        
    Raises:
        ValueError: If invalid transport or missing required parameters
    """
    transport = transport.lower()
    
    if transport == "stdio":
        if not command:
            raise ValueError("command is required for stdio transport")
        return StdioMCPClient(command=command, args=args, env=env)
    
    elif transport == "http":
        if not url:
            raise ValueError("url is required for HTTP transport")
        return HTTPMCPClient(url=url, headers=headers, api_key=api_key)
    
    elif transport == "sse":
        if not url:
            raise ValueError("url is required for SSE transport")
        return SSEMCPClient(url=url, headers=headers, api_key=api_key)
    
    else:
        raise ValueError(f"Unsupported transport: {transport}")
