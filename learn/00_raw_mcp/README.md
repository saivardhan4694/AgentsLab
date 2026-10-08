# Milestone 0: MCP by hand

A complete MCP server and client in plain Python, with no SDK. Read these two files before the SDK hides the protocol.

```bash
uv run python learn/00_raw_mcp/raw_client.py
```

## What happens on the wire

Transport is **stdio**: the client starts the server as a subprocess. Each message is one line of JSON on stdin (client to server) or stdout (server to client). The server logs to stderr, because anything else on stdout corrupts the protocol.

Every message is **JSON-RPC 2.0**:

| Kind | Has `id` | Has `method` | Gets a reply |
|---|---|---|---|
| Request | yes | yes | yes |
| Response | yes (same as the request) | no | — |
| Notification | no | yes | never |

The session:

1. `initialize` (request). The client offers a `protocolVersion` and its capabilities. The server answers with the version it accepts, its capabilities (`tools`), and `serverInfo`.
2. `notifications/initialized` (notification). The client says it is ready.
3. `tools/list` (request). The server returns each tool's `name`, `description`, and `inputSchema` (JSON Schema). This is everything the LLM sees about a tool.
4. `tools/call` (request) with `name` and `arguments`. The server returns `content` (a list of text, image, or other blocks) and `isError`.

## Two kinds of errors

- **Protocol error** (JSON-RPC `error` object): the request is malformed, for example an unknown method or unknown tool. The host application handles it.
- **Tool error** (normal `result` with `isError: true`): the tool ran and failed, for example with bad argument values. The message goes back to the LLM, so the model can correct itself and retry.

This split matters for the Gateway: a policy denial is returned as a tool error, so the model learns that the action is forbidden and can choose another path.

## Proof of compliance

`tests/test_raw_mcp.py` connects the **official SDK client** to `raw_server.py` and calls its tools. If that test passes, the hand-written server speaks real MCP.
