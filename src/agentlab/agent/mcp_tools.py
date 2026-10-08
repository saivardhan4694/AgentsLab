"""MCP tools as LangChain tools.

`langchain-mcp-adapters` 0.3 still imports MCP SDK v1 modules, so this is a small adapter for
SDK v2. Each MCP tool becomes a `StructuredTool` whose argument schema is the tool's JSON Schema.
An MCP tool error (`is_error`) becomes a `ToolException`, which LangChain returns to the model as
an error message, so a policy denial reaches the model and it can choose another way.
"""

import json
from contextvars import ContextVar
from typing import Any

from langchain_core.tools import StructuredTool, ToolException
from mcp import Client
from mcp.types import CallToolResult, Tool

# The agent run in progress. Sent with each call so the Gateway audit can be joined to the trace.
current_run: ContextVar[str | None] = ContextVar("agentlab_current_run", default=None)


def result_text(result: CallToolResult) -> str:
    parts = [c.text for c in result.content if getattr(c, "type", None) == "text"]
    if parts:
        return "\n".join(parts)
    if result.structured_content is not None:
        return json.dumps(result.structured_content)
    return "(no output)"


def to_langchain(client: Client, tool: Tool) -> StructuredTool:
    async def call(**arguments: Any) -> str:
        run_id = current_run.get()
        meta = {"agentlab/run_id": run_id} if run_id else None
        result = await client.call_tool(tool.name, arguments, meta=meta)
        text = result_text(result)
        if result.is_error:
            raise ToolException(text)
        return text

    return StructuredTool(
        name=tool.name,
        description=tool.description or tool.name,
        args_schema=tool.input_schema,
        coroutine=call,
        handle_tool_error=True,
        metadata={"annotations": tool.annotations.model_dump() if tool.annotations else None},
    )


async def load_tools(client: Client) -> list[StructuredTool]:
    tools: list[Tool] = []
    cursor = None
    while True:
        page = await client.list_tools(cursor=cursor)
        tools.extend(page.tools)
        cursor = page.next_cursor
        if cursor is None:
            break
    return [to_langchain(client, t) for t in tools]
