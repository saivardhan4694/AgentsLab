"""Downstream server registry.

Connects to every configured MCP server, merges their tools under `server__tool` names,
and routes calls back to the owning server. A server that fails to start is marked
unhealthy and skipped, so one broken server does not take the Gateway down.
"""

import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from mcp import Client

from agentlab.gateway.policy.matching import name_matches
from mcp.types import CallToolResult, TextContent, Tool

log = logging.getLogger(__name__)

SEPARATOR = "__"
META_RISK = "agentlab/risk"
META_WRITES = "agentlab/writes"


@dataclass
class ServerEntry:
    name: str
    target: Any  # anything `mcp.Client` accepts: StdioServerParameters, URL, or in-process server
    timeout: float | None = None
    risk: dict[str, str] = field(default_factory=dict)  # tool glob -> level; overrides annotations
    writes: dict[str, list[str]] = field(default_factory=dict)  # tool -> argument names of paths it changes
    trust_meta: bool = False  # first-party only: take risk and writes from the tool's `_meta`
    client: Client | None = None
    tools: dict[str, Tool] = field(default_factory=dict)  # keyed by the downstream (original) name
    error: str | None = None

    @property
    def healthy(self) -> bool:
        return self.client is not None and self.error is None


def tool_risk(entry: ServerEntry, tool: Tool) -> str:
    """Risk level of a tool: config override first, then MCP annotations.

    Annotations come from the server, so a third-party server can lie. Use overrides for those.
    A trusted (first-party) server can state its risk in `_meta["agentlab/risk"]`.
    Following the MCP spec defaults, a tool that is not read-only counts as destructive unless
    it says otherwise.
    """
    for pattern, level in entry.risk.items():
        if name_matches(pattern, tool.name):
            return level
    meta_risk = (tool.meta or {}).get(META_RISK) if entry.trust_meta else None
    if meta_risk in ("low", "medium", "high", "critical"):
        return meta_risk
    a = tool.annotations
    if a is not None and a.read_only_hint:
        return "low"
    if a is not None and a.destructive_hint is False:
        return "medium"
    return "high"


def tool_error(message: str) -> CallToolResult:
    # A tool error, not a protocol error: the message reaches the LLM so it can change course.
    return CallToolResult(content=[TextContent(type="text", text=message)], is_error=True)


class Registry:
    def __init__(self, servers: list[ServerEntry]):
        names = [s.name for s in servers]
        if len(names) != len(set(names)):
            raise ValueError(f"Duplicate server names: {names}")
        bad = [n for n in names if SEPARATOR in n]
        if bad:
            raise ValueError(f"Server names must not contain {SEPARATOR!r}: {bad}")
        self.servers = {s.name: s for s in servers}
        self._stack = AsyncExitStack()

    async def __aenter__(self) -> "Registry":
        await self._stack.__aenter__()
        for entry in self.servers.values():
            await self._connect(entry)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._stack.__aexit__(*exc)

    async def _connect(self, entry: ServerEntry) -> None:
        # Each server gets its own exit stack, so a failed connect cleans up only itself.
        stack = AsyncExitStack()
        try:
            client = await stack.enter_async_context(
                Client(entry.target, read_timeout_seconds=entry.timeout)
            )
            entry.tools = {t.name: t for t in await _list_all_tools(client)}
        except Exception as e:  # noqa: BLE001 - any startup failure marks the server unhealthy
            await stack.aclose()
            entry.error = f"{type(e).__name__}: {e}"
            log.warning("Server %s failed to start: %s", entry.name, entry.error)
            return
        entry.client = client
        self._stack.push_async_callback(stack.aclose)
        log.info("Server %s connected with %d tools", entry.name, len(entry.tools))

    def list_tools(self) -> list[Tool]:
        return [
            tool.model_copy(update={"name": f"{entry.name}{SEPARATOR}{tool.name}"})
            for entry in self.servers.values()
            if entry.healthy
            for tool in entry.tools.values()
        ]

    def resolve(self, name: str) -> tuple[ServerEntry, Tool] | None:
        server_name, sep, tool_name = name.partition(SEPARATOR)
        entry = self.servers.get(server_name)
        if not sep or entry is None or not entry.healthy or tool_name not in entry.tools:
            return None
        return entry, entry.tools[tool_name]

    async def call_tool(self, name: str, arguments: dict[str, Any] | None) -> CallToolResult:
        resolved = self.resolve(name)
        if resolved is None:
            return tool_error(f"Unknown tool: {name}")
        entry, tool = resolved
        assert entry.client is not None
        try:
            return await entry.client.call_tool(tool.name, arguments or {})
        except Exception as e:  # noqa: BLE001 - downstream failures become tool errors
            log.warning("Call %s failed: %s", name, e)
            return tool_error(f"Server {entry.name} failed: {type(e).__name__}: {e}")


async def _list_all_tools(client: Client) -> list[Tool]:
    tools: list[Tool] = []
    cursor: str | None = None
    while True:
        page = await client.list_tools(cursor=cursor)
        tools.extend(page.tools)
        cursor = page.next_cursor
        if cursor is None:
            return tools
