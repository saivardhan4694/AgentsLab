"""Generic MCP server that serves every tool from a folder of CLI manifests.

Each tool advertises its manifest risk and `writes` in `_meta` (`agentlab/risk`, `agentlab/writes`).
The Gateway trusts these only for servers marked `trust_meta: true` in servers.yaml.
"""

from pathlib import Path
from typing import Any

from mcp.server.lowlevel import Server
from mcp.types import CallToolRequestParams, CallToolResult, ListToolsResult, TextContent, Tool, ToolAnnotations

from agentlab.cli_bridge.manifest import ArgumentError, CliTool
from agentlab.shared.proc import minimal_env, run_process


def _error(message: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=message)], is_error=True)


def to_mcp(tool: CliTool) -> Tool:
    read_only = tool.risk == "low" and not tool.writes
    return Tool(
        name=tool.name,
        description=tool.description or f"Runs {tool.binary.name}",
        input_schema=tool.input_schema(),
        annotations=ToolAnnotations(read_only_hint=read_only, destructive_hint=tool.risk in ("high", "critical"),
                                    open_world_hint=True),
        meta={"agentlab/risk": tool.risk, "agentlab/writes": tool.writes},
    )


def create_server(tools: list[CliTool], roots: list[Path] | None = None) -> Server:
    by_name = {t.name: t for t in tools}
    roots = [r.expanduser().resolve() for r in roots or []]

    def inside_roots(p: Path) -> bool:
        return not roots or any(p == r or p.is_relative_to(r) for r in roots)

    async def list_tools(ctx: Any, params: Any) -> ListToolsResult:
        return ListToolsResult(tools=[to_mcp(t) for t in tools])

    async def call_tool(ctx: Any, params: CallToolRequestParams) -> CallToolResult:
        tool = by_name.get(params.name)
        if tool is None:
            return _error(f"Unknown tool: {params.name}")
        try:
            bound = tool.bind(params.arguments or {})
            cwd = tool.render_cwd(bound) or (roots[0] if roots else Path.home())
            argv = tool.render(bound)
        except ArgumentError as e:
            return _error(str(e))
        for name, arg in tool.args.items():
            if arg.type == "path" and bound[name] is not None and not inside_roots(Path(bound[name])):
                return _error(f"{name} is outside the allowed roots: {bound[name]}")
        if not inside_roots(cwd) or not cwd.is_dir():
            return _error(f"Working directory is outside the allowed roots or missing: {cwd}")
        result = await run_process(argv, cwd, minimal_env(tool.env), tool.timeout_s)
        result["argv"] = argv
        failed = result["exit_code"] != 0 or result["timed_out"]
        text = result["stdout"] if not failed else f"exit code {result['exit_code']}\n{result['stderr'] or result['stdout']}"
        return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=result, is_error=failed)

    return Server("agentlab-cli-bridge", version="0.1.0",
                  instructions="Installed command-line programs exposed as tools.",
                  on_list_tools=list_tools, on_call_tool=call_tool)
