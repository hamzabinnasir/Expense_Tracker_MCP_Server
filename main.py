import random
from fastmcp import FastMCP
mcp = FastMCP("Hamza Random Num Gen")
import json

@mcp.tool
def generate_random_number(min_val: int, max_val: int)-> int:
    """It will generate a random number between min_val and max_val"""
    return random.randint(min_val, max_val)

@mcp.tool
def add(a: int, b: int) -> int:
    """It will add two numbers"""
    return a+b

@mcp.resource("info://server")
def info() -> str:
    """Get Information about the server"""
    info = {
        "name": "Simple Calculator Server",
        "version": "1.0.0",
        "description": "A Basic MCP server with Math tools",
        "tools": ["generate_random_number", "add"],
        "author": "Hamza Bin Nasir",
    }

    return json.dumps(obj=info, indent=2)
if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000) # to run as a remote server