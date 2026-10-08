"""The official SDK client must be able to talk to the hand-written server."""

import sys
from pathlib import Path

from mcp import Client, StdioServerParameters

RAW_SERVER = Path(__file__).parents[1] / "learn" / "00_raw_mcp" / "raw_server.py"


async def test_sdk_client_talks_to_raw_server():
    params = StdioServerParameters(command=sys.executable, args=[str(RAW_SERVER)])
    async with Client(params) as client:
        tools = (await client.list_tools()).tools
        assert [t.name for t in tools] == ["add", "echo", "utc_now"]

        ok = await client.call_tool("add", {"a": 2, "b": 40})
        assert not ok.is_error
        assert ok.content[0].text == "42"

        bad = await client.call_tool("add", {"a": "two", "b": 40})
        assert bad.is_error
