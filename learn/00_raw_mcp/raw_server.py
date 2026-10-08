"""A minimal MCP server written by hand, with no SDK.

Transport: stdio. Each message is one line of JSON (JSON-RPC 2.0) on stdin/stdout.
stdout is reserved for protocol messages, so all logging goes to stderr.

Run it through the client:  uv run python learn/00_raw_mcp/raw_client.py
"""

import json
import sys
from datetime import datetime, timezone

SUPPORTED_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26"]

# JSON-RPC error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


def log(*args):
    print("[raw_server]", *args, file=sys.stderr, flush=True)


# --- Tools -------------------------------------------------------------------
# A tool is a name, a description, and a JSON Schema for its arguments.
# The LLM reads the description and schema to decide when and how to call it.

TOOLS = [
    {
        "name": "add",
        "description": "Add two numbers.",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "echo",
        "description": "Return the given text unchanged.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "utc_now",
        "description": "Return the current UTC time in ISO 8601 format.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
]


def run_tool(name, args):
    """Execute a tool. Raise ValueError for bad arguments."""
    if name == "add":
        a, b = args.get("a"), args.get("b")
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            raise ValueError("'a' and 'b' must be numbers")
        return str(a + b)
    if name == "echo":
        if not isinstance(args.get("text"), str):
            raise ValueError("'text' must be a string")
        return args["text"]
    if name == "utc_now":
        return datetime.now(timezone.utc).isoformat()
    raise KeyError(name)


# --- Method handlers ---------------------------------------------------------


def handle_initialize(params):
    # Version negotiation: echo the client's version if we support it,
    # otherwise answer with our newest one and let the client decide.
    requested = params.get("protocolVersion")
    version = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "raw-server", "version": "0.1.0"},
        "instructions": "Toy server for learning the MCP wire protocol.",
    }


def handle_tools_list(params):
    return {"tools": TOOLS}


def handle_tools_call(params):
    name = params.get("name")
    args = params.get("arguments") or {}
    if name not in {t["name"] for t in TOOLS}:
        # Unknown tool is a protocol error: the request itself is wrong.
        raise RpcError(INVALID_PARAMS, f"Unknown tool: {name}")
    try:
        text = run_tool(name, args)
    except ValueError as e:
        # A failed tool run is NOT a protocol error. It is a normal result with
        # isError=true, so the LLM can read the message and try again.
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    return {"content": [{"type": "text", "text": text}], "isError": False}


HANDLERS = {
    "initialize": handle_initialize,
    "ping": lambda params: {},
    "tools/list": handle_tools_list,
    "tools/call": handle_tools_call,
}


class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# --- Main loop ---------------------------------------------------------------


def send(message):
    sys.stdout.buffer.write(json.dumps(message).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def error_response(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle_line(line):
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        return error_response(None, PARSE_ERROR, "Parse error")
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        # Responses from the client (to server-initiated requests) also land here.
        # This server never sends requests, so it ignores them.
        if isinstance(msg, dict) and ("result" in msg or "error" in msg):
            return None
        return error_response(msg.get("id") if isinstance(msg, dict) else None, INVALID_REQUEST, "Invalid request")

    method = msg["method"]
    # A message without "id" is a notification: never send a response to it.
    if "id" not in msg:
        log("notification:", method)
        return None

    handler = HANDLERS.get(method)
    if handler is None:
        return error_response(msg["id"], METHOD_NOT_FOUND, f"Method not found: {method}")
    try:
        result = handler(msg.get("params") or {})
    except RpcError as e:
        return error_response(msg["id"], e.code, e.message)
    return {"jsonrpc": "2.0", "id": msg["id"], "result": result}


def main():
    log("started")
    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8").strip()
        if not line:
            continue
        response = handle_line(line)
        if response is not None:
            send(response)
    log("stdin closed, exiting")


if __name__ == "__main__":
    main()
