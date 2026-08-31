import os
from fastmcp import Client
from fastmcp.server import create_proxy
from fastmcp.client.auth import BearerAuth

remote_client = Client(
    "https://normal-gray-dinosaur.fastmcp.app/mcp",
    auth=BearerAuth(token=os.environ["CLAUDE_DESKTOP_PROXY"]),
)

proxy_server = create_proxy(
    remote_client,
    name="Hamza Server Proxy",
)

if __name__ == "__main__":
    proxy_server.run()