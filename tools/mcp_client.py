"""
MCP Client - lightweight client for Model Context Protocol.
"""
import json
import os
import subprocess
import sys
import threading
import queue
import time
from typing import Dict, List, Any, Optional, Union, Callable
import logging

logger = logging.getLogger(__name__)

class StdioMCPClient:
    """Lightweight JSON-RPC client for MCP over stdio transport.

This client manages a subprocess running an MCP server and communicates
via stdin/stdout using JSON-RPC 2.0 protocol.

Attributes:
    command: Executable to run
    args: Command arguments
    env: Environment variables for subprocess
    process: Subprocess handle
    request_id: Monotonically increasing request ID
    pending_requests: Map of request ID to Queue for responses
    _lock: Thread lock for synchronization
    _reader_thread: Background thread for reading responses
    _shutdown: Flag indicating client is shutting down
"""
    
    def __init__(self, command: str, args: Optional[List[str]] = None, env: Optional[Dict[str, str]] = None):
        self.command = command
        self.args = args or []
        self.env = env
        self.process = None
        self.request_id = 0
        self.pending_requests: Dict[int, queue.Queue] = {}
        self._lock = threading.RLock()
        self._reader_thread = None
        self._shutdown = False
        
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
        
    def _read_loop(self):
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
        logger.debug("Reader thread exiting")
        
    def _handle_message(self, msg: Dict[str, Any]):
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
        """Send a JSON-RPC request and wait for response."""
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
        json_str = json.dumps(request)
        self.process.stdin.write(json_str + "\n")
        self.process.stdin.flush()
        # Wait for response with timeout
        try:
            response = q.get(timeout=30.0)
        except queue.Empty:
            with self._lock:
                self.pending_requests.pop(request_id, None)
            raise TimeoutError(f"Timeout waiting for response to {method}")
        if "error" in response:
            error = response["error"]
            raise Exception(f"MCP error {error.get('code')}: {error.get('message')}")
        return response.get("result")
        
    def initialize(self):
        """Send initialize request."""
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
        """Request list of tools from server."""
        result = self._send_request("tools/list")
        return result.get("tools", [])
        
    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """Call a tool with given arguments."""
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
        
    def stop(self):
        """Stop the client and subprocess."""
        self._shutdown = True
        if self.process:
            self.process.terminate()
            self.process.wait(timeout=5)
            self.process = None
        if self._reader_thread:
            self._reader_thread.join(timeout=2)
            
    def __enter__(self):
        self.start()
        return self
        
    def __exit__(self, *args):
        self.stop()

