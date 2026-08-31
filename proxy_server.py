from fastmcp import FastMCP
proxy_server = FastMCP.as_proxy(
    "https://normal-gray-dinosaur.fastmcp.app/mcp",
    name="Hamza Server Proxy"
)

if __name__ == "__main__":
    proxy_server.run() # local server so thats' why, we are not giving transport="http", port and host