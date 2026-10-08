"""A minimal MCP client written by hand, with no SDK.

It launches raw_server.py as a subprocess and walks through the protocol,
printing every message sent (>>>) and received (<<<).

Run:  uv run python learn/00_raw_mcp/raw_client.py
"""

import itertools
import json
import subprocess
import sys
from pathlib import Path

SERVER = Path(__file__).with_name("raw_server.py")


class RawClient:
    def __init__(self, command):
        # stderr is inherited, so server logs show up in this terminal.
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.ids = itertools.count(1)

    def _write(self, message):
        print(">>>", json.dumps(message))
        self.proc.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
        self.proc.stdin.flush()

    def request(self, method, params=None):
        msg_id = next(self.ids)
        self._write({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})
        # This toy client handles one request at a time, so the next line is our answer.
        # A real client matches responses to requests by "id", because they can arrive out of order.
        response = json.loads(self.proc.stdout.readline())
        print("<<<", json.dumps(response))
        assert response["id"] == msg_id
        return response

    def notify(self, method, params=None):
        # Notifications have no "id" and get no response.
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=5)


def step(title):
    print(f"\n=== {title} ===")


def main():
    client = RawClient([sys.executable, str(SERVER)])

    step("1. Handshake: initialize")
    client.request(
        "initialize",
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "raw-client", "version": "0.1.0"},
        },
    )

    step("2. Tell the server we are ready (notification, no response)")
    client.notify("notifications/initialized")

    step("3. Discover tools")
    tools = client.request("tools/list")["result"]["tools"]
    print("tools:", [t["name"] for t in tools])

    step("4. Call a tool")
    client.request("tools/call", {"name": "add", "arguments": {"a": 2, "b": 40}})

    step("5. Tool failure: bad arguments give a normal result with isError=true")
    client.request("tools/call", {"name": "add", "arguments": {"a": "two", "b": 40}})

    step("6. Protocol error: unknown tool gives a JSON-RPC error")
    client.request("tools/call", {"name": "rm_rf", "arguments": {}})

    step("7. Protocol error: unknown method")
    client.request("resources/list")

    client.close()


if __name__ == "__main__":
    main()
