import asyncio
from fastmcp import Client
from fastmcp.client.auth import BearerAuth

TOKEN = "fmcp_HjkKgX3maRcoWrO660SrRubsNam_4oNeNnnLtQ0aG5E"

async def main():
    print("Connecting with bearer token...")
    async with Client(
        "https://normal-gray-dinosaur.fastmcp.app/mcp",
        auth=BearerAuth(token=TOKEN),
    ) as client:
        tools = await client.list_tools()
        print(f"Success — got {len(tools)} tool(s):")
        for tool in tools:
            print(f"  - {tool.name}")

if __name__ == "__main__":
    asyncio.run(main())