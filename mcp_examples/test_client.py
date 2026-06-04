#!/usr/bin/env python3
"""
Test client for the Internet Search MCP server.
Properly implements MCP initialization handshake.
"""

import subprocess
import json
import sys
import os

def send_json(proc, obj):
    """Send a JSON object to the server (newline delimited)."""
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()

def read_response(proc):
    """Read a single JSON-RPC response from the server."""
    line = proc.stdout.readline()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON response: {e}", file=sys.stderr)
        return None

def perform_handshake(proc):
    """Perform MCP initialization handshake."""
    # 1. Send initialize request
    init_req = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {
                "name": "test_client",
                "version": "1.0.0"
            }
        }
    }
    send_json(proc, init_req)
    
    # 2. Wait for initialize response
    init_resp = read_response(proc)
    if not init_resp or 'result' not in init_resp:
        error = init_resp.get('error') if init_resp else 'No response'
        print(f"Initialize failed: {error}")
        return False
    
    server_info = init_resp['result'].get('serverInfo', {})
    print(f"  Server: {server_info.get('name', 'unknown')} v{server_info.get('version', '?')}")
    
    # 3. Send initialized notification
    init_notif = {
        "jsonrpc": "2.0",
        "method": "notifications/initialized"
    }
    send_json(proc, init_notif)
    
    return True

def test_list_tools():
    """Test the tools/list method."""
    print("\n=== Testing tools/list ===")
    proc = subprocess.Popen(
        [sys.executable, 'internet_search_server.py'],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1
    )
    
    try:
        if not perform_handshake(proc):
            return False
        
        # 4. Send tools/list request
        list_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list"
        }
        send_json(proc, list_req)
        
        # 5. Read response
        list_resp = read_response(proc)
        if not list_resp:
            print("Error: No response to tools/list")
            return False
        
        if 'result' in list_resp:
            tools = list_resp['result'].get('tools', [])
            print(f"Found {len(tools)} tool(s):")
            print(f"DEBUG: tools = {tools}")  # Debug output
            for tool in tools:
                # Tool might be a dict or a Tool object with attributes
                if isinstance(tool, dict):
                    print(f"- {tool.get('name')}: {tool.get('description')}")
                    schema = tool.get('inputSchema', {})
                else:
                    # Assuming it's a Tool object/BaseModel
                    print(f"- {getattr(tool, 'name', 'unknown')}: {getattr(tool, 'description', 'no desc')}")
                    schema = getattr(tool, 'inputSchema', {})
                if schema:
                    print(f"  Parameters: {schema}")
            return True
        else:
            error = list_resp.get('error', 'Unknown error')
            print(f"Error: {error}")
            return False
            
    finally:
        proc.terminate()
        proc.wait()

def test_call_tool(query, max_results=5):
    """Test the internet_search tool."""
    print(f"\n=== Testing internet_search ===")
    print(f"Query: {query}")
    print(f"Max results: {max_results}")
    
    proc = subprocess.Popen(
        [sys.executable, 'internet_search_server.py'],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1
    )
    
    try:
        if not perform_handshake(proc):
            return False
        
        # Call the tool
        call_req = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "internet_search",
                "arguments": {
                    "query": query,
                    "max_results": max_results
                }
            }
        }
        send_json(proc, call_req)
        
        # Read response
        call_resp = read_response(proc)
        if not call_resp:
            print("Error: No response to tools/call")
            return False
        
        if 'result' in call_resp:
            result = call_resp['result']
            print("\n--- Search Results ---")
            if 'content' in result:
                for content in result['content']:
                    if content.get('type') == 'text':
                        print(content['text'])
            else:
                print(f"Unexpected result format: {result}")
            print("---------------------")
            return True
        else:
            error = call_resp.get('error', 'Unknown error')
            print(f"Error: {error}")
            return False
            
    finally:
        proc.terminate()
        proc.wait()

def main():
    """Run tests."""
    print("=" * 50)
    print("Internet Search MCP Server - Test Client")
    print("=" * 50)
    
    # Check if server file exists
    server_file = os.path.join(os.path.dirname(__file__), 'internet_search_server.py')
    if not os.path.exists(server_file):
        print(f"Error: Server file not found: {server_file}")
        print("Make sure you're running this from the mcp_examples directory.")
        sys.exit(1)
    
    # Test 1: List tools
    if not test_list_tools():
        print("\n❌ tools/list test FAILED")
        sys.exit(1)
    
    # Test 2: Perform a search
    query = input("\nEnter a search query (or press Enter for default 'Python programming'): ").strip()
    if not query:
        query = "Python programming"
    
    max_results = input("Max results (1-10, default 5): ").strip()
    max_results = int(max_results) if max_results.isdigit() else 5
    
    if not test_call_tool(query, max_results):
        print("\n❌ tools/call test FAILED")
        sys.exit(1)
    
    print("\n" + "=" * 50)
    print("✅ All tests PASSED!")
    print("=" * 50)
    print("\nYour MCP server is working!")
    print("Now add it to your ThoughtMachine GUI using the instructions in README.md")

if __name__ == "__main__":
    main()
