"""Gateway MCP endpoint.

Upstream clients see one MCP server whose tools are the merged tools of every healthy
downstream server, filtered by the client's profile. Every call goes through the pipeline
in pipeline.py: policy, approvals, snapshots, audit.

stdio (Claude Desktop):  python -m agentlab.gateway.server --config <repo>/config/servers.yaml --profile coding
HTTP (any client):       python -m agentlab.gateway.server --http --config <repo>/config/servers.yaml
"""

import argparse
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolRequestParams, CallToolResult, ListToolsResult, PaginatedRequestParams

from agentlab.gateway.approvals import ApprovalStore
from agentlab.gateway.audit import AuditLog
from agentlab.gateway.config import load_servers
from agentlab.gateway.pipeline import APPROVAL_TIMEOUT_S, Gateway
from agentlab.gateway.policy.engine import PolicyEngine, load_profiles
from agentlab.gateway.registry import Registry
from agentlab.gateway.snapshots import SnapshotStore


def create_server(gateway: Gateway | Callable[[Any], Gateway]) -> Server:
    """`gateway` is one pipeline (stdio), or a function that picks the caller's pipeline from the request (HTTP)."""
    pick = gateway if callable(gateway) else (lambda ctx: gateway)

    async def list_tools(ctx, params: PaginatedRequestParams | None) -> ListToolsResult:
        return ListToolsResult(tools=pick(ctx).list_tools())

    async def call_tool(ctx, params: CallToolRequestParams) -> CallToolResult:
        meta = params.meta if isinstance(params.meta, dict) else None
        return await pick(ctx).call_tool(params.name, params.arguments, meta)

    return Server(
        "agentlab-gateway",
        version="0.1.0",
        instructions="AgentLab Gateway. Tools are named server__tool. Some calls wait for human approval.",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


async def serve_stdio(config: Path, profiles: Path, profile: str, approval_timeout: float) -> None:
    available = load_profiles(profiles)
    if profile not in available:
        raise SystemExit(f"Unknown profile {profile!r}. Available: {', '.join(sorted(available))}")
    engine = PolicyEngine(available[profile])
    async with Registry(load_servers(config)) as registry:
        gateway = Gateway(registry, engine, ApprovalStore(), SnapshotStore(), approval_timeout, AuditLog())
        logging.getLogger(__name__).info("Session %s, profile %s", gateway.session_id, profile)
        server = create_server(gateway)
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())


def main() -> None:
    parser = argparse.ArgumentParser(description="AgentLab MCP Gateway")
    parser.add_argument("--config", type=Path, default=Path("config/servers.yaml"))
    parser.add_argument("--profiles", type=Path, help="profiles folder (default: profiles/ next to --config)")
    parser.add_argument("--approval-timeout", type=float, default=APPROVAL_TIMEOUT_S, help="seconds; then deny")
    parser.add_argument("--profile", default="readonly", help="stdio mode: the profile for the one client")
    parser.add_argument("--http", action="store_true", help="serve Streamable HTTP instead of stdio")
    parser.add_argument("--clients", type=Path, help="HTTP mode: clients.yaml (default: next to --config)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    config = args.config.resolve()
    profiles = (args.profiles or config.parent / "profiles").resolve()
    # In stdio mode stdout carries the protocol, so logs always go to stderr.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    if args.http:
        from agentlab.gateway.http import serve_http

        clients = (args.clients or config.parent / "clients.yaml").resolve()
        anyio.run(serve_http, config, profiles, clients, args.host, args.port, args.approval_timeout)
    else:
        anyio.run(serve_stdio, config, profiles, args.profile, args.approval_timeout)


if __name__ == "__main__":
    main()
