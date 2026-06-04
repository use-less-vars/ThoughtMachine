#!/usr/bin/env python3
"""
Test the internet search MCP server using the project's own MCP client.
This properly handles the MCP initialization handshake.
"""

import sys
import os
from pathlib import Path

# Add parent directory to path to import tools
sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
from tools.mcp_client import create_mcp_client

async def test():
    print("=" * 50)
    print("Testing Internet Search MCP Server")
    print("=" * 50)
    
    server_script = Path(__file__).parent / "internet_search_server.py"
    
    # Create MCP client using stdio transport
    client = create_mcp_client(
        transport="stdio",
        command=sys.executable,
        args=[str(server_script)],
        env={}
    )
    client.start()
    
    try:
        # List tools
        print("\n1. Listing tools...")
        tools = client.list_tools()
        print(f"Found {len(tools)} tool(s):")
        for tool in tools:
            print(f"  - {tool.get('name')}: {tool.get('description')}")
            schema = tool.get('inputSchema', {})
            if schema:
                print(f"    Parameters: {schema}")
        
        # Call the search tool
        print("\n2. Testing internet_search tool...")
        query = input("Enter a search query (or press Enter for 'Python asyncio'): ").strip()
        if not query:
            query = "Python asyncio"
        
        result = client.call_tool("internet_search", {"query": query, "max_results": 3})
        
        print("\n--- Search Results ---")
        # result can be different types; handle accordingly
        if isinstance(result, list):
            for item in result:
                if hasattr(item, 'text'):
                    print(item.text)
                elif isinstance(item, dict):
                    print(item.get('text', str(item)))
        elif hasattr(result, 'content'):
            for content in result.content:
                if hasattr(content, 'text'):
                    print(content.text)
        else:
            print(result)
        print("---------------------")
        
        print("\n✅ Test completed successfully!")
        
    finally:
        client.stop()

if __name__ == "__main__":
    asyncio.run(test())
